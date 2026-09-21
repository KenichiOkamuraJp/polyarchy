# ── 入力変数 ───────────────────────────────────────────────────────────────
# 環境間の差分はすべてここに寄せる（staging と prod の差分＝deploy/PROD_MIGRATION.md）。
# 転写＝同じ .tf をアカウント/プロファイル/enable_web_app だけ変えて apply する。

variable "project" {
  description = "リソース名・タグの接頭辞。"
  type        = string
  default     = "polyarchy"
}

variable "environment" {
  description = "環境識別子。staging=自分AWS(構築・検証) / prod=導入団体 AWS(本番配布)。"
  type        = string
  default     = "staging"

  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment は staging か prod のいずれか。"
  }
}

variable "aws_region" {
  description = "デプロイ先リージョン。東京固定（ローカルモデル推論の低レイテンシ・国内データ）。"
  type        = string
  default     = "ap-northeast-1"
}

variable "aws_profile" {
  description = "AWS CLI プロファイル名。空なら既定の資格情報解決に委ねる。staging/prod でここだけ差し替える。"
  type        = string
  default     = ""
}

variable "instance_type" {
  description = "EC2 インスタンスタイプ。仕様§3.3 で t3.xlarge(4vCPU/16GB) 確定（Lightsail/Lambda 却下）。定常多人数なら m7i.xlarge。"
  type        = string
  default     = "t3.xlarge"
}

variable "root_volume_gb" {
  description = "ルート EBS(gp3) サイズGB。Qdrant 2.0GB＋PDF 2.0GB＋HFモデル＋conda で余裕を見て100。"
  type        = number
  default     = 100
}

variable "enable_web_app" {
  description = "chat_app(Streamlit:8502) を常駐させるか。web は 2026-09-02 に廃止＝staging・prod とも false（MCP のみ＝ランタイム秘密ゼロ）。true は旧構成の再現用（要 Anthropic キー）。"
  type        = bool
  default     = false
}

# ── 入口（Cloudflare Tunnel）関連 ─────────────────────────────────────────────
# Tunnel は Cloudflare 側の資産で Terraform 管理外。ここでは bootstrap の config.yml 生成に
# 渡す「表示名」だけを扱う（DNS は UUID 向きのまま＝変更不要・ロードマップ§2.4）。
variable "tunnel_hostname_mcp" {
  description = "recommendations を出す公開ホスト名（staging=recommendations.polyarchy.net / prod=recommendations.<導入団体ドメイン>。mcp. という名は 2026-09-02 廃止）。"
  type        = string
  default     = "recommendations.polyarchy.net"
}

variable "collection_name" {
  description = "recommendations の検索コレクション名（systemd の COLLECTION_NAME）。既定＝本線 v7/Qdrant。切り戻しは Qdrant 内の policy_claims_v6（Chroma 経路はバッチ2 段4〔2026-08-28〕で全廃）。"
  type        = string
  default     = "policy_claims_v7"
}

variable "vector_backend" {
  description = "recommendations のベクトルバックエンド（qdrant のみ有効＝Chroma 経路はバッチ2 段4〔2026-08-28〕で全廃・他値はアプリが起動時エラー）。bootstrap ⑥b が Qdrant を導入し qdrant.service を常駐させる（data/qdrant は S3 経由で配布）。"
  type        = string
  default     = "qdrant"
}

variable "enable_stats_app" {
  description = "統計参照DB（stats）の MCP サーバ(:8766)を常駐させるか（B6 案 B・別プロセス）。既定 false。C2 以降に有効化。"
  type        = bool
  default     = false
}

variable "tunnel_hostname_stats" {
  description = "stats MCP を出す公開ホスト名（enable_stats_app=true のとき ingress に追加）。"
  type        = string
  default     = ""
}

variable "enable_companies_app" {
  description = "企業情報DB（companies）の MCP サーバ(:8767)を常駐させるか（別プロセス・モデル不要・ランタイム秘密なし）。既定 false。"
  type        = bool
  default     = false
}

variable "tunnel_hostname_companies" {
  description = "companies MCP を出す公開ホスト名（enable_companies_app=true のとき ingress に追加）。"
  type        = string
  default     = ""
}

variable "tunnel_hostname_web" {
  description = "Web(chat_app) を出す公開ホスト名。enable_web_app=false の prod では未使用。"
  type        = string
  default     = "" # web 廃止（2026-09-02）＝旧既定のホスト名は撤去
}

# ── S3（コード配布・データ・燃料ログ・snapshot）─────────────────────────────
variable "code_s3_key" {
  description = "S3 上のアプリコード tar のキー。scripts/upload_to_s3.sh がここへ置く。"
  type        = string
  default     = "code/polyarchy.tar.gz"
}

variable "data_s3_prefix" {
  description = "S3 上のデータ接頭辞（recommendations: qdrant/bm25/pdfs/eval/catalog/query_log、stats: stats/{registry,values,eval,query_log} をこの下に sync。chroma/ は v5 データの保管のみ）。"
  type        = string
  default     = "data"
}

variable "alert_email" {
  description = "アラーム通知（SNS）の宛先メール（運用設計 §1.1/§1.2）。空＝購読を作らない（トピックとアラームは作る）。購読はメール側の Confirm リンクで有効化。"
  type        = string
  default     = ""
}

variable "extra_tags" {
  description = "追加タグ（コスト配賦など）。"
  type        = map(string)
  default     = {}
}
