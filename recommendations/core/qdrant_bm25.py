"""
Phase B（B1）：BM25 を Qdrant の sparse ベクトルへ移す（in-memory BM25 の 180k 破綻問題の解消側）。

式（rank_bm25 = BM25Okapi と厳密に同じ）・語彙サイドカー・検索の実装は共有ライブラリ
`polyarchy_retrieval.bm25`（2026-10-03 に切り出し）。本モジュールは政策主張DB の置き場
（`data/bm25/<collection>_vocab.json.gz`）とコレクションの既定（COLLECTION_NAME）を与える。
BM25 は本質的にコーパス大域統計（idf・avgdl）に依存するため、**文書追加時は再構築**。

実行（sparse 重みの構築・冪等）:
    python -m recommendations.core.qdrant_bm25            # 語彙構築→全点に sparse 重みを upsert
検索は QdrantBM25（hybrid.py が使用）。
"""
from polyarchy_common.logsetup import configure_quiet_logging
from polyarchy_retrieval.bm25 import QdrantBM25 as _QdrantBM25
from polyarchy_retrieval.bm25 import build_sparse_bm25 as _build_sparse_bm25
from recommendations.core.config import COLLECTION_NAME, DATA_DIR


def _vocab_path(collection: str):
    return DATA_DIR / "bm25" / f"{collection}_vocab.json.gz"


def build_sparse_bm25(collection: str = COLLECTION_NAME) -> None:
    """全チャンクをトークナイズし、doc 側 sparse 重みを Qdrant へ・語彙をサイドカーへ書く。"""
    from recommendations.core.qdrant_store import QdrantCorpusStore

    _build_sparse_bm25(QdrantCorpusStore(timeout=300), collection, _vocab_path(collection))


class QdrantBM25(_QdrantBM25):
    """Qdrant sparse による BM25 検索（語彙は `data/bm25/<collection>_vocab.json.gz`）。

    検索は `search(query, top_k, qdrant_filter(sf))`（layer 必須の Qdrant Filter で push-down）。
    """

    def __init__(self, store, collection: str = COLLECTION_NAME):
        super().__init__(store, collection, _vocab_path(collection),
                         missing_hint="（先に python -m recommendations.core.qdrant_bm25 を実行）")


def main() -> None:
    configure_quiet_logging()
    build_sparse_bm25()


if __name__ == "__main__":
    main()
