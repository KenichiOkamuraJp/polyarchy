#!/bin/bash
# 運用ダッシュボードの生成と S3 保存（毎時・polyarchy-dashboard.timer から）。
# 閲覧＝導入団体側担当者が ReadOnly アカウントで S3 コンソールから開く（docs/導入団体側_監査ガイド.md §4）。
set -euo pipefail
source /etc/polyarchy/deploy.env
cd "$REPO_DIR"
PY=/opt/miniconda/envs/polyarchy/bin/python
PYTHONPATH="$REPO_DIR" ENVIRONMENT="$ENVIRONMENT" ENABLE_COMPANIES_APP="${ENABLE_COMPANIES_APP:-false}" "$PY" -m polyarchy_common.ops_dashboard
aws s3 cp ops/dashboard/index.html "s3://$S3_BUCKET/ops/dashboard/index.html" \
  --content-type "text/html; charset=utf-8" --region "$AWS_REGION"
echo "[dashboard] 生成と S3 保存 完了 $(date -Is)"
