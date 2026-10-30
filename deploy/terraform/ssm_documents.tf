# 手動キック用の SSM ドキュメント（運用設計 §1.4・2026-09-03）。
# AWS コンソール（Systems Manager → Run Command）から「実行」を押すだけで更新チェックが走る＝
# S3 静的ダッシュボードに置けない「ボタン」の代替。実行できる人は IAM でこのドキュメントに限定可能。
# チェックのみ＝取込（書き込み）は含まない。実行記録は CloudTrail に残る（監査ガイド整合）。
# 出力はこの実行の分を全部（B48・2026-10-30）＝冒頭の警告（⚠ ESTAT_APP_ID なし・⚠ CRAWLER_CONTACT なし）が末尾 20 行に
# 入らず「出ていない」と読み違えるおそれがあった。更新チェックの出力は journal だけ（CW Logs へは送らない）。
resource "aws_ssm_document" "check_updates" {
  name            = "${var.project}-${var.environment}-check-updates"
  document_type   = "Command"
  document_format = "YAML"
  content         = <<-DOC
    schemaVersion: "2.2"
    description: "Polyarchy update check (freshness + new-docs -> dashboard). Read-only check; no ingest."
    mainSteps:
      - action: aws:runShellScript
        name: runUpdateCheck
        inputs:
          timeoutSeconds: "1800"
          runCommand:
            - T0="$(date '+%Y-%m-%d %H:%M:%S')"
            - systemctl start polyarchy-updatecheck.service
            - sleep 2
            - journalctl -u polyarchy-updatecheck --since "$T0" --no-pager
  DOC
  tags = var.extra_tags
}
