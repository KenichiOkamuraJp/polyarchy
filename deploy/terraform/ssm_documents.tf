# 手動キック用の SSM ドキュメント（運用設計 §1.4・2026-09-03）。
# AWS コンソール（Systems Manager → Run Command）から「実行」を押すだけで更新チェックが走る＝
# S3 静的ダッシュボードに置けない「ボタン」の代替。実行できる人は IAM でこのドキュメントに限定可能。
# チェックのみ＝取込（書き込み）は含まない。実行記録は CloudTrail に残る（監査ガイド整合）。
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
            - systemctl start polyarchy-updatecheck.service
            - sleep 2
            - journalctl -u polyarchy-updatecheck -n 20 --no-pager
  DOC
  tags = var.extra_tags
}
