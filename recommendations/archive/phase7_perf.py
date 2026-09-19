"""
Phase 7：規模拡大の性能計測（クレジット0・ローカルのみ・prod 不変）。

1コレクションを対象に、Qdrant 等への移行判断の材料を実測する:
  - 規模: チャンク数 / 埋め込み次元 / Chroma ベクトルセグメントのディスクサイズ
  - 起動コスト: 全チャンク読込 (collection.get) と BM25 索引構築の所要時間
  - メモリ: 段階別ピーク RSS（macOS: ru_maxrss はバイト）
  - 検索レイテンシ: eval_set の answerable 設問で retrieve(hybrid) と rerank(bge) を
    分計して p50 / p95 / mean / max

使い方（プロセスを分けて実行＝ピークRSSの汚染を避ける）:
    python -m recommendations.eval.phase7_perf policy_docs_v3
    COLLECTION_NAME は使わず引数で明示。結果は data/perf/phase7_<collection>.json に保存。
"""
import json
import resource
import sqlite3
import statistics
import sys
import time
from pathlib import Path


def rss_mb() -> float:
    """ピーク RSS (MB)。macOS の ru_maxrss はバイト（Linux は KB なので注意）。"""
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v / 1e6 if sys.platform == "darwin" else v / 1e3


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def segment_disk_mb(chroma_dir: Path, collection_name: str) -> float:
    """chroma.sqlite3 からコレクションのセグメント dir を引き、合計サイズ(MB)を返す。"""
    con = sqlite3.connect(chroma_dir / "chroma.sqlite3")
    segs = [r[0] for r in con.execute(
        "SELECT s.id FROM segments s JOIN collections c ON s.collection = c.id "
        "WHERE c.name = ?", (collection_name,))]
    con.close()
    total = 0
    for s in segs:
        d = chroma_dir / s
        if d.is_dir():
            total += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    return total / 1e6


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python -m recommendations.eval.phase7_perf <collection_name>")
    name = sys.argv[1]
    stages = {}
    t = time.perf_counter()

    import chromadb
    from recommendations.core.config import (CHROMA_DIR, DATA_DIR, HYBRID_KB, HYBRID_KV,
                            RERANK_RETRIEVE_K, TOP_K)
    from recommendations.core.embeddings import production_embed_model
    from recommendations.core.hybrid import BM25Index, build_hybrid_retriever, get_all
    from recommendations.core.rerankers import production_reranker_postprocessor
    stages["import_s"] = round(time.perf_counter() - t, 2)
    stages["rss_after_import_mb"] = round(rss_mb(), 1)

    # --- モデルロード（ruri 埋め込み / bge リランカー）
    t = time.perf_counter()
    embed_model = production_embed_model()
    embed_model.get_query_embedding("ウォームアップ")
    stages["embed_load_s"] = round(time.perf_counter() - t, 2)
    stages["rss_after_embed_mb"] = round(rss_mb(), 1)

    t = time.perf_counter()
    reranker = production_reranker_postprocessor(top_n=TOP_K)
    stages["reranker_load_s"] = round(time.perf_counter() - t, 2)
    stages["rss_after_reranker_mb"] = round(rss_mb(), 1)

    # --- コレクション読込 + BM25 構築（起動コストの本体）
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = client.get_collection(name)
    n_chunks = col.count()

    t = time.perf_counter()
    g = get_all(col)
    stages["chroma_get_all_s"] = round(time.perf_counter() - t, 2)
    stages["rss_after_get_mb"] = round(rss_mb(), 1)

    t = time.perf_counter()
    BM25Index(g["ids"], [d or "" for d in g["documents"]],
              [m.get("file_name", "?") for m in g["metadatas"]])
    stages["bm25_build_s"] = round(time.perf_counter() - t, 2)
    stages["rss_after_bm25_mb"] = round(rss_mb(), 1)
    del g

    # --- 本番経路の retriever（内部で再度 get+BM25 構築）
    pool_k = max(RERANK_RETRIEVE_K, TOP_K)
    t = time.perf_counter()
    retriever = build_hybrid_retriever(col, embed_model, HYBRID_KV, HYBRID_KB, pool_k)
    stages["retriever_build_s"] = round(time.perf_counter() - t, 2)
    stages["rss_peak_mb"] = round(rss_mb(), 1)

    # --- 検索レイテンシ（eval の answerable 設問全問）
    from llama_index.core.schema import QueryBundle
    eval_set = json.loads((DATA_DIR / "eval" / "eval_set.json").read_text())
    questions = [q["question"] for q in eval_set
                 if q.get("expected_source") or q.get("expected_sources")]
    retriever.retrieve(questions[0])  # ウォームアップ（キャッシュ/遅延初期化を除外）

    t_ret, t_rer = [], []
    for q in questions:
        t = time.perf_counter()
        nodes = retriever.retrieve(q)
        t_ret.append(time.perf_counter() - t)
        t = time.perf_counter()
        reranker.postprocess_nodes(nodes, query_bundle=QueryBundle(q))
        t_rer.append(time.perf_counter() - t)

    def summ(xs):
        return {"mean_ms": round(statistics.mean(xs) * 1e3, 1),
                "p50_ms": round(pct(xs, 50) * 1e3, 1),
                "p95_ms": round(pct(xs, 95) * 1e3, 1),
                "max_ms": round(pct(xs, 100) * 1e3, 1)}

    result = {
        "collection": name,
        "n_chunks": n_chunks,
        "embed_dim": len(embed_model.get_query_embedding("次元")),
        "disk_segment_mb": round(segment_disk_mb(CHROMA_DIR, name), 1),
        "stages": stages,
        "latency_n_queries": len(questions),
        "retrieve": summ(t_ret),
        "rerank": summ(t_rer),
        "end_to_end": summ([a + b for a, b in zip(t_ret, t_rer)]),
    }

    out = DATA_DIR / "perf" / f"phase7_{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n保存: {out}")


if __name__ == "__main__":
    main()
