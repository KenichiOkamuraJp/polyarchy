# S3 データイベントの監査証跡（監査ガイド §0・2026-09-03）。
# 目的＝データバケットへの書き込み（release.sh の upload・マニフェスト）を「誰が・いつ・何を」で記録する。
# CloudTrail の既定（管理イベント 90 日）は S3 オブジェクト操作を含まないため、専用トレイルで data events を有効化。
resource "aws_s3_bucket" "trail" {
  bucket = "${var.project}-${var.environment}-trail-${data.aws_caller_identity.current.account_id}"
  tags   = var.extra_tags
}

resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    id     = "trail-retention-400d"
    status = "Enabled"
    filter {}
    expiration { days = 400 }
  }
}

resource "aws_s3_bucket_policy" "trail" {
  bucket = aws_s3_bucket.trail.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      { Sid = "AWSCloudTrailAclCheck", Effect = "Allow",
        Principal = { Service = "cloudtrail.amazonaws.com" },
        Action = "s3:GetBucketAcl", Resource = aws_s3_bucket.trail.arn },
      { Sid = "AWSCloudTrailWrite", Effect = "Allow",
        Principal = { Service = "cloudtrail.amazonaws.com" },
        Action = "s3:PutObject",
        Resource = "${aws_s3_bucket.trail.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*",
        Condition = { StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" } } }
    ]
  })
}

resource "aws_cloudtrail" "s3_data" {
  name                       = "${var.project}-${var.environment}-s3-data"
  s3_bucket_name             = aws_s3_bucket.trail.id
  include_global_service_events = false
  event_selector {
    read_write_type           = "WriteOnly"
    include_management_events = false
    data_resource {
      type   = "AWS::S3::Object"
      values = ["${aws_s3_bucket.data.arn}/"]
    }
  }
  depends_on = [aws_s3_bucket_policy.trail]
  tags       = var.extra_tags
}
