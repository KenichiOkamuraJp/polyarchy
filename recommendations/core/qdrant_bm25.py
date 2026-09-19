"""
Phase B（B1）：BM25 を Qdrant の sparse ベクトルへ移す（in-memory BM25 の 180k 破綻問題の解消側）。

■ スコア一致の担保（rank_bm25 = BM25Okapi と厳密に同じ式）
BM25Okapi の score(q,d) = Σ_{t∈q} idf(t) · tf·(k1+1) / (tf + k1·(1−b+b·|d|/avgdl)) を
sparse 内積に分解する：
  - **doc 側の重み**  = tf·(k1+1) / (tf + k1·(1−b+b·|d|/avgdl))   …… Qdrant の sparse ベクトルに格納
  - **query 側の重み** = idf(t) × クエリ内の出現数                 …… 検索時にクライアントで計算
内積 = BM25Okapi.get_scores と同値（Qdrant の IDF modifier は式が異なるため**使わない**）。
idf は BM25Okapi の実装と同一（ln((N−n+0.5)/(n+0.5))・負値は ε·平均idf に床上げ・ε=0.25）。
トークナイズも本番と同一（fugashi＝recommendations/core/hybrid.tokenize_ja）。

■ 語彙サイドカー
token → (sparse index, idf) の辞書と корпус統計（avgdl・N・k1/b/ε）を
`data/bm25/<collection>_vocab.json.gz` に永続化する。BM25 は本質的にコーパス大域統計
（idf・avgdl）に依存するため、**文書追加時は再構築**（従来の in-memory 全構築と同じ性質。
違いは「毎起動17秒＋数GBメモリ」が「ingest 時1回＋辞書ロード数秒」になること）。

実行（sparse 重みの構築・冪等）:
    python -m recommendations.core.qdrant_bm25            # 語彙構築→全点に sparse 重みを upsert
検索は QdrantBM25（hybrid.py が VECTOR_BACKEND=qdrant のとき使用）。
"""
import gzip
import json
import math
import time
from collections import Counter

from qdrant_client import models

from recommendations.core.config import COLLECTION_NAME, DATA_DIR
from polyarchy_common.logsetup import configure_quiet_logging, get_logger

# rank_bm25.BM25Okapi の既定と同値（BM25Index は既定値で生成している）
K1, B, EPSILON = 1.5, 0.75, 0.25
SPARSE_NAME = "bm25"
UPSERT_BATCH = 256

log = get_logger("polyarchy.qdrant_bm25")


def _vocab_path(collection: str):
    return DATA_DIR / "bm25" / f"{collection}_vocab.json.gz"


def build_sparse_bm25(collection: str = COLLECTION_NAME) -> None:
    """全チャンクをトークナイズし、doc 側 sparse 重みを Qdrant へ・語彙をサイドカーへ書く。"""
    from recommendations.core.hybrid import tokenize_ja
    from recommendations.core.qdrant_store import QdrantCorpusStore

    store = QdrantCorpusStore(timeout=300)
    t0 = time.time()
    g = store.get_all(collection)
    ids, texts = g["ids"], g["documents"]
    n = len(ids)
    log.info("sparse BM25 構築開始: %s %d 件", collection, n)

    # ── 1パス目：トークナイズ・文書頻度（BM25Okapi._initialize と同じ集計）
    doc_freqs: list[Counter] = []
    doc_len: list[int] = []
    nd: Counter = Counter()  # token → その token を含む文書数
    for text in texts:
        toks = tokenize_ja(text or "")
        doc_len.append(len(toks))
        freq = Counter(toks)
        doc_freqs.append(freq)
        nd.update(freq.keys())
    avgdl = sum(doc_len) / n
    log.info("トークナイズ完了: 語彙 %d・avgdl %.1f（%.0f秒）", len(nd), avgdl, time.time() - t0)

    # ── idf（BM25Okapi._calc_idf と同一：負の idf は ε·平均idf へ床上げ）
    idf: dict[str, float] = {}
    idf_sum = 0.0
    negative: list[str] = []
    for tok, freq in nd.items():
        v = math.log(n - freq + 0.5) - math.log(freq + 0.5)
        idf[tok] = v
        idf_sum += v
        if v < 0:
            negative.append(tok)
    eps = EPSILON * (idf_sum / len(idf))
    for tok in negative:
        idf[tok] = eps

    vocab = {tok: (i, idf[tok]) for i, tok in enumerate(nd)}

    # ── doc 側重みを upsert（tf 正規化のみ。idf は query 側＝BM25Okapi の構造どおり）
    done = empty = 0
    buf: list[models.PointVectors] = []
    for cid, freq, dl in zip(ids, doc_freqs, doc_len):
        if not freq:
            # トークン0のチャンク（記号・空白のみ等）。Qdrant は空 sparse の update を拒否する
            # (422)。sparse を付けない＝BM25 で決して当たらない＝in-memory 版と同値の挙動。
            # 統計（avgdl・N）には BM25Okapi と同様に算入済み。
            empty += 1
            continue
        denom_norm = K1 * (1 - B + B * dl / avgdl)
        indices, values = [], []
        for tok, tf in freq.items():
            indices.append(vocab[tok][0])
            values.append(tf * (K1 + 1) / (tf + denom_norm))
        buf.append(models.PointVectors(
            id=cid, vector={SPARSE_NAME: models.SparseVector(indices=indices, values=values)}))
        if len(buf) >= UPSERT_BATCH:
            store.qc.update_vectors(collection, points=buf, wait=True)
            done += len(buf)
            buf = []
            if done % 10_000 < UPSERT_BATCH:
                log.info("  %d/%d upsert（%.0f秒）", done, n, time.time() - t0)
    if buf:
        store.qc.update_vectors(collection, points=buf, wait=True)
        done += len(buf)
    if empty:
        log.info("  トークン0チャンク %d 件は sparse なし（BM25 非対象＝in-memory 版と同値）", empty)

    # ── サイドカー（語彙＋コーパス統計）
    p = _vocab_path(collection)
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump({"params": {"k1": K1, "b": B, "epsilon": EPSILON,
                              "avgdl": avgdl, "n_docs": n},
                   "vocab": {t: [i, v] for t, (i, v) in vocab.items()}}, f,
                  ensure_ascii=False)
    log.info("完了: %d 件 upsert・語彙 %d 語 → %s（%.0f秒）",
             done, len(vocab), p, time.time() - t0)


class QdrantBM25:
    """Qdrant sparse による BM25 検索（BM25Index.search 互換の id 列を返す）。

    語彙サイドカーを1回ロードし、以後の検索は「トークナイズ→query 側重み→sparse 内積」。
    org/layer/date フィルタは Qdrant へ push-down する（in-memory 版は push 不可のため
    広取り→Python 後段だった。フィルタ付き検索は本実装の方が正確＝真のフィルタ内 top-k）。
    """

    def __init__(self, store, collection: str = COLLECTION_NAME):
        self.store = store
        self.collection = collection
        p = _vocab_path(collection)
        if not p.exists():
            raise FileNotFoundError(
                f"BM25 語彙サイドカーがありません: {p}（先に python -m recommendations.core.qdrant_bm25 を実行）")
        with gzip.open(p, "rt", encoding="utf-8") as f:
            d = json.load(f)
        self.params = d["params"]
        self.vocab: dict[str, list] = d["vocab"]
        log.info("BM25 語彙ロード: %s 語彙%d・avgdl %.1f・n_docs %d",
                 self.collection, len(self.vocab),
                 self.params["avgdl"], self.params["n_docs"])

    def search(self, query: str, top_k: int, sf=None) -> list[str]:
        """BM25 上位 top_k の chunk id を降順で返す（sf は org/layer/date を push-down）。"""
        from recommendations.core.hybrid import tokenize_ja
        from recommendations.core.qdrant_store import qdrant_filter

        counts = Counter(t for t in tokenize_ja(query) if t in self.vocab)
        if not counts:
            return []
        indices = [self.vocab[t][0] for t in counts]
        values = [self.vocab[t][1] * c for t, c in counts.items()]  # idf × クエリ内出現数
        res = self.store.qc.query_points(
            self.collection,
            query=models.SparseVector(indices=indices, values=values),
            using=SPARSE_NAME,
            query_filter=qdrant_filter(sf) if sf is not None else None,
            limit=top_k,
            with_payload=False,
        )
        return [str(p.id) for p in res.points]


def main() -> None:
    configure_quiet_logging()
    build_sparse_bm25()


if __name__ == "__main__":
    main()
