"""
埋め込みモデルの定義（レジストリ）と本番の解決（production_embed_model＝config.PRODUCTION_EMBEDDING＝ruri_v3_310m_pfx）。

もとは Phase 3 の比較の器：比較の変数を「検索に使う埋め込みモデル」だけに絞る。
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

ローカル HF のモデル（ruri ほか）の登録と読み込みは共有ライブラリ `polyarchy_retrieval.models`
（2026-10-03 に切り出し＝本番の重みの版〔commit〕の固定も同所）。本モジュールは OpenAI（API 鍵＝本コーパスの config）
の登録と、本番のキー（config.PRODUCTION_EMBEDDING）の解決を持つ。
"""
from polyarchy_retrieval.models import HF_EMBEDDING_MODELS
from recommendations.core.config import OPENAI_API_KEY, PRODUCTION_EMBEDDING


def _openai(model_name: str):
    """OpenAI 埋め込み（API）。timeout は eval と同値でハング対策。"""
    def build():
        from llama_index.embeddings.openai import OpenAIEmbedding
        return OpenAIEmbedding(model=model_name, api_key=OPENAI_API_KEY, timeout=60.0)
    return build


# キー → embed_model 生成関数。比較対象はここを増減するだけで拡張できる。
# キーはそのままコレクション名の一部（policy_docs_emb_<key>）になるので簡潔に。
EMBEDDING_MODELS: dict[str, callable] = {
    # --- OpenAI（API・追加依存なし） ---
    "openai_small": _openai("text-embedding-3-small"),  # Phase 3 までの本番（現行は ruri）。1536次元・安価
    "openai_large": _openai("text-embedding-3-large"),  # 3072次元・高精度・高料金

    # --- 日本語特化 / 多言語（ローカル・要 llama-index-embeddings-huggingface）＝共有ライブラリの登録 ---
    **HF_EMBEDDING_MODELS,
}


def production_embed_model():
    """本番（config.PRODUCTION_EMBEDDING）の埋め込みモデルインスタンスを生成する。

    ingest.main / query / eval.main の本番入口はこれを使う（＝本番モデルの単一の真実源）。
    """
    return EMBEDDING_MODELS[PRODUCTION_EMBEDDING]()
