"""Phase 5 ハイブリッド検索評価（BM25＋ベクトル→RRF→rerank, 検索専用・クレジット0）。

rerank_eval.py の続き。本番 policy_docs_v3 に対し、chromadb を直接使って
  1) ベクトル: ruri でクエリ埋め込み → col.query で上位 K_v
  2) 語彙: BM25(fugashi) で上位 K_b
  3) RRF 融合 → 上位 RERANK_RETRIEVE_K を候補プール
  4) 本番リランカー(bge)で再順位付け → top-5
の hit@5/MRR をドメイン別に測る。**同一ハーネスでベクトル単独→rerank も併算**して
公平に比較する（そちらは §15 の 92.9% を再現するはず＝ハーネス健全性チェック）。

id 空間は Chroma のチャンク id で統一（埋め込みと BM25 が同じ id）ので RRF が素直。

使い方:
    python -m recommendations.eval.hybrid_eval                 # K_v=K_b=50, pool=RERANK_RETRIEVE_K
    python -m recommendations.eval.hybrid_eval --kv 100 --kb 100
"""
import argparse
import json
from collections import defaultdict

from transformers import logging as _hf_logging
_hf_logging.set_verbosity_error()

import chromadb

from recommendations.core.config import (CHROMA_DIR, COLLECTION_NAME, DATA_DIR, PRODUCTION_RERANKER,
                        RERANK_RETRIEVE_K, TOP_K)
from recommendations.core.embeddings import production_embed_model
from recommendations.core.hybrid import load_bm25_from_chroma, rrf_fuse
from recommendations.core.rerankers import RERANKERS

EVAL_SET = DATA_DIR / "eval" / "eval_set.json"
DOMAINS = ["keidanren", "gov", "rengo", "nissho"]


def mean(x):
    return sum(x) / len(x) if x else float("nan")


def metrics(files, expected):
    """順序付き file_name 列の上位 TOP_K で (hit, rr)。"""
    top = files[:TOP_K]
    rank = next((i for i, s in enumerate(top, 1) if s == expected), None)
    return (1.0 if rank else 0.0), (1.0 / rank if rank else 0.0)


def aggregate_print(name, per_q):
    by = defaultdict(lambda: {"hit": [], "rr": []})
    for org, h, r in per_q:
        by[org]["hit"].append(h)
        by[org]["rr"].append(r)
    allhit = [h for _, h, _ in per_q]
    allrr = [r for _, _, r in per_q]
    print(f"\n### {name} ###")
    print(f"{'org':11}{'n':>4}{'hit@'+str(TOP_K):>9}{'MRR':>9}")
    for org in DOMAINS:
        b = by.get(org, {"hit": [], "rr": []})
        print(f"{org:11}{len(b['hit']):>4}{mean(b['hit'])*100:>8.1f}%{mean(b['rr']):>9.3f}")
    print(f"{'ALL':11}{len(allhit):>4}{mean(allhit)*100:>8.1f}%{mean(allrr):>9.3f}")
    return {"by": by, "allhit": allhit, "allrr": allrr}


def main():
    ap = argparse.ArgumentParser(description="Phase 5 ハイブリッド検索評価（検索専用）")
    ap.add_argument("--kv", type=int, default=50, help="ベクトルで取る件数")
    ap.add_argument("--kb", type=int, default=50, help="BM25で取る件数")
    ap.add_argument("--pool", type=int, default=RERANK_RETRIEVE_K,
                    help="融合後にリランカーへ渡す候補数")
    args = ap.parse_args()

    embed = production_embed_model()
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = client.get_or_create_collection(COLLECTION_NAME)

    print(f"collection={COLLECTION_NAME} ({col.count()} chunks), "
          f"K_v={args.kv} K_b={args.kb} pool={args.pool} top_k={TOP_K}, "
          f"reranker={PRODUCTION_RERANKER}")

    # 全チャンク → id→text/file と BM25 索引（1回だけ）
    print("BM25索引を構築中（全チャンクを fugashi トークナイズ）...")
    g = col.get(include=["documents", "metadatas"])
    id2text = {i: (d or "") for i, d in zip(g["ids"], g["documents"])}
    id2file = {i: m.get("file_name", "不明") for i, m in zip(g["ids"], g["metadatas"])}
    from recommendations.core.hybrid import BM25Index
    bm25 = BM25Index(g["ids"], [d or "" for d in g["documents"]],
                     [m.get("file_name", "不明") for m in g["metadatas"]])
    print("BM25索引 構築完了")

    rerank = RERANKERS[PRODUCTION_RERANKER]()

    es = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    answerable = [q for q in es if q.get("expected_source")]

    vec_only, hybrid = [], []
    for q in answerable:
        org = q.get("expected_org", "?")
        exp = q["expected_source"]
        # 1) ベクトル順位（chroma 直接, ruri クエリ埋め込み）
        qemb = embed.get_query_embedding(q["question"])
        vres = col.query(query_embeddings=[qemb], n_results=args.kv)
        vids = vres["ids"][0]
        # 2) BM25 順位
        bids = bm25.search(q["question"], args.kb)

        # --- ベクトル単独→rerank（比較用ベースライン） ---
        vpool = vids[:args.pool]
        vscores = rerank(q["question"], [id2text[i] for i in vpool])
        vorder = sorted(range(len(vpool)), key=lambda i: vscores[i], reverse=True)
        vfiles = [id2file[vpool[i]] for i in vorder]
        vec_only.append((org, *metrics(vfiles, exp)))

        # --- ハイブリッド（RRF）→rerank ---
        fused = rrf_fuse([vids, bids])[:args.pool]
        hscores = rerank(q["question"], [id2text[i] for i in fused])
        horder = sorted(range(len(fused)), key=lambda i: hscores[i], reverse=True)
        hfiles = [id2file[fused[i]] for i in horder]
        hybrid.append((org, *metrics(hfiles, exp)))

    r_vec = aggregate_print("vector-only → rerank（ベースライン, §15の92.9%再現想定）", vec_only)
    r_hyb = aggregate_print("hybrid(BM25+vector RRF) → rerank", hybrid)

    print("\n" + "=" * 60)
    print(f"{'method':40}{'ALL hit@5':>10}{'ALL MRR':>9}")
    print("-" * 60)
    for nm, d in [("vector→rerank", r_vec), ("hybrid→rerank", r_hyb)]:
        print(f"{nm:40}{mean(d['allhit'])*100:>9.1f}%{mean(d['allrr']):>9.3f}")

    out = DATA_DIR / "eval" / "results" / f"hybrid_eval_kv{args.kv}_kb{args.kb}.json"
    payload = {
        "kv": args.kv, "kb": args.kb, "pool": args.pool, "reranker": PRODUCTION_RERANKER,
        "methods": {
            nm: {
                "all_hit": mean(d["allhit"]), "all_mrr": mean(d["allrr"]),
                "by_domain": {o: {"n": len(b["hit"]), "hit": mean(b["hit"]),
                                  "mrr": mean(b["rr"])} for o, b in d["by"].items()},
            } for nm, d in [("vector_rerank", r_vec), ("hybrid_rerank", r_hyb)]
        },
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存: {out}")


if __name__ == "__main__":
    main()
