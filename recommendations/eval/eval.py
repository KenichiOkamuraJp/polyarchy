"""
評価セット（data/eval/eval_set.json）で性能を測定する CLI（薄いディスパッチャ）。

実体は 3 モジュールに分かれている（所見 2026-08-19 段 4・機械分割＝数値/出力は不変）：
  recommendations/eval/full.py       生成込み eval（既定・クレジット消費）
  recommendations/eval/retrieval.py  --retrieval-only（検索のみ・クレジット0・確定的アンカー）
  recommendations/eval/filters.py    --filter-eval（構造化フィルタ＋層ゲート・クレジット0）
共通の入出力は _common.py、採点ヘルパは _scoring.py。
検索系（--retrieval-only / --filter-eval）はバッチ2 段1（2026-08-28）で本番経路＝
PolicySearchService＋diversify=True に一本化（所見 2026-08-19 段 5-1 の決定を実施）。

測定する指標:
  1. キーワード再現率 (keyword recall)  … 生成回答に期待キーワードが含まれる割合（生成の質）
  2. 検索ヒット率 (retrieval hit@k)     … 正解PDFが検索結果に含まれた割合（Retrieverの質）
  3. 棄却正解率 (abstention accuracy)   … expected_source=null の設問で正しく「確認できません」と答えた割合

使い方:
    python -m recommendations.eval.eval                 # 全問を生成込みで評価（クレジット消費）
    python -m recommendations.eval.eval --retrieval-only # 検索のみ（クレジット0）。hit@k/MRR＋Phase 9 網羅メトリクス
    python -m recommendations.eval.eval --filter-eval    # 構造化フィルタ＋層ゲート（クレジット0）
    python -m recommendations.eval.eval --limit 3        # 先頭3問だけ（スモークテスト）
    python -m recommendations.eval.eval --top-k 8        # TOP_K を上書きして評価
    python -m recommendations.eval.eval --retrieval-only --eval-set both   # 正典＋ユーザー由来

Phase 9（2026-07-14）で設問型を2つ追加:
  coverage_multi … 横断・網羅型。top_k 内に拾えた「異なる団体」数が min_coverage 以上かで採点
                   （知見1＝1団体偏りの物差し。retrieval-only の網羅メトリクス節に出力）。
  aggregation    … 集約・最上級型。多くは棄却が正解＝③で採点、日付確定できる少数のみ通常採点。
"""
import argparse
import sys

from recommendations.core.config import TOP_K
from recommendations.eval._common import EVAL_SET_PATH, load_eval_set


def main() -> None:
    parser = argparse.ArgumentParser(description="RAGベースライン評価")
    parser.add_argument("--limit", type=int, default=None, help="先頭N問だけ評価する")
    parser.add_argument("--top-k", type=int, default=TOP_K, help="検索する上位チャンク数")
    parser.add_argument("--retrieval-only", action="store_true",
                        help="検索のみ評価（LLM不要・クレジット0）。hit@k/MRR をドメイン別に出す")
    parser.add_argument("--filter-eval", action="store_true",
                        help="Phase 6 構造化フィルタ検索の評価（検索専用・クレジット0）")
    parser.add_argument("--eval-set", choices=["canonical", "user", "both"],
                        default="canonical",
                        help="評価対象（Phase 12）: canonical=正典のみ（既定・アンカー）／"
                             "user=ユーザー由来のみ／both=連結")
    args = parser.parse_args()

    if args.filter_eval:
        from recommendations.eval.filters import run_filter_eval
        run_filter_eval(args.top_k)
        return

    if not EVAL_SET_PATH.exists():
        sys.exit(f"ERROR: 評価セットが見つかりません: {EVAL_SET_PATH}")

    eval_set = load_eval_set(args.eval_set)
    if not eval_set:
        sys.exit(f"ERROR: 評価対象が空です（--eval-set {args.eval_set}）")
    if args.eval_set != "canonical":
        # 由来を明示（正典との混同防止）。canonical 既定は従来どおり無出力＝挙動不変。
        print(f"[eval-set={args.eval_set}] 正典 {len(load_eval_set('canonical'))}問"
              f" ＋ ユーザー由来 {len(load_eval_set('user'))}問 → 評価対象 {len(eval_set)}問")
    if args.limit:
        eval_set = eval_set[: args.limit]

    if args.retrieval_only:
        from recommendations.eval.retrieval import run_retrieval_only
        run_retrieval_only(eval_set, args.top_k)
        return

    from recommendations.eval.full import run_full
    run_full(eval_set, args.top_k)


if __name__ == "__main__":
    main()
