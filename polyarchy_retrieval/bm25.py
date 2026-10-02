"""
BM25：in-memory 版（`BM25Index`）と、Qdrant の sparse ベクトルによる版（`build_sparse_bm25`・`QdrantBM25`）。

■ スコア一致の担保（rank_bm25 = BM25Okapi と厳密に同じ式）
BM25Okapi の score(q,d) = Σ_{t∈q} idf(t) · tf·(k1+1) / (tf + k1·(1−b+b·|d|/avgdl)) を
sparse 内積に分解する：
  - **doc 側の重み**  = tf·(k1+1) / (tf + k1·(1−b+b·|d|/avgdl))   …… Qdrant の sparse ベクトルに格納
  - **query 側の重み** = idf(t) × クエリ内の出現数                 …… 検索時にクライアントで計算
内積 = BM25Okapi.get_scores と同値（Qdrant の IDF modifier は式が異なるため**使わない**）。
idf は BM25Okapi の実装と同一（ln((N−n+0.5)/(n+0.5))・負値は ε·平均idf に床上げ・ε=0.25）。
トークナイズは `tokenizer.tokenize_ja`（in-memory 版と同一）。

■ 語彙サイドカー
token → (sparse index, idf) の辞書とコーパス統計（avgdl・N・k1/b/ε）を、呼び出し側が決めた
パス（gzip の JSON）に永続化する。BM25 は本質的にコーパス大域統計（idf・avgdl）に依存するため、
**文書追加時は再構築**する。語彙は索引と別ファイル＝索引だけが作り直されると検索のクエリ側の
重みがずれる（読み込み時に n_docs と索引のチャンク数を照合して警告する）。
"""
import gzip
import json
import math
import time
from collections import Counter
from pathlib import Path

from qdrant_client import models
from rank_bm25 import BM25Okapi

from polyarchy_common.logsetup import get_logger
from polyarchy_retrieval.qdrant import SPARSE_NAME, QdrantCorpusStore, require_layer
from polyarchy_retrieval.tokenizer import tokenize_ja

# rank_bm25.BM25Okapi の既定と同値（BM25Index は既定値で生成している）
K1, B, EPSILON = 1.5, 0.75, 0.25
UPSERT_BATCH = 256

log = get_logger("polyarchy.qdrant_bm25")


class BM25Index:
    """全チャンク本文に対する in-memory BM25 索引。search(query, k) で上位 chunk id を返す。

    本線は Qdrant sparse（QdrantBM25）＝本索引は語彙サイドカーの無い一時コレクション
    （層ゲート自己検証など）用のフォールバック。
    """

    def __init__(self, ids: list[str], texts: list[str], file_names: list[str]):
        self.ids = ids
        self.texts = texts
        self.file_names = file_names
        # id → file_name（融合結果から出典名を引くため）
        self.file_of = dict(zip(ids, file_names))
        self._bm25 = BM25Okapi([tokenize_ja(t) for t in texts])

    def search(self, query: str, top_k: int) -> list[str]:
        """BM25 スコア上位 top_k の chunk id を降順で返す。"""
        scores = self._bm25.get_scores(tokenize_ja(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [self.ids[i] for i in order[:top_k]]


def build_sparse_bm25(store: QdrantCorpusStore, corpus: str, vocab_path: Path) -> None:
    """全チャンクをトークナイズし、doc 側 sparse 重みを Qdrant へ・語彙を vocab_path へ書く（冪等）。"""
    collection = store.collection(corpus)
    t0 = time.time()
    g = store.get_all(corpus)
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
    p = Path(vocab_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump({"params": {"k1": K1, "b": B, "epsilon": EPSILON,
                              "avgdl": avgdl, "n_docs": n},
                   "vocab": {t: [i, v] for t, (i, v) in vocab.items()}}, f,
                  ensure_ascii=False)
    log.info("完了: %d 件 upsert・語彙 %d 語 → %s（%.0f秒）",
             done, len(vocab), p, time.time() - t0)


class QdrantBM25:
    """Qdrant sparse による BM25 検索（BM25Index.search と同じ id 列を返す）。

    語彙サイドカーを1回ロードし、以後の検索は「トークナイズ→query 側重み→sparse 内積」。
    絞り込みは Qdrant の Filter で push-down する（layer 必須）。語彙ファイルが無ければ
    FileNotFoundError（呼び出し側は in-memory 版へ切り替えられる）。
    """

    def __init__(self, store: QdrantCorpusStore, collection: str, vocab_path: Path,
                 missing_hint: str = ""):
        self.store = store
        self.collection = collection
        p = Path(vocab_path)
        if not p.exists():
            raise FileNotFoundError(f"BM25 語彙サイドカーがありません: {p}{missing_hint}")
        with gzip.open(p, "rt", encoding="utf-8") as f:
            d = json.load(f)
        self.params = d["params"]
        self.vocab: dict[str, list] = d["vocab"]
        log.info("BM25 語彙ロード: %s 語彙%d・avgdl %.1f・n_docs %d",
                 self.collection, len(self.vocab),
                 self.params["avgdl"], self.params["n_docs"])
        # 語彙は索引と別ファイル＝索引だけ作り直されると（索引を別のフォルダと共有する手元など）
        # クエリ側の重みがずれ、黙って順位が変わる（2026-10-02：MRR 0.710→0.692）。
        n_points = self.store.qc.count(self.collection, exact=True).count
        if n_points != self.params["n_docs"]:
            log.warning("BM25 語彙の n_docs %d と索引のチャンク数 %d が食い違う＝語彙が索引と"
                        "同じ時点のものでない（語彙を作り直すか、索引を作った側の語彙を複製する）",
                        self.params["n_docs"], n_points)

    def search(self, query: str, top_k: int, qfilter: models.Filter) -> list[str]:
        """BM25 上位 top_k の chunk id を降順で返す（qfilter は layer 必須）。"""
        counts = Counter(t for t in tokenize_ja(query) if t in self.vocab)
        if not counts:
            return []
        indices = [self.vocab[t][0] for t in counts]
        values = [self.vocab[t][1] * c for t, c in counts.items()]  # idf × クエリ内出現数
        res = self.store.qc.query_points(
            self.collection,
            query=models.SparseVector(indices=indices, values=values),
            using=SPARSE_NAME,
            query_filter=require_layer(qfilter),
            limit=top_k,
            with_payload=False,
        )
        return [str(p.id) for p in res.points]
