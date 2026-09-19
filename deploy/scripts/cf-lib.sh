#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Cloudflare API 共通ライブラリ（cloudflare-guard.sh / access-oauth.sh が source する）。
#   - トークン取得（env CF_API_TOKEN → macOS Keychain の順。値は表示しない・改行は除去）
#   - cf METHOD PATH [JSON] … curl ラッパ
#   - log / warn / die
# 呼び出し側は source 前に CF_TOKEN_KEYCHAIN_ITEM（既定 cloudflare-access-token）と LOG_TAG（既定 cf）を設定できる。
#
# Keychain のトークン（2026-08-19 時点）：
#   cloudflare-access-token … Access: Apps and Policies Edit・Cloudflare Pages Edit・Account Settings Read・
#                              User Memberships Read・Zone Read・DNS Edit（公式コネクタ化で作成・推奨）
#   cloudflare-api-token    … Zone Read＋WAF Edit（旧・cloudflare-guard.sh の WAF ルール用）
#   → guard は WAF:Edit が要るので旧トークン、access-oauth は新トークン。環境変数で切替可。
# ═══════════════════════════════════════════════════════════════════════════
CF_API="https://api.cloudflare.com/client/v4"
CF_TOKEN_KEYCHAIN_ITEM="${CF_TOKEN_KEYCHAIN_ITEM:-cloudflare-access-token}"
LOG_TAG="${LOG_TAG:-cf}"

log()  { printf '\033[1;34m[%s]\033[0m %s\n'       "$LOG_TAG" "$*" >&2; }
warn() { printf '\033[1;33m[%s:warn]\033[0m %s\n'  "$LOG_TAG" "$*" >&2; }
die()  { printf '\033[1;31m[%s:ERROR]\033[0m %s\n' "$LOG_TAG" "$*" >&2; exit 1; }

cf_get_token() {
  if [[ -n "${CF_API_TOKEN:-}" ]]; then printf '%s' "$CF_API_TOKEN" | tr -d '\r\n'; return; fi
  if command -v security >/dev/null 2>&1; then
    local t; t="$(security find-generic-password -s "$CF_TOKEN_KEYCHAIN_ITEM" -w 2>/dev/null | tr -d '\r\n')"
    [[ -n "$t" ]] && { printf '%s' "$t"; return; }
  fi
  die "CF_API_TOKEN が未設定（Keychain '${CF_TOKEN_KEYCHAIN_ITEM}' も無し）。export CF_API_TOKEN=... か Keychain 登録を。"
}

cf() { # $1=METHOD $2=PATH [$3=JSON body] → レスポンス本文を stdout
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "$method" "${CF_API}${path}" -H "Authorization: Bearer $(cf_get_token)" -H "Content-Type: application/json")
  [[ -n "$body" ]] && args+=(--data "$body")
  curl "${args[@]}"
}
