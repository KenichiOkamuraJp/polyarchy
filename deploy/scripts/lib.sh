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
    "${AWS_REGION:?env に AWS_REGION}"
  # 参照用 Web UI は 2026-10-09 にコードを削除＝未設定は false・true は止める（bootstrap も止める）
  ENABLE_WEB_APP="${ENABLE_WEB_APP:-false}"
  [[ "$ENABLE_WEB_APP" == "true" ]] && die "ENABLE_WEB_APP=true だが Web UI（chat_app）のコードは削除済み＝env を false に"
  export AWS_PROFILE AWS_DEFAULT_REGION="$AWS_REGION"

  # 認証確認＋アカウントID→バケット名（terraform の local.bucket_name と同式）。
  # ★env に BUCKET が書いてあってもここでは使わず再計算する（deploy.sh 系は state と同じ式で足りる）。
  #   env の BUCKET が効くのは release.sh／upload_to_s3.sh（tfstate を持たない運用者用＝RUNBOOK §5）だけ。
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
    -var "enable_companies_app=${ENABLE_COMPANIES_APP:-false}"
    -var "tunnel_hostname_companies=${TUNNEL_HOST_COMPANIES:-}"
    -var "enable_deliberations_app=${ENABLE_DELIBERATIONS_APP:-false}"
    -var "tunnel_hostname_deliberations=${TUNNEL_HOST_DELIBERATIONS:-}"
    -var "mcp_http_host=${MCP_HTTP_HOST:-127.0.0.1}"   # MCP 4 本の待受アドレス（deploy.env へ）。既定のまま使う
    # recommendations の検索構成（既定＝本線 v7/qdrant。切り戻しは Qdrant 内 v6 の COLLECTION_NAME 切替＝
    # Chroma 経路はバッチ2 段4〔2026-08-28〕で全廃・vector_backend は qdrant のみ有効）。
    # ★シェルに export 済みの COLLECTION_NAME（ローカル運用）が混入しないよう env 側は RECOMMENDATIONS_ 接頭辞。
    -var "collection_name=${RECOMMENDATIONS_COLLECTION_NAME:-policy_claims_v7}"
    -var "vector_backend=${RECOMMENDATIONS_VECTOR_BACKEND:-qdrant}"
    # アラーム通知の宛先（運用機能・A1）。空＝SNS 購読なし（トピック/アラームは作られる）。
    -var "alert_email=${ALERT_EMAIL:-}"
  )
  # 箱の機種（2026-10-15・B45 段 4）。空＝terraform の既定（variables.tf）。上げる・戻すは RUNBOOK §1「機種を上げる・戻す」。
  if [[ -n "${INSTANCE_TYPE:-}" ]]; then
    TF_ARGS+=(-var "instance_type=$INSTANCE_TYPE")
  fi
  # アラーム top-user の閾値（2026-10-15・B44 第 1 便）。空＝terraform の既定（variables.tf）。
  if [[ -n "${TOP_USER_CALLS_THRESHOLD:-}" ]]; then
    TF_ARGS+=(-var "top_user_calls_threshold=$TOP_USER_CALLS_THRESHOLD")
  fi
  # 中継の口の送り元（2026-10-16・B44 第 2 便）。空＝規則を作らない（口は閉）。同じ値を secrets が SSM relay_allow_cidrs へ。
  if [[ -n "${RELAY_SOURCE_CIDRS:-}" ]]; then
    TF_ARGS+=(-var "relay_source_cidrs=[\"${RELAY_SOURCE_CIDRS//,/\",\"}\"]")
  fi

  log "env=$ENVIRONMENT profile=$AWS_PROFILE region=$AWS_REGION account=$ACCOUNT_ID web=$ENABLE_WEB_APP instance=${INSTANCE_TYPE:-既定}"
  log "bucket=$BUCKET  ssm=$SSM_PREFIX/*"
}

# ── §2 秘密を SSM へ（両環境で cloudflared・web=true の時だけ Anthropic）──────────
do_register_secrets() {
  # トンネルを張らない箱（公開ホスト TUNNEL_HOST_* が 1 つも無く TUNNEL_ID も空＝中継の口だけで出す構成・PROD_MIGRATION §2.6・2026-10-17）
  # ＝トンネルの登録（cloudflared_tunnel_id・cloudflared_credentials）を警告つきで飛ばし、残りの登録へ進む。bootstrap ⑦ は未登録を
  # 警告して config を書かず、⑧ は公開ホストが無ければ認証なしで建てる（test_common の test_relay_wiring が固定）。
  # 公開ホストがあるのに TUNNEL_ID が無いのは従来どおり止める（公開に出るのに入口が無い）。
  local pub_hosts="${TUNNEL_HOST_MCP:-}${TUNNEL_HOST_STATS:-}${TUNNEL_HOST_COMPANIES:-}${TUNNEL_HOST_DELIBERATIONS:-}"
  if [[ -z "${TUNNEL_ID:-}" && -z "$pub_hosts" ]]; then
    warn "env に TUNNEL_ID も TUNNEL_HOST_* も無い＝トンネルを張らない箱（公開しない・中継の口だけ）としてトンネルの登録を飛ばす"
  else
    [[ -n "${TUNNEL_ID:-}" ]] || die "env に TUNNEL_ID が要る（公開ホスト TUNNEL_HOST_* があるのにトンネルが無い）"
    log "SSM: $SSM_PREFIX/cloudflared_tunnel_id"
    aws ssm put-parameter --overwrite --type String \
      --name "$SSM_PREFIX/cloudflared_tunnel_id" --value "$TUNNEL_ID" >/dev/null

    # トンネルの資格情報は、登録の後に作業用 PC から消す（ルート CLAUDE.md §5）＝ファイルが無くても、SSM に登録済みなら触らずに
    # 先へ（認証の値などを登録し直せるように・2026-10-15）。初回（未登録）はファイルが要る。
    if [[ -f "${CLOUDFLARED_CRED_FILE:-/nonexistent}" ]]; then
      log "SSM: $SSM_PREFIX/cloudflared_credentials（SecureString）"
      aws ssm put-parameter --overwrite --type SecureString \
        --name "$SSM_PREFIX/cloudflared_credentials" \
        --value "file://$CLOUDFLARED_CRED_FILE" >/dev/null
    elif aws ssm get-parameter --name "$SSM_PREFIX/cloudflared_credentials" --query Parameter.Name --output text >/dev/null 2>&1; then
      log "トンネルの資格情報は手元に無い＝SSM の登録済みのものを使う（触らない）"
    else
      die "CLOUDFLARED_CRED_FILE が見つからず、SSM にも登録が無い: ${CLOUDFLARED_CRED_FILE:-未設定}"
    fi
  fi

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
    for svc in stats mcp companies deliberations; do
      local var="ACCESS_AUD_$(echo "$svc" | tr a-z A-Z)" val; val="${!var:-}"
      if [[ -n "$val" ]]; then
        log "SSM: $SSM_PREFIX/access_aud_$svc"
        aws ssm put-parameter --overwrite --type String \
          --name "$SSM_PREFIX/access_aud_$svc" --value "$val" >/dev/null
      fi
    done
  fi

  # 外部 IdP（個人認証・案 B＝docs/個人認証_案B設計.md）。env に AUTH_ISSUER と AUTH_AUD_STATS /
  # AUTH_AUD_MCP / AUTH_AUD_COMPANIES / AUTH_AUD_DELIBERATIONS があれば登録。bootstrap が該当サービスの env に MCP_AUTH_*（resource URL は
  # TUNNEL_HOST_<SVC>＋秘密パスから合成）を書く。AUTH_AUD_<SVC> はサービス毎の有効化スイッチ兼 aud 値。
  if [[ -n "${AUTH_ISSUER:-}" ]]; then
    log "SSM: $SSM_PREFIX/auth_issuer"
    aws ssm put-parameter --overwrite --type String \
      --name "$SSM_PREFIX/auth_issuer" --value "$AUTH_ISSUER" >/dev/null
    for svc in stats mcp companies deliberations; do
      local avar="AUTH_AUD_$(echo "$svc" | tr a-z A-Z)" aval; aval="${!avar:-}"
      if [[ -n "$aval" ]]; then
        log "SSM: $SSM_PREFIX/auth_aud_$svc"
        aws ssm put-parameter --overwrite --type String \
          --name "$SSM_PREFIX/auth_aud_$svc" --value "$aval" >/dev/null
      fi
    done
  fi

  # 中継の口の許可の範囲（2026-10-16・B44 第 2 便・秘密ではないが SSM を単一の真実にする）。値の正は env の RELAY_SOURCE_CIDRS
  # （terraform の網の規則にも同じ値が渡る）。空にしたら、登録があるときだけ off に（次の配布で口が閉じる）。
  if [[ -n "${RELAY_SOURCE_CIDRS:-}" ]]; then
    log "SSM: $SSM_PREFIX/relay_allow_cidrs"
    aws ssm put-parameter --overwrite --type String \
      --name "$SSM_PREFIX/relay_allow_cidrs" --value "$RELAY_SOURCE_CIDRS" >/dev/null
  elif aws ssm get-parameter --name "$SSM_PREFIX/relay_allow_cidrs" --query Parameter.Name --output text >/dev/null 2>&1; then
    log "SSM: $SSM_PREFIX/relay_allow_cidrs＝off（env の RELAY_SOURCE_CIDRS が空＝中継の口を閉じる）"
    aws ssm put-parameter --overwrite --type String \
      --name "$SSM_PREFIX/relay_allow_cidrs" --value off >/dev/null
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

# ── §4' 対話なしで流す形（2026-10-13）＝plan を保存 → 差分を読む → 保存した plan をそのまま適用 → 直後の plan で差分 0。
#    読んだ plan と適用する plan が同じ。保存した plan が無ければ適用しない＝plan を読む段を飛ばせない。
#    plan の後に state が動いていれば terraform が「stale」で止める。置き場は .terraform/（git 外）。
tf_plan_file() { printf '%s' "$TF_DIR/.terraform/polyarchy-${ENVIRONMENT}.tfplan"; }

do_plan() {
  local pf rc=0; pf="$(tf_plan_file)"
  log "Terraform plan（読み取りだけ・保存先 ${pf}）"
  mkdir -p "$(dirname "$pf")"
  ( cd "$TF_DIR" && terraform init -input=false >/dev/null \
      && terraform plan -input=false -detailed-exitcode "${TF_ARGS[@]}" "-out=$pf" ) || rc=$?
  # 保存した plan を残すのは差分があるときだけ＝差分なしの plan が残ると、後日 plan を取り直さずに流した apply-plan に
  # 古い plan が当たる（2026-10-13 staging）。前に残っていた plan も、ここで上書きされるか消える。
  case "$rc" in
    0) rm -f "$pf"; log "差分なし（No changes）＝plan は残さない（apply-plan で流すものは無い）" ;;
    2) log "差分を読んでから: bash deploy.sh ${ENVIRONMENT} apply-plan（保存した plan をそのまま適用する）" ;;
    *) rm -f "$pf"; die "terraform plan が失敗（rc=${rc}）" ;;
  esac
}

do_apply_plan() {
  local pf rc=0; pf="$(tf_plan_file)"
  [[ -f "$pf" ]] || die "保存した plan が無い（先に bash deploy.sh ${ENVIRONMENT} plan で差分を読む）"
  log "Terraform 適用（保存した plan・対話なし）"
  ( cd "$TF_DIR" && terraform init -input=false >/dev/null && terraform apply -input=false "$pf" ) || rc=$?
  rm -f "$pf"                                   # 成否にかかわらず残さない（同じ plan は 2 回適用できない）
  [[ "$rc" == 0 ]] || die "terraform apply が失敗（plan は消した＝直してから plan からやり直す）"
  rc=0
  ( cd "$TF_DIR" && terraform plan -input=false -detailed-exitcode "${TF_ARGS[@]}" >/dev/null ) || rc=$?
  case "$rc" in
    0) log "適用の直後の plan: No changes ✓" ;;
    2) die "適用の直後の plan に差分が残っている（bash deploy.sh ${ENVIRONMENT} plan で読む）" ;;
    *) die "適用の直後の plan が失敗（rc=${rc}）" ;;
  esac
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
  log "── 次（初回構築のときの手順＝README §5〜。上の terraform の next_steps も同じ。既存の箱への小さな変更〔S3 のライフサイクル・アラーム・IAM 等〕の apply なら読み飛ばしてよい） ──"
  log "箱に入る:     aws ssm start-session --target ${iid:-<instance_id>}"
  log "bootstrap監視: sudo tail -f /var/log/polyarchy-bootstrap.log"
  log "次:           箱の上の smoke → 入口を開く（PROD_MIGRATION §5.2・§5.3）。以後の更新は release.sh だけ（§6.3）"
}
