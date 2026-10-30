#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# 提言の定型更新（②の前半）を 1 コマンド化＝運用者ロールの台本（運用設計 §0/§2.3）。
#
#   使い方: bash deploy/scripts/update.sh <staging|prod> [--no-release]
#
# 工程（新着検出 → 新着のある団体だけ収集 → 増分取込 → 追い判定 → release.sh）:
#   ① check_new（読み取り専用）＝新着 0 で、前回の残り（③の取込の残り・④の未判定）も 0 なら何もせず終了
#   ② collect --skip-existing（新着のある団体のみ。keidanren は今年＋昨年を対象）
#   ③ qdrant_ingest ingest（doc 単位増分・冪等。ローカル qdrant-dev 起動が前提。本文の区切り〔semantic 境界検出〕に
#      OpenAI API を使用＝recommendations/.env に OPENAI_API_KEY）
#   ④ policy_tagger（分野タグ・文書性格の追い判定＝Anthropic API を使用。recommendations/.env に ANTHROPIC_API_KEY）
#   ★2 つの鍵は②の前に確かめる＝無ければ何も書かずに止まる（②が catalog に行を足した後に③で止まる半端を作らない・2026-10-30）
#      ★取込の後に回す＝判定は本文を Qdrant から読む（取込の前だと新規文書は題名だけで判定される・
#      2026-10-02 に判明）。未判定の文書は分野タグなしで取り込まれ、判定が catalog と payload の両方に書く
#   ⑤ release.sh <env>（配る前の確認＋全ゲート〔一覧はルート README「品質の担保」〕全 PASS のときだけ配布＝FAIL なら箱には何も起きない）
#
# 安全設計＝失敗の最悪ケースは「リリースが起きない」（fail-closed）。--no-release で⑤の手前まで。
# 途中で止まったら（③④の失敗・中断）、原因を直してもう一度 update.sh を流す＝①が新着 0 でも、③の取込の残り・④の未判定が
#   あれば②を飛ばして③から続ける（②の後は catalog に新しい行があるので、①の新着チェックは 0 件になる・2026-10-30）。
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
RESUME=0
if [[ -z "$NEW_ORGS" ]]; then
  # 前回の残り＝②の後に③④で止まった分（catalog には行があるので新着チェックには出ない）
  count_of() { local n; n="$("$@" | tail -1)"; [[ "$n" =~ ^[0-9]+$ ]] || fail "残りの数を取れない（$*）: ${n}"; echo "$n"; }
  PEND_INGEST="$(count_of env COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.qdrant_ingest ingest --count)"
  PEND_TAG="$(count_of env COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.policy_tagger --count)"
  if [[ "$PEND_INGEST" == 0 && "$PEND_TAG" == 0 ]]; then
    echo "✅ 新着なし＝何もしません（チェック失敗団体があれば stderr 参照）"
    exit 0
  fi
  echo "   新着は 0 だが前回の残りがある（取込の残り ${PEND_INGEST} 件・分野タグの未判定 ${PEND_TAG} 件）＝②を飛ばして③から続ける"
  RESUME=1
else
  echo "   新着のある団体: $NEW_ORGS"
fi

# ③は OpenAI（本文の区切り）・④は Anthropic（分野タグ）の鍵を使う＝②の前に確かめる（読むのは recommendations/.env か環境変数）
python - <<'PYEOF' || fail "API の鍵が無い（recommendations/.env に書いてから流し直す＝何も書いていない）"
import sys
from recommendations.core import config as c
need = {"OPENAI_API_KEY": "③ 取込の本文の区切り", "ANTHROPIC_API_KEY": "④ 分野タグ・文書性格の判定"}
missing = [f"{k}（{v}）" for k, v in need.items() if not getattr(c, k, None)]
if missing:
    sys.exit("未設定: " + "・".join(missing))
PYEOF

if [[ "$RESUME" == 0 ]]; then
  say "② 収集（--skip-existing＝既存は再取得しない）"
  YEAR="$(date +%Y)"
  for org in $NEW_ORGS; do
    if [[ "$org" == "keidanren" ]]; then
      python -m recommendations.ingest.collect keidanren --years "$YEAR" "$((YEAR-1))" --skip-existing
    else
      python -m recommendations.ingest.collect "$org" --skip-existing
    fi
  done
fi

say "③ 増分取込（doc 単位・冪等）"
COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.qdrant_ingest ingest

say "④ 分野タグ・文書性格の追い判定（本文が索引に入った後＝対象確認 → 本実行）"
COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.policy_tagger --dry-run
COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.policy_tagger

echo "★catalog.csv の未コミット差分（レビューしてコミットすること）:"
git -C "$REPO_DIR" diff --stat -- recommendations/data/catalog.csv || true

if [[ "$NO_RELEASE" == "--no-release" ]]; then
  echo "⏸ --no-release 指定＝ここで停止（配布するときは bash deploy/scripts/release.sh ${ENV_NAME}）"
  exit 0
fi

say "⑤ リリース（配る前の確認 → 全ゲート → 全 PASS のときだけ配布・箱は自動適用）"
bash "$SCRIPT_DIR/release.sh" "$ENV_NAME"
