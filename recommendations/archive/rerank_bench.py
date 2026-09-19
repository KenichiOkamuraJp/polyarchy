"""Phase 8 前哨：リランカー精度×速度ベンチ（検索専用・クレジット0）。

rerank_eval.py の思想を本番経路に載せ替えた版：
- 候補プールは **本番のハイブリッド retriever（BM25+ruri→RRF、比較型マルチクエリ分解ON）**
  で1問1回だけ取得しキャッシュ（＝全リランカーを同一プールで公平比較・§22.3「本番経路で測る」）。
- 各候補リランカーで同一プールを再順位付け→top5 の hit@5/MRR を org別/qtype別（doyukai含む）に集計。
- 同時に rerank 1問あたりのレイテンシ p50/p95/mean を実測（phase7_perf の rerank p50 と可比）。

速度と精度を同じ実走から出す（同一の実プールを再ランクするため）。使い方:
    python -m recommendations.eval.rerank_bench                 # 全候補
    python -m recommendations.eval.rerank_bench --rerankers jp_reranker_xsmall_v1 bge_reranker_v2_m3
    python -m recommendations.eval.rerank_bench --pool-k 20     # プール深さ（K）を変えて感度も見る
"""
import argparse
import json
import statistics
import time
from collections import defaultdict

from transformers import logging as _hf_logging
_hf_logging.set_verbosity_error()

import chromadb
from llama_index.core import Settings, StorageContext, VectorStoreIndex
from llama_index.vector_stores.chroma import ChromaVectorStore

from recommendations.core.config import CHROMA_DIR, COLLECTION_NAME, DATA_DIR, RERANK_RETRIEVE_K, TOP_K
from recommendations.core.embeddings import production_embed_model
from recommendations.eval.eval import expected_sources, source_name
from recommendations.core.hybrid import production_retriever


def _cross_encoder(model_name, max_length=512):
    """ローカル HF クロスエンコーダ → (query,[passages])→scores 関数。"""
    def build():
        from sentence_transformers import CrossEncoder
        model = CrossEncoder(model_name, max_length=max_length)

        def rerank(query, passages):
            return [float(s) for s in model.predict([(query, p) for p in passages])]
        return rerank
    return build


# 候補（全てローカル日本語対応クロスエンコーダ・ローカル完結要件を満たす）。params は概算。
CANDIDATES = {
    "bge_reranker_v2_m3":       _cross_encoder("BAAI/bge-reranker-v2-m3"),           # 568M prod
    "ruri_v3_reranker_310m":    _cross_encoder("cl-nagoya/ruri-v3-reranker-310m"),   # 315M
    "ruri_reranker_base":       _cross_encoder("cl-nagoya/ruri-reranker-base"),      # 111M
    "jp_reranker_base_v1":      _cross_encoder("hotchpotch/japanese-reranker-cross-encoder-base-v1"),   # 111M
    "jp_reranker_small_v1":     _cross_encoder("hotchpotch/japanese-reranker-cross-encoder-small-v1"),  # 118M
    "jp_reranker_xsmall_v1":    _cross_encoder("hotchpotch/japanese-reranker-cross-encoder-xsmall-v1"), # 107M
    "jp_reranker_xsmall_v2":    _cross_encoder("hotchpotch/japanese-reranker-xsmall-v2"),  # 37M
}
ORGS = ["keidanren", "gov", "rengo", "nissho", "doyukai"]
QTYPES = ["baseline", "paraphrase", "temporal", "comparative", "coverage"]


def mean(x):
    return sum(x) / len(x) if x else float("nan")


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool-k", type=int, default=RERANK_RETRIEVE_K)
    ap.add_argument("--rerankers", nargs="*", default=list(CANDIDATES))
    args = ap.parse_args()

    Settings.embed_model = production_embed_model()
    Settings.llm = None
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = client.get_or_create_collection(COLLECTION_NAME)
    vs = ChromaVectorStore(chroma_collection=col)
    index = VectorStoreIndex.from_vector_store(
        vs, storage_context=StorageContext.from_defaults(vector_store=vs))
    pool_k = max(args.pool_k, TOP_K)
    retriever = production_retriever(index, col, Settings.embed_model, pool_k)
    print(f"collection={COLLECTION_NAME} ({col.count()} chunks), pool_k={pool_k}, top_k={TOP_K}")

    es = json.loads((DATA_DIR / "eval" / "eval_set.json").read_text(encoding="utf-8"))
    # --- 本番プールを1問1回だけ取得しキャッシュ（全リランカー共通） ---
    cache = []
    for q in es:
        targets = expected_sources(q)
        if not targets:
            continue
        nodes = retriever.retrieve(q["question"])
        cache.append({
            "org": q.get("expected_org", "?"), "qtype": q.get("qtype", "baseline"),
            "targets": targets, "question": q["question"],
            "sources": [source_name(n) for n in nodes],
            "texts": [n.node.get_content() for n in nodes],
        })
    print(f"retrieved prod pools for {len(cache)} answerable Qs "
          f"(pool sizes p50={pct([len(c['texts']) for c in cache],50)})\n")

    rows = []
    for key in args.rerankers:
        if key not in CANDIDATES:
            print(f"!! unknown {key}"); continue
        print(f"--- loading {key} ---", flush=True)
        try:
            rerank = CANDIDATES[key]()
        except Exception as e:
            print(f"!! {key} load failed: {e}"); continue
        rerank(cache[0]["question"], cache[0]["texts"])  # warmup

        by = defaultdict(lambda: {"hit": [], "rr": []})
        byt = defaultdict(lambda: {"hit": [], "rr": []})
        lat = []
        for c in cache:
            t = time.perf_counter()
            scores = rerank(c["question"], c["texts"])
            lat.append(time.perf_counter() - t)
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            got = [c["sources"][i] for i in order[:TOP_K]]
            rank = next((i for i, s in enumerate(got, 1) if s in c["targets"]), None)
            hit, rr = (1.0 if rank else 0.0), (1.0 / rank if rank else 0.0)
            by[c["org"]]["hit"].append(hit); by[c["org"]]["rr"].append(rr)
            byt[c["qtype"]]["hit"].append(hit); byt[c["qtype"]]["rr"].append(rr)
        allhit = [v for b in by.values() for v in b["hit"]]
        allrr = [v for b in by.values() for v in b["rr"]]
        row = {
            "key": key, "hit": mean(allhit), "mrr": mean(allrr),
            "p50_ms": pct(lat, 50) * 1e3, "p95_ms": pct(lat, 95) * 1e3,
            "mean_ms": mean(lat) * 1e3,
            "by_org": {o: (mean(by[o]["hit"]), mean(by[o]["rr"])) for o in ORGS},
            "by_qt": {t: (mean(byt[t]["hit"]), mean(byt[t]["rr"])) for t in QTYPES if t in byt},
        }
        rows.append(row)
        print(f"  {key}: hit@5 {row['hit']*100:.1f}%  MRR {row['mrr']:.3f}  "
              f"rerank p50 {row['p50_ms']:.0f}ms  mean {row['mean_ms']:.0f}ms\n", flush=True)

    # --- 比較表 ---
    print("=" * 100)
    print(f"{'model':24}{'hit@5':>7}{'MRR':>7}{'p50ms':>8}{'p95ms':>8}"
          f"{'  doyukai':>10}{'  gov':>10}{'  rengo':>10}")
    print("-" * 100)
    base = next((r for r in rows if r["key"] == "bge_reranker_v2_m3"), None)
    for r in rows:
        d = r["by_org"]
        spd = f"({base['p50_ms']/r['p50_ms']:.1f}x)" if base else ""
        print(f"{r['key']:24}{r['hit']*100:>6.1f}%{r['mrr']:>7.3f}{r['p50_ms']:>7.0f}{spd:<6}"
              f"{r['p95_ms']:>7.0f}"
              f"{d['doyukai'][0]*100:>8.1f}% {d['gov'][0]*100:>8.1f}% {d['rengo'][0]*100:>8.1f}%")
    print("\nqtype別 hit@5:")
    print(f"{'model':24}" + "".join(f"{t[:9]:>11}" for t in QTYPES))
    for r in rows:
        print(f"{r['key']:24}" + "".join(
            f"{r['by_qt'].get(t,(float('nan'),))[0]*100:>10.1f}%" for t in QTYPES))

    out = DATA_DIR / "perf" / f"rerank_bench_k{args.pool_k}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存: {out}")


if __name__ == "__main__":
    main()
