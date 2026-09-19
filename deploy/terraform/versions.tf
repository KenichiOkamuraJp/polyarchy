# Terraform / プロバイダのバージョン固定。
# 二段構え（staging=自分AWS → prod=導入団体 AWS）で「同じ定義を別アカウントに転写」するため、
# バージョンは固定して環境間の挙動差を消す（deploy/README.md §0）。
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # ── state backend ─────────────────────────────────────────────────────────
  # 既定は local state（「最小 Terraform」の方針）。まず staging を local で
  # 立ち上げ、prod 昇格・引き継ぎ整備の段で S3 backend へ移す（下記コメントを有効化して
  # `terraform init -migrate-state`）。backend は変数展開不可なので値は直書きになる点に注意。
  #
  # backend "s3" {
  #   bucket       = "polyarchy-tfstate-<ACCOUNT_ID>"   # 事前に手動作成（versioning ON 推奨）
  #   key          = "polyarchy/staging/terraform.tfstate"
  #   region       = "ap-northeast-1"
  #   encrypt      = true
  #   use_lockfile = true                                # S3 native ロック（DynamoDB 不要・TF>=1.10）
  # }
}
