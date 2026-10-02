"""
Phase 5：リランカー（クロスエンコーダ）の定義（レジストリ）。

埋め込み（bi-encoder）はクエリと文書を独立にベクトル化するため速いが精度に上限がある。
クロスエンコーダは (クエリ, 文書) を1本の入力として同時に符号化し関連度を直接スコア化する
ため精度が高い代わりに遅い。そこで **ベクトル検索で top-k を広め（例 k=20）に絞ってから
クロスエンコーダで再順位付けし top-5 に切る** 二段構えにする（retrieve→rerank）。

ローカル完結要件（ローカル/無料/外部送信なし）を維持するため、リランカーは全てローカル HF の
日本語対応クロスエンコーダ（sentence-transformers の CrossEncoder で読める重み）に限定する。
API は使わない。

登録（キー → 生成関数〔遅延〕）とクロスエンコーダの読み込みは共有ライブラリ
`polyarchy_retrieval.models` の RERANKERS（2026-10-03 に切り出し＝モデルの版の固定を 1 箇所に）。
本モジュールに残すのは llama_index の postprocessor の包装と、本番のキー（config.PRODUCTION_RERANKER）の解決。
CrossEncoder は初回のみモデルを DL しローカルにキャッシュする（以降はオフラインで動く）。
"""
from polyarchy_retrieval.models import RERANKERS  # モデルの登録は共有ライブラリの 1 箇所


# ---------------------------------------------------------------------------
# 本番組み込み用：llama-index の node_postprocessor 実装
# ---------------------------------------------------------------------------
# 本番検索は「ベクトルで広め(top-k=RERANK_RETRIEVE_K)に取り → クロスエンコーダで
# 再順位付け → top_n(=TOP_K) に切る」。これを query.py / eval.py が使う postprocessor
# として実装する。llama-index の as_query_engine(node_postprocessors=[...]) に渡すと
# retriever とジェネレータの間で自動適用される。

def _make_postprocessor_cls():
    """CrossEncoderRerank クラスを遅延生成（llama-index import を関数使用時まで遅らせる）。"""
    from typing import List, Optional
    from llama_index.core.bridge.pydantic import Field, PrivateAttr
    from llama_index.core.postprocessor.types import BaseNodePostprocessor
    from llama_index.core.schema import NodeWithScore, QueryBundle

    class CrossEncoderRerank(BaseNodePostprocessor):
        """ベクトル検索の結果をクロスエンコーダで再順位付けし top_n に絞る postprocessor。"""

        top_n: int = Field(default=5, description="再順位付け後に残す件数")
        model_key: str = Field(default="", description="RERANKERS のキー（記録用）")
        _rerank = PrivateAttr()

        def __init__(self, rerank_fn, top_n: int = 5, model_key: str = ""):
            super().__init__(top_n=top_n, model_key=model_key)
            self._rerank = rerank_fn

        @classmethod
        def class_name(cls) -> str:
            return "CrossEncoderRerank"

        def _postprocess_nodes(
            self,
            nodes: "List[NodeWithScore]",
            query_bundle: "Optional[QueryBundle]" = None,
        ) -> "List[NodeWithScore]":
            if not nodes or query_bundle is None:
                return nodes[: self.top_n]
            passages = [n.node.get_content() for n in nodes]
            scores = self._rerank(query_bundle.query_str, passages)
            for n, s in zip(nodes, scores):
                n.score = float(s)  # クロスエンコーダのスコアで上書き
            ranked = sorted(nodes, key=lambda n: n.score, reverse=True)
            return ranked[: self.top_n]

    return CrossEncoderRerank


def production_reranker_postprocessor(top_n: int):
    """本番リランカー（config.PRODUCTION_RERANKER）の postprocessor を返す。

    PRODUCTION_RERANKER が None（無効）なら None を返す＝呼び出し側はベクトル単独に戻す。
    query.py / eval.py の本番入口はこれを使う（＝本番リランカーの単一の真実源）。
    """
    from recommendations.core.config import PRODUCTION_RERANKER
    if not PRODUCTION_RERANKER:
        return None
    rerank_fn = RERANKERS[PRODUCTION_RERANKER]()
    cls = _make_postprocessor_cls()
    return cls(rerank_fn, top_n=top_n, model_key=PRODUCTION_RERANKER)
