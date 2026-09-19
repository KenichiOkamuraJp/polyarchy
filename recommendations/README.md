# recommendations — 政策主張DB（Polyarchy）

> **状態：正典（recommendations の入口）。** 数値（ゲート基準）は写さずルート README を参照（公開リポジトリと CI で誰でも再現できる形にする＝運用設計 §2.5）。ライセンス＝ルート README「ライセンス」（Apache-2.0・公開する予定）。web UI の節は廃止（2026-09-02）の記録。

経済団体・政府の**主張文書**（提言・意見・答申・建議・方針）のコーパス。Polyarchy の兄弟サービスの一つ
（全体像は [../README.md](../README.md)、共通契約は `../polyarchy_common/`）。以下は本サービスのセットアップ・実行・評価。
実行は**リポジトリ root を cwd** にして `python -m recommendations.<pkg>.<module>` で行う（`.env` は `recommendations/.env`）。

## セットアップ

conda環境 `polyarchy`（Python 3.12）を使う。

```bash
# 環境作成（初回のみ）
conda create -n polyarchy python=3.12 -y
conda activate polyarchy

cd /path/to/polyarchy        # リポジトリ root（pyproject.toml がある場所）
pip install --upgrade pip
pip install -e ".[recommendations,stats]"   # recommendations だけなら ".[recommendations]"（pyproject の extras）
```

`.env.example` をコピーして `.env` を作り、APIキーを設定する：

```bash
cp .env.example .env
# .env を編集して OPENAI_API_KEY と ANTHROPIC_API_KEY を入れる
# （キーが要るのは取込と回答まで含む評価だけ。MCP サーバと検索ゲート 4 本はキーなしで動く）
```

## 実行

`conda activate polyarchy` した状態で：

1. `data/pdfs/` に PDF を配置し、`data/catalog.csv` に行を登録（collect.py 経由なら自動）
2. インデックス構築（v7/Qdrant 本線・doc 単位増分・冪等）：
   ```bash
   COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.qdrant_ingest ingest
   ```
   （旧 Chroma 取込 CLI `recommendations.ingest.ingest` はバッチ2 段4〔2026-08-28〕で削除＝
   同モジュールは load_documents 等の共有部のみ）
3. 質問応答は MCP サーバから（Web UI＝`app.py`／`chat_app.py` は **2026-09-02 廃止（B13・MCP 特化）＝コード残置・ローカル検証用のみ**）。
   （旧 CLI `recommendations.serving.query` は Chroma 専用のため `recommendations/archive/` に退避。生成プロンプトは `recommendations/core/prompts.py`）

### Web UI（Phase 13・多段対応 参照 UI）— **廃止（2026-09-02・B13）。コード残置・ローカル検証用。以下は当時の記述**

設問の形に応じて **多段オーケストレーション**（`recommendations.core.multistage.orchestrate`）で検索戦略を
自動選択する Streamlit UI。検索は本番の `PolicySearchService`（層=公開固定＋フェイルクローズ）、
生成は本番と同一の SYSTEM_PROMPT＋Anthropic：

```bash
conda activate polyarchy
cd /path/to/polyarchy   # リポジトリ root
streamlit run recommendations/serving/app.py --server.headless true   # ブラウザで http://localhost:8501
```

- **検索戦略の自動切替**（多段が発火）：
  - 「**各団体は**〜」「複数団体名指し」→ **coverage**（団体別 fan-out・単一検索の1団体偏りを回避）
  - 「〜を**最初に/唯一/最も**」→ **aggregation**（全団体走査→argmin か健全な棄却・**判定は決定論＝無料**）
  - 「**経団連は**〜」→ **targeted**（その団体に絞る）／それ以外 → **baseline**（通常の関連度検索）
- 集約型は判定パネル（棄却/確定＋各団体の最古言及テーブル＋根拠）を提示。集約以外は多段が選んだ
  チャンクを compact 合成し、出典（団体・日付・ページ・原文リンク・チャンク本文）を併記。棄却語を
  含む回答は警告バナーで明示。
- **層＝公開固定＋フェイルクローズ**（`PolicySearchService`）。機密は UI から広げる術がない
  （Phase 8 の「機密層 opt-in」は PoC で撤去。多人数化で価値が増す物理境界）。
- **利用者質問の捕捉（第2チャネル）**：app.py も `capture_query(source="app")` で実質問を追記 JSONL に
  残す（共有層 `search_api` には置かない＝eval 197問の非混入）。Phase 12 の器の燃料。
- モデル・DB・BM25 索引・リランカーは `@st.cache_resource` で起動時1回だけロードし常駐。
- サイドバーで団体・日付・分野タグの絞り込み（多段に primitive で渡す）。
- 回答生成（集約型以外）は Anthropic API のクレジットを消費する（動作確認は少数クエリで）。
- 配布・ホスティング・認証の現行設計＝`deploy/PROD_MIGRATION.md` §2.5・`docs/個人認証_案B設計.md`。
- ※ `st.table` は使わない（pyarrow/mimalloc が Streamlit スレッドで segfault するため Markdown 表で描画）。
  `.streamlit/config.toml` で fileWatcher 無効化済み。

### 対話UI（Phase 13+・エージェント型 chat）— **廃止（2026-09-02・B13）。コード残置。以下は当時の記述**

`app.py` の一発QAに対し、**Claude 自身に検索ツールを多段で叩かせる会話UI**（`chat_app.py`）。
ドッグフーディングで「実用レベル」と評価された MCP エージェント体験を Streamlit チャットに載せたもの。

```bash
conda activate polyarchy && cd /path/to/polyarchy   # リポジトリ root
streamlit run recommendations/serving/chat_app.py --server.headless true   # http://localhost:8502
```

- **会話しながら多段検索**：追い質問・掘り下げ・横断を Claude が自律的に判断（1発話に複数回検索）。
  会話履歴を保持し、「その目標について詳しく」等の文脈依存の追い質問にも答える。
- `st.status` に検索の経緯（🔎 各検索）をライブ表示。回答は出典（団体/日付/ページ/原文リンク/本文）付き。
- 検索は `PolicySearchService`（**層=公開固定＋フェイルクローズ**）。外に出るのは生成のみ。各**ユーザー発話**を
  `capture_query(source="app_chat")` で捕捉（LLM 内部の検索クエリは捕捉しない）。
- **クレジット**：1発話に複数回の LLM／ツール呼び出し（1発話 ~20–100秒・上限 `MAX_TOOL_CALLS`）。素早い単発は
  `app.py`（決定論・軽量）、じっくり対話は `chat_app.py`、と使い分け。`.streamlit/config.toml` を共用。

### MCP サーバ（Phase 10・検索のツール化 / retrieval-as-a-tool）

本番検索を **MCP ツール** として公開し、生成側 LLM（Claude）に**多段検索**させて網羅・集約型の
弱点を突破する。検索はローカル（公開層固定）、外へ出るのは生成のみ。

公開ツール：
- `search_policy_docs(query, orgs?, since?, until?, field?, top_k?, diversify?)` … 本番検索
  （hybrid＋比較型マルチクエリ＋リランカー）でランク済チャンク＋メタを JSON で返す
  （top_k 既定 5・上限 20＝バッチ2 段2〔2026-08-28〕で 5→20 に緩和）。
- `sweep_policy_docs(query, mode?, orgs?, per_org?, since?, until?, field?)` … 団体横断の
  多段検索（`core/multistage.py` の決定論実装をバッチ2 段2 でツールに昇格）。網羅
  （coverage＝団体別 fan-out→均等合流）と集約/最上級（aggregation＝全社走査→確定 or
  棄却根拠）を 1 コールで返す。mode=auto（既定）は設問の形から自動選択。結果上限 15 件。
  従来はクライアントの Claude が説明文経由で 10〜30 回検索して実現していた動きの決定論化。
- `list_orgs()` … 団体コード一覧（団体ごとに叩くため）。

**収録範囲の明示（2026-08-20）**：全検索レスポンスに `coverage_note`（未収録の情報類型＝負の
スコープ宣言）と `freshness`（団体別の `last_ingested`/`latest_doc_date`）を付与し、低ヒット時
（count < 3）のみ `coverage_warning` を追加する。「DB にない＝存在しない」の誤推論（探索停止）への
対策＝介入点はレスポンス本文（説明文はツール選択時にしか効かない）。実装は
`recommendations/core/coverage.py`（文言・閾値は env で差し替え可）、設計は
[docs/コーパス収録範囲の明示.md](docs/コーパス収録範囲の明示.md)。

**機密の物理遮断（最重要）**：検索ツール（`search_policy_docs`・`sweep_policy_docs`）は **layer 引数を持たない**。検索は常に
公開層固定で走り、境界フェイルクローズで非公開を落とす（`recommendations/core/search_api.py`）。外部から機密を
要求する術は存在しない。非公開文書を扱うモジュールは本サービスの範囲外＝**境界の外は作らない**（長期開発計画 §2・ルート README「拡張の形」）。層の不変条件は公開 DB を守るために残す。層分類は provenance ベース（自動収集=公開／手動足し=機密・フェイル
クローズ、`recommendations/ingest/ingest.py` の `classify_layer`）。

```bash
conda activate polyarchy && cd /path/to/polyarchy   # リポジトリ root

# サーバ単体起動（stdio・ログは日本語で stderr）
python -m recommendations.serving.mcp_server

# 動作確認・証明（クレジット0）
python -m recommendations.eval.mcp_smoke            # (a) MCP 疎通＋stdout クリーン＋layer 引数なし
python -m recommendations.eval.mcp_layer_gate_test  # (b) 機密が MCP 経路で物理的に返らないことの証明
python -m recommendations.eval.multistage_eval      # (c) 多段検索 vs 一発QA を Phase 9 メトリクスで数値比較
                               #     網羅pass 86.7%→100% / 集約棄却 12/12 / 答え可能 3/3

# ライブ実証（任意・クレジット消費）：Claude に MCP ツールを多段で叩かせる
python -m recommendations.eval.multistage_agent_demo
```

Claude Code / 対応クライアントからは、リポジトリ直下の `../.mcp.json` で
`polyarchy-policy-search` サーバが自動登録される（polyarchy env の python を指す）。

#### 公式コネクタ対応（2026-08-19・recommendations 側）

Claude の公式コネクタ（リモート MCP・ディレクトリ掲載）要件は `../docs/公式コネクタ要件.md`、認証・課金の設計は
`../docs/個人認証_案B設計.md`（案 B＝外部 IdP・2026-09-02 採択・ログイン必須。案 A＝Cloudflare Access の OAuth は不採用・凍結）。recommendations はこれに合わせるだけ。
再配布・引用の立脚点（発行体別グレード・著作権法 47 条の 5／32 条の枠）は [docs/再配布条件.md](docs/再配布条件.md)。

| 項目 | 状態 |
|---|---|
| ツール注釈 | `search_policy_docs`・`list_orgs` に `title`＋`ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)`、`FastMCP(instructions=SERVER_INSTRUCTIONS)`（何を返し・何を返さないか）。説明文は「何をするか・いつ使うか」に整理（振る舞い指示調を除去）＝**済** |
| ゲート不変 | 注釈追加後に 4 種 PASS（数値の正典＝ルート README「品質の担保」・本書には写さない）＋ `mcp_layer_gate_test` PASS＝**済** |
| 公開ドキュメント | `https://docs.polyarchy.net/recommendations`（原稿 `../deploy/pages/recommendations.html`・Cloudflare Pages 配置済・旧ドメインのエイリアスは 2026-09-02 撤去）。接続 URL は書かない（PoC は秘密パス付き authless＝個別案内） |
| 利用者 ID→捕捉ログ | コード変更なし（`polyarchy_common.capture` が認証済みなら `user_hash` を自動付与）。案 B（2026-09-02〜）で 3 経路とも付く |
| 認証 | **案 B（外部 IdP＝WorkOS AuthKit・ログイン必須・2026-09-02〜）**。authless／IP 許可／案 A（Access）は廃止・凍結（経緯の文書は公開リポジトリに含めていない） |
| 配布 | カスタムコネクタ。ディレクトリ提出は Team 組織を作ってから（stats と同時） |

## 構成（B6 再編後・リポジトリ root が Python の root＝`python -m recommendations.…`）

```
recommendations/                       政策主張DB（フォルダ名＝コーパスID。データは recommendations/data/）
├── core/                     検索スタック
│   ├── config.py             設定値（パス・コレクション名・本番モデル）
│   ├── orgs.py               団体の 1 表（コード・言及語・表示名・正式名・issuer）＝全モジュールが参照
│   ├── prompts.py            生成（回答合成）用 SYSTEM_PROMPT（app.py と eval が共用）
│   ├── embeddings.py / rerankers.py / hybrid.py / qdrant_bm25.py / qdrant_store.py / filters.py / multiquery.py
│   ├── search_api.py         本番検索を薄く包む共有サービス（層は公開固定＋フェイルクローズ）
│   ├── multistage.py         多段検索オーケストレータ（団体 fan-out / 集約走査 / 棄却）
│   └── query_capture.py      実クエリの永続捕捉（レコード形は recommendations 固有・器は polyarchy_common.capture）
├── serving/                  入口
│   ├── mcp_server.py         MCP サーバ（search_policy_docs / sweep_policy_docs / list_orgs・stdio と Streamable HTTP）
│   ├── chat_app.py / app.py  Streamlit UI（廃止 2026-09-02・残置）
│   └── _ui.py                2 つの UI の共通部品（表示名・日付変換・出典描画）
├── ingest/                   収集・取込
│   ├── collect.py            収集（5 団体アダプタ＝index 関数＋正規化・取得〜catalog 行は fetch_and_record に共通化）
│   ├── chunking.py / ingest.py（文書ローダ共有部）/ qdrant_ingest.py（取込の本線＝doc 単位増分）
│   ├── policy_tagger.py      分野タグ・文書性格の追い判定（語彙は polyarchy_common.taxonomy）
│   └── metadata_audit.py     共通コア監査 CLI（定義は polyarchy_common.metadata_core）
├── eval/                     評価・回帰ゲート
│   ├── eval.py               CLI ディスパッチャ（既定＝生成込み / --retrieval-only / --filter-eval）
│   ├── full.py / retrieval.py / filters.py   eval.py の実体（生成込み / 検索のみ / フィルタ＋層ゲート）
│   ├── _common.py / _scoring.py  共通の入出力（評価セット・コレクション参照）／採点ヘルパ（org_of / expected_sources / normalize）
│   ├── multistage_eval.py / mcp_smoke.py / mcp_capture_test.py / mcp_layer_gate_test.py
│   ├── evalset_gate.py / phase12_pipeline.py（トリアージ→eval 昇華）
│   └── multistage_agent_demo.py
├── archive/                  過去フェーズの記録（本線から未参照・実行保証なし。一覧は archive/README.md）
├── data/                     pdfs / catalog.csv / eval / query_log（大物は git 外・S3 管理）
├── .env.example              API キーのテンプレ（実体の .env は git 管理外・recommendations/ 直下）
└── （Streamlit 設定はリポジトリ root の .streamlit/config.toml＝cwd=root で起動）
```

共通契約（`../polyarchy_common/`）：メタデータ共通コア（層の不変条件）・分野タグ21分類・`logsetup`（日本語ログ＋stdio 保護）・
`capture`・`access`（Cloudflare Access JWT）・`mcp_http`（Streamable HTTP 定型）。依存定義は `../pyproject.toml`。

> Phase 12 のツール（`recommendations/eval/`）: `phase12_pipeline.py`（triage/scaffold/append）・`evalset_gate.py`（決定論ゲート）・
> `mcp_capture_test.py`（捕捉配線テスト）。評価セットは**正典 `data/eval/eval_set.json`（不触）とユーザー由来
> `data/eval/eval_set_userderived.json`（id `u001…`）を別扱い**（provenance 分離）。

## 設定の主要値（`recommendations/core/config.py`）

| 項目 | 値 | メモ |
|---|---|---|
| 埋め込みモデル | `ruri_v3_310m_pfx`（本番・ローカル）| 格納/検索の本番埋め込み。境界検出のみ `text-embedding-3-small` |
| LLM | `claude-sonnet-4-6` | 必要に応じて変更 |
| チャンク戦略 | `semantic`（構造的分割）| Phase 2 比較で最良。実体は `recommendations/ingest/chunking.py` |
| コレクション | `policy_claims_v7`（Qdrant・既定） | 本線＝約3,700文書/187k チャンク（2026-09 時点）。切り戻しは Qdrant 内の `COLLECTION_NAME=policy_claims_v6`（80k）。Chroma 経路はバッチ2 段4〔2026-08-28〕で全廃（v5 データは S3 と退避先に保管のみ） |
| 検索件数 | 5 | top-K |

> チャンクサイズ(`CHUNK_SIZE`/`CHUNK_OVERLAP`)は固定長戦略を使う場合のみ有効な後方互換値。

## 評価・実験

```bash
python -m recommendations.eval.eval               # 本番コレクションを評価セットで採点（正典197・既定）
python -m recommendations.eval.eval --retrieval-only   # 検索のみ・クレジット0（確定的アンカー）
python -m recommendations.eval.eval --filter-eval      # 構造化フィルタ＋層ゲート・クレジット0

# Phase 12: 実運用質問→評価セット（捕捉→トリアージ→ゴールド構築→ゲート→バイト不変追記）
python -m recommendations.ops.quality_candidates --s3                     # 燃料→候補の草稿（zero/low/frequent・stats 版と同型。週次は deploy/scripts/triage.sh）
python -m recommendations.eval.phase12_pipeline triage                    # 捕捉済み実クエリを一覧
python -m recommendations.eval.phase12_pipeline scaffold --from-log N     # 実質問を本番検索しゴールド雛形（careful に編集）
python -m recommendations.eval.phase12_pipeline append cand.json          # evalset_gate 検証→ユーザー由来へバイト不変追記
python -m recommendations.eval.eval --retrieval-only --eval-set both  # クレジット0で正典＋ユーザー由来を採点
# ★最終ステップ：検索型を追加したら、上の実測値でアンカー記載（ルート README「品質の担保」ほか＝
#   append が印字する一覧）を同一コミットで更新する（更新漏れ＝偽の回帰警報。2026-08-20 u010 の教訓）
```

## トラブル時の確認

- スキャン画像PDFはテキスト抽出不可。`pdftotext` 等でテキスト抽出可否を事前確認
- 埋め込み生成が遅い・失敗する場合はPDF数を絞る
- Qdrant コレクションの中身を覗く：`curl -s localhost:6333/collections`（一覧）、
  `python -c "from recommendations.core.qdrant_store import QdrantCorpusStore; print(QdrantCorpusStore().qc.scroll('policy_claims_v7', limit=3, with_payload=True)[0])"`
