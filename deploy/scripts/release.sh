#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# データリリース（ローカルで実行）＝品質ゲート全 PASS のときだけ S3 へ配布しマニフェストを書く。
#
#   使い方: bash deploy/scripts/release.sh <staging|prod>
#
# 工程（一気通貫＝手順を飛ばす余地を作らない・運用設計 §2.4／RUNBOOK §5）:
#   ① 品質ゲート実行（recommendations 4種＋stats 4種＋共通テスト＝9 本。ENABLE_COMPANIES_APP=true なら companies 4種を足して 13 本。qdrant-dev 起動が前提）
#   ② 全 PASS を機械判定（1つでも FAIL なら upload せず終了＝箱には何も起きない）
#   ③ qdrant-dev を止めて upload_to_s3.sh（データ転送。転送失敗でもマニフェストは書かれない）
#   ④ リリースマニフェスト release/data.json を最後に書く（→ 箱の polyarchy-dataapply.timer が
#      15 分以内に検知して自動適用＝コード tar の再展開も含む・smoke FAIL なら自動切り戻し・人手の箱操作ゼロ・全て記録経路）
#
# 合否の物差し（アンカー・正典＝ルート README「品質の担保」。改定時は env で上書き）:
#   REQUIRE_HIT5（既定 86.2）・REQUIRE_MRR（既定 0.713）＝非劣化条件（>=）
#   （2026-09-08 B19 遡及拡充で 86.9/0.715 から改定＝README 注を参照）
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
ENV_NAME="${1:?使い方: bash release.sh <staging|prod>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$SCRIPT_DIR/../env/${ENV_NAME}.env"
[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE が無い" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"
REQUIRE_HIT5="${REQUIRE_HIT5:-86.2}"
REQUIRE_MRR="${REQUIRE_MRR:-0.713}"
GATE_LOG="$(mktemp -t polyarchy-release-gates.XXXXXX)"
cd "$REPO_DIR"

fail() { echo "❌ リリース中止: $*（ログ: $GATE_LOG）" >&2; exit 1; }
say()  { echo "── $*"; }

say "① 品質ゲート（qdrant-dev 起動が前提・全出力→$GATE_LOG）"
curl -fsS -m 3 http://localhost:6333/collections >/dev/null || fail "qdrant-dev が起動していない（docker start qdrant-dev）"

say "  [1/9] retrieval アンカー（--eval-set both）"
python -m recommendations.eval.eval --retrieval-only --eval-set both >>"$GATE_LOG" 2>&1 || fail "retrieval eval が異常終了"
ALL_LINE="$(grep -E '^ALL[[:space:]]' "$GATE_LOG" | tail -1)"
[[ -n "$ALL_LINE" ]] || fail "retrieval の ALL 行が見つからない"
HIT5="$(echo "$ALL_LINE" | grep -oE '[0-9]+\.[0-9]+%' | head -1 | tr -d '%')"
MRR="$(echo "$ALL_LINE" | grep -oE '0\.[0-9]+' | tail -1)"
python3 -c "import sys; sys.exit(0 if float('$HIT5')>=float('$REQUIRE_HIT5') and float('$MRR')>=float('$REQUIRE_MRR') else 1)" \
  || fail "retrieval アンカー劣化: hit@5 ${HIT5}%（要 ${REQUIRE_HIT5}）/ MRR ${MRR}（要 ${REQUIRE_MRR}）"
echo "      hit@5 ${HIT5}% / MRR ${MRR} ✓"

say "  [2/9] filter-eval"
python -m recommendations.eval.eval --filter-eval >>"$GATE_LOG" 2>&1 || fail "filter-eval が異常終了"
grep -q "フィルタ 27/27" "$GATE_LOG" || fail "フィルタ 27/27 が確認できない"

say "  [3/9] multistage"
python -m recommendations.eval.multistage_eval >>"$GATE_LOG" 2>&1 || fail "multistage_eval が異常終了"
grep -q "12/12" "$GATE_LOG" || fail "集約棄却 12/12 が確認できない"

say "  [4/9] recommendations mcp_smoke"
python -m recommendations.eval.mcp_smoke >>"$GATE_LOG" 2>&1 || fail "recommendations mcp_smoke FAIL"
grep -q "総合: PASS" "$GATE_LOG" || fail "recommendations mcp_smoke の PASS 表示が無い"

say "  [5-8/9] stats 4 ゲート（exit code が合否）"
python -m stats.eval.mcp_smoke     >>"$GATE_LOG" 2>&1 || fail "stats mcp_smoke FAIL"
python -m stats.eval.exact_match   >>"$GATE_LOG" 2>&1 || fail "stats exact_match FAIL"
python -m stats.eval.test_core     >>"$GATE_LOG" 2>&1 || fail "stats test_core FAIL"
python -m stats.eval.find_quality  >>"$GATE_LOG" 2>&1 || fail "stats find_quality FAIL"

say "  [9/9] 共通契約テスト"
python -m polyarchy_common.tests.test_common >>"$GATE_LOG" 2>&1 || fail "polyarchy_common tests FAIL"

# companies（企業情報DB）は opt-in＝env の ENABLE_COMPANIES_APP=true のときだけゲートに入る（使わない導入団体は 9 本のまま）。
# 有効なのに値の置き場が無ければ中止する（黙って省かない＝箱の companies が空になる配布を出さない）。
COMPANIES_GATES="対象外"; COMPANIES_N=0
if [[ "${ENABLE_COMPANIES_APP:-false}" == "true" ]]; then
  [[ -f companies/data/store/companies.json ]] || fail "ENABLE_COMPANIES_APP=true だが companies/data/store が無い（取込 or S3 から復元＝RUNBOOK §3）"
  say "  [10-13/13] companies 4 ゲート（exit code が合否）"
  python -m companies.eval.mcp_smoke    >>"$GATE_LOG" 2>&1 || fail "companies mcp_smoke FAIL"
  python -m companies.eval.exact_match  >>"$GATE_LOG" 2>&1 || fail "companies exact_match FAIL"
  python -m companies.eval.test_core    >>"$GATE_LOG" 2>&1 || fail "companies test_core FAIL"
  python -m companies.eval.find_quality >>"$GATE_LOG" 2>&1 || fail "companies find_quality FAIL"
  COMPANIES_GATES="4/4 PASS"
  COMPANIES_N="$(python3 -c "import json; print(len(json.load(open('companies/data/store/companies.json'))))")"
fi
echo "② 全ゲート PASS ✅"

say "③ 配布（qdrant-dev 停止 → upload → 再開）"
# バケット名＝環境変数 BUCKET があれば優先（tfstate を持たない運用者用＝RUNBOOK_OPS §5）。無ければ terraform output。
BUCKET_NAME="${BUCKET:-}"
if [[ -z "$BUCKET_NAME" ]]; then
  BUCKET_NAME="$(cd "$SCRIPT_DIR/../terraform" && AWS_PROFILE="$AWS_PROFILE" terraform output -raw s3_bucket 2>/dev/null)" \
    || BUCKET_NAME=""
fi
[[ -n "$BUCKET_NAME" ]] || fail "S3 バケット名を取得できない（環境変数 BUCKET を指定するか、terraform の state を用意する）"
docker stop qdrant-dev >/dev/null
UPLOAD_OK=0
BUCKET="$BUCKET_NAME" PROFILE="$AWS_PROFILE" REGION="$AWS_REGION" bash "$SCRIPT_DIR/upload_to_s3.sh" && UPLOAD_OK=1
docker start qdrant-dev >/dev/null
[[ "$UPLOAD_OK" == 1 ]] || fail "upload_to_s3.sh 失敗（マニフェストは書かない＝箱は動かない）"

say "④ リリースマニフェスト（これが箱の自動適用トリガ）"
GITV="$(git -C "$REPO_DIR" describe --tags --always --dirty 2>/dev/null || echo unknown)"
SERIES_N="$(wc -l < stats/data/registry/series.jsonl | tr -d ' ')"
DOCS_N="$(($(wc -l < recommendations/data/catalog.csv | tr -d ' ') - 1))"
MANIFEST="$(mktemp -t polyarchy-release.XXXXXX.json)"
python3 - "$MANIFEST" <<PYEOF
import json, sys, datetime
json.dump({
  "released_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
  "code_version": "$GITV",
  "gates": {"hit5_pct": float("$HIT5"), "mrr": float("$MRR"),
             "filter": "27/27", "multistage": "12/12", "smoke": "PASS",
             "stats": "4/4 PASS", "common": "PASS", "companies": "$COMPANIES_GATES"},
  "scale": {"stats_series": int("$SERIES_N"), "recommendations_docs": int("$DOCS_N"), "companies": int("$COMPANIES_N")},
  "released_by": "release.sh（ゲート全 PASS 時のみ本ファイルが書かれる）",
}, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
PYEOF
aws s3 cp "$MANIFEST" "s3://$BUCKET_NAME/release/data.json" --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --content-type "application/json; charset=utf-8"
echo "✅ リリース完了: release/data.json（$GITV・hit@5 ${HIT5}%）＝箱が 15 分以内に自動適用 → ダッシュボードで版一致を確認"
