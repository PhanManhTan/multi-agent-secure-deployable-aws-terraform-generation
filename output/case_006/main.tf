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

resource "aws_iam_role" "codebuild_role" {
  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "codebuild.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
  description        = "CodeBuild service role"
}

resource "aws_iam_role_policy" "codebuild_policy" {
  role   = aws_iam_role.codebuild_role.name
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [
      {
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:log-group:/aws/codebuild/*"
        Effect   = "Allow"
      },
      {
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.artifact_bucket.arn}/*",
          "${aws_s3_bucket.secondary_source_bucket.arn}/*",
          "${aws_s3_bucket.secondary_artifact_bucket.arn}/*"
        ]
        Effect   = "Allow"
      }
    ]
  })
}

resource "aws_s3_bucket" "artifact_bucket" {
  bucket_prefix = "codebuild-artifact-"
}

resource "aws_s3_bucket" "secondary_source_bucket" {
  bucket_prefix = "cb-secondary-source-"
}

resource "aws_s3_bucket" "secondary_artifact_bucket" {
  bucket_prefix = "cb-secondary-artifact-"
}

resource "aws_codebuild_project" "example" {
  name         = "example-project"
  description  = "Example CodeBuild project with secondary sources and artifacts"
  service_role = aws_iam_role.codebuild_role.arn

  source {
    type      = "NO_SOURCE"
    buildspec = <<-EOF
    version: 0.2
    phases:
      build:
        commands:
          - echo "Hello, CodeBuild!"
    EOF
  }

  artifacts {
    type           = "S3"
    location       = aws_s3_bucket.artifact_bucket.bucket
    namespace_type = "NONE"
    packaging      = "ZIP"
  }

  secondary_sources {
    source_identifier = "secondary_source_1"
    type              = "S3"
    location          = "${aws_s3_bucket.secondary_source_bucket.bucket}/source.zip"
  }

  secondary_artifacts {
    type                = "S3"
    location            = aws_s3_bucket.secondary_artifact_bucket.bucket
    artifact_identifier = "secondary_artifact_1"
    name                = "secondary-artifact.zip"
    namespace_type      = "NONE"
    packaging           = "ZIP"
  }

  environment {
    compute_type = "BUILD_GENERAL1_SMALL"
    image        = "aws/codebuild/amazonlinux2-x86_64-standard:4.0"
    type         = "LINUX_CONTAINER"

    environment_variable {
      name  = "ENV_VAR1"
      value = "value1"
    }

    environment_variable {
      name  = "ENV_VAR2"
      value = "value2"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifact_bucket_public_access" {
  bucket = aws_s3_bucket.artifact_bucket.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifact_bucket_encryption" {
  bucket = aws_s3_bucket.artifact_bucket.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "secondary_artifact_bucket_public_access" {
  bucket = aws_s3_bucket.secondary_artifact_bucket.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "secondary_artifact_bucket_encryption" {
  bucket = aws_s3_bucket.secondary_artifact_bucket.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "secondary_source_bucket_public_access" {
  bucket = aws_s3_bucket.secondary_source_bucket.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "secondary_source_bucket_encryption" {
  bucket = aws_s3_bucket.secondary_source_bucket.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

