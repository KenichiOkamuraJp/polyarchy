"""
polyarchy_retrieval — 文書検索のコーパスに依存しない部品（共有ライブラリ）。

長期開発計画 §3（2026-10-02 改訂）・審議会議事録DB_開発計画 §2.3 の切り出し。
政策主張DB（recommendations）と審議会議事録DB（deliberations）が同じ部品を使う：

- `tokenizer` … 日本語の分かち書き（fugashi・プロセス内で 1 回だけロード）
- `bm25`      … in-memory BM25 と、Qdrant sparse による BM25（語彙サイドカーの構築と検索）
- `fusion`    … RRF 融合・交互合流
- `models`    … 埋め込み・リランカーの読み込みとモデルの登録（本番の重みの版＝HF の commit の固定はここ 1 箇所）
- `qdrant`    … Qdrant のコーパス単位のストア・コレクションの作成（**層の条件は必須**）
- `hybrid`    … ベクトル＋BM25→RRF の 1 フィルタ条件分の融合

置き場を `polyarchy_common` にしないのは依存の分（本パッケージは torch・sentence-transformers を
引く＝統計参照DB だけの箱は `pip install -e ".[stats]"` で torch を引かない）。

約束：
- 各コーパスの config を import しない（値は引数で受ける＝import 時の `.env` 読み込みや
  env 名の衝突を持ち込まない）。
- llama_index は持ち込まない。例外は埋め込みの読み込み（`models.hf_embedding`）だけで、
  ベクトルの同一性を保つため llama_index の HuggingFaceEmbedding をそのまま使う。
- フィルタの意味（団体・日付・分野 など）は各コーパスが持つ。本パッケージは Qdrant の Filter と
  Python の述語を受け取るだけで、Filter に `layer` の条件が無ければ検索を拒む。
- 複数コレクションの融合は作らない（残タスク D「検索コアのコレクション引数化」は別の能力）。
"""
