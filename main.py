"""Entry point — Multi-agent Terraform generation pipeline.

Single prompt:
    python main.py "Create an S3 bucket with versioning and encryption"
    python main.py "prompt" --output infra.tf
    python main.py "prompt" --log-level DEBUG

Batch (data-no-deploy.csv):
    python main.py --batch                          # 10 case mặc định
    python main.py --batch --cases 3 8 17 12        # chỉ định case cụ thể
    python main.py --batch --all                    # toàn bộ 63 case
    python main.py --batch --workers 4              # parallel
    python main.py --batch --save results.json      # ghi kết quả ra file
"""
import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from graph import run_pipeline, destroy_resources  # noqa: E402 — must be after load_dotenv

# ── ANSI colours ──────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text

GREEN = lambda t: _c("32", t)
RED   = lambda t: _c("31", t)
BOLD  = lambda t: _c("1",  t)
DIM   = lambda t: _c("2",  t)

_LOG_LOCK = threading.Lock()

def _log(idx: int, msg: str) -> None:
    with _LOG_LOCK:
        print(f"[{idx:2d}] {msg}", flush=True)


# ── Dataset ───────────────────────────────────────────────────────────────────
_CSV_PATH = Path(__file__).parent / "dataset" / "data-dev-fast.csv"
_BATCH_DEFAULT = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27]
_RES_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"')


def _load_csv(indices: list[int]) -> list[tuple[int, str, str]]:
    rows = list(csv.DictReader(_CSV_PATH.open(encoding="utf-8")))
    return [
        (i, rows[i]["Prompt"], rows[i].get("Reference output", ""))
        for i in indices if i < len(rows)
    ]


def _coverage(generated_code: str, reference: str) -> dict:
    gen = Counter(t for t, _ in _RES_RE.findall(generated_code))
    ref = Counter(t for t, _ in _RES_RE.findall(reference))
    matched = sum((gen & ref).values())
    ref_total = sum(ref.values())
    return {
        "matched":      matched,
        "ref_count":    ref_total,
        "pct":          round(100 * matched / ref_total, 1) if ref_total else 0.0,
        "missing":      sorted((ref - gen).elements()),
        "extra":        sorted((gen - ref).elements()),
    }


# ── Single prompt mode ────────────────────────────────────────────────────────
def _fmt_resources(plan: dict) -> str:
    resources = plan.get("resources", [])
    if not resources:
        return DIM("  (none)")
    return "\n".join(f"  • {r['type']}.{r['name']}" for r in resources)


def _fmt_constraints(sc: dict) -> str:
    if not sc:
        return DIM("  (none)")
    lines = []
    for label, attrs in sc.items():
        for attr, val in attrs.items():
            lines.append(f"  • {label}.{BOLD(attr)} = {val!r}")
    return "\n".join(lines)


def _fmt_validation(fb: dict) -> str:
    if not fb:
        return DIM("  (not run)")
    passed   = fb.get("overall_passed")
    validate = "✓" if fb.get("validate_passed") else "✗"
    plan     = "✓" if fb.get("plan_passed") else "✗"
    ck       = fb.get("checkov", {})
    ck_str   = f"checkov {ck.get('passed_count', 0)}pass/{len(ck.get('failed', []))}fail"
    status   = GREEN("PASS") if passed else RED(f"FAIL [{fb.get('error_type')}]")
    line     = f"  {status}  validate={validate}  plan={plan}  {ck_str}"
    if not passed and fb.get("fix_instruction"):
        line += f"\n  {DIM('fix: ' + fb['fix_instruction'][:120])}"
    return line


def _fmt_deployment(dr: dict) -> str:
    if not dr:
        return DIM("  (not run)")
    if dr.get("success"):
        n = len(dr.get("resources_created") or [])
        return GREEN(f"  APPLY OK  ({n} resources)")
    err = dr.get("error_type", "?")
    msg = (dr.get("fix_instruction") or "")[:120]
    return RED(f"  FAIL [{err}]  {msg}")


def _fmt_destroy_check(dr: dict) -> str:
    """Hiển thị trạng thái destroy — resources còn live hay đã được dọn sạch."""
    if not dr:
        return DIM("  (không có deployment)")

    created = dr.get("resources_created") or []

    if not dr.get("success"):
        if dr.get("destroy_failed"):
            err = (dr.get("destroy_error") or "")[:120]
            lines = RED(f"  CÒN LIVE — partial apply, destroy thất bại ({len(created)} resources)")
            if created:
                lines += "\n" + "\n".join(f"    • {r}" for r in created)
            if err:
                lines += f"\n  {DIM('lỗi: ' + err)}"
            lines += f"\n  {DIM('→ xóa thủ công bằng: python main.py --destroy <file.tf>')}"
            return lines
        if dr.get("partial_apply_destroyed"):
            return GREEN("  CLEAN — partial apply đã được destroy tự động")
        return DIM("  N/A — apply thất bại, không có resource nào được tạo")

    # apply thành công
    auto_destroyed = dr.get("auto_destroyed")
    auto_destroy_error = dr.get("auto_destroy_error")

    if auto_destroyed:
        return GREEN(f"  CLEAN — {len(created)} resource(s) đã được destroy tự động")
    if auto_destroy_error:
        lines = RED(f"  CÒN LIVE — auto-destroy thất bại ({len(created)} resources):")
        if created:
            lines += "\n" + "\n".join(f"    • {r}" for r in created)
        lines += f"\n  {DIM('lỗi: ' + auto_destroy_error[:120])}"
        lines += f"\n  {DIM('→ xóa thủ công bằng: python main.py --destroy <file.tf>')}"
        return lines

    # Không có auto_destroy (single mode mặc định)
    lines = RED(f"  CÒN LIVE — {len(created)} resource(s) chưa được xóa:")
    if created:
        lines += "\n" + "\n".join(f"    • {r}" for r in created)
    else:
        lines += "\n    (không rõ tên resource)"
    lines += f"\n  {DIM('→ xóa thủ công bằng: python main.py --destroy <file.tf>')}"
    return lines


def _fmt_routing(log: list) -> str:
    if not log:
        return DIM("  (no retries)")
    return "\n".join(
        f"  [{e['round']}] {e['error_type']} → {e.get('predicted_route', '?')}"
        f"  {DIM(str(e.get('fix_instruction', ''))[:80])}"
        for e in log
    )


def _print_section(title: str) -> None:
    print(f"\n{BOLD('─── ' + title + ' ' + '─' * max(0, 56 - len(title)))}")


def _single_main(args) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    print(BOLD(f"\nPrompt: {args.prompt[:100]}"))
    print(DIM(f"Timeout: {args.timeout}s  Running pipeline…"))

    try:
        final = run_pipeline(
            args.prompt,
            terraform_plan_timeout=args.timeout,
        )
    except KeyboardInterrupt:
        print(RED("\nInterrupted."), file=sys.stderr)
        return 130
    except Exception as e:
        print(RED(f"\nPipeline error: {e}"), file=sys.stderr)
        logging.debug("Pipeline exception", exc_info=True)
        return 1

    plan = final.get("infrastructure_plan") or {}
    sc   = final.get("security_constraints") or {}
    fb   = final.get("fix_feedback") or {}
    dr   = final.get("deployment_result") or {}
    code = final.get("generated_code") or ""
    log  = final.get("routing_log") or []

    _print_section("Resources planned")
    print(_fmt_resources(plan))
    _print_section("Security constraints")
    print(_fmt_constraints(sc))
    _print_section("Validation")
    print(_fmt_validation(fb))
    _print_section("Deployment")
    print(_fmt_deployment(dr))
    _print_section("Destroy check")
    print(_fmt_destroy_check(dr))
    _print_section("Retries")
    print(f"  total={final.get('total_retry_count', 0)}  "
          f"deploy={final.get('deploy_retry_count', 0)}")
    print(_fmt_routing(log))

    if code and not args.no_hcl:
        if args.output:
            Path(args.output).write_text(code, encoding="utf-8")
            _print_section("HCL")
            print(f"  Saved → {args.output}  ({len(code)} chars)")
        else:
            _print_section("Generated HCL")
            print(code)

    print()

    if dr.get("success"):
        return 0
    if fb.get("overall_passed") and not dr:
        return 0
    return 1


# ── Batch mode ────────────────────────────────────────────────────────────────
def _run_one(idx: int, prompt: str, reference: str, timeout: int,
             tf_dir: Path | None = None) -> dict:
    t0 = time.time()
    _log(idx, prompt[:70])
    error = None
    final = {}
    try:
        final = run_pipeline(prompt, terraform_plan_timeout=timeout, auto_destroy=True)
    except Exception as e:
        error = str(e)
        _log(idx, RED(f"ERROR: {e}"))

    elapsed = time.time() - t0
    fb   = final.get("fix_feedback") or {}
    dr   = final.get("deployment_result") or {}
    code = final.get("generated_code") or ""
    plan = final.get("infrastructure_plan") or {}
    ck   = fb.get("checkov") or {}

    a4_pass = fb.get("overall_passed", False)
    a5_ok   = dr.get("success", False)

    sym = "PASS" if a4_pass else f"FAIL/{fb.get('error_type', '?')}"
    _log(idx, f"A4: {GREEN(sym) if a4_pass else RED(sym)}  "
              f"validate={'ok' if fb.get('validate_passed') else 'FAIL'}  "
              f"plan={'ok' if fb.get('plan_passed') else 'FAIL'}  "
              f"checkov={ck.get('passed_count',0)}pass/{len(ck.get('failed',[]))}fail  "
              f"({elapsed:.1f}s)")
    if not a4_pass and fb.get("fix_instruction"):
        _log(idx, DIM(f"    fix: {fb['fix_instruction'][:100]}"))
    if dr:
        a5_sym = "APPLY OK" if a5_ok else f"FAIL/{dr.get('error_type', '?')}"
        destroyed_tag = ""
        if a5_ok and dr.get("auto_destroyed"):
            destroyed_tag = "  destroyed=ok"
        elif a5_ok and dr.get("auto_destroy_error"):
            destroyed_tag = f"  destroyed=FAIL"
        _log(idx, f"A5: {GREEN(a5_sym) if a5_ok else RED(a5_sym)}  "
                  f"resources={len(dr.get('resources_created') or [])}{destroyed_tag}")

    if tf_dir and code:
        tf_path = tf_dir / f"case_{idx:03d}.tf"
        tf_path.write_text(code, encoding="utf-8")
        apply_err = dr.get("apply_raw_error") or ""
        if apply_err:
            (tf_dir / f"case_{idx:03d}_apply.log").write_text(apply_err, encoding="utf-8")

    cov = _coverage(code, reference) if reference and code else {}
    if cov:
        miss = f"  missing:{cov['missing']}" if cov["missing"] else ""
        _log(idx, f"    cov: {cov['matched']}/{cov['ref_count']} ({cov['pct']}%){miss}")

    return {
        "csv_idx":   idx,
        "prompt":    prompt,
        "elapsed_s": round(elapsed, 1),
        "error":     error,
        "agent1": {"resource_count": len(plan.get("resources", []))},
        "agent2": {"secured_count": len(final.get("security_constraints") or {})},
        "agent3": {"lines": code.count("\n"), "code": code},
        "agent4": {
            "overall_passed":  a4_pass,
            "error_type":      fb.get("error_type"),
            "validate_passed": fb.get("validate_passed", False),
            "plan_passed":     fb.get("plan_passed", False),
            "checkov_passed":  ck.get("passed_count", 0),
            "checkov_failed":  ck.get("failed", []),
            "fix_instruction": fb.get("fix_instruction"),
        },
        "agent5": {
            "success":            a5_ok,
            "error_type":         dr.get("error_type"),
            "resources_created":  dr.get("resources_created", []),
            "fix_instruction":    dr.get("fix_instruction"),
            "apply_raw_error":    (dr.get("apply_raw_error") or "")[:1000],
            "auto_destroyed":     dr.get("auto_destroyed", False),
            "auto_destroy_error": dr.get("auto_destroy_error"),
            "destroy_failed":     dr.get("destroy_failed", False),
        },
        "total_retry_count":  final.get("total_retry_count", 0),
        "deploy_retry_count": final.get("deploy_retry_count", 0),
        "routing_log":        final.get("routing_log", []),
        "ground_truth":       cov,
    }


def _batch_main(args) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    total_rows = sum(1 for _ in csv.DictReader(_CSV_PATH.open(encoding="utf-8")))

    if args.all:
        indices = list(range(total_rows))
    elif args.cases:
        indices = args.cases
    else:
        indices = _BATCH_DEFAULT

    items = _load_csv(indices)
    if not items:
        print(RED("No valid cases found."), file=sys.stderr)
        return 1

    provider = os.getenv("LLM_PROVIDER", "?")
    model    = os.getenv("DEEPSEEK_MODEL") or os.getenv("NVIDIA_MODEL") or os.getenv("GEMINI_MODEL") or "?"
    print(BOLD(f"\nBatch | model={model} | csv={_CSV_PATH.name} | workers={args.workers}"))
    print(f"Cases: {[i for i, _, _ in items]}\n")

    tf_dir = Path(args.tf_dir)
    tf_dir.mkdir(parents=True, exist_ok=True)
    print(DIM(f"TF outputs → {tf_dir}/"))

    t0_all   = time.time()
    results  = []
    errors   = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_run_one, idx, prompt, ref, args.timeout, tf_dir): idx
            for idx, prompt, ref in items
        }
        for fut in as_completed(futures):
            try:
                r = fut.result()
                results.append(r)
            except Exception as e:
                idx = futures[fut]
                errors.append({"csv_idx": idx, "error": str(e)})
                _log(idx, RED(f"FATAL: {e}"))

    elapsed_all = time.time() - t0_all
    results.sort(key=lambda r: r["csv_idx"])

    # ── Summary ───────────────────────────────────────────────────────────────
    n         = len(results)
    a4_pass   = sum(1 for r in results if r["agent4"]["overall_passed"])
    val_pass  = sum(1 for r in results if r["agent4"]["validate_passed"])
    plan_pass = sum(1 for r in results if r["agent4"]["plan_passed"])
    a5_ok     = sum(1 for r in results if r["agent5"]["success"])
    cov_list  = [r["ground_truth"]["pct"] for r in results if r.get("ground_truth")]
    avg_cov   = round(sum(cov_list) / len(cov_list), 1) if cov_list else "-"
    avg_retry = round(sum(r["total_retry_count"] for r in results) / n, 1) if n else 0

    a4_err_dist = Counter(
        r["agent4"]["error_type"]
        for r in results if not r["agent4"]["overall_passed"] and r["agent4"]["error_type"]
    )
    a5_err_dist = Counter(
        r["agent5"]["error_type"]
        for r in results if not r["agent5"]["success"] and r["agent5"]["error_type"]
    )

    print(f"\n{'='*70}")
    print(BOLD(f"SUMMARY  ({n} cases, {elapsed_all:.1f}s)  model={model}"))
    print(f"  A4 pass (validate+plan+checkov) : {GREEN(str(a4_pass))}/{n}")
    print(f"  tf validate pass                : {val_pass}/{n}")
    print(f"  tf plan pass                    : {plan_pass}/{n}")
    print(f"  A5 apply success                : {a5_ok}/{n}")
    print(f"  avg retry rounds                : {avg_retry}")
    print(f"  avg resource coverage           : {avg_cov}%")
    if a4_err_dist:
        print(f"  A4 fail breakdown               : {dict(a4_err_dist)}")
    if a5_err_dist:
        print(f"  A5 fail breakdown               : {dict(a5_err_dist)}")
    if errors:
        print(RED(f"  pipeline errors                 : {len(errors)}"))

    # ── Save JSON ─────────────────────────────────────────────────────────────
    out_path = Path(args.save) if args.save else (
        Path(__file__).parent / "tmp" / "batch_results.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "model":    model,
        "provider": provider,
        "cases":    [i for i, _, _ in items],
        "summary": {
            "n": n, "a4_pass": a4_pass, "val_pass": val_pass,
            "plan_pass": plan_pass, "a5_ok": a5_ok,
            "avg_retry": avg_retry, "avg_cov": avg_cov,
        },
        "results": results,
        "errors":  errors,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved → {out_path}")

    # ── Destroy check ─────────────────────────────────────────────────────────
    live: list[tuple[int, list[str], str]] = []
    for r in results:
        a5 = r["agent5"]
        if not a5["success"]:
            if a5.get("destroy_failed"):
                live.append((r["csv_idx"], a5["resources_created"], "destroy_failed"))
            continue
        if a5.get("auto_destroyed"):
            continue
        if a5.get("auto_destroy_error"):
            live.append((r["csv_idx"], a5["resources_created"], a5["auto_destroy_error"]))
        elif a5["success"]:
            live.append((r["csv_idx"], a5["resources_created"], "no_auto_destroy"))

    print(f"\n{'='*70}")
    if not live:
        print(GREEN("DESTROY CHECK: tất cả resources đã được dọn sạch ✓"))
    else:
        print(RED(f"DESTROY CHECK: {len(live)} case(s) CÒN LIVE — cần xóa thủ công:"))
        for idx, resources, reason in live:
            tag = DIM(f"[{reason}]")
            print(f"  case {idx:2d}  {tag}  {len(resources)} resource(s)")
            for res in resources:
                print(f"    • {res}")
        print(DIM("  → xóa từng case: python main.py --destroy tmp/tf_outputs/case_NNN.tf"))

    return 0 if a4_pass == n else 1


# ── Destroy mode ─────────────────────────────────────────────────────────────
def _destroy_main(args) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    tf_file = Path(args.destroy)
    if not tf_file.exists():
        print(RED(f"File not found: {tf_file}"), file=sys.stderr)
        return 1
    code = tf_file.read_text(encoding="utf-8")
    print(BOLD(f"\nDestroying resources from: {tf_file}"))
    print(DIM("Running terraform destroy…"))
    result = destroy_resources(code)
    if result["success"]:
        n = len(result.get("resources_destroyed") or [])
        print(GREEN(f"  DESTROY OK  ({n} resources removed)"))
        return 0
    print(RED(f"  DESTROY FAILED  {result.get('error', '')[:200]}"))
    return 1


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tfmultiagent",
        description="Generate Terraform IaC from a natural-language prompt.",
    )

    # shared options
    parser.add_argument("--timeout", type=int, metavar="SEC",
        default=int(os.environ.get("TF_PLAN_TIMEOUT", "120")),
        help="terraform plan timeout in seconds")
    parser.add_argument("--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # destroy mode
    parser.add_argument("--destroy", metavar="FILE",
        help="Destroy AWS resources from a .tf file (terraform destroy)")

    # batch flag
    parser.add_argument("--batch", action="store_true",
        help="Run against data-no-deploy.csv dataset")
    parser.add_argument("--cases", nargs="+", type=int, metavar="N",
        help="CSV row indices to run (batch only)")
    parser.add_argument("--all", action="store_true",
        help="Run all rows in dataset (batch only)")
    parser.add_argument("--workers", type=int, default=2, metavar="N",
        help="Parallel workers (batch only, default: 2)")
    parser.add_argument("--save", metavar="FILE",
        help="Save batch results JSON to this path")
    parser.add_argument("--tf-dir", metavar="DIR",
        default="tmp/tf_outputs",
        help="Directory to save generated .tf files per case (batch only, default: tmp/tf_outputs)")

    # single-prompt options
    parser.add_argument("prompt", nargs="?",
        help="Natural-language infrastructure description (single mode)")
    parser.add_argument("--output", "-o", metavar="FILE",
        help="Write generated HCL to this file (single mode)")
    parser.add_argument("--no-hcl", action="store_true",
        help="Suppress HCL output (single mode)")

    args = parser.parse_args()

    if args.destroy:
        return _destroy_main(args)

    if args.batch:
        return _batch_main(args)

    if not args.prompt:
        parser.error("prompt is required in single mode (or use --batch)")

    return _single_main(args)


if __name__ == "__main__":
    sys.exit(main())
