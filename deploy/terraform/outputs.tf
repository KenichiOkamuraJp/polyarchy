# ── 出力 ─────────────────────────────────────────────────────────────────────
output "instance_id" {
  description = "EC2 インスタンスID。SSM Session Manager の接続先。"
  value       = aws_instance.app.id
}

output "s3_bucket" {
  description = "コード配布・データ・捕捉ログ・snapshot のバケット名。"
  value       = aws_s3_bucket.data.bucket
}

output "ssm_start_session" {
  description = "箱に入る（SSH は開けていない・SSM 経由）。"
  value       = "aws ssm start-session --region ${var.aws_region} --target ${aws_instance.app.id}"
}

output "tail_bootstrap_log" {
  description = "ブートストラップ進捗の追跡（SSM で入ってから）。"
  value       = "sudo tail -f /var/log/polyarchy-bootstrap.log /var/log/cloud-init-output.log"
}

output "next_steps" {
  description = "apply 後の手順（詳細は deploy/README.md）。"
  value = join("\n", [
    "1) 秘密登録: SSM Parameter Store に cloudflared 資格情報（＋staging は anthropic_api_key）を SecureString で。",
    "2) コード/データ投入: scripts/upload_to_s3.sh でこのバケットへ push（apply 前でも可）。",
    "3) 監視: ${aws_instance.app.id} に SSM で入り /var/log/polyarchy-bootstrap.log を追う。",
    "4) 回帰ゲート4種を箱の上で PASS（README §6）。",
    "5) 入口を開く: 箱で cloudflared を起動 → curl で /healthz 200・未認証 401 を確認（README §7・PROD_MIGRATION §5.3）。",
  ])
}
