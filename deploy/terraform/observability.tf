# ── 運用（監視・アラーム）＝運用設計 §1.1/§1.2（A1 で導入・2026-08-28）─────────
# メトリクスは箱の health スクリプト（bootstrap ⑪・毎分）が namespace=polyarchy へ送る：
#   health{service=mcp|stats|companies|deliberations}＝1/0・disk_used_percent{service=box}・
#   mem_available_percent{service=box}・service_restarts{service=box}（前の分からの自動再起動の回数）。
#   定期実行のユニットの失敗は unit_failed{service=box}（polyarchy-unit-failed@.service）・自動適用は dataapply{service=box}。
#   外からの到達の確認（polyarchy-edgeprobe・5 分ごと）は edge{service=<サービス>}・idp_reachable{service=box}＝1/0。
#   ★箱が送るメトリクスとアラームは 1 対 1（`test_common` が突き合わせる）。
# ログは CW agent がサービスごとのロググループへ転送（保持 30 日＝プライバシーポリシーと一致）。
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

resource "aws_cloudwatch_log_group" "companies" {
  count             = var.enable_companies_app ? 1 : 0
  name              = "polyarchy/companies"
  retention_in_days = 30
  tags              = var.extra_tags
}

resource "aws_cloudwatch_log_group" "deliberations" {
  count             = var.enable_deliberations_app ? 1 : 0
  name              = "polyarchy/deliberations"
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

# 自動適用の失敗（B15 論点1・2026-09-03）：apply は 2 つの場合に dataapply=0 を送る＝
#   (a) 反映後の検証（smoke・/healthz）が FAIL で旧版へ自動復帰した（マーク ROLLED_BACK）
#   (b) 止める前の退避段で中止した（サービスは無停止・マークは書かない＝15 分後に再試行）
# どちらも可用性は保たれている（旧版でサービング中）が、リリースが入っていない事実は人が知る必要がある＝メール。
# 見分けは CW Logs（polyarchy/dataapply）の「切り戻し完了」か「①退避段で失敗」の行（RUNBOOK §5）。
resource "aws_cloudwatch_metric_alarm" "dataapply_rollback" {
  alarm_name          = "${var.project}-${var.environment}-dataapply-rollback"
  alarm_description   = "リリースの自動適用が失敗した＝検証 FAIL で自動切り戻し、または退避段で中止（どちらも旧版で稼働中・ログで見分ける＝RUNBOOK §5）"
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

resource "aws_cloudwatch_metric_alarm" "health_companies" {
  count               = var.enable_companies_app ? 1 : 0
  alarm_name          = "${var.project}-${var.environment}-health-companies"
  alarm_description   = "companies(:8767) の /healthz が 3 分連続で失敗（または無応答）"
  namespace           = "polyarchy"
  metric_name         = "health"
  dimensions          = { service = "companies" }
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

resource "aws_cloudwatch_metric_alarm" "health_deliberations" {
  count               = var.enable_deliberations_app ? 1 : 0
  alarm_name          = "${var.project}-${var.environment}-health-deliberations"
  alarm_description   = "deliberations(:8768) の /healthz が 3 分連続で失敗（または無応答）"
  namespace           = "polyarchy"
  metric_name         = "health"
  dimensions          = { service = "deliberations" }
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

# 箱全体の空きメモリ（MemAvailable／MemTotal）。bootstrap は swap を作らない＝尽きると OOM でプロセスが落ちる。MemAvailable は回収できる
# ファイルキャッシュ（Qdrant が mmap で読んだセグメント等）を空きに数える。重い問の一時的な山では鳴らさない＝15 分続いたとき。
resource "aws_cloudwatch_metric_alarm" "mem_available" {
  alarm_name          = "${var.project}-${var.environment}-mem"
  alarm_description   = "箱の空きメモリが 15 分続けて 10% 未満（OOM の手前）"
  namespace           = "polyarchy"
  metric_name         = "mem_available_percent"
  dimensions          = { service = "box" }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 10
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching" # 生き死には health 側が見る（二重で鳴らさない）
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}

# 常駐のユニットが自分で落ちて systemd に上げ直された回数（NRestarts の増分）。Restart=always は数秒で戻す＝health
# （3 分連続 0）にはかからない。配布の停止・起動（systemctl restart）は数えない。どのユニットかはダッシュボード①。
resource "aws_cloudwatch_metric_alarm" "service_restarts" {
  alarm_name          = "${var.project}-${var.environment}-restarts"
  alarm_description   = "常駐のユニットが落ちて自動で上げ直された（OOM・例外）"
  namespace           = "polyarchy"
  metric_name         = "service_restarts"
  dimensions          = { service = "box" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}

# 定期実行のユニット（捕捉ログの削除・S3 への保護コピー・更新チェック・ダッシュボード・週次レポート）の失敗。
# 捕捉ログの削除が止まると保持 30 日の約束が外れる。どのユニットかはダッシュボード①「失敗したユニット」。
resource "aws_cloudwatch_metric_alarm" "unit_failed" {
  alarm_name          = "${var.project}-${var.environment}-unit-failed"
  alarm_description   = "定期実行のユニットが失敗した（捕捉ログの削除・保護コピー・更新チェック・ダッシュボード・週次レポート）"
  namespace           = "polyarchy"
  metric_name         = "unit_failed"
  dimensions          = { service = "box" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}

# ── ログからの異常検知（メトリクスフィルタ・§2）─────────────────────────────
# 5xx＝httplog の行（"HTTP <method> <path> <status> <ms>ms user=…"）を対象。パターンはサブストリング一致
# （" 502 " 等）＝稀な誤マッチは許容し、閾値側で控えめに拾う。
# 認証の失敗＝認証が出す警告の行の目印（polyarchy_common/access.py の REJECT_INVALID）を対象。アクセスログは認証の内側に
# あり、拒否された要求は httplog の行にならない（2026-10-12 までアクセスログの 401 を数えていて、30 日で 0 件だった）。

locals {
  # 5xx はアプリ異常・認証の失敗の急増は設定壊れ（aud・発行者の食い違い）か IdP の障害（公開鍵が取れない）のシグナル。
  log_filter_groups = merge(
    { mcp = aws_cloudwatch_log_group.mcp.name },
    var.enable_stats_app ? { stats = aws_cloudwatch_log_group.stats[0].name } : {},
    var.enable_companies_app ? { companies = aws_cloudwatch_log_group.companies[0].name } : {},
    var.enable_deliberations_app ? { deliberations = aws_cloudwatch_log_group.deliberations[0].name } : {}
  )
}

# ── 外からの到達の確認（運用設計 §1.1・2026-10-12）────────────────────────────
# 箱自身が公開 URL を資格情報なしで叩いた結果（deploy/bootstrap/edge_probe.sh）。箱の中の監視では見えない、トンネル・DNS・
# エッジの規則・ディスカバリ・設定の食い違いを拾う。2 回続けて 0（約 10 分）で発報＝配布の停止・起動の 1 回では鳴らさない。
# 確認が止まったとき（missing）はここでは鳴らさない＝箱ごとの死は health、確認の失敗は unit-failed が知らせる。
resource "aws_cloudwatch_metric_alarm" "edge" {
  for_each            = local.log_filter_groups
  alarm_name          = "${var.project}-${var.environment}-edge-${each.key}"
  alarm_description   = "${each.key} が公開 URL から届かない・認証の入口が壊れている（/healthz・守るパスの 401・ディスカバリの照合のどれかが 2 回続けて失敗）"
  namespace           = "polyarchy"
  metric_name         = "edge"
  dimensions          = { service = each.key }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}

# IdP（発行者）のディスカバリと公開鍵が取れない。サービスごとに分けない＝IdP の障害でメールをサービスの数だけ出さない。
# 稼働中のサービスは公開鍵を覚えている＝すぐには落ちない（再起動・鍵の入れ替えの後に全員が通らなくなる）。
resource "aws_cloudwatch_metric_alarm" "idp_reachable" {
  alarm_name          = "${var.project}-${var.environment}-idp"
  alarm_description   = "IdP のディスカバリか公開鍵が箱から取れない（2 回続けて失敗）"
  namespace           = "polyarchy"
  metric_name         = "idp_reachable"
  dimensions          = { service = "box" }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
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

# 数えるのは「トークンを出したのに通らない」だけ。トークン無しの 401 は新しい接続の最初の 1 回に必ず出る＝数えない。
resource "aws_cloudwatch_log_metric_filter" "auth_invalid_token" {
  for_each       = local.log_filter_groups
  name           = "polyarchy-${each.key}-auth-invalid-token"
  log_group_name = each.value
  pattern        = "\"auth_reject=invalid_token\""
  metric_transformation {
    name          = "auth_invalid_token_${each.key}"
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

# 閾値の根拠＝staging の 30 日（2026-09-10〜10-10）でこの行はサービスごとに 3 件（IdP の環境を切り替えた直後の数秒）だけ。
# 利用が少ない時間帯の設定壊れは件数が届かない＝箱の側の食い違いは外からの到達の確認（上の edge）が拾う。
resource "aws_cloudwatch_metric_alarm" "auth_invalid_token" {
  for_each            = local.log_filter_groups
  alarm_name          = "${var.project}-${var.environment}-auth-${each.key}"
  alarm_description   = "${each.key} でトークンの検証の失敗が 5 分で 10 件以上（設定壊れ＝aud・発行者の食い違い or IdP の障害）"
  namespace           = "polyarchy"
  metric_name         = "auth_invalid_token_${each.key}"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.extra_tags
}
