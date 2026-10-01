#!/bin/bash
# 捕捉ログ（燃料）の S3 保護コピー（polyarchy-fuelsync.timer から・User=polyarchy）。設置＝bootstrap ⑩ が /usr/local/bin/polyarchy-fuelsync へ。
# サービスごとに同期し、結果を ops/dashboard/fuelsync/ に残す（運用ダッシュボード①が「最終同期」として読む＝数字・状態のみ）。
#   無いフォルダ（そのサービスを有効にしていない箱）は飛ばす＝旧 unit の「-ExecStart（失敗を無視）」の理由はこれだけだった。
#   同期の失敗は他のサービスの同期を止めず、最後に非 0 で終わる＝unit が failed になり journal に残る（2026-10-01＝失敗が見えなかった）。
# --delete は付けない（EBS が主・S3 は保護コピー）。s3_mirror/ は手元の usage_report のミラー＝S3 へ戻さない（2026-09-03）。
set -u
source /etc/polyarchy/deploy.env
REPO_DIR="${REPO_DIR:-/opt/polyarchy/polyarchy}"
STATE="$REPO_DIR/ops/dashboard/fuelsync"
mkdir -p "$STATE"
rc=0
sync_one() { # $1=サービス名 $2=ローカルの query_log $3=S3 の prefix の後ろ
  if [[ ! -d "$2" ]]; then
    echo absent > "$STATE/$1.result"
    return
  fi
  if /usr/local/bin/aws s3 sync "$2" "s3://${S3_BUCKET}/${DATA_S3_PREFIX}/$3" --region "${AWS_REGION}" --exclude "s3_mirror/*" --only-show-errors; then
    echo ok > "$STATE/$1.result"
    date -Is > "$STATE/$1.last_ok"
  else
    echo failed > "$STATE/$1.result"
    echo "[fuelsync] $1 の同期に失敗" >&2
    rc=1
  fi
}
sync_one recommendations "$REPO_DIR/recommendations/data/query_log" query_log
sync_one stats "$REPO_DIR/stats/data/query_log" stats/query_log
sync_one companies "$REPO_DIR/companies/data/query_log" companies/query_log
date -Is > "$STATE/last_run"
exit $rc
