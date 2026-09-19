# ── EC2（常駐 compute）─────────────────────────────────────────────────────
# 仕様§2.2：ローカルモデル推論（ruri 310M＋reranker 107M）と in-memory BM25 を抱えるため
# 常駐 EC2（サーバレス不可）。§3.3：t3.xlarge(4vCPU/16GB)・x86・gp3 100GB 暗号化・IMDSv2。

# Canonical 公式 Ubuntu 22.04 LTS(amd64) の最新AMI。conda/torch/fugashi の実績が厚い x86。
# （Graviton t4g への移行は staging で ARM 動作検証後・仕様§3.3。その際は arm64 フィルタへ）。
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# user_data＝薄いブートローダ（awscli 導入→S3 から code tar 取得→展開→bootstrap.sh 起動）。
# 重い処理（conda/モデルDL/データ同期）は code tar 内の bootstrap.sh 側に置く。
locals {
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    aws_region        = var.aws_region
    bucket_name       = aws_s3_bucket.data.bucket
    code_s3_key       = var.code_s3_key
    data_s3_prefix    = var.data_s3_prefix
    project           = var.project
    environment       = var.environment
    enable_web_app    = var.enable_web_app
    collection_name   = var.collection_name # 既定 policy_claims_v7（v7/Qdrant 本線。切り戻しは env で policy_claims_v6）
    vector_backend    = var.vector_backend
    tunnel_host_mcp   = var.tunnel_hostname_mcp
    tunnel_host_web   = var.tunnel_hostname_web
    enable_stats_app  = var.enable_stats_app
    tunnel_host_stats = var.tunnel_hostname_stats
    install_dir       = "/opt/polyarchy"
    hf_home           = "/opt/polyarchy/models"
  })
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  user_data                   = local.user_data
  user_data_replace_on_change = false # 再 apply で無闇に作り直さない（bootstrap は SSM から手動再実行）

  # ルート EBS：gp3 100GB・暗号化ON（仕様§3.3/§4.1）。
  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = true
    tags                  = merge({ Name = "${var.project}-${var.environment}-root" }, var.extra_tags)
  }

  # IMDSv2 必須（SSRF 経由の資格情報窃取を防ぐ）。
  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
  }

  tags = merge({ Name = "${var.project}-${var.environment}-app" }, var.extra_tags)

  # user_data やAMIの差し替えでの再作成を避けたい運用に合わせ、必要ならライフサイクルを調整。
  lifecycle {
    # user_data は初回起動でしか走らない＝in-place 更新は EC2 の停止/起動を伴うだけで箱の中身は変わらない。
    # 差し替え（ドメイン移行・web 廃止 等）は箱側で deploy.env を直して bootstrap 再走行済＝新 user_data は建て直し時に反映（2026-09-03）。
    ignore_changes = [ami, user_data] # 新AMI公開のたびに作り直さない（更新は意図的に）
  }
}
