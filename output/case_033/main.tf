terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = "ap-southeast-1"
}

data "aws_iam_policy_document" "lambda_assume_role_upload_cat" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "archive_file" "upload_cat_lambda_zip" {
  type        = "zip"
  source_file = "upload_cat.js"
  output_path = "upload_cat.zip"
}

data "aws_iam_policy_document" "lambda_assume_role_random_cat" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "archive_file" "random_cat_lambda_zip" {
  type        = "zip"
  source_file = "random_cat.js"
  output_path = "random_cat.zip"
}

resource "aws_dynamodb_table" "cats" {
  name         = "cat-pictures"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  deletion_protection_enabled = true

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.generated_hardening.arn
  }
}

resource "aws_s3_bucket" "cat_pictures" {
  bucket_prefix = "cat-pictures-"
}

resource "aws_api_gateway_rest_api" "cat_api" {
  name = "cat-api"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "execute-api:Invoke"
      Resource  = "*"
    }]
  })
}

resource "aws_api_gateway_resource" "cats_2" {
  rest_api_id = aws_api_gateway_rest_api.cat_api.id
  parent_id   = aws_api_gateway_rest_api.cat_api.root_resource_id
  path_part   = "cats"
}

resource "aws_iam_role" "upload_cat_lambda_role" {
  name_prefix        = "upload_cat-lambda-"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role_upload_cat.json
}

resource "aws_lambda_function" "upload_cat_lambda" {
  function_name     = "upload_cat-lambda"
  role              = aws_iam_role.upload_cat_lambda_role.arn
  handler           = "upload_cat.handler"
  runtime           = "nodejs18.x"
  filename          = data.archive_file.upload_cat_lambda_zip.output_path
  source_code_hash  = data.archive_file.upload_cat_lambda_zip.output_base64sha256

  tracing_config {
    mode = "Active"
  }
}

resource "aws_api_gateway_method" "upload_cat_method" {
  rest_api_id   = aws_api_gateway_rest_api.cat_api.id
  resource_id   = aws_api_gateway_resource.cats_2.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "upload_cat_integration" {
  rest_api_id             = aws_api_gateway_rest_api.cat_api.id
  resource_id             = aws_api_gateway_resource.cats_2.id
  http_method             = aws_api_gateway_method.upload_cat_method.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.upload_cat_lambda.invoke_arn
}

resource "aws_lambda_permission" "upload_cat_api_permission" {
  statement_id  = "AllowExecutionFromAPIGatewayUpload_Cat"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.upload_cat_lambda.function_name
  principal     = "apigateway.amazonaws.com"
}

resource "aws_iam_role" "random_cat_lambda_role" {
  name_prefix        = "random_cat-lambda-"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role_random_cat.json
}

resource "aws_lambda_function" "random_cat_lambda" {
  function_name     = "random_cat-lambda"
  role              = aws_iam_role.random_cat_lambda_role.arn
  handler           = "random_cat.handler"
  runtime           = "nodejs18.x"
  filename          = data.archive_file.random_cat_lambda_zip.output_path
  source_code_hash  = data.archive_file.random_cat_lambda_zip.output_base64sha256

  tracing_config {
    mode = "Active"
  }
}

resource "aws_api_gateway_method" "random_cat_method" {
  rest_api_id   = aws_api_gateway_rest_api.cat_api.id
  resource_id   = aws_api_gateway_resource.cats_2.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "random_cat_integration" {
  rest_api_id             = aws_api_gateway_rest_api.cat_api.id
  resource_id             = aws_api_gateway_resource.cats_2.id
  http_method             = aws_api_gateway_method.random_cat_method.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.random_cat_lambda.invoke_arn
}

resource "aws_lambda_permission" "random_cat_api_permission" {
  statement_id  = "AllowExecutionFromAPIGatewayRandom_Cat"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.random_cat_lambda.function_name
  principal     = "apigateway.amazonaws.com"
}

resource "aws_api_gateway_deployment" "cat_api_deployment" {
  rest_api_id = aws_api_gateway_rest_api.cat_api.id

  depends_on = [
    aws_api_gateway_integration.upload_cat_integration,
    aws_api_gateway_integration.random_cat_integration,
  ]
}

resource "aws_api_gateway_stage" "cat_api_stage" {
  rest_api_id   = aws_api_gateway_rest_api.cat_api.id
  deployment_id = aws_api_gateway_deployment.cat_api_deployment.id
  stage_name    = "prod"

  xray_tracing_enabled = true

  cache_cluster_enabled = true
  cache_cluster_size    = "0.5"
}

resource "aws_kms_key" "generated_hardening" {
  description             = "KMS key for generated benchmark resource encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_s3_bucket_public_access_block" "cat_pictures_public_access" {
  bucket = aws_s3_bucket.cat_pictures.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cat_pictures_encryption" {
  bucket = aws_s3_bucket.cat_pictures.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "cat_pictures_versioning" {
  bucket = aws_s3_bucket.cat_pictures.id

  versioning_configuration {
    status = "Enabled"
  }
}

