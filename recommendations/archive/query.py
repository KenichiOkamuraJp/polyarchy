"""
インタラクティブな質問応答スクリプト。

使い方:
    python -m recommendations.serving.query
    # Phase 6：構造化フィルタ（セッション単位で全設問に適用）
    python -m recommendations.serving.query --org keidanren --since 20230101      # 2023年以降の経団連だけ
    python -m recommendations.serving.query --org gov --org keidanren --field 環境  # 政府か経団連の「環境」分野
    python -m recommendations.serving.query --layer 機密                            # 層の指定（旧 CLI。現行のツールは公開層に固定）
"""
import argparse
import sys
from pathlib import Path

import chromadb
from llama_index.core import Settings, StorageContext, VectorStoreIndex
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.llms.anthropic import Anthropic
from llama_index.vector_stores.chroma import ChromaVectorStore

from recommendations.core.config import (
    ANTHROPIC_API_KEY,
    CHROMA_DIR,
    COLLECTION_NAME,
    DEFAULT_LAYERS,
    HYBRID_SEARCH,
    LLM_MODEL,
    PRODUCTION_RERANKER,
    RERANK_RETRIEVE_K,
    TOP_K,
)
from recommendations.core.embeddings import production_embed_model
from recommendations.core.filters import SearchFilter
from recommendations.core.hybrid import production_retriever
from recommendations.core.rerankers import production_reranker_postprocessor


SYSTEM_PROMPT = """あなたは政策文書の専門アナリストです。
提供された文書のみを根拠に、ユーザーの質問に回答してください。

回答時のルール：
- 必ず日本語で回答する
- 文書に書かれていない情報は推測せず、「文書からは確認できません」と明示する
- 根拠となる文書箇所を必ず出典として示す（ファイル名・該当部分の要旨）
- 複数の文書に異なる主張がある場合は、それぞれの立場を整理して提示する
- 簡潔で構造化された日本語で回答する
"""


def _parse_filter(argv=None) -> SearchFilter:
    """CLI 引数からセッション単位の SearchFilter を組み立てる（Phase 6）。"""
    p = argparse.ArgumentParser(description="政策文書QA（Phase 6 構造化フィルタ対応）")
    p.add_argument("--org", action="append", default=[],
                   help="団体で絞る（複数指定可: keidanren/gov/rengo/nissho）")
    p.add_argument("--since", type=int, default=None, help="この日付以降 YYYYMMDD")
    p.add_argument("--until", type=int, default=None, help="この日付以前 YYYYMMDD")
    p.add_argument("--field", default=None, help="分野タグの部分一致（例: 環境）")
    p.add_argument("--layer", action="append", default=None,
                   help="層で絞る（既定: 公開のみ）。機密は明示指定時のみ")
    a = p.parse_args(argv)
    return SearchFilter(
        orgs=tuple(a.org),
        date_from=a.since,
        date_to=a.until,
        field_tag=a.field,
        layers=tuple(a.layer) if a.layer else DEFAULT_LAYERS,
    )


def main() -> None:
    if not ANTHROPIC_API_KEY:
        sys.exit("ERROR: ANTHROPIC_API_KEY が .env に設定されていません")

    search_filter = _parse_filter()

    # 本番の埋め込み（ruri, ローカル）。OpenAI 依存は構築時の境界検出のみで、検索時は不要。
    Settings.embed_model = production_embed_model()
    Settings.llm = Anthropic(
        model=LLM_MODEL,
        api_key=ANTHROPIC_API_KEY,
        system_prompt=SYSTEM_PROMPT,
    )

    chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    chroma_collection = chroma_client.get_or_create_collection(COLLECTION_NAME)
    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    if chroma_collection.count() == 0:
        sys.exit("ERROR: Chromaコレクションが空です。先に `python -m recommendations.ingest.ingest` を実行してください")

    print(f"インデックス読み込み完了（{chroma_collection.count()}チャンク）")

    index = VectorStoreIndex.from_vector_store(
        vector_store,
        storage_context=storage_context,
    )

    # Phase 5：本番検索は「ハイブリッド(BM25＋ベクトル→RRF)で候補を広く取り→リランカーで
    # TOP_K に絞る」。HYBRID_SEARCH / PRODUCTION_RERANKER で各段をトグルできる。
    reranker = production_reranker_postprocessor(top_n=TOP_K)
    pool_k = RERANK_RETRIEVE_K if reranker else TOP_K
    retriever = production_retriever(index, chroma_collection, Settings.embed_model,
                                     pool_k, search_filter)
    print(f"検索構成: hybrid={HYBRID_SEARCH}"
          f"{', rerank=' + PRODUCTION_RERANKER if reranker else ''}"
          f"（候補{pool_k}→top-{TOP_K}）")
    print(f"フィルタ: {search_filter.describe()}")
    query_engine = RetrieverQueryEngine.from_args(
        retriever,
        llm=Settings.llm,
        node_postprocessors=[reranker] if reranker else [],
        response_mode="compact",
    )

    print("\n" + "=" * 60)
    print("質問を入力してください（終了: Ctrl+C または 空行）")
    print("=" * 60)

    while True:
        try:
            question = input("\n質問> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n終了します")
            break

        if not question:
            print("終了します")
            break

        print("\n--- 回答 ---")
        response = query_engine.query(question)
        print(response)

        print("\n--- 参照ソース ---")
        for i, node in enumerate(response.source_nodes, 1):
            metadata = node.node.metadata
            file_name = (
                metadata.get("file_name")
                or Path(metadata.get("file_path", "")).name
                or "不明"
            )
            page = (
                metadata.get("page_label")
                or metadata.get("source")
                or "不明"
            )
            score = node.score if node.score is not None else 0.0
            # 出典タグ表示（org / date / layer）＝ロードマップ §2.4「検索結果に出典タグ表示」。
            tag = "/".join(str(metadata.get(k)) for k in ("org", "date", "layer")
                           if metadata.get(k))
            print(f"[{i}] {file_name} (p.{page}) [{tag}] score={score:.3f}")


if __name__ == "__main__":
    main()
