#!/bin/bash
# 定期実行のユニットの失敗を CloudWatch へ送る（polyarchy-unit-failed@.service から・root）。設置＝bootstrap ⑩ が
# /usr/local/bin/polyarchy-unit-failed へ。呼ぶ側＝各ユニットの OnFailure=polyarchy-unit-failed@%p.service（$1＝失敗したユニット名）。
# メトリクスは箱で 1 本（unit_failed{service=box}）＝アラームも 1 本。どのユニットかは、この行（journal・
# /var/log/polyarchy/polyarchy-unit-failed.log）と運用ダッシュボード①「失敗したユニット」で分かる。
set -u
source /etc/polyarchy/deploy.env
echo "[unit-failed] ${1:-不明} が失敗した $(date -Is)"
aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace polyarchy \
  --metric-data "MetricName=unit_failed,Dimensions=[{Name=service,Value=box}],Value=1,Unit=Count"
