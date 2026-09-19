# ── 運用（監視・アラーム）＝運用設計 §1.1/§1.2（A1 で導入・2026-08-28）─────────
# メトリクスは箱の health スクリプト（bootstrap ⑪・毎分）が namespace=polyarchy へ送る：
#   health{service=mcp|stats}＝1/0・disk_used_percent{service=box}。
# ログは CW agent が polyarchy/mcp・polyarchy/stats へ転送（保持 30 日＝プライバシーポリシーと一致）。
# 通知は SNS → メール（var.alert_email。購読はメール側の Confirm リンクで有効化される）。

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-${var.environment}-alerts"
  tags = var.extra_tags
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ロググループは terraform が先に作る（保持 30 日を単一の真実に。CW agent 側の retention 指定と同値）。
resource "aws_cloudwatch_log_group" "mcp" {
  name              = "polyarchy/mcp"
  retention_in_days = 30
  tags              = var.extra_tags
}

resource "aws_cloudwatch_log_group" "stats" {
  count             = var.enable_stats_app ? 1 : 0
  name              = "polyarchy/stats"
  retention_in_days = 30
  tags              = var.extra_tags
}

# 自動適用（dataapply）の全出力＝データ更新の監査線（監査ガイド §4b）。箱操作ゼロで「何がいつ適用/切り戻しされたか」を追える。
resource "aws_cloudwatch_log_group" "dataapply" {
  name              = "polyarchy/dataapply"
  retention_in_days = 30
  tags              = var.extra_tags
}

# ── アラーム（閾値は控えめに始める＝誤報でメールが無視されるのが最悪・§2）──────────

resource "aws_cloudwatch_metric_alarm" "health_mcp" {
  alarm_name          = "${var.project}-${var.environment}-health-mcp"
  alarm_description   = "recommendations(:8765) の /healthz が 3 分連続で失敗（または無応答）"
  namespace           = "polyarchy"
  metric_name         = "health"
  dimensions          = { service = "mcp" }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching" # timer 自体の死（箱ごと死んだ等）も検知する
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}

# 自動適用の切り戻し（B15 論点1・2026-09-03）：apply が smoke FAIL で旧版へ自動復帰したとき dataapply=0 を送る。
# 可用性は保たれている（旧版でサービング中）が、リリースが不採用になった事実は人が知る必要がある＝メール。
resource "aws_cloudwatch_metric_alarm" "dataapply_rollback" {
  alarm_name          = "${var.project}-${var.environment}-dataapply-rollback"
  alarm_description   = "リリースの自動適用が smoke FAIL で自動切り戻しされた（旧版で稼働中・RUNBOOK §5「切り戻し後」）"
  namespace           = "polyarchy"
  metric_name         = "dataapply"
  dimensions          = { service = "box" }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching" # 適用が無い期間は正常（値は適用時にしか送られない）
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}

resource "aws_cloudwatch_metric_alarm" "health_stats" {
  count               = var.enable_stats_app ? 1 : 0
  alarm_name          = "${var.project}-${var.environment}-health-stats"
  alarm_description   = "stats(:8766) の /healthz が 3 分連続で失敗（または無応答）"
  namespace           = "polyarchy"
  metric_name         = "health"
  dimensions          = { service = "stats" }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}

resource "aws_cloudwatch_metric_alarm" "ec2_status" {
  alarm_name          = "${var.project}-${var.environment}-ec2-status"
  alarm_description   = "EC2 StatusCheckFailed（ハード/OS 異常）"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.app.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}

resource "aws_cloudwatch_metric_alarm" "disk" {
  alarm_name          = "${var.project}-${var.environment}-disk"
  alarm_description   = "ルートディスク使用率 80% 超（gp3 100GB・データ増か捕捉ログ肥大を疑う）"
  namespace           = "polyarchy"
  metric_name         = "disk_used_percent"
  dimensions          = { service = "box" }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching" # 生き死には health 側が見る（二重で鳴らさない）
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}

# ── ログからの異常検知（メトリクスフィルタ・§2）─────────────────────────────
# httplog の行（"HTTP <method> <path> <status> <ms>ms user=…"）を対象。パターンはサブストリング一致
# （" 502 " 等）＝稀な誤マッチは許容し、閾値側で控えめに拾う。

locals {
  # 5xx はアプリ異常・401 急増は Access 検証失敗（攻撃 or 設定壊れ）のシグナル。
  log_filter_groups = merge(
    { mcp = aws_cloudwatch_log_group.mcp.name },
    var.enable_stats_app ? { stats = aws_cloudwatch_log_group.stats[0].name } : {}
  )
}

resource "aws_cloudwatch_log_metric_filter" "http_5xx" {
  for_each       = local.log_filter_groups
  name           = "polyarchy-${each.key}-5xx"
  log_group_name = each.value
  pattern        = "?\" 500 \" ?\" 502 \" ?\" 503 \" ?\" 504 \""
  metric_transformation {
    name          = "http_5xx_${each.key}"
    namespace     = "polyarchy"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "http_401" {
  for_each       = local.log_filter_groups
  name           = "polyarchy-${each.key}-401"
  log_group_name = each.value
  pattern        = "\" 401 \""
  metric_transformation {
    name          = "http_401_${each.key}"
    namespace     = "polyarchy"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "http_5xx" {
  for_each            = local.log_filter_groups
  alarm_name          = "${var.project}-${var.environment}-5xx-${each.key}"
  alarm_description   = "${each.key} の 5xx が 5 分で 5 件超（アプリ異常）"
  namespace           = "polyarchy"
  metric_name         = "http_5xx_${each.key}"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}

resource "aws_cloudwatch_metric_alarm" "http_401" {
  for_each            = local.log_filter_groups
  alarm_name          = "${var.project}-${var.environment}-401-${each.key}"
  alarm_description   = "${each.key} の 401 が 5 分で 30 件超（Access 検証失敗の急増＝攻撃 or 設定壊れ）"
  namespace           = "polyarchy"
  metric_name         = "http_401_${each.key}"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 30
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}
