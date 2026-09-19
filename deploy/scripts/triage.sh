#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# 燃料サイクルの週次トリアージを 1 コマンド化＝運用者ロールの台本（B15 論点2・2026-09-03）。
#
#   使い方: bash deploy/scripts/triage.sh <staging|prod> [--local] [--recheck]
#     --local   … S3 の燃料（箱の fuelsync バックアップ）を読まずローカル捕捉分だけで起こす
#     --recheck … recommendations の zero/low を今の索引で再検索（ruri 読込＝重い・開発者向け）
#
# 工程（読み取りのみ・評価セットには一切書かない＝fail-safe）:
#   ① 燃料取得＝両サービスの捕捉ログ（S3 data/query_log・data/stats/query_log）をローカル分と合算
#   ② 草稿生成＝stats.ops.quality_candidates／recommendations.ops.quality_candidates（決定論・LLM 不使用）
#   ③ 出力＝ops/triage/<日付>/{stats,recommendations}_candidates.jsonl（git 外）＋要約
#
# 役割分担（運用設計 §0 の境界原理＝「ゲートが検出できる失敗は運用者・開発者、意味の判断は PdM」）:
#   運用者＝本スクリプトを週次で実行し草稿を PdM へ渡す（生成まで）。
#   PdM＝採否／開発者＝その反映（stats: `quality_candidates --accept`／recommendations: phase12 scaffold→careful→append）
#          ＋検索型を昇華したらアンカー記載を同一コミットで更新（README「品質の担保」）。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
ENV_NAME="${1:?使い方: bash triage.sh <staging|prod> [--local] [--recheck]}"; shift
LOCAL=0; RECHECK=0
for a in "$@"; do case "$a" in --local) LOCAL=1;; --recheck) RECHECK=1;; *) echo "未知の引数: $a" >&2; exit 1;; esac; done
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$SCRIPT_DIR/../env/${ENV_NAME}.env"
[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE が無い" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"
cd "$REPO_DIR"
command -v python >/dev/null || { echo "python が無い（conda activate polyarchy を先に）" >&2; exit 1; }

OUT="$REPO_DIR/ops/triage/$(date +%Y-%m-%d)"
mkdir -p "$OUT"
S3_ARGS=()
if [[ "$LOCAL" == 0 ]]; then
  BUCKET="${BUCKET:-$(cd "$SCRIPT_DIR/../terraform" && AWS_PROFILE="$AWS_PROFILE" terraform output -raw s3_bucket 2>/dev/null || true)}"
  if [[ -n "$BUCKET" ]]; then
    S3_ARGS=(--s3 --bucket "$BUCKET" --profile "$AWS_PROFILE")
    echo "── ① 燃料＝S3 $BUCKET（fuelsync バックアップ）＋ローカル捕捉分"
  else
    echo "── ① ⚠ S3 バケット名を terraform output から取得できない＝ローカル捕捉分のみ" >&2
  fi
else
  echo "── ① 燃料＝ローカル捕捉分のみ（--local）"
fi

echo "── ② stats：発見層の候補（find_quality の次の問）"
python -m stats.ops.quality_candidates ${S3_ARGS[@]+"${S3_ARGS[@]}"} --out "$OUT/stats_candidates.jsonl" >/dev/null
echo "── ② recommendations：Phase 12 の候補（zero/low/frequent）"
REC_ARGS=(${S3_ARGS[@]+"${S3_ARGS[@]}"}); [[ "$RECHECK" == 1 ]] && REC_ARGS+=(--recheck)
# ${arr[@]+"${arr[@]}"}＝空配列でも set -u で落ちない書き方（macOS の bash 3.2 対策）
python -m recommendations.ops.quality_candidates ${REC_ARGS[@]+"${REC_ARGS[@]}"} --out "$OUT/recommendations_candidates.jsonl" >/dev/null

echo "── ③ 要約（草稿＝$OUT・git 外）"
python3 - "$OUT" <<'PY'
import json, sys
from collections import Counter
from pathlib import Path
out = Path(sys.argv[1])
def rows(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []
st = rows(out / "stats_candidates.jsonl"); rc = rows(out / "recommendations_candidates.jsonl")
print(f"  stats: {len(st)} 件（{dict(Counter(r.get('status') for r in st))}）")
for r in st[:8]:
    print(f"    [{r.get('status')}] {r['id']} {r['query']!r} targets={r.get('targets')}")
print(f"  recommendations: {len(rc)} 件（{dict(Counter(r.get('kind') for r in rc))}）")
for r in rc[:8]:
    print(f"    [{r.get('kind')}] ×{r['count']} {r['query']!r} rc={r.get('result_count')}"
          + (f" now={r['now_count']}" if 'now_count' in r else ""))
print("""
次の一手（PdM＝採否の判断・開発者＝反映）:
  stats  : python -m stats.ops.quality_candidates --accept %s/stats_candidates.jsonl --ids <id,id>   # find_quality へ追記→ゲート
  rec    : 各草稿の next（phase12 scaffold …）→ expected_keywords を本文 verbatim で埋める → phase12 append
  共通   : 検索型を昇華したらアンカー記載（README「品質の担保」）を同一コミットで更新""" % out)
PY
