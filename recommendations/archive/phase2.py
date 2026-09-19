"""
Phase 2：チャンク戦略の比較実験。

同一データ・同一検索条件(top_k)で、チャンク分割方法だけを変えて評価セットを回し、
① キーワード再現率 ② 検索ヒット率@k ③ 棄却正解率 を戦略間で比較する。

使い方:
    python -m recommendations.eval.phase2                       # 全戦略を構築＋評価
    python -m recommendations.eval.phase2 --strategies fixed_512_64 semantic
    python -m recommendations.eval.phase2 --skip-build          # 既存インデックスを使い評価だけ
    python -m recommendations.eval.phase2 --limit 3             # スモークテスト（先頭3問）
"""
import argparse
import json
import sys
from datetime import datetime

from recommendations.ingest.chunking import STRATEGIES, collection_for
from recommendations.core.config import DATA_DIR, EMBEDDING_MODEL, LLM_MODEL, TOP_K
from recommendations.core.embeddings import EMBEDDING_MODELS
from recommendations.eval.full import build_query_engine, run_eval
from recommendations.ingest.ingest import build_index, load_documents

EVAL_SET_PATH = DATA_DIR / "eval" / "eval_set.json"
RESULTS_DIR = DATA_DIR / "eval" / "results"


def load_eval_set(limit: int | None) -> list[dict]:
    if not EVAL_SET_PATH.exists():
        sys.exit(f"ERROR: 評価セットが見つかりません: {EVAL_SET_PATH}")
    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        eval_set = json.load(f)
    return eval_set[:limit] if limit else eval_set


def print_comparison(rows: list[dict]) -> None:
    print("\n" + "=" * 78)
    print("Phase 2 チャンク戦略比較")
    print("=" * 78)
    print(f"{'戦略':<18}{'チャンク数':>8}{'①kw再現':>10}{'②検索@k':>10}{'③棄却':>9}{'全的中':>8}")
    print("-" * 78)
    for r in rows:
        s = r["summary"]
        kr = f"{s['keyword_recall_mean']*100:.1f}%" if s["keyword_recall_mean"] is not None else "-"
        rh = f"{s['retrieval_hit_rate']*100:.1f}%" if s["retrieval_hit_rate"] is not None else "-"
        aa = f"{s['abstention_accuracy']*100:.1f}%" if s["abstention_accuracy"] is not None else "-"
        fc = f"{s['fully_correct']}/{s['num_questions']}"
        print(f"{r['strategy']:<18}{r['num_chunks']:>8}{kr:>10}{rh:>10}{aa:>9}{fc:>8}")
    print("-" * 78)

    # ベスト戦略（キーワード再現率で判定。同点なら検索ヒット率）
    best = max(
        rows,
        key=lambda r: (
            r["summary"]["keyword_recall_mean"] or 0,
            r["summary"]["retrieval_hit_rate"] or 0,
        ),
    )
    print(f"→ 最良（①キーワード再現率基準）: {best['strategy']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2 チャンク戦略比較")
    parser.add_argument("--strategies", nargs="+", default=list(STRATEGIES),
                        help="比較する戦略名（既定: 全戦略）")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--limit", type=int, default=None, help="先頭N問だけ評価")
    parser.add_argument("--skip-build", action="store_true",
                        help="インデックス構築をスキップし既存コレクションで評価")
    args = parser.parse_args()

    unknown = [s for s in args.strategies if s not in STRATEGIES]
    if unknown:
        sys.exit(f"ERROR: 未知の戦略: {unknown}  （利用可能: {list(STRATEGIES)}）")

    eval_set = load_eval_set(args.limit)
    documents = None if args.skip_build else load_documents()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "phase2_latest.json"

    def save(rows_so_far):
        """途中経過を毎戦略ごとに保存する（1戦略が落ちても成果を失わないため）。"""
        payload = {
            "phase": 2,
            "config": {
                "embedding_model": EMBEDDING_MODEL,
                "llm_model": LLM_MODEL,
                "top_k": args.top_k,
                "num_questions": len(eval_set),
            },
            "comparison": [
                {k: r[k] for k in ("strategy", "collection", "num_chunks", "summary", "error")}
                for r in rows_so_far
            ],
            "detail": rows_so_far,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    # Phase 2 の埋め込みは openai-small 固定（比較変数はチャンク戦略だけ）。
    # eval.build_query_engine の既定は本番(ruri)になったため、ここでは明示的に渡す。
    embed_model = EMBEDDING_MODELS["openai_small"]()

    rows = []
    for strategy in args.strategies:
        collection = collection_for(strategy)
        print("\n" + "#" * 78)
        print(f"# 戦略: {strategy}  (collection={collection})")
        print("#" * 78)

        # 1戦略の失敗が他の戦略の成果（＝API課金）を無駄にしないよう分離
        try:
            num_chunks = "-"
            if not args.skip_build:
                # 再現性のため既存コレクションを作り直してから構築
                num_chunks = build_index(
                    documents,
                    collection_name=collection,
                    transformations=STRATEGIES[strategy](),
                    reset=True,
                    embed_model=embed_model,
                )
            query_engine = build_query_engine(
                args.top_k, collection_name=collection, embed_model=embed_model,
            )
            results, summary = run_eval(query_engine, eval_set)
            rows.append({
                "strategy": strategy, "collection": collection,
                "num_chunks": num_chunks, "summary": summary,
                "results": results, "error": None,
            })
        except Exception as e:  # noqa: BLE001 — 戦略単位で握って続行
            print(f"!! 戦略 {strategy} で失敗: {type(e).__name__}: {e}")
            rows.append({
                "strategy": strategy, "collection": collection,
                "num_chunks": "-", "summary": None, "results": [],
                "error": f"{type(e).__name__}: {e}",
            })
        save(rows)  # 逐次保存

    ok_rows = [r for r in rows if r["summary"] is not None]
    if ok_rows:
        print_comparison(ok_rows)
    failed = [r["strategy"] for r in rows if r["summary"] is None]
    if failed:
        print(f"\n失敗した戦略: {failed}")

    # タイムスタンプ版にもコピー保存（履歴として残す）
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stamped = RESULTS_DIR / f"phase2_{stamp}.json"
    with open(out_path, encoding="utf-8") as f:
        data = f.read()
    with open(stamped, "w", encoding="utf-8") as f:
        f.write(data)
    print(f"\n結果を保存しました: {stamped}")


if __name__ == "__main__":
    main()
