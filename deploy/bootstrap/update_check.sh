#!/bin/bash
# 更新チェック（毎日・polyarchy-updatecheck.timer から。手動キック＝SSM ドキュメント polyarchy-<env>-check-updates）。
# 内容＝①stats freshness（取得元の Last-Modified/ETag 変化検知・--json を保存）②提言の新着チェック
# （目次走査・catalog 未収録を数える＝ダウンロードなし）③ダッシュボード再生成（結果を即時反映）。
# ★チェックのみ＝取込は実行しない（品質ゲートを通す RUNBOOK §5 が唯一の取込経路）。
set -euo pipefail
source /etc/polyarchy/deploy.env
cd "$REPO_DIR"
PY=/opt/miniconda/envs/polyarchy/bin/python
export PYTHONPATH="$REPO_DIR"
# 出力先（cache/ は tar にも S3 にも無い＝箱で作る）。e-Stat の appId は SSM estat_app_id → /etc/polyarchy/estat.env（bootstrap ⑩）。
mkdir -p stats/data/cache recommendations/data/cache
if [[ -f /etc/polyarchy/estat.env ]]; then set -a; source /etc/polyarchy/estat.env; set +a; fi
[[ -n "${ESTAT_APP_ID:-}" ]] || echo "[update-check] ⚠ ESTAT_APP_ID なし＝e-Stat 系の更新は検知できない（SSM estat_app_id を登録）" >&2
echo "[update-check] ① stats freshness"
# freshness の終了コード＝0 変化なし／1 **更新あり**（正常）／2 取得失敗。1 を失敗扱いしない（2026-09-03 修正）。
set +e; "$PY" -m stats.ops.freshness --json > stats/data/cache/freshness_last_run.json; RC=$?; set -e
case "$RC" in 0) echo "[update-check] freshness: 変化なし";; 1) echo "[update-check] freshness: 更新あり（ダッシュボード③参照）";;
  *) echo "[update-check] ⚠ freshness 取得失敗 rc=$RC（best-effort・続行）" >&2;; esac
echo "[update-check] ② 提言の新着チェック"
"$PY" -m recommendations.ingest.check_new \
  || echo "[update-check] ⚠ check_new 失敗（best-effort・続行）" >&2
echo "[update-check] ③ ダッシュボード再生成"
/usr/local/bin/polyarchy-dashboard
echo "[update-check] 完了 $(date -Is)"
