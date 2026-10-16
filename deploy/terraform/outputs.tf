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

# 中継の口（B44 第 2 便）＝中継のクライアント（bot）を同じ VPC に置くときに要る値。口を開くかは env の RELAY_SOURCE_CIDRS（PROD_MIGRATION §2.6）。
output "relay_vpc_id" {
  description = "中継のクライアントを置く VPC（箱と同じ・CIDR 10.0.0.0/16）。"
  value       = aws_vpc.main.id
}

output "relay_subnet_id" {
  description = "箱のサブネット（10.0.1.0/24）。中継のクライアントを同じサブネットに置くなら RELAY_SOURCE_CIDRS はその IP の /32 か 10.0.1.0/24。"
  value       = aws_subnet.public.id
}

output "relay_private_ip" {
  description = "中継のクライアントが呼ぶ箱の IP（http://<この IP>:8865〜8868/<各サービスのパス>）。"
  value       = aws_instance.app.private_ip
}

output "next_steps" {
  description = "apply 後の手順（詳細は deploy/README.md）。"
  value = join("\n", [
    "1) 監視: ${aws_instance.app.id} に SSM で入り /var/log/polyarchy-bootstrap.log を「完了」まで追う（PROD_MIGRATION §5.1）。",
    "2) 箱の上の smoke（PROD_MIGRATION §5.2）。検索の質のゲートは手元で（release.sh・README「品質の担保」）。",
    "3) 入口を開く: 箱で cloudflared を起動 → curl で /healthz 200・未認証 401 を確認（PROD_MIGRATION §5.3）。",
    "4) 以後のデータとコードの更新は release.sh だけ（PROD_MIGRATION §6.3・RUNBOOK_OPS §5）。",
  ])
}
