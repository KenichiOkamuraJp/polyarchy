#!/bin/bash
# 外からの到達の確認（5 分ごと・polyarchy-edgeprobe.timer から・User=polyarchy）。設置＝bootstrap ⑪ が /usr/local/bin/polyarchy-edge-probe へ。
# 確認の中身は polyarchy_common.edge_probe（公開 URL の /healthz・守るパスの 401・ディスカバリの照合・IdP の公開鍵＝資格情報なし）。
# ここは結果の行（`edge <サービス> <1|0>`・`idp <1|0>`）をメトリクスにして送るだけ。結果が 1 行も無い箱（公開していない）は送らない。
# 確認そのものが落ちたら非 0 で終わる＝unit が failed になり知らせが出る（確認が止まったまま「異常なし」に見えない）。
# ★パスは出さない（秘密パスの構成では合言葉）＝失敗した確認の名前は運用ダッシュボード①「外からの確認」。
set -u
source "${POLYARCHY_DEPLOY_ENV:-/etc/polyarchy/deploy.env}"
REPO_DIR="${REPO_DIR:-/opt/polyarchy/polyarchy}"
PY="${POLYARCHY_EDGE_PY:-/opt/miniconda/envs/polyarchy/bin/python}"
cd "$REPO_DIR" || exit 1
LINES_OUT="$(PYTHONPATH="$REPO_DIR" "$PY" -m polyarchy_common.edge_probe)" || { echo "[edge-probe] 確認が落ちた" >&2; exit 1; }
DATA=""
while read -r kind a b; do
  case "$kind" in
    edge) [[ "$a" =~ ^[a-z]+$ && "$b" =~ ^[01]$ ]] && DATA="$DATA MetricName=edge,Dimensions=[{Name=service,Value=$a}],Value=$b,Unit=None" ;;
    idp)  [[ "$a" =~ ^[01]$ ]] && DATA="$DATA MetricName=idp_reachable,Dimensions=[{Name=service,Value=box}],Value=$a,Unit=None" ;;
  esac
done <<< "$LINES_OUT"
if [[ -z "$DATA" ]]; then
  echo "[edge-probe] 確認の対象なし（公開していない箱）"
  exit 0
fi
echo "[edge-probe] $(echo "$LINES_OUT" | tr '\n' ' ')"
# shellcheck disable=SC2086  # DATA はスペース区切りで複数エントリを渡す（値に空白は含まれない）
aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace polyarchy --metric-data $DATA
