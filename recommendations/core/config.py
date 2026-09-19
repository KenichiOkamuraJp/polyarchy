from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PDF_DIR = DATA_DIR / "pdfs"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# semantic のチャンク境界検出に使う「参照埋め込み」。Phase 3 の判断①(1A)で openai-small を
# 据え置き（境界を Phase 2 検証時と同一に保つため）。※本番の格納/検索埋め込みは
# PRODUCTION_EMBEDDING（下記）で、これとは別物。chunking._semantic がこの値を参照する。
EMBEDDING_MODEL = "text-embedding-3-small"
LLM_MODEL = "claude-sonnet-4-6"

# 本番の格納/検索に使う埋め込みモデル（recommendations/core/embeddings.py の EMBEDDING_MODELS のキー）。
# Phase 3 の比較で日本語特化の ruri（プレフィックス付き）が主指標トップ（MRR 1.000）かつ
# ローカル完結（無料・外部送信なし）の要件に合致したため採用（判断②=2B）。
PRODUCTION_EMBEDDING = "ruri_v3_310m_pfx"

# 本番のチャンク戦略。Phase 2 の比較で構造的分割(semantic)が最良だったため採用。
# 戦略の実体は recommendations/ingest/chunking.py の STRATEGIES を参照。
DEFAULT_STRATEGY = "semantic"

# 固定長戦略を使う場合のパラメータ（semantic 採用後は ingest では未使用の後方互換値）。
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

TOP_K = 5

# バッチ2 段2（2026-08-28）：MCP search_policy_docs の top_k 要求上限。既定値は TOP_K=5 のまま、
# 明示要求があれば TOP_K_MAX まで許す（応答肥大の歯止め＝無制限にはしない。20件×本文1200字≒24k字）。
# eval のアンカーは top_k=5 で測るため本値は数値に影響しない。
TOP_K_MAX = int(os.getenv("TOP_K_MAX", "20"))

# Phase 5〜7.6：本番リランカー（recommendations/core/rerankers.py の RERANKERS のキー）。
# Phase 8 前哨（rerank 短縮）で bge-reranker-v2-m3(568M) から hotchpotch の
# japanese-reranker-cross-encoder-xsmall-v1(107M・層が浅い) に差し替え。167問ベンチで
# hit@5 93.6%（bge と同値・基準「≥93%維持」達成）・MRR 0.741→0.771（改善）・
# rerank p50 4141→306ms（1/13.5・目標「半減」を大幅超過）・フィルタ26/26+層ゲートPASS。
# 旧 bge に戻すには PRODUCTION_RERANKER=bge_reranker_v2_m3。ローカル/無料/プライベート。
# 空文字/"none" で無効化＝ベクトル単独(Phase 4 の挙動)に戻せる（環境変数で上書き可）。
PRODUCTION_RERANKER = os.getenv("PRODUCTION_RERANKER", "jp_reranker_xsmall_v1")
if PRODUCTION_RERANKER.lower() in ("", "none"):
    PRODUCTION_RERANKER = None

# リランク時に候補として先に取る件数（この中から TOP_K に再順位付けして絞る）。
# retrieve深さの感度比較で bge は 20→30 で ALL hit@5 90.0→92.9%・MRR 0.752→0.787、
# nissho の取りこぼしも解消（100%）したため既定30。ハイブリッド有効時は融合後プールの数。
# Phase 8 前哨で新リランカー(xsmall_v1)でも K 再感度を確認：K=30 hit@5 93.6% > K=20 92.2%
# > K=15 90.8%（削ると正解がプール外に落ち低下）＝K=30 据え置きが最良（速度は306msで十分）。
RERANK_RETRIEVE_K = int(os.getenv("RERANK_RETRIEVE_K", "30"))

# Phase 5：ハイブリッド検索（BM25語彙＋ベクトル→RRF融合）。検索専用比較で
# ALL hit@5 92.9→100.0%（全ドメイン100%）・MRR 0.787→0.848、弱点の rengo は
# 75→100% に到達したため本番採用。BM25語彙一致が recall漏れと recency混同を同時に解消。
# fugashi＋rank_bm25 でローカル完結（外部送信なしの要件を維持）。"0"/"false" で無効化。
HYBRID_SEARCH = os.getenv("HYBRID_SEARCH", "1").lower() not in ("0", "false", "no", "")
HYBRID_KV = int(os.getenv("HYBRID_KV", "50"))  # ベクトルで取る件数（融合前）
HYBRID_KB = int(os.getenv("HYBRID_KB", "50"))  # BM25で取る件数（融合前）

# Phase 6：構造化フィルタ検索とデータ層分離。
# 本番の既定は公開層のみ＝機密層は明示的に層を広げない限り検索経路に出さない
# （「意志ではなく仕組みで分離」ロードマップ §2.4）。現状データは全て公開なので実挙動は不変。
DEFAULT_LAYERS = ("公開",)

# Phase 7.5：比較・横断型クエリのマルチクエリ分解（recommendations/core/multiquery.py）。クエリが
# 2団体以上に言及し比較マーカー（それぞれ/異なる 等）を含むとき、団体別に検索して
# 候補プールを均等合流する。"0"/"false" で無効化＝単一クエリ検索に戻せる。
MULTIQUERY_COMPARATIVE = os.getenv("MULTIQUERY_COMPARATIVE", "1").lower() not in (
    "0", "false", "no", "")

# field_tags 部分一致などフィルタ push-down で表現できない条件が有効なときに、
# ベクトル/BM25 候補を広げて Python 後段フィルタ後も融合前プールが痩せない様にする倍率。
FILTER_WIDEN = int(os.getenv("FILTER_WIDEN", "8"))

# ingest の境界検出（semantic split の OpenAI 埋め込み往復）を並列化するワーカ数。
# PyMuPDF が 1ページ=1doc を吐くため各ページ独立に split でき、ドキュメント並列でも
# 結果は単一スレッド（VectorStoreIndex.from_documents）とバイト等価（検証済）。
# 4509ページの逐次往復（約55分）がボトルネックなのを N プロセスで割る。0/1 で逐次。
# 環境変数 INGEST_NUM_WORKERS で上書き可（パイプライン側で CPU 数に自動キャップ）。
INGEST_NUM_WORKERS = int(os.getenv("INGEST_NUM_WORKERS", "8"))

# semantic の境界検出に使う OpenAI 埋め込みの per-call タイムアウト（秒）。eval の
# SIGALRM ガードと同趣旨で、長時間ジョブ中の API ハングでingest全体が固まるのを防ぐ。
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "60"))

# ベクトル検索バックエンド＝Qdrant のみ（バッチ2 段4〔2026-08-28〕で Chroma 経路を全廃。
# 切り戻しは Qdrant 内の v7→v6 コレクション切替＝COLLECTION_NAME の env 1 行）。
# env VECTOR_BACKEND は互換のため読むが、qdrant 以外は起動時に明示エラー（黙って旧経路に
# 落ちる事故を防ぐ）。QDRANT_EXACT=1（既定）で HNSW を使わない厳密検索（パリティ検証で
# 総当たりと完全一致を確認済。15〜20 万チャンク規模では実用速度）。
VECTOR_BACKEND = os.getenv("VECTOR_BACKEND", "qdrant").lower()
if VECTOR_BACKEND != "qdrant":
    raise RuntimeError(
        f"VECTOR_BACKEND={VECTOR_BACKEND!r} はサポート外です（Chroma 経路は 2026-08-28 バッチ2 段4 で全廃。"
        "切り戻しは COLLECTION_NAME=policy_claims_v6 等の Qdrant コレクション切替で行う）")
QDRANT_EXACT = os.getenv("QDRANT_EXACT", "1").lower() not in ("0", "false", "no", "")

# v1=固定長(openai-small), v2=semantic+openai-small, v3=semantic+ruri(pfx) 158文書,
# v4=同構成で537文書/41,109チャンク（Phase 7 規模拡大）,
# v5=同構成で1,249文書/80,632チャンク（Phase 11 データ健全性＆網羅性＝連合日付修正＋直近10年窓収集）。
# Phase 11 で v5 を prod に昇格（v1/v2/v3/v4 は切り戻し用に保持）。
# Phase B（B4）で Qdrant の policy_claims_v7（全期間拡大・ゲート済。2026-09 時点で約3,700文書/187k チャンク）を本線に確定。
# 既定＝本線 v7/qdrant。切り戻しは env `COLLECTION_NAME=policy_claims_v6`（80k・Qdrant 内の中間段。
# Chroma v5 経路はバッチ2 段4〔2026-08-28〕で全廃＝データは S3 data/chroma/ と退避先に保管のみ）。
# 環境変数で上書き可（ingest/eval 全てに効く）。
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "policy_claims_v7")
