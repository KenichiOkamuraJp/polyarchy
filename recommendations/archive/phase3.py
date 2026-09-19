"""
Phase 3：埋め込みモデルの比較実験。

同一データ・同一チャンク（semantic）・同一検索条件(top_k)・同一LLMで、
**格納/検索に使う埋め込みモデルだけ**を変えて評価セットを回し、検索精度を比較する。
埋め込み空間はモデルごとに異なるため、モデルごとに専用コレクションへ再インデックスする。

主指標は検索専用の ② 検索ヒット率@k と MRR@k（生成を挟まずブレが小さい）。
① キーワード再現率・③ 棄却正解率は生成を挟むため補助指標（±2〜3pt 揺れる）。

使い方:
    python -m recommendations.eval.phase3                        # 既定（OpenAI small/large）を構築＋評価
    python -m recommendations.eval.phase3 --models openai_small openai_large multilingual_e5_large
    python -m recommendations.eval.phase3 --skip-build           # 既存コレクションを使い評価だけ
    python -m recommendations.eval.phase3 --limit 3              # スモークテスト（先頭3問）
"""
import argparse
import json
import sys
from datetime import datetime

from recommendations.ingest.chunking import STRATEGIES
from recommendations.core.config import DATA_DIR, DEFAULT_STRATEGY, LLM_MODEL, TOP_K
from recommendations.core.embeddings import EMBEDDING_MODELS, OPENAI_MODELS, collection_for
from recommendations.eval.eval import build_query_engine, run_eval
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
    print("\n" + "=" * 82)
    print("Phase 3 埋め込みモデル比較")
    print("=" * 82)
    print(f"{'モデル':<24}{'チャンク数':>8}{'②検索@k':>10}{'MRR@k':>9}{'①kw再現':>10}{'③棄却':>8}")
    print("-" * 82)
    for r in rows:
        s = r["summary"]
        rh = f"{s['retrieval_hit_rate']*100:.1f}%" if s["retrieval_hit_rate"] is not None else "-"
        mrr = f"{s['mrr']:.3f}" if s.get("mrr") is not None else "-"
        kr = f"{s['keyword_recall_mean']*100:.1f}%" if s["keyword_recall_mean"] is not None else "-"
        aa = f"{s['abstention_accuracy']*100:.1f}%" if s["abstention_accuracy"] is not None else "-"
        print(f"{r['model']:<24}{r['num_chunks']:>8}{rh:>10}{mrr:>9}{kr:>10}{aa:>8}")
    print("-" * 82)

    # ベスト（主指標＝検索ヒット率@k、同点なら MRR）
    best = max(
        rows,
        key=lambda r: (
            r["summary"]["retrieval_hit_rate"] or 0,
            r["summary"].get("mrr") or 0,
        ),
    )
    print(f"→ 最良（②検索ヒット率@k / MRR 基準）: {best['model']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3 埋め込みモデル比較")
    parser.add_argument("--models", nargs="+", default=OPENAI_MODELS,
                        help=f"比較するモデルキー（既定: {OPENAI_MODELS}）。"
                             f"利用可能: {list(EMBEDDING_MODELS)}")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--limit", type=int, default=None, help="先頭N問だけ評価")
    parser.add_argument("--skip-build", action="store_true",
                        help="インデックス構築をスキップし既存コレクションで評価")
    args = parser.parse_args()

    unknown = [m for m in args.models if m not in EMBEDDING_MODELS]
    if unknown:
        sys.exit(f"ERROR: 未知のモデル: {unknown}  （利用可能: {list(EMBEDDING_MODELS)}）")

    eval_set = load_eval_set(args.limit)
    documents = None if args.skip_build else load_documents()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "phase3_latest.json"

    def save(rows_so_far):
        """途中経過をモデルごとに保存する（1モデルが落ちても成果を失わないため）。"""
        payload = {
            "phase": 3,
            "config": {
                # 比較の固定条件。チャンクは全モデル共通（semantic）。
                "chunk_strategy": DEFAULT_STRATEGY,
                "llm_model": LLM_MODEL,
                "top_k": args.top_k,
                "num_questions": len(eval_set),
            },
            "comparison": [
                {k: r[k] for k in ("model", "collection", "num_chunks", "summary", "error")}
                for r in rows_so_far
            ],
            "detail": rows_so_far,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    rows = []
    for model_key in args.models:
        collection = collection_for(model_key)
        print("\n" + "#" * 82)
        print(f"# 埋め込みモデル: {model_key}  (collection={collection})")
        print("#" * 82)

        # 1モデルの失敗（DL失敗・上限超過・未インストール等）が他モデルの成果を無駄に
        # しないよう分離。HF未インストールは embeddings._hf が ImportError を投げる。
        try:
            embed_model = EMBEDDING_MODELS[model_key]()

            num_chunks = "-"
            if not args.skip_build:
                # 全モデルで同一チャンク（semantic）。境界は chunking._semantic の参照埋め込みで
                # 決まり比較対象モデルに依存しない。格納ベクトルだけ embed_model で差し替える。
                num_chunks = build_index(
                    documents,
                    collection_name=collection,
                    transformations=STRATEGIES[DEFAULT_STRATEGY](),
                    reset=True,
                    embed_model=embed_model,
                )
            query_engine = build_query_engine(
                args.top_k, collection_name=collection, embed_model=embed_model,
            )
            results, summary = run_eval(query_engine, eval_set)
            rows.append({
                "model": model_key, "collection": collection,
                "num_chunks": num_chunks, "summary": summary,
                "results": results, "error": None,
            })
        except Exception as e:  # noqa: BLE001 — モデル単位で握って続行
            print(f"!! モデル {model_key} で失敗: {type(e).__name__}: {e}")
            rows.append({
                "model": model_key, "collection": collection,
                "num_chunks": "-", "summary": None, "results": [],
                "error": f"{type(e).__name__}: {e}",
            })
        save(rows)  # 逐次保存

    ok_rows = [r for r in rows if r["summary"] is not None]
    if ok_rows:
        print_comparison(ok_rows)
    failed = [r["model"] for r in rows if r["summary"] is None]
    if failed:
        print(f"\n失敗したモデル: {failed}")

    # タイムスタンプ版にもコピー保存（履歴として残す）
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stamped = RESULTS_DIR / f"phase3_{stamp}.json"
    with open(out_path, encoding="utf-8") as f:
        data = f.read()
    with open(stamped, "w", encoding="utf-8") as f:
        f.write(data)
    print(f"\n結果を保存しました: {stamped}")


if __name__ == "__main__":
    main()
