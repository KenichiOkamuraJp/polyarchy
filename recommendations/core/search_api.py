"""
Phase 10：本番検索を「薄く包む」共有サービス（retrieval-as-a-tool の中核）。

`recommendations.eval.eval`（retrieval.py）と同じ本番検索経路（ハイブリッド BM25＋ruri→RRF＋比較型
マルチクエリ分解 → jp_reranker_xsmall_v1）を**関数化して 1 箇所に集約**し、MCP サーバ
（`recommendations.serving.mcp_server`）と多段検索ハーネス（`recommendations.core.multistage`）の両方から呼べるようにする。
**新規の検索ロジックは書かない**＝既存の `production_retriever` ＋
`production_reranker_postprocessor` ＋ `SearchFilter` をそのまま組み立てるだけ。

■ 機密の物理遮断（Phase 10 の最重要要件・§25.4）
本サービスの公開メソッド `search()` は **layer 引数を受け付けない**。フィルタは常に
`layers=PUBLIC_ONLY`（＝公開のみ）で構築し、さらに取得後に「公開以外を落とす」
**境界フェイルクローズ**を二重に掛ける。したがって機密チャンクは、たとえコレクション内に
存在しても、この境界の外へは物理的に出られない。
"""
from dataclasses import dataclass
from typing import Optional

from llama_index.core import Settings
from llama_index.core.schema import QueryBundle

from recommendations.core.config import (
    COLLECTION_NAME,
    DEFAULT_LAYERS,
    HYBRID_SEARCH,
    PRODUCTION_RERANKER,
    RERANK_RETRIEVE_K,
    TOP_K,
)
from recommendations.core.embeddings import production_embed_model
from recommendations.core.filters import SearchFilter, policy_tags_of
from recommendations.core.hybrid import production_retriever
from polyarchy_common.logsetup import configure_quiet_logging, get_logger, quiet_stdout
from recommendations.core.rerankers import production_reranker_postprocessor

# 外部境界で固定する層。config.DEFAULT_LAYERS（=("公開",)）と一致。ここを変数化しない
# ＝MCP から機密層を要求する術がない（物理遮断）。
PUBLIC_ONLY: tuple[str, ...] = DEFAULT_LAYERS


@dataclass
class Chunk:
    """検索結果 1 チャンク。MCP は `to_dict()` を JSON 化して返す。"""

    rank: int
    score: float
    file_name: str
    org: str
    org_type: str
    title: str
    date: str
    date_int: Optional[int]
    doc_type: str
    field_tags: str
    # 統一 21 分類の分野タグ（polyarchy_common.taxonomy・全文書に付与）。field 絞りの主な照合先（B22）。
    policy_tags: list[str]
    # 原典 PDF の通し番号（1 始まり・取込メタ page_label）。印字ページ番号とは一致しない場合がある（M5）。
    page: str
    layer: str
    source_url: str
    text: str
    # diversify（文書単位の重複抑制）時のみ非0：候補プール内の同一文書の追加ヒット数。
    same_doc_hits: int = 0

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "score": round(self.score, 4),
            "file_name": self.file_name,
            "org": self.org,
            "org_type": self.org_type,
            "title": self.title,
            "date": self.date,
            "date_int": self.date_int,
            "doc_type": self.doc_type,
            "field_tags": self.field_tags,
            "policy_tags": self.policy_tags,
            "page": self.page,
            "layer": self.layer,
            "source_url": self.source_url,
            "text": self.text,
            "same_doc_hits": self.same_doc_hits,
        }


def _page_of(meta: dict) -> str:
    return str(meta.get("page_label") or meta.get("source") or "不明")


def _date_int_of(meta: dict) -> Optional[int]:
    di = meta.get("date_int")
    if isinstance(di, int):
        return di
    try:
        return int(di) if di not in (None, "") else None
    except (TypeError, ValueError):
        return None


class PolicySearchService:
    """本番検索スタックを 1 回だけ読み込み、`search()` で何度でも叩けるサービス。

    起動時に 埋め込み(ruri)・Qdrant コーパス参照・ハイブリッド retriever（BM25 sparse を
    含む）・リランカーを構築する（156k チャンクで十数秒）。以後の
    1 検索は ~0.6 秒（本番 p50 と同じ）。フィルタは `retriever.set_filter()` で差し替える
    （BM25 索引・埋め込みは再構築しない）。

    text_chars: 各チャンク本文の返却上限（0 以下で無制限）。MCP のペイロードを軽くする用途。
    """

    def __init__(self, collection_name: str = COLLECTION_NAME,
                 default_top_k: int = TOP_K, text_chars: int = 1200,
                 logger=None):
        configure_quiet_logging()
        self.log = logger or get_logger("polyarchy.search")
        self.collection_name = collection_name
        self.default_top_k = default_top_k
        self.text_chars = text_chars

        # llama_index の生 print（MockLLM/MockEmbedding）を飲み込みつつ構築する。
        with quiet_stdout():
            self.embed_model = production_embed_model()
            Settings.embed_model = self.embed_model
            Settings.llm = None  # 生成はサービス外（検索のみ・ローカル完結）

            # バッチ2 段4（2026-08-28・Chroma 全廃）：コレクション参照は Qdrant のみ。
            # collection はコーパス名の str（CORPUS_REGISTRY で解決）・件数確認も Qdrant に対して行う。
            from recommendations.core.qdrant_store import QdrantCorpusStore
            n = QdrantCorpusStore().count(collection_name)
            if n == 0:
                raise RuntimeError(
                    f"Qdrant コレクション {collection_name} が空です（先に qdrant_ingest が必要）")
            self.collection = collection_name  # str＝hybrid が Qdrant 起点で構築する
            self.index = None  # ハイブリッド経路のみ（互換のため属性は残す）

            # リランカーの top_n はプール全件に広げる（C3 検証＝diversify 用にリランク順列を保持）。
            # スコアリングは従来からプール全件に行っており top_n は truncate 位置のみ
            # ＝既定経路（search の nodes[:k]）の上位 k は不変（ゲートで同一性確認済）。
            self.pool_k = (max(RERANK_RETRIEVE_K, default_top_k)
                           if PRODUCTION_RERANKER else default_top_k)
            self.reranker = production_reranker_postprocessor(top_n=self.pool_k)
            # 既定フィルタ（公開固定）で 1 度だけ retriever を組む。以後は set_filter で条件差し替え。
            self.retriever = production_retriever(
                self.index, self.collection, self.embed_model, self.pool_k,
                SearchFilter(layers=PUBLIC_ONLY))
        # チャンク総数（backend 非依存）。UI（app/chat_app）はこちらを参照する
        # （qdrant では self.collection が str のため collection.count() は使えない）。
        self.chunk_count = n
        self._can_set_filter = hasattr(self.retriever, "set_filter")
        self.log.info(
            "検索サービス初期化完了: コレクション=%s チャンク数=%d ハイブリッド=%s "
            "リランカー=%s 候補プール=%d→top-%d 層=固定[%s]",
            collection_name, n, HYBRID_SEARCH, PRODUCTION_RERANKER or "無効",
            self.pool_k, default_top_k, "・".join(PUBLIC_ONLY))

    def _make_filter(self, orgs, since, until, field) -> SearchFilter:
        """公開層に**固定**して SearchFilter を組む（layer は引数で受け取らない＝物理遮断）。"""
        return SearchFilter(
            orgs=tuple(orgs) if orgs else (),
            date_from=since,
            date_to=until,
            field_tag=field,
            layers=PUBLIC_ONLY,  # ← 常に公開固定。呼び出し側は上書きできない。
        )

    def search(self, query: str, orgs=None, since: Optional[int] = None,
               until: Optional[int] = None, field: Optional[str] = None,
               top_k: Optional[int] = None, diversify: bool = False) -> list[Chunk]:
        """本番検索経路でランク済チャンクを返す（層は公開固定）。

        orgs:  団体コード列（keidanren/gov/rengo/nissho/doyukai）。空=団体で絞らない。
        since/until: YYYYMMDD の int（含む）。field: 分野の部分一致（統一 21 分類 policy_tags 主・
          発行元タグ field_tags 副＝`core/filters.field_tag_matches`）。
        diversify: True で**文書単位の重複抑制**＝リランク順列から file_name 初出チャンク
          のみを採用（opt-in・C3 検証中）。gold 文書の初出順位は構造的に下がらない（他文書の
          重複を除くだけ）＝hit@5/MRR は非劣化（scratchpad/b4_dedup_study.py で実測：
          実効文書数 3.80→5.00・hit@5 81.8→83.0%）。プール内の同一文書の追加ヒット数は
          Chunk.same_doc_hits で返す。
        戻り値: スコア降順の Chunk 列（`.to_dict()` で JSON 化可能）。
        """
        k = top_k or self.default_top_k
        sf = self._make_filter(orgs, since, until, field)

        if self._can_set_filter:
            self.retriever.set_filter(sf)
            nodes = self.retriever.retrieve(query)
        else:
            r = production_retriever(self.index, self.collection, self.embed_model,
                                     self.pool_k, sf)
            nodes = r.retrieve(query)
        if self.reranker is not None:
            nodes = self.reranker.postprocess_nodes(nodes, query_bundle=QueryBundle(query))
        same_doc_hits: dict[str, int] = {}
        if diversify:
            seen_files: set[str] = set()
            picked = []
            for nd in nodes:
                fn = nd.node.metadata.get("file_name") or "不明"
                if fn in seen_files:
                    same_doc_hits[fn] = same_doc_hits.get(fn, 0) + 1
                    continue
                seen_files.add(fn)
                picked.append(nd)
            nodes = picked[:k]
        else:
            nodes = nodes[:k]

        # ── 境界フェイルクローズ：公開以外は物理的に落とす（二重ガード）。
        # フィルタで既に公開のみのはずだが、万一の取りこぼしも境界で必ず遮断する。
        dropped = [n for n in nodes if (n.node.metadata.get("layer") or "公開") != "公開"]
        if dropped:
            self.log.warning("境界フェイルクローズ: 非公開チャンク %d 件を遮断（公開固定違反の保険）",
                             len(dropped))
        nodes = [n for n in nodes if (n.node.metadata.get("layer") or "公開") == "公開"]

        out: list[Chunk] = []
        for i, n in enumerate(nodes, 1):
            m = n.node.metadata
            text = n.node.get_content() or ""
            if self.text_chars > 0 and len(text) > self.text_chars:
                text = text[: self.text_chars] + "…"
            out.append(Chunk(
                rank=i,
                score=float(n.score) if n.score is not None else 0.0,
                file_name=m.get("file_name") or "不明",
                org=m.get("org") or "不明",
                org_type=m.get("org_type") or "",
                title=m.get("title") or "",
                date=m.get("date") or "",
                date_int=_date_int_of(m),
                doc_type=m.get("doc_type") or "",
                field_tags=m.get("field_tags") or "",
                policy_tags=policy_tags_of(m),
                page=_page_of(m),
                layer=m.get("layer") or "公開",
                source_url=m.get("source_url") or "",
                text=text,
                same_doc_hits=same_doc_hits.get(m.get("file_name") or "不明", 0),
            ))
        return out


