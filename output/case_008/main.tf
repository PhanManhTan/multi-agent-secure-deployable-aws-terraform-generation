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

data "aws_vpc" "selected" {
  default = true
}

resource "aws_egress_only_internet_gateway" "pike" {
  vpc_id = data.aws_vpc.selected.id
  tags = {
    permissions = "true"
  }
}
