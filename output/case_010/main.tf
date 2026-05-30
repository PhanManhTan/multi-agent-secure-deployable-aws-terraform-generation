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

data "aws_iam_policy_document" "firehose_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["firehose.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "firehose_splunk_policy" {
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.firehose_backup.arn}/*"]
  }
}

resource "aws_s3_bucket" "firehose_backup" {
  bucket_prefix = "firehose-splunk-backup-"
}

resource "aws_iam_role" "firehose_splunk_role" {
  name_prefix         = "firehose-splunk-role-"
  assume_role_policy  = data.aws_iam_policy_document.firehose_assume_role.json
}

resource "aws_iam_policy" "firehose_splunk_policy" {
  name_prefix = "firehose-splunk-policy-"
  policy      = data.aws_iam_policy_document.firehose_splunk_policy.json
}

resource "aws_iam_role_policy_attachment" "firehose_splunk_role_attachment" {
  role       = aws_iam_role.firehose_splunk_role.name
  policy_arn = aws_iam_policy.firehose_splunk_policy.arn
}

resource "aws_kinesis_firehose_delivery_stream" "splunk_stream" {
  name        = "splunk-firehose-stream"
  destination = "splunk"

  splunk_configuration {
    hec_endpoint                 = "https://example.splunkcloud.com:8088"
    hec_token                    = "EXAMPLE-TOKEN"
    hec_acknowledgment_timeout   = 600
    s3_backup_mode               = "FailedEventsOnly"

    s3_configuration {
      bucket_arn        = aws_s3_bucket.firehose_backup.arn
      buffering_size    = 5
      buffering_interval = 300
      role_arn          = aws_iam_role.firehose_splunk_role.arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "firehose_backup_public_access" {
  bucket = aws_s3_bucket.firehose_backup.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "firehose_backup_encryption" {
  bucket = aws_s3_bucket.firehose_backup.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

