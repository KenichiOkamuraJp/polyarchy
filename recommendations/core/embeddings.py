"""
Phase 3：埋め込みモデルの定義（レジストリ）。

比較の変数を「検索に使う埋め込みモデル」だけに絞る。
  - チャンク分割は全モデル共通（config.DEFAULT_STRATEGY="semantic"）。
    しかも semantic の文境界は breakpoint 用の**参照埋め込み**（chunking._semantic が
    使う config.EMBEDDING_MODEL）で決まるため、比較対象モデルを変えても
    **チャンク境界は同一**になる。→ 純粋に「格納・検索ベクトルだけ」を変えた比較になる。
  - top_k・LLM も全モデル共通（config.py の値）。

埋め込み空間はモデルごとに異なるので、モデルごとに別コレクションへ**再インデックス**する
（既存コレクションは流用不可）。次元数・トークン上限・料金はモデルにより異なる。

各モデルは「キー → embed_model インスタンスを生成する関数」で登録する。
遅延生成（関数）にしているのは、HuggingFace 系がモデルDLを伴い import も重いため、
実際に使うモデルの分だけ初期化するため。
"""
from recommendations.core.config import OPENAI_API_KEY, PRODUCTION_EMBEDDING


def _openai(model_name: str):
    """OpenAI 埋め込み（API）。timeout は eval と同値でハング対策。"""
    def build():
        from llama_index.embeddings.openai import OpenAIEmbedding
        return OpenAIEmbedding(model=model_name, api_key=OPENAI_API_KEY, timeout=60.0)
    return build


def _hf(model_name: str, query_instruction: str = None, text_instruction: str = None):
    """HuggingFace 埋め込み（ローカル推論）。要 `llama-index-embeddings-huggingface`。

    未インストールの場合は import 時に分かりやすいエラーを出す（phase3 の
    モデル単位 try/except に拾われ、他モデルの結果は失われない）。

    query_instruction/text_instruction を渡すと、検索クエリ/格納文書の各テキストに
    モデル指定のプレフィックスを付与する（e5 の 'query: '/'passage: '、
    ruri の '検索クエリ: '/'検索文書: ' 等）。プレフィックスは埋め込み値を変えるため、
    付与版は別コレクションとして再インデックスすること。
    """
    def build():
        try:
            from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        except ImportError as e:
            raise ImportError(
                f"'{model_name}' には HuggingFace 埋め込みが必要です。"
                "`pip install llama-index-embeddings-huggingface` を実行してください。"
            ) from e
        kwargs = {}
        if query_instruction is not None:
            kwargs["query_instruction"] = query_instruction
        if text_instruction is not None:
            kwargs["text_instruction"] = text_instruction
        return HuggingFaceEmbedding(model_name=model_name, **kwargs)
    return build


# キー → embed_model 生成関数。比較対象はここを増減するだけで拡張できる。
# キーはそのままコレクション名の一部（policy_docs_emb_<key>）になるので簡潔に。
EMBEDDING_MODELS: dict[str, callable] = {
    # --- OpenAI（API・追加依存なし） ---
    "openai_small": _openai("text-embedding-3-small"),  # 現行本番。1536次元・安価
    "openai_large": _openai("text-embedding-3-large"),  # 3072次元・高精度・高料金

    # --- 日本語特化 / 多言語（ローカル・要 llama-index-embeddings-huggingface） ---
    # プレフィックス無し（out-of-the-box。第1ラウンドの比較で使用）
    "multilingual_e5_large": _hf("intfloat/multilingual-e5-large"),  # 1024次元・多言語強い
    "ruri_v3_310m": _hf("cl-nagoya/ruri-v3-310m"),                    # 日本語特化・高性能
    # プレフィックス付き（各モデルカード指定。検索用途の正当な使い方）
    "multilingual_e5_large_pfx": _hf(
        "intfloat/multilingual-e5-large",
        query_instruction="query: ", text_instruction="passage: ",
    ),
    "ruri_v3_310m_pfx": _hf(
        "cl-nagoya/ruri-v3-310m",
        query_instruction="検索クエリ: ", text_instruction="検索文書: ",
    ),
}

# 追加依存(HuggingFace)なしで即実行できるモデル。既定の比較対象。
OPENAI_MODELS = ["openai_small", "openai_large"]


def production_embed_model():
    """本番（config.PRODUCTION_EMBEDDING）の埋め込みモデルインスタンスを生成する。

    ingest.main / query / eval.main の本番入口はこれを使う（＝本番モデルの単一の真実源）。
    """
    return EMBEDDING_MODELS[PRODUCTION_EMBEDDING]()
