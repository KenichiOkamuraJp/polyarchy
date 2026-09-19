# ── IAM（EC2 instance role・最小権限）───────────────────────────────────────
# ロードマップ§2.1：S3（配布物は読取のみ・書込は箱の成果物だけ）・SSM 読取・CloudWatch の最小権限。SSH を開けない代わりに
# SSM Session Manager で入るため AmazonSSMManagedInstanceCore を付ける。
# 秘密は .env で運ばず SSM Parameter Store（SecureString）から読む（仕様§3.3/§4.1）＝
#   staging: /polyarchy/staging/anthropic_api_key（web 生成用）＋ cloudflared 資格情報
#   prod   : Anthropic 無し（MCPのみ＝ランタイム秘密ゼロ）＋ cloudflared 資格情報のみ

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.project}-${var.environment}-ec2"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.extra_tags
}

# SSM Session Manager（22/SSH を開けない代替の管理経路）。
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# CloudWatch Agent（メトリクス/ログ）。Phase C の監視まで見据えつつ、まず素で付けておく。
resource "aws_iam_role_policy_attachment" "cw_agent" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

# このバケットに限定した S3 権限＝最小権限（B17 (2)・運用設計 §2.6「被害を小さく」・2026-09-04）。
# 箱側スクリプトが S3 に触る先を全部列挙し、それ以外を閉じる（列挙＝残タスク §E-B17）：
#   読取のみ … release/data.json（apply の検知）・code/polyarchy.tar.gz（apply の tar 再展開・user_data）・
#              data/**（bootstrap ⑥ の sync・apply の qdrant ミラー）
#   書込    … ops/dashboard/index.html（dashboard timer）・ops/report/{usage_report.html,weekly.jsonl}（usagereport timer）・
#              data/query_log/**・data/stats/query_log/**（fuelsync timer＝aws s3 sync・--delete なし）
#   Delete  … 箱のどのスクリプトも使わない（fuelsync は --delete を付けない・cp は上書きのみ）＝付与しない。
#   ★配布物（data/・code/・release/）に Put が無い＝箱が乗っ取られても配布物を書き換えて次の apply に載せる経路が無い。
#     配布物の書込は手元の release.sh（データ運用者＝iam-data-operator-policy.json）だけ。
#   ★apply の退避・切り戻しは箱内（/opt/polyarchy/rollback・releases）で完結し S3 には及ばない。
locals {
  s3_code_prefix = "${dirname(var.code_s3_key)}/" # 既定 code/
  s3_read_prefixes = [
    "${var.data_s3_prefix}/*",  # data/**（配布データ。query_log も読める＝sync の比較に無害）
    "${local.s3_code_prefix}*", # code/**
    "release/*",                # release/data.json（apply_data_update.sh が固定で参照）
  ]
  s3_write_prefixes = [
    "ops/*",                                   # ops/dashboard/・ops/report/
    "${var.data_s3_prefix}/query_log/*",       # 捕捉ログ（燃料）の保護コピー
    "${var.data_s3_prefix}/stats/query_log/*", # 同・stats
  ]
}

data "aws_iam_policy_document" "s3_box" {
  # 一覧はバケット全体（aws s3 sync が両方向で ListObjectsV2 を使う。鍵名のメタデータのみ）。
  statement {
    sid       = "ListBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.data.arn]
  }
  # 配布物は読取のみ。
  statement {
    sid       = "ReadDistribution"
    actions   = ["s3:GetObject"]
    resources = [for p in local.s3_read_prefixes : "${aws_s3_bucket.data.arn}/${p}"]
  }
  # 箱が生む成果物だけ書ける。AbortMultipartUpload＝aws CLI が 8MB 超（query_log の jsonl）を分割送信し失敗時に片付けるため
  # （残骸は storage.tf のライフサイクル 7 日でも消える）。DeleteObject は無い（上の列挙どおり不要）。
  statement {
    sid       = "WriteOwnOutputs"
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
    resources = [for p in local.s3_write_prefixes : "${aws_s3_bucket.data.arn}/${p}"]
  }
}

resource "aws_iam_role_policy" "s3_rw" {
  name   = "${var.project}-${var.environment}-s3-rw" # 名前は据え置き（ロールへの付け直しを避ける・中身は最小権限）
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.s3_box.json
}

# SSM Parameter Store の自環境プレフィックスだけ読める（秘密の取得経路）。
# GetParameters(WithDecryption) は KMS の Decrypt が要る＝既定の aws/ssm キーを許可。
data "aws_iam_policy_document" "ssm_params" {
  statement {
    sid = "ReadParams"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project}/${var.environment}/*",
    ]
  }
  statement {
    sid       = "DecryptSecureString"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "ssm_params" {
  name   = "${var.project}-${var.environment}-ssm-params"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.ssm_params.json
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.project}-${var.environment}-ec2"
  role = aws_iam_role.instance.name
}
