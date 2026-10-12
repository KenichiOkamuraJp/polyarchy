#!/bin/bash
# 捕捉ログの保持期間超過分の削除（polyarchy-logprune.timer から・User=polyarchy）。設置＝bootstrap ⑩ が /usr/local/bin/polyarchy-logprune へ。
# サービスごとに deploy/scripts/prune_query_log.py を呼ぶ（保持日数は unit の LOG_RETENTION_DAYS）。ログの無いサービス
# （有効にしていない箱）は prune 側が「対象なし」で 0 を返す。1 つの失敗は残りを止めず、最後に非 0 で終わる＝unit が failed に
# なり知らせが出る（プライバシーポリシーの「30 日で自動削除」が黙って外れない・2026-10-12）。
# ★サービスの一覧は polyarchy_common.usage_report の LOG_DIRS が正＝`test_common` が突き合わせる。
set -u
REPO_DIR="${REPO_DIR:-/opt/polyarchy/polyarchy}"
PY="${PY:-/opt/miniconda/envs/polyarchy/bin/python}"
rc=0
prune_one() { # $1=サービス名 $2=ローカルの query_log
  if ! POLYARCHY_QUERY_LOG="$2/queries.jsonl" "$PY" "$REPO_DIR/deploy/scripts/prune_query_log.py"; then
    echo "[logprune] $1 の削除に失敗" >&2
    rc=1
  fi
}
prune_one recommendations "$REPO_DIR/recommendations/data/query_log"
prune_one stats "$REPO_DIR/stats/data/query_log"
prune_one companies "$REPO_DIR/companies/data/query_log"
prune_one deliberations "$REPO_DIR/deliberations/data/query_log"
exit $rc
