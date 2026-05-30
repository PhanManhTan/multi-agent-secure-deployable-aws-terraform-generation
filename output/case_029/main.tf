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

resource "aws_efs_file_system" "example" {
  creation_token = "example-efs"
  encrypted = true

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }
  kms_key_id = aws_kms_key.generated_hardening.arn
}

resource "aws_kms_key" "generated_hardening" {
  description             = "KMS key for generated benchmark resource encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

