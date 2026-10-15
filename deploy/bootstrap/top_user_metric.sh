#!/bin/bash
# 直近 1 時間の最多の 1 人の呼び出しの数（B44 第 1 便・2026-10-15・5 分ごと・polyarchy-topuser.timer から・User=polyarchy）。
# 設置＝bootstrap ⑪ が /usr/local/bin/polyarchy-top-user へ。数えるのは polyarchy_common.top_user（全サービスの捕捉ログを合わせて
# 利用者ごとに数える）。ここは数をメトリクス top_user_calls_1h にして送るだけ。★利用者のハッシュは出さない・送らない。
# 数えるのが落ちたら送らずに非 0 で終わる＝unit が failed になり知らせが出る（止まったまま「静か」に見えない）。
set -u
source "${POLYARCHY_DEPLOY_ENV:-/etc/polyarchy/deploy.env}"
REPO_DIR="${REPO_DIR:-/opt/polyarchy/polyarchy}"
PY="${POLYARCHY_TOPUSER_PY:-/opt/miniconda/envs/polyarchy/bin/python}"
cd "$REPO_DIR" || exit 1
ARGS=()
for svc in recommendations stats companies deliberations; do   # 捕捉ログを持つサービス（無いファイルは 0 件）
  ARGS+=(--log "$REPO_DIR/$svc/data/query_log/queries.jsonl")
done
OUT="$(PYTHONPATH="$REPO_DIR" "$PY" -m polyarchy_common.top_user "${ARGS[@]}")" || { echo "[top-user] 数えるのが落ちた" >&2; exit 1; }
N="$(awk '$1 == "top_user" {print $2}' <<< "$OUT")"
[[ "$N" =~ ^[0-9]+$ ]] || { echo "[top-user] 数が読めない" >&2; exit 1; }
echo "[top-user] 直近 1 時間の最多の 1 人＝$N 回"
aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace polyarchy \
  --metric-data "MetricName=top_user_calls_1h,Dimensions=[{Name=service,Value=box}],Value=$N,Unit=Count"
