# ── S3（コード配布・データ・捕捉ログ＝「燃料」・eval・snapshot）─────────────────
# 仕様§3.3/§4.1：versioning ON（捕捉ログの保護）＋暗号化＋public access 全ブロック。
# bucket 名はグローバル一意が要るので project-environment-accountid で衝突を避ける。

locals {
  bucket_name = "${var.project}-${var.environment}-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "data" {
  bucket = local.bucket_name
  tags   = merge({ Name = local.bucket_name }, var.extra_tags)
}

# versioning ON＝捕捉ログ（燃料）の誤上書き/削除からの保護（仕様§4.1）。
resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

# 既定の暗号化（SSE-S3/AES256）。KMS が要件になれば aws:kms へ差し替え。
resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# public access は全ブロック（データは EC2 の IAM role からのみ読む）。
resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# 旧バージョンの整理（燃料の直近は残しつつ、古い版のコストを抑える）。運用に合わせて調整。
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # ★捕捉ログ（クエリログ）だけは保持30日で自動削除（プライバシーポリシーの約束）。
  # 箱側は systemd polyarchy-logprune.timer が JSONL の該当行を毎日削除し、S3 側は本ルールで
  # オブジェクト自体を失効させる（versioning ON のため旧版も 30 日で消す）。両者で日数を揃えること。
  rule {
    id     = "query-log-retention-30d"
    status = "Enabled"
    filter {
      prefix = "${var.data_s3_prefix}/query_log/"
    }
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
  # stats（統計参照DB）の捕捉ログも同じ 30 日（polyarchy-logprune.service の stats 行と揃える）。
  rule {
    id     = "stats-query-log-retention-30d"
    status = "Enabled"
    filter {
      prefix = "${var.data_s3_prefix}/stats/query_log/"
    }
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
  # companies（企業情報DB）の捕捉ログも同じ 30 日（polyarchy-logprune.service の companies 行と揃える）。
  rule {
    id     = "companies-query-log-retention-30d"
    status = "Enabled"
    filter {
      prefix = "${var.data_s3_prefix}/companies/query_log/"
    }
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}
