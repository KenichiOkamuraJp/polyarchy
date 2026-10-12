#!/bin/bash
# /healthz を localhost で叩き CloudWatch（namespace polyarchy）へ 1/0 を送る（運用設計 §1.1）。
# あわせてルートディスク使用率も送る（CW agent の metrics 設定を持たない分をここで補完）。
# polyarchy-health.timer（毎分）から root で実行。資格情報はインスタンスロール。
# ★このスクリプトが壊れると health が全部 missing になり、全サービスのアラームが鳴る＝生き死に（＋ディスク）の送信は
#   そのままにし、足した分（メモリ・再起動・2026-10-12）は別の送信に分ける。取れない値は送らない（0 を送ると誤って鳴る）。
#   `test_common` が偽のコマンドで通しで実行する（下の POLYARCHY_* はそのための差し替え口＝箱では既定のまま）。
set -u
source "${POLYARCHY_DEPLOY_ENV:-/etc/polyarchy/deploy.env}"

probe() { # $1=port → 1/0
  curl -fsS -m 5 "http://127.0.0.1:$1/healthz" >/dev/null 2>&1 && echo 1 || echo 0
}

DATA="MetricName=health,Dimensions=[{Name=service,Value=mcp}],Value=$(probe 8765),Unit=None"
if [[ "${ENABLE_STATS_APP:-false}" == "true" ]]; then
  DATA="$DATA MetricName=health,Dimensions=[{Name=service,Value=stats}],Value=$(probe 8766),Unit=None"
fi
if [[ "${ENABLE_COMPANIES_APP:-false}" == "true" ]]; then
  DATA="$DATA MetricName=health,Dimensions=[{Name=service,Value=companies}],Value=$(probe 8767),Unit=None"
fi
if [[ "${ENABLE_DELIBERATIONS_APP:-false}" == "true" ]]; then
  DATA="$DATA MetricName=health,Dimensions=[{Name=service,Value=deliberations}],Value=$(probe 8768),Unit=None"
fi
DISK=$(df --output=pcent / | tail -1 | tr -dc '0-9')
DATA="$DATA MetricName=disk_used_percent,Dimensions=[{Name=service,Value=box}],Value=${DISK:-0},Unit=Percent"

# shellcheck disable=SC2086  # DATA はスペース区切りで複数エントリを渡す（値に空白は含まれない）
aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace polyarchy --metric-data $DATA

# 箱全体の空きメモリ（MemAvailable／MemTotal＝回収できるファイルキャッシュは空きに数える）。
EXTRA=""
MEM=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{if (t > 0 && a != "") printf "%d", 100 * a / t}' "${POLYARCHY_MEMINFO:-/proc/meminfo}" 2>/dev/null)
if [[ -n "$MEM" ]]; then
  EXTRA="MetricName=mem_available_percent,Dimensions=[{Name=service,Value=box}],Value=$MEM,Unit=Percent"
fi
# 常駐のユニットが自分で落ちて systemd に上げ直された回数（NRestarts の合計）の、前の分からの増分。Restart=always は数秒で
# 戻す＝health（3 分連続 0）にはかからない。systemctl restart（配布・手作業）は自動の再起動に数えない＝合計が減ったときは 0 を送る。
# 初回（再起動の直後・状態ファイルなし）も 0。ユニットは名前の型で拾う＝サービスを足しても一覧を足さない。
STATE="${POLYARCHY_RESTART_STATE:-/run/polyarchy-health-restarts}"
NOW=$(systemctl show 'polyarchy-*.service' 'qdrant*.service' cloudflared.service -p NRestarts --value 2>/dev/null | awk '/^[0-9]+$/{s+=$1} END{print s+0}')
PREV=$(cat "$STATE" 2>/dev/null)
[[ "$PREV" =~ ^[0-9]+$ ]] || PREV="$NOW"
DELTA=$(( NOW > PREV ? NOW - PREV : 0 ))
echo "$NOW" > "$STATE"
EXTRA="$EXTRA MetricName=service_restarts,Dimensions=[{Name=service,Value=box}],Value=$DELTA,Unit=Count"
# shellcheck disable=SC2086
aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace polyarchy --metric-data $EXTRA
