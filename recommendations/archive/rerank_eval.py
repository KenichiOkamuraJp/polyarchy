"""Phase 5 リランカー評価（検索専用・LLM不要＝クレジット0）。

retrieval_only.py の雛形を踏襲。本番 policy_docs_v3 からベクトルで **top-k=RETRIEVE_K
（広め, 既定20）** を取得し、ローカル日本語クロスエンコーダで再順位付け→上位 TOP_K(=5) に
切って hit@5/MRR を測る（retrieve→rerank の二段構え）。

- 検索を1問1回だけ実行し (source_name, 本文) をキャッシュ → ベースライン（ベクトル順の
  top-5）と全リランカーを **同一の取得集合** から算出する（公平・高速・決定論的）。
- ベースラインは §13.6 のベクトル単独 hit@5 84.3%/MRR 0.747 を再現するはず（回帰チェック）。
- 天井 hit@RETRIEVE_K も出す：正解がベクトル top-k に入っていなければリランクでは救えない
  ＝リランクで到達可能な hit@5 の上限。
- ドメイン別（keidanren/gov/rengo/nissho）＝弱点の連合(rengo)が上がったか見る。

使い方:
    python -m recommendations.eval.rerank_eval                       # 全リランカー, RETRIEVE_K=20
    python -m recommendations.eval.rerank_eval --retrieve-k 30       # 取得深さを変える
    python -m recommendations.eval.rerank_eval --rerankers ruri_reranker_large japanese_reranker_large
"""
import argparse
import json
from collections import defaultdict

# クロスエンコーダのトークナイザが 512 超のペアを truncate する度に出す情報ログ
# （"overflowing tokens are not returned ..."）を黙らせる。挙動には無関係の告知。
from transformers import logging as _hf_logging
_hf_logging.set_verbosity_error()

import chromadb
from llama_index.core import Settings, StorageContext, VectorStoreIndex
from llama_index.vector_stores.chroma import ChromaVectorStore

from recommendations.core.config import CHROMA_DIR, COLLECTION_NAME, DATA_DIR, TOP_K
from recommendations.core.embeddings import production_embed_model
from recommendations.eval.eval import source_name
from recommendations.core.rerankers import RERANKERS

EVAL_SET = DATA_DIR / "eval" / "eval_set.json"
DOMAINS = ["keidanren", "gov", "rengo", "nissho"]


def mean(x):
    return sum(x) / len(x) if x else float("nan")


def metrics_for_order(sources, expected, top_k):
    """順序付き source 名リストの上位 top_k で (hit, reciprocal_rank) を返す。"""
    top = sources[:top_k]
    rank = next((i for i, s in enumerate(top, 1) if s == expected), None)
    return (1.0 if rank else 0.0), (1.0 / rank if rank else 0.0)


def aggregate(per_q):
    """[(org, hit, rr)] → per-domain と ALL の hit/MRR。"""
    by = defaultdict(lambda: {"hit": [], "rr": []})
    for org, hit, rr in per_q:
        by[org]["hit"].append(hit)
        by[org]["rr"].append(rr)
    allhit = [h for _, h, _ in per_q]
    allrr = [r for _, _, r in per_q]
    return by, allhit, allrr


def print_table(name, by, allhit, allrr, retrieve_k):
    print(f"\n### {name} (retrieve_k={retrieve_k} → top_{TOP_K}) ###")
    print(f"{'org':11}{'n':>4}{'hit@'+str(TOP_K):>9}{'MRR':>9}")
    for org in DOMAINS:
        b = by.get(org, {"hit": [], "rr": []})
        print(f"{org:11}{len(b['hit']):>4}{mean(b['hit'])*100:>8.1f}%{mean(b['rr']):>9.3f}")
    print(f"{'ALL':11}{len(allhit):>4}{mean(allhit)*100:>8.1f}%{mean(allrr):>9.3f}")


def main():
    ap = argparse.ArgumentParser(description="Phase 5 リランカー評価（検索専用）")
    ap.add_argument("--retrieve-k", type=int, default=20, help="リランク前にベクトルで取る件数")
    ap.add_argument("--rerankers", nargs="*", default=list(RERANKERS),
                    help="評価するリランカーキー（既定=全て）")
    args = ap.parse_args()
    retrieve_k = args.retrieve_k

    Settings.embed_model = production_embed_model()
    Settings.llm = None
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = client.get_or_create_collection(COLLECTION_NAME)
    vs = ChromaVectorStore(chroma_collection=col)
    index = VectorStoreIndex.from_vector_store(
        vs, storage_context=StorageContext.from_defaults(vector_store=vs))
    retriever = index.as_retriever(similarity_top_k=retrieve_k)
    print(f"collection={COLLECTION_NAME} ({col.count()} chunks), "
          f"retrieve_k={retrieve_k}, top_k={TOP_K}")

    es = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    answerable = [q for q in es if q.get("expected_source")]

    # --- 検索を1問1回だけ実行してキャッシュ（source名・本文・org・正解） ---
    cache = []  # {org, expected, sources[list], texts[list]}
    for q in answerable:
        nodes = retriever.retrieve(q["question"])
        cache.append({
            "org": q.get("expected_org", "?"),
            "expected": q["expected_source"],
            "question": q["question"],
            "sources": [source_name(n) for n in nodes],
            "texts": [n.node.get_content() for n in nodes],
        })
    print(f"retrieved for {len(cache)} answerable Qs\n")

    results = {}  # name -> {by, allhit, allrr}

    # --- ベースライン（ベクトル順 top-5）＝ §13.6 の 84.3%/0.747 を再現するはず ---
    base = []
    for c in cache:
        h, r = metrics_for_order(c["sources"], c["expected"], TOP_K)
        base.append((c["org"], h, r))
    by, allhit, allrr = aggregate(base)
    results["baseline_vector"] = {"by": by, "allhit": allhit, "allrr": allrr}
    print_table("baseline_vector", by, allhit, allrr, retrieve_k)

    # --- 天井（hit@RETRIEVE_K）＝リランクで到達可能な hit の上限 ---
    ceil = []
    for c in cache:
        h, r = metrics_for_order(c["sources"], c["expected"], retrieve_k)
        ceil.append((c["org"], h, r))
    by, allhit, allrr = aggregate(ceil)
    results["ceiling@k"] = {"by": by, "allhit": allhit, "allrr": allrr}
    print_table(f"ceiling(hit@{retrieve_k})", by, allhit, allrr, retrieve_k)

    # --- 各リランカー ---
    for key in args.rerankers:
        if key not in RERANKERS:
            print(f"!! unknown reranker '{key}', skip")
            continue
        try:
            print(f"\n--- loading reranker: {key} ---")
            rerank = RERANKERS[key]()
        except Exception as e:  # モデル単位で失敗を隔離（他は継続）
            print(f"!! {key} load failed: {e}")
            continue
        per_q = []
        for c in cache:
            if not c["texts"]:
                per_q.append((c["org"], 0.0, 0.0))
                continue
            scores = rerank(c["question"], c["texts"])
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            reranked_sources = [c["sources"][i] for i in order]
            h, r = metrics_for_order(reranked_sources, c["expected"], TOP_K)
            per_q.append((c["org"], h, r))
        by, allhit, allrr = aggregate(per_q)
        results[key] = {"by": by, "allhit": allhit, "allrr": allrr}
        print_table(key, by, allhit, allrr, retrieve_k)

    # --- まとめ表（ALL と rengo を横並び） ---
    print("\n" + "=" * 64)
    print(f"{'method':28}{'ALL hit@5':>11}{'ALL MRR':>10}{'rengo hit@5':>13}")
    print("-" * 64)
    for name, d in results.items():
        rengo = d["by"].get("rengo", {"hit": []})
        print(f"{name:28}{mean(d['allhit'])*100:>10.1f}%{mean(d['allrr']):>10.3f}"
              f"{mean(rengo['hit'])*100:>12.1f}%")

    # --- 保存 ---
    out = DATA_DIR / "eval" / "results" / f"rerank_eval_k{retrieve_k}.json"
    payload = {
        "retrieve_k": retrieve_k, "top_k": TOP_K, "collection": COLLECTION_NAME,
        "num_answerable": len(cache),
        "methods": {
            name: {
                "all_hit": mean(d["allhit"]), "all_mrr": mean(d["allrr"]),
                "by_domain": {o: {"n": len(b["hit"]), "hit": mean(b["hit"]),
                                  "mrr": mean(b["rr"])}
                              for o, b in d["by"].items()},
            } for name, d in results.items()
        },
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存: {out}")


if __name__ == "__main__":
    main()
