#!/bin/bash
# /healthz を localhost で叩き CloudWatch（namespace polyarchy）へ 1/0 を送る（運用設計 §1.1）。
# あわせてルートディスク使用率も送る（CW agent の metrics 設定を持たない分をここで補完）。
# polyarchy-health.timer（毎分）から root で実行。資格情報はインスタンスロール。
set -u
source /etc/polyarchy/deploy.env

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
DISK=$(df --output=pcent / | tail -1 | tr -dc '0-9')
DATA="$DATA MetricName=disk_used_percent,Dimensions=[{Name=service,Value=box}],Value=${DISK:-0},Unit=Percent"

# shellcheck disable=SC2086  # DATA はスペース区切りで複数エントリを渡す（値に空白は含まれない）
aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace polyarchy --metric-data $DATA
