"""
Phase B（B1）：Chroma vs Qdrant の dense 検索パリティ検証（クレジット0・ローカル完結）。

アダプタ差し替えの受け入れ基準は「既存アンカーとのバイト一致」（開発環境方針 §3）。その前段の
計測として、**ベクトル検索単体**の順位一致を全 eval 質問で確認する：

    各質問 → ruri クエリ埋め込み（本番と同一）
      → Chroma `col.query`（本番経路・HNSW/l2・layer=公開）で上位 K
      → Qdrant `search_dense`（HNSW/Cosine と exact の両方・layer=公開）で上位 K
      → id 列を突き合わせ

見るもの：
- **完全一致率**（上位 K の id 列が同一）と初回分岐順位
- 上位5 / 上位30 の集合一致率（RRF→リランク後の top-5 に効くのはこちら）
- Qdrant の HNSW と exact の差（Qdrant 側の近似誤差の切り分け）

埋め込みが L2 正規化済（ノルム≒1.0・移行時確認）のため l2 と Cosine の順位は理論上同一。
分岐が出るならどちらかの HNSW の近似（recall<1）か同点タイの順序差であり、その規模を測る。

実行:  python -m recommendations.eval.qdrant_parity [--k 50] [--limit 0(=全問)]
"""
import argparse
import json
import time

from recommendations.core.config import CHROMA_DIR, COLLECTION_NAME, PROJECT_ROOT
from recommendations.core.filters import SearchFilter
from polyarchy_common.logsetup import configure_quiet_logging, get_logger, quiet_stdout
from recommendations.core.qdrant_store import QdrantCorpusStore

log = get_logger("polyarchy.qdrant_parity")

EVAL_FILES = ("data/eval/eval_set.json", "data/eval/eval_set_userderived.json")


def load_questions() -> list[str]:
    qs: list[str] = []
    for f in EVAL_FILES:
        p = PROJECT_ROOT / f
        if p.exists():
            qs.extend(item["question"] for item in json.loads(p.read_text()))
    return qs


def compare(a: list[str], b: list[str]) -> dict:
    """2つの id 列の一致度。first_div は初めて食い違う順位（一致なら None）。"""
    first_div = next((i + 1 for i, (x, y) in enumerate(zip(a, b)) if x != y), None)
    if first_div is None and len(a) != len(b):
        first_div = min(len(a), len(b)) + 1
    return {
        "exact": first_div is None,
        "first_div": first_div,
        "top5_set": set(a[:5]) == set(b[:5]),
        "top30_set": set(a[:30]) == set(b[:30]),
    }


def run_bm25_parity(k: int, limit: int) -> None:
    """rank_bm25（in-memory）vs Qdrant sparse の BM25 パリティ（id 順＋スコア一致）。

    rank_bm25 は非マッチ文書（スコア0）も末尾に返しうるが Qdrant はマッチのみ返すため、
    比較は「スコア>0 の範囲」で行う。スコアは float32（Qdrant）と float64 の丸め差を
    相対誤差 1e-4 まで許容して突き合わせる。
    """
    import chromadb

    from recommendations.core.hybrid import load_bm25_from_chroma
    from recommendations.core.qdrant_bm25 import QdrantBM25
    from recommendations.core.qdrant_store import QdrantCorpusStore

    questions = load_questions()
    if limit:
        questions = questions[:limit]
    chroma = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = chroma.get_collection(COLLECTION_NAME)
    log.info("in-memory BM25 索引を構築中（比較基準）…")
    ref = load_bm25_from_chroma(col)
    pos_of = {cid: i for i, cid in enumerate(ref.ids)}
    store = QdrantCorpusStore()
    qbm = QdrantBM25(store, COLLECTION_NAME)

    from collections import Counter

    from qdrant_client import models

    from recommendations.core.hybrid import tokenize_ja

    n_exact = n_top5 = 0
    max_rel_err = 0.0
    divs = []
    t0 = time.time()
    for i, q in enumerate(questions, 1):
        scores = ref._bm25.get_scores(tokenize_ja(q))
        order = sorted(range(len(scores)), key=lambda j: scores[j], reverse=True)
        ref_ids = [ref.ids[j] for j in order[:k] if scores[j] > 0]

        # Qdrant 側は id とスコアの両方を取る（QdrantBM25.search と同じクエリ構成）
        counts = Counter(t for t in tokenize_ja(q) if t in qbm.vocab)
        res = store.qc.query_points(
            COLLECTION_NAME,
            query=models.SparseVector(
                indices=[qbm.vocab[t][0] for t in counts],
                values=[qbm.vocab[t][1] * c for t, c in counts.items()]),
            using="bm25", limit=k, with_payload=False) if counts else None
        got = [(str(p.id), p.score) for p in (res.points if res else [])][: len(ref_ids)]

        cmpres = compare(ref_ids, [g[0] for g in got])
        n_exact += cmpres["exact"]
        n_top5 += cmpres["top5_set"]
        if not cmpres["exact"]:
            divs.append(cmpres["first_div"])
        for cid, qs in got[:10]:
            rs = scores[pos_of[cid]]
            if rs > 0:
                max_rel_err = max(max_rel_err, abs(qs - rs) / rs)
        if i % 50 == 0:
            log.info("  %d/%d 問（%.0f秒）", i, len(questions), time.time() - t0)
    divs.sort()
    log.info("―― BM25 パリティ（n=%d・上位%d・スコア>0 範囲）――", len(questions), k)
    log.info("rank_bm25 vs Qdrant sparse: 完全一致 %d/%d・top5集合一致 %d/%d・分岐順位 %s・"
             "スコア最大相対誤差 %.2e",
             n_exact, len(questions), n_top5, len(questions),
             (f"min={divs[0]} med={divs[len(divs)//2]} max={divs[-1]}" if divs else "―"),
             max_rel_err)


def main() -> None:
    configure_quiet_logging()
    ap = argparse.ArgumentParser(description="Chroma vs Qdrant dense パリティ検証")
    ap.add_argument("--k", type=int, default=50, help="比較する上位件数（既定50=HYBRID_KV）")
    ap.add_argument("--limit", type=int, default=0, help="質問数の上限（0=全問）")
    ap.add_argument("--bm25", action="store_true",
                    help="dense の代わりに BM25（rank_bm25 vs Qdrant sparse）を検証する")
    args = ap.parse_args()
    if args.bm25:
        run_bm25_parity(args.k, args.limit)
        return

    questions = load_questions()
    if args.limit:
        questions = questions[: args.limit]
    log.info("パリティ検証: %d 問 × 上位%d（Chroma HNSW vs Qdrant HNSW/exact）",
             len(questions), args.k)

    import chromadb

    from recommendations.core.embeddings import production_embed_model

    with quiet_stdout():
        embed = production_embed_model()
    chroma = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = chroma.get_collection(COLLECTION_NAME)
    store = QdrantCorpusStore()
    sf = SearchFilter()  # 公開層のみ（本番既定と同一）
    where = sf.chroma_where()

    stats = {"hnsw": [], "exact": [], "hnsw_vs_exact": []}
    t0 = time.time()
    for i, q in enumerate(questions, 1):
        qemb = embed.get_query_embedding(q)
        cids = col.query(query_embeddings=[qemb], n_results=args.k,
                         where=where)["ids"][0]
        q_hnsw = store.search_dense(COLLECTION_NAME, qemb, sf, args.k, exact=False)
        q_exact = store.search_dense(COLLECTION_NAME, qemb, sf, args.k, exact=True)
        stats["hnsw"].append(compare(cids, q_hnsw))
        stats["exact"].append(compare(cids, q_exact))
        stats["hnsw_vs_exact"].append(compare(q_hnsw, q_exact))
        if i % 50 == 0:
            log.info("  %d/%d 問（%.0f秒）", i, len(questions), time.time() - t0)

    n = len(questions)
    log.info("―― 結果（n=%d・上位%d・%.0f秒）――", n, args.k, time.time() - t0)
    for name, label in (("hnsw", "Chroma vs Qdrant(HNSW)"),
                        ("exact", "Chroma vs Qdrant(exact)"),
                        ("hnsw_vs_exact", "Qdrant HNSW vs exact")):
        rs = stats[name]
        ex = sum(r["exact"] for r in rs)
        t5 = sum(r["top5_set"] for r in rs)
        t30 = sum(r["top30_set"] for r in rs)
        divs = sorted(r["first_div"] for r in rs if r["first_div"] is not None)
        log.info("%s: 完全一致 %d/%d・top5集合一致 %d/%d・top30集合一致 %d/%d・"
                 "分岐順位の分布 %s", label, ex, n, t5, n, t30, n,
                 (f"min={divs[0]} med={divs[len(divs)//2]} max={divs[-1]}"
                  if divs else "―"))


if __name__ == "__main__":
    main()
