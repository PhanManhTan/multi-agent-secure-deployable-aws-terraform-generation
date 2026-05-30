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

resource "aws_route53_zone" "example" {
  name = "example-generated-a1b2c3.com"
}

resource "aws_route53_record" "verification_txt" {
  zone_id = aws_route53_zone.example.zone_id
  name    = "example-generated-a1b2c3.com"
  type    = "TXT"
  ttl     = 60
  records = ["passwordpassword"]
}
