# Terraform / プロバイダのバージョン固定。
# 二段構え（staging=自分AWS → prod=導入団体 AWS）で「同じ定義を別アカウントに転写」するため、
# バージョンは固定して環境間の挙動差を消す（deploy/README.md §0）。
terraform {
  required_version = ">= 1.10.0" # use_lockfile（S3 の native のロック）

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # ── state backend（B46 (c)・2026-10-19＝local から S3 へ）──────────────────────
  # state は S3（環境ごとに別のバケット・別のキー）。backend は変数を使えない＝値は deploy/scripts/lib.sh の tf_init が
  # -backend-config で渡す（bucket＝<project>-tfstate-<environment>-<アカウントID>・key＝<project>/<environment>/terraform.tfstate・
  # encrypt・use_lockfile＝S3 の native のロック〔TF>=1.10〕）。バケットは lib.sh の ensure_state_bucket が terraform の外で作る。
  # ★terraform を直に init しない（値が渡らない）＝deploy.sh を通す。local の state を持つフォルダは 1 回だけ
  #   `bash deploy.sh <env> state-migrate` で移す（PROD_MIGRATION §4）。
  backend "s3" {}
}
