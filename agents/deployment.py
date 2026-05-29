"""Deployment Agent — Agent 5 trong pipeline.

Thực thi `terraform apply` lên Floci. Nếu fail:
  - kiểm tra partial apply (state list) → terraform destroy để cleanup dirty state;
  - phân loại lỗi rồi route.

Phân loại:
  - Timeout / connection → TRANSIENT → retry A5 (deploy_retry_count, tối đa 2 lần)
  - FIXABLE → A3 sửa code (deploy_retry_count <= 2, tối đa 2 lần route về A3)
  - MISSING_RESOURCE → A1 re-plan (deploy_retry_count <= 2, tối đa 2 lần route về A1)
  - Còn lại → LLM phân loại FIXABLE / UNKNOWN
    FIXABLE → A3 sửa code (qua fix_feedback, tăng eng_retry_count, deploy_retry <= 2)
    UNKNOWN → requires_human

State writes:
  - deployment_result: luôn cập nhật
  - deploy_retry_count: chỉ tăng khi A5 xử lý lỗi (cả TRANSIENT lẫn FIXABLE/UNKNOWN)
  - fix_feedback + eng_retry_count: chỉ set khi FIXABLE để trigger A3 retry
"""
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

# Attrs that prevent AWS resource deletion — must be disabled before destroy.
# Each tuple: (pattern, replacement). Applied in order via re.sub.
_DESTROY_PATCHES = [
    # DynamoDB deletion protection
    (r'(deletion_protection_enabled\s*=\s*)true', r'\g<1>false'),
    # RDS / Aurora / DocumentDB / ALB deletion protection
    (r'(deletion_protection\s*=\s*)true', r'\g<1>false'),
    # RDS: must skip final snapshot when destroying
    (r'(skip_final_snapshot\s*=\s*)false', r'\g<1>true'),
    # RDS: remove final_snapshot_identifier (conflicts with skip_final_snapshot=true)
    (r'\n[ \t]*final_snapshot_identifier\s*=\s*[^\n]+', ''),
    # ElastiCache: remove final snapshot to speed up deletion
    (r'\n[ \t]*final_snapshot_identifier\s*=\s*[^\n]+', ''),
    (r'(apply_immediately\s*=\s*)false', r'\g<1>true'),
    # ElastiCache: disable multi-AZ to allow faster deletion
    (r'(automatic_failover_enabled\s*=\s*)true', r'\g<1>false'),
    (r'(multi_az_enabled\s*=\s*)true', r'\g<1>false'),
]


def _patch_for_destroy(code: str) -> str:
    """Disable deletion-protection attrs so terraform destroy can succeed."""
    for pattern, replacement in _DESTROY_PATCHES:
        code = re.sub(pattern, replacement, code)
    return code


def _prepare_destroy_config(tmpdir: str | Path) -> None:
    """Best-effort config patch before destroy for resources with delete protection."""
    tf_path = Path(tmpdir) / "main.tf"
    if not tf_path.exists():
        return
    original = tf_path.read_text(encoding="utf-8")
    patched = _patch_for_destroy(original)
    if patched == original:
        return
    logger.info("Agent 5: patching deletion-protection attrs before cleanup destroy")
    tf_path.write_text(patched, encoding="utf-8")
    try:
        run_terraform(
            ["terraform", "apply", "-auto-approve", "-no-color"],
            tmpdir,
            min(_APPLY_TIMEOUT, 180),
        )
    except subprocess.TimeoutExpired:
        pass  # best-effort; destroy may still succeed with patched config

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from core.terraform import run_terraform, write_terraform_dir, terraform_workdir
from prompts.deployment import SYSTEM_PROMPT as _SYSTEM_PROMPT
from prompts.deployment import TOP_PROMPT as _TOP, BOTTOM_PROMPT as _BOTTOM

logger = logging.getLogger(__name__)

_INIT_TIMEOUT = 60
_APPLY_TIMEOUT = 360
_DESTROY_TIMEOUT = 600  # ElastiCache/RDS deletion có thể mất 5-10 phút
_STATE_TIMEOUT = 30

_TRANSIENT_PATTERNS = (
    "connection refused", "connection reset", "could not connect",
    "timeout", "timed out", "i/o timeout", "eof", "no such host",
    # AWS rate limits / quota — retry thay vì fail
    "requestlimitexceeded", "throttling", "rate exceeded",
    "vpcquotaexceeded", "limitexceeded",
)


def _deterministic_deploy_fix(error_text: str) -> tuple[str | None, str | None]:
    """Classify common AWS apply errors without spending retries on the wrong route."""
    text = (error_text or "").lower()

    if "putfunctionconcurrency" in text or "reservedconcurrentexecutions" in text:
        return (
            "FIXABLE",
            "Remove `reserved_concurrent_executions` from the failing `aws_lambda_function` "
            "unless the prompt explicitly asks for reserved concurrency. Account concurrency "
            "limits are environment-specific and should not be hard-coded in generated examples.",
        )

    if "route 53" in text and ("invaliddomainname" in text or "reserved by aws" in text):
        return (
            "FIXABLE",
            "For `aws_route53_zone.name`, do not use reserved names such as `example` or "
            "`example.com`. Use a syntactically valid generated domain such as "
            "`generated-${random_id.<name>.hex}.com` and reference it consistently.",
        )

    if "elasticache" in text and "user id" in text and "must begin" in text:
        return (
            "FIXABLE",
            "For `aws_elasticache_user.user_id`, use a compliant id: start with a letter, "
            "use only letters, digits, and hyphens, and avoid underscores/consecutive hyphens.",
        )

    if "does not support specifying cpuoptions" in text:
        return (
            "FIXABLE",
            "For `aws_instance`, do not use `t2.micro` with `cpu_options`. If the prompt "
            "requires CPU options, use an instance type that supports them such as `t3.micro` "
            "or `m5.large`; "
            "otherwise remove the `cpu_options` block.",
        )

    if "not eligible for free tier" in text and "cpu_options" in text:
        return (
            "FIXABLE",
            "This AWS account requires a Free Tier eligible instance type. Use `t3.micro` "
            "with `cpu_options { core_count = 1, threads_per_core = 2 }`, or remove "
            "`cpu_options` if the prompt does not require it.",
        )

    if "invalidparametercombination" in text and "instance type" in text:
        return (
            "FIXABLE",
            "Change the `aws_instance.instance_type` to one compatible with the selected AMI, "
            "architecture, and any `cpu_options`. If the error says the account requires "
            "Free Tier eligibility, use `t3.micro` with `core_count = 1` and "
            "`threads_per_core = 2`; do not use `t2.micro` with CPU options.",
        )

    if "iaminstanceprofile" in text or "iam instance profile" in text:
        return (
            "MISSING_RESOURCE",
            "Add an `aws_iam_instance_profile` resource backed by the EC2 IAM role and set "
            "`aws_instance.<name>.iam_instance_profile` to that profile name.",
        )

    if "invalidkey.format" in text or "importkeypair" in text:
        return (
            "FIXABLE",
            "Use a valid OpenSSH public key for `aws_key_pair.public_key`. If the prompt gives "
            "a local key path such as `./key.pub`, use `public_key = file(\"./key.pub\")` and "
            "ensure the stub file contains a valid `ssh-rsa` public key.",
        )

    if "bucketalreadyexists" in text or "s3 bucket" in text and "already exists" in text:
        return (
            "FIXABLE",
            "S3 bucket names must be globally unique. Replace fixed `bucket = \"example...\"` "
            "names with `bucket_prefix` or append a `random_id` suffix consistently to all "
            "references.",
        )

    if "entityalreadyexists" in text and "role" in text:
        return (
            "FIXABLE",
            "IAM role names are account-global. Replace fixed `name` on `aws_iam_role` with "
            "`name_prefix` or append a random suffix, and keep all references resource-based.",
        )

    if "codebuild" in text and "buildspec must be set" in text:
        return (
            "FIXABLE",
            "For `aws_codebuild_project` with `source.type = \"NO_SOURCE\"`, put a valid "
            "`buildspec` string inside the `source { ... }` block.",
        )

    if "codebuild" in text and "invalid artifacts" in text and "location" in text:
        return (
            "FIXABLE",
            "For CodeBuild S3 artifacts, set `artifacts.location` to the bucket name only, "
            "without a slash or object path. Put object names in `artifacts.name`.",
        )

    if "api gateway" in text and "no integration defined for method" in text:
        return (
            "FIXABLE",
            "For `aws_api_gateway_deployment`, add `depends_on` containing every "
            "`aws_api_gateway_integration` used by the API. Ensure each method has a matching "
            "integration before the deployment resource is created.",
        )

    if "advanced backup setting" in text and "resource type or backup options is null" in text:
        return (
            "FIXABLE",
            "For `aws_backup_plan.advanced_backup_setting`, set both "
            "`resource_type = \"EC2\"` and a non-empty `backup_options` map. Do not leave the "
            "advanced backup setting block empty.",
        )

    if "lightsail" in text and ("some names are already in use" in text or "already" in text):
        return (
            "FIXABLE",
            "Lightsail names must be unique in the account/region. Use stable generated names "
            "for all related Lightsail resources and make disk attachments reference the "
            "resource names, not repeated fixed strings.",
        )

    if "lightsail" in text and "notfoundexception" in text and "disk" in text:
        return (
            "FIXABLE",
            "For Lightsail disk attachments, reference the disk and instance resource names "
            "directly and add `depends_on` for the disk/instance if AWS eventually consistent "
            "creation causes attach to run too early.",
        )

    if "putbucketnotificationconfiguration" in text:
        return (
            "FIXABLE",
            "When configuring S3 bucket notifications to SNS/Lambda/SQS, add the destination "
            "permission policy first and make the notification resource depend on it.",
        )

    if "deletionprotectionenabled" in text or "delete protected" in text:
        return (
            "FIXABLE",
            "Set deletion protection attributes to false for benchmark-generated resources so "
            "auto-destroy can clean them up.",
        )

    return None, None


def _matches(text: str, patterns: tuple) -> bool:
    low = (text or "").lower()
    return any(p in low for p in patterns)


def _extract_error(stdout: str, stderr: str) -> str:
    """Trích error text từ terraform apply output.

    Terraform ghi plan vào stdout (dài) và lỗi vào stderr (ngắn).
    Nếu chỉ lấy tail của (stderr+stdout), stderr ngắn bị cắt mất.
    Fix: giữ toàn bộ stderr + tail của stdout để LLM luôn thấy lỗi thực.
    """
    stderr_clean = (stderr or "").strip()
    stdout_tail = (stdout or "")[-2000:]
    combined = (stderr_clean + "\n" + stdout_tail).strip()
    error_lines = [ln for ln in combined.splitlines() if re.match(r"\s*(?:Error|error):", ln)]
    if error_lines:
        return combined + "\n\n--- Error lines ---\n" + "\n".join(error_lines[-20:])
    return combined


def _resource_labels(plan: dict) -> list[str]:
    return [f"{r['type']}.{r['name']}" for r in plan.get("resources", [])]


def _guess_failed_resource(error_text: str, labels: list[str]) -> str | None:
    """Đoán resource gây lỗi từ error text — cung cấp hint cho LLM."""
    for label in labels:
        rtype, rname = label.split(".", 1)
        if rtype in error_text or rname in error_text or label in error_text:
            return label
    return None


def _deploy_result(success: bool, error_type: str | None, *, fix_instruction=None,
                   resources_created=None, partial_apply_destroyed=False,
                   destroy_failed=False, destroy_error=None, apply_raw_error=None) -> dict:
    return {
        "success": success,
        "error_type": error_type,
        "resources_created": resources_created or [],
        "partial_apply_destroyed": partial_apply_destroyed,
        "destroy_failed": destroy_failed,
        "destroy_error": destroy_error,
        "fix_instruction": fix_instruction,
        "apply_raw_error": apply_raw_error,
    }


def _state_resources(tmpdir: str) -> list:
    try:
        r = run_terraform(["terraform", "state", "list"], tmpdir, _STATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _llm_classify_deploy(
    error_text: str,
    resource_labels: list[str],
    failed_resource: str | None,
    partial: bool,
    destroyed: bool,
    retry: int,
) -> tuple[str, str | None]:
    """LLM phân loại FIXABLE / UNKNOWN + sinh fix_instruction. Fallback UNKNOWN."""
    ctx = (
        _TOP
        + f"RESOURCE LIST: {json.dumps(resource_labels)}\n"
        + f"SUSPECTED FAILED RESOURCE: {failed_resource or 'unknown'}\n\n"
        + f"APPLY ERROR:\n{error_text[:2000]}\n\n"
        + f"PARTIAL APPLY: {partial} | DESTROYED: {destroyed} | DEPLOY RETRY: {retry}"
        + _BOTTOM
    )
    try:
        parsed = parse_llm_json(
            call_llm([{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": ctx}]),
            {"error_type": None, "fix_instruction": None},
        )
    except Exception as e:
        logger.warning("Agent 5 LLM classify error (%s) — UNKNOWN", e)
        return "UNKNOWN", None
    et = parsed.get("error_type")
    if et not in ("FIXABLE", "MISSING_RESOURCE", "PERMISSION", "QUOTA", "UNKNOWN"):
        et = "UNKNOWN"
    fix = parsed.get("fix_instruction") if et in ("FIXABLE", "MISSING_RESOURCE") else None
    return et, (str(fix)[:500] if fix else None)


def _handle_failure(
    state: AgentState, tmpdir: str,
    apply_stdout: str, apply_stderr: str,
    is_timeout: bool,
) -> dict:
    """Xử lý apply fail: cleanup partial state, phân loại, trả dict update."""
    error_text = _extract_error(apply_stdout, apply_stderr)
    plan = state.get("infrastructure_plan") or {}
    resource_labels = _resource_labels(plan)

    # Pattern-based classification (tất định, không cần LLM)
    deterministic_error_type, deterministic_fix = _deterministic_deploy_fix(error_text)
    if is_timeout:
        error_type = "TRANSIENT"
    elif deterministic_error_type:
        error_type = deterministic_error_type
    elif _matches(error_text, _TRANSIENT_PATTERNS):
        error_type = "TRANSIENT"
    else:
        error_type = None  # cần LLM

    # Khi timeout, terraform bị SIGKILL giữa chừng — state file có thể rỗng/corrupt.
    # Chạy refresh trước để rebuild state từ AWS thực tế.
    if is_timeout:
        try:
            run_terraform(["terraform", "refresh", "-no-color"], tmpdir, 60)
        except subprocess.TimeoutExpired:
            pass  # best-effort

    # Cleanup partial state — LUÔN chạy destroy (safe nếu state rỗng: no-op).
    created = _state_resources(tmpdir)
    partial = bool(created)
    partial_destroyed = destroy_failed = False
    destroy_error = None

    _prepare_destroy_config(tmpdir)
    try:
        destroy = run_terraform(
            ["terraform", "destroy", "-auto-approve", "-no-color"],
            tmpdir, _DESTROY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        destroy_failed = True
        destroy_error = f"terraform destroy timed out (>{_DESTROY_TIMEOUT}s)"
    else:
        if destroy.returncode == 0:
            partial_destroyed = True
        else:
            destroy_failed = True
            destroy_error = (destroy.stderr or "")[:500]

    # LLM classify chỉ khi chưa xác định được error_type
    fix = deterministic_fix
    if error_type is None:
        failed_resource = _guess_failed_resource(error_text, resource_labels)
        error_type, fix = _llm_classify_deploy(
            error_text, resource_labels, failed_resource,
            partial, partial_destroyed, state["deploy_retry_count"],
        )

    logger.info(
        "Agent 5: FAIL %s (partial=%s destroyed=%s destroy_failed=%s)",
        error_type, partial, partial_destroyed, destroy_failed,
    )

    result: dict = {
        "deployment_result": _deploy_result(
            False, error_type,
            fix_instruction=fix,
            resources_created=created,
            partial_apply_destroyed=partial_destroyed,
            destroy_failed=destroy_failed,
            destroy_error=destroy_error,
            apply_raw_error=error_text[:3000],
        ),
        "deploy_retry_count": state["deploy_retry_count"] + 1,
    }

    # FIXABLE: HCL code sai → route về A3 để sửa code.
    if error_type == "FIXABLE" and not destroy_failed:
        result["fix_feedback"] = {
            "overall_passed": False,
            "error_type": "LOGIC",
            "root_cause": "engineering",
            "fix_instruction": fix,
            "checkov": {"passed_count": 0, "failed": []},
            "validate_passed": True,
            "plan_passed": True,
        }
        result["deploy_eng_retry_count"] = state.get("deploy_eng_retry_count", 0) + 1

    # MISSING_RESOURCE: resource thiếu trong plan → route về A1 để re-plan.
    if error_type == "MISSING_RESOURCE" and not destroy_failed:
        result["fix_feedback"] = {
            "overall_passed": False,
            "error_type": "MISSING_RESOURCE",
            "root_cause": "architecture",
            "fix_instruction": fix,
            "checkov": {"passed_count": 0, "failed": []},
            "validate_passed": True,
            "plan_passed": True,
        }
        result["arch_retry_count"] = state["arch_retry_count"] + 1

    return result


def destroy_resources(code: str) -> dict:
    """Chạy terraform init + destroy trên HCL code. Không cần state — dùng độc lập.

    Returns:
        {"success": bool, "error": str | None, "resources_destroyed": list[str]}
    """
    with tempfile.TemporaryDirectory() as d:
        write_terraform_dir(d, code)

        logger.info("destroy: terraform init (timeout=%ds)", _INIT_TIMEOUT)
        try:
            init = run_terraform(["terraform", "init", "-no-color"], d, _INIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"terraform init timed out (>{_INIT_TIMEOUT}s)",
                    "resources_destroyed": []}

        if init.returncode != 0:
            return {"success": False, "error": f"terraform init failed: {init.stderr[:300]}",
                    "resources_destroyed": []}

        resources = _state_resources(d)

        logger.info("destroy: terraform destroy (timeout=%ds)", _DESTROY_TIMEOUT)
        try:
            result = run_terraform(
                ["terraform", "destroy", "-auto-approve", "-no-color"], d, _DESTROY_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {"success": False,
                    "error": f"terraform destroy timed out (>{_DESTROY_TIMEOUT}s)",
                    "resources_destroyed": []}

        if result.returncode == 0:
            logger.info("destroy: OK — %d resources", len(resources))
            return {"success": True, "error": None, "resources_destroyed": resources}

        error = _extract_error(result.stdout or "", result.stderr or "")
        logger.error("destroy: FAILED — %s", error[:200])
        return {"success": False, "error": error[:500], "resources_destroyed": []}


def deployment_node(state: AgentState) -> dict:
    """LangGraph node function cho Deployment Agent (Agent 5)."""
    code = state["generated_code"]

    logger.info(
        "Agent 5: deploy_retry=%d eng_retry=%d",
        state.get("deploy_retry_count", 0),
        state.get("eng_retry_count", 0),
    )

    run_dir = state.get("run_dir") or ""
    files_dir = (Path(run_dir) / "files") if run_dir else None

    with terraform_workdir(run_dir or None, "a5") as d:
        write_terraform_dir(d, code, files_dir=files_dir)

        logger.info("Agent 5: terraform init (timeout=%ds)", _INIT_TIMEOUT)
        try:
            init = run_terraform(["terraform", "init", "-no-color"], d, _INIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.error("Agent 5: terraform init TIMEOUT")
            return {
                "deployment_result": _deploy_result(
                    False, "TRANSIENT",
                    fix_instruction=f"terraform init timed out (>{_INIT_TIMEOUT}s)",
                ),
                "deploy_retry_count": state["deploy_retry_count"] + 1,
            }

        if init.returncode != 0:
            logger.error("Agent 5: terraform init FAILED")
            return {
                "deployment_result": _deploy_result(
                    False, "TRANSIENT",
                    fix_instruction=f"terraform init failed: {init.stderr[:300]}",
                ),
                "deploy_retry_count": state["deploy_retry_count"] + 1,
            }

        logger.info("Agent 5: terraform apply (timeout=%ds)", _APPLY_TIMEOUT)
        try:
            apply = run_terraform(
                ["terraform", "apply", "-auto-approve", "-no-color"], d, _APPLY_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            logger.error("Agent 5: terraform apply TIMEOUT")
            return _handle_failure(
                state, d, "", "terraform apply timed out", is_timeout=True
            )

        if apply.returncode == 0:
            created = _state_resources(d)
            logger.info("Agent 5: APPLY OK — %d resources", len(created))

            auto_destroyed = False
            auto_destroy_error = None
            if state.get("auto_destroy"):
                logger.info("Agent 5: auto-destroy (eval mode)")
                _prepare_destroy_config(d)
                try:
                    cleanup = run_terraform(
                        ["terraform", "destroy", "-auto-approve", "-no-color"],
                        d, _DESTROY_TIMEOUT,
                    )
                    auto_destroyed = cleanup.returncode == 0
                    if not auto_destroyed:
                        auto_destroy_error = (cleanup.stderr or "")[:300]
                        logger.warning("Agent 5: auto-destroy FAILED — %s", auto_destroy_error)
                    else:
                        logger.info("Agent 5: auto-destroy OK")
                except subprocess.TimeoutExpired:
                    auto_destroy_error = f"terraform destroy timed out (>{_DESTROY_TIMEOUT}s)"
                    logger.warning("Agent 5: auto-destroy TIMEOUT")

            result = _deploy_result(True, None, resources_created=created)
            result["auto_destroyed"] = auto_destroyed
            result["auto_destroy_error"] = auto_destroy_error
            return {"deployment_result": result}

        return _handle_failure(
            state, d, apply.stdout or "", apply.stderr or "", is_timeout=False
        )


def route_after_deployment(state: AgentState) -> str:
    """Conditional edge sau Agent 5. KHÔNG ghi state."""
    dr = state["deployment_result"]

    if dr["success"]:
        return "end"

    # Dirty state không cleanup được → luôn cần người can thiệp
    if dr.get("destroy_failed"):
        return "requires_human"

    error_type = dr["error_type"]
    deploy_retry = state["deploy_retry_count"]  # đã +1 trong node

    deploy_eng_retry = state.get("deploy_eng_retry_count", 0)  # đã +1 trong node
    if error_type == "TRANSIENT" and deploy_retry <= 2:
        return "agent5"
    if error_type == "FIXABLE" and deploy_eng_retry <= 3:
        return "engineering"
    if error_type == "MISSING_RESOURCE" and deploy_retry <= 2:
        return "architecture"
    # PERMISSION, QUOTA, UNKNOWN, retry exhausted — all require human
    logger.info("Agent 5: route requires_human (error_type=%s deploy_retry=%d)", error_type, deploy_retry)
    return "requires_human"
