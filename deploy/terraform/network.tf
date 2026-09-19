# ── ネットワーク ─────────────────────────────────────────────────────────────
# Cloudflare Tunnel が「箱→外」への outbound 接続で入口を張るため、inbound は一切開けない
# （ロードマップ§2.1「public subnet 1つ・SG は egress のみ・管理は SSM Session Manager」）。
# public subnet＋IGW＋public IP で egress を確保（NAT Gateway を置かずコストを抑える）。
# 管理は SSM Session Manager 経由＝22/SSH を開けない（キー漏洩・踏み台の面を無くす）。

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge({ Name = "${var.project}-${var.environment}-vpc" }, var.extra_tags)
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge({ Name = "${var.project}-${var.environment}-igw" }, var.extra_tags)
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true # egress 用の public IP（inbound は SG で全閉）
  tags                    = merge({ Name = "${var.project}-${var.environment}-public" }, var.extra_tags)
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = merge({ Name = "${var.project}-${var.environment}-public-rt" }, var.extra_tags)
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# セキュリティグループ：inbound 全閉／egress のみ許可。
#   - inbound: ルール無し＝全拒否（Tunnel は outbound で張るので公開ポート不要）。
#   - egress : 全許可。理由＝HFモデルDL(443)・apt/conda(80/443)・DNS(53)・SSM(443)・S3(443)・
#              cloudflared のエッジ接続(7844/QUIC・443 fallback) を一括で通すため。
#     必要ならここを 443/80/53/7844 に絞れる（cloudflared の 7844 を落とすとトンネルが張れない点に注意）。
resource "aws_security_group" "instance" {
  name = "${var.project}-${var.environment}-sg"
  # AWS の SG description は ASCII 限定（^[0-9A-Za-z_ .:/()#,@\[\]+=&;{}!$*-]*$）なので英語で書く。
  # ＝inbound 全閉・egress のみ（Cloudflare Tunnel は outbound・管理は SSM）。
  description = "inbound closed; egress only (Cloudflare Tunnel outbound; managed via SSM)"
  vpc_id      = aws_vpc.main.id

  egress {
    # ＝全 egress 許可（モデルDL/パッケージ/SSM/S3/cloudflared 7844）。ASCII 限定ゆえ英語。
    description = "all egress (model download, packages, SSM, S3, cloudflared 7844)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge({ Name = "${var.project}-${var.environment}-sg" }, var.extra_tags)
}
