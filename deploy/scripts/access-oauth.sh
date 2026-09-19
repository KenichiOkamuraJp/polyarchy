#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Cloudflare Access アプリ（Managed OAuth＝Claude コネクタ用の OAuth 認可サーバ）を冪等に作る／確認する
#
#   bash access-oauth.sh status                     … アプリ・ポリシー・OAuth 設定・AUD を表示（読み取りのみ）
#   bash access-oauth.sh apply                      … アプリを作成/更新（Managed OAuth ON・DCR 許可・許可メール一覧）
#   bash access-oauth.sh remove                     … 本スクリプトが作ったアプリ（APP_NAME）を削除
#   bash access-oauth.sh discovery                  … 公開ホスト越しに OAuth メタデータを取得して確認（curl）
#
# 設計（案 A＝Cloudflare Access の OAuth。2026-09-02 に不採用・資産温存）：
#   - 1 アプリに複数ホスト（例 stats.<domain>・mcp.<domain>）＝同一ポリシー・同一 AUD。dev は別アプリ（APP_NAME=polyarchy-dev）。
#   - Managed OAuth ON・Dynamic Client Registration 許可（Claude＝https://claude.ai/api/mcp/auth_callback・Claude Code＝loopback）。
#   - ポリシー＝Allow 許可メール一覧（ALLOW_EMAILS）。将来 Stripe webhook からここを更新する。
#   - IdP は Zero Trust 側の設定（One-time PIN は既定で有効・Google は Zero Trust → Settings → Authentication で追加）。
#   - origin 側は AUD（本スクリプトの status で表示）を SSM access_aud_<service> に登録し bootstrap が env に書く。
#
# 環境変数：
#   CF_API_TOKEN（または Keychain 'cloudflare-access-token'）… 必要権限 Account → Access: Apps and Policies: Edit（＋Zone Read）
#   APP_NAME       既定 polyarchy           HOSTS  既定 "stats.polyarchy.net,recommendations.polyarchy.net"（カンマ区切り）
#   ALLOW_EMAILS   必須（apply 時・カンマ区切り）  SESSION 既定 24h   TOKEN_LIFETIME 既定 10m
#   ALLOWED_URIS   既定 https://claude.ai/api/mcp/auth_callback
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

export APP_NAME="${APP_NAME:-polyarchy}"
export HOSTS="${HOSTS:-stats.polyarchy.net,recommendations.polyarchy.net}"
export ALLOW_EMAILS="${ALLOW_EMAILS:-}"
SESSION="${SESSION:-24h}"
TOKEN_LIFETIME="${TOKEN_LIFETIME:-10m}"
export ALLOWED_URIS="${ALLOWED_URIS:-https://claude.ai/api/mcp/auth_callback}"
# 共通（log/warn/die・トークン取得・cf）は cf-lib.sh。既定トークン＝Keychain cloudflare-access-token（Access:Edit）。
LOG_TAG=access
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cf-lib.sh"
PY="${PY:-python3}"
jq_() { "$PY" -c "import sys,json; d=json.load(sys.stdin); $1"; }

ZONE_NAME="${ZONE_NAME:-polyarchy.net}"
account_id() { # /accounts が空（Account Settings:Read 無し）ならゾーンの account.id で代替（Zone:Read）
  local id; id="$(cf GET /accounts | jq_ 'r=d.get("result") or []; print(r[0]["id"] if r else "")')"
  [[ -n "$id" ]] || id="$(cf GET "/zones?name=$ZONE_NAME" | jq_ 'r=d.get("result") or []; print(r[0]["account"]["id"] if r else "")')"
  [[ -n "$id" ]] || die "アカウント ID を取得できない（トークンに Zone:Read か Account Settings:Read が要る）"
  printf '%s' "$id"
}
find_app() { # → app JSON or empty
  cf GET "/accounts/$1/access/apps?per_page=1000" | jq_ '
import os
apps=[a for a in d.get("result",[]) if a.get("name")==os.environ["APP_NAME"]]
print(json.dumps(apps[0]) if apps else "")'
}

hosts_json() { "$PY" -c "import json,os; print(json.dumps([h.strip() for h in os.environ['HOSTS'].split(',') if h.strip()]))"; }
emails_json() { "$PY" -c "import json,os; print(json.dumps([{'email':{'email':e.strip()}} for e in os.environ['ALLOW_EMAILS'].split(',') if e.strip()]))"; }
uris_json() { "$PY" -c "import json,os; print(json.dumps([u.strip() for u in os.environ['ALLOWED_URIS'].split(',') if u.strip()]))"; }

app_body() {
  local hosts; hosts="$(hosts_json)"
  local first; first="$("$PY" -c "import json,sys; print(json.loads(sys.argv[1])[0])" "$hosts")"
  cat <<EOF
{
  "name": "${APP_NAME}",
  "type": "self_hosted",
  "domain": "${first}",
  "self_hosted_domains": ${hosts},
  "session_duration": "${SESSION}",
  "auto_redirect_to_identity": false,
  "app_launcher_visible": false,
  "http_only_cookie_attribute": true,
  "policies": [
    {"name": "${APP_NAME}-allow-emails", "decision": "allow", "precedence": 1,
     "include": $(emails_json)}
  ],
  "oauth_configuration": {
    "enabled": true,
    "dynamic_client_registration": {
      "enabled": true,
      "allow_any_on_localhost": true,
      "allow_any_on_loopback": true,
      "allowed_uris": $(uris_json)
    },
    "grant": {"access_token_lifetime": "${TOKEN_LIFETIME}", "session_duration": "${SESSION}"}
  }
}
EOF
}

cmd_status() {
  local acc; acc="$(account_id)"
  local app; app="$(find_app "$acc")"
  if [[ -z "$app" ]]; then log "アプリ '${APP_NAME}' は未作成"; return 0; fi
  printf '%s' "$app" | jq_ '
print("name      :", d.get("name"))
print("id        :", d.get("id"))
print("AUD       :", d.get("aud"), "  ← SSM access_aud_<service> に登録")
print("domains   :", d.get("self_hosted_domains") or [d.get("domain")])
print("session   :", d.get("session_duration"))
oc=d.get("oauth_configuration") or {}
print("oauth     :", json.dumps(oc, ensure_ascii=False))
for p in d.get("policies") or []:
    print("policy    :", p.get("name"), p.get("decision"), json.dumps(p.get("include"), ensure_ascii=False))'
}

cmd_apply() {
  [[ -n "$ALLOW_EMAILS" ]] || die "ALLOW_EMAILS（許可メール・カンマ区切り）が要る"
  local acc; acc="$(account_id)"
  local app; app="$(find_app "$acc")"
  local body; body="$(app_body)"
  local resp
  if [[ -z "$app" ]]; then
    log "作成: ${APP_NAME} hosts=${HOSTS}"
    resp="$(cf POST "/accounts/$acc/access/apps" "$body")"
  else
    local id; id="$(printf '%s' "$app" | jq_ 'print(d["id"])')"
    log "更新: ${APP_NAME} (${id}) hosts=${HOSTS}"
    resp="$(cf PUT "/accounts/$acc/access/apps/$id" "$body")"
  fi
  printf '%s' "$resp" | jq_ '
if not d.get("success"):
    print("失敗:", json.dumps(d.get("errors"), ensure_ascii=False)); sys.exit(1)
r=d["result"]; print("OK id=", r.get("id")); print("AUD=", r.get("aud"))
print("oauth=", json.dumps(r.get("oauth_configuration"), ensure_ascii=False))'
}

cmd_remove() {
  local acc; acc="$(account_id)"
  local app; app="$(find_app "$acc")"
  [[ -n "$app" ]] || { log "アプリ '${APP_NAME}' は無い"; return 0; }
  local id; id="$(printf '%s' "$app" | jq_ 'print(d["id"])')"
  cf DELETE "/accounts/$acc/access/apps/$id" | jq_ 'print("削除:", d.get("success"), d.get("errors"))'
}

cmd_discovery() { # 公開ホスト越しに OAuth メタデータ・401 の形を確認（Anthropic と同じ経路の目視）
  local h; for h in $(echo "$HOSTS" | tr ',' ' '); do
    log "== $h"
    printf 'AS metadata : '; curl -sS -o /dev/null -w '%{http_code}\n' "https://$h/.well-known/oauth-authorization-server"
    curl -sS "https://$h/.well-known/oauth-authorization-server" | "$PY" -c 'import sys,json
try:
    d=json.load(sys.stdin)
    for k in ("issuer","authorization_endpoint","token_endpoint","registration_endpoint","code_challenge_methods_supported","scopes_supported","token_endpoint_auth_methods_supported"):
        print(f"  {k}: {d.get(k)}")
except Exception as e: print("  (JSON でない)", e)'
    printf 'PRM         : '; curl -sS -o /dev/null -w '%{http_code}\n' "https://$h/.well-known/oauth-protected-resource"
    printf 'MCP 401     : '; curl -sS -o /dev/null -D - -X POST "https://$h/mcp" -H 'content-type: application/json' \
      -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
      | grep -i -E '^(HTTP/|www-authenticate|location)' | head -3
  done
}

case "${1:-}" in
  status) cmd_status ;;
  apply) cmd_apply ;;
  remove) cmd_remove ;;
  discovery) cmd_discovery ;;
  *) sed -n '2,25p' "$0"; exit 1 ;;
esac
