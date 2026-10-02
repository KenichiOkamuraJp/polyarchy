"""
Qdrant のコーパス単位のストア（長期開発計画 §6-1「検索アダプタの契約をコーパス単位にする」）。

埋め込み計算は持たない（呼び出し側が query embedding を渡す）。フィルタの意味は各コーパスが持ち、
本モジュールは Qdrant の Filter をそのまま受け取る。**層は共通コアの不変条件**＝検索に渡す
Filter に `layer` の条件が無ければ拒む（`require_layer`）。コレクションの作成も `layer` の
絞り込み用の索引を必須にする。
"""
import os

from qdrant_client import QdrantClient, models

DENSE_NAME = "dense"
SPARSE_NAME = "bm25"
LAYER_KEY = "layer"


def require_layer(qfilter: models.Filter | None) -> models.Filter:
    """検索の Filter が `layer` の条件を must に持つことを確かめて返す（無ければ ValueError）。

    層の絞り込みを各コーパスの書き忘れに任せない＝公開固定（フェイルクローズ）を共有側でも担保する。
    """
    must = (qfilter.must if qfilter is not None else None) or []
    if not isinstance(must, list):
        must = [must]
    if not any(isinstance(c, models.FieldCondition) and c.key == LAYER_KEY for c in must):
        raise ValueError("検索の Filter に layer の条件がありません（層は全コーパス共通の不変条件）")
    return qfilter


def ensure_collection(qc: QdrantClient, name: str, recreate: bool, *, dense_dim: int,
                      keyword_fields: tuple[str, ...], integer_fields: tuple[str, ...] = ()) -> None:
    """コレクションと payload index を用意する（存在すれば再利用・recreate で作り直し）。

    dense（COSINE）と sparse（BM25 の予約枠＝重みはクライアント側で計算して載せる。IDF modifier
    なし＝rank_bm25 一致用）の 2 本。絞り込み用の索引は keyword_fields → integer_fields の順に作る。
    keyword_fields に `layer` が無ければ ValueError（層の push-down を必ず効かせる）。
    """
    if LAYER_KEY not in keyword_fields:
        raise ValueError("keyword_fields に layer が必要です（層は全コーパス共通の不変条件）")
    if recreate and qc.collection_exists(name):
        qc.delete_collection(name)
    if not qc.collection_exists(name):
        qc.create_collection(
            collection_name=name,
            vectors_config={DENSE_NAME: models.VectorParams(
                size=dense_dim, distance=models.Distance.COSINE)},
            sparse_vectors_config={SPARSE_NAME: models.SparseVectorParams()},
        )
        for f in keyword_fields:
            qc.create_payload_index(name, f, models.PayloadSchemaType.KEYWORD)
        for f in integer_fields:
            qc.create_payload_index(name, f, models.PayloadSchemaType.INTEGER)


class QdrantCorpusStore:
    """コーパス単位の Qdrant ストア。dense 検索と全件読み出し（BM25・id→meta 辞書の構築用）を提供する。

    registry＝コーパス名 → コレクション名（コーパスごとに持つ。コレクション名の自己写像も可）。
    url を省くと env `QDRANT_URL`（無ければ localhost）を**構築時に**読む。
    """

    def __init__(self, registry: dict[str, str], url: str | None = None, timeout: int = 60):
        self.qc = QdrantClient(url=url or os.getenv("QDRANT_URL", "http://localhost:6333"),
                               timeout=timeout)
        self.registry = registry

    def collection(self, corpus: str) -> str:
        """コーパス名 → コレクション名（未登録は KeyError）。"""
        if corpus not in self.registry:
            raise KeyError(f"未登録のコーパス: {corpus}（registry: {list(self.registry)}）")
        return self.registry[corpus]

    def count(self, corpus: str) -> int:
        return self.qc.count(self.collection(corpus), exact=True).count

    def search_dense(self, corpus: str, query_embedding: list[float],
                     qfilter: models.Filter, k: int, exact: bool = False) -> list[str]:
        """dense ベクトル検索の上位 k チャンク id を降順で返す（qfilter は layer 必須）。

        exact=True で HNSW を迂回した厳密検索（パリティ検証・少量クエリ向け）。
        push-down できない条件（部分一致など）は呼び出し側で後段フィルタする。
        """
        res = self.qc.query_points(
            self.collection(corpus),
            query=query_embedding,
            using=DENSE_NAME,
            query_filter=require_layer(qfilter),
            limit=k,
            with_payload=False,
            search_params=models.SearchParams(exact=exact),
        )
        return [str(p.id) for p in res.points]

    def get_all(self, corpus: str, batch: int = 2_000) -> dict:
        """全チャンクの id・本文・メタデータを返す（BM25 索引・id→meta 辞書の構築用）。

        dict {ids, documents, metadatas}。本文は payload の `text`（metadatas からは除く）。
        """
        out = {"ids": [], "documents": [], "metadatas": []}
        offset = None
        name = self.collection(corpus)
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
