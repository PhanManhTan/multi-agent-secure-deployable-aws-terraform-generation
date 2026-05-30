"""Engineering Agent (engi) — nhận output của archi_node + secu_node, sinh Terraform HCL.

Input state:
  infrastructure_plan  — JSON plan từ Agent 1 (archi)
  security_ckv_ids     — CKV check IDs từ Agent 2 (secu)

Output state:
  generated_code       — HCL string hoàn chỉnh (provider block đã prepend)
"""
import json
import logging
import os
import re
from pathlib import Path

from core.llm import call_llm
from core.errors import make_fail
from core.parsers import strip_code_block
from prompts.engineering import SYSTEM_PROMPT as _SYSTEM_PROMPT, USER_TEMPLATE as _USER_TEMPLATE

logger = logging.getLogger(__name__)

_PLAN_TAG = re.compile(r"<plan>.*?</plan>", re.DOTALL | re.IGNORECASE)
_PROVIDER_BLOCK = (Path(__file__).parent.parent / "core" / "provider.tf").read_text(encoding="utf-8").strip()
_BLOCK_HEADER = re.compile(r'(?m)^[ \t]*(terraform|provider)\b[^\n{]*\{')
_RESOURCE_DECL_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"')
_DATA_DECL_RE = re.compile(r'data\s+"([^"]+)"\s+"([^"]+)"')
_DATA_S3_BUCKET_HEADER_RE = re.compile(r'data\s+"aws_s3_bucket"\s+"([^"]+)"\s*\{')
_HCL_BLOCK_START = re.compile(r'(?:resource|data|variable|output|module|moved|import)\s+"')
_KEY_PAIR_BLOCK_RE = re.compile(
    r'(resource\s+"aws_key_pair"\s+"[^"]+"\s*\{.*?\n\s*\})',
    re.DOTALL,
)
_S3_BUCKET_HEADER_RE = re.compile(r'resource\s+"aws_s3_bucket"\s+"[^"]+"\s*\{')
_CODEBUILD_HEADER_RE = re.compile(r'resource\s+"aws_codebuild_project"\s+"[^"]+"\s*\{')
_S3_BUCKET_DECL_RE = re.compile(r'resource\s+"aws_s3_bucket"\s+"([^"]+)"')
_RESOURCE_HEADER_TEMPLATE = r'resource\s+"{}"\s+"([^"]+)"\s*\{{'
_IAM_POLICY_DOC_TYPE = "aws_iam_policy_document"
_CANONICAL_DATA_SOURCE_TYPES = {
    _IAM_POLICY_DOC_TYPE,
    "aws_ami",
    "aws_availability_zones",
    "aws_caller_identity",
    "aws_iam_policy_document",
    "aws_partition",
    "aws_region",
}
_AWS_REGION_RE = re.compile(
    r"\b(?:us|eu|ap|ca|sa|me|af|il)(?:-gov)?-[a-z]+(?:-[a-z]+)?-\d\b",
    re.IGNORECASE,
)


def _resource_blocks_of_type(hcl: str, resource_type: str) -> list[tuple[str, str]]:
    header_re = re.compile(rf'resource\s+"{re.escape(resource_type)}"\s+"([^"]+)"\s*\{{')
    blocks: list[tuple[str, str]] = []
    pos = 0
    while True:
        match = header_re.search(hcl or "", pos)
        if not match:
            return blocks
        open_idx = match.end() - 1
        depth = 0
        end_idx = None
        for idx in range(open_idx, len(hcl)):
            if hcl[idx] == "{":
                depth += 1
            elif hcl[idx] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = idx + 1
                    break
        if end_idx is None:
            return blocks
        blocks.append((match.group(1), hcl[match.start():end_idx]))
        pos = end_idx


def _rewrite_resource_blocks(
    hcl: str,
    resource_type: str,
    rewrite,
) -> tuple[str, list[str]]:
    header_re = re.compile(_RESOURCE_HEADER_TEMPLATE.format(re.escape(resource_type)))
    parts: list[str] = []
    repairs: list[str] = []
    pos = 0
    while True:
        match = header_re.search(hcl or "", pos)
        if not match:
            parts.append(hcl[pos:])
            break

        open_idx = match.end() - 1
        depth = 0
        end_idx = None
        for idx in range(open_idx, len(hcl)):
            if hcl[idx] == "{":
                depth += 1
            elif hcl[idx] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = idx + 1
                    break
        if end_idx is None:
            parts.append(hcl[pos:])
            break

        parts.append(hcl[pos:match.start()])
        block = hcl[match.start():end_idx]
        new_block, block_repairs = rewrite(match.group(1), block)
        parts.append(new_block)
        repairs.extend(block_repairs)
        pos = end_idx

    return "".join(parts), sorted(set(repairs))


def _bucket_companion_exists(hcl: str, resource_type: str, bucket_name: str) -> bool:
    bucket_ref_re = re.compile(
        rf'(?m)^\s*bucket\s*=\s*aws_s3_bucket\.{re.escape(bucket_name)}\.(?:id|bucket)\s*$'
    )
    return any(bucket_ref_re.search(block) for _, block in _resource_blocks_of_type(hcl, resource_type))


def _has_resource(hcl: str, resource_type: str, name: str | None = None) -> bool:
    if name:
        return bool(re.search(rf'resource\s+"{re.escape(resource_type)}"\s+"{re.escape(name)}"\s*\{{', hcl or ""))
    return bool(re.search(rf'resource\s+"{re.escape(resource_type)}"\s+"[^"]+"\s*\{{', hcl or ""))


def _strip_preamble(hcl: str) -> str:
    m = _HCL_BLOCK_START.search(hcl)
    return hcl[m.start():] if m else hcl


def _strip_injected_blocks(hcl: str) -> str:
    """Xóa terraform{} / provider{} do LLM sinh (prompt yêu cầu emit nhưng ta prepend tĩnh)."""
    while True:
        m = _BLOCK_HEADER.search(hcl)
        if not m:
            return hcl
        open_idx = m.end() - 1
        depth, end_idx = 0, None
        for i in range(open_idx, len(hcl)):
            if hcl[i] == "{":
                depth += 1
            elif hcl[i] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break
        if end_idx is None:
            return hcl[: m.start()].strip()
        hcl = (hcl[: m.start()] + hcl[end_idx:]).strip()


def _clean_body(raw: str) -> str:
    cleaned = _PLAN_TAG.sub("", raw).strip()
    return _strip_preamble(_strip_injected_blocks(strip_code_block(cleaned).strip()))


def _infer_provider_region(prompt: str) -> str:
    match = _AWS_REGION_RE.search(prompt or "")
    if match:
        return match.group(0).lower()
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"


def _provider_block_for_prompt(prompt: str) -> str:
    region = _infer_provider_region(prompt)
    return re.sub(
        r'region\s*=\s*"[^"]+"',
        f'region = "{region}"',
        _PROVIDER_BLOCK,
        count=1,
    )


def _planned_declarations(plan: dict) -> set[tuple[str, str, str]]:
    declarations: set[tuple[str, str, str]] = set()
    for entry in plan.get("resources") or []:
        if entry.get("type") and entry.get("name"):
            kind = "data" if entry["type"] in _CANONICAL_DATA_SOURCE_TYPES else "resource"
            declarations.add((kind, entry["type"], entry["name"]))
    for entry in plan.get("data_sources") or []:
        if entry.get("type") and entry.get("name"):
            declarations.add(("data", entry["type"], entry["name"]))
    return declarations


def _generated_declarations(hcl: str) -> set[tuple[str, str, str]]:
    declarations = {
        ("data" if t in _CANONICAL_DATA_SOURCE_TYPES else "resource", t, n)
        for t, n in _RESOURCE_DECL_RE.findall(hcl or "")
    }
    declarations.update({("data", t, n) for t, n in _DATA_DECL_RE.findall(hcl or "")})
    return declarations


def _missing_plan_declarations(plan: dict, hcl: str) -> list[tuple[str, str, str]]:
    return sorted(_planned_declarations(plan) - _generated_declarations(hcl))


def _format_declarations(declarations: list[tuple[str, str, str]]) -> str:
    return ", ".join(f'{kind} "{typ}" "{name}"' for kind, typ, name in declarations)


def _replace_invalid_key_pair_public_key(block: str) -> str:
    def repl(match: re.Match) -> str:
        indent, value = match.group(1), match.group(2).strip()
        if value.startswith(("ssh-rsa ", "ssh-ed25519 ", "ecdsa-")):
            return match.group(0)
        return f'{indent}public_key = file("./key.pub")'

    return re.sub(r'(?m)^(\s*)public_key\s*=\s*"([^"]*)"\s*$', repl, block)


def _prompt_mentions_literal(prompt: str, value: str) -> bool:
    return bool(value and value.lower() in (prompt or "").lower())


def _is_placeholder_global_bucket_name(name: str, prompt: str = "") -> bool:
    low = (name or "").lower()
    if _prompt_mentions_literal(prompt, name) and low not in {
        "your-bucket-name",
        "example",
    } and not low.startswith("example-") and not re.match(r"logging-[0-9][0-9-]*$", low):
        return False
    return (
        low == "your-bucket-name"
        or low == "example"
        or low.startswith("example-")
        or bool(re.match(r"logging-[0-9][0-9-]*$", low))
        or (low.startswith("my-") and not _prompt_mentions_literal(prompt, name))
        or (low.startswith("test-") and not _prompt_mentions_literal(prompt, name))
        or low in {"autograder-results", "autograder-source", "source-bucket", "artifacts-bucket"}
        or low.endswith("-source-bucket")
        or low.endswith("-artifacts-bucket")
    )


def _rewrite_placeholder_s3_bucket_names(hcl: str, prompt: str = "") -> tuple[str, list[str]]:
    parts: list[str] = []
    pos = 0
    repairs: list[str] = []

    while True:
        match = _S3_BUCKET_HEADER_RE.search(hcl, pos)
        if not match:
            parts.append(hcl[pos:])
            break

        open_idx = match.end() - 1
        depth = 0
        end_idx = None
        for idx in range(open_idx, len(hcl)):
            if hcl[idx] == "{":
                depth += 1
            elif hcl[idx] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = idx + 1
                    break
        if end_idx is None:
            parts.append(hcl[pos:])
            break

        parts.append(hcl[pos:match.start()])
        block = hcl[match.start():end_idx]

        def repl(line_match: re.Match) -> str:
            indent, name = line_match.group(1), line_match.group(2)
            if not _is_placeholder_global_bucket_name(name, prompt):
                return line_match.group(0)
            prefix = name.rstrip("-") + "-"
            repairs.append(f"converted placeholder S3 bucket name `{name}` to bucket_prefix")
            return f'{indent}bucket_prefix = "{prefix}"'

        block = re.sub(r'(?m)^(\s*)bucket\s*=\s*"([^"]+)"\s*$', repl, block)
        parts.append(block)
        pos = end_idx

    return "".join(parts), sorted(set(repairs))


def _prompt_requests_reserved_concurrency(prompt: str) -> bool:
    text = (prompt or "").lower()
    return (
        "reserved_concurrent_executions" in text
        or "reserved concurrent" in text
        or "reserved concurrency" in text
        or "function concurrency" in text
    )


def _remove_duplicate_s3_bucket_data_sources(hcl: str) -> tuple[str, list[str]]:
    resource_names = set(_S3_BUCKET_DECL_RE.findall(hcl or ""))
    if not resource_names:
        return hcl, []

    parts: list[str] = []
    removed: list[str] = []
    pos = 0
    while True:
        match = _DATA_S3_BUCKET_HEADER_RE.search(hcl or "", pos)
        if not match:
            parts.append(hcl[pos:])
            break

        name = match.group(1)
        open_idx = match.end() - 1
        depth = 0
        end_idx = None
        for idx in range(open_idx, len(hcl)):
            if hcl[idx] == "{":
                depth += 1
            elif hcl[idx] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = idx + 1
                    break
        if end_idx is None:
            parts.append(hcl[pos:])
            break

        parts.append(hcl[pos:match.start()])
        if name in resource_names:
            removed.append(name)
        else:
            parts.append(hcl[match.start():end_idx])
        pos = end_idx

    rewritten = "".join(parts)
    for name in removed:
        rewritten = re.sub(
            rf'\bdata\.aws_s3_bucket\.{re.escape(name)}\.',
            f'aws_s3_bucket.{name}.',
            rewritten,
        )
    if not removed:
        return hcl, []
    return rewritten, [f"removed duplicate data.aws_s3_bucket lookup for {name}" for name in sorted(set(removed))]


def _prompt_requests_public_s3(prompt: str) -> bool:
    text = (prompt or "").lower()
    if re.search(r"\b(block|disable|deny|prevent|restrict|no)\b.{0,30}\bpublic access\b", text):
        return False
    return (
        "public website" in text
        or "static website" in text
        or "website hosting" in text
        or "public-read" in text
        or "public read" in text
        or "allow public access" in text
        or "enable public access" in text
    )


def _append_s3_safety_resources(hcl: str, prompt: str) -> tuple[str, list[str]]:
    bucket_names = sorted(set(_S3_BUCKET_DECL_RE.findall(hcl or "")))
    if not bucket_names:
        return hcl, []

    additions: list[str] = []
    repairs: list[str] = []
    allow_public = _prompt_requests_public_s3(prompt)

    for bucket in bucket_names:
        public_name = f"{bucket}_public_access"
        if not allow_public and not _bucket_companion_exists(
            hcl, "aws_s3_bucket_public_access_block", bucket
        ):
            additions.append(
                f'''
resource "aws_s3_bucket_public_access_block" "{public_name}" {{
  bucket = aws_s3_bucket.{bucket}.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}'''.strip()
            )
            repairs.append("added S3 public access block")

        sse_name = f"{bucket}_encryption"
        if not _bucket_companion_exists(
            hcl, "aws_s3_bucket_server_side_encryption_configuration", bucket
        ):
            additions.append(
                f'''
resource "aws_s3_bucket_server_side_encryption_configuration" "{sse_name}" {{
  bucket = aws_s3_bucket.{bucket}.id

  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "AES256"
    }}
  }}
}}'''.strip()
            )
            repairs.append("added S3 default SSE-S3 encryption")

        versioning_name = f"{bucket}_versioning"
        if not _bucket_companion_exists(hcl, "aws_s3_bucket_versioning", bucket):
            additions.append(
                f'''
resource "aws_s3_bucket_versioning" "{versioning_name}" {{
  bucket = aws_s3_bucket.{bucket}.id

  versioning_configuration {{
    status = "Enabled"
  }}
}}'''.strip()
            )
            repairs.append("added S3 bucket versioning")

    if not additions:
        return hcl, []
    return hcl.rstrip() + "\n\n" + "\n\n".join(additions) + "\n", sorted(set(repairs))


def _rewrite_canonical_data_sources(hcl: str) -> tuple[str, bool, list[str]]:
    """Rewrite Terraform data-source-only types emitted as `resource` blocks."""
    original = hcl
    rewritten: list[str] = []
    for data_type in sorted(_CANONICAL_DATA_SOURCE_TYPES):
        before = hcl
        hcl = re.sub(
            rf'resource\s+"{re.escape(data_type)}"\s+"([^"]+)"',
            rf'data "{data_type}" "\1"',
            hcl,
        )
        if hcl != before:
            rewritten.append(data_type)
        hcl = re.sub(
            rf'(?<!data\.)\b{re.escape(data_type)}\.([A-Za-z0-9_-]+)\.',
            rf'data.{data_type}.\1.',
            hcl,
        )
    return hcl, hcl != original, rewritten


def _line_depths(block: str) -> list[int]:
    depths = []
    depth = 0
    for line in block.splitlines():
        depths.append(depth)
        depth += line.count("{") - line.count("}")
    return depths


def _strip_top_level_block(lines: list[str], depths: list[int], idx: int) -> int:
    target_depth = depths[idx]
    j = idx + 1
    while j < len(lines):
        if depths[j] <= target_depth:
            break
        j += 1
    return j


_DEFAULT_CODEBUILD_BUILDSPEC = (
    '"version: 0.2\\nphases:\\n  build:\\n    commands:\\n      - echo build"'
)


def _move_top_level_codebuild_buildspec(block: str) -> tuple[str, list[str]]:
    lines = block.splitlines()
    depths = _line_depths(block)
    top_buildspec_idx = None
    source_idx = None
    source_has_buildspec = False
    source_type_no_source = False
    has_artifacts = False
    has_environment = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if depths[idx] == 1 and re.match(r'buildspec\s*=', stripped):
            top_buildspec_idx = idx
            continue
        if depths[idx] == 1 and re.match(r'source\s*\{', stripped):
            source_idx = idx
            continue
        if depths[idx] == 1 and re.match(r'artifacts\s*\{', stripped):
            has_artifacts = True
            continue
        if depths[idx] == 1 and re.match(r'environment\s*\{', stripped):
            has_environment = True
            continue
        if depths[idx] == 2 and re.match(r'buildspec\s*=', stripped):
            source_has_buildspec = True
            continue
        if depths[idx] == 2 and re.match(r'type\s*=\s*"NO_SOURCE"', stripped):
            source_type_no_source = True

    repairs: list[str] = []
    buildspec_value = _DEFAULT_CODEBUILD_BUILDSPEC

    if top_buildspec_idx is not None:
        buildspec_line = lines[top_buildspec_idx]
        buildspec_value = buildspec_line.split("=", 1)[1].strip()
        del lines[top_buildspec_idx]
        if source_idx is not None and top_buildspec_idx < source_idx:
            source_idx -= 1

    if source_idx is not None and not source_has_buildspec:
        source_indent = re.match(r'^(\s*)', lines[source_idx]).group(1)
        if source_type_no_source or top_buildspec_idx is not None:
            lines.insert(source_idx + 1, f"{source_indent}  buildspec = {buildspec_value}")
            repairs.append("moved/added CodeBuild buildspec inside source")

    if source_idx is None:
        insert_idx = len(lines) - 1
        block_indent = re.match(r'^(\s*)', lines[-1]).group(1)
        lines[insert_idx:insert_idx] = [
            f'{block_indent}  source {{',
            f'{block_indent}    type      = "NO_SOURCE"',
            f'{block_indent}    buildspec = {buildspec_value}',
            f'{block_indent}  }}',
        ]
        repairs.append("added CodeBuild source block")

    if not has_artifacts:
        insert_idx = len(lines) - 1
        block_indent = re.match(r'^(\s*)', lines[-1]).group(1)
        lines[insert_idx:insert_idx] = [
            f'{block_indent}  artifacts {{',
            f'{block_indent}    type = "NO_ARTIFACTS"',
            f'{block_indent}  }}',
        ]
        repairs.append("added CodeBuild artifacts block")

    if not has_environment:
        insert_idx = len(lines) - 1
        block_indent = re.match(r'^(\s*)', lines[-1]).group(1)
        lines[insert_idx:insert_idx] = [
            f'{block_indent}  environment {{',
            f'{block_indent}    compute_type = "BUILD_GENERAL1_SMALL"',
            f'{block_indent}    image        = "aws/codebuild/standard:7.0"',
            f'{block_indent}    type         = "LINUX_CONTAINER"',
            f'{block_indent}  }}',
        ]
        repairs.append("added CodeBuild environment block")

    return "\n".join(lines), repairs


def _repair_codebuild_project_block(_name: str, block: str) -> tuple[str, list[str]]:
    lines = block.splitlines()
    depths = _line_depths(block)
    has_name = any(depths[idx] == 1 and re.match(r'\s*name\s*=', line) for idx, line in enumerate(lines))
    repairs: list[str] = []
    rewritten: list[str] = []
    cloudwatch_logs_depth: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if cloudwatch_logs_depth is not None and depths[idx] <= cloudwatch_logs_depth:
            cloudwatch_logs_depth = None
        if depths[idx] == 1:
            match = re.match(r'^(\s*)name_prefix\s*=\s*"([^"]+)"\s*$', line)
            if match:
                if not has_name:
                    value = match.group(2).rstrip("-") or match.group(2)
                    rewritten.append(f'{match.group(1)}name = "{value}"')
                    repairs.append("replaced unsupported CodeBuild name_prefix with name")
                else:
                    repairs.append("removed unsupported CodeBuild name_prefix")
                continue
        if re.match(r'cloudwatch_logs\s*\{', stripped):
            cloudwatch_logs_depth = depths[idx]
        if (
            cloudwatch_logs_depth is not None
            and depths[idx] > cloudwatch_logs_depth
            and re.match(r'encryption_disabled\s*=', stripped)
        ):
            repairs.append("removed unsupported CodeBuild cloudwatch_logs encryption_disabled")
            continue
        rewritten.append(line)
    return "\n".join(rewritten), repairs


def _rewrite_codebuild_blocks(hcl: str) -> tuple[str, list[str]]:
    parts: list[str] = []
    pos = 0
    repairs: list[str] = []

    while True:
        match = _CODEBUILD_HEADER_RE.search(hcl, pos)
        if not match:
            parts.append(hcl[pos:])
            break

        open_idx = match.end() - 1
        depth = 0
        end_idx = None
        for idx in range(open_idx, len(hcl)):
            if hcl[idx] == "{":
                depth += 1
            elif hcl[idx] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = idx + 1
                    break
        if end_idx is None:
            parts.append(hcl[pos:])
            break

        parts.append(hcl[pos:match.start()])
        block = hcl[match.start():end_idx]
        rewritten, block_repairs = _move_top_level_codebuild_buildspec(block)
        parts.append(rewritten)
        repairs.extend(block_repairs)
        pos = end_idx

    return "".join(parts), repairs


def _repair_firehose_splunk_block(_name: str, block: str) -> tuple[str, list[str]]:
    if 'destination = "splunk"' not in block and "destination = 'splunk'" not in block:
        return block, []

    lines = block.splitlines()
    depths = _line_depths(block)
    repairs: list[str] = []
    rewritten: list[str] = []
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].strip()
        if depths[idx] == 1 and re.match(r'role_arn\s*=', stripped):
            repairs.append("removed top-level Firehose role_arn")
            idx += 1
            continue
        if depths[idx] == 1 and re.match(r'cloudwatch_logging_options\s*\{', stripped):
            repairs.append("removed invalid top-level Firehose cloudwatch_logging_options")
            idx = _strip_top_level_block(lines, depths, idx)
            continue
        rewritten.append(lines[idx])
        idx += 1

    return "\n".join(rewritten), repairs


def _rewrite_firehose_splunk_blocks(hcl: str) -> tuple[str, list[str]]:
    return _rewrite_resource_blocks(
        hcl,
        "aws_kinesis_firehose_delivery_stream",
        _repair_firehose_splunk_block,
    )


def _repair_ssm_parameter_block(_name: str, block: str) -> tuple[str, list[str]]:
    rewritten = re.sub(r'(?m)^(\s*)kms_key_id\s*=', r'\1key_id =', block)
    if rewritten == block:
        return block, []
    return rewritten, ["renamed aws_ssm_parameter kms_key_id to key_id"]


def _rewrite_ssm_parameter_blocks(hcl: str) -> tuple[str, list[str]]:
    return _rewrite_resource_blocks(hcl, "aws_ssm_parameter", _repair_ssm_parameter_block)


def _repair_s3_notification_sns_policy(hcl: str) -> tuple[str, list[str]]:
    if "aws_s3_bucket_notification" not in hcl or "aws_sns_topic_policy" not in hcl:
        return hcl, []
    bucket_names = _S3_BUCKET_DECL_RE.findall(hcl or "")
    bucket_ref = f"aws_s3_bucket.{bucket_names[0]}.arn" if len(bucket_names) == 1 else None
    rewritten = re.sub(
        r'"\$\{aws_s3_bucket\.([A-Za-z0-9_-]+)\.arn\}/\*"',
        r'aws_s3_bucket.\1.arn',
        hcl,
    )
    rewritten = re.sub(
        r'"(aws_s3_bucket\.[A-Za-z0-9_-]+\.arn)/\*"',
        r'\1',
        rewritten,
    )
    if bucket_ref:
        rewritten = re.sub(
            r'values\s*=\s*\["arn:aws:s3:::[^"]+"\]',
            f"values   = [{bucket_ref}]",
            rewritten,
        )
    rewritten = rewritten.replace('"SNS:Publish"', '"sns:Publish"')
    if 'resource "aws_sns_topic"' in rewritten:
        rewritten = re.sub(
            r'(?m)^\s*kms_master_key_id\s*=\s*"alias/aws/sns"\s*\n?',
            '',
            rewritten,
        )
    if rewritten == hcl:
        return hcl, []
    return rewritten, ["fixed S3 notification SNS destination policy for deployability"]


def _bucket_refs_for_acl_resources(hcl: str) -> set[str]:
    refs: set[str] = set()
    for _, block in _resource_blocks_of_type(hcl, "aws_s3_bucket_acl"):
        match = re.search(r'(?m)^\s*bucket\s*=\s*aws_s3_bucket\.([A-Za-z0-9_-]+)\.(?:id|bucket)\s*$', block)
        if match:
            refs.add(match.group(1))
    return refs


def _repair_s3_ownership_controls_block(acl_bucket_refs: set[str]):
    def rewrite(_name: str, block: str) -> tuple[str, list[str]]:
        bucket_match = re.search(
            r'(?m)^\s*bucket\s*=\s*aws_s3_bucket\.([A-Za-z0-9_-]+)\.(?:id|bucket)\s*$',
            block,
        )
        if bucket_match and bucket_match.group(1) not in acl_bucket_refs:
            return block, []
        rewritten = re.sub(
            r'object_ownership\s*=\s*"BucketOwnerEnforced"',
            'object_ownership = "BucketOwnerPreferred"',
            block,
        )
        if rewritten == block:
            return block, []
        return rewritten, ["changed S3 ownership controls to allow explicit ACL resource"]

    return rewrite


def _rewrite_s3_acl_ownership_conflicts(hcl: str) -> tuple[str, list[str]]:
    acl_bucket_refs = _bucket_refs_for_acl_resources(hcl)
    if not acl_bucket_refs:
        return hcl, []
    return _rewrite_resource_blocks(
        hcl,
        "aws_s3_bucket_ownership_controls",
        _repair_s3_ownership_controls_block(acl_bucket_refs),
    )


def _prompt_is_elasticache_user_only(prompt: str) -> bool:
    text = (prompt or "").lower()
    if "elasticache" not in text or "user" not in text:
        return False
    cluster_terms = ("replication group", "cluster", "cache node", "subnet group", "vpc")
    return not any(term in text for term in cluster_terms)


def _repair_elasticache_user_block(prompt: str):
    def rewrite(_name: str, block: str) -> tuple[str, list[str]]:
        text = (prompt or "").lower()
        if "elasticache" not in text or "password" not in text:
            return block, []
        repairs: list[str] = []
        rewritten = re.sub(
            r'passwords\s*=\s*\[[^\]]*\]',
            'passwords = ["password1password1", "password2password2"]',
            block,
        )
        if rewritten != block:
            repairs.append("normalized ElastiCache passwords to deployable examples")
        rewritten = re.sub(
            r'(?m)^(\s*)user_id\s*=\s*"auth-user"\s*$',
            r'\1user_id = "auth-user-${random_id.elasticache_user_suffix.hex}"',
            rewritten,
        )
        rewritten = re.sub(
            r'(?m)^(\s*)user_name\s*=\s*"auth-user"\s*$',
            r'\1user_name = "auth-user-${random_id.elasticache_user_suffix.hex}"',
            rewritten,
        )
        if rewritten != block and "random_id.elasticache_user_suffix" in rewritten:
            repairs.append("made ElastiCache user id/name globally unique")
        return rewritten, sorted(set(repairs))

    return rewrite


def _rewrite_elasticache_user_blocks(hcl: str, prompt: str) -> tuple[str, list[str]]:
    return _rewrite_resource_blocks(hcl, "aws_elasticache_user", _repair_elasticache_user_block(prompt))


def _remove_resource_blocks_of_type(hcl: str, resource_type: str) -> tuple[str, int]:
    header_re = re.compile(_RESOURCE_HEADER_TEMPLATE.format(re.escape(resource_type)))
    parts: list[str] = []
    pos = 0
    removed = 0
    while True:
        match = header_re.search(hcl or "", pos)
        if not match:
            parts.append(hcl[pos:])
            break
        open_idx = match.end() - 1
        depth = 0
        end_idx = None
        for idx in range(open_idx, len(hcl)):
            if hcl[idx] == "{":
                depth += 1
            elif hcl[idx] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = idx + 1
                    break
        if end_idx is None:
            parts.append(hcl[pos:])
            break
        parts.append(hcl[pos:match.start()])
        removed += 1
        pos = end_idx
    return "".join(parts), removed


def _remove_slow_elasticache_cluster_for_user_only(hcl: str, prompt: str) -> tuple[str, list[str]]:
    if not _prompt_is_elasticache_user_only(prompt):
        return hcl, []
    hcl, removed = _remove_resource_blocks_of_type(hcl, "aws_elasticache_replication_group")
    if not removed:
        return hcl, []
    return hcl, ["removed slow ElastiCache replication group from user-only prompt"]


def _ensure_elasticache_user_random_suffix(hcl: str) -> tuple[str, list[str]]:
    if "random_id.elasticache_user_suffix" not in hcl:
        return hcl, []
    if re.search(r'resource\s+"random_id"\s+"elasticache_user_suffix"\s*\{', hcl):
        return hcl, []
    addition = '''
resource "random_id" "elasticache_user_suffix" {
  byte_length = 4
}'''.strip()
    return hcl.rstrip() + "\n\n" + addition + "\n", ["added random_id suffix for ElastiCache user"]


def _ensure_hardening_kms_key(hcl: str) -> tuple[str, list[str]]:
    needs_kms = (
        'resource "aws_dynamodb_table"' in hcl
        or 'resource "aws_efs_file_system"' in hcl
    )
    if not needs_kms or _has_resource(hcl, "aws_kms_key", "generated_hardening"):
        return hcl, []
    addition = '''
resource "aws_kms_key" "generated_hardening" {
  description             = "KMS key for generated benchmark resource encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}'''.strip()
    return hcl.rstrip() + "\n\n" + addition + "\n", ["added generated KMS key for encryption hardening"]


def _repair_dynamodb_table_block(_name: str, block: str) -> tuple[str, list[str]]:
    repairs: list[str] = []
    if "server_side_encryption" in block:
        rewritten = re.sub(
            r'(?m)^(\s*)kms_key_arn\s*=\s*[^\n]+$',
            r'\1kms_key_arn = aws_kms_key.generated_hardening.arn',
            block,
        )
        if "kms_key_arn" not in rewritten:
            rewritten = re.sub(
                r'(server_side_encryption\s*\{[^}]*enabled\s*=\s*true[^\n]*\n)',
                r'\1    kms_key_arn = aws_kms_key.generated_hardening.arn\n',
                rewritten,
                count=1,
                flags=re.DOTALL,
            )
        if rewritten != block:
            repairs.append("hardened DynamoDB table with CMK encryption")
        return rewritten, repairs

    insert = '''
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.generated_hardening.arn
  }
'''.rstrip()
    rewritten = block.rsplit("\n}", 1)[0].rstrip() + "\n" + insert + "\n}"
    return rewritten, ["hardened DynamoDB table with CMK encryption"]


def _rewrite_dynamodb_tables(hcl: str) -> tuple[str, list[str]]:
    return _rewrite_resource_blocks(hcl, "aws_dynamodb_table", _repair_dynamodb_table_block)


def _repair_efs_file_system_block(_name: str, block: str) -> tuple[str, list[str]]:
    rewritten = block
    repairs: list[str] = []
    if re.search(r'(?m)^\s*encrypted\s*=', rewritten):
        rewritten = re.sub(r'(?m)^(\s*)encrypted\s*=\s*[^\n]+$', r'\1encrypted = true', rewritten)
    else:
        rewritten = rewritten.rsplit("\n}", 1)[0].rstrip() + "\n  encrypted = true\n}"
    if re.search(r'(?m)^\s*kms_key_id\s*=', rewritten):
        rewritten = re.sub(
            r'(?m)^(\s*)kms_key_id\s*=\s*[^\n]+$',
            r'\1kms_key_id = aws_kms_key.generated_hardening.arn',
            rewritten,
        )
    else:
        rewritten = rewritten.rsplit("\n}", 1)[0].rstrip() + "\n  kms_key_id = aws_kms_key.generated_hardening.arn\n}"
    if rewritten != block:
        repairs.append("hardened EFS file system with CMK encryption")
    return rewritten, repairs


def _rewrite_efs_file_systems(hcl: str) -> tuple[str, list[str]]:
    return _rewrite_resource_blocks(hcl, "aws_efs_file_system", _repair_efs_file_system_block)


def _extract_efs_lifecycle_policies(hcl: str) -> tuple[str, dict[str, str], list[str]]:
    policies: dict[str, str] = {}
    names: list[str] = []

    def rewrite(_name: str, block: str) -> tuple[str, list[str]]:
        fs_match = re.search(r'file_system_id\s*=\s*aws_efs_file_system\.([A-Za-z0-9_-]+)\.id', block)
        transition_match = re.search(r'transition_to_ia\s*=\s*"([^"]+)"', block)
        if fs_match and transition_match:
            policies[fs_match.group(1)] = transition_match.group(1)
            names.append(_name)
            return "", [f"moved EFS lifecycle policy {_name} into aws_efs_file_system"]
        return block, []

    rewritten, repairs = _rewrite_resource_blocks(hcl, "aws_efs_file_system_lifecycle_policy", rewrite)
    return rewritten, policies, repairs


def _add_efs_lifecycle_policies(hcl: str, policies: dict[str, str]) -> tuple[str, list[str]]:
    if not policies:
        return hcl, []

    def rewrite(name: str, block: str) -> tuple[str, list[str]]:
        transition = policies.get(name)
        if not transition or "lifecycle_policy" in block:
            return block, []
        insert = f'''
  lifecycle_policy {{
    transition_to_ia = "{transition}"
  }}
'''.rstrip()
        rewritten = block.rsplit("\n}", 1)[0].rstrip() + "\n" + insert + "\n}"
        return rewritten, [f"added lifecycle_policy block to aws_efs_file_system.{name}"]

    return _rewrite_resource_blocks(hcl, "aws_efs_file_system", rewrite)


def _rewrite_efs_lifecycle_policy_resources(hcl: str) -> tuple[str, list[str]]:
    hcl, policies, extract_repairs = _extract_efs_lifecycle_policies(hcl)
    hcl, add_repairs = _add_efs_lifecycle_policies(hcl, policies)
    return hcl, sorted(set(extract_repairs + add_repairs))


def _repair_lambda_function_block(_name: str, block: str) -> tuple[str, list[str]]:
    if "tracing_config" in block:
        return block, []
    insert = '''
  tracing_config {
    mode = "Active"
  }
'''.rstrip()
    rewritten = block.rsplit("\n}", 1)[0].rstrip() + "\n" + insert + "\n}"
    return rewritten, ["enabled Lambda X-Ray tracing"]


def _rewrite_lambda_functions(hcl: str) -> tuple[str, list[str]]:
    return _rewrite_resource_blocks(hcl, "aws_lambda_function", _repair_lambda_function_block)


def _repair_api_gateway_stage_block(_name: str, block: str) -> tuple[str, list[str]]:
    if re.search(r'(?m)^\s*xray_tracing_enabled\s*=', block):
        rewritten = re.sub(
            r'(?m)^(\s*)xray_tracing_enabled\s*=\s*[^\n]+$',
            r'\1xray_tracing_enabled = true',
            block,
        )
        if rewritten != block:
            return rewritten, ["enabled API Gateway X-Ray tracing"]
        return block, []
    rewritten = block.rsplit("\n}", 1)[0].rstrip() + "\n  xray_tracing_enabled = true\n}"
    return rewritten, ["enabled API Gateway X-Ray tracing"]


def _rewrite_api_gateway_stages(hcl: str) -> tuple[str, list[str]]:
    return _rewrite_resource_blocks(hcl, "aws_api_gateway_stage", _repair_api_gateway_stage_block)


def _postprocess_known_hcl_pitfalls(hcl: str, prompt: str = "") -> tuple[str, list[str]]:
    """Apply conservative local fixes for provider/API mistakes seen repeatedly.

    These are intentionally narrow text repairs. They target values that are
    never useful in this benchmark pipeline and would otherwise burn A4/A5
    retries before the LLM receives a fix instruction.
    """
    warnings: list[str] = []
    original = hcl

    hcl, changed_data_sources, rewritten_types = _rewrite_canonical_data_sources(hcl)
    if changed_data_sources:
        warnings.append(
            "converted data-source-only resource blocks/references to data: "
            + ", ".join(rewritten_types)
        )
        original = hcl

    hcl = re.sub(r'(?m)^(\s*)build_spec\s*=', r'\1buildspec =', hcl)
    if hcl != original:
        warnings.append("renamed build_spec to buildspec")
        original = hcl

    hcl, codebuild_repairs = _rewrite_codebuild_blocks(hcl)
    if codebuild_repairs:
        warnings.extend(sorted(set(codebuild_repairs)))
        original = hcl

    hcl, duplicate_s3_data_repairs = _remove_duplicate_s3_bucket_data_sources(hcl)
    if duplicate_s3_data_repairs:
        warnings.extend(duplicate_s3_data_repairs)
        original = hcl

    hcl, codebuild_name_repairs = _rewrite_resource_blocks(
        hcl,
        "aws_codebuild_project",
        _repair_codebuild_project_block,
    )
    if codebuild_name_repairs:
        warnings.extend(codebuild_name_repairs)
        original = hcl

    hcl, firehose_repairs = _rewrite_firehose_splunk_blocks(hcl)
    if firehose_repairs:
        warnings.extend(firehose_repairs)
        original = hcl

    hcl, ssm_repairs = _rewrite_ssm_parameter_blocks(hcl)
    if ssm_repairs:
        warnings.extend(ssm_repairs)
        original = hcl

    hcl, s3_notification_repairs = _repair_s3_notification_sns_policy(hcl)
    if s3_notification_repairs:
        warnings.extend(s3_notification_repairs)
        original = hcl

    hcl, s3_acl_repairs = _rewrite_s3_acl_ownership_conflicts(hcl)
    if s3_acl_repairs:
        warnings.extend(s3_acl_repairs)
        original = hcl

    hcl, elasticache_repairs = _rewrite_elasticache_user_blocks(hcl, prompt)
    if elasticache_repairs:
        warnings.extend(elasticache_repairs)
        original = hcl

    hcl, elasticache_suffix_repairs = _ensure_elasticache_user_random_suffix(hcl)
    if elasticache_suffix_repairs:
        warnings.extend(elasticache_suffix_repairs)
        original = hcl

    hcl, elasticache_cluster_repairs = _remove_slow_elasticache_cluster_for_user_only(hcl, prompt)
    if elasticache_cluster_repairs:
        warnings.extend(elasticache_cluster_repairs)
        original = hcl

    hcl, kms_repairs = _ensure_hardening_kms_key(hcl)
    if kms_repairs:
        warnings.extend(kms_repairs)
        original = hcl

    hcl, dynamodb_repairs = _rewrite_dynamodb_tables(hcl)
    if dynamodb_repairs:
        warnings.extend(dynamodb_repairs)
        original = hcl

    hcl, efs_repairs = _rewrite_efs_file_systems(hcl)
    if efs_repairs:
        warnings.extend(efs_repairs)
        original = hcl

    hcl, efs_lifecycle_repairs = _rewrite_efs_lifecycle_policy_resources(hcl)
    if efs_lifecycle_repairs:
        warnings.extend(efs_lifecycle_repairs)
        original = hcl

    hcl, lambda_repairs = _rewrite_lambda_functions(hcl)
    if lambda_repairs:
        warnings.extend(lambda_repairs)
        original = hcl

    hcl, api_stage_repairs = _rewrite_api_gateway_stages(hcl)
    if api_stage_repairs:
        warnings.extend(api_stage_repairs)
        original = hcl

    hcl, s3_name_repairs = _rewrite_placeholder_s3_bucket_names(hcl, prompt)
    if s3_name_repairs:
        warnings.extend(s3_name_repairs)
        original = hcl

    hcl, s3_safety_repairs = _append_s3_safety_resources(hcl, prompt)
    if s3_safety_repairs:
        warnings.extend(s3_safety_repairs)
        original = hcl

    hcl = re.sub(r'(?m)^\s*publicly_accessible\s*=\s*[^\n]+\n?', '', hcl)
    if hcl != original:
        warnings.append("removed unsupported Lightsail publicly_accessible")
        original = hcl

    if not _prompt_requests_reserved_concurrency(prompt):
        hcl = re.sub(r'(?m)^\s*reserved_concurrent_executions\s*=\s*[^\n]+\n?', '', hcl)
        if hcl != original:
            warnings.append("removed Lambda reserved_concurrent_executions")
            original = hcl

    if "cpu_options" in hcl:
        hcl = re.sub(
            r'instance_type\s*=\s*"t2\.micro"',
            'instance_type = "t3.micro"',
            hcl,
        )
        if hcl != original:
            warnings.append("changed t2.micro with cpu_options to t3.micro")
            original = hcl

    hcl = _KEY_PAIR_BLOCK_RE.sub(
        lambda match: _replace_invalid_key_pair_public_key(match.group(1)),
        hcl,
    )
    if hcl != original:
        warnings.append('replaced invalid aws_key_pair public_key with file("./key.pub")')

    return hcl, warnings


def engi_node(state: dict) -> dict:
    """LangGraph node — serialize A1 plan sang HCL, áp dụng CKV requirements từ A2."""
    archi_plan = state.get("infrastructure_plan") or {}
    if not archi_plan.get("resources"):
        return make_fail(
            "MISSING_RESOURCE", "architecture",
            "Engi agent nhận infrastructure_plan rỗng — archi agent phải chạy trước.",
        )

    ckv_ids = state.get("security_ckv_ids") or {}
    if ckv_ids:
        ckv_lines = "\n".join(f"  {label}: {', '.join(ids)}" for label, ids in ckv_ids.items())
    else:
        ckv_lines = "  (none)"

    user_content = _USER_TEMPLATE.format(
        PLAN=json.dumps(archi_plan, indent=2),
        CKV_REQUIREMENTS=ckv_lines,
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]

    fix_feedback = state.get("fix_feedback") or {}
    fix_instruction = fix_feedback.get("fix_instruction", "")
    eng_retry_count = state.get("eng_retry_count", 0)
    if fix_instruction and (eng_retry_count > 0 or state.get("deploy_eng_retry_count", 0) > 0):
        eng_error_history = state.get("eng_error_history") or []
        fix_msg = f"REQUIRED FIX (apply exactly):\n{fix_instruction}"
        past = [
            e.get("fix_instruction", "")[:200]
            for e in eng_error_history[-2:]
            if e.get("fix_instruction") and e.get("fix_instruction") != fix_instruction
        ]
        if past:
            fix_msg += "\n\nPREVIOUS ERRORS (do NOT repeat these):\n" + "\n".join(f"- {p}" for p in past)
        messages.append({"role": "user", "content": fix_msg})

    raw = ""
    try:
        raw = call_llm(messages)
    except TimeoutError as e:
        logger.error("Engi agent timeout: %s", e)
        return make_fail("INFRA", None, f"Engi agent LLM timeout: {e}")
    except Exception as e:
        logger.error("Engi agent error: %s", e)
        return make_fail("INFRA", None, f"Engi agent error: {e}")

    body = _clean_body(raw)
    missing = _missing_plan_declarations(archi_plan, body)

    # Guard: output must declare every resource/data source from the architecture plan.
    if 'resource "' not in body or missing:
        if missing:
            logger.warning("Engi agent: missing plan declarations — retry: %s", missing)
            retry_instruction = (
                "Your response omitted Terraform blocks from the plan. "
                f"Missing declarations: {_format_declarations(missing)}. "
                "Output the complete Terraform HCL with every missing resource and data block. "
                "Preserve the exact type and local name from the plan."
            )
        else:
            logger.warning("Engi agent: không có resource block — retry")
            retry_instruction = (
                "Your response did not contain any `resource \"` blocks. "
                "Output the complete Terraform HCL with ALL resource blocks "
                "from the plan. Do not omit any resource."
            )
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": retry_instruction},
        ]
        try:
            raw = call_llm(retry_msgs)
        except Exception as e:
            return make_fail("INFRA", None, f"Engi agent retry error: {e}")
        body = _clean_body(raw)
        missing = _missing_plan_declarations(archi_plan, body)
        if 'resource "' not in body or missing:
            missing_text = _format_declarations(missing) if missing else "all resource blocks"
            return make_fail(
                "SYNTAX", "engineering",
                "Engi agent did not emit every block from the architecture plan "
                f"after retry. Missing: {missing_text}. Raw: {raw[:300]}",
            )

    body, postprocess_warnings = _postprocess_known_hcl_pitfalls(body, state.get("prompt", ""))
    generated_code = f"{_provider_block_for_prompt(state.get('prompt', ''))}\n\n{body}\n"

    gen_pairs = set(_RESOURCE_DECL_RE.findall(body))
    gen_data = set(_DATA_DECL_RE.findall(body))
    logger.info(
        "Engi agent: %d chars, %d resources, %d data sources",
        len(generated_code), len(gen_pairs), len(gen_data),
    )
    result = {"generated_code": generated_code, "engineering_warnings": []}
    if postprocess_warnings:
        logger.info("Engi agent postprocess: %s", postprocess_warnings)
        result["engineering_warnings"].append({
            "kind": "hcl_postprocess",
            "message": "Applied deterministic repairs for known Terraform/AWS pitfalls.",
            "repairs": postprocess_warnings,
        })
    return result
