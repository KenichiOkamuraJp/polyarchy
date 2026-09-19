#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# 共通ライブラリ（register-secrets.sh / deploy.sh が source する）。
# env/<name>.env（非秘密パラメータ）を読み、AWS CLI と Terraform に橋渡しする。
# ★bash で実行されるため、対話 zsh の「クォート無し変数を単語分割しない」問題は起きない。
# ★macOS 既定の bash 3.2 は multibyte バグあり：`$var` の直後に日本語（例 全角括弧）を置くと
#   UTF-8 ロケールで変数名に食い込み "unbound variable" になる → 日本語に隣接する変数は `${var}` と囲む。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # …/polyarchy/deploy
SCRIPTS_DIR="$DEPLOY_DIR/scripts"
TF_DIR="$DEPLOY_DIR/terraform"

log()  { printf '\033[1;34m[deploy]\033[0m %s\n'       "$*" >&2; }
warn() { printf '\033[1;33m[deploy:warn]\033[0m %s\n'  "$*" >&2; }
die()  { printf '\033[1;31m[deploy:ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

confirm() { # $1=プロンプト。yes とタイプした時だけ 0。
  local ans; read -r -p "$1 [yes/no] " ans
  [[ "$ans" == "yes" ]]
}

# 秘密キーを SOURCE 指定（file:/env:/prompt）に従って解決して stdout。$1=source $2=キー名(file: の grep用)。
resolve_key() {
  local src="$1" keyname="$2"
  case "$src" in
    file:*) grep -E "^${keyname}=" "${src#file:}" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r\n' ;;
    env:*)  local v="${src#env:}"; printf '%s' "${!v:-}" ;;
    prompt) local k; read -r -s -p "${keyname} を入力: " k; printf '\n' >&2; printf '%s' "$k" ;;
    *)      return 1 ;;
  esac
}

# env ファイルを読み、AWS/Terraform 用のグローバルを整える。
load_env() {
  local name="${1:-${POLYARCHY_ENV:-}}"
  [[ -n "$name" ]] || die "環境名が必要（staging|prod）。例: deploy.sh staging"
  local f="$DEPLOY_DIR/env/$name.env"
  [[ -f "$f" ]] || die "$f が無い。cp env/$name.env.example env/$name.env して編集してください。"
  # shellcheck disable=SC1090
  set -a; source "$f"; set +a
  PROJECT="${PROJECT:-polyarchy}"

  : "${ENVIRONMENT:?env に ENVIRONMENT が要る}" \
    "${AWS_PROFILE:?env に AWS_PROFILE}" \
    "${AWS_REGION:?env に AWS_REGION}" \
    "${ENABLE_WEB_APP:?env に ENABLE_WEB_APP}"
  export AWS_PROFILE AWS_DEFAULT_REGION="$AWS_REGION"

  # 認証確認＋アカウントID→バケット名（terraform の local.bucket_name と同式）。
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
    || die "AWS 認証に失敗（AWS_PROFILE=${AWS_PROFILE}）。aws configure --profile ${AWS_PROFILE} を確認。"
  BUCKET="${PROJECT}-${ENVIRONMENT}-${ACCOUNT_ID}"
  SSM_PREFIX="/${PROJECT}/${ENVIRONMENT}"

  # Terraform へは -var で明示注入（stray な terraform.tfvars に上書きされないよう最優先の -var を使う）。
  TF_ARGS=(
    -var "project=$PROJECT"
    -var "environment=$ENVIRONMENT"
    -var "aws_region=$AWS_REGION"
    -var "aws_profile=$AWS_PROFILE"
    -var "enable_web_app=$ENABLE_WEB_APP"
    -var "tunnel_hostname_mcp=${TUNNEL_HOST_MCP:-}"
    -var "tunnel_hostname_web=${TUNNEL_HOST_WEB:-}"
    -var "enable_stats_app=${ENABLE_STATS_APP:-false}"
    -var "tunnel_hostname_stats=${TUNNEL_HOST_STATS:-}"
    # recommendations の検索構成（既定＝本線 v7/qdrant。切り戻しは Qdrant 内 v6 の COLLECTION_NAME 切替＝
    # Chroma 経路はバッチ2 段4〔2026-08-28〕で全廃・vector_backend は qdrant のみ有効）。
    # ★シェルに export 済みの COLLECTION_NAME（ローカル運用）が混入しないよう env 側は RECOMMENDATIONS_ 接頭辞。
    -var "collection_name=${RECOMMENDATIONS_COLLECTION_NAME:-policy_claims_v7}"
    -var "vector_backend=${RECOMMENDATIONS_VECTOR_BACKEND:-qdrant}"
    # アラーム通知の宛先（運用機能・A1）。空＝SNS 購読なし（トピック/アラームは作られる）。
    -var "alert_email=${ALERT_EMAIL:-}"
  )

  log "env=$ENVIRONMENT profile=$AWS_PROFILE region=$AWS_REGION account=$ACCOUNT_ID web=$ENABLE_WEB_APP"
  log "bucket=$BUCKET  ssm=$SSM_PREFIX/*"
}

# ── §2 秘密を SSM へ（両環境で cloudflared・web=true の時だけ Anthropic）──────────
do_register_secrets() {
  [[ -n "${TUNNEL_ID:-}" ]] || die "env に TUNNEL_ID が要る"
  [[ -f "${CLOUDFLARED_CRED_FILE:-/nonexistent}" ]] \
    || die "CLOUDFLARED_CRED_FILE が見つからない: ${CLOUDFLARED_CRED_FILE:-未設定}"

  log "SSM: $SSM_PREFIX/cloudflared_tunnel_id"
  aws ssm put-parameter --overwrite --type String \
    --name "$SSM_PREFIX/cloudflared_tunnel_id" --value "$TUNNEL_ID" >/dev/null

  log "SSM: $SSM_PREFIX/cloudflared_credentials（SecureString）"
  aws ssm put-parameter --overwrite --type SecureString \
    --name "$SSM_PREFIX/cloudflared_credentials" \
    --value "file://$CLOUDFLARED_CRED_FILE" >/dev/null

  if [[ "$ENABLE_WEB_APP" == "true" ]]; then
    local key; key="$(resolve_key "${ANTHROPIC_SOURCE:-}" ANTHROPIC_API_KEY || true)"
    [[ -n "$key" ]] || die "Anthropic キーを取得できない（ANTHROPIC_SOURCE=${ANTHROPIC_SOURCE:-未設定}）"
    log "SSM: $SSM_PREFIX/anthropic_api_key（SecureString）"
    aws ssm put-parameter --overwrite --type SecureString \
      --name "$SSM_PREFIX/anthropic_api_key" --value "$key" >/dev/null
  else
    log "web 無効＝Anthropic は登録しない（ランタイム秘密ゼロ）"
  fi

  # OpenAI（eval ゲートのガード用・staging のみ・任意）。OPENAI_SOURCE があれば登録。serving では
  # 未使用（検索は ruri ローカル）＝prod では設定しない＝prod ランタイム秘密ゼロを維持。
  if [[ -n "${OPENAI_SOURCE:-}" ]]; then
    local okey; okey="$(resolve_key "$OPENAI_SOURCE" OPENAI_API_KEY || true)"
    if [[ -n "$okey" ]]; then
      log "SSM: $SSM_PREFIX/openai_api_key（SecureString・eval ゲート用）"
      aws ssm put-parameter --overwrite --type SecureString \
        --name "$SSM_PREFIX/openai_api_key" --value "$okey" >/dev/null
    else
      warn "OPENAI_SOURCE 指定ありだがキーを取得できず（eval ゲートが OPENAI_API_KEY 不足で失敗し得る）"
    fi
  fi

  # e-Stat appId（stats の更新チェック＝freshness が箱で e-Stat 系を検知するため）。ESTAT_SOURCE があれば登録。
  # 環境ごとに別の appId（導入団体 prod は導入団体が本人登録で取得したもの）。
  if [[ -n "${ESTAT_SOURCE:-}" ]]; then
    local ekey; ekey="$(resolve_key "$ESTAT_SOURCE" ESTAT_APP_ID || true)"
    if [[ -n "$ekey" ]]; then
      log "SSM: $SSM_PREFIX/estat_app_id（SecureString・更新チェック用）"
      aws ssm put-parameter --overwrite --type SecureString \
        --name "$SSM_PREFIX/estat_app_id" --value "$ekey" >/dev/null
    else
      warn "ESTAT_SOURCE 指定ありだが appId を取得できず（箱の e-Stat 更新検知は無効のまま）"
    fi
  fi

  # Cloudflare Access Managed OAuth（公式コネクタ化・案 A）。env に ACCESS_TEAM_DOMAIN と ACCESS_AUD_STATS / ACCESS_AUD_MCP が
  # あれば登録（秘密ではないが SSM を単一の真実にする）。bootstrap が該当サービスの env に MCP_ACCESS_* を書く。
  if [[ -n "${ACCESS_TEAM_DOMAIN:-}" ]]; then
    log "SSM: $SSM_PREFIX/access_team_domain"
    aws ssm put-parameter --overwrite --type String \
      --name "$SSM_PREFIX/access_team_domain" --value "$ACCESS_TEAM_DOMAIN" >/dev/null
    for svc in stats mcp; do
      local var="ACCESS_AUD_$(echo "$svc" | tr a-z A-Z)" val; val="${!var:-}"
      if [[ -n "$val" ]]; then
        log "SSM: $SSM_PREFIX/access_aud_$svc"
        aws ssm put-parameter --overwrite --type String \
          --name "$SSM_PREFIX/access_aud_$svc" --value "$val" >/dev/null
      fi
    done
  fi

  # 外部 IdP（個人認証・案 B＝docs/個人認証_案B設計.md）。env に AUTH_ISSUER と AUTH_AUD_STATS /
  # AUTH_AUD_MCP があれば登録。bootstrap が該当サービスの env に MCP_AUTH_*（resource URL は
  # TUNNEL_HOST_<SVC>＋秘密パスから合成）を書く。AUTH_AUD_<SVC> はサービス毎の有効化スイッチ兼 aud 値。
  if [[ -n "${AUTH_ISSUER:-}" ]]; then
    log "SSM: $SSM_PREFIX/auth_issuer"
    aws ssm put-parameter --overwrite --type String \
      --name "$SSM_PREFIX/auth_issuer" --value "$AUTH_ISSUER" >/dev/null
    for svc in stats mcp; do
      local avar="AUTH_AUD_$(echo "$svc" | tr a-z A-Z)" aval; aval="${!avar:-}"
      if [[ -n "$aval" ]]; then
        log "SSM: $SSM_PREFIX/auth_aud_$svc"
        aws ssm put-parameter --overwrite --type String \
          --name "$SSM_PREFIX/auth_aud_$svc" --value "$aval" >/dev/null
      fi
    done
  fi

  log "登録済み（名前のみ）:"
  aws ssm get-parameters-by-path --path "$SSM_PREFIX" --query 'Parameters[].Name' --output table
}

# ── §3a S3 バケットを先に作る（upload の受け皿）──────────────────────────────
do_provision_bucket() {
  log "Terraform init ＋ S3 バケット先行作成（${BUCKET}）"
  ( cd "$TF_DIR" && terraform init -input=false >/dev/null \
      && terraform apply -input=false -auto-approve \
           "${TF_ARGS[@]}" -target=aws_s3_bucket_versioning.data )
}

# ── §3b コード tar ＋ データを S3 へ（所要時間の山：約 7GB）──────────────────
do_upload() {
  log "コード/データを S3 へ投入（upload_to_s3.sh）"
  BUCKET="$BUCKET" PROFILE="$AWS_PROFILE" REGION="$AWS_REGION" \
    bash "$SCRIPTS_DIR/upload_to_s3.sh"
}

# ── §4 本適用（EC2/VPC/IAM…）＝プランを見せて対話で承認 ─────────────────────
do_apply() {
  log "Terraform 本適用（プラン確認 → yes で実行）"
  ( cd "$TF_DIR" && terraform init -input=false >/dev/null \
      && terraform apply "${TF_ARGS[@]}" )
}

# 稼働中インスタンスIDをタグ(Project/Environment)で検索（terraform state 非依存＝rollback でも使える）。
# load_env 済み（PROJECT/ENVIRONMENT/AWS_PROFILE 設定済）が前提。
instance_id() {
  aws ec2 describe-instances \
    --filters "Name=tag:Project,Values=$PROJECT" "Name=tag:Environment,Values=$ENVIRONMENT" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null | tr '\t' '\n' | head -1
}

# インスタンスのルート EBS ボリュームIDを返す。
root_volume_id() {
  local iid="$1"
  aws ec2 describe-instances --instance-ids "$iid" \
    --query 'Reservations[].Instances[].BlockDeviceMappings[?DeviceName==`/dev/sda1`||DeviceName==`/dev/xvda`].Ebs.VolumeId' \
    --output text 2>/dev/null | tr '\t' '\n' | head -1
}

print_next_steps() {
  local iid; iid="$( ( cd "$TF_DIR" && terraform output -raw instance_id 2>/dev/null ) || true )"
  log "── 次（README §5〜） ──"
  log "箱に入る:     aws ssm start-session --target ${iid:-<instance_id>}"
  log "bootstrap監視: sudo tail -f /var/log/polyarchy-bootstrap.log"
  log "回帰ゲート:   README §6（4種 PASS）→ Tunnel カットオーバー §7"
}
