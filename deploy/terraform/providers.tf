# AWS プロバイダ。region と profile は変数で切替＝同一定義を staging/prod の別アカウントへ転写する要。
#   staging: -var 'aws_profile=polyarchy-staging'（自分のAWS）
#   prod   : -var 'aws_profile=adopter-prod'（導入団体 AWS・enable_web_app=false）
# profile を空文字にすると既定の資格情報解決（環境変数・EC2/SSO 等）に委ねる。
provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile != "" ? var.aws_profile : null

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Component   = "polyarchy-rag"
    }
  }
}

# アカウントID（S3 バケット名のグローバル一意化などに使う）。
data "aws_caller_identity" "current" {}

# リージョン内の利用可能AZ（public subnet を先頭AZに置く）。
data "aws_availability_zones" "available" {
  state = "available"
}
