"""Benchmark runner — chạy pipeline A1→A2→A3→A4→Rego→A5 trên dataset CSV.

Chạy:
    uv run python3 benchmark_pipeline.py
    uv run python3 benchmark_pipeline.py --limit 5
    uv run python3 benchmark_pipeline.py --cases 0 3 7-10
    uv run python3 benchmark_pipeline.py --no-secu            # bỏ qua A2, security_ckv_ids = {}
    uv run python3 benchmark_pipeline.py --no-rego            # bỏ qua Rego intent
    uv run python3 benchmark_pipeline.py --no-deploy          # dừng sau A4
    uv run python3 benchmark_pipeline.py --no-destroy         # giữ lại resources sau apply
    uv run python3 benchmark_pipeline.py --workers 3          # chạy 3 case song song
    uv run python3 benchmark_pipeline.py --out reviews/pipeline_results.json
"""
import argparse
import csv
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from agents.architecture import archi_node
from agents.security import secu_node
from agents.engineering import engi_node
from agents.validation import validation_node, route_after_validation
from agents.deployment import deployment_node, route_after_deployment
from core.terraform import run_rego_intent_on_hcl

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)

CSV_PATH = ROOT / "dataset" / "data-dev.csv"
_RESOURCE_RE = re.compile(r'resource\s+"[^"]+"\s+"[^"]+"')
_HCL_DECL_RE = re.compile(r'(?m)^\s*(?:resource|data)\s+"([^"]+)"\s+"([^"]+)"')
_PRINT_LOCK = threading.Lock()
_SOFT_RESOURCE_TYPES = {
    "archive_file",
    "random_id",
    "random_string",
    "random_password",
    "tls_private_key",
    "aws_availability_zones",
    "aws_caller_identity",
    "aws_iam_policy_document",
    "aws_partition",
    "aws_region",
}

# Giới hạn vòng retry trong test — khớp RECURSION_LIMIT / ~4 node/cycle
MAX_ITERATIONS = 20


def make_state(prompt: str, idx: int = 0, auto_destroy: bool = True) -> dict:
    run_dir = ROOT / "tmp" / f"row_{idx}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return {
        "prompt": prompt,
        "auto_destroy": auto_destroy,
        "terraform_plan_timeout": int(os.environ.get("TF_PLAN_TIMEOUT", "120")),
        "infrastructure_plan": {},
        "security_ckv_ids": {},
        "generated_code": "",
        "fix_feedback": {},
        "deployment_result": {},
        "arch_retry_count": 0,
        "sec_retry_count": 0,
        "eng_retry_count": 0,
        "total_retry_count": 0,
        "deploy_retry_count": 0,
        "deploy_eng_retry_count": 0,
        "error_history": [],
        "arch_error_history": [],
        "sec_error_history": [],
        "eng_error_history": [],
        "routing_log": [],
        "run_dir": str(run_dir),
    }


def load_csv(csv_path: Path, limit: int | None) -> list[dict]:
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    if limit:
        rows = rows[:limit]
    return [
        {
            "idx": i,
            "difficulty": row.get("Difficulty", ""),
            "prompt": row["Prompt"],
            "resources": row.get("Resource") or row.get("esource") or "",
            "rego_intent": row.get("Rego intent", ""),
            "reference_output": row.get("Reference output", ""),
            "intent": row.get("Intent", ""),
        }
        for i, row in enumerate(rows)
    ]


def _parse_cases(tokens: list[str]) -> set[int]:
    result = set()
    for part in tokens:
        if "-" in part:
            lo, hi = part.split("-", 1)
            result.update(range(int(lo), int(hi) + 1))
        else:
            result.add(int(part))
    return result


def _n_resources(code: str) -> int:
    return len(_RESOURCE_RE.findall(code))


def _type_counter_from_field(resource_field: str) -> Counter:
    return Counter(r.strip() for r in (resource_field or "").split(",") if r.strip())


def _type_counter_from_hcl(hcl: str) -> Counter:
    return Counter(t for t, _ in _HCL_DECL_RE.findall(hcl or ""))


def _split_hard_soft(counter: Counter) -> tuple[Counter, Counter]:
    hard = Counter()
    soft = Counter()
    for resource_type, count in counter.items():
        if resource_type in _SOFT_RESOURCE_TYPES:
            soft[resource_type] = count
        else:
            hard[resource_type] = count
    return hard, soft


def _coverage(actual: Counter, expected: Counter) -> dict:
    matched = sum((actual & expected).values())
    total = sum(expected.values())
    missing = sorted((expected - actual).elements())
    extra = sorted((actual - expected).elements())
    return {
        "ok": not missing,
        "matched": matched,
        "expected_count": total,
        "actual_count": sum(actual.values()),
        "coverage_pct": round(100 * matched / total, 1) if total else 100.0,
        "missing": missing,
        "extra": extra,
    }


def _contains_literal(code: str, literal: str) -> bool:
    return literal.lower() in (code or "").lower()


def _cron_candidates(raw: str) -> list[str]:
    """Return deployable cron literals implied by prompt/intent text."""
    text = raw or ""
    candidates = set(re.findall(r"cron\([^)]+\)", text, flags=re.IGNORECASE))
    low = text.lower()
    if "7 utc" in low or "7:00 utc" in low or "everyday at 7" in low:
        candidates.add("cron(0 7 * * ? *)")
    normalized = set()
    for cron in candidates:
        if "**" in cron:
            normalized.add("cron(0 7 * * ? *)")
        else:
            normalized.add(cron)
    return sorted(normalized)


def _intent_literal_eval(code: str, sample: dict) -> dict:
    """Best-effort, LLM-free checks for explicit literals in Prompt/Intent.

    This does not try to prove full semantic correctness. It catches high-signal
    cases where the dataset explicitly names a value/resource behavior and the
    generated HCL should preserve it.
    """
    prompt = sample.get("prompt", "") or ""
    intent = sample.get("intent", "") or ""
    text = f"{prompt}\n{intent}".lower()
    checks: list[dict] = []

    def add(name: str, required: str, ok: bool) -> None:
        checks.append({"name": name, "required": required, "ok": bool(ok)})

    if "custom_ttl_attribute" in text:
        add("dynamodb_ttl_attribute", "custom_ttl_attribute", _contains_literal(code, "custom_ttl_attribute"))
    if "password1" in text or "password2" in text:
        add("elasticache_password1", "password1", _contains_literal(code, "password1"))
        add("elasticache_password2", "password2", _contains_literal(code, "password2"))
    if "lambda.js" in text:
        add("lambda_source_file", "lambda.js", _contains_literal(code, "lambda.js"))
    if "passwordpassword" in text:
        add("route53_txt_value", "passwordpassword", _contains_literal(code, "passwordpassword"))
    if "payer" in text and "bucketowner" in text:
        add("s3_request_payer", "BucketOwner", _contains_literal(code, "BucketOwner"))
        add(
            "s3_request_payment_resource",
            "aws_s3_bucket_request_payment_configuration",
            _contains_literal(code, "aws_s3_bucket_request_payment_configuration"),
        )
    if "target_prefix" in text and "log/" in text:
        add("s3_logging_prefix", "log/", _contains_literal(code, "log/"))
        add("s3_logging_resource", "aws_s3_bucket_logging", _contains_literal(code, "aws_s3_bucket_logging"))

    cron_values = _cron_candidates(prompt + "\n" + intent)
    for idx, cron in enumerate(cron_values, start=1):
        add(f"cron_expression_{idx}", cron, _contains_literal(code, cron))

    missing = [c for c in checks if not c["ok"]]
    return {
        "ok": None if not checks else not missing,
        "checks": checks,
        "missing": missing,
    }


def _is_deploy_environment_blocked(row: dict) -> bool:
    deploy = row.get("deploy") or {}
    if deploy.get("ok") is not False:
        return False
    attempts = row.get("deploy_attempt_log") or [deploy]
    env_types = {"ENV_LIMITATION", "QUOTA"}
    return any(attempt.get("error_type") in env_types for attempt in attempts)


def _row_code_success_flags(row: dict) -> dict:
    final_eval = row.get("final_eval") or {}
    dataset = row.get("dataset_eval") or {}
    resource_ok = (dataset.get("required_resource_match") or {}).get("ok")
    intent_literal_ok = (dataset.get("intent_literal_match") or {}).get("ok")
    terraform_ok = (row.get("val") or {}).get("ok")
    rego = row.get("rego") or {}
    deploy = row.get("deploy") or {}
    rego_ok = None if rego.get("skipped") else rego.get("ok")
    deploy_ok = deploy.get("ok")

    code_predeploy_ok = bool(terraform_ok and resource_ok and intent_literal_ok is not False)
    deployable_code_ok = bool(code_predeploy_ok and (deploy_ok is True or deploy_ok is None))
    benchmark_only_rego_fail = bool(code_predeploy_ok and rego_ok is False and deploy_ok is True)
    deploy_environment_blocked = bool(code_predeploy_ok and _is_deploy_environment_blocked(row))
    adjusted_code_success_ok = bool(deployable_code_ok or deploy_environment_blocked)

    return {
        "code_predeploy_ok": code_predeploy_ok,
        "deployable_code_ok": deployable_code_ok,
        "adjusted_code_success_ok": adjusted_code_success_ok,
        "benchmark_only_rego_fail": benchmark_only_rego_fail,
        "deploy_environment_blocked": deploy_environment_blocked,
        "strict_ok": bool(final_eval.get("end_to_end_strict_ok")),
    }


def _dataset_eval(code: str, sample: dict) -> dict:
    generated = _type_counter_from_hcl(code)
    expected = _type_counter_from_field(sample.get("resources", ""))
    reference = _type_counter_from_hcl(sample.get("reference_output", ""))
    generated_hard, generated_soft = _split_hard_soft(generated)
    expected_hard, expected_soft = _split_hard_soft(expected)
    reference_hard, reference_soft = _split_hard_soft(reference)
    return {
        "expected_resources": dict(expected),
        "generated_resources": dict(generated),
        "reference_resources": dict(reference),
        "required_resource_match": _coverage(generated_hard, expected_hard),
        "helper_resource_match": _coverage(generated_soft, expected_soft),
        "reference_required_match": _coverage(generated_hard, reference_hard),
        "reference_helper_match": _coverage(generated_soft, reference_soft),
        "resource_match": _coverage(generated_hard, expected_hard),
        "reference_match": _coverage(generated_hard, reference_hard),
        "soft_resource_types": sorted(_SOFT_RESOURCE_TYPES),
        "intent": sample.get("intent", ""),
        "intent_literal_match": _intent_literal_eval(code, sample),
    }


# ─── Per-agent runners (trả về elapsed và cập nhật state inplace) ────────────

def _run_archi(state: dict) -> tuple[bool, float, str | None]:
    t0 = time.time()
    r = archi_node(state)
    elapsed = round(time.time() - t0, 2)
    state.update(r)
    ok = bool(state.get("infrastructure_plan"))
    err = None if ok else str(r.get("error", "unknown"))
    return ok, elapsed, err


def _run_secu(state: dict) -> float:
    t0 = time.time()
    r = secu_node(state)
    elapsed = round(time.time() - t0, 2)
    state.update(r)
    return elapsed


def _run_engi(state: dict) -> tuple[bool, float, str | None]:
    t0 = time.time()
    r = engi_node(state)
    elapsed = round(time.time() - t0, 2)
    state.update(r)
    ok = bool(state.get("generated_code", "").strip())
    err = None if ok else str(r.get("error", "unknown"))
    return ok, elapsed, err


def _run_val(state: dict) -> tuple[bool, float, str]:
    t0 = time.time()
    r = validation_node(state)
    elapsed = round(time.time() - t0, 2)
    state.update(r)
    fb = state.get("fix_feedback") or {}
    passed = bool(fb.get("overall_passed"))
    route = route_after_validation(state)
    return passed, elapsed, route


def _run_deploy(state: dict) -> tuple[bool, float, str]:
    t0 = time.time()
    r = deployment_node(state)
    elapsed = round(time.time() - t0, 2)
    state.update(r)
    dr = state.get("deployment_result") or {}
    ok = bool(dr.get("success"))
    route = route_after_deployment(state)
    return ok, elapsed, route


def _run_rego(state: dict, rego_intent: str) -> tuple[dict, float]:
    t0 = time.time()
    run_dir = Path(state.get("run_dir") or "")
    files_dir = run_dir / "files" if run_dir else None
    result = run_rego_intent_on_hcl(
        state.get("generated_code", ""),
        rego_intent,
        run_dir=run_dir or None,
        files_dir=files_dir,
        timeout=int(os.environ.get("TF_PLAN_TIMEOUT", "120")),
    )
    elapsed = round(time.time() - t0, 2)
    return result, elapsed


# ─── Row runner ───────────────────────────────────────────────────────────────

def run_row(sample: dict,
            deploy: bool, auto_destroy: bool, no_secu: bool = False,
            no_rego: bool = False) -> tuple[dict, str]:
    """Chạy 1 row qua toàn bộ pipeline. Trả về (result_dict, output_str).

    Output được buffer nội bộ thay vì print trực tiếp — cho phép gọi song song
    mà không bị interleave giữa các worker.
    """
    lines: list[str] = []
    def log(msg: str = "") -> None:
        lines.append(msg)

    idx = sample["idx"]
    difficulty = sample["difficulty"]
    prompt = sample["prompt"]
    rego_intent = sample["rego_intent"]

    sep = "=" * 72
    log(f"\n{sep}")
    log(f"ROW {idx:4d}  difficulty={difficulty or '?'}")
    log(f"  {prompt[:100]}")
    log(sep)

    state = make_state(prompt, idx=idx, auto_destroy=auto_destroy)
    state["expected_resource_types"] = sample.get("resources", "")

    archi_result = secu_result = engi_result = None
    dataset_result = val_result = rego_result = deploy_result = None
    deploy_attempt_log: list[dict] = []
    val_attempts = deploy_attempts = 0

    next_agent = "architecture"
    iteration = 0

    while iteration < MAX_ITERATIONS:
        iteration += 1

        # ── A1: archi ──────────────────────────────────────────────────────
        if next_agent == "architecture":
            ok, elapsed, err = _run_archi(state)
            plan = state.get("infrastructure_plan") or {}
            n_res = len(plan.get("resources", []))
            archi_result = {
                "ok": ok, "elapsed_s": elapsed,
                "resource_count": n_res, "plan": plan,
                "diagnostics": state.get("architecture_diagnostics"),
                "warnings": state.get("architecture_warnings", []),
                "strategy": state.get("architecture_strategy", "llm"),
            }
            if not ok:
                aerr = state.get("architecture_error") or {}
                kind = aerr.get("kind") or "unknown"
                log(f"  [archi] FAILED ({elapsed}s) [{kind}]: {err}")
                if aerr.get("raw_preview"):
                    log(f"  [archi] raw preview: {aerr['raw_preview']}")
                diag = aerr.get("diagnostics") or {}
                if diag.get("missing_expected_types") or diag.get("malformed_entries"):
                    log(
                        "  [archi] diagnostics: "
                        f"missing={diag.get('missing_expected_types', [])} "
                        f"malformed={diag.get('malformed_entries', [])}"
                    )
                archi_result["error"] = err
                archi_result["architecture_error"] = state.get("architecture_error")
                break
            log(f"  [archi] {n_res} resources ({elapsed}s)")
            missing = (
                (state.get("architecture_diagnostics") or {})
                .get("missing_expected_types") or []
            )
            if missing:
                log(f"  [archi] missing_resources warning: missing expected types {missing}")
            next_agent = "security"

        # ── A2: secu ───────────────────────────────────────────────────────
        if next_agent == "security":
            if no_secu:
                secu_result = {"ok": True, "elapsed_s": 0, "skipped": True,
                               "ckv_resource_count": 0, "ckv_total": 0, "ckv_ids": {}}
                log(f"  [secu]  skipped (--no-secu)")
            else:
                elapsed = _run_secu(state)
                ckv_ids = state.get("security_ckv_ids") or {}
                n_ckv_res = len(ckv_ids)
                n_ckv_total = sum(len(v) for v in ckv_ids.values())
                secu_result = {
                    "ok": True, "elapsed_s": elapsed,
                    "ckv_resource_count": n_ckv_res, "ckv_total": n_ckv_total,
                    "ckv_ids": ckv_ids,
                }
                log(f"  [secu]  {n_ckv_res} resources, {n_ckv_total} CKV IDs ({elapsed}s)")
                for label, checks in ckv_ids.items():
                    log(f"    {label}: {', '.join(checks)}")
            next_agent = "engineering"

        # ── A3: engi ───────────────────────────────────────────────────────
        if next_agent == "engineering":
            ok, elapsed, err = _run_engi(state)
            code = state.get("generated_code", "")
            n_gen = _n_resources(code)
            n_lines = code.count("\n")
            engi_result = {
                "ok": ok, "elapsed_s": elapsed,
                "resource_count": n_gen, "line_count": n_lines,
                "generated_code": code,
                "warnings": state.get("engineering_warnings", []),
            }
            if not ok:
                log(f"  [engi]  FAILED ({elapsed}s): {err}")
                engi_result["error"] = err
                break
            log(f"  [engi]  {n_gen} resources, {n_lines} lines ({elapsed}s)")
            for warning in state.get("engineering_warnings", []):
                if warning.get("repairs"):
                    log(f"  [engi]  postprocess: {', '.join(warning['repairs'])}")
            next_agent = "validation"

        # ── A4: val ────────────────────────────────────────────────────────
        if next_agent == "validation":
            val_attempts += 1
            passed, elapsed, route = _run_val(state)
            fb = state.get("fix_feedback") or {}
            et = fb.get("error_type", "")
            ck = fb.get("checkov") or {}

            val_result = {
                "ok": passed,
                "elapsed_s": elapsed,
                "error_type": et,
                "checkov_passed": ck.get("passed_count", 0),
                "checkov_failed": ck.get("failed", []),
                "validate_ok": fb.get("validate_ok"),
                "plan_ok": fb.get("plan_ok"),
                "raw_error": (fb.get("raw_error") or "")[:2000],
                "fix_instruction": (fb.get("fix_instruction") or "")[:500],
                "attempts": val_attempts,
            }

            status = "PASS" if passed else f"FAIL [{et}]"
            ck_str = (f"ckv pass={ck.get('passed_count',0)} "
                      f"fail={ck.get('failed',[])}") if ck else ""
            log(f"  [val]   {status} ({elapsed}s) {ck_str}"
                f" → route={route} attempt={val_attempts}")
            if not passed and fb.get("fix_instruction"):
                log(f"  [val]   fix: {fb['fix_instruction']}")

            if route == "agent5":
                dataset_result = _dataset_eval(state.get("generated_code", ""), sample)
                rm = dataset_result["required_resource_match"]
                hm = dataset_result["helper_resource_match"]
                refm = dataset_result["reference_required_match"]
                im = dataset_result["intent_literal_match"]
                res_status = "PASS" if rm["ok"] else "FAIL"
                helper_status = "PASS" if hm["ok"] else "WARN"
                intent_status = "NA" if im["ok"] is None else ("PASS" if im["ok"] else "FAIL")
                log(f"  [data]  required resources {res_status} coverage={rm['coverage_pct']}% "
                    f"missing={rm['missing']} extra={rm['extra']}")
                log(f"  [data]  helper resources {helper_status} coverage={hm['coverage_pct']}% "
                    f"missing={hm['missing']} extra={hm['extra']}")
                log(f"  [data]  reference required coverage={refm['coverage_pct']}% "
                    f"missing={refm['missing']} extra={refm['extra']}")
                if im["ok"] is not None:
                    missing_intent = [m["name"] for m in im["missing"]]
                    log(f"  [data]  intent literals {intent_status} checks={len(im['checks'])} "
                        f"missing={missing_intent}")
                if no_rego:
                    rego_result = {
                        "ok": None,
                        "skipped": True,
                        "elapsed_s": 0,
                        "rule": None,
                        "error": "skipped by --no-rego",
                    }
                    log("  [rego]  skipped (--no-rego)")
                else:
                    rr, rego_elapsed = _run_rego(state, rego_intent)
                    rego_result = {
                        "ok": rr.get("ok"),
                        "skipped": rr.get("skipped", False),
                        "elapsed_s": rego_elapsed,
                        "rule": rr.get("rule"),
                        "error": rr.get("error"),
                        "values": rr.get("values", {}),
                        "opa_errors": rr.get("opa_errors", {}),
                        "true_rules": rr.get("true_rules", []),
                        "false_rules": rr.get("false_rules", []),
                        "entrypoint_type": rr.get("entrypoint_type"),
                    }
                    if rr.get("skipped"):
                        log(f"  [rego]  SKIP ({rego_elapsed}s): {rr.get('error')}")
                    elif rr.get("ok"):
                        log(f"  [rego]  PASS ({rego_elapsed}s) rule={rr.get('rule')}")
                    else:
                        log(f"  [rego]  FAIL ({rego_elapsed}s) rule={rr.get('rule')}: {rr.get('error')}")
                log("  [eval]  dataset/rego recorded; deployability will be tested separately")
                if not deploy:
                    log("  [deploy] skipped (--no-deploy)")
                    break
                next_agent = "deployment"
            elif route == "requires_human":
                log(f"  [val]   → REQUIRES_HUMAN")
                break
            else:
                next_agent = route
                log(f"  [val]   → retry via {next_agent} "
                    f"(total_retry={state.get('total_retry_count',0)})")
                continue

        # ── A5: deploy ─────────────────────────────────────────────────────
        if next_agent == "deployment":
            deploy_attempts += 1
            ok, elapsed, route = _run_deploy(state)
            dr = state.get("deployment_result") or {}
            et = dr.get("error_type", "")
            created = dr.get("resources_created", [])
            destroyed = dr.get("auto_destroyed", False)

            deploy_result = {
                "ok": ok,
                "elapsed_s": elapsed,
                "error_type": et if not ok else None,
                "resources_created": created,
                "auto_destroyed": destroyed,
                "auto_destroy_error": dr.get("auto_destroy_error"),
                "apply_raw_error": (dr.get("apply_raw_error") or "")[:2000],
                "fix_instruction": (dr.get("fix_instruction") or "")[:500],
                "attempts": deploy_attempts,
            }
            deploy_attempt_log.append({
                "attempt": deploy_attempts,
                "ok": ok,
                "elapsed_s": elapsed,
                "route": route,
                "error_type": et if not ok else None,
                "resources_created": created,
                "auto_destroyed": destroyed,
                "auto_destroy_error": dr.get("auto_destroy_error"),
                "destroy_failed": dr.get("destroy_failed", False),
                "destroy_error": dr.get("destroy_error"),
                "fix_instruction": (dr.get("fix_instruction") or "")[:500],
                "apply_raw_error": (dr.get("apply_raw_error") or "")[:1000],
            })

            if ok:
                d_str = "(destroyed)" if destroyed else "(resources kept)"
                log(f"  [deploy] OK ({elapsed}s) {len(created)} resources {d_str}")
                break
            else:
                log(f"  [deploy] FAIL [{et}] ({elapsed}s) → route={route} "
                    f"attempt={deploy_attempts}")
                if dr.get("fix_instruction"):
                    log(f"  [deploy] fix: {dr['fix_instruction']}")
                if dr.get("apply_raw_error"):
                    first_err = "\n".join(
                        ln for ln in (dr["apply_raw_error"]).splitlines()
                        if ln.strip().lower().startswith("error")
                    )[:300]
                    if first_err:
                        log(f"  [deploy] err: {first_err}")

            if route == "end":
                break
            elif route == "agent5":
                next_agent = "deployment"
            elif route == "requires_human":
                log(f"  [deploy] → REQUIRES_HUMAN")
                break
            else:
                next_agent = route
                log(f"  [deploy] → retry via {next_agent}")

    # Cleanup per-run dir sau khi xong — xóa a4/, a5/, files/ nhưng giữ lại nếu muốn debug
    run_dir_path = Path(state.get("run_dir", ""))
    if run_dir_path.exists():
        shutil.rmtree(run_dir_path, ignore_errors=True)

    dataset_ok = bool(
        dataset_result
        and dataset_result.get("resource_match", {}).get("ok")
    )
    intent_literal_ok = (
        None if not dataset_result
        else dataset_result.get("intent_literal_match", {}).get("ok")
    )
    terraform_ok = bool(val_result and val_result.get("ok"))
    rego_ok = (
        None if not rego_result or rego_result.get("skipped")
        else bool(rego_result.get("ok"))
    )
    deploy_ok = None if deploy_result is None else bool(deploy_result.get("ok"))
    failed_dimensions = []
    if archi_result and not archi_result.get("ok"):
        failed_dimensions.append("architecture")
    if engi_result and not engi_result.get("ok"):
        failed_dimensions.append("engineering")
    if dataset_result and not dataset_ok:
        failed_dimensions.append("dataset_resource")
    if intent_literal_ok is False:
        failed_dimensions.append("intent_literal")
    if val_result and not terraform_ok:
        failed_dimensions.append("terraform_validation")
    if rego_result and rego_ok is False:
        failed_dimensions.append("rego_intent")
    if deploy_result and deploy_ok is False:
        failed_dimensions.append("aws_deploy")

    final_eval = {
        "dataset_resource_ok": dataset_ok if dataset_result else None,
        "intent_literal_ok": intent_literal_ok,
        "terraform_validation_ok": terraform_ok if val_result else None,
        "rego_intent_ok": rego_ok,
        "deploy_ok": deploy_ok,
        "predeploy_strict_ok": bool(
            terraform_ok
            and dataset_ok
            and intent_literal_ok is not False
            and rego_ok is True
        ),
        "end_to_end_strict_ok": bool(
            terraform_ok
            and dataset_ok
            and intent_literal_ok is not False
            and rego_ok is True
            and deploy_ok is True
        ),
        "failed_dimensions": failed_dimensions,
    }

    result = {
        "row": idx,
        "difficulty": difficulty,
        "prompt": prompt,
        "generated_code": state.get("generated_code", ""),
        "archi": archi_result,
        "architecture_error": state.get("architecture_error"),
        "architecture_diagnostics": state.get("architecture_diagnostics"),
        "architecture_warnings": state.get("architecture_warnings", []),
        "architecture_strategy": state.get("architecture_strategy", "llm" if archi_result else None),
        "secu": secu_result,
        "engi": engi_result,
        "dataset_eval": dataset_result,
        "val": val_result,
        "rego": rego_result,
        "deploy": deploy_result,
        "deploy_attempt_log": deploy_attempt_log,
        "final_eval": final_eval,
        "total_retry_count": state.get("total_retry_count", 0),
        "deploy_retry_count": state.get("deploy_retry_count", 0),
        "routing_log": state.get("routing_log", []),
        "iterations": iteration,
    }
    final_eval.update(_row_code_success_flags(result))
    return result, "\n".join(lines)


def _update_counters(counters: dict, r: dict, no_deploy: bool, lock: threading.Lock) -> None:
    def _ok(key): return r.get(key) and r[key].get("ok")
    with lock:
        if _ok("archi"):   counters["ok1"] += 1
        else:              counters["fail1"] += 1
        if r["archi"]:
            if _ok("secu"):  counters["ok2"] += 1
            elif r["secu"]:  counters["fail2"] += 1
        if r["secu"]:
            if _ok("engi"):  counters["ok3"] += 1
            elif r["engi"]:  counters["fail3"] += 1
        if r["engi"]:
            if _ok("val"):   counters["ok4"] += 1
            elif r["val"]:   counters["fail4"] += 1
        if r.get("dataset_eval"):
            if r["dataset_eval"]["required_resource_match"]["ok"]:
                counters["ok_resource"] += 1
            else:
                counters["fail_resource"] += 1
            intent_lit = (r["dataset_eval"].get("intent_literal_match") or {}).get("ok")
            if intent_lit is True:
                counters["ok_intent_literal"] += 1
            elif intent_lit is False:
                counters["fail_intent_literal"] += 1
        if r["val"] and r["val"].get("ok"):
            rego = r.get("rego")
            if rego and rego.get("skipped"):
                counters["skip_rego"] += 1
            elif rego and rego.get("ok"):
                counters["ok_rego"] += 1
            elif rego:
                counters["fail_rego"] += 1
        if r.get("deploy") and not no_deploy:
            counters["deploy_rows"] += 1
            if _ok("deploy"):  counters["ok5"] += 1
            elif r["deploy"]:  counters["fail5"] += 1
            counters["deploy_attempts"] += max(1, int(r["deploy"].get("attempts") or 0))
        final_eval = r.get("final_eval") or {}
        if final_eval.get("predeploy_strict_ok"):
            counters["ok_predeploy_strict"] += 1
        if final_eval.get("end_to_end_strict_ok"):
            counters["ok_e2e_strict"] += 1
        if final_eval.get("code_predeploy_ok"):
            counters["ok_code_predeploy"] += 1
        if final_eval.get("deployable_code_ok"):
            counters["ok_deployable_code"] += 1
        if final_eval.get("adjusted_code_success_ok"):
            counters["ok_adjusted_code_success"] += 1
        if final_eval.get("benchmark_only_rego_fail"):
            counters["benchmark_only_rego_fail"] += 1
        if final_eval.get("deploy_environment_blocked"):
            counters["deploy_environment_blocked"] += 1


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test full pipeline A1→A2→A3→A4→Rego→A5")
    parser.add_argument("--csv", type=str, default=None,
                        help="Đường dẫn đến file CSV dataset (e.g. dataset/data-test.csv)")
    parser.add_argument("--limit", type=int, default=None, help="Số row tối đa")
    parser.add_argument("--out", type=str, default=None, help="Output JSON path")
    parser.add_argument("--cases", nargs="+", default=None,
                        help="Row indices, e.g. --cases 0 3 7-10 15")
    parser.add_argument("--no-secu", action="store_true",
                        help="Bỏ qua A2, security_ckv_ids = {} (test A1→A3→A4→A5)")
    parser.add_argument("--no-rego", action="store_true",
                        help="Bỏ qua stage Rego intent sau A4")
    parser.add_argument("--no-deploy", action="store_true",
                        help="Dừng sau A4 (không chạy terraform apply)")
    parser.add_argument("--no-destroy", action="store_true",
                        help="Giữ resources sau apply (không auto-destroy)")
    parser.add_argument("--workers", type=int, default=1,
                        help="Số worker chạy song song (mặc định 1 = tuần tự)")
    args = parser.parse_args()

    provider = os.getenv("LLM_PROVIDER", "nvidia").lower()
    if provider == "deepseek":
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    else:
        model = os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")
    deploy_str = "no-deploy" if args.no_deploy else ("no-destroy" if args.no_destroy else "auto-destroy")
    rego_str = "no-rego" if args.no_rego else "rego"
    csv_path = Path(args.csv) if args.csv else CSV_PATH
    print(f"Full pipeline A1→A2→A3→A4→Rego→A5  |  model={model}  "
          f"|  csv={csv_path.name}  |  deploy={deploy_str}  |  workers={args.workers}")
    print(f"Eval: {rego_str}")

    rows = load_csv(csv_path, args.limit)
    if args.cases:
        selected = _parse_cases(args.cases)
        rows = [row for row in rows if row["idx"] in selected]
        print(f"--cases filter: {len(rows)} rows")
    print(f"Loaded {len(rows)} rows\n")

    results: list[dict] = []
    counters = {k: 0 for k in ("ok1", "ok2", "ok3", "ok4", "ok5",
                                "fail1", "fail2", "fail3", "fail4", "fail5",
                                "ok_rego", "fail_rego", "skip_rego",
                                "ok_resource", "fail_resource",
                                "ok_intent_literal", "fail_intent_literal",
                                "ok_predeploy_strict", "ok_e2e_strict",
                                "ok_code_predeploy", "ok_deployable_code",
                                "ok_adjusted_code_success",
                                "benchmark_only_rego_fail", "deploy_environment_blocked",
                                "deploy_rows", "deploy_attempts")}
    counter_lock = threading.Lock()

    def _run_one(row_args: tuple) -> tuple[dict, str]:
        return run_row(row_args,
                       deploy=not args.no_deploy,
                       auto_destroy=not args.no_destroy,
                       no_secu=args.no_secu,
                       no_rego=args.no_rego)

    interrupted = False

    if args.workers <= 1:
        # ── Tuần tự ────────────────────────────────────────────────────────
        for row in rows:
            try:
                r, output = _run_one(row)
                print(output)
                results.append(r)
                _update_counters(counters, r, args.no_deploy, counter_lock)
            except KeyboardInterrupt:
                print("\n[interrupted]")
                interrupted = True
                break
            except Exception as e:
                print(f"  [error] row={row['idx']}: {e}")
                import traceback; traceback.print_exc()
                with counter_lock:
                    counters["fail1"] += 1
    else:
        # ── Song song ──────────────────────────────────────────────────────
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_idx = {
                executor.submit(_run_one, row_args): row_args["idx"]
                for row_args in rows
            }
            try:
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        r, output = future.result()
                        with _PRINT_LOCK:
                            print(output)
                        results.append(r)
                        _update_counters(counters, r, args.no_deploy, counter_lock)
                    except Exception as e:
                        with _PRINT_LOCK:
                            print(f"  [error] row={idx}: {e}")
                            import traceback; traceback.print_exc()
                        with counter_lock:
                            counters["fail1"] += 1
            except KeyboardInterrupt:
                print("\n[interrupted — cancelling remaining futures]")
                for f in future_to_idx:
                    f.cancel()
                interrupted = True

        # Sắp xếp lại theo row index (as_completed không đảm bảo thứ tự)
        results.sort(key=lambda r: r["row"])

    total = counters["ok1"] + counters["fail1"]
    print(f"\n{'='*72}")
    print(f"SUMMARY  total={total}" + (" [interrupted]" if interrupted else ""))
    print(f"  A1 archi:  {counters['ok1']}/{total}  ok")
    if counters["ok1"]:
        print(f"  A2 secu:   {counters['ok2']}/{counters['ok1']}  ok  "
              f"(always passes — thin CKV assignment)")
        print(f"  A3 engi:   {counters['ok3']}/{counters['ok1']}  ok")
    if counters["ok3"]:
        print(f"  A4 val:    {counters['ok4']}/{counters['ok3']}  ok")
    resource_total = counters["ok_resource"] + counters["fail_resource"]
    if resource_total:
        print(f"  Resource:  {counters['ok_resource']}/{resource_total}  ok")
    intent_literal_total = counters["ok_intent_literal"] + counters["fail_intent_literal"]
    if intent_literal_total:
        print(f"  Intent literals: {counters['ok_intent_literal']}/{intent_literal_total}  ok")
    rego_total = counters["ok_rego"] + counters["fail_rego"] + counters["skip_rego"]
    if rego_total:
        print(f"  Rego:      {counters['ok_rego']}/{rego_total}  ok"
              f"  ({counters['skip_rego']} skipped)")
    deploy_total = counters["ok5"] + counters["fail5"]
    if deploy_total and not args.no_deploy:
        print(f"  A5 deploy rows:     {counters['ok5']}/{deploy_total}  ok")
        print(f"  A5 deploy attempts: {counters['deploy_attempts']}  total")
    print(f"  Strict predeploy (A4+Resource+Rego): {counters['ok_predeploy_strict']}/{len(results)}  ok")
    if not args.no_deploy:
        print(f"  Strict end-to-end (+Deploy):        {counters['ok_e2e_strict']}/{len(results)}  ok")
    print(f"  Code predeploy (A4+Resource+Intent): {counters['ok_code_predeploy']}/{len(results)}  ok")
    if not args.no_deploy:
        print(f"  Deployable code (excludes Rego):     {counters['ok_deployable_code']}/{len(results)}  ok")
    print(
        f"  Adjusted code-success:              {counters['ok_adjusted_code_success']}/{len(results)}  ok"
    )
    if counters["benchmark_only_rego_fail"] or counters["deploy_environment_blocked"]:
        print(
            "  Non-code blockers: "
            f"rego_benchmark_only={counters['benchmark_only_rego_fail']}, "
            f"aws_environment={counters['deploy_environment_blocked']}"
        )

    out_path = (Path(args.out) if args.out
                else ROOT / "reviews" / "pipeline_results.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nSaved {len(results)} results → {out_path}")


if __name__ == "__main__":
    main()
