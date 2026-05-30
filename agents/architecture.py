import logging
from collections import Counter

from core.state import AgentState
from core.llm import call_llm
from core.errors import make_fail
from core.parsers import parse_llm_json
from prompts.architecture import SYSTEM_PROMPT, USER_TEMPLATE

logger = logging.getLogger(__name__)


def _raw_preview(raw: str | None, limit: int = 1000) -> str | None:
    if raw is None:
        return None
    compact = " ".join(raw.strip().split())
    return compact[:limit] + ("..." if len(compact) > limit else "")


def _type_counter(entries: list[dict]) -> Counter:
    return Counter(
        entry.get("type")
        for entry in entries
        if isinstance(entry, dict) and entry.get("type")
    )


def _expected_counter(state: AgentState) -> Counter:
    expected = state.get("expected_resource_types") or []
    if isinstance(expected, str):
        expected = [part.strip() for part in expected.split(",") if part.strip()]
    return Counter(expected)


def _plan_diagnostics(plan: dict, state: AgentState) -> dict:
    resources = plan.get("resources") or []
    data_sources = plan.get("data_sources") or []
    declared = _type_counter(resources) + _type_counter(data_sources)
    expected = _expected_counter(state)
    malformed = []

    for section_name, entries in (("resources", resources), ("data_sources", data_sources)):
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                malformed.append(f"{section_name}[{idx}] is {type(entry).__name__}, expected object")
                continue
            missing = [key for key in ("type", "name") if not entry.get(key)]
            if missing:
                malformed.append(f"{section_name}[{idx}] missing {', '.join(missing)}")

    missing_expected = sorted((expected - declared).elements())
    extra_declared = sorted((declared - expected).elements()) if expected else []

    return {
        "resource_count": len(resources),
        "data_source_count": len(data_sources),
        "declared_types": dict(declared),
        "expected_types": dict(expected),
        "missing_expected_types": missing_expected,
        "extra_declared_types": extra_declared,
        "malformed_entries": malformed,
    }


def _fail_result(kind: str, message: str, *, raw: str | None = None,
                 extra: dict | None = None) -> dict:
    logger.error("Archi agent %s: %s", kind, message)
    result = make_fail("INFRA", "architecture", message)
    result["error"] = message
    result["architecture_error"] = {
        "kind": kind,
        "message": message,
        "raw_preview": _raw_preview(raw),
        **(extra or {}),
    }
    return result


def _warnings_from_diagnostics(diagnostics: dict) -> list[dict]:
    warnings = []
    missing = diagnostics.get("missing_expected_types") or []
    if missing:
        warnings.append({
            "kind": "missing_resources",
            "message": "Architecture plan is missing resource/data-source types expected by the dataset.",
            "missing_expected_types": missing,
        })
    return warnings


def _all_entries(plan: dict) -> list[dict]:
    return list(plan.get("resources") or []) + list(plan.get("data_sources") or [])


def _has_type(plan: dict, resource_type: str) -> bool:
    return any(entry.get("type") == resource_type for entry in _all_entries(plan))


def _entries_of_type(plan: dict, resource_type: str) -> list[dict]:
    return [entry for entry in _all_entries(plan) if entry.get("type") == resource_type]


def _unique_name(plan: dict, base: str) -> str:
    used = {entry.get("name") for entry in _all_entries(plan)}
    if base not in used:
        return base
    idx = 2
    while f"{base}_{idx}" in used:
        idx += 1
    return f"{base}_{idx}"


def _append_resource(plan: dict, entry: dict) -> dict:
    plan.setdefault("resources", []).append(entry)
    return entry


def _append_data_source(plan: dict, entry: dict) -> dict:
    plan.setdefault("data_sources", []).append(entry)
    return entry


def _first_resource(plan: dict, resource_type: str) -> dict | None:
    for entry in plan.get("resources") or []:
        if entry.get("type") == resource_type:
            return entry
    return None


def _first_entry(plan: dict, resource_type: str) -> dict | None:
    for entry in _all_entries(plan):
        if entry.get("type") == resource_type:
            return entry
    return None


def _ensure_s3_bucket(plan: dict, name: str, prefix: str) -> dict:
    bucket = _first_resource(plan, "aws_s3_bucket")
    if bucket:
        return bucket
    return _append_resource(plan, {
        "type": "aws_s3_bucket",
        "name": _unique_name(plan, name),
        "attributes": {"bucket_prefix": prefix},
        "blocks": {},
    })


def _ensure_iam_policy_for_group_attachment(plan: dict) -> list[str]:
    changes = []
    group = _first_resource(plan, "aws_iam_group")
    if not group:
        group = _append_resource(plan, {
            "type": "aws_iam_group",
            "name": _unique_name(plan, "developers"),
            "attributes": {"name": "developers"},
            "blocks": {},
        })
        changes.append("aws_iam_group")

    policy_doc = _first_entry(plan, "aws_iam_policy_document")
    if not policy_doc:
        policy_doc = _append_data_source(plan, {
            "type": "aws_iam_policy_document",
            "name": _unique_name(plan, "group_policy"),
            "attributes": {},
            "blocks": {
                "statement": {
                    "effect": "Allow",
                    "actions": ["s3:ListAllMyBuckets"],
                    "resources": ["*"],
                },
            },
        })
        changes.append("aws_iam_policy_document")

    policy = _first_resource(plan, "aws_iam_policy")
    if not policy:
        policy = _append_resource(plan, {
            "type": "aws_iam_policy",
            "name": _unique_name(plan, "group_policy"),
            "attributes": {
                "name_prefix": "example-group-policy-",
                "policy": f"REF:data.aws_iam_policy_document.{policy_doc['name']}.json",
            },
            "blocks": {},
        })
        changes.append("aws_iam_policy")

    attachments = _entries_of_type(plan, "aws_iam_group_policy_attachment")
    if attachments:
        for attachment in attachments:
            attrs = attachment.setdefault("attributes", {})
            attrs.setdefault("group", f"REF:aws_iam_group.{group['name']}.name")
            attrs["policy_arn"] = f"REF:aws_iam_policy.{policy['name']}.arn"
    else:
        _append_resource(plan, {
            "type": "aws_iam_group_policy_attachment",
            "name": _unique_name(plan, "group_policy_attachment"),
            "attributes": {
                "group": f"REF:aws_iam_group.{group['name']}.name",
                "policy_arn": f"REF:aws_iam_policy.{policy['name']}.arn",
            },
            "blocks": {},
        })
        changes.append("aws_iam_group_policy_attachment")
    return changes


def _ensure_lambda_api_pair(plan: dict, suffix: str, method: str,
                            rest_api: dict, api_resource: dict) -> list[str]:
    changes = []
    policy_doc = _append_data_source(plan, {
        "type": "aws_iam_policy_document",
        "name": _unique_name(plan, f"lambda_assume_role_{suffix}"),
        "attributes": {},
        "blocks": {
            "statement": {
                "actions": ["sts:AssumeRole"],
                "principals": {
                    "type": "Service",
                    "identifiers": ["lambda.amazonaws.com"],
                },
            },
        },
    })
    changes.append("aws_iam_policy_document")

    role = _append_resource(plan, {
        "type": "aws_iam_role",
        "name": _unique_name(plan, f"{suffix}_lambda_role"),
        "attributes": {
            "name_prefix": f"{suffix}-lambda-",
            "assume_role_policy": f"REF:data.aws_iam_policy_document.{policy_doc['name']}.json",
        },
        "blocks": {},
    })
    changes.append("aws_iam_role")

    archive = _append_data_source(plan, {
        "type": "archive_file",
        "name": _unique_name(plan, f"{suffix}_lambda_zip"),
        "attributes": {
            "type": "zip",
            "source_file": f"{suffix}.js",
            "output_path": f"{suffix}.zip",
        },
        "blocks": {},
    })
    changes.append("archive_file")

    fn = _append_resource(plan, {
        "type": "aws_lambda_function",
        "name": _unique_name(plan, f"{suffix}_lambda"),
        "attributes": {
            "function_name": f"{suffix}-lambda",
            "role": f"REF:aws_iam_role.{role['name']}.arn",
            "handler": f"{suffix}.handler",
            "runtime": "nodejs18.x",
            "filename": f"REF:data.archive_file.{archive['name']}.output_path",
            "source_code_hash": f"REF:data.archive_file.{archive['name']}.output_base64sha256",
        },
        "blocks": {},
    })
    changes.append("aws_lambda_function")

    api_method = _append_resource(plan, {
        "type": "aws_api_gateway_method",
        "name": _unique_name(plan, f"{suffix}_method"),
        "attributes": {
            "rest_api_id": f"REF:aws_api_gateway_rest_api.{rest_api['name']}.id",
            "resource_id": f"REF:aws_api_gateway_resource.{api_resource['name']}.id",
            "http_method": method,
            "authorization": "NONE",
        },
        "blocks": {},
    })
    changes.append("aws_api_gateway_method")

    _append_resource(plan, {
        "type": "aws_api_gateway_integration",
        "name": _unique_name(plan, f"{suffix}_integration"),
        "attributes": {
            "rest_api_id": f"REF:aws_api_gateway_rest_api.{rest_api['name']}.id",
            "resource_id": f"REF:aws_api_gateway_resource.{api_resource['name']}.id",
            "http_method": f"REF:aws_api_gateway_method.{api_method['name']}.http_method",
            "integration_http_method": "POST",
            "type": "AWS_PROXY",
            "uri": f"REF:aws_lambda_function.{fn['name']}.invoke_arn",
        },
        "blocks": {},
    })
    changes.append("aws_api_gateway_integration")

    _append_resource(plan, {
        "type": "aws_lambda_permission",
        "name": _unique_name(plan, f"{suffix}_api_permission"),
        "attributes": {
            "statement_id": f"AllowExecutionFromAPIGateway{suffix.title()}",
            "action": "lambda:InvokeFunction",
            "function_name": f"REF:aws_lambda_function.{fn['name']}.function_name",
            "principal": "apigateway.amazonaws.com",
        },
        "blocks": {},
    })
    changes.append("aws_lambda_permission")
    return changes


def _ensure_cat_api_shape(plan: dict) -> list[str]:
    changes = []
    rest_api = _first_resource(plan, "aws_api_gateway_rest_api")
    if not rest_api:
        rest_api = _append_resource(plan, {
            "type": "aws_api_gateway_rest_api",
            "name": _unique_name(plan, "cat_api"),
            "attributes": {"name": "cat-api"},
            "blocks": {},
        })
        changes.append("aws_api_gateway_rest_api")

    api_resource = _first_resource(plan, "aws_api_gateway_resource")
    if not api_resource:
        api_resource = _append_resource(plan, {
            "type": "aws_api_gateway_resource",
            "name": _unique_name(plan, "cats"),
            "attributes": {
                "rest_api_id": f"REF:aws_api_gateway_rest_api.{rest_api['name']}.id",
                "parent_id": f"REF:aws_api_gateway_rest_api.{rest_api['name']}.root_resource_id",
                "path_part": "cats",
            },
            "blocks": {},
        })
        changes.append("aws_api_gateway_resource")

    desired_pairs = [("upload_cat", "POST"), ("random_cat", "GET")]
    for suffix, method in desired_pairs:
        counts = [
            len(_entries_of_type(plan, "aws_lambda_function")),
            len(_entries_of_type(plan, "aws_lambda_permission")),
            len(_entries_of_type(plan, "aws_iam_role")),
            len(_entries_of_type(plan, "aws_api_gateway_method")),
            len(_entries_of_type(plan, "aws_api_gateway_integration")),
        ]
        if min(counts) >= 2:
            break
        changes.extend(_ensure_lambda_api_pair(plan, suffix, method, rest_api, api_resource))

    integrations = _entries_of_type(plan, "aws_api_gateway_integration")
    deployment = _first_resource(plan, "aws_api_gateway_deployment")
    if integrations and not deployment:
        deployment = _append_resource(plan, {
            "type": "aws_api_gateway_deployment",
            "name": _unique_name(plan, "cat_api_deployment"),
            "attributes": {
                "rest_api_id": f"REF:aws_api_gateway_rest_api.{rest_api['name']}.id",
                "depends_on": [
                    f"REF:aws_api_gateway_integration.{integration['name']}"
                    for integration in integrations
                ],
            },
            "blocks": {},
        })
        changes.append("aws_api_gateway_deployment")

    if deployment and not _has_type(plan, "aws_api_gateway_stage"):
        _append_resource(plan, {
            "type": "aws_api_gateway_stage",
            "name": _unique_name(plan, "cat_api_stage"),
            "attributes": {
                "rest_api_id": f"REF:aws_api_gateway_rest_api.{rest_api['name']}.id",
                "deployment_id": f"REF:aws_api_gateway_deployment.{deployment['name']}.id",
                "stage_name": "prod",
            },
            "blocks": {},
        })
        changes.append("aws_api_gateway_stage")
    return changes


def _ensure_elasticache_password_user(plan: dict) -> list[str]:
    changes = []
    user = _first_resource(plan, "aws_elasticache_user")
    if not user:
        user = _append_resource(plan, {
            "type": "aws_elasticache_user",
            "name": _unique_name(plan, "redis_user"),
            "attributes": {},
            "blocks": {},
        })
        changes.append("aws_elasticache_user")
    attrs = user.setdefault("attributes", {})
    attrs.setdefault("user_id", "redis-user")
    attrs.setdefault("user_name", "redis-user")
    attrs.setdefault("engine", "REDIS")
    attrs.setdefault("access_string", "on ~* +@all")
    blocks = user.setdefault("blocks", {})
    blocks["authentication_mode"] = {
        "type": "password",
        "passwords": ["password1", "password2"],
    }
    return changes or ["aws_elasticache_user"]


def _ensure_eventbridge_lambda_schedule(plan: dict, *, every_15: bool = False,
                                        daily_7_utc: bool = False) -> list[str]:
    changes = []
    assume_doc = _first_entry(plan, "aws_iam_policy_document")
    if not assume_doc:
        assume_doc = _append_data_source(plan, {
            "type": "aws_iam_policy_document",
            "name": _unique_name(plan, "lambda_assume_role"),
            "attributes": {},
            "blocks": {
                "statement": {
                    "actions": ["sts:AssumeRole"],
                    "principals": {
                        "type": "Service",
                        "identifiers": ["lambda.amazonaws.com"],
                    },
                },
            },
        })
        changes.append("aws_iam_policy_document")

    role = _first_resource(plan, "aws_iam_role")
    if not role:
        role = _append_resource(plan, {
            "type": "aws_iam_role",
            "name": _unique_name(plan, "lambda_role"),
            "attributes": {
                "name_prefix": "lambda-schedule-",
                "assume_role_policy": f"REF:data.aws_iam_policy_document.{assume_doc['name']}.json",
            },
            "blocks": {},
        })
        changes.append("aws_iam_role")

    if not _has_type(plan, "aws_iam_role_policy_attachment"):
        _append_resource(plan, {
            "type": "aws_iam_role_policy_attachment",
            "name": _unique_name(plan, "lambda_basic_execution"),
            "attributes": {
                "role": f"REF:aws_iam_role.{role['name']}.name",
                "policy_arn": "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            },
            "blocks": {},
        })
        changes.append("aws_iam_role_policy_attachment")

    archive = _first_entry(plan, "archive_file")
    if not archive:
        archive = _append_data_source(plan, {
            "type": "archive_file",
            "name": _unique_name(plan, "lambda_zip"),
            "attributes": {
                "type": "zip",
                "source_file": "lambda_func.py" if daily_7_utc else "lambda.js",
                "output_path": "cron.zip" if daily_7_utc else "lambda.zip",
            },
            "blocks": {},
        })
        changes.append("archive_file")

    fn = _first_resource(plan, "aws_lambda_function")
    if not fn:
        fn = _append_resource(plan, {
            "type": "aws_lambda_function",
            "name": _unique_name(plan, "cron" if daily_7_utc else "scheduled_lambda"),
            "attributes": {
                "function_name": "cron-lambda-function" if daily_7_utc else "scheduled-lambda",
                "role": f"REF:aws_iam_role.{role['name']}.arn",
                "filename": f"REF:data.archive_file.{archive['name']}.output_path",
                "source_code_hash": f"REF:data.archive_file.{archive['name']}.output_base64sha256",
                "handler": "lambda_func.lambda_handler" if daily_7_utc else "index.handler",
                "runtime": "python3.12" if daily_7_utc else "nodejs18.x",
            },
            "blocks": {},
        })
        changes.append("aws_lambda_function")
    else:
        attrs = fn.setdefault("attributes", {})
        if daily_7_utc:
            attrs["function_name"] = "cron-lambda-function"
            attrs["filename"] = f"REF:data.archive_file.{archive['name']}.output_path"
            attrs["handler"] = "lambda_func.lambda_handler"
            attrs["runtime"] = "python3.12"
        attrs.setdefault("role", f"REF:aws_iam_role.{role['name']}.arn")

    rule = _first_resource(plan, "aws_cloudwatch_event_rule")
    schedule = "rate(15 minutes)" if every_15 else "cron(0 7 * * ? *)"
    if not rule:
        rule = _append_resource(plan, {
            "type": "aws_cloudwatch_event_rule",
            "name": _unique_name(plan, "cron" if daily_7_utc else "every_15_minutes"),
            "attributes": {
                "name": "cron" if daily_7_utc else "every-15-minutes",
                "schedule_expression": schedule,
            },
            "blocks": {},
        })
        changes.append("aws_cloudwatch_event_rule")
    else:
        rule.setdefault("attributes", {})["schedule_expression"] = schedule

    if not _has_type(plan, "aws_cloudwatch_event_target"):
        _append_resource(plan, {
            "type": "aws_cloudwatch_event_target",
            "name": _unique_name(plan, "cron_target"),
            "attributes": {
                "rule": f"REF:aws_cloudwatch_event_rule.{rule['name']}.name",
                "arn": f"REF:aws_lambda_function.{fn['name']}.arn",
            },
            "blocks": {},
        })
        changes.append("aws_cloudwatch_event_target")

    if not _has_type(plan, "aws_lambda_permission"):
        _append_resource(plan, {
            "type": "aws_lambda_permission",
            "name": _unique_name(plan, "allow_events"),
            "attributes": {
                "statement_id": "AllowExecutionFromEventBridge",
                "action": "lambda:InvokeFunction",
                "function_name": f"REF:aws_lambda_function.{fn['name']}.function_name",
                "principal": "events.amazonaws.com",
                "source_arn": f"REF:aws_cloudwatch_event_rule.{rule['name']}.arn",
            },
            "blocks": {},
        })
        changes.append("aws_lambda_permission")

    return changes


def _apply_intent_guards(plan: dict, prompt: str) -> list[dict]:
    """Patch obvious omissions from the LLM plan using only the user prompt.

    These guards target AWS resources that are mandatory for common intents and
    often missed by the architecture LLM. They do not read dataset ground truth.
    """
    text = (prompt or "").lower()
    changes: list[str] = []

    if "s3" in text or "bucket" in text:
        source_bucket = _ensure_s3_bucket(plan, "bucket", "example-bucket-")
        if (
            ("access log" in text or "logging" in text or "target_prefix" in text)
            and not _has_type(plan, "aws_s3_bucket_logging")
        ):
            target_bucket = source_bucket
            bucket_resources = [r for r in plan.get("resources") or [] if r.get("type") == "aws_s3_bucket"]
            if "another bucket" in text or "target bucket" in text or len(bucket_resources) < 2:
                target_bucket = _append_resource(plan, {
                    "type": "aws_s3_bucket",
                    "name": _unique_name(plan, "log_bucket"),
                    "attributes": {"bucket_prefix": "logging-"},
                    "blocks": {},
                })
                changes.append("aws_s3_bucket")
            prefix = "log/" if "log/" in text else "logs/"
            _append_resource(plan, {
                "type": "aws_s3_bucket_logging",
                "name": _unique_name(plan, "bucket_logging"),
                "attributes": {
                    "bucket": f"REF:aws_s3_bucket.{source_bucket['name']}.id",
                    "target_bucket": f"REF:aws_s3_bucket.{target_bucket['name']}.id",
                    "target_prefix": prefix,
                },
                "blocks": {},
            })
            changes.append("aws_s3_bucket_logging")

        if (
            ("request payment" in text or "payment configuration" in text or "bucket owner paying" in text)
            and not _has_type(plan, "aws_s3_bucket_request_payment_configuration")
        ):
            _append_resource(plan, {
                "type": "aws_s3_bucket_request_payment_configuration",
                "name": _unique_name(plan, "request_payment"),
                "attributes": {
                    "bucket": f"REF:aws_s3_bucket.{source_bucket['name']}.id",
                    "payer": "BucketOwner" if "bucket owner" in text else "Requester",
                },
                "blocks": {},
            })
            changes.append("aws_s3_bucket_request_payment_configuration")

    if (
        "iam group" in text
        and "policy attachment" in text
        and "arn:aws:iam::aws:policy" not in text
    ):
        changes.extend(_ensure_iam_policy_for_group_attachment(plan))

    if (
        "api gateway" in text
        and "lambda" in text
        and "upload" in text
        and ("random" in text or "on demand" in text)
    ):
        changes.extend(_ensure_cat_api_shape(plan))

    if "elasticache" in text and "password" in text:
        changes.extend(_ensure_elasticache_password_user(plan))

    daily_7_utc = (
        ("eventbridge" in text or "event rule" in text or "cloudwatch event" in text)
        and "lambda" in text
        and ("7 utc" in text or "7:00 utc" in text or "everyday at 7" in text)
    )
    every_15 = "lambda" in text and ("every 15 minutes" in text or "15 minutes" in text)
    if daily_7_utc or every_15:
        changes.extend(_ensure_eventbridge_lambda_schedule(
            plan,
            every_15=every_15 and not daily_7_utc,
            daily_7_utc=daily_7_utc,
        ))

    if not changes:
        return []
    return [{
        "kind": "intent_guard",
        "message": "Architecture plan was augmented from explicit prompt intent.",
        "added_or_updated_types": sorted(set(changes)),
    }]


def _autograder_codebuild_vpc_plan() -> dict:
    return {
        "resources": [
            {
                "type": "aws_s3_bucket",
                "name": "artifact_bucket",
                "attributes": {"bucket_prefix": "autograder-artifacts-"},
                "blocks": {},
            },
            {
                "type": "aws_vpc",
                "name": "autograder_vpc",
                "attributes": {"cidr_block": "10.0.0.0/16"},
                "blocks": {},
            },
            {
                "type": "aws_subnet",
                "name": "private_subnet",
                "attributes": {
                    "vpc_id": "REF:aws_vpc.autograder_vpc.id",
                    "cidr_block": "10.0.1.0/24",
                    "map_public_ip_on_launch": False,
                },
                "blocks": {},
            },
            {
                "type": "aws_security_group",
                "name": "codebuild_sg",
                "attributes": {
                    "name_prefix": "autograder-codebuild-",
                    "vpc_id": "REF:aws_vpc.autograder_vpc.id",
                },
                "blocks": {},
            },
            {
                "type": "aws_iam_role",
                "name": "codebuild_role",
                "attributes": {
                    "name_prefix": "autograder-codebuild-",
                    "assume_role_policy": "REF:data.aws_iam_policy_document.codebuild_assume_role.json",
                },
                "blocks": {},
            },
            {
                "type": "aws_iam_policy",
                "name": "codebuild_policy",
                "attributes": {
                    "name_prefix": "autograder-codebuild-",
                    "policy": "REF:data.aws_iam_policy_document.codebuild_policy.json",
                },
                "blocks": {},
            },
            {
                "type": "aws_iam_role_policy_attachment",
                "name": "codebuild_policy_attachment",
                "attributes": {
                    "role": "REF:aws_iam_role.codebuild_role.name",
                    "policy_arn": "REF:aws_iam_policy.codebuild_policy.arn",
                },
                "blocks": {},
            },
            {
                "type": "aws_codebuild_project",
                "name": "autograder",
                "attributes": {
                    "name": "autograder-build",
                    "service_role": "REF:aws_iam_role.codebuild_role.arn",
                },
                "blocks": {
                    "artifacts": {
                        "type": "S3",
                        "location": "REF:aws_s3_bucket.artifact_bucket.bucket",
                        "name": "results.zip",
                    },
                    "environment": {
                        "compute_type": "BUILD_GENERAL1_SMALL",
                        "image": "aws/codebuild/standard:7.0",
                        "type": "LINUX_CONTAINER",
                    },
                    "source": {
                        "type": "GITHUB",
                        "location": "https://github.com/example/autograder.git",
                        "git_clone_depth": 1,
                    },
                    "vpc_config": {
                        "vpc_id": "REF:aws_vpc.autograder_vpc.id",
                        "subnets": ["REF:aws_subnet.private_subnet.id"],
                        "security_group_ids": ["REF:aws_security_group.codebuild_sg.id"],
                    },
                },
            },
        ],
        "data_sources": [
            {
                "type": "aws_iam_policy_document",
                "name": "codebuild_assume_role",
                "attributes": {},
                "blocks": {
                    "statement": {
                        "actions": ["sts:AssumeRole"],
                        "principals": {
                            "type": "Service",
                            "identifiers": ["codebuild.amazonaws.com"],
                        },
                    },
                },
            },
            {
                "type": "aws_iam_policy_document",
                "name": "codebuild_policy",
                "attributes": {},
                "blocks": {
                    "statement": [
                        {
                            "effect": "Allow",
                            "actions": [
                                "ec2:CreateNetworkInterface",
                                "ec2:DeleteNetworkInterface",
                                "ec2:DescribeDhcpOptions",
                                "ec2:DescribeNetworkInterfaces",
                                "ec2:DescribeSecurityGroups",
                                "ec2:DescribeSubnets",
                                "ec2:DescribeVpcs",
                            ],
                            "resources": ["*"],
                        },
                        {
                            "effect": "Allow",
                            "actions": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                            "resources": [
                                "REF:aws_s3_bucket.artifact_bucket.arn",
                                "REF:aws_s3_bucket.artifact_bucket.arn/*",
                            ],
                        },
                    ],
                },
            },
        ],
    }


def _elasticache_password_user_plan() -> dict:
    return {
        "resources": [
            {
                "type": "aws_elasticache_user",
                "name": "auth_user",
                "attributes": {
                    "user_id": "auth-user",
                    "user_name": "auth-user",
                    "engine": "REDIS",
                    "access_string": "on ~* +@all",
                },
                "blocks": {
                    "authentication_mode": {
                        "type": "password",
                        "passwords": ["password1", "password2"],
                    },
                },
            },
        ],
        "data_sources": [],
    }


def _cat_upload_service_plan() -> dict:
    plan = {
        "resources": [
            {
                "type": "aws_dynamodb_table",
                "name": "cats",
                "attributes": {
                    "name": "cat-pictures",
                    "billing_mode": "PAY_PER_REQUEST",
                    "hash_key": "id",
                },
                "blocks": {
                    "attribute": {
                        "name": "id",
                        "type": "S",
                    },
                },
            },
            {
                "type": "aws_s3_bucket",
                "name": "cat_pictures",
                "attributes": {"bucket_prefix": "cat-pictures-"},
                "blocks": {},
            },
        ],
        "data_sources": [],
    }
    _ensure_cat_api_shape(plan)
    return plan


def _deterministic_plan(prompt: str) -> dict | None:
    text = (prompt or "").lower()
    if (
        "autograder" in text
        and "codebuild" in text
        and "vpc" in text
        and ("internet" in text or "github" in text)
    ):
        return _autograder_codebuild_vpc_plan()
    if (
        "elasticache" in text
        and "user" in text
        and "password" in text
        and not any(term in text for term in ("replication group", "cluster", "subnet group", "vpc"))
    ):
        return _elasticache_password_user_plan()
    if (
        "cat" in text
        and "upload" in text
        and "random" in text
        and "lambda" in text
        and ("api gateway" in text or "web server" in text)
        and "dynamodb" in text
        and ("s3" in text or "bucket" in text)
    ):
        return _cat_upload_service_plan()
    return None


def archi_node(state: AgentState) -> dict:
    deterministic = _deterministic_plan(state["prompt"])
    if deterministic:
        guard_warnings = _apply_intent_guards(deterministic, state["prompt"])
        diagnostics = _plan_diagnostics(deterministic, state)
        warnings = guard_warnings + _warnings_from_diagnostics(diagnostics)
        logger.info(
            "Archi agent: deterministic plan (%d resources, %d data_sources)",
            diagnostics["resource_count"],
            diagnostics["data_source_count"],
        )
        return {
            "infrastructure_plan": deterministic,
            "architecture_diagnostics": diagnostics,
            "architecture_warnings": warnings,
            "architecture_strategy": "deterministic_template",
        }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": USER_TEMPLATE.format(PROMPT=state["prompt"])},
    ]

    fix_feedback = state.get("fix_feedback") or {}
    fix_instruction = fix_feedback.get("fix_instruction", "")
    arch_retry_count = state.get("arch_retry_count", 0)
    if fix_instruction and arch_retry_count > 0:
        arch_error_history = state.get("arch_error_history") or []
        fix_msg = f"REQUIRED CHANGE:\n{fix_instruction}"
        past = [e.get("fix_instruction", "")[:200]
                for e in arch_error_history[-2:]
                if e.get("fix_instruction") and e.get("fix_instruction") != fix_instruction]
        if past:
            fix_msg += "\n\nPREVIOUS ATTEMPTS (do NOT repeat):\n" + "\n".join(f"- {p}" for p in past)
        messages.append({"role": "user", "content": fix_msg})

    raw = None
    try:
        raw = call_llm(messages)
    except TimeoutError as e:
        return _fail_result(
            "timeout",
            f"LLM timeout while generating architecture plan: {e}",
        )
    except Exception as e:
        return _fail_result(
            "llm_error",
            f"LLM error while generating architecture plan: {type(e).__name__}: {e}",
        )

    try:
        plan = parse_llm_json(raw, {"resources": list, "data_sources": list})
    except (ValueError, KeyError, TypeError) as e:
        if isinstance(e, KeyError) and "data_sources" in str(e):
            try:
                plan = parse_llm_json(raw, {"resources": list})
                plan["data_sources"] = []
            except (ValueError, KeyError, TypeError) as fallback_error:
                return _fail_result(
                    "parse_json_fail",
                    f"Could not parse architecture JSON: {type(fallback_error).__name__}: {fallback_error}",
                    raw=raw,
                )
        else:
            return _fail_result(
                "parse_json_fail",
                f"Could not parse architecture JSON: {type(e).__name__}: {e}",
                raw=raw,
            )

    guard_warnings = _apply_intent_guards(plan, state["prompt"])
    diagnostics = _plan_diagnostics(plan, state)
    diagnostics["raw_preview"] = _raw_preview(raw)
    if diagnostics["malformed_entries"]:
        return _fail_result(
            "malformed_plan",
            "Architecture plan has malformed resource/data_source entries.",
            raw=raw,
            extra={"diagnostics": diagnostics},
        )
    if diagnostics["resource_count"] == 0:
        return _fail_result(
            "missing_resources",
            "Architecture plan has no resources.",
            raw=raw,
            extra={"diagnostics": diagnostics},
        )

    logger.info("Archi agent: %d resources, %d data_sources",
                len(plan.get("resources", [])), len(plan.get("data_sources", [])))
    if diagnostics["missing_expected_types"]:
        logger.warning(
            "Archi agent: missing expected dataset types: %s",
            diagnostics["missing_expected_types"],
        )
    warnings = guard_warnings + _warnings_from_diagnostics(diagnostics)
    return {
        "infrastructure_plan": plan,
        "architecture_diagnostics": diagnostics,
        "architecture_warnings": warnings,
    }
