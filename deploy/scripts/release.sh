#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# データリリース（ローカルで実行）＝品質ゲート全 PASS のときだけ S3 へ配布しマニフェストを書く。
#
#   使い方: bash deploy/scripts/release.sh <staging|prod>
#
# 工程（一気通貫＝手順を飛ばす余地を作らない・運用設計 §2.4／RUNBOOK §5）:
#   ⓪ 配る前の確認（release_preflight.sh）＝qdrant のマウント元・AWS プロファイル・コレクション名・調整値の混入・
#      ロックとの一致・BM25 語彙と索引・コードの範囲の未コミット（測るもの＝配るもの を機械で確かめる）
#   ① 品質ゲート実行（一覧と基準はルート README「品質の担保」＝本数をここに写さない。companies・deliberations は env の
#      ENABLE_COMPANIES_APP／ENABLE_DELIBERATIONS_APP=true のときだけ〔deliberations は束の書き出しも〕。qdrant-dev〔と審議会DB の qdrant-delib〕起動が前提）
#   ② 全 PASS を機械判定（アンカーは ALL 行の数値と基準値・それ以外は各ゲートの終了コード。1つでも FAIL なら upload せず終了＝箱には何も起きない）
#   ③ qdrant-dev を止めて upload_to_s3.sh（データ転送。転送失敗でもマニフェストは書かれない）
#   ④ リリースマニフェスト release/data.json を最後に書く（→ 箱の polyarchy-dataapply.timer が
#      15 分以内に検知して自動適用＝コード tar の再展開も含む・smoke FAIL なら自動切り戻し・人手の箱操作ゼロ・全て記録経路）
#
# 合否の物差し（アンカー・正典＝ルート README「品質の担保」。改定時は env で上書き）:
#   REQUIRE_HIT5（既定 86.3）・REQUIRE_MRR（既定 0.707）＝非劣化条件（>=）。改定の経緯は README「品質の担保」から辿る（ここに写さない）。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
ENV_NAME="${1:?使い方: bash release.sh <staging|prod>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$SCRIPT_DIR/../env/${ENV_NAME}.env"
[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE が無い" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"
REQUIRE_HIT5="${REQUIRE_HIT5:-86.3}"
REQUIRE_MRR="${REQUIRE_MRR:-0.707}"
GATE_LOG="$(mktemp -t polyarchy-release-gates.XXXXXX)"
cd "$REPO_DIR"

fail() { echo "❌ リリース中止: $*（ログ: ${GATE_LOG}）" >&2; exit 1; }
say()  { echo "── $*"; }

# shellcheck source=release_preflight.sh
source "$SCRIPT_DIR/release_preflight.sh"
preflight || fail "配る前の確認で中止（上の ❌ を直して再実行）"   # COLLECTION_NAME をこのシェルに export する

say "① 品質ゲート（qdrant-dev 起動が前提・全出力→${GATE_LOG}）"
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

RETRIEVAL_CFG="$(grep -E '^\[retrieval-only\]' "$GATE_LOG" | tail -1 || true)"   # 測った構成（collection・pool・rerank）＝マニフェストへ（無くても止めない）

# アンカー（上の [1/9]・審議会DB の retrieval）以外の合否は終了コード（出力の文字列を grep しない＝累積ログの別の行に当たって
# FAIL を見逃す・2026-10-09）。アンカーは ALL 行の数値を基準値と比べる（数値の比較は終了コードでは表せない）。
say "  [2/9] filter-eval（全問＋層ゲート PASS で 0）"
python -m recommendations.eval.eval --filter-eval >>"$GATE_LOG" 2>&1 || fail "filter-eval FAIL（フィルタ全問＋層ゲート）"

say "  [3/9] multistage（網羅・集約棄却・答え可能が全問で 0）"
python -m recommendations.eval.multistage_eval >>"$GATE_LOG" 2>&1 || fail "multistage_eval FAIL"

say "  [4/9] recommendations mcp_smoke"
python -m recommendations.eval.mcp_smoke >>"$GATE_LOG" 2>&1 || fail "recommendations mcp_smoke FAIL"

say "  [5-8/9] stats 4 ゲート（exit code が合否）"
python -m stats.eval.mcp_smoke     >>"$GATE_LOG" 2>&1 || fail "stats mcp_smoke FAIL"
python -m stats.eval.exact_match   >>"$GATE_LOG" 2>&1 || fail "stats exact_match FAIL"
STATS_TEST_STRICT=1 python -m stats.eval.test_core >>"$GATE_LOG" 2>&1 || fail "stats test_core FAIL（STRICT＝値ストア依存の検査のスキップも FAIL）"
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
# deliberations（審議会議事録DB）も opt-in＝env の ENABLE_DELIBERATIONS_APP=true のときだけ 4 ゲート＋束の書き出し。
# 手元の専用 Qdrant（qdrant-delib・:6340）と解析結果（deliberations/data/cache）が前提。アンカーは非劣化（>=）。
DELIB_GATES="対象外"; DELIB_POINTS=0
if [[ "${ENABLE_DELIBERATIONS_APP:-false}" == "true" ]]; then
  DELIB_URL="${DELIB_QDRANT_URL:-http://localhost:6340}"
  curl -fsS -m 3 "$DELIB_URL/collections" >/dev/null || fail "ENABLE_DELIBERATIONS_APP=true だが審議会DB の Qdrant（${DELIB_URL}）が応答しない（docker start qdrant-delib）"
  [[ -f deliberations/data/cache/units.jsonl ]] || fail "ENABLE_DELIBERATIONS_APP=true だが deliberations/data/cache が無い（deliberations/CLAUDE.md の手順で収集・解析・取り込み）"
  DELIB_REQUIRE_HIT5="${DELIB_REQUIRE_HIT5:-94.7}"; DELIB_REQUIRE_MRR="${DELIB_REQUIRE_MRR:-0.856}"
  say "  [+4] deliberations 4 ゲート（アンカー非劣化 hit@5>=${DELIB_REQUIRE_HIT5}・MRR>=${DELIB_REQUIRE_MRR}・帰属・層・スモーク）"
  python -m deliberations.eval.retrieval >>"$GATE_LOG" 2>&1 || fail "deliberations retrieval が異常終了"
  D_LINE="$(grep -E '^ALL[[:space:]]' "$GATE_LOG" | tail -1)"
  D_HIT5="$(awk '{print $3}' <<<"$D_LINE" | tr -d '%')"; D_MRR="$(awk '{print $4}' <<<"$D_LINE")"
  python3 -c "import sys; sys.exit(0 if float('$D_HIT5')>=float('$DELIB_REQUIRE_HIT5') and float('$D_MRR')>=float('$DELIB_REQUIRE_MRR') else 1)" \
    || fail "deliberations アンカー劣化（hit@5 ${D_HIT5}%・MRR ${D_MRR}＜基準 ${DELIB_REQUIRE_HIT5}%・${DELIB_REQUIRE_MRR}）"
  python -m deliberations.eval.attribution_gate >>"$GATE_LOG" 2>&1 || fail "deliberations 帰属ゲート FAIL"
  python -m deliberations.eval.layer_gate       >>"$GATE_LOG" 2>&1 || fail "deliberations 層ゲート FAIL"
  python -m deliberations.eval.mcp_smoke        >>"$GATE_LOG" 2>&1 || fail "deliberations mcp_smoke FAIL"
  python -m deliberations.ops.bundle export --url "$DELIB_URL" >>"$GATE_LOG" 2>&1 || fail "deliberations の束の書き出しに失敗"
  DELIB_GATES="4/4 PASS（hit@5 ${D_HIT5}%・MRR ${D_MRR}）"
  DELIB_POINTS="$(python3 -c "import json; print(json.load(open('deliberations/data/bundle/deliberations_v1/bundle.json'))['points'])")"
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
GITV="$(git -C "$REPO_DIR" describe --tags --always 2>/dev/null || echo unknown)"   # tar＝HEAD（コードの未コミットは配る前の確認で止まる）＝VERSION と同じ刻み
SERIES_N="$(wc -l < stats/data/registry/series.jsonl | tr -d ' ')"
DOCS_N="$(($(wc -l < recommendations/data/catalog.csv | tr -d ' ') - 1))"
MANIFEST="$(mktemp -t polyarchy-release.XXXXXX.json)"
RETRIEVAL_CFG="$RETRIEVAL_CFG" python3 - "$MANIFEST" <<PYEOF
import json, os, sys, datetime
json.dump({
  "released_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
  "code_version": "$GITV",
  "gates": {"hit5_pct": float("$HIT5"), "mrr": float("$MRR"),
             "filter": "PASS", "multistage": "PASS", "smoke": "PASS",
             "stats": "4/4 PASS", "common": "PASS", "companies": "$COMPANIES_GATES", "deliberations": "$DELIB_GATES"},
  "scale": {"stats_series": int("$SERIES_N"), "recommendations_docs": int("$DOCS_N"), "companies": int("$COMPANIES_N"),
            "deliberations_points": int("$DELIB_POINTS")},
  "measured": {"retrieval": os.environ.get("RETRIEVAL_CFG", ""), "collection": "$COLLECTION_NAME", "lock": "match",
               "qdrant_mount": "recommendations/data/qdrant（配布するフォルダの・照合済み）"},
  "released_by": "release.sh（配る前の確認とゲート全 PASS 時のみ本ファイルが書かれる）",
}, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
PYEOF
aws s3 cp "$MANIFEST" "s3://$BUCKET_NAME/release/data.json" --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --content-type "application/json; charset=utf-8"
echo "✅ リリース完了: release/data.json（${GITV}・hit@5 ${HIT5}%）＝箱が 15 分以内に自動適用 → ダッシュボードで版一致を確認"
