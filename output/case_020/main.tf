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

resource "aws_dynamodb_table" "example_ttl_table" {
  name         = "example-ttl-table"
  hash_key     = "id"
  billing_mode = "PAY_PER_REQUEST"
  deletion_protection_enabled = true

  attribute {
    name = "id"
    type = "S"
  }

  ttl {
    attribute_name = "custom_ttl_attribute"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.generated_hardening.arn
  }
}

resource "aws_kms_key" "generated_hardening" {
  description             = "KMS key for generated benchmark resource encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

