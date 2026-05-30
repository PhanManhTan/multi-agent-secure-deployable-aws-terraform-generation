"""Validation Agent — Agent 4 trong pipeline.

Kiểm tra generated_code: terraform validate (static) → Checkov + terraform plan.
Nếu pass → route Agent 5. Nếu fail → phân loại lỗi (error_type/root_cause) +
sinh fix_instruction, route về đúng agent để retry.

Phân loại hybrid (giảm phụ thuộc LLM, tăng độ tin cậy):
  - terraform validate fail → SYNTAX/engineering (TẤT ĐỊNH, fix = lỗi terraform).
    Bỏ qua Checkov/plan vì HCL hỏng thì chạy tiếp vô nghĩa.
  - terraform plan fail vì timeout/init → INFRA → requires_human.
  - terraform plan fail vì code → LLM phân loại LOGIC (engineering) / MISSING_RESOURCE (arch).
  - Checkov required check fail → LLM phân loại SECURITY (engineering) / WRONG_CONSTRAINT (security).

Output: fix_feedback + cập nhật counters/error_history/routing_log.
LangGraph pattern: RETURN dict update, không mutation trực tiếp state.
"""
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from core.hcl import extract_resource_block
from core.terraform import run_terraform, run_checkov_on_hcl, write_terraform_dir, terraform_workdir
from prompts.validation import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.validation import TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM

logger = logging.getLogger(__name__)

_RESOURCE_DECL_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"')

_INIT_TIMEOUT = 300       # cold cache lần đầu có thể tải provider
_VALIDATE_TIMEOUT = 60

def _hcl_resource_labels(code: str) -> list[str]:
    """Trích 'type.name' từ HCL thực tế A3 sinh ra — dùng để ground fix_instruction."""
    return [f"{t}.{n}" for t, n in _RESOURCE_DECL_RE.findall(code)]


def _extract_code_context(validate_err: str, code: str, window: int = 4) -> str:
    """Trích dòng code xung quanh vị trí lỗi từ terraform validate stderr.

    Giúp Agent 3 thấy chính xác đoạn code cần sửa thay vì chỉ nhận line number.
    """
    m = re.search(r"on main\.tf line (\d+)", validate_err)
    if not m:
        return ""
    line_num = int(m.group(1))
    lines = code.split("\n")
    start = max(0, line_num - window - 1)
    end   = min(len(lines), line_num + window)
    parts = []
    for i, ln in enumerate(lines[start:end], start=start + 1):
        marker = ">>>" if i == line_num else "   "
        parts.append(f"{i:3d} {marker} {ln}")
    return "\n".join(parts)



def _error_signature(error_type: str, validate_err: str, plan_err: str,
                     failed_security: list) -> list:
    """Signature cho error_history (dùng để phát hiện repeat/oscillation).

    Security: dùng tập "resource_label.attr" thiếu. Syntax/logic: dùng các dòng
    'Error: ...' đã chuẩn hoá để 2 lần CÙNG lỗi khớp nhau, 2 lỗi KHÁC nhau
    (đang tiến triển) không bị nhầm là 'exact repeat'.
    """
    if error_type in ("SECURITY", "WRONG_CONSTRAINT"):
        return sorted(f"{lbl}.{attr}" for lbl, attr, _ in failed_security)
    text = validate_err if error_type == "SYNTAX" else plan_err
    errs = re.findall(r"Error:\s*(.+)", text or "")
    sig = sorted({e.strip()[:80] for e in errs})
    return sig or [((text or "").strip()[:80] or error_type)]


def _success_result(checkov: dict) -> dict:
    return {
        "fix_feedback": {
            "overall_passed": True, "error_type": None, "root_cause": None,
            "fix_instruction": None, "checkov": checkov,
            "validate_passed": True, "plan_passed": True,
        },
        "eng_retry_count": 0,
    }


def _infra_return(state: AgentState, fix_instruction: str, checkov: dict,
                  validate_passed: bool, plan_passed: bool, raw_error: str = "") -> dict:
    """INFRA: lỗi môi trường (Floci/timeout/init). Không tăng per-loop counter."""
    new_total = state["total_retry_count"] + 1
    logger.info("Agent 4: INFRA — %s", fix_instruction[:80])
    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": "INFRA", "root_cause": None,
            "fix_instruction": fix_instruction, "raw_error": raw_error,
            "checkov": checkov,
            "validate_passed": validate_passed, "plan_passed": plan_passed,
        },
        "total_retry_count": new_total,
        "error_history": state["error_history"] + [{
            "round": new_total, "error_type": "INFRA", "failed_checks": [],
        }],
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": "INFRA", "root_cause": None,
            "fix_instruction": fix_instruction, "predicted_route": "requires_human",
        }],
    }


def _fail_return(state: AgentState, error_type: str, root_cause: str,
                 fix_instruction: str, checkov: dict, validate_passed: bool,
                 plan_passed: bool, signature: list, raw_error: str = "") -> dict:
    new_total = state["total_retry_count"] + 1
    entry = {"round": new_total, "error_type": error_type, "failed_checks": signature,
             "fix_instruction": fix_instruction}
    is_eng = error_type in ("SECURITY", "SYNTAX", "LOGIC")
    is_arch = error_type == "MISSING_RESOURCE"
    is_sec = error_type == "WRONG_CONSTRAINT"
    return {
        "fix_feedback": {
            "overall_passed": False, "error_type": error_type, "root_cause": root_cause,
            "fix_instruction": fix_instruction, "raw_error": raw_error,
            "checkov": checkov,
            "validate_passed": validate_passed, "plan_passed": plan_passed,
        },
        "total_retry_count": new_total,
        "eng_retry_count":  state["eng_retry_count"]  + (1 if is_eng  else 0),
        "arch_retry_count": state["arch_retry_count"] + (1 if is_arch else 0),
        "sec_retry_count":  state["sec_retry_count"]  + (1 if is_sec  else 0),
        "error_history":      state["error_history"]      + [entry],
        "eng_error_history":  state.get("eng_error_history",  []) + ([entry] if is_eng  else []),
        "arch_error_history": state.get("arch_error_history", []) + ([entry] if is_arch else []),
        "sec_error_history":  state.get("sec_error_history",  []) + ([entry] if is_sec  else []),
        "routing_log": state["routing_log"] + [{
            "round": new_total, "error_type": error_type, "root_cause": root_cause,
            "fix_instruction": fix_instruction, "predicted_route": root_cause,
        }],
    }


def _llm_classify(context: str, allowed_types: set,
                  default_type: str, default_fix: str) -> tuple[str, str, str]:
    """LLM phân loại error_type + fix_instruction. Root_cause suy ra TẤT ĐỊNH từ
    error_type (mapping bắt buộc). Fallback an toàn nếu LLM lỗi/sai spec."""
    def _root(et: str) -> str:
        return {"MISSING_RESOURCE": "architecture", "WRONG_CONSTRAINT": "security"}.get(et, "engineering")

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]
    try:
        raw = call_llm(messages)
        parsed = parse_llm_json(raw, {
            "error_type": None, "fix_instruction": None,
        })
    except Exception as e:
        logger.warning("Agent 4 LLM classify lỗi (%s) — dùng default", e)
        return default_type, _root(default_type), default_fix

    et = parsed.get("error_type")
    if et not in allowed_types:
        et = default_type
    fix = str(parsed.get("fix_instruction") or default_fix)[:500]
    return et, _root(et), fix


def _deterministic_schema_fix(error_text: str, code_ctx: str) -> str:
    """Return high-confidence Terraform schema repair hints for recurring errors."""
    text = f"{error_text}\n{code_ctx}".lower()
    hints: list[str] = []

    if "block_public_access" in text and "aws_s3_bucket" in text:
        hints.append(
            "For S3 public access blocking, remove any `block_public_access` block from "
            "`aws_s3_bucket` and create a separate `aws_s3_bucket_public_access_block` "
            "resource with `bucket = aws_s3_bucket.<name>.id`."
        )
    if "aws_lightsail_disk" in text and ("encrypted" in text or "encryption" in text):
        hints.append(
            "`aws_lightsail_disk` does not support an `encrypted`/`encryption_enabled` "
            "argument; remove it. Lightsail disks are encrypted by AWS."
        )
    if "aws_lightsail_instance" in text and "publicly_accessible" in text:
        hints.append(
            "`aws_lightsail_instance` does not support `publicly_accessible`; remove that "
            "argument. Use valid Lightsail arguments only, and keep disk attachments "
            "referencing the instance/disk resource names."
        )
    if "aws_ami" in text and (
        "invalid resource type" in text
        or "unsupported argument" in text
        or "most_recent" in text
        or "owners" in text
    ):
        hints.append(
            "When selecting an existing/latest AMI, use `data \"aws_ami\" \"<name>\"`, not "
            "`resource \"aws_ami\"`. Reference it as `data.aws_ami.<name>.id` from EC2 "
            "instances."
        )
    if "aws_s3_bucket" in text and "server_side_encryption_configuration" in text:
        hints.append(
            "For S3 default encryption with AWS provider ~> 5.0, do not put "
            "`server_side_encryption_configuration` inside `aws_s3_bucket`. Create a "
            "separate `aws_s3_bucket_server_side_encryption_configuration` resource with "
            "`bucket = aws_s3_bucket.<name>.id`, `rule { apply_server_side_encryption_by_default { ... } }`."
        )
    if "aws_kms_key" in text and "aws_s3_bucket" in text and (
        "kms_master_key_id" in text or "sse_algorithm" in text
    ):
        hints.append(
            "For S3 SSE-KMS, set `sse_algorithm = \"aws:kms\"` and "
            "`kms_master_key_id = aws_kms_key.<name>.arn` inside "
            "`aws_s3_bucket_server_side_encryption_configuration.rule.apply_server_side_encryption_by_default`."
        )
    if "aws_secretsmanager_secret_version" in text and (
        "secret_string" in text or "secret_binary" in text or "missing required" in text
    ):
        hints.append(
            "`aws_secretsmanager_secret_version` requires exactly one secret value. Set "
            "`secret_string = jsonencode({...})` or another non-empty string, and keep "
            "`secret_id = aws_secretsmanager_secret.<name>.id`."
        )
    if "aws_elasticache_user_group" in text:
        if "user_ids" in text or "missing required" in text:
            hints.append(
                "`aws_elasticache_user_group` requires `engine` and `user_ids`. Create or "
                "reference `aws_elasticache_user` resources and set "
                "`user_ids = [aws_elasticache_user.<name>.user_id]`."
            )
        if "unsupported block" in text or "user {" in text:
            hints.append(
                "Do not use nested `user {}` blocks in `aws_elasticache_user_group`; use "
                "the `user_ids` list argument instead."
            )
    if "aws_elasticache_user" in text and "authentication_mode" in text:
        hints.append(
            "`aws_elasticache_user.authentication_mode` is a block, e.g. "
            "`authentication_mode { type = \"password\" passwords = [\"...\"] }`; do not "
            "set it as a plain string and do not use a separate top-level `passwords` argument."
        )
    if "aws_codebuild_project" in text:
        if "build_spec" in text:
            hints.append(
                "`aws_codebuild_project` uses `buildspec` with no underscore, and it belongs "
                "inside the `source { ... }` block for AWS provider ~> 5.0."
            )
        if "source_version" in text and "no_source" in text:
            hints.append(
                "For `aws_codebuild_project` with `source.type = \"NO_SOURCE\"`, remove "
                "`source_version`; use `buildspec` inside the `source` block instead."
            )
        if "buildspec" in text and ("not expected" in text or "not allowed" in text):
            hints.append(
                "In `aws_codebuild_project`, `buildspec` belongs inside the `source { ... }` "
                "block, not as a top-level argument."
            )
        if "secondary_source" in text:
            hints.append(
                "Use AWS provider block names `secondary_sources { ... }` and "
                "`secondary_artifacts { ... }` for CodeBuild; singular names are invalid."
            )
        if "missing required" in text and "environment" in text:
            hints.append(
                "`aws_codebuild_project` requires an `environment { compute_type, image, type }` block."
            )
        if "missing required" in text and "artifacts" in text:
            hints.append(
                "`aws_codebuild_project` requires an `artifacts { type = ... }` block; use "
                "`NO_ARTIFACTS` when the project does not publish artifacts."
            )
    if "endpoint_configuration" in text and "aws_api_gateway_rest_api" in text:
        hints.append(
            "For `aws_api_gateway_rest_api`, write `endpoint_configuration { ... }` as a "
            "block, not `endpoint_configuration = { ... }`."
        )
    if "multiple ec2 subnets matched" in text or (
        "data.aws_subnet" in text and "multiple" in text and "matched" in text
    ):
        hints.append(
            "Do not use an ambiguous `data.aws_subnet` lookup. If the prompt does not require "
            "an existing subnet, create an `aws_vpc` and `aws_subnet` resource and reference "
            "the created subnet. If an existing subnet is required, filter by exact id or a "
            "unique tag/AZ/VPC combination."
        )
    if "aws_kinesis_firehose_delivery_stream" in text and "splunk" in text:
        hints.append(
            "For Firehose Splunk destinations, use `splunk_configuration { ... }` with "
            "required HEC arguments and put S3 backup settings in nested "
            "`s3_configuration { ... }` inside `splunk_configuration`. Use a fixed `name`, "
            "not `name_prefix`, for the delivery stream. Do not put `role_arn` at the "
            "top level; put it inside the nested `s3_configuration` block. Use "
            "`buffering_size` and `buffering_interval`, not `buffer_size` or "
            "`buffer_interval`."
        )
    if "aws_kinesis_firehose_delivery_stream" in text and "name_prefix" in text:
        hints.append(
            "`aws_kinesis_firehose_delivery_stream` does not support `name_prefix`; use the "
            "required `name` argument with a deployable generated string."
        )
    if "aws_backup_plan" in text and "advanced_backup_setting" in text:
        hints.append(
            "In `aws_backup_plan`, `advanced_backup_setting` is a top-level block, not a "
            "nested `rule` block. Also include the required backup plan `name` argument, "
            "`resource_type = \"EC2\"`, and a non-empty `backup_options` map."
        )
    if "advanced backup setting" in text and "resource type or backup options is null" in text:
        hints.append(
            "For `aws_backup_plan.advanced_backup_setting`, set both "
            "`resource_type = \"EC2\"` and a non-empty `backup_options` map; do not emit an "
            "empty `advanced_backup_setting` block."
        )
    if "data.aws_ssm_parameter" in text and "couldn't find resource" in text:
        hints.append(
            "Do not use a `data.aws_ssm_parameter` lookup for a parameter that this same "
            "configuration creates. Remove the data source and reference the "
            "`aws_ssm_parameter` resource directly, or omit SSM entirely if it is not "
            "required by the prompt."
        )
    if "replication_configuration" in text and "self" in text and "aws_s3_bucket" in text:
        hints.append(
            "Do not configure S3 replication from a bucket to itself. Use a distinct "
            "destination bucket resource/ARN or remove replication if the prompt does not "
            "require it."
        )
    if "unsupported argument" in text or "unsupported block type" in text:
        hints.append(
            "Do not rename the resource to work around this. Remove or move the unsupported "
            "argument/block to the AWS provider ~> 5.0 resource/block where it is valid."
        )
    if "missing required argument" in text or "missing required block" in text:
        hints.append(
            "Add the exact missing required argument/block named by Terraform using the "
            "provider schema for that resource."
        )

    return "\n".join(f"- {h}" for h in hints)


def validation_node(state: AgentState) -> dict:
    """LangGraph node function cho Validation Agent (Agent 4)."""
    code = state["generated_code"]
    # ckv_ids_map: {"type.name": ["CKV_AWS_NNN", ...]} — A2 gán, A4 chạy checkov --check
    ckv_ids_map  = state.get("security_ckv_ids") or {}
    all_ckv_ids  = sorted({ckv for ids in ckv_ids_map.values() for ckv in ids})
    plan_obj = state.get("infrastructure_plan") or {}
    plan_timeout = state.get("terraform_plan_timeout", 120)
    _no_checkov = {"passed_count": 0, "failed": []}

    if not (code or "").strip():
        return _fail_return(
            state, "MISSING_RESOURCE", "architecture",
            "generated_code rỗng — Engineering agent không sinh được HCL. "
            "Kiểm tra lại toàn bộ pipeline từ Agent 1.",
            _no_checkov, False, False, ["empty_code"],
        )

    run_dir = state.get("run_dir") or ""
    files_dir = (Path(run_dir) / "files") if run_dir else None

    with terraform_workdir(run_dir or None, "a4") as d:
        write_terraform_dir(d, code, files_dir=files_dir)

        # ── terraform init ─────────────────────────────────────────────────────
        try:
            init = run_terraform(["terraform", "init", "-no-color"], d, _INIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _infra_return(state, f"terraform init timed out (>{_INIT_TIMEOUT}s)", _no_checkov, False, False)
        if init.returncode != 0:
            # Combine stderr+stdout: terraform init writes "problems with the configuration"
            # to stdout but actual Error: lines to stderr — need both to detect config errors.
            init_err = ((init.stderr or "") + "\n" + (init.stdout or "")).strip()
            # "problems with the configuration" → lỗi code (resource type không tồn tại,
            # block sai cấu trúc) → route về engineering để sửa HCL.
            # Các lỗi khác (network, lock file) → INFRA → requires_human.
            if "problems with the configuration" in init_err or init_err.startswith("Error: Invalid"):
                sig = _error_signature("SYNTAX", init_err, "", [])
                return _fail_return(
                    state, "SYNTAX", "engineering",
                    f"terraform init failed — fix the HCL:\n{init_err[:600]}",
                    _no_checkov, False, False, sig, raw_error=init_err[:2000],
                )
            return _infra_return(state, f"terraform init failed: {init_err[:500]}", _no_checkov, False, False, raw_error=init_err[:2000])

        # ── terraform validate ─────────────────────────────────────────────────
        try:
            val = run_terraform(["terraform", "validate", "-no-color"], d, _VALIDATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _infra_return(state, "terraform validate timed out", _no_checkov, False, False)

        if val.returncode != 0:
            validate_err = (val.stderr or val.stdout or "").strip()
            sig = _error_signature("SYNTAX", validate_err, "", [])
            logger.info("Agent 4: FAIL SYNTAX (validate)")
            code_ctx = _extract_code_context(validate_err, code)
            # Dùng LLM để sinh fix cụ thể: nói ĐÚNG phải viết gì thay thế,
            # không chỉ paste lại error message (Agent 3 không tự suy ra được).
            syntax_ctx = (
                _TOP
                + f"TERRAFORM VALIDATE FAILED:\n{validate_err[:600]}\n\n"
                + (f"FAILING CODE CONTEXT:\n{code_ctx}\n\n" if code_ctx else "")
                + f"GENERATED HCL RESOURCES: {_hcl_resource_labels(code)}\n"
                + f"ERROR HISTORY: {json.dumps(state['error_history'][-2:])}"
                + _BOTTOM
            )
            _, _, fix = _llm_classify(
                syntax_ctx, {"SYNTAX"}, "SYNTAX",
                f"terraform validate failed — fix the HCL: {validate_err[:300]}"
            )
            deterministic_fix = _deterministic_schema_fix(validate_err, code_ctx)
            if deterministic_fix:
                fix = f"{deterministic_fix}\n\nLLM-specific fix:\n{fix}"[:800]
            return _fail_return(
                state, "SYNTAX", "engineering", fix,
                _no_checkov, False, False, sig, raw_error=validate_err[:2000])

        # ── Validate passed → Checkov (non-blocking — log only) ──────────────────
        if all_ckv_ids:
            try:
                ck = run_checkov_on_hcl(code, timeout=60, check_ids=all_ckv_ids)
                checkov = {"passed_count": ck["passed_count"], "failed": sorted(ck["failed_ckv_ids"])}
                if ck["failed_ckv_ids"]:
                    logger.info("Agent 4: Checkov partial fail (non-blocking) — %s", ck["failed_ckv_ids"])
            except RuntimeError as e:
                logger.warning("Checkov không chạy được (%s) — skipped", e)
                checkov = dict(_no_checkov)
        else:
            checkov = dict(_no_checkov)

        # ── terraform plan ─────────────────────────────────────────────────────
        plan_passed, plan_err = True, ""
        try:
            plan = run_terraform(["terraform", "plan", "-no-color"], d, plan_timeout)
        except subprocess.TimeoutExpired:
            return _infra_return(state, f"terraform plan timed out (>{plan_timeout}s)", checkov, True, False)
        plan_passed = plan.returncode == 0
        plan_err = (plan.stderr or plan.stdout or "").strip()

    # ── overall_passed ──────────────────────────────────────────────────────────
    if plan_passed:
        logger.info("Agent 4: PASS")
        return _success_result(checkov)

    # ── Plan failed → phân loại LOGIC / MISSING_RESOURCE ─────────────────────
    ctx = (
        _TOP
        + f"TERRAFORM VALIDATE: passed\nTERRAFORM PLAN: FAILED\n{plan_err[:1500]}\n\n"
        + f"GENERATED HCL RESOURCES: {_hcl_resource_labels(code)}\n"
        + f"ERROR HISTORY: {json.dumps(state['error_history'][-3:])}"
        + _BOTTOM
    )
    error_type, root_cause, fix_instruction = _llm_classify(
        ctx, {"LOGIC", "MISSING_RESOURCE"}, "LOGIC",
        f"terraform plan failed: {plan_err[:300]}")
    deterministic_fix = _deterministic_schema_fix(plan_err, "")
    if deterministic_fix and root_cause == "engineering":
        fix_instruction = f"{deterministic_fix}\n\nLLM-specific fix:\n{fix_instruction}"[:800]
    sig = _error_signature(error_type, "", plan_err, [])
    logger.info("Agent 4: FAIL %s (plan)", error_type)

    return _fail_return(state, error_type, root_cause, fix_instruction,
                        checkov, True, False, sig, raw_error=plan_err[:2000])


def route_after_validation(state: AgentState) -> str:
    """Conditional edge: node kế tiếp sau Agent 4. KHÔNG ghi state."""
    if state["fix_feedback"]["overall_passed"]:
        return "agent5"

    history = state["error_history"]
    if len(history) >= 2 and history[-1]["failed_checks"] == history[-2]["failed_checks"]:
        return "requires_human"
    if len(history) >= 3:
        current = history[-1]["failed_checks"]
        if any(h["failed_checks"] == current for h in history[:-1]):
            return "requires_human"
    if state["total_retry_count"] >= 5:
        return "requires_human"

    error_type = state["fix_feedback"]["error_type"]
    if error_type == "INFRA":
        return "requires_human"
    if error_type == "MISSING_RESOURCE" and state["arch_retry_count"] >= 2:
        return "requires_human"
    if error_type == "WRONG_CONSTRAINT" and state["sec_retry_count"] >= 2:
        return "requires_human"
    if error_type in ("SECURITY", "SYNTAX", "LOGIC") and state["eng_retry_count"] >= 3:
        return "requires_human"

    _ROUTE_MAP = {"architecture": "architecture", "security": "security", "engineering": "engineering"}
    root_cause = state["fix_feedback"]["root_cause"]
    if root_cause not in _ROUTE_MAP:
        logger.error("Invalid root_cause '%s' — route requires_human", root_cause)
        return "requires_human"
    return _ROUTE_MAP[root_cause]
