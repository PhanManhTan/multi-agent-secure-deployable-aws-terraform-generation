"""Security Agent (secu) — gán CKV check IDs cho từng resource trong plan.

Input: infrastructure_plan (A1) + prompt (user intent)
Output: security_ckv_ids — {"type.name": ["CKV_AWS_NNN", ...]}
"""
import json
import logging
from pathlib import Path

from core.state import AgentState
from core.llm import call_llm
from core.parsers import parse_llm_json
from prompts.security import SYSTEM_PROMPT, USER_TEMPLATE

logger = logging.getLogger(__name__)

_VALID_CKV_IDS: frozenset[str] = frozenset(
    json.loads((Path(__file__).parent / ".checkov_ids.json").read_text())
)


def secu_node(state: AgentState) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(
            PROMPT=state["prompt"],
            PLAN=json.dumps(state["infrastructure_plan"], indent=2),
        )},
    ]
    try:
        raw = call_llm(messages)
        parsed = parse_llm_json(raw, {})
    except Exception as e:
        logger.warning("Secu agent error: %s — continuing with empty CKV IDs", e)
        return {"security_ckv_ids": {}}

    plan_keys = {
        f"{r['type']}.{r['name']}"
        for r in state["infrastructure_plan"].get("resources", [])
    }
    ckv_ids: dict[str, list[str]] = {}
    for label, checks in parsed.items():
        if not isinstance(checks, list):
            continue
        if label not in plan_keys:
            logger.warning("Secu agent: unknown resource '%s' — dropped", label)
            continue
        valid = [c for c in checks if isinstance(c, str) and c in _VALID_CKV_IDS]
        unknown = [c for c in checks if isinstance(c, str) and c not in _VALID_CKV_IDS]
        if unknown:
            logger.warning("Secu agent: unknown CKV IDs for '%s': %s — dropped", label, unknown)
        if valid:
            ckv_ids[label] = valid

    total = sum(len(v) for v in ckv_ids.values())
    logger.info("Secu agent: %d resources, %d CKV IDs total", len(ckv_ids), total)
    return {"security_ckv_ids": ckv_ids}
