#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# 提言の定型更新（②の前半）を 1 コマンド化＝運用者ロールの台本（運用設計 §0/§2.3）。
#
#   使い方: bash deploy/scripts/update.sh <staging|prod> [--no-release]
#
# 工程（新着検出 → 新着のある団体だけ収集 → 追い判定 → 増分取込 → release.sh）:
#   ① check_new（読み取り専用）＝新着 0 なら何もせず終了
#   ② collect --skip-existing（新着のある団体のみ。keidanren は今年＋昨年を対象）
#   ③ policy_tagger（分野タグ・文書性格の追い判定＝Anthropic API を使用。recommendations/.env に鍵）
#   ④ qdrant_ingest ingest（doc 単位増分・冪等。ローカル qdrant-dev 起動が前提）
#   ⑤ release.sh <env>（ゲート 9 本〔companies 有効時は 13 本〕全 PASS のときだけ配布＝FAIL なら箱には何も起きない）
#
# 安全設計＝失敗の最悪ケースは「リリースが起きない」（fail-closed）。--no-release で⑤の手前まで。
# ★catalog.csv（git 追跡）が変わる＝実行後に差分をレビューしてコミットする（開発者 or 運用者・下記に表示）。
# 対象外＝stats の更新（`stats.ops.refresh` / `stats.ops.qe_update` が既に 1 コマンド）・
#         評価問の追加（週次トリアージ・拡充③b の領分）。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
ENV_NAME="${1:?使い方: bash update.sh <staging|prod> [--no-release]}"
NO_RELEASE="${2:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_DIR"

say()  { echo "── $*"; }
fail() { echo "❌ 更新中止: $*" >&2; exit 1; }

command -v python >/dev/null || fail "python が無い（conda activate polyarchy を先に）"
python -c "import recommendations" 2>/dev/null || fail "recommendations が import できない（conda env を確認）"
curl -fsS -m 3 http://localhost:6333/collections >/dev/null || fail "qdrant-dev が起動していない（docker start qdrant-dev）"

say "① 新着チェック（読み取り専用）"
python -m recommendations.ingest.check_new
UC="$REPO_DIR/recommendations/data/cache/update_check.json"
NEW_ORGS="$(python3 -c "
import json
d = json.load(open('$UC'))
orgs = [k for k, v in d['orgs'].items() if v.get('new', 0) > 0]
errs = [k for k, v in d['orgs'].items() if v.get('error')]
print(' '.join(orgs))
import sys; print(' '.join(errs), file=sys.stderr)")"
if [[ -z "$NEW_ORGS" ]]; then
  echo "✅ 新着なし＝何もしません（チェック失敗団体があれば stderr 参照）"
  exit 0
fi
echo "   新着のある団体: $NEW_ORGS"

say "② 収集（--skip-existing＝既存は再取得しない）"
YEAR="$(date +%Y)"
for org in $NEW_ORGS; do
  if [[ "$org" == "keidanren" ]]; then
    python -m recommendations.ingest.collect keidanren --years "$YEAR" "$((YEAR-1))" --skip-existing
  else
    python -m recommendations.ingest.collect "$org" --skip-existing
  fi
done

say "③ 分野タグ・文書性格の追い判定（対象確認 → 本実行）"
python -m recommendations.ingest.policy_tagger --dry-run
python -m recommendations.ingest.policy_tagger

say "④ 増分取込（doc 単位・冪等）"
COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.qdrant_ingest ingest

echo "★catalog.csv の未コミット差分（レビューしてコミットすること）:"
git -C "$REPO_DIR" diff --stat -- recommendations/data/catalog.csv || true

if [[ "$NO_RELEASE" == "--no-release" ]]; then
  echo "⏸ --no-release 指定＝ここで停止（配布するときは bash deploy/scripts/release.sh $ENV_NAME）"
  exit 0
fi

say "⑤ リリース（ゲート 9〜13 本 → 全 PASS のときだけ配布・箱は自動適用）"
bash "$SCRIPT_DIR/release.sh" "$ENV_NAME"
