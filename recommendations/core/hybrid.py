"""
Phase 5：ハイブリッド検索（BM25 語彙検索 ＋ ベクトル）と RRF 融合＝政策主張DB の retriever。

ベクトル（ruri）は意味的に近いチャンクを拾うが、**数値・固有名詞・制度名**の完全一致に
弱いことがある（政府/日商の設問に多い）。BM25 は逆に語彙一致に強い。両者の順位を
RRF（Reciprocal Rank Fusion）で融合し、リランカーに渡す候補プールの recall を上げる。

コーパスに依存しない部品（分かち書き・BM25・RRF・1 フィルタ条件分の融合）は共有ライブラリ
`polyarchy_retrieval` にある（2026-10-03 に切り出し＝審議会議事録DB_開発計画 §2.3）。本モジュールに
残すのは政策主張DB の固有部分＝SearchFilter の翻訳・比較型の団体別分解（Phase 7.5）・llama_index の包装。
バックエンドは Qdrant のみ（バッチ2 段4〔2026-08-28〕で Chroma 経路を全廃）：
ベクトルは dense 検索・BM25 は sparse サイドカー（`core/qdrant_bm25.py`）を push-down で使う。
サイドカーの無い一時コレクション（層ゲート自己検証など）だけ、明示（allow_inmemory_bm25=True）で in-memory BM25 を使う
（既定は止まる＝本番・ゲートが黙って別実装の BM25 で動かない・2026-10-09）。ローカル完結要件（ローカル/無料/外部送信なし）は不変。
"""
from polyarchy_retrieval.bm25 import BM25Index
from polyarchy_retrieval.fusion import rrf_fuse  # noqa: F401（再公開＝既存の import 先を保つ）
from polyarchy_retrieval.hybrid import HybridSearcher
from polyarchy_retrieval.tokenizer import tokenize_ja  # noqa: F401（再公開）


# ---------------------------------------------------------------------------
# 本番組み込み用：llama-index の Retriever 実装（ベクトル＋BM25→RRF、Phase 6 でフィルタ対応）
# ---------------------------------------------------------------------------
def build_hybrid_retriever(collection: str, embed_model, kv: int, kb: int, pool: int,
                           search_filter=None, allow_inmemory_bm25: bool = False):
    """Qdrant コーパスからハイブリッド retriever を構築して返す。

    `collection` はコーパス名の str（CORPUS_REGISTRY で解決。コレクション名の自己写像も可）。
    `allow_inmemory_bm25`：語彙サイドカーが無いとき in-memory BM25 で代替してよいか。True を渡すのは語彙を作らない
    一時コレクション（eval/filters の層ゲート自己検証・eval/mcp_layer_gate_test）だけ。既定 False＝語彙が無ければ
    FileNotFoundError で止まる（in-memory 版は絞り込みの候補の取り方が違う〔kb×widen を取って Python で絞る〕＝
    本番・ゲートが黙って別の意味論で動かない）。
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

    from recommendations.core.config import FILTER_WIDEN, QDRANT_EXACT
    from recommendations.core.filters import default_filter
    from recommendations.core.qdrant_store import QdrantCorpusStore, qdrant_filter

    qstore = QdrantCorpusStore()
    qcorpus = collection

    g = qstore.get_all(qcorpus)
    id2text = {i: (d or "") for i, d in zip(g["ids"], g["documents"])}
    id2meta = {i: m for i, m in zip(g["ids"], g["metadatas"])}

    # BM25 側：sparse サイドカー（語彙）を使い、in-memory 索引は構築しない（毎起動の
    # 全件トークナイズを省く＝180k 破綻問題の解消側）。語彙サイドカーの無い一時コレクション
    # （層ゲート自己試験など）だけ、明示で in-memory 版へ切り替える。
    qbm25 = None
    from polyarchy_common.logsetup import get_logger
    from recommendations.core.qdrant_bm25 import QdrantBM25
    try:
        qbm25 = QdrantBM25(qstore, qcorpus)
    except FileNotFoundError as e:
        if not allow_inmemory_bm25:
            raise
        get_logger("polyarchy.hybrid").warning(
            "BM25 sparse 未構築のため in-memory 版で代替（一時コレクションの明示の許可）: %s", e)
    bm25 = None if qbm25 is not None else BM25Index(
        g["ids"], [d or "" for d in g["documents"]],
        [m.get("file_name", "不明") for m in g["metadatas"]])

    searcher = HybridSearcher(qstore, qcorpus, qbm25 if qbm25 is not None else bm25, id2meta,
                              kv, kb, widen=FILTER_WIDEN, exact=QDRANT_EXACT)

    class HybridRetriever(BaseRetriever):
        def __init__(self, sf):
            self._embed = embed_model
            self._searcher = searcher
            self._pool = pool
            self._filter = sf
            super().__init__()

        def set_filter(self, sf) -> None:
            """検索フィルタを差し替える（BM25 索引・埋め込みは再構築しない）。"""
            self._filter = sf

        def _fused_ids(self, qemb, q: str, sf) -> "list[str]":
            """1フィルタ条件でのベクトル＋BM25→RRF融合の id 列（best-first, 未truncate）。

            org/date/layer は Qdrant へ push-down、分野の部分一致（push できない）だけ候補を
            FILTER_WIDEN 倍に広げて `sf.matches` で後段フィルタする（`polyarchy_retrieval.hybrid`）。
            """
            return self._searcher.fused_ids(qemb, q, qdrant_filter(sf), sf.matches,
                                            sf.needs_python_postfilter())

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


def production_retriever(collection: str, embed_model, pool_k: int, search_filter=None,
                         allow_inmemory_bm25: bool = False):
    """本番の retriever を返す（ハイブリッド一本＝Qdrant dense＋BM25 sparse→RRF）。

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
                                  pool_k, sf, allow_inmemory_bm25=allow_inmemory_bm25)
