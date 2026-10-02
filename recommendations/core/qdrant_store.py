"""
Phase B（B1）：Qdrant 検索アダプタ——**コーパス単位の契約**（長期開発計画 §6-1）。

最小契約は `search(corpus, query, filter, k)`。土台（ストア・コレクションの作成・layer 必須の検査）は
共有ライブラリ `polyarchy_retrieval.qdrant`（2026-10-03 に切り出し）で、本モジュールは政策主張DB の
固有部分＝コーパスの登録・SearchFilter の翻訳・コレクションの形（次元・絞り込み用の索引）を持つ。
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

from polyarchy_retrieval.qdrant import DENSE_NAME, SPARSE_NAME  # noqa: F401（再公開＝取込・評価が使う）
from polyarchy_retrieval.qdrant import QdrantCorpusStore as _CorpusStore
from polyarchy_retrieval.qdrant import ensure_collection as _ensure_collection
from recommendations.core.config import COLLECTION_NAME
from recommendations.core.filters import SearchFilter

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")  # 唯一の定義
DENSE_DIM = 768  # ruri-v3-310m


def ensure_collection(qc: QdrantClient, name: str, recreate: bool) -> None:
    """コレクションと payload index を用意する（存在すれば再利用・recreate で作り直し）。

    バッチ2 段4（2026-08-28・Chroma 全廃）：スキーマ定義を qdrant_migrate.py（削除済）から移設。
    ingest（qdrant_ingest.py）が唯一の呼び出し元。絞り込み用の索引は filters.SearchFilter と同じ3条件。
    """
    _ensure_collection(qc, name, recreate, dense_dim=DENSE_DIM,
                       keyword_fields=("org", "layer"), integer_fields=("date_int",))


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


class QdrantCorpusStore(_CorpusStore):
    """政策主張DB の Qdrant ストア（registry の既定＝構築時の CORPUS_REGISTRY の写し・url の既定＝QDRANT_URL）。

    dense 検索（`search_dense(corpus, emb, qdrant_filter(sf), k, exact)`・layer 必須）と全件読み出しは
    共有ライブラリ（`polyarchy_retrieval.qdrant.QdrantCorpusStore`）。
    """

    def __init__(self, url: str = QDRANT_URL,
                 registry: dict[str, str] | None = None, timeout: int = 60):
        super().__init__(registry or dict(CORPUS_REGISTRY), url=url, timeout=timeout)
