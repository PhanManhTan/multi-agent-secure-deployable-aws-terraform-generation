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

resource "aws_elasticache_user" "example" {
  user_id       = "example-user-id"
  user_name     = "example-user"
  access_string = "on ~* +@all"
  engine        = "redis"

  authentication_mode {
    type      = "password"
    passwords = ["password123456789"]
  }
}
