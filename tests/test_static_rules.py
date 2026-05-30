import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import architecture, engineering
from agents.deployment import _deterministic_deploy_fix
from agents.validation import _deterministic_schema_fix
from core.terraform import _STUB_CONTENT, write_terraform_dir
from dataset.analyze_results import _final_flag_rows, _issue_owner, _primary_issue
from benchmark_pipeline import _intent_literal_eval, _row_code_success_flags


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
        self.assertGreaterEqual(resource_counts.get("aws_api_gateway_deployment", 0), 1)
        self.assertGreaterEqual(resource_counts.get("aws_api_gateway_stage", 0), 1)

    def test_architecture_uses_deterministic_cat_upload_plan(self):
        state = {
            "prompt": (
                "An AWS service that holds a web server which allows you to upload cat pictures "
                "and provides random cat pictures on demand. Accomplish this using AWS DynamoDB "
                "table, AWS S3 bucket, AWS Lambda function, AWS Lambda permission, AWS API "
                "Gateway rest API, AWS API Gateway resource, AWS API Gateway method."
            )
        }
        with patch.object(architecture, "call_llm", side_effect=TimeoutError("should not call LLM")):
            result = architecture.archi_node(state)

        plan = result["infrastructure_plan"]
        types = [r["type"] for r in plan["resources"]]
        self.assertEqual(result["architecture_strategy"], "deterministic_template")
        self.assertIn("aws_dynamodb_table", types)
        self.assertIn("aws_s3_bucket", types)
        self.assertGreaterEqual(types.count("aws_lambda_function"), 2)
        self.assertGreaterEqual(types.count("aws_api_gateway_method"), 2)
        self.assertIn("aws_api_gateway_deployment", types)
        self.assertIn("aws_api_gateway_stage", types)

    def test_architecture_uses_minimal_elasticache_user_plan(self):
        state = {"prompt": "authenticate a elasticache user with passwords"}
        with patch.object(architecture, "call_llm", side_effect=TimeoutError("should not call LLM")):
            result = architecture.archi_node(state)

        plan = result["infrastructure_plan"]
        types = [r["type"] for r in plan["resources"]]
        self.assertEqual(result["architecture_strategy"], "deterministic_template")
        self.assertEqual(types, ["aws_elasticache_user"])
        auth = plan["resources"][0]["blocks"]["authentication_mode"]
        self.assertEqual(auth["passwords"], ["password1", "password2"])

    def test_architecture_intent_guard_preserves_elasticache_passwords(self):
        raw = """
        {
          "resources": [
            {
              "type": "aws_elasticache_user",
              "name": "redis_user",
              "attributes": {},
              "blocks": {}
            }
          ],
          "data_sources": []
        }
        """
        state = {"prompt": "Provide an ElastiCache user resource with password."}
        with patch.object(architecture, "call_llm", return_value=raw):
            result = architecture.archi_node(state)

        user = result["infrastructure_plan"]["resources"][0]
        self.assertEqual(user["attributes"]["engine"], "REDIS")
        auth = user["blocks"]["authentication_mode"]
        self.assertEqual(auth["type"], "password")
        self.assertEqual(auth["passwords"], ["password1", "password2"])

    def test_architecture_intent_guard_adds_eventbridge_lambda_schedule(self):
        raw = """
        {
          "resources": [
            {
              "type": "aws_lambda_function",
              "name": "cron",
              "attributes": {},
              "blocks": {}
            }
          ],
          "data_sources": []
        }
        """
        state = {
            "prompt": (
                "An AWS EventBridge event rule named cron scheduled everyday at 7 UTC "
                "linked to a Lambda function with deployment packet."
            )
        }
        with patch.object(architecture, "call_llm", return_value=raw):
            result = architecture.archi_node(state)

        plan = result["infrastructure_plan"]
        types = [r["type"] for r in plan["resources"]]
        data_types = [d["type"] for d in plan["data_sources"]]
        self.assertIn("aws_cloudwatch_event_rule", types)
        self.assertIn("aws_cloudwatch_event_target", types)
        self.assertIn("aws_lambda_permission", types)
        self.assertIn("aws_iam_role", types)
        self.assertIn("archive_file", data_types)
        rule = next(r for r in plan["resources"] if r["type"] == "aws_cloudwatch_event_rule")
        self.assertEqual(rule["attributes"]["schedule_expression"], "cron(0 7 * * ? *)")
        fn = next(r for r in plan["resources"] if r["type"] == "aws_lambda_function")
        self.assertEqual(fn["attributes"]["runtime"], "python3.12")

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

    def test_engineering_provider_region_follows_prompt(self):
        state = {
            "prompt": "Create a VPC in the AWS us-east-2 region.",
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_vpc", "name": "main", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = 'resource "aws_vpc" "main" { cidr_block = "10.0.0.0/16" }'
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        self.assertIn('region = "us-east-2"', result["generated_code"])
        self.assertNotIn('region = "us-east-1"\n}', result["generated_code"])

    def test_engineering_converts_iam_policy_document_resource_to_data(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_iam_role", "name": "example", "attributes": {}, "blocks": {}},
                    {"type": "aws_iam_policy_document", "name": "assume", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_iam_policy_document" "assume" {
          statement {
            actions = ["sts:AssumeRole"]
          }
        }
        resource "aws_iam_role" "example" {
          assume_role_policy = aws_iam_policy_document.assume.json
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('data "aws_iam_policy_document" "assume"', code)
        self.assertNotIn('resource "aws_iam_policy_document"', code)
        self.assertIn("data.aws_iam_policy_document.assume.json", code)

    def test_engineering_converts_aws_ami_resource_to_data(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_instance", "name": "example", "attributes": {}, "blocks": {}},
                    {"type": "aws_ami", "name": "latest_al2", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_ami" "latest_al2" {
          most_recent = true
          owners      = ["amazon"]
        }
        resource "aws_instance" "example" {
          ami           = aws_ami.latest_al2.id
          instance_type = "t3.micro"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('data "aws_ami" "latest_al2"', code)
        self.assertNotIn('resource "aws_ami"', code)
        self.assertIn("data.aws_ami.latest_al2.id", code)

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

    def test_engineering_repairs_codebuild_name_prefix(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_codebuild_project", "name": "project", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_codebuild_project" "project" {
          name_prefix  = "example-project-"
          service_role = aws_iam_role.example.arn
          source { type = "NO_SOURCE" }
          artifacts { type = "NO_ARTIFACTS" }
          environment {
            compute_type = "BUILD_GENERAL1_SMALL"
            image        = "aws/codebuild/standard:7.0"
            type         = "LINUX_CONTAINER"
          }
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('name = "example-project"', code)
        self.assertNotIn("name_prefix", code)

    def test_engineering_removes_invalid_codebuild_cloudwatch_logs_encryption_flag(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_codebuild_project", "name": "project", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_codebuild_project" "project" {
          name         = "example-project"
          service_role = aws_iam_role.example.arn
          source { type = "NO_SOURCE" }
          artifacts {
            type                = "NO_ARTIFACTS"
            encryption_disabled = false
          }
          environment {
            compute_type = "BUILD_GENERAL1_SMALL"
            image        = "aws/codebuild/standard:7.0"
            type         = "LINUX_CONTAINER"
          }
          logs_config {
            cloudwatch_logs {
              group_name          = "/aws/codebuild/example"
              stream_name         = "main"
              encryption_disabled = false
            }
          }
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn("artifacts {", code)
        self.assertIn("encryption_disabled = false", code)
        logs_block = code.split("cloudwatch_logs", 1)[1]
        self.assertNotIn("encryption_disabled", logs_block)

    def test_engineering_repairs_firehose_splunk_schema(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {
                        "type": "aws_kinesis_firehose_delivery_stream",
                        "name": "splunk_stream",
                        "attributes": {},
                        "blocks": {},
                    },
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_kinesis_firehose_delivery_stream" "splunk_stream" {
          name        = "splunk-firehose-stream"
          destination = "splunk"
          role_arn    = aws_iam_role.firehose_role.arn

          splunk_configuration {
            hec_endpoint   = "https://splunk-hec.example.com:8088"
            hec_token      = "SplunkHECTokenExample"
            s3_backup_mode = "FailedDataOnly"
            s3_configuration {
              bucket_arn = aws_s3_bucket.backup.arn
              role_arn   = aws_iam_role.firehose_role.arn
            }
          }

          cloudwatch_logging_options {
            enabled = true
          }
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertNotIn("role_arn    = aws_iam_role.firehose_role.arn", code)
        self.assertIn("role_arn   = aws_iam_role.firehose_role.arn", code)
        self.assertNotIn("cloudwatch_logging_options", code)

    def test_engineering_repairs_ssm_parameter_key_id(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_ssm_parameter", "name": "github_token", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_ssm_parameter" "github_token" {
          name       = "/codebuild/github/token"
          type       = "SecureString"
          value      = "example-token"
          kms_key_id = "alias/aws/ssm"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('key_id = "alias/aws/ssm"', code)
        self.assertNotIn("kms_key_id", code)

    def test_engineering_repairs_s3_notification_sns_policy_source_arn(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_sns_topic", "name": "topic", "attributes": {}, "blocks": {}},
                    {"type": "aws_s3_bucket", "name": "bucket", "attributes": {}, "blocks": {}},
                    {"type": "aws_sns_topic_policy", "name": "topic_policy", "attributes": {}, "blocks": {}},
                    {
                        "type": "aws_s3_bucket_notification",
                        "name": "bucket_notification",
                        "attributes": {},
                        "blocks": {},
                    },
                ],
                "data_sources": [
                    {"type": "aws_iam_policy_document", "name": "topic_policy", "attributes": {}, "blocks": {}},
                ],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_sns_topic" "topic" {
          name = "topic"
          kms_master_key_id = "alias/aws/sns"
        }
        resource "aws_s3_bucket" "bucket" {
          bucket_prefix = "bucket-"
        }
        data "aws_iam_policy_document" "topic_policy" {
          statement {
            actions   = ["SNS:Publish"]
            resources = [aws_sns_topic.topic.arn]
            principals {
              type        = "Service"
              identifiers = ["s3.amazonaws.com"]
            }
            condition {
              test     = "ArnLike"
              variable = "aws:SourceArn"
              values   = ["arn:aws:s3:::example-bucket"]
            }
          }
        }
        resource "aws_sns_topic_policy" "topic_policy" {
          arn    = aws_sns_topic.topic.arn
          policy = data.aws_iam_policy_document.topic_policy.json
        }
        resource "aws_s3_bucket_notification" "bucket_notification" {
          bucket     = aws_s3_bucket.bucket.id
          depends_on = [aws_sns_topic_policy.topic_policy]
          topic {
            topic_arn = aws_sns_topic.topic.arn
            events    = ["s3:ObjectCreated:*"]
          }
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn("values   = [aws_s3_bucket.bucket.arn]", code)
        self.assertIn('actions   = ["sns:Publish"]', code)
        self.assertNotIn("kms_master_key_id", code)
        self.assertNotIn("arn:aws:s3:::example-bucket", code)

    def test_engineering_repairs_s3_acl_ownership_controls(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_s3_bucket", "name": "logging_bucket", "attributes": {}, "blocks": {}},
                    {
                        "type": "aws_s3_bucket_ownership_controls",
                        "name": "logging_bucket_ownership_controls",
                        "attributes": {},
                        "blocks": {},
                    },
                    {"type": "aws_s3_bucket_acl", "name": "logging_bucket_acl", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_s3_bucket" "logging_bucket" {
          bucket_prefix = "logging-"
        }
        resource "aws_s3_bucket_ownership_controls" "logging_bucket_ownership_controls" {
          bucket = aws_s3_bucket.logging_bucket.id
          rule {
            object_ownership = "BucketOwnerEnforced"
          }
        }
        resource "aws_s3_bucket_acl" "logging_bucket_acl" {
          bucket = aws_s3_bucket.logging_bucket.id
          acl    = "log-delivery-write"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('object_ownership = "BucketOwnerPreferred"', code)
        self.assertNotIn('object_ownership = "BucketOwnerEnforced"', code)

    def test_engineering_removes_duplicate_s3_bucket_data_source(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_s3_bucket", "name": "logging_bucket", "attributes": {}, "blocks": {}},
                    {"type": "aws_s3_bucket_logging", "name": "logs", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_s3_bucket" "logging_bucket" {
          bucket = "logging-680235478471"
        }
        data "aws_s3_bucket" "logging_bucket" {
          bucket = "logging-680235478471"
        }
        resource "aws_s3_bucket_logging" "logs" {
          bucket        = aws_s3_bucket.logging_bucket.id
          target_bucket = data.aws_s3_bucket.logging_bucket.bucket
          target_prefix = "log/"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertNotIn('data "aws_s3_bucket" "logging_bucket"', code)
        self.assertIn("target_bucket = aws_s3_bucket.logging_bucket.bucket", code)
        self.assertIn('bucket_prefix = "logging-680235478471-"', code)

    def test_engineering_normalizes_elasticache_passwords_and_removes_slow_cluster(self):
        state = {
            "prompt": "authenticate a elasticache user with passwords",
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_elasticache_user", "name": "auth_user", "attributes": {}, "blocks": {}},
                    {
                        "type": "aws_elasticache_replication_group",
                        "name": "main",
                        "attributes": {},
                        "blocks": {},
                    },
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_elasticache_user" "auth_user" {
          user_id       = "auth-user"
          user_name     = "default"
          engine        = "redis"
          access_string = "on ~* +@all"
          authentication_mode {
            type      = "password"
            passwords = ["MySecretPassword123!"]
          }
        }
        resource "aws_elasticache_replication_group" "main" {
          replication_group_id = "auth-rg"
          description          = "slow"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('"password1password1"', code)
        self.assertIn('"password2password2"', code)
        self.assertIn('resource "random_id" "elasticache_user_suffix"', code)
        self.assertIn('user_id = "auth-user-${random_id.elasticache_user_suffix.hex}"', code)
        self.assertNotIn('resource "aws_elasticache_replication_group"', code)

    def test_engineering_rewrites_placeholder_s3_bucket_names_only(self):
        state = {
            "prompt": "Create two S3 buckets.",
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_s3_bucket", "name": "example", "attributes": {}, "blocks": {}},
                    {"type": "aws_s3_bucket", "name": "specific", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_s3_bucket" "example" {
          bucket = "example-codebuild-source-bucket"
        }
        resource "aws_s3_bucket" "specific" {
          bucket = "pike-680235478471"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('bucket_prefix = "example-codebuild-source-bucket-"', code)
        self.assertNotIn('bucket = "example-codebuild-source-bucket"', code)
        self.assertIn('bucket = "pike-680235478471"', code)

    def test_engineering_preserves_prompt_named_my_bucket(self):
        state = {
            "prompt": 'Create an S3 bucket named "my-company-logs".',
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_s3_bucket", "name": "logs", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_s3_bucket" "logs" {
          bucket = "my-company-logs"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('bucket = "my-company-logs"', code)
        self.assertNotIn('bucket_prefix = "my-company-logs-"', code)

    def test_engineering_adds_s3_safety_companion_resources(self):
        state = {
            "prompt": "Create a private S3 bucket.",
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_s3_bucket", "name": "bucket", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_s3_bucket" "bucket" {
          bucket_prefix = "private-bucket-"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('resource "aws_s3_bucket_public_access_block" "bucket_public_access"', code)
        self.assertIn("restrict_public_buckets = true", code)
        self.assertIn(
            'resource "aws_s3_bucket_server_side_encryption_configuration" "bucket_encryption"',
            code,
        )
        self.assertIn('sse_algorithm = "AES256"', code)
        self.assertIn('resource "aws_s3_bucket_versioning" "bucket_versioning"', code)
        self.assertIn('status = "Enabled"', code)

    def test_engineering_block_public_access_prompt_still_adds_public_access_block(self):
        state = {
            "prompt": "Create an S3 bucket and block public access.",
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_s3_bucket", "name": "bucket", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_s3_bucket" "bucket" {
          bucket_prefix = "private-bucket-"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        self.assertIn(
            'resource "aws_s3_bucket_public_access_block" "bucket_public_access"',
            result["generated_code"],
        )

    def test_engineering_skips_public_access_block_for_public_s3_website(self):
        state = {
            "prompt": "Create an S3 static website with public access.",
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_s3_bucket", "name": "website", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_s3_bucket" "website" {
          bucket_prefix = "website-bucket-"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertNotIn('aws_s3_bucket_public_access_block" "website_public_access"', code)
        self.assertIn('aws_s3_bucket_server_side_encryption_configuration" "website_encryption"', code)

    def test_engineering_does_not_duplicate_existing_s3_safety_resources(self):
        state = {
            "prompt": "Create a private S3 bucket.",
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_s3_bucket", "name": "bucket", "attributes": {}, "blocks": {}},
                    {
                        "type": "aws_s3_bucket_public_access_block",
                        "name": "custom_public_block",
                        "attributes": {},
                        "blocks": {},
                    },
                    {
                        "type": "aws_s3_bucket_server_side_encryption_configuration",
                        "name": "custom_sse",
                        "attributes": {},
                        "blocks": {},
                    },
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_s3_bucket" "bucket" {
          bucket_prefix = "private-bucket-"
        }
        resource "aws_s3_bucket_public_access_block" "custom_public_block" {
          bucket = aws_s3_bucket.bucket.id
          block_public_acls = true
        }
        resource "aws_s3_bucket_server_side_encryption_configuration" "custom_sse" {
          bucket = aws_s3_bucket.bucket.id
          rule {
            apply_server_side_encryption_by_default {
              sse_algorithm = "AES256"
            }
          }
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertEqual(code.count('resource "aws_s3_bucket_public_access_block"'), 1)
        self.assertEqual(
            code.count('resource "aws_s3_bucket_server_side_encryption_configuration"'),
            1,
        )
        self.assertNotIn('"bucket_public_access"', code)
        self.assertNotIn('"bucket_encryption"', code)

    def test_engineering_adds_low_risk_security_hardening(self):
        state = {
            "prompt": "Create DynamoDB, EFS, Lambda, and API Gateway stage resources.",
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_dynamodb_table", "name": "table", "attributes": {}, "blocks": {}},
                    {"type": "aws_efs_file_system", "name": "efs", "attributes": {}, "blocks": {}},
                    {"type": "aws_lambda_function", "name": "fn", "attributes": {}, "blocks": {}},
                    {"type": "aws_api_gateway_stage", "name": "stage", "attributes": {}, "blocks": {}},
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_dynamodb_table" "table" {
          name         = "table"
          billing_mode = "PAY_PER_REQUEST"
          hash_key     = "id"
          attribute {
            name = "id"
            type = "S"
          }
        }
        resource "aws_efs_file_system" "efs" {
          lifecycle_policy {
            transition_to_ia = "AFTER_30_DAYS"
          }
        }
        resource "aws_lambda_function" "fn" {
          function_name = "fn"
          role          = aws_iam_role.fn.arn
          filename      = "lambda.zip"
          handler       = "index.handler"
          runtime       = "nodejs18.x"
        }
        resource "aws_api_gateway_stage" "stage" {
          rest_api_id   = aws_api_gateway_rest_api.api.id
          deployment_id = aws_api_gateway_deployment.api.id
          stage_name    = "prod"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertIn('resource "aws_kms_key" "generated_hardening"', code)
        self.assertIn("kms_key_arn = aws_kms_key.generated_hardening.arn", code)
        self.assertIn("kms_key_id = aws_kms_key.generated_hardening.arn", code)
        self.assertIn("tracing_config", code)
        self.assertIn('mode = "Active"', code)
        self.assertIn("xray_tracing_enabled = true", code)

    def test_engineering_moves_efs_lifecycle_policy_into_file_system(self):
        state = {
            "infrastructure_plan": {
                "resources": [
                    {"type": "aws_efs_file_system", "name": "example", "attributes": {}, "blocks": {}},
                    {
                        "type": "aws_efs_file_system_lifecycle_policy",
                        "name": "example",
                        "attributes": {},
                        "blocks": {},
                    },
                ],
                "data_sources": [],
            },
            "security_ckv_ids": {},
            "fix_feedback": {},
            "eng_retry_count": 0,
            "deploy_eng_retry_count": 0,
        }
        raw = '''
        resource "aws_efs_file_system" "example" {
          tags = {
            Name = "example"
          }
        }
        resource "aws_efs_file_system_lifecycle_policy" "example" {
          file_system_id   = aws_efs_file_system.example.id
          transition_to_ia = "AFTER_7_DAYS"
        }
        '''
        with patch.object(engineering, "call_llm", return_value=raw):
            result = engineering.engi_node(state)

        code = result["generated_code"]
        self.assertNotIn('resource "aws_efs_file_system_lifecycle_policy"', code)
        self.assertIn("lifecycle_policy", code)
        self.assertIn('transition_to_ia = "AFTER_7_DAYS"', code)

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
        self.assertIn("tracing_config", result["generated_code"])
        self.assertTrue(result.get("engineering_warnings"))

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

        ami = _deterministic_schema_fix(
            'aws_ami unsupported argument most_recent owners',
            "",
        )
        self.assertIn('data "aws_ami"', ami)

        s3_sse = _deterministic_schema_fix(
            'aws_s3_bucket unsupported block type server_side_encryption_configuration aws_kms_key kms_master_key_id',
            "",
        )
        self.assertIn("aws_s3_bucket_server_side_encryption_configuration", s3_sse)
        self.assertIn("aws:kms", s3_sse)

        secret_version = _deterministic_schema_fix(
            "aws_secretsmanager_secret_version missing required argument secret_string",
            "",
        )
        self.assertIn("secret_string", secret_version)

        elasticache_group = _deterministic_schema_fix(
            "aws_elasticache_user_group missing required argument user_ids unsupported block user",
            "",
        )
        self.assertIn("user_ids", elasticache_group)

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

        error_type, fix = _deterministic_deploy_fix(
            "Error: creating ElastiCache User (auth-user): UserAlreadyExists: User auth-user already exists."
        )
        self.assertEqual(error_type, "FIXABLE")
        self.assertIn("random_id", fix)

    def test_deploy_classifier_separates_environment_limits(self):
        error_type, fix = _deterministic_deploy_fix(
            "SubscriptionRequiredException: You are not currently subscribed to this service."
        )
        self.assertEqual(error_type, "ENV_LIMITATION")
        self.assertIn("account/region", fix)

        error_type, fix = _deterministic_deploy_fix(
            "Lightsail InvalidInputException: account quota limit exceeded"
        )
        self.assertEqual(error_type, "QUOTA")
        self.assertIn("quota", fix.lower())

        error_type, fix = _deterministic_deploy_fix(
            "AuthorizationHeaderMalformed: the region 'us-east-1' is wrong; expecting 'us-east-2'"
        )
        self.assertEqual(error_type, "FIXABLE")
        self.assertIn("provider region", fix)

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

        row = {
            "row": 30,
            "val": {"ok": True},
            "dataset_eval": {"required_resource_match": {"ok": True}},
            "deploy": {"ok": False},
            "deploy_attempt_log": [
                {
                    "error_type": "ENV_LIMITATION",
                    "apply_raw_error": "SubscriptionRequiredException: not currently subscribed",
                }
            ],
        }
        label, _ = _primary_issue(row, "")
        self.assertEqual(label, "a5_env_service_subscription")

    def test_analyzer_issue_owner_separates_pipeline_from_benchmark(self):
        owner, _ = _issue_owner("dataset_missing_s3_logging")
        self.assertEqual(owner, "pipeline_a1_a3_intent_coverage")

        owner, _ = _issue_owner("dataset_prompt_mismatch")
        self.assertEqual(owner, "benchmark_dataset_rego_audit")

        owner, _ = _issue_owner("rego_dataset_mismatch")
        self.assertEqual(owner, "benchmark_dataset_rego_audit")

        owner, _ = _issue_owner("a5_env_service_subscription")
        self.assertEqual(owner, "aws_environment")

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

        sample = {
            "prompt": "Run the Lambda everyday at 7 UTC.",
            "intent": 'schedule_expression = "cron(0 7 ** ? *)"',
        }
        result = _intent_literal_eval(
            'resource "aws_cloudwatch_event_rule" "cron" { schedule_expression = "cron(0 7 * * ? *)" }',
            sample,
        )
        self.assertTrue(result["ok"])

    def test_row_code_success_flags_separate_rego_and_environment(self):
        row = {
            "dataset_eval": {
                "required_resource_match": {"ok": True},
                "intent_literal_match": {"ok": True},
            },
            "val": {"ok": True},
            "rego": {"ok": False},
            "deploy": {"ok": True},
            "final_eval": {"end_to_end_strict_ok": False},
        }
        flags = _row_code_success_flags(row)
        self.assertTrue(flags["code_predeploy_ok"])
        self.assertTrue(flags["deployable_code_ok"])
        self.assertTrue(flags["adjusted_code_success_ok"])
        self.assertTrue(flags["benchmark_only_rego_fail"])

        row["deploy"] = {"ok": False}
        row["deploy_attempt_log"] = [{"error_type": "ENV_LIMITATION"}]
        flags = _row_code_success_flags(row)
        self.assertFalse(flags["deployable_code_ok"])
        self.assertTrue(flags["deploy_environment_blocked"])
        self.assertTrue(flags["adjusted_code_success_ok"])

    def test_analyzer_reads_final_eval_code_success_flags(self):
        rows = [
            {"row": 1, "final_eval": {"adjusted_code_success_ok": True}},
            {"row": 2, "final_eval": {"adjusted_code_success_ok": False}},
            {"row": 3, "final_eval": {"adjusted_code_success_ok": True}},
        ]
        self.assertEqual(_final_flag_rows(rows, "adjusted_code_success_ok"), [1, 3])
        self.assertIsNone(_final_flag_rows(rows, "missing_flag"))

    def test_pub_stub_is_realistic_openssh_rsa_key(self):
        pub = _STUB_CONTENT[".pub"]
        self.assertIsInstance(pub, str)
        self.assertTrue(pub.startswith("ssh-rsa AAAA"))
        self.assertIn(" user@example", pub)
        self.assertNotIn("terraformGeneratedBenchmarkKey", pub)

    def test_pdf_stub_is_created_for_local_s3_object_source(self):
        code = '''
        resource "aws_s3_object" "pdf_upload" {
          bucket = "example"
          key    = "test.pdf"
          source = "assets/test.pdf"
        }
        '''
        with tempfile.TemporaryDirectory() as tmp:
            write_terraform_dir(tmp, code)
            pdf = Path(tmp) / "assets" / "test.pdf"
            self.assertTrue(pdf.exists())
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
