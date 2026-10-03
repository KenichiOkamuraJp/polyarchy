"""
審議会議事録DB の設定（調整値は本ファイルだけ・env で上書き可）。

env 名は他のサービスと衝突しない名前にする（箱では deploy.env が全ユニット共通＝政策主張DB の
COLLECTION_NAME を拾わないように DELIB_ を付ける・開発計画 §2.3）。手元の Qdrant は政策主張DB の
qdrant-dev と分ける（qdrant-dev のデータは配布で箱へ同期される＝評価前のコレクションを混ぜない）。
"""
import os

from deliberations.core.paths import DATA_DIR

QDRANT_URL = os.getenv("DELIB_QDRANT_URL", "http://localhost:6340")
COLLECTION = os.getenv("DELIB_COLLECTION", "deliberations_v1")
CORPUS = "deliberations"
VOCAB_DIR = DATA_DIR / "bm25"

# モデルは共有ライブラリの登録のキー（版の固定は polyarchy_retrieval.models の 1 箇所）
EMBEDDING = os.getenv("DELIB_EMBEDDING", "ruri_v3_310m_pfx")
RERANKER = os.getenv("DELIB_RERANKER", "jp_reranker_xsmall_v1")
DENSE_DIM = 768  # ruri-v3-310m

# 検索の幅（ベクトル・BM25 の候補数 → RRF → リランカーに渡す候補 → 返す件数）
HYBRID_KV = int(os.getenv("DELIB_HYBRID_KV", "40"))
HYBRID_KB = int(os.getenv("DELIB_HYBRID_KB", "40"))
POOL_K = int(os.getenv("DELIB_POOL_K", "30"))
TOP_K = 5
TOP_K_MAX = 20
FILTER_WIDEN = 5
QDRANT_EXACT = os.getenv("DELIB_QDRANT_EXACT", "1").lower() not in ("0", "false", "no", "")

# 取り込みから外す短い単位（司会の取り次ぎ「ありがとうございました。」など）。0＝外さない。
MIN_UNIT_CHARS = int(os.getenv("DELIB_MIN_UNIT_CHARS", "0"))

DEFAULT_LAYERS = ("公開",)

# 絞り込み用の索引（payload index）。layer は共有ライブラリが必須にしている。
KEYWORD_FIELDS = ("org", "layer", "doc_kind", "role", "mode", "unit")
INTEGER_FIELDS = ("date_int", "session_no")
