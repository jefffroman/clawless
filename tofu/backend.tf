terraform {
  backend "s3" {
    key          = "clawless/terraform.tfstate"
    use_lockfile = true # S3 native locking — no DynamoDB needed (OpenTofu 1.10+)
    encrypt      = true
  }
}
