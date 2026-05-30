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

resource "aws_lightsail_instance" "example" {
  name              = "example-instance"
  blueprint_id      = "amazon_linux_2"
  bundle_id         = "nano_2_0"
  availability_zone = "ap-southeast-1a"
}

resource "aws_lightsail_disk" "example" {
  name              = "example-disk"
  size_in_gb        = 8
  availability_zone = aws_lightsail_instance.example.availability_zone
}

resource "aws_lightsail_disk_attachment" "example" {
  disk_name     = aws_lightsail_disk.example.name
  instance_name = aws_lightsail_instance.example.name
  disk_path     = "/dev/xvdf"
}
