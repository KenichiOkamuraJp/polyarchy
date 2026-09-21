#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Cloudflare 入口ガード（MCP エンドポイントの IP 許可リスト ＋ レート制限）
#
#   bash cloudflare-guard.sh status              現在のルールと設定値を表示（読み取りのみ）
#   bash cloudflare-guard.sh apply [strict|connector]   ルールを適用（既定 connector）
#   ★現行方針（2026-08-28〜）：IP 許可は廃止＝IP_ALLOWLIST の既定は off（秘密パス＋レート制限のみ適用）。
#     strict/connector のモード・許可IPの説明は IP_ALLOWLIST=on を明示した場合にだけ意味を持つ（歴史的経緯）。
#   ★prod で使うときは既定値が staging 固定な点に注意＝ZONE_NAME・MCP_HOST・SSM_PATH_PARAM・
#     AWS_PROFILE_CF・RL_HOSTS・SERVICE を必ず上書きする（例は deploy/PROD_MIGRATION.md §2.3）。
#   ★レート制限は Free プラン1本＝description は polyarchy-mcp-ratelimit 固定で全サービス共有
#     （SERVICE=stats remove ではレート制限は消えない＝消すのは mcp 側の remove か手動）。
#   bash cloudflare-guard.sh remove              本スクリプトが作ったルールだけ削除
#   bash cloudflare-guard.sh test                この端末から到達できるか確認
#
# ★モードの違い（重要・Anthropic 公式仕様に基づく）
#   Claude の「カスタムコネクタ」(claude.ai / Claude Desktop) は、利用者PCからではなく
#   **Anthropic のサーバ**から MCP サーバへ接続する。したがって送信元IPは組織IPではなく
#   Anthropic のレンジ(160.79.104.0/21)になる。
#     connector … 組織IP ＋ Anthropic レンジを許可（claude.ai/Desktop で使える）
#     strict    … 組織IPのみ許可（Claude Code など端末から直接叩く場合のみ疎通）
#   → 利用者がカスタムコネクタで使うなら connector が必須。ただし connector は
#     「Claude 経由なら第三者も到達しうる」ため、組織限定にはならない点を必ず共有すること。
#
# 認証：Cloudflare API トークンを CF_API_TOKEN で渡す（このスクリプトは値を保存も表示もしない）。
#   作成場所 https://dash.cloudflare.com/profile/api-tokens
#   必要権限 Zone → Zone:Read ＋ Zone → WAF:Edit（対象ゾーン＝ZONE_NAME）
#   例) export CF_API_TOKEN=xxxx   /  or  macOS Keychain（下記 CF_TOKEN_KEYCHAIN_ITEM）
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── 設定（環境変数で上書き可）─────────────────────────────────────────────────
# 既定＝staging の現行（2026-09-02 ドメイン移行後）。prod は ZONE_NAME/MCP_HOST/SSM_PATH_PARAM/AWS_PROFILE_CF/RL_HOSTS を必ず上書き（PROD_MIGRATION §2.3）。
ZONE_NAME="${ZONE_NAME:-polyarchy.net}"
MCP_HOST="${MCP_HOST:-recommendations.polyarchy.net}"
# サービス識別子（B6 案 B：ホストごとに別プロセス）。ルールの description を "polyarchy-<SERVICE>-…" にして
# サービス単位で入れ替える＝stats 用に適用しても recommendations(mcp) のルールを消さない。
#   recommendations: 既定（SERVICE=mcp・MCP_HOST=mcp.…・SSM mcp_http_path）
#   stats : SERVICE=stats MCP_HOST=stats.… SSM_PATH_PARAM=/polyarchy/<env>/stats_http_path bash cloudflare-guard.sh apply
#   companies : SERVICE=companies MCP_HOST=companies.… SSM_PATH_PARAM=/polyarchy/<env>/companies_http_path bash cloudflare-guard.sh apply
SERVICE="${SERVICE:-mcp}"
# レート制限ルールは Free プランで1本のため単一ルールに全ホストを載せる（RL_HOSTS＝カンマ区切り・既定 MCP_HOST）。
RL_HOSTS="${RL_HOSTS:-$MCP_HOST}"
ORG_IPS="${ORG_IPS:-}"                               # 導入団体の固定 IP（IP_ALLOWLIST=on のときだけ使う・既定は空）
ANTHROPIC_IPS="${ANTHROPIC_IPS:-160.79.104.0/21}"    # Anthropic outbound（MCP ツール呼び出し元）
EXTRA_IPS="${EXTRA_IPS:-}"                           # 任意：自分の作業IP等を足す場合
RATE_PER_MIN="${RATE_PER_MIN:-1200}"                 # 目安 600〜1200/分（anti-flood）
CF_TOKEN_KEYCHAIN_ITEM="${CF_TOKEN_KEYCHAIN_ITEM:-cloudflare-api-token}"   # WAF:Edit を持つ旧トークン（cf-lib.sh 参照）

TAG="polyarchy-${SERVICE}"  # ルール識別子（LOG_TAG とは別）                            # 本スクリプト管理ルールの目印（description 接頭辞・サービス単位）
# 秘密パスの取得元（SSM・単一の真実）。AWS_PROFILE/REGION は環境変数で切替（prod は別プロファイル）。
SSM_PATH_PARAM="${SSM_PATH_PARAM:-/polyarchy/staging/mcp_http_path}"
AWS_PROFILE_CF="${AWS_PROFILE_CF:-polyarchy-staging}"
AWS_REGION_CF="${AWS_REGION_CF:-ap-northeast-1}"

# 共通（log/warn/die・トークン取得・cf）は cf-lib.sh（guard は WAF:Edit のある旧トークンが既定）。
LOG_TAG=cf
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cf-lib.sh"
get_token() { cf_get_token; }
API="$CF_API"

# 秘密パスを SSM から取得（無ければ空＝既定 /mcp 扱い）。MCP_PATH env で上書き可。
load_mcp_path() {
  if [[ -n "${MCP_PATH:-}" ]]; then return; fi
  MCP_PATH="$(aws ssm get-parameter --with-decryption --name "$SSM_PATH_PARAM" \
      --profile "$AWS_PROFILE_CF" --region "$AWS_REGION_CF" \
      --query 'Parameter.Value' --output text 2>/dev/null || true)"
  # 末尾を必ず成功で終える（`[[ ]] && ...` が偽だと戻り値1になり set -e で静かに落ちるため）。
  [[ "$MCP_PATH" == "None" ]] && MCP_PATH="" || true
  return 0
}

cf_ok() { # stdin=response → success なら 0。失敗時はエラーを表示。
  local resp; resp="$(cat)"
  if [[ "$(printf '%s' "$resp" | jq -r '.success' 2>/dev/null)" == "true" ]]; then
    printf '%s' "$resp"; return 0
  fi
  printf '%s' "$resp" | jq -r '.errors[]? | "  API error \(.code): \(.message)"' >&2 2>/dev/null || printf '%s\n' "$resp" >&2
  return 1
}

# ── ゾーン情報 ───────────────────────────────────────────────────────────────
# ZONE_ID を環境変数で渡せば Zone:Read 権限が無くても動く（WAF:Edit だけで完結）。
# ゾーンIDは Cloudflare ダッシュボードで対象ドメインを開いた右下「Zone ID」からコピーできる。
zone_info() {
  if [[ -n "${ZONE_ID:-}" ]]; then
    ZONE_PLAN="${ZONE_PLAN:-free}"   # 取得できない場合は free 相当（10秒窓＝全プランで有効）で安全側に
    log "ZONE_ID を環境変数から使用（ゾーン照会をスキップ）"
    return
  fi
  local r; r="$(cf GET "/zones?name=${ZONE_NAME}" | cf_ok)" \
    || die "ゾーン照会に失敗。トークンに Zone:Read が無い場合は ZONE_ID=... を指定して再実行してください。"
  ZONE_ID="$(printf '%s' "$r" | jq -r '.result[0].id // empty')"
  ZONE_PLAN="$(printf '%s' "$r" | jq -r '.result[0].plan.legacy_id // .result[0].plan.name // "unknown"')"
  [[ -n "$ZONE_ID" ]] || die "ゾーン ${ZONE_NAME} が見つからない（トークンのゾーン範囲を確認）"
}

# 無料プランはレート制限が「10秒窓・10秒ブロック・1ルール」に制限されるため窓を合わせる。
rate_params() {
  if [[ "$ZONE_PLAN" == "free" ]]; then
    RL_PERIOD=10; RL_REQS=$(( RATE_PER_MIN / 6 )); RL_TIMEOUT=10
  else
    RL_PERIOD=60; RL_REQS="$RATE_PER_MIN";        RL_TIMEOUT=60
  fi
}

allow_list() { # 許可IPを Cloudflare 式の集合表記に（スペース区切り）
  # ORG_IPS を空にして運用することも可能（コネクタ経由なら組織IPは使われないため）。
  # 前後の空白を落として `ip.src in { ... }` が常に妥当な式になるようにする。
  local ips="$ORG_IPS"
  [[ "$MODE" == "connector" ]] && ips="$ips $ANTHROPIC_IPS"
  [[ -n "$EXTRA_IPS" ]] && ips="$ips $EXTRA_IPS"
  ips="$(echo "$ips" | tr ',' ' ' | tr -s ' ' | sed 's/^ *//; s/ *$//')"
  [[ -n "$ips" ]] || die "許可IPが空。ORG_IPS か ANTHROPIC_IPS のいずれかは必要。"
  printf '%s' "$ips"
}

# ── 現在の phase entrypoint ルールを取得（無ければ空配列）─────────────────────
get_rules() { # $1=phase
  local r
  r="$(cf GET "/zones/${ZONE_ID}/rulesets/phases/$1/entrypoint" 2>/dev/null)" || true
  if [[ "$(printf '%s' "$r" | jq -r '.success' 2>/dev/null)" == "true" ]]; then
    printf '%s' "$r" | jq '.result.rules // []'
  else
    printf '[]'   # 未作成のフェーズ＝ルール無し
  fi
}

put_rules() { # $1=phase  $2=rules(JSON array)
  local body; body="$(jq -n --argjson rules "$2" '{rules:$rules}')"
  cf PUT "/zones/${ZONE_ID}/rulesets/phases/$1/entrypoint" "$body" | cf_ok >/dev/null \
    || die "$1 の更新に失敗"
}

# ── サブコマンド ─────────────────────────────────────────────────────────────
cmd_status() {
  zone_info; rate_params
  log "zone=${ZONE_NAME} (${ZONE_ID}) plan=${ZONE_PLAN}"
  log "対象ホスト=${MCP_HOST}"
  echo "── WAF カスタムルール（http_request_firewall_custom）──"
  get_rules http_request_firewall_custom \
    | jq -r '.[] | "  [\(.action)] \(.description // "(no desc)")\n      \(.expression)"' 2>/dev/null || echo "  （なし）"
  echo "── レート制限（http_ratelimit）──"
  get_rules http_ratelimit \
    | jq -r '.[] | "  [\(.action)] \(.description // "(no desc)")  \(.ratelimit.requests_per_period)req/\(.ratelimit.period)s\n      \(.expression)"' 2>/dev/null || echo "  （なし）"
  echo "── この端末のグローバルIP ──"
  echo "  $(curl -sS -m 10 https://api.ipify.org 2>/dev/null || echo '取得失敗')"
}

cmd_apply() {
  MODE="${1:-connector}"
  [[ "$MODE" == "strict" || "$MODE" == "connector" ]] || die "モードは strict か connector"
  zone_info; rate_params; load_mcp_path
  local ips; ips="$(allow_list)"

  log "zone=${ZONE_NAME} plan=${ZONE_PLAN} mode=${MODE}"
  log "許可IP: ${ips}"
  log "レート制限: ${RL_REQS} req / ${RL_PERIOD}s（≒ ${RATE_PER_MIN}/分）"
  if [[ "$MODE" == "connector" ]]; then
    warn "connector モード＝Anthropic レンジを許可。claude.ai/Desktop のコネクタで疎通する一方、"
    warn "  「Claude 経由なら第三者も到達しうる」点は導入団体へ必ず共有すること（組織限定にはならない）。"
  else
    warn "strict モード＝組織IPのみ。claude.ai/Desktop のカスタムコネクタは**繋がらない**"
    warn "  （Anthropic 経由のため）。Claude Code 等の直接接続のみ疎通する。"
  fi

  # ① WAF カスタムルール（許可IP以外を block ／ 秘密パス以外を block）
  # ★IP_ALLOWLIST=off で IP 許可ルールを作らない（既存の同名ルールも除去される）。
  #   経緯：OAuth のディスカバリ取得や Access ログインは Anthropic の公表レンジ外から来るため、
  #   IP 方式は OAuth 化したサービスと両立しない（2026-08-28 stats で実測・mcp は 2026-08-27 に同結論）。
  local expr_ip rules_now rules_new add_rules
  if [[ "${IP_ALLOWLIST:-off}" == "off" ]]; then   # ★既定 off（IP 方式は 2026-08-27/28 廃止・on は明示時のみ）
    warn "IP_ALLOWLIST=off＝IP 許可ルールは作らない（防御は OAuth/秘密パス/レート制限）"
    add_rules='[]'
  else
    expr_ip="(http.host eq \"${MCP_HOST}\" and not ip.src in {${ips}})"
    add_rules="$(jq -n --arg expr "$expr_ip" --arg tag "$TAG" \
        '[{action:"block", expression:$expr, description:($tag + "-ip-allowlist"), enabled:true}]')"
  fi

  # 秘密パス（SSM 由来）。設定されていればエッジで「正しいパス以外」を遮断＝origin に届かせない。
  # ★除外 3 つ（2026-08-28 実測で追加）：
  #   /.well-known/ … OAuth ディスカバリ（AS metadata/PRM）。WAF は Access より先に評価されるため、
  #     ここを塞ぐと Claude のコネクタ登録（Anthropic サーバからの取得）が失敗する（Managed OAuth の前提）。
  #   /cdn-cgi/    … Cloudflare 内部＝Access のログイン/コールバック（ブラウザが app ホスト経由で
  #     認証 Cookie を張る）。塞ぐと OTP ログインが「Sorry, you have been blocked」で死ぬ（実測）。
  #   /healthz     … 外形監視用（運用設計 §1.1。WAF から除外＝認証不要で 200が、
  #     Bypass ポリシーを足す将来に備えて WAF では通す）。
  if [[ -n "$MCP_PATH" && "$MCP_PATH" != "/mcp" ]]; then
    local expr_path
    expr_path="(http.host eq \"${MCP_HOST}\" and not starts_with(http.request.uri.path, \"${MCP_PATH}\") and not starts_with(http.request.uri.path, \"/.well-known/\") and not starts_with(http.request.uri.path, \"/cdn-cgi/\") and not http.request.uri.path eq \"/healthz\")"
    add_rules="$(jq -n --argjson a "$add_rules" --arg expr "$expr_path" --arg tag "$TAG" \
        '$a + [{action:"block", expression:$expr, description:($tag + "-secret-path"), enabled:true}]')"
    log "秘密パス: 設定あり（エッジで正しいパス以外を遮断・値は表示しない）"
  else
    warn "秘密パス未設定（既定 /mcp）＝推測可能。SSM の mcp_http_path を設定して再適用を推奨。"
  fi

  rules_now="$(get_rules http_request_firewall_custom)"
  rules_new="$(jq -n --argjson now "$rules_now" --argjson add "$add_rules" --arg tag "$TAG" '
      ($now | map(select((.description // "") | startswith($tag + "-") | not))) + $add')"
  put_rules http_request_firewall_custom "$rules_new"
  if [[ "${IP_ALLOWLIST:-off}" == "off" ]]; then log "✅ 秘密パスルールを適用（IP 許可は作らない）"; else log "✅ IP 許可リスト（＋秘密パス）を適用"; fi

  # ② レート制限（anti-flood）
  local expr_rl rl_now rl_new
  expr_rl="$(printf '%s' "$RL_HOSTS" | tr ',' '\n' | sed '/^$/d;s/.*/(http.host eq "&")/' | paste -sd'|' - | sed 's/|/ or /g')"
  rl_now="$(get_rules http_ratelimit)"
  rl_new="$(jq -n --argjson now "$rl_now" --arg expr "$expr_rl" --arg tag "$TAG" \
              --argjson period "$RL_PERIOD" --argjson reqs "$RL_REQS" --argjson mt "$RL_TIMEOUT" '
      ($now | map(select((.description // "") | startswith("polyarchy-") | not)))
      + [{action:"block", expression:$expr, description:"polyarchy-mcp-ratelimit", enabled:true,
          ratelimit:{characteristics:["ip.src","cf.colo.id"], period:$period,
                     requests_per_period:$reqs, mitigation_timeout:$mt}}]')"
  put_rules http_ratelimit "$rl_new"
  log "✅ レート制限を適用"
  echo ""
  log "反映は即時。確認 → bash ${BASH_SOURCE[0]##*/} test"
}

cmd_remove() {
  zone_info
  local p
  for p in http_request_firewall_custom http_ratelimit; do
    local now new
    now="$(get_rules "$p")"
    new="$(jq -n --argjson now "$now" --arg tag "$TAG" '$now | map(select((.description // "") | startswith($tag) | not))')"
    put_rules "$p" "$new"
    log "✅ ${p} から ${TAG}-* ルールを削除"
  done
  warn "MCP エンドポイントは再び無制限（authless）に戻った。"
}

probe() { # $1=path → HTTP コード
  curl -sS -m 25 -o /dev/null -w '%{http_code}' -X POST "https://${MCP_HOST}$1" \
    -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' 2>/dev/null || echo ERR
}

cmd_test() {
  load_mcp_path
  local ip c_old c_secret
  ip="$(curl -sS -m 10 https://api.ipify.org 2>/dev/null || echo '不明')"
  log "この端末のグローバルIP: ${ip}"
  c_old="$(probe /mcp)"
  log "旧パス /mcp             → HTTP ${c_old}   （403/404 が正・誰でも試せる入口が塞がったか）"
  if [[ -n "$MCP_PATH" && "$MCP_PATH" != "/mcp" ]]; then
    c_secret="$(probe "$MCP_PATH")"
    log "秘密パス（値は非表示）   → HTTP ${c_secret}   （この端末が許可外なら 403 が正）"
  fi
  echo ""
  case "$c_old" in
    403) log "✅ エッジで遮断されている（IP許可リスト／秘密パスが有効）" ;;
    200) warn "⚠ 旧パスに到達できている＝ルール未適用かこのIPが許可対象" ;;
    *)   log "（${c_old}：origin まで届いていない可能性）" ;;
  esac
  log "※ 利用者側ネットワークからの疎通確認は別途必要（この端末からは検証できない）。"
}

case "${1:-}" in
  status) cmd_status ;;
  apply)  cmd_apply "${2:-connector}" ;;
  remove) cmd_remove ;;
  test)   cmd_test ;;
  *) die "使い方: cloudflare-guard.sh {status|apply [strict|connector]|remove|test}" ;;
esac
