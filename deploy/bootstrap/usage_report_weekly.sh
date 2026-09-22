#!/bin/bash
# 週次利用レポートの生成と S3 保存（運用設計 §4.2 段2・polyarchy-usagereport.timer から）。
# ★箱では --s3-bucket を付けない（2026-09-03 文書監査 1-1）：S3 の保護コピーは箱自身の fuelsync 先＝同じ内容であり、
#   ミラーすると query_log/s3_mirror/ が生まれ、fuelsync がそれごと S3 へ戻して毎週 1 段深くなる（生ログが 30 日削除の外に残る）。
#   --s3-bucket は手元（Mac）で箱の燃料をマージするときだけ使う。weekly.jsonl は箱で恒久蓄積（tar からも除外）。
set -euo pipefail
source /etc/polyarchy/deploy.env
cd "$REPO_DIR"
PY=/opt/miniconda/envs/polyarchy/bin/python
PYTHONPATH="$REPO_DIR" ENABLE_COMPANIES_APP="${ENABLE_COMPANIES_APP:-false}" "$PY" -m polyarchy_common.usage_report
aws s3 cp ops/usage/usage_report.html "s3://$S3_BUCKET/ops/report/usage_report.html" --region "$AWS_REGION"
aws s3 cp ops/usage/weekly.jsonl "s3://$S3_BUCKET/ops/report/weekly.jsonl" --region "$AWS_REGION"
echo "[usage-report] 生成と S3 保存 完了 $(date -Is)"
