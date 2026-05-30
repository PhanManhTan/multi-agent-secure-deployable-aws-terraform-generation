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

resource "aws_elasticache_user" "auth_user" {
  user_id = "auth-user-${random_id.elasticache_user_suffix.hex}"
  user_name = "auth-user-${random_id.elasticache_user_suffix.hex}"
  engine        = "REDIS"
  access_string = "on ~* +@all"

  authentication_mode {
    type      = "password"
    passwords = ["password1password1", "password2password2"]
  }
}

resource "random_id" "elasticache_user_suffix" {
  byte_length = 4
}

