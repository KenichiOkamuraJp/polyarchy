"""
ハイブリッド検索の 1 フィルタ条件分：ベクトル（dense）＋BM25 → RRF 融合の id 列。

ベクトルは意味的に近いチャンクを拾うが、**数値・固有名詞・制度名**の完全一致に弱いことがある。
BM25 は逆に語彙一致に強い。両者の順位を RRF で融合し、後段のリランカーに渡す候補の recall を上げる。

絞り込みは 2 経路（各コーパスのフィルタが両方を作って渡す）：
- `qfilter`（Qdrant の Filter・layer 必須）＝ベクトル・sparse BM25 に push-down する条件。
  ベクトル検索は「絞り込んだ母集合の中で」近傍を返す必要がある（後段だけだと上位が全部圏外になる）。
- `matches`（Python の述語）＝push-down できない条件を含む完全な述語。`needs_post` が True のときだけ
  候補を `widen` 倍に広げて後段で絞る。

クエリの分解（比較型の団体別検索など）・候補の合流・llama_index の包装は各コーパスに残す。
"""
from typing import Callable

from qdrant_client import models

from polyarchy_retrieval.bm25 import BM25Index, QdrantBM25
from polyarchy_retrieval.fusion import rrf_fuse
from polyarchy_retrieval.qdrant import QdrantCorpusStore


class HybridSearcher:
    """1 コーパス（1 コレクション）のハイブリッド検索。

    sparse＝`QdrantBM25`（本線・push-down）か `BM25Index`（語彙の無い一時コレクション用。push できない
    ため常に widen 倍で取り、全条件を後段で絞る）。meta_of＝chunk id → メタデータ（起動時に全件を
    読んだもの・後段の述語に使う）。kv／kb＝ベクトル／BM25 の候補数。exact＝HNSW を迂回した厳密検索。
    """

    def __init__(self, store: QdrantCorpusStore, corpus: str, sparse: QdrantBM25 | BM25Index,
                 meta_of: dict[str, dict], kv: int, kb: int, *, widen: int, exact: bool):
        self.store = store
        self.corpus = corpus
        self.sparse = sparse
        self.meta_of = meta_of
        self.kv, self.kb = kv, kb
        self.widen = widen
        self.exact = exact

    def fused_ids(self, query_embedding: list[float], query: str, qfilter: models.Filter,
                  matches: Callable[[dict], bool], needs_post: bool) -> list[str]:
        """1フィルタ条件でのベクトル＋BM25→RRF融合の id 列（best-first, 未truncate）。"""
        widen = self.widen if needs_post else 1

        # ベクトル: qfilter を push-down し、後段の条件があるときだけ広く取る。
        vids = self.store.search_dense(self.corpus, query_embedding, qfilter,
                                       self.kv * widen, exact=self.exact)
        if needs_post:
            vids = [i for i in vids if matches(self.meta_of.get(i, {}))]
        vids = vids[: self.kv]

        # BM25: sparse は qfilter を push-down（真のフィルタ内 top-k）し、後段の条件だけ Python。
        # in-memory 版は push 不可のため広めに取って全条件を Python で後段フィルタ。
        if isinstance(self.sparse, QdrantBM25):
            bids = self.sparse.search(query, self.kb * widen, qfilter)
            if needs_post:
                bids = [i for i in bids if matches(self.meta_of.get(i, {}))]
            bids = bids[: self.kb]
        else:
            bids = self.sparse.search(query, self.kb * self.widen)
            bids = [i for i in bids if matches(self.meta_of.get(i, {}))][: self.kb]

        return rrf_fuse([vids, bids])
