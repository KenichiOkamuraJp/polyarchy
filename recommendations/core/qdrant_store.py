"""
Phase B（B1）：Qdrant 検索アダプタ——**コーパス単位の契約**（長期開発計画 §6-1）。

最小契約は `search(corpus, query, filter, k)`。本モジュールはその Qdrant 実装の土台で、
埋め込み計算は持たない（呼び出し側が query embedding を渡す＝既存 HybridRetriever の
分業と同じ）。以後のコーパス追加は registry への登録のみで、既存コーパスの索引にも
回帰テストにも触れない。

フィルタは既存 `recommendations/core/filters.SearchFilter` をそのまま受け取り、Qdrant の Filter に翻訳する
（org $in / layer $in / date_int range。field_tags 部分一致は
現行どおり呼び出し側の Python 後段フィルタ）。**layer は共通コアの不変条件**であり、
翻訳時に必ず must 条件へ載る（SearchFilter の既定が公開のみ）。
"""
import os

from qdrant_client import QdrantClient, models

from recommendations.core.config import COLLECTION_NAME
from recommendations.core.filters import SearchFilter

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")  # 唯一の定義
DENSE_NAME = "dense"
SPARSE_NAME = "bm25"
DENSE_DIM = 768


def ensure_collection(qc: QdrantClient, name: str, recreate: bool) -> None:
    """コレクションと payload index を用意する（存在すれば再利用・recreate で作り直し）。

    バッチ2 段4（2026-08-28・Chroma 全廃）：スキーマ定義を qdrant_migrate.py（削除済）から移設。
    ingest（qdrant_ingest.py）が唯一の呼び出し元。
    """
    if recreate and qc.collection_exists(name):
        qc.delete_collection(name)
    if not qc.collection_exists(name):
        qc.create_collection(
            collection_name=name,
            vectors_config={DENSE_NAME: models.VectorParams(
                size=DENSE_DIM, distance=models.Distance.COSINE)},
            # 予約枠：重みはクライアント側で計算して載せる（IDF modifier なし＝rank_bm25 一致用）
            sparse_vectors_config={SPARSE_NAME: models.SparseVectorParams()},
        )
        # フィルタ push-down 用 index（filters.SearchFilter と同じ3条件）
        qc.create_payload_index(name, "org", models.PayloadSchemaType.KEYWORD)
        qc.create_payload_index(name, "layer", models.PayloadSchemaType.KEYWORD)
        qc.create_payload_index(name, "date_int", models.PayloadSchemaType.INTEGER)

# コーパス名 → Qdrant コレクション名。コーパス追加はここへの登録のみ（既存に触れない）。
CORPUS_REGISTRY: dict[str, str] = {
    COLLECTION_NAME: COLLECTION_NAME,  # 自己写像（env の COLLECTION_NAME をそのまま解決＝一時コレクションもこれで通る）
    # 政策主張DB＝バッチ1（改名＋issuer）＋B4 180k 拡大（155,663点・ゲート済）。
    # C2 適用の既定は v7 と決定（2026-08-13・公開情報の網羅が価値のため C1 を待たず確定）。
    # 切り戻し段：v7 → v6（バッチ1のみ・80k）。切替は env 1行（Chroma v5 経路は段4で全廃）。
    "policy_claims": "policy_claims_v7",
}


def qdrant_filter(sf: SearchFilter) -> models.Filter | None:
    """SearchFilter → Qdrant Filter（chroma_where と同一意味論・field_tags は対象外）。"""
    must: list[models.Condition] = []
    if sf.orgs:
        must.append(models.FieldCondition(key="org", match=models.MatchAny(any=list(sf.orgs))))
    if sf.layers:
        must.append(models.FieldCondition(key="layer", match=models.MatchAny(any=list(sf.layers))))
    if sf.date_from is not None or sf.date_to is not None:
        must.append(models.FieldCondition(key="date_int", range=models.Range(
            gte=sf.date_from, lte=sf.date_to)))
    return models.Filter(must=must) if must else None


class QdrantCorpusStore:
    """コーパス単位の Qdrant ストア。dense 検索と全件読み出し（BM25 索引構築用）を提供する。"""

    def __init__(self, url: str = QDRANT_URL,
                 registry: dict[str, str] | None = None, timeout: int = 60):
        self.qc = QdrantClient(url=url, timeout=timeout)
        self.registry = registry or dict(CORPUS_REGISTRY)

    def _collection(self, corpus: str) -> str:
        if corpus not in self.registry:
            raise KeyError(f"未登録のコーパス: {corpus}（registry: {list(self.registry)}）")
        return self.registry[corpus]

    def count(self, corpus: str) -> int:
        return self.qc.count(self._collection(corpus), exact=True).count

    def search_dense(self, corpus: str, query_embedding: list[float],
                     sf: SearchFilter, k: int, exact: bool = False) -> list[str]:
        """dense（ruri）ベクトル検索の上位 k チャンク id を降順で返す。

        exact=True で HNSW を迂回した厳密検索（パリティ検証・少量クエリ向け）。
        field_tags 部分一致は呼び出し側で後段フィルタする（現行 HybridRetriever と同じ分業）。
        """
        res = self.qc.query_points(
            self._collection(corpus),
            query=query_embedding,
            using=DENSE_NAME,
            query_filter=qdrant_filter(sf),
            limit=k,
            with_payload=False,
            search_params=models.SearchParams(exact=exact),
        )
        return [str(p.id) for p in res.points]

    def get_all(self, corpus: str, batch: int = 2_000) -> dict:
        """全チャンクの id・本文・メタデータを返す（BM25 索引・id→meta 辞書の構築用）。

        dict {ids, documents, metadatas} を返し、BM25Index 構築コードへそのまま差し込める。
        """
        out = {"ids": [], "documents": [], "metadatas": []}
        offset = None
        name = self._collection(corpus)
        while True:
            points, offset = self.qc.scroll(name, limit=batch, offset=offset,
                                            with_payload=True, with_vectors=False)
            for p in points:
                payload = dict(p.payload or {})
                out["ids"].append(str(p.id))
                out["documents"].append(payload.pop("text", ""))
                out["metadatas"].append(payload)
            if offset is None:
                break
        return out
