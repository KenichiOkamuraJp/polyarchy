"""Phase 3 embedding re-test on the EXPANDED 78Q eval set + 158-doc corpus.

Per handoff §11 the decision metric is the retrieval hit@k / MRR (deterministic,
no generation). Builds each model's collection at 158-doc scale (semantic, boundaries
from the openai-small reference = identical across models) and compares retrieval on
the 78Q set. Reuses policy_docs_v3 for ruri_v3_310m_pfx (already built on this corpus).

Generation metrics (kw-recall/abstention) are intentionally NOT re-run per model:
§11 says they are noisy secondary indicators and don't drive the model choice.
"""
import json
from collections import defaultdict

import chromadb
from llama_index.core import Settings, StorageContext, VectorStoreIndex
from llama_index.vector_stores.chroma import ChromaVectorStore

from recommendations.ingest.chunking import STRATEGIES
from recommendations.core.config import CHROMA_DIR, DATA_DIR, DEFAULT_STRATEGY, TOP_K
from recommendations.core.embeddings import EMBEDDING_MODELS, collection_for
from recommendations.eval.eval import source_name
from recommendations.ingest.ingest import build_index, load_documents

MODELS = ["openai_small", "openai_large", "multilingual_e5_large_pfx", "ruri_v3_310m_pfx"]
# policy_docs_v3 IS semantic + ruri_v3_310m_pfx on the current 158-doc corpus → reuse it.
REUSE = {"ruri_v3_310m_pfx": "policy_docs_v3"}
RESULTS = DATA_DIR / "eval" / "results" / "phase3_retest_78q.json"


def build_all():
    docs = load_documents()
    counts = {}
    for mk in MODELS:
        if mk in REUSE:
            print(f"\n### reuse {REUSE[mk]} for {mk} (already 158-doc scale) ###")
            continue
        print(f"\n{'#'*70}\n# build {mk} -> {collection_for(mk)}\n{'#'*70}")
        em = EMBEDDING_MODELS[mk]()
        counts[mk] = build_index(
            docs, collection_name=collection_for(mk),
            transformations=STRATEGIES[DEFAULT_STRATEGY](), reset=True, embed_model=em,
        )
    return counts


def retrieval_eval(mk, answerable, client, org_of):
    coll_name = REUSE.get(mk, collection_for(mk))
    col = client.get_collection(coll_name)
    em = EMBEDDING_MODELS[mk]()
    Settings.embed_model = em
    Settings.llm = None
    vs = ChromaVectorStore(chroma_collection=col)
    index = VectorStoreIndex.from_vector_store(
        vs, storage_context=StorageContext.from_defaults(vector_store=vs))
    retr = index.as_retriever(similarity_top_k=TOP_K)
    by = defaultdict(lambda: {"hit": [], "rr": []})
    allhit, allrr = [], []
    for q in answerable:
        nodes = retr.retrieve(q["question"])
        got = [source_name(n) for n in nodes]
        rank = next((i for i, s in enumerate(got, 1) if s == q["expected_source"]), None)
        h = 1.0 if rank else 0.0
        r = 1.0 / rank if rank else 0.0
        org = org_of[q["id"]]
        by[org]["hit"].append(h); by[org]["rr"].append(r)
        allhit.append(h); allrr.append(r)
    return coll_name, col.count(), by, allhit, allrr


def m(x):
    return sum(x) / len(x) if x else float("nan")


def main():
    build_all()
    es = json.loads((DATA_DIR / "eval" / "eval_set.json").read_text(encoding="utf-8"))
    answerable = [q for q in es if q.get("expected_source")]
    org_of = {q["id"]: q.get("expected_org", "?") for q in es}
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    rows = []
    for mk in MODELS:
        coll, nchunks, by, allhit, allrr = retrieval_eval(mk, answerable, client, org_of)
        rows.append({"model": mk, "collection": coll, "num_chunks": nchunks,
                     "hit": m(allhit), "mrr": m(allrr),
                     "by_domain": {o: {"n": len(b["hit"]), "hit": m(b["hit"]), "mrr": m(b["rr"])}
                                   for o, b in by.items()}})

    print("\n" + "=" * 74)
    print(f"Phase 3 埋め込み再テスト（拡充78問 / answerable {len(answerable)} / top_k={TOP_K}）")
    print("=" * 74)
    print(f"{'model':<26}{'chunks':>8}{'hit@5':>9}{'MRR':>8}")
    print("-" * 74)
    for r in sorted(rows, key=lambda x: (x["hit"], x["mrr"]), reverse=True):
        print(f"{r['model']:<26}{r['num_chunks']:>8}{r['hit']*100:>8.1f}%{r['mrr']:>8.3f}")
    print("-" * 74)
    best = max(rows, key=lambda x: (x["hit"], x["mrr"]))
    print(f"→ 最良（hit@5 / MRR 基準）: {best['model']}")

    print("\n--- per-domain hit@5 ---")
    doms = ["keidanren", "gov", "rengo", "nissho"]
    print(f"{'model':<26}" + "".join(f"{d:>11}" for d in doms))
    for r in rows:
        cells = []
        for d in doms:
            bd = r["by_domain"].get(d)
            cells.append(f"{bd['hit']*100:>10.0f}%" if bd else f"{'-':>11}")
        print(f"{r['model']:<26}" + "".join(cells))

    RESULTS.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存: {RESULTS}")


if __name__ == "__main__":
    main()
