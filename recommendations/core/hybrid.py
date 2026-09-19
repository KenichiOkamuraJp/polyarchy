"""
Phase 5：ハイブリッド検索（BM25 語彙検索 ＋ ベクトル）と RRF 融合。

ベクトル（ruri）は意味的に近いチャンクを拾うが、**数値・固有名詞・制度名**の完全一致に
弱いことがある（政府/日商の設問に多い）。BM25 は逆に語彙一致に強い。両者の順位を
RRF（Reciprocal Rank Fusion）で融合し、リランカーに渡す候補プールの recall を上げる。

トークナイズは fugashi（MeCab, ローカル/無料）＝リランカーと同じ日本語形態素。
バックエンドは Qdrant のみ（バッチ2 段4〔2026-08-28〕で Chroma 経路を全廃）：
ベクトルは dense 検索・BM25 は sparse サイドカー（`core/qdrant_bm25.py`）を push-down で使い、
サイドカーの無い一時コレクション（層ゲート自己検証など）だけ in-memory BM25 に
フォールバックする。ローカル完結要件（ローカル/無料/外部送信なし）は不変。
"""
from rank_bm25 import BM25Okapi

_TAGGER = None


def _tagger():
    """fugashi Tagger を遅延生成（プロセス内で1回だけロード）。"""
    global _TAGGER
    if _TAGGER is None:
        import fugashi
        _TAGGER = fugashi.Tagger()
    return _TAGGER


def tokenize_ja(text: str) -> list[str]:
    """日本語を形態素の表層形に分割（空白のみのトークンは除外）。BM25 用。"""
    return [w.surface for w in _tagger()(text) if w.surface.strip()]


class BM25Index:
    """全チャンク本文に対する in-memory BM25 索引。search(query, k) で上位 chunk id を返す。

    本線は Qdrant sparse サイドカー（qdrant_bm25）＝本索引は語彙サイドカーの無い
    一時コレクション（層ゲート自己検証など）用のフォールバック。
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


def rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[str]:
    """複数の順位リスト（各々 best-first の id 列）を RRF で融合し、id を降順で返す。

    RRF スコア = Σ 1/(k + rank)。k はスコア平滑化の定数（60 が慣例）。
    順位のみ使うため、ベクトル距離と BM25 スコアのスケール差を気にせず融合できる。
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, _id in enumerate(ranking, 1):
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda i: scores[i], reverse=True)


# ---------------------------------------------------------------------------
# 本番組み込み用：llama-index の Retriever 実装（ベクトル＋BM25→RRF、Phase 6 でフィルタ対応）
# ---------------------------------------------------------------------------
def build_hybrid_retriever(collection, embed_model, kv: int, kb: int, pool: int,
                           search_filter=None):
    """Qdrant コーパスからハイブリッド retriever を構築して返す。

    `collection` はコーパス名の str（CORPUS_REGISTRY で解決。コレクション名の自己写像も可）。
    _retrieve(query) で：ruri クエリ埋め込みの dense 検索上位 kv ＋ BM25 上位 kb を
    RRF 融合 → 上位 pool を NodeWithScore で返す（後段のリランカーが top_k に絞る）。
    起動時に全チャンクを1回読み込み id→text/metadata を作る（数秒）。

    Phase 6：`search_filter`（`recommendations/core/filters.SearchFilter`）で団体・日付・分野・層を絞る。
    - ベクトル側は Qdrant filter push-down（org/layer/date をインデックスで絞る）。
    - BM25 sparse も同条件を push-down。field_tags 部分一致だけは push できないので
      候補を広げ Python で後段フィルタ（FILTER_WIDEN）。
    フィルタは `set_filter()` で後から差し替え可（索引を作り直さず、eval の設問毎切替に使う）。
    """
    from llama_index.core.retrievers import BaseRetriever
    from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

    from recommendations.core.config import FILTER_WIDEN
    from recommendations.core.filters import default_filter
    from recommendations.core.qdrant_store import QdrantCorpusStore

    qstore = QdrantCorpusStore()
    qcorpus = (collection if isinstance(collection, str)
               else getattr(collection, "name", None) or "policy_claims")

    g = qstore.get_all(qcorpus)
    id2text = {i: (d or "") for i, d in zip(g["ids"], g["documents"])}
    id2meta = {i: m for i, m in zip(g["ids"], g["metadatas"])}

    # BM25 側：sparse サイドカー（語彙）を使い、in-memory 索引は構築しない（毎起動の
    # 全件トークナイズを省く＝180k 破綻問題の解消側）。語彙サイドカーの無い一時コレクション
    # （層ゲート自己試験など）は in-memory 版へフォールバック。
    qbm25 = None
    from polyarchy_common.logsetup import get_logger
    from recommendations.core.qdrant_bm25 import QdrantBM25
    try:
        qbm25 = QdrantBM25(qstore, qcorpus)
    except FileNotFoundError as e:
        get_logger("polyarchy.hybrid").info(
            "BM25 sparse 未構築のため in-memory 版で代替: %s", e)
    bm25 = None if qbm25 is not None else BM25Index(
        g["ids"], [d or "" for d in g["documents"]],
        [m.get("file_name", "不明") for m in g["metadatas"]])

    class HybridRetriever(BaseRetriever):
        def __init__(self, sf):
            self._embed = embed_model
            self._bm25 = bm25
            self._kv, self._kb, self._pool = kv, kb, pool
            self._filter = sf
            super().__init__()

        def set_filter(self, sf) -> None:
            """検索フィルタを差し替える（BM25 索引・埋め込みは再構築しない）。"""
            self._filter = sf

        def _fused_ids(self, qemb, q: str, sf) -> "list[str]":
            """1フィルタ条件でのベクトル＋BM25→RRF融合の id 列（best-first, 未truncate）。"""
            widen = FILTER_WIDEN if sf.needs_python_postfilter() else 1

            # ベクトル: org/date/layer を push-down で絞り、field_tags 有効時のみ広く取る。
            from recommendations.core.config import QDRANT_EXACT
            vids = qstore.search_dense(qcorpus, qemb, sf,
                                       self._kv * widen, exact=QDRANT_EXACT)
            if sf.needs_python_postfilter():
                vids = [i for i in vids if sf.matches(id2meta.get(i, {}))]
            vids = vids[: self._kv]

            # BM25: sparse は org/layer/date を push-down（真のフィルタ内 top-k）し、
            # field_tags のみ Python 後段。in-memory フォールバックは push 不可のため
            # 広めに取って全条件を Python で後段フィルタ。
            if qbm25 is not None:
                bids = qbm25.search(q, self._kb * widen, sf)
                if sf.needs_python_postfilter():
                    bids = [i for i in bids if sf.matches(id2meta.get(i, {}))]
                bids = bids[: self._kb]
            else:
                bids = self._bm25.search(q, self._kb * FILTER_WIDEN)
                bids = [i for i in bids if sf.matches(id2meta.get(i, {}))][: self._kb]

            return rrf_fuse([vids, bids])

        def _retrieve(self, query_bundle: "QueryBundle") -> "list[NodeWithScore]":
            from dataclasses import replace

            from recommendations.core.config import MULTIQUERY_COMPARATIVE
            from recommendations.core.multiquery import detect_comparative_orgs, interleave_unique

            q = query_bundle.query_str
            sf = self._filter
            qemb = self._embed.get_query_embedding(q)

            # Phase 7.5: 比較型（2団体以上の言及＋比較マーカー）は団体別に検索して
            # 候補を均等合流する。単一クエリだと片団体がプールを占有し、もう片方の
            # 正解が沈むため（§20.4）。利用側が org フィルタを明示したときは尊重して分解しない。
            orgs = (detect_comparative_orgs(q)
                    if MULTIQUERY_COMPARATIVE and not sf.orgs else ())
            if orgs:
                pools = [self._fused_ids(qemb, q, replace(sf, orgs=(o,)))
                         for o in orgs]
                fused = interleave_unique(pools, self._pool)
            else:
                fused = self._fused_ids(qemb, q, sf)[: self._pool]
            out = []
            for rank, cid in enumerate(fused, 1):
                node = TextNode(text=id2text.get(cid, ""), id_=cid,
                                metadata=id2meta.get(cid, {}))
                # 融合順の暫定スコア（リランカーが上書きする）。
                out.append(NodeWithScore(node=node, score=1.0 / rank))
            return out

    return HybridRetriever(search_filter or default_filter())


def production_retriever(index, collection, embed_model, pool_k: int, search_filter=None):
    """本番の retriever を返す（ハイブリッド一本＝Qdrant dense＋BM25 sparse→RRF）。

    `index` は旧 Chroma 経路の名残の互換引数（常に None を渡してよい・未使用）。
    search_api / eval の本番入口はこれを使う（＝本番 retriever の単一の真実源）。
    後段の reranker が pool_k から TOP_K に絞る想定。

    Phase 6：`search_filter`（省略時は公開層のみの既定フィルタ）で構造化フィルタ検索。
    """
    from recommendations.core.config import HYBRID_KB, HYBRID_KV, HYBRID_SEARCH
    from recommendations.core.filters import default_filter

    if not HYBRID_SEARCH:
        raise RuntimeError("HYBRID_SEARCH 無効（ベクトル単独）は Chroma 全廃（バッチ2 段4）で"
                           "サポート外になりました＝本番既定はハイブリッド一本")
    sf = search_filter or default_filter()
    return build_hybrid_retriever(collection, embed_model, HYBRID_KV, HYBRID_KB,
                                  pool_k, sf)
