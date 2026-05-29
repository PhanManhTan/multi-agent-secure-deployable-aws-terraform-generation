"""Engineering Agent (engi) — nhận output của archi_node + secu_node, sinh Terraform HCL.

Input state:
  infrastructure_plan  — JSON plan từ Agent 1 (archi)
  security_ckv_ids     — CKV check IDs từ Agent 2 (secu)

Output state:
  generated_code       — HCL string hoàn chỉnh (provider block đã prepend)
"""
import json
import logging
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
_HCL_BLOCK_START = re.compile(r'(?:resource|data|variable|output|module|moved|import)\s+"')
_KEY_PAIR_BLOCK_RE = re.compile(
    r'(resource\s+"aws_key_pair"\s+"[^"]+"\s*\{.*?\n\s*\})',
    re.DOTALL,
)
_CODEBUILD_HEADER_RE = re.compile(r'resource\s+"aws_codebuild_project"\s+"[^"]+"\s*\{')


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


def _planned_declarations(plan: dict) -> set[tuple[str, str, str]]:
    declarations: set[tuple[str, str, str]] = set()
    for entry in plan.get("resources") or []:
        if entry.get("type") and entry.get("name"):
            declarations.add(("resource", entry["type"], entry["name"]))
    for entry in plan.get("data_sources") or []:
        if entry.get("type") and entry.get("name"):
            declarations.add(("data", entry["type"], entry["name"]))
    return declarations


def _generated_declarations(hcl: str) -> set[tuple[str, str, str]]:
    declarations = {("resource", t, n) for t, n in _RESOURCE_DECL_RE.findall(hcl or "")}
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


def _prompt_requests_reserved_concurrency(prompt: str) -> bool:
    text = (prompt or "").lower()
    return (
        "reserved_concurrent_executions" in text
        or "reserved concurrent" in text
        or "reserved concurrency" in text
        or "function concurrency" in text
    )


def _line_depths(block: str) -> list[int]:
    depths = []
    depth = 0
    for line in block.splitlines():
        depths.append(depth)
        depth += line.count("{") - line.count("}")
    return depths


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


def _postprocess_known_hcl_pitfalls(hcl: str, prompt: str = "") -> tuple[str, list[str]]:
    """Apply conservative local fixes for provider/API mistakes seen repeatedly.

    These are intentionally narrow text repairs. They target values that are
    never useful in this benchmark pipeline and would otherwise burn A4/A5
    retries before the LLM receives a fix instruction.
    """
    warnings: list[str] = []
    original = hcl

    hcl = re.sub(r'(?m)^(\s*)build_spec\s*=', r'\1buildspec =', hcl)
    if hcl != original:
        warnings.append("renamed build_spec to buildspec")
        original = hcl

    hcl, codebuild_repairs = _rewrite_codebuild_blocks(hcl)
    if codebuild_repairs:
        warnings.extend(sorted(set(codebuild_repairs)))
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
    generated_code = f"{_PROVIDER_BLOCK}\n\n{body}\n"

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
