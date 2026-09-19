"""
Phase 5：リランカー（クロスエンコーダ）の定義（レジストリ）。

埋め込み（bi-encoder）はクエリと文書を独立にベクトル化するため速いが精度に上限がある。
クロスエンコーダは (クエリ, 文書) を1本の入力として同時に符号化し関連度を直接スコア化する
ため精度が高い代わりに遅い。そこで **ベクトル検索で top-k を広め（例 k=20）に絞ってから
クロスエンコーダで再順位付けし top-5 に切る** 二段構えにする（retrieve→rerank）。

ローカル完結要件（ローカル/無料/外部送信なし）を維持するため、リランカーは全てローカル HF の
日本語対応クロスエンコーダ（sentence-transformers の CrossEncoder で読める重み）に限定する。
API は使わない。

embeddings.py の EMBEDDING_MODELS と同じく「キー → 生成関数（遅延）」で登録する。
遅延生成にしているのは HF モデルの DL/ロードが重く、使うモデルだけ初期化するため。
CrossEncoder は初回のみモデルを DL しローカルにキャッシュする（以降はオフラインで動く）。
"""


def _cross_encoder(model_name: str, max_length: int = 512):
    """ローカル HF クロスエンコーダ（sentence-transformers CrossEncoder）。

    未インストール時は import で分かりやすいエラーを出す（ランナーのモデル単位
    try/except に拾われ、他モデルの結果は失われない）。
    返すのは「(query, [passage,...]) → スコア列」の rerank 関数。スコアは相対順位のみ
    使う（モデルにより sigmoid 済み/生ロジットで絶対値の意味は異なるが順位付けには十分）。
    """
    def build():
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            raise ImportError(
                f"'{model_name}' には sentence-transformers が必要です。"
                "`pip install sentence-transformers` を実行してください。"
            ) from e
        model = CrossEncoder(model_name, max_length=max_length)

        def rerank(query: str, passages: list[str]) -> list[float]:
            pairs = [(query, p) for p in passages]
            scores = model.predict(pairs)
            return [float(s) for s in scores]

        return rerank
    return build


# キー → rerank関数生成関数（遅延）。比較対象はここを増減するだけで拡張できる。
# 全て日本語対応・ローカル HF・クロスエンコーダ（ローカル完結要件を満たす）。
RERANKERS: dict[str, callable] = {
    # ruri 埋め込みと同じ cl-nagoya ファミリ。本番埋め込みと学習思想が揃う。337M。
    # 要 fugashi+unidic-lite（BERT-japanese の MeCab トークナイザ）。
    "ruri_reranker_large": _cross_encoder("cl-nagoya/ruri-reranker-large"),
    # 日本語特化の定番クロスエンコーダ（hotchpotch）。337M(large)。要 fugashi。
    "japanese_reranker_large": _cross_encoder(
        "hotchpotch/japanese-reranker-cross-encoder-large-v1"),
    # 強力な多言語リランカー。日本語も強い。568M。追加トークナイザ依存なし。
    # Phase 5 の78問比較で総合 hit@5 最良(90.0%)・MRR 非劣化 → Phase 5〜7.6 の本番。
    "bge_reranker_v2_m3": _cross_encoder("BAAI/bge-reranker-v2-m3"),
    # 日本語特化の軽量クロスエンコーダ（hotchpotch）。107M だが層が浅く MPS で桁違いに速い。
    # Phase 8 前哨の167問ベンチで hit@5 93.6%（bge と同値）・MRR 0.771（bge 0.741 を上回る）・
    # rerank p50 318ms（bge 4088ms の 1/12.9）→ **本番採用**（config.PRODUCTION_RERANKER 既定）。
    # 要 fugashi（BERT-japanese MeCab トークナイザ）。ローカル/無料/プライベート。
    "jp_reranker_xsmall_v1": _cross_encoder(
        "hotchpotch/japanese-reranker-cross-encoder-xsmall-v1"),
    # 同ファミリの中型（111M・層が深い）。B21 ベンチ（2026-09-08）で**不採用が確定**＝
    # hit@5 86.2→84.1%・MRR 0.713→0.633 と劣化し CPU p50 も 4.4 倍（残タスク §E-B21）。
    # 比較記録のため残置。要 fugashi。
    "jp_reranker_base_v1": _cross_encoder(
        "hotchpotch/japanese-reranker-cross-encoder-base-v1"),
}


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
