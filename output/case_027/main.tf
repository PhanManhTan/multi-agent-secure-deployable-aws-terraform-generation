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

resource "random_pet" "this" {
  length = 2
}

resource "aws_lightsail_instance" "wordpress" {
  name              = random_pet.this.id
  availability_zone = "ap-southeast-1a"
  blueprint_id      = "wordpress"
  bundle_id         = "nano_2_0"
}
