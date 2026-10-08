# ── S3（コード配布・データ・捕捉ログ＝「燃料」・eval・snapshot）─────────────────
# 仕様§3.3/§4.1：versioning ON（捕捉ログの保護）＋暗号化＋public access 全ブロック。
# bucket 名はグローバル一意が要るので project-environment-accountid で衝突を避ける。

locals {
  bucket_name = "${var.project}-${var.environment}-${data.aws_caller_identity.current.account_id}"
  # 捕捉ログを持つサービス（S3 の data/ の後ろの prefix）。rule の id は既存のまま（順も既存のまま）。
  query_log_rules = [
    { id = "query-log-retention-30d", prefix = "query_log/" },
    { id = "stats-query-log-retention-30d", prefix = "stats/query_log/" },
    { id = "companies-query-log-retention-30d", prefix = "companies/query_log/" },
    { id = "deliberations-query-log-retention-30d", prefix = "deliberations/query_log/" },
  ]
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

  # ★捕捉ログ（クエリログ）だけは保持30日で自動削除（プライバシーポリシーの約束）。正典＝deploy/README.md「捕捉ログの保持」。
  # 箱側は systemd polyarchy-logprune.timer が JSONL の記録を ts で 30 日を超えた分だけ毎日削除し、
  # fuelsync が毎時その JSONL（サービスごとに固定のキー 1 本）を S3 へ上書きする。
  # ★時計が 2 つある：箱は「記録の年齢」、S3 は「版の年齢」。versioning ON で毎時上書きされるので
  #   現行版は常に若く expiration は実質発火せず、効くのは「非現行になってからの日数」の方。
  #   30 日目の記録を含む最後の版が非現行になるのは箱の prune の後＝S3 に残る最長は
  #   「箱 30 日＋非現行の日数＋ライフサイクルの非同期の遅れ」。非現行の日数を 30 にすると約 60 日残る（2026-10-09 是正）。
  #   ＝非現行は 1 日（箱の EBS が原本・S3 は保護コピー＝fuelsync.sh）。箱の日数と同じ値に「揃えない」こと。
  # サービスの一覧は fuelsync.sh の sync_one・polyarchy-logprune.service・iam.tf の書込 prefix と同じ（test_common が突き合わせる）。
  dynamic "rule" {
    for_each = local.query_log_rules
    content {
      id     = rule.value.id
      status = "Enabled"
      filter {
        prefix = "${var.data_s3_prefix}/${rule.value.prefix}"
      }
      expiration {
        days = 30
      }
      noncurrent_version_expiration {
        noncurrent_days = 1
      }
    }
  }
}
