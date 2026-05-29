"""Summarize benchmark result JSON by failure dimension."""
import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path


def _short(text: str | None, limit: int = 220) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _status(value) -> str:
    if value is True:
        return "PASS"
    if value is False:
        return "FAIL"
    if value is None:
        return "NA"
    return str(value)


def _rego_fragility_flags(rego: str | None) -> list[str]:
    rego = rego or ""
    flags = []
    if ".expression." in rego:
        flags.append("typo_expression_singular")
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\.name\s*==\s*\"", rego):
        flags.append("uses_terraform_local_name_as_cloud_name")
    if re.search(r"aws_[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+", rego):
        flags.append("hardcoded_resource_address")
    if '"aws_vpc._.id"' in rego:
        flags.append("impossible_wildcard_reference_string")
    if "prior_state" in rego:
        flags.append("depends_on_prior_state")
    if "constant_value" in rego and rego.count("constant_value") >= 5:
        flags.append("many_exact_constants")
    if "input.configuration.root_module.resources" in rego and "resource_changes" not in rego:
        flags.append("configuration_only_expression_checks")
    return flags


def _known_intent_mismatch(row: dict) -> str | None:
    """Catch high-confidence cases where generated code misses an explicit intent value."""
    intent_literal = ((row.get("dataset_eval") or {}).get("intent_literal_match") or {})
    if intent_literal.get("ok") is False:
        missing = [m.get("name") for m in intent_literal.get("missing") or []]
        return f"Generated code is missing explicit Prompt/Intent literals: {missing}."

    code = row.get("generated_code") or ""
    prompt = row.get("prompt") or ""
    intent = ((row.get("dataset_eval") or {}).get("intent") or "")
    text = f"{prompt}\n{intent}".lower()

    if "custom_ttl_attribute" in text and "custom_ttl_attribute" not in code:
        return "DynamoDB TTL intent requires `custom_ttl_attribute`, but generated code uses another TTL attribute."
    if "password1" in text and "password1" not in code:
        return "ElastiCache password intent includes `password1`/`password2`, but generated code does not."
    if "lambda.js" in text and "lambda.js" not in code:
        return "Lambda source intent mentions `lambda.js`, but generated code uses another package/file."
    return None


def _deployability_name_conflict(row: dict, rego_source: str | None) -> str | None:
    code = row.get("generated_code") or ""
    prompt = (row.get("prompt") or "").lower()
    rego = (rego_source or "").lower()
    if (
        "aws_s3_bucket" in code
        and "example-bucket" in (prompt + "\n" + rego)
        and (
            "bucket_prefix" in code
            or re.search(r'bucket\s*=\s*"example-bucket[-$]', code)
        )
    ):
        return (
            "Dataset/Rego expects fixed S3 bucket name `example-bucket`, but generated code "
            "uses a unique deployable name. S3 bucket names are globally unique, so the "
            "dataset exact-name check conflicts with deployability."
        )
    return None


def _classify_rego_failure(row: dict, rego_source: str | None = None) -> tuple[str, str]:
    """Give a conservative first-pass label for Rego failures.

    This is intentionally not a final judge. It narrows the row into the most
    useful review bucket before a human compares Prompt/Intent/Rego/generated HCL.
    """
    dataset = row.get("dataset_eval") or {}
    required = dataset.get("required_resource_match") or {}
    val = row.get("val") or {}
    deploy = row.get("deploy") or {}
    rego = row.get("rego") or {}

    if val.get("ok") is False:
        return (
            "model_wrong",
            "Generated Terraform did not pass A4 validation, so Rego failure is secondary.",
        )
    if required.get("ok") is False:
        missing = required.get("missing") or []
        return (
            "model_wrong",
            f"Generated code is missing required dataset resources: {missing}.",
        )
    intent_literal = dataset.get("intent_literal_match") or {}
    if intent_literal.get("ok") is False:
        missing = [m.get("name") for m in intent_literal.get("missing") or []]
        return (
            "model_wrong_attribute",
            f"Generated code is missing explicit Prompt/Intent literal checks: {missing}.",
        )
    if deploy.get("ok") is False:
        return (
            "model_or_deployability",
            "A4 passed but AWS apply failed; inspect deploy error before blaming Rego.",
        )
    known_mismatch = _known_intent_mismatch(row)
    if known_mismatch:
        return ("model_wrong_attribute", known_mismatch)
    name_conflict = _deployability_name_conflict(row, rego_source)
    if name_conflict:
        return ("dataset_deployability_conflict", name_conflict)
    fragility = _rego_fragility_flags(rego_source)
    if fragility:
        return (
            "rego_dataset_mismatch",
            "Generated code passes resource/A4/deploy gates, while Rego has fragile or "
            f"dataset-specific checks: {fragility}.",
        )
    false_rules = ", ".join(rego.get("false_rules") or [])
    return (
        "needs_semantic_review",
        "Resource types, A4, and deploy passed; compare false Rego rules with Prompt/Intent "
        f"for attribute/reference mismatch or over-specific Rego. False rules: {false_rules}",
    )


def _deploy_pattern(text: str | None) -> str:
    text = (text or "").lower()
    if "putfunctionconcurrency" in text or "reservedconcurrentexecutions" in text:
        return "lambda_reserved_concurrency"
    if "route 53" in text and ("invaliddomainname" in text or "reserved by aws" in text):
        return "route53_reserved_domain"
    if "elasticache" in text and "user id" in text:
        return "elasticache_user_id_format"
    if "cpuoptions" in text:
        return "ec2_cpu_options_instance_type"
    if "not eligible for free tier" in text:
        return "ec2_free_tier_instance_type"
    if "invalidparametercombination" in text and "instance type" in text:
        return "ec2_instance_type_combination"
    if "iaminstanceprofile" in text or "iam instance profile" in text:
        return "ec2_missing_instance_profile"
    if "invalidkey.format" in text or "importkeypair" in text:
        return "ec2_key_pair_public_key"
    if "bucketalreadyexists" in text:
        return "s3_bucket_global_name"
    if "entityalreadyexists" in text and "role" in text:
        return "iam_name_collision"
    if "codebuild" in text and "buildspec" in text:
        return "codebuild_buildspec"
    if "codebuild" in text and "invalid artifacts" in text:
        return "codebuild_artifacts_location"
    if "api gateway" in text and "no integration defined for method" in text:
        return "api_gateway_missing_integration_dependency"
    if "advanced backup setting" in text and "resource type or backup options is null" in text:
        return "backup_advanced_setting_empty"
    if "lightsail" in text and "already" in text:
        return "lightsail_name_collision"
    if "lightsail" in text and "notfoundexception" in text:
        return "lightsail_eventual_consistency"
    if "putbucketnotificationconfiguration" in text:
        return "s3_notification_destination_policy"
    if "deletionprotection" in text or "delete protected" in text:
        return "destroy_deletion_protection"
    return "other"


def _primary_issue(row: dict, rego_source: str | None) -> tuple[str, str]:
    """Classify the dominant actionable issue for a row."""
    row_id = row["row"]
    prompt = (row.get("prompt") or "").lower()
    arch_error = row.get("architecture_error") or ((row.get("archi") or {}).get("architecture_error")) or {}
    val = row.get("val") or {}
    dataset = row.get("dataset_eval") or {}
    required = dataset.get("required_resource_match") or {}
    rego = row.get("rego") or {}
    deploy = row.get("deploy") or {}

    if arch_error:
        message = (arch_error.get("message") or "").lower()
        if "data_sources" in message:
            return (
                "a1_parse_missing_data_sources",
                "A1 returned valid resources JSON without `data_sources`; fill `data_sources = []` and continue.",
            )
        return (f"a1_{arch_error.get('kind') or 'unknown'}", _short(arch_error.get("message")))

    if val.get("ok") is False:
        text = f"{val.get('raw_error') or ''}\n{val.get('fix_instruction') or ''}".lower()
        if "build_spec" in text:
            return ("a4_codebuild_buildspec_schema", "Use CodeBuild `source.buildspec`, not `build_spec`.")
        if "firehose" in text and "splunk" in text:
            return ("a4_firehose_splunk_schema", "Render Firehose Splunk with nested `splunk_configuration.s3_configuration` schema.")
        if "publicly_accessible" in text:
            return ("a4_lightsail_schema", "Remove unsupported `aws_lightsail_instance.publicly_accessible`.")
        if "data.aws_ssm_parameter" in text or "couldn't find resource" in text:
            return ("a4_ssm_self_lookup", "Do not data-source lookup an SSM parameter created by the same config.")
        return (f"a4_{val.get('error_type') or 'unknown'}", _short(val.get("fix_instruction") or val.get("raw_error")))

    if required.get("ok") is False:
        missing = required.get("missing") or []
        if "aws_s3_bucket_logging" in missing:
            return ("dataset_missing_s3_logging", "Use standalone `aws_s3_bucket_logging` for S3 access logging.")
        if "aws_s3_bucket_request_payment_configuration" in missing:
            return (
                "dataset_missing_s3_request_payment",
                "Use `aws_s3_bucket_request_payment_configuration` with `payer = \"BucketOwner\"`.",
            )
        if "aws_dynamodb_table" in missing and "lambda function alias" in prompt:
            return (
                "dataset_prompt_mismatch",
                "Dataset expects DynamoDB, but prompt only asks for a Lambda alias; audit benchmark row.",
            )
        if {"aws_iam_role", "aws_lambda_function", "aws_lambda_permission"} & set(missing):
            return (
                "model_under_modeled_api_actions",
                "Prompt has upload/read API actions; model should generate separate Lambda/IAM/permission sets when expected.",
            )
        return ("dataset_missing_resources", f"Missing required dataset resources: {missing}.")

    intent_literal = dataset.get("intent_literal_match") or {}
    if intent_literal.get("ok") is False:
        missing = [m.get("name") for m in intent_literal.get("missing") or []]
        return ("intent_literal_mismatch", f"Missing explicit Prompt/Intent literal checks: {missing}.")

    if deploy.get("ok") is False:
        attempts = row.get("deploy_attempt_log") or []
        text = "\n".join(
            (a.get("apply_raw_error") or "") + "\n" + (a.get("fix_instruction") or "")
            for a in attempts
        ).lower()
        if "invalidkey.format" in text or "openssh public key" in text:
            return ("a5_invalid_key_stub", "Use a real OpenSSH public key stub for `file(\"./key.pub\")`.")
        if "not eligible for free tier" in text:
            return ("a5_ec2_free_tier_cpu_options", "Use Free Tier compatible `t3.micro` with valid CPU options.")
        if "lightsail" in text and ("already" in text or "notfoundexception" in text):
            return ("a5_lightsail_name_or_consistency", "Use unique Lightsail names and resource-name references for attachments.")
        return ("a5_deployability", _short((deploy.get("fix_instruction") or deploy.get("apply_raw_error"))))

    if rego.get("ok") is False:
        label, reason = _classify_rego_failure(row, rego_source)
        return (label, reason)

    if (row.get("final_eval") or {}).get("end_to_end_strict_ok"):
        return ("pass", "Strict end-to-end passed.")

    return ("unknown", "No dominant issue classified.")


def _issue_owner(label: str) -> tuple[str, str]:
    """Map a primary issue label to the workstream that should handle it."""
    if label == "pass":
        return ("pass", "No action.")
    if label.startswith("a1_"):
        return ("pipeline_a1_architecture", "Fix A1 parsing/templates/prompt so architecture planning is robust.")
    if label.startswith("a4_"):
        return ("pipeline_a4_validation", "Add deterministic schema/logic repair or strengthen A3 Terraform prompt.")
    if label.startswith("a5_"):
        return ("pipeline_a5_deployability", "Add deterministic deploy repair and AWS-environment-safe defaults.")
    if label in {
        "dataset_missing_s3_logging",
        "dataset_missing_s3_request_payment",
        "model_under_modeled_api_actions",
        "dataset_missing_resources",
        "intent_literal_mismatch",
        "model_wrong",
        "model_wrong_attribute",
    }:
        return ("pipeline_a1_a3_intent_coverage", "Improve A1/A3 so generated resources and explicit literals match the request.")
    if label in {
        "dataset_prompt_mismatch",
        "rego_dataset_mismatch",
        "dataset_deployability_conflict",
    }:
        return ("benchmark_dataset_rego_audit", "Audit dataset/Rego; generated code may be valid/deployable while benchmark is over-specific or inconsistent.")
    if label in {"needs_semantic_review", "model_or_deployability"}:
        return ("manual_semantic_review", "Review generated HCL against Prompt/Intent/Rego before assigning blame.")
    return ("manual_semantic_review", "Unclassified issue; inspect row artifacts.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze pipeline_results.json")
    parser.add_argument(
        "path",
        nargs="?",
        default="reviews/pipeline_results.json",
        help="Path to pipeline result JSON",
    )
    parser.add_argument(
        "--csv",
        default="dataset/data-dev.csv",
        help="Dataset CSV used to fetch Rego intent text for deeper classification",
    )
    args = parser.parse_args()

    path = Path(args.path)
    rows = json.loads(path.read_text(encoding="utf-8"))
    dataset_rows = []
    csv_path = Path(args.csv)
    if csv_path.exists():
        dataset_rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))

    def rego_for(row_id: int) -> str:
        if 0 <= row_id < len(dataset_rows):
            return dataset_rows[row_id].get("Rego intent") or ""
        return ""

    dims = Counter()
    for row in rows:
        for dim in (row.get("final_eval") or {}).get("failed_dimensions") or []:
            dims[dim] += 1

    print(f"Results: {path} ({len(rows)} rows)")
    print(f"Failed dimensions: {dict(dims)}")
    if rows and all(
        "intent_literal_match" not in ((row.get("dataset_eval") or {}))
        for row in rows
        if row.get("dataset_eval")
    ):
        print(
            "Note: this result file does not contain intent_literal_match; rerun "
            "test_pipeline.py to include the newer literal-intent gate."
        )

    strict = [r["row"] for r in rows if (r.get("final_eval") or {}).get("end_to_end_strict_ok")]
    print(f"Strict end-to-end pass rows ({len(strict)}): {strict}")

    resource_missing = {}
    intent_literal_failures = {}
    rego_semantic = []
    rego_dataset_mismatch = []
    rego_dataset_deployability_conflict = []
    rego_model_wrong_attribute = []
    rego_model_wrong = []
    a4_failures = {}
    deploy_patterns = Counter()
    deploy_pattern_rows: dict[str, set[int]] = {}
    architecture_failures = []
    deploy_failure_rows = []

    for row in rows:
        row_id = row["row"]
        final_eval = row.get("final_eval") or {}
        failed_dimensions = final_eval.get("failed_dimensions") or []
        if "architecture" in failed_dimensions:
            architecture_failures.append(row_id)
        if "aws_deploy" in failed_dimensions:
            deploy_failure_rows.append(row_id)

        dataset = row.get("dataset_eval") or {}
        required = dataset.get("required_resource_match") or {}
        if required.get("ok") is False:
            resource_missing[row_id] = required.get("missing") or []
        intent_literal = dataset.get("intent_literal_match") or {}
        if intent_literal.get("ok") is False:
            intent_literal_failures[row_id] = [
                m.get("name") for m in intent_literal.get("missing") or []
            ]

        val = row.get("val") or {}
        if val.get("ok") is False:
            a4_failures[row_id] = val.get("error_type")

        rego = row.get("rego") or {}
        if rego.get("ok") is False:
            label, _ = _classify_rego_failure(row, rego_for(row_id))
            if label == "needs_semantic_review":
                rego_semantic.append(row_id)
            elif label == "rego_dataset_mismatch":
                rego_dataset_mismatch.append(row_id)
            elif label == "dataset_deployability_conflict":
                rego_dataset_deployability_conflict.append(row_id)
            elif label == "model_wrong_attribute":
                rego_model_wrong_attribute.append(row_id)
            else:
                rego_model_wrong.append(row_id)

        for attempt in row.get("deploy_attempt_log") or []:
            if attempt.get("ok"):
                continue
            pattern = _deploy_pattern(attempt.get("apply_raw_error") or attempt.get("fix_instruction"))
            deploy_patterns[pattern] += 1
            deploy_pattern_rows.setdefault(pattern, set()).add(row_id)

    code_issue_rows = set(architecture_failures)
    code_issue_rows.update(resource_missing)
    code_issue_rows.update(intent_literal_failures)
    code_issue_rows.update(a4_failures)
    code_issue_rows.update(deploy_failure_rows)
    code_issue_rows.update(rego_model_wrong_attribute)
    code_issue_rows.update(rego_model_wrong)

    benchmark_issue_rows = set(rego_dataset_mismatch) | set(rego_dataset_deployability_conflict)
    benchmark_only_rows = sorted(benchmark_issue_rows - code_issue_rows)
    adjusted_code_success_rows = sorted(row["row"] for row in rows if row["row"] not in code_issue_rows)
    deploy_ok_rows = sorted(
        row["row"] for row in rows
        if (row.get("deploy") or {}).get("ok") is True
    )
    terraform_ok_rows = sorted(
        row["row"] for row in rows
        if (row.get("val") or {}).get("ok") is True
    )

    print("\nAction backlog")
    print(f"- A1 architecture hard fail rows: {architecture_failures}")
    if architecture_failures:
        print("- A1 failure details:")
        for row in rows:
            if row["row"] not in architecture_failures:
                continue
            arch_error = row.get("architecture_error") or ((row.get("archi") or {}).get("architecture_error")) or {}
            diagnostics = row.get("architecture_diagnostics") or ((row.get("archi") or {}).get("diagnostics")) or {}
            print(
                f"  row {row['row']}: kind={arch_error.get('kind')} "
                f"message={_short(arch_error.get('message'))}"
            )
            if diagnostics.get("missing_expected_types") or diagnostics.get("malformed_entries"):
                print(
                    f"    diagnostics: missing={diagnostics.get('missing_expected_types', [])} "
                    f"malformed={diagnostics.get('malformed_entries', [])}"
                )
            raw_preview = arch_error.get("raw_preview") or diagnostics.get("raw_preview")
            if raw_preview:
                print(f"    raw_preview={_short(raw_preview, 300)}")
    print(f"- Dataset resource missing rows: {resource_missing}")
    print(f"- Intent literal missing rows: {intent_literal_failures}")
    print(f"- A4 validation failure rows: {a4_failures}")
    print(f"- Rego dataset-mismatch likely rows: {rego_dataset_mismatch}")
    print(f"- Rego dataset-vs-deployability conflict rows: {rego_dataset_deployability_conflict}")
    print(f"- Rego model-wrong-attribute rows: {rego_model_wrong_attribute}")
    print(f"- Rego unresolved semantic-review rows: {rego_semantic}")
    print(f"- Rego model-wrong/secondary rows: {rego_model_wrong}")
    if deploy_patterns:
        print("- Deploy failure patterns:")
        for pattern, count in deploy_patterns.most_common():
            print(f"  {pattern}: {count} attempts rows={sorted(deploy_pattern_rows[pattern])}")

    print("\nQuality gates")
    print(f"- Terraform validated rows: {len(terraform_ok_rows)}/{len(rows)} rows={terraform_ok_rows}")
    print(f"- AWS deploy OK rows:       {len(deploy_ok_rows)}/{len(rows)} rows={deploy_ok_rows}")
    print(f"- Strict dataset E2E rows:  {len(strict)}/{len(rows)} rows={strict}")
    print(
        f"- Adjusted code-success rows (ignores benchmark-only Rego issues): "
        f"{len(adjusted_code_success_rows)}/{len(rows)} rows={adjusted_code_success_rows}"
    )
    print(f"- Benchmark-only issue rows: {benchmark_only_rows}")

    print("\nRecommended next actions")
    if architecture_failures:
        print(f"- A1: add/verify deterministic architecture templates for rows {architecture_failures}.")
    if resource_missing:
        print("- A1/A3: improve intent coverage for missing resource types:")
        for row_id, missing in resource_missing.items():
            print(f"  row {row_id}: missing {missing}")
    if intent_literal_failures:
        print("- A1/A3: preserve explicit Prompt/Intent literals:")
        for row_id, missing in intent_literal_failures.items():
            print(f"  row {row_id}: missing literal checks {missing}")
    if a4_failures:
        print("- A4: add schema/logic repair rules for validation failures:")
        for row_id, error_type in a4_failures.items():
            print(f"  row {row_id}: {error_type}")
    if deploy_failure_rows:
        print(f"- A5: prioritize deterministic deploy fixes for rows {sorted(set(deploy_failure_rows))}.")
    if rego_model_wrong_attribute:
        print(f"- A1/A3 literal preservation: fix semantic attributes for rows {rego_model_wrong_attribute}.")
    if benchmark_only_rows:
        print(
            "- Dataset/Rego: audit benchmark-only rows; these should not be counted as code failure "
            "when code validates and deploys."
        )

    print("\nPrimary issue by failed row")
    owner_rows: dict[str, list[int]] = {}
    owner_reasons: dict[str, str] = {}
    primary_rows: dict[int, tuple[str, str, str]] = {}
    for row in sorted(rows, key=lambda r: r["row"]):
        final_eval = row.get("final_eval") or {}
        if not final_eval.get("failed_dimensions"):
            continue
        label, reason = _primary_issue(row, rego_for(row["row"]))
        owner, owner_reason = _issue_owner(label)
        owner_rows.setdefault(owner, []).append(row["row"])
        owner_reasons.setdefault(owner, owner_reason)
        primary_rows[row["row"]] = (label, reason, owner)
        print(f"- row {row['row']}: {label} - {_short(reason, 260)}")

    if owner_rows:
        print("\nAction owners")
        order = [
            "pipeline_a1_architecture",
            "pipeline_a1_a3_intent_coverage",
            "pipeline_a4_validation",
            "pipeline_a5_deployability",
            "benchmark_dataset_rego_audit",
            "manual_semantic_review",
        ]
        for owner in order:
            if owner not in owner_rows:
                continue
            print(f"- {owner}: rows={owner_rows[owner]}")
            print(f"  why: {owner_reasons[owner]}")

        pipeline_rows = sorted(
            row_id
            for owner, row_ids in owner_rows.items()
            if owner.startswith("pipeline_")
            for row_id in row_ids
        )
        audit_rows = sorted(owner_rows.get("benchmark_dataset_rego_audit", []))
        review_rows = sorted(owner_rows.get("manual_semantic_review", []))
        if pipeline_rows:
            print(f"- targeted pipeline rerun: python test_pipeline.py --cases {' '.join(map(str, pipeline_rows))} --workers 4")
        if audit_rows:
            print(f"- benchmark/Rego audit rows: {audit_rows}")
        if review_rows:
            print(f"- manual semantic review rows: {review_rows}")

    print("\nRow summary")
    print("row | A4 | Resource | Intent | Rego | Deploy | Strict | failed_dimensions")
    for row in sorted(rows, key=lambda r: r["row"]):
        final_eval = row.get("final_eval") or {}
        dataset = row.get("dataset_eval") or {}
        resource_ok = (dataset.get("required_resource_match") or {}).get("ok")
        intent_literal_ok = (dataset.get("intent_literal_match") or {}).get("ok")
        rego = row.get("rego") or {}
        deploy = row.get("deploy") or {}
        print(
            f"{row['row']:>3} | "
            f"{_status((row.get('val') or {}).get('ok')):<4} | "
            f"{_status(resource_ok):<8} | "
            f"{_status(intent_literal_ok):<6} | "
            f"{_status(None if rego.get('skipped') else rego.get('ok')):<4} | "
            f"{_status(deploy.get('ok')):<6} | "
            f"{_status(final_eval.get('end_to_end_strict_ok')):<6} | "
            f"{final_eval.get('failed_dimensions') or []}"
        )

    print("\nRego failures")
    for row in rows:
        rego = row.get("rego") or {}
        if rego.get("ok") is not False:
            continue
        deploy = row.get("deploy") or {}
        dataset = row.get("dataset_eval") or {}
        required = dataset.get("required_resource_match") or {}
        print(
            f"- row {row['row']}: rule={rego.get('rule')} "
            f"type={rego.get('entrypoint_type')} deploy_ok={deploy.get('ok')} "
            f"resource_ok={required.get('ok')}"
        )
        rego_source = rego_for(row["row"])
        label, reason = _classify_rego_failure(row, rego_source)
        print(f"  classification: {label} - {reason}")
        flags = _rego_fragility_flags(rego_source)
        if flags:
            print(f"  rego_fragility: {flags}")
        print(f"  prompt: {_short(row.get('prompt'))}")
        print(f"  intent: {_short(dataset.get('intent'))}")
        print(f"  error: {_short(rego.get('error'))}")
        if rego.get("true_rules"):
            print(f"  true_rules: {rego.get('true_rules')}")
        if rego.get("false_rules"):
            print(f"  false_rules: {rego.get('false_rules')}")

    print("\nTerraform validation failures")
    for row in rows:
        val = row.get("val") or {}
        if val.get("ok") is not False:
            continue
        print(f"- row {row['row']}: error_type={val.get('error_type')}")
        print(f"  fix: {_short(val.get('fix_instruction'))}")

    print("\nDeploy failures and retries")
    for row in rows:
        attempts = row.get("deploy_attempt_log") or []
        deploy = row.get("deploy") or {}
        if not attempts and not deploy:
            continue
        if deploy.get("ok") and len(attempts) <= 1:
            continue
        print(f"- row {row['row']}: final_deploy_ok={deploy.get('ok')} attempts={len(attempts)}")
        for attempt in attempts:
            print(
                f"  attempt {attempt.get('attempt')}: ok={attempt.get('ok')} "
                f"route={attempt.get('route')} error={attempt.get('error_type')}"
            )
            if attempt.get("fix_instruction"):
                print(f"    fix: {_short(attempt.get('fix_instruction'))}")
            if attempt.get("apply_raw_error"):
                print(f"    err: {_short(attempt.get('apply_raw_error'))}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
