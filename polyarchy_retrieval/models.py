"""
ローカル HF の埋め込みとリランカー（クロスエンコーダ）の読み込みと登録＝**本番の重みの版（HF の commit）の固定はここ 1 箇所**
（PINNED_REVISIONS）。どのキー（モデル）を本番に使うかは各コーパスの config。

各モデルは「キー → インスタンス（関数）を生成する関数」で登録する（遅延生成＝HF のモデルは DL と
import が重く、実際に使うモデルの分だけ初期化するため）。どのキーを本番に使うかは各コーパスの config。
キャッシュ先は埋め込み・リランカーとも HF 標準（HF_HOME/hub）＝事前DL（deploy/bootstrap/prefetch_models.py）と同じ。

★版＝リポジトリ名だけでは固定にならない（読み込みのたびに HF の main を解決し直す＝上流が重みを差し替えると黙って
ベクトルが変わる）。本番で使うモデルは commit を PINNED_REVISIONS に書き、読み込み（ここ）と箱の事前 DL
（deploy/bootstrap/prefetch_models.py）の両方がその commit を使う（2026-10-09）。上げるときは問ベクトルの
sha256 と上位 k 件が変わらないか（変わるならアンカーを測り直す）を確かめてから、この表だけを直す。
比較用のベンチのモデルは固定しない（main のまま）。

埋め込みは llama_index の HuggingFaceEmbedding をそのまま使う（プレフィックス・正規化・バッチの扱いを
変えるとベクトルが変わり得る＝置き換えはベクトルの同一性を確かめてから別の変更で）。
API の埋め込み（OpenAI など）は鍵を持つ各コーパスの側に置く。
"""


# 本番で使う重みの版（HF のリポジトリ → commit）。2026-10-09 に HF の main と手元・箱の取得物が一致することを確認して固定。
PINNED_REVISIONS: dict[str, str] = {
    "cl-nagoya/ruri-v3-310m": "18b60fb8c2b9df296fb4212bb7d23ef94e579cd3",                        # 埋め込み（ruri_v3_310m_pfx）
    "hotchpotch/japanese-reranker-cross-encoder-xsmall-v1": "8547ac84ae5aee35387aad7000b379bd1b968dc6",  # リランカー（jp_reranker_xsmall_v1）
}


def hf_embedding(model_name: str, query_instruction: str = None, text_instruction: str = None):
    """HuggingFace 埋め込み（ローカル推論）の生成関数を返す。要 `llama-index-embeddings-huggingface`。

    未インストールの場合は生成時に分かりやすいエラーを出す（比較ランナーのモデル単位
    try/except に拾われ、他モデルの結果は失われない）。

    query_instruction/text_instruction を渡すと、検索クエリ/格納文書の各テキストに
    モデル指定のプレフィックスを付与する（e5 の 'query: '/'passage: '、
    ruri の '検索クエリ: '/'検索文書: ' 等）。プレフィックスは埋め込み値を変えるため、
    付与版は別コレクションとして再インデックスすること。

    キャッシュ先は HF 標準（HF_HOME/hub）に揃える。cache_folder を省くと llama_index は
    自前の get_cache_dir()（LLAMA_INDEX_CACHE_DIR／OS のユーザキャッシュ）を使い、HF_HOME を見ない
    ＝事前DL（deploy/bootstrap/prefetch_models.py）やリランカー（CrossEncoder）と食い違う。
    """
    def build():
        try:
            from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        except ImportError as e:
            raise ImportError(
                f"'{model_name}' には HuggingFace 埋め込みが必要です。"
                "`pip install llama-index-embeddings-huggingface` を実行してください。"
            ) from e
        from huggingface_hub.constants import HF_HUB_CACHE
        kwargs = {"cache_folder": HF_HUB_CACHE}
        if model_name in PINNED_REVISIONS:   # model_kwargs として SentenceTransformer にそのまま渡る
            kwargs["revision"] = PINNED_REVISIONS[model_name]
        if query_instruction is not None:
            kwargs["query_instruction"] = query_instruction
        if text_instruction is not None:
            kwargs["text_instruction"] = text_instruction
        return HuggingFaceEmbedding(model_name=model_name, **kwargs)
    return build


def cross_encoder(model_name: str, max_length: int = 512):
    """ローカル HF クロスエンコーダ（sentence-transformers CrossEncoder）の生成関数を返す。

    未インストール時は生成時に分かりやすいエラーを出す（ランナーのモデル単位
    try/except に拾われ、他モデルの結果は失われない）。
    生成されるのは「(query, [passage,...]) → スコア列」の rerank 関数。スコアは相対順位のみ
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
        pin = {"revision": PINNED_REVISIONS[model_name]} if model_name in PINNED_REVISIONS else {}
        model = CrossEncoder(model_name, max_length=max_length, **pin)

        def rerank(query: str, passages: list[str]) -> list[float]:
            pairs = [(query, p) for p in passages]
            scores = model.predict(pairs)
            return [float(s) for s in scores]

        return rerank
    return build


# キー → 埋め込みの生成関数（ローカル HF）。キーはそのままコレクション名の一部になり得るので簡潔に。
HF_EMBEDDING_MODELS: dict[str, callable] = {
    # プレフィックス無し（out-of-the-box。第1ラウンドの比較で使用）
    "multilingual_e5_large": hf_embedding("intfloat/multilingual-e5-large"),  # 1024次元・多言語強い
    "ruri_v3_310m": hf_embedding("cl-nagoya/ruri-v3-310m"),                    # 日本語特化・高性能
    # プレフィックス付き（各モデルカード指定。検索用途の正当な使い方）
    "multilingual_e5_large_pfx": hf_embedding(
        "intfloat/multilingual-e5-large",
        query_instruction="query: ", text_instruction="passage: ",
    ),
    "ruri_v3_310m_pfx": hf_embedding(
        "cl-nagoya/ruri-v3-310m",
        query_instruction="検索クエリ: ", text_instruction="検索文書: ",
    ),
}

# キー → rerank関数の生成関数（遅延）。比較対象はここを増減するだけで拡張できる。
# 全て日本語対応・ローカル HF・クロスエンコーダ（ローカル完結要件を満たす）。
RERANKERS: dict[str, callable] = {
    # ruri 埋め込みと同じ cl-nagoya ファミリ。本番埋め込みと学習思想が揃う。337M。
    # 要 fugashi+unidic-lite（BERT-japanese の MeCab トークナイザ）。
    "ruri_reranker_large": cross_encoder("cl-nagoya/ruri-reranker-large"),
    # 日本語特化の定番クロスエンコーダ（hotchpotch）。337M(large)。要 fugashi。
    "japanese_reranker_large": cross_encoder(
        "hotchpotch/japanese-reranker-cross-encoder-large-v1"),
    # 強力な多言語リランカー。日本語も強い。568M。追加トークナイザ依存なし。
    # Phase 5 の78問比較で総合 hit@5 最良(90.0%)・MRR 非劣化 → Phase 5〜7.6 の本番。
    "bge_reranker_v2_m3": cross_encoder("BAAI/bge-reranker-v2-m3"),
    # 日本語特化の軽量クロスエンコーダ（hotchpotch）。107M だが層が浅く MPS で桁違いに速い。
    # Phase 8 前哨の167問ベンチで hit@5 93.6%（bge と同値）・MRR 0.771（bge 0.741 を上回る）・
    # rerank p50 318ms（bge 4088ms の 1/12.9）→ 政策主張DB の**本番採用**。
    # 要 fugashi（BERT-japanese MeCab トークナイザ）。ローカル/無料/プライベート。
    "jp_reranker_xsmall_v1": cross_encoder(
        "hotchpotch/japanese-reranker-cross-encoder-xsmall-v1"),
    # 同ファミリの中型（111M・層が深い）。B21 ベンチ（2026-09-08）で政策主張DB では**不採用が確定**＝
    # hit@5 86.2→84.1%・MRR 0.713→0.633 と劣化し CPU p50 も 4.4 倍（残タスク §E-B21）。
    # 比較記録のため残置。要 fugashi。
    "jp_reranker_base_v1": cross_encoder(
        "hotchpotch/japanese-reranker-cross-encoder-base-v1"),
}
