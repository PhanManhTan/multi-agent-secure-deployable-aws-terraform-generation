import unittest
from unittest.mock import patch

from agents import architecture, engineering
from agents.deployment import _deterministic_deploy_fix
from agents.validation import _deterministic_schema_fix
from core.terraform import _STUB_CONTENT
from dataset.analyze_results import _issue_owner, _primary_issue
from test_pipeline import _intent_literal_eval


class StaticRuleTests(unittest.TestCase):
    def test_architecture_accepts_missing_data_sources(self):
        raw = """
        {
          "resources": [
            {
              "type": "aws_dynamodb_table",
              "name": "example",
              "attributes": {"name": "example_table"},
              "blocks": {}
            }
          ]
        }
        """
        state = {
            "prompt": "Create a DynamoDB with point-in-time recovery enabled.",
            "expected_resource_types": "aws_dynamodb_table",
        }
        with patch.object(architecture, "call_llm", return_value=raw):
            result = architecture.archi_node(state)

        plan = result["infrastructure_plan"]
        self.assertEqual(plan["data_sources"], [])
        self.assertEqual(plan["resources"][0]["type"], "aws_dynamodb_table")
        self.assertEqual(result["architecture_diagnostics"]["missing_expected_types"], [])

    def test_architecture_intent_guard_adds_s3_logging_and_request_payment(self):
        raw = """
        {
          "resources": [
            {
              "type": "aws_s3_bucket",
              "name": "example",
              "attributes": {"bucket_prefix": "example-bucket-"},
              "blocks": {}
            }
          ],
          "data_sources": []
        }
        """
        state = {
            "prompt": (
                "Create a bucket. Configure logging for access logs to another bucket "
                "with target_prefix log/. Also add request payment configuration with "
                "the bucket owner paying for fees."
            )
        }
        with patch.object(architecture, "call_llm", return_value=raw):
            result = architecture.archi_node(state)

        types = [r["type"] for r in result["infrastructure_plan"]["resources"]]
        self.assertIn("aws_s3_bucket_logging", types)
        self.assertIn("aws_s3_bucket_request_payment_configuration", types)
        self.assertTrue(any(w["kind"] == "intent_guard" for w in result["architecture_warnings"]))

    def test_architecture_intent_guard_adds_iam_policy_for_group_attachment(self):
        raw = """
        {
          "resources": [
            {
              "type": "aws_iam_group",
              "name": "developers",
              "attributes": {"name": "developers"},
              "blocks": {}
            },
            {
              "type": "aws_iam_group_policy_attachment",
              "name": "attach",
              "attributes": {"group": "REF:aws_iam_group.developers.name"},
              "blocks": {}
            }
          ],
          "data_sources": []
        }
        """
        state = {
            "prompt": "Make a basic AWS IAM group example with an example group policy attachment resource attaching a policy to it"
        }
        with patch.object(architecture, "call_llm", return_value=raw):
            result = architecture.archi_node(state)

        plan = result["infrastructure_plan"]
        resource_types = [r["type"] for r in plan["resources"]]
        data_types = [d["type"] for d in plan["data_sources"]]
        attachment = next(r for r in plan["resources"] if r["type"] == "aws_iam_group_policy_attachment")
        self.assertIn("aws_iam_policy", resource_types)
        self.assertIn("aws_iam_policy_document", data_types)
        self.assertTrue(attachment["attributes"]["policy_arn"].startswith("REF:aws_iam_policy."))

    def test_architecture_intent_guard_expands_two_action_api_lambda_shape(self):
        raw = """
        {
          "resources": [
            {
              "type": "aws_api_gateway_rest_api",
              "name": "cat_api",
              "attributes": {"name": "cat-api"},
              "blocks": {}
            },
            {
              "type": "aws_api_gateway_resource",
              "name": "cats",
              "attributes": {
                "rest_api_id": "REF:aws_api_gateway_rest_api.cat_api.id",
                "parent_id": "REF:aws_api_gateway_rest_api.cat_api.root_resource_id",
                "path_part": "cats"
              },
              "blocks": {}
            }
          ],
          "data_sources": []
        }
        """
        state = {
            "prompt": (
                "Build an API Gateway web server with Lambda functions that allows "
                "users to upload cat pictures and provides random cat pictures on demand."
            )
        }
        with patch.object(architecture, "call_llm", return_value=raw):
            result = architecture.archi_node(state)

        plan = result["infrastructure_plan"]
        resource_counts = {}
        for entry in plan["resources"]:
            resource_counts[entry["type"]] = resource_counts.get(entry["type"], 0) + 1
        self.assertGreaterEqual(resource_counts.get("aws_lambda_function", 0), 2)
        self.assertGreaterEqual(resource_counts.get("aws_lambda_permission", 0), 2)
        self.assertGreaterEqual(resource_counts.get("aws_iam_role", 0), 2)
        self.assertGreaterEqual(resource_counts.get("aws_api_gateway_method", 0), 2)
        self.assertGreaterEqual(resource_counts.get("aws_api_gateway_integration", 0), 2)

    def test_engineering_retries_when_plan_declarations_are_omitted(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {
                        "type": "aws_s3_bucket",
                        "name": "bucket",
                        "attributes": {"bucket_prefix": "example-"},
                        "blocks": {},
                    }
                ],
                "data_sources": [
                    {
                        "type": "archive_file",
                        "name": "lambda_zip",
                        "attributes": {
                            "type": "zip",
                            "source_file": "lambda.js",
                            "output_path": "lambda.zip",
                        },
                        "blocks": {},
                    }
                ],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        first = 'resource "aws_s3_bucket" "bucket" { bucket_prefix = "example-" }'
        second = """
        data "archive_file" "lambda_zip" {
          type        = "zip"
          source_file = "lambda.js"
          output_path = "lambda.zip"
        }

        resource "aws_s3_bucket" "bucket" {
          bucket_prefix = "example-"
        }
        """
        with patch.object(engineering, "call_llm", side_effect=[first, second]) as mocked:
            result = engineering.engi_node(state)

        self.assertIn('data "archive_file" "lambda_zip"', result["generated_code"])
        self.assertIn('resource "aws_s3_bucket" "bucket"', result["generated_code"])
        self.assertEqual(mocked.call_count, 2)

    def test_engineering_postprocess_repairs_known_deploy_pitfalls(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_key_pair", "name": "example", "attributes": {}, "blocks": {}},
                    {"type": "aws_instance", "name": "example", "attributes": {}, "blocks": {}},
                    {"type": "aws_lightsail_instance", "name": "example", "attributes": {}, "blocks": {}},
                    {"type": "aws_lambda_function", "name": "example", "attributes": {}, "blocks": {}},
                    {"type": "aws_codebuild_project", "name": "example", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_key_pair" "example" {
          public_key = "not-a-real-key"
        }
        resource "aws_instance" "example" {
          instance_type = "t2.micro"
          cpu_options {
            core_count       = 1
            threads_per_core = 2
          }
        }
        resource "aws_lightsail_instance" "example" {
          publicly_accessible = true
        }
        resource "aws_lambda_function" "example" {
          reserved_concurrent_executions = 10
        }
        resource "aws_codebuild_project" "example" {
          build_spec = "version: 0.2"
          source {
            type = "NO_SOURCE"
          }
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('public_key = file("./key.pub")', code)
        self.assertIn('instance_type = "t3.micro"', code)
        self.assertNotIn("publicly_accessible", code)
        self.assertNotIn("reserved_concurrent_executions", code)
        self.assertIn("buildspec =", code)
        self.assertIn('artifacts {', code)
        self.assertIn('environment {', code)
        self.assertNotRegex(code, r'resource "aws_codebuild_project" "example" \{[\s\S]*?\n\s{2}buildspec =')
        self.assertTrue(result.get("engineering_warnings"))

    def test_engineering_postprocess_adds_missing_codebuild_required_blocks(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_codebuild_project", "name": "example", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_codebuild_project" "example" {
          name         = "example"
          service_role = aws_iam_role.example.arn
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('source {', code)
        self.assertIn('type      = "NO_SOURCE"', code)
        self.assertIn('buildspec =', code)
        self.assertIn('artifacts {', code)
        self.assertIn('type = "NO_ARTIFACTS"', code)
        self.assertIn('environment {', code)

    def test_engineering_postprocess_preserves_requested_reserved_concurrency(self):
        state = {
            "prompt": "Create a Lambda function with reserved concurrency set to 10.",
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_lambda_function", "name": "example", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_lambda_function" "example" {
          reserved_concurrent_executions = 10
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        self.assertIn("reserved_concurrent_executions = 10", result["generated_code"])
        self.assertEqual(result.get("engineering_warnings"), [])

    def test_validation_hints_cover_known_schema_failures(self):
        firehose = _deterministic_schema_fix(
            "aws_kinesis_firehose_delivery_stream splunk unsupported argument role_arn buffer_size buffer_interval",
            "",
        )
        self.assertIn("s3_configuration", firehose)
        self.assertIn("buffering_size", firehose)
        self.assertIn("top level", firehose)

        codebuild = _deterministic_schema_fix(
            'aws_codebuild_project unsupported argument build_spec',
            "",
        )
        self.assertIn("buildspec", codebuild)
        self.assertIn("no underscore", codebuild)

        lightsail = _deterministic_schema_fix(
            'aws_lightsail_instance unsupported argument publicly_accessible',
            "",
        )
        self.assertIn("publicly_accessible", lightsail)
        self.assertIn("remove", lightsail.lower())

    def test_deploy_classifier_covers_free_tier_and_key_stub(self):
        error_type, fix = _deterministic_deploy_fix(
            "InvalidParameterCombination: The specified instance type is not eligible for Free Tier. "
            "cpu_options { core_count = 1 threads_per_core = 2 }"
        )
        self.assertEqual(error_type, "FIXABLE")
        self.assertIn("t3.micro", fix)

        error_type, fix = _deterministic_deploy_fix(
            "InvalidKey.Format: Key is not in valid OpenSSH public key format"
        )
        self.assertEqual(error_type, "FIXABLE")
        self.assertIn("OpenSSH", fix)

    def test_analyzer_primary_issue_classifies_actionable_rows(self):
        row = {
            "row": 19,
            "architecture_error": {
                "kind": "parse_json_fail",
                "message": "Could not parse architecture JSON: missing data_sources",
            },
        }
        label, _ = _primary_issue(row, "")
        self.assertEqual(label, "a1_parse_missing_data_sources")

        row = {
            "row": 12,
            "dataset_eval": {
                "required_resource_match": {
                    "ok": False,
                    "missing": ["aws_s3_bucket_logging"],
                }
            },
            "val": {"ok": True},
        }
        label, _ = _primary_issue(row, "")
        self.assertEqual(label, "dataset_missing_s3_logging")

    def test_analyzer_issue_owner_separates_pipeline_from_benchmark(self):
        owner, _ = _issue_owner("dataset_missing_s3_logging")
        self.assertEqual(owner, "pipeline_a1_a3_intent_coverage")

        owner, _ = _issue_owner("dataset_prompt_mismatch")
        self.assertEqual(owner, "benchmark_dataset_rego_audit")

        owner, _ = _issue_owner("rego_dataset_mismatch")
        self.assertEqual(owner, "benchmark_dataset_rego_audit")

    def test_intent_literal_eval_detects_missing_values(self):
        sample = {
            "prompt": "Configure a custom Time to Live (TTL) attribute for data expiration.",
            "intent": 'ttl attribute_name = "custom_ttl_attribute"',
        }
        result = _intent_literal_eval(
            'resource "aws_dynamodb_table" "t" { ttl { attribute_name = "expires_at" enabled = true } }',
            sample,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing"][0]["name"], "dynamodb_ttl_attribute")

        result = _intent_literal_eval(
            'resource "aws_dynamodb_table" "t" { ttl { attribute_name = "custom_ttl_attribute" enabled = true } }',
            sample,
        )
        self.assertTrue(result["ok"])

    def test_pub_stub_is_realistic_openssh_rsa_key(self):
        pub = _STUB_CONTENT[".pub"]
        self.assertIsInstance(pub, str)
        self.assertTrue(pub.startswith("ssh-rsa AAAA"))
        self.assertIn(" user@example", pub)
        self.assertNotIn("terraformGeneratedBenchmarkKey", pub)


if __name__ == "__main__":
    unittest.main()
