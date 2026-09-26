# Polyarchy（polyarchy.net）

> **状態：正典（本書が文書の地図・分類の凡例は下記「ドキュメント」節）。** ライセンス＝Apache-2.0（[LICENSE](LICENSE)・[NOTICE](NOTICE)・下記「ライセンス」節）。

日本の政策言説（経済団体・政府審議会の**公表資料**）と公的統計（**公開データ**）を、利用者の Claude／ChatGPT から
ツールとして参照できるようにする MCP サーバ群。現行 3 サービス＝**政策主張DB `recommendations`**（主張）と
**統計参照DB `stats`**（事実）、**企業情報DB `companies`**（企業側の事実・2026-09 追加）。「主張↔事実の突き合わせ」が並べる理由。扱うのは**すべて公開資料・公開データ**。

Polyarchy はサービス全体の名前。実体は**コーパス（文書ジャンル）ごとに独立したサービス**の集まりで、
各サービスは自分の MCP サーバ（別プロセス・別ホスト名）を持ち、共通契約 `polyarchy_common` だけを共有する。

## できること（現行 3 サービス・いずれも MCP サーバとして公開・利用者は自分の Claude／ChatGPT から呼ぶ）

**政策主張DB `recommendations`**（[recommendations/README.md](recommendations/README.md)・公開 `https://docs.polyarchy.net/recommendations`）
- 経団連 / 政府（骨太方針・規制改革・財政審 等）/ 連合 / 日本商工会議所 / 経済同友会 の公表資料を横断検索
- 約 3,700 文書・約 187,000 チャンクを日本語特化のハイブリッド検索（BM25 語彙 ＋ ベクトル → RRF 融合 → 日本語リランカー）
- ツール 3 本＝`search_policy_docs`／`sweep_policy_docs`（団体横断の多段検索＝団体別 fan-out・集約型は健全な棄却）／`list_orgs`

**統計参照DB `stats`**（[stats/README.md](stats/README.md)・公開 `https://docs.polyarchy.net/stats`）
- 日本の公的統計（GDP・QE・需給ギャップ・物価・賃金・労働・財政・資金循環・人口・法人企業統計 業種×規模 等）と
  国際機関統計（IMF／OECD／世銀）の**値の厳密参照**＝公表どおりの文字列を出典付きで返し、無ければ「ない」と返す（近似・補間はしない）
- 2 層＝発見層（`list_datasets`／`find_statistics`／`list_sources`）と参照層（`lookup_statistic`／`lookup_panel`）＝ツール 5 本
- 各値に再配布・商用利用条件（◎／○／△・`status=guide`）を付す（[stats/docs/再配布条件.md](stats/docs/再配布条件.md)）

**企業情報DB `companies`**（[companies/CLAUDE.md](companies/CLAUDE.md)・[計画](companies/docs/開発計画.md)・公開 `https://docs.polyarchy.net/companies`〔箱で有効化後〕）
- 有価証券報告書（EDINET・金融庁・PDL1.0）の「主要な経営指標等の推移」と従業員の状況を、提出会社 約 4,100 社×5 期で**値の厳密参照**＝XBRL の文字列のまま出典付きで返す
- 連結と単体は別の系列・「売上高」が無い会社（IFRS＝売上収益・銀行＝経常収益 等）には代わりに開示している項目と引き直し方を返す・遡及修正と会計基準の併記はもう一方の値を必ず添える
- 2 層＝発見層（`find_company`／`list_items`）と参照層（`lookup_company_facts`）＝ツール 3 本・モデル不要・ランタイム秘密なし（EDINET の API キーは取込側だけ）

## 設計上の性質（セキュリティ）

- **参照専用**：サーバはメール送信・ファイル書き込み等の外部作用を持たない（読み取りツールのみ＝recommendations 3・stats 5・companies 4）
- **サーバ側で生成 AI を呼ばない**：意味検索はローカルモデル（`cl-nagoya/ruri-v3-310m`）で完結・stats／companies はモデル不要。文章生成は各利用者の Claude／ChatGPT 側（ランタイム秘密ゼロ）
- **データ層は公開固定＋フェイルクローズ**：検索経路は公開層に固定され、非公開データは構造的に返らない（`recommendations/core/search_api.py`・不変条件の定義は `polyarchy_common/metadata_core.py`）
- **受信ポートを開けない**：公開は Cloudflare Tunnel 経由（サーバから外向き接続のみ）。管理は AWS Session Manager
- **利用者はログイン必須**（外部 IdP＝WorkOS AuthKit・案 B・2026-09-02〜）＋秘密パス＋レート制限。IP 許可は使えない（コネクタは Anthropic／OpenAI 側から接続）
- **残る壊れ方は供給元と資格情報**（汚染された依存・箱のロールによる配布物改ざん・捕捉ログ・可用性）＝対策の設計は [docs/運用設計.md](docs/運用設計.md) §2.6・実施は [docs/残タスク.md](docs/残タスク.md) B17（依存の sha256 固定と箱ロールの最小化は 2026-09-04 済）。最後の砦＝冪等な bootstrap＋S3 のデータで箱を数分で作り直せること

## 構成（リポジトリ＝Polyarchy 全体・**フォルダ名＝コーパスID**）

```
polyarchy/
├── polyarchy_common/     共通契約：メタデータ共通コア（層の不変条件）・分野タグ21分類・ログ/stdio保護・捕捉ログ・Access JWT・MCP HTTP 定型
├── recommendations/               政策主張DB（経済団体の提言＋政府の主張系文書）
│   ├── core/             検索スタック（config・埋め込み・BM25/ハイブリッド・リランカー・Qdrant アダプタ・search_api・多段）
│   ├── serving/          入口（mcp_server。chat_app／app は 2026-09-02 廃止・残置）
│   ├── ingest/           収集・チャンキング・取込・タグ付け・メタデータ監査
│   ├── eval/             評価セット運用・回帰ゲート・スモークテスト
│   └── data/             PDF・catalog・eval・捕捉ログ（大物は S3 管理・git 外）
├── stats/                統計参照DB — 主張↔事実の突き合わせ。ベクトルではなく発見層（目録・系列検索）＋厳密参照層（値の完全一致）。core/ingest/serving/eval/ops/docs
├── deploy/               AWS 構成（Terraform / bootstrap / systemd / Cloudflare / 運用手順）— 全サービス共通
├── ops/usage/            週次利用レポートの集計の出力先（weekly.jsonl＝数字のみ。実利用の数字なので git 外。生成は `python -m polyarchy_common.usage_report`）
├── docs/                 全体文書（残タスク・運用設計・評価設計・導入団体向け文書・長期計画＝下記「ドキュメント」）
└── pyproject.toml        依存定義（共通＋extras recommendations/stats。ローカルは `pip install -e ".[recommendations,stats]"`・stats 単独箱は `.[stats]`）
```

## 拡張の形（コーパス連邦＝サービスを足しても既存を壊さない）

現行は 2 サービスだが、構造は**足すことを前提**に作ってある。新しいサービスは兄弟フォルダとして増え、
共通契約 `polyarchy_common`（メタデータの共通コア・層の不変条件・捕捉ログ・認証・MCP HTTP 定型）と
`deploy/`（同じ箱・同じ配布経路・同じ監視）だけを共有する＝**あるサービスの作業は他サービスのソースを読まない**
（把握範囲を 1 サービスに閉じる）。連邦レイヤは作らず、利用者側の Claude がルーターになる（設計＝[docs/長期開発計画.md](docs/長期開発計画.md)）。
想定している拡張（**境界＝外部の公開データの整理と利活用促進**。導入組織の非公開文書を扱うモジュールは本ソフトウェアの範囲外＝
必要な組織が自組織帰属の別リポジトリで、本ソフトウェアを依存として使う形で作る。層の不変条件＝公開固定・フェイルクローズは共通契約が担保）：

| 候補 | 内容 | 位置づけ |
|---|---|---|
| **企業情報サービス（`companies/`）** | 有価証券報告書等の企業開示を「主張↔事実」の企業側の事実として参照。最初の取得元＝EDINET（API・PDL 1.0 で商用可） | 着手済（パーサ検証済・計画＝[companies/docs/開発計画.md](companies/docs/開発計画.md)） |
| **政府系の詳細情報サービス** | 審議会・検討会の議事録／論点（`deliberations/`）・国会（`diet/`）・地方議会（`assemblies/`）・外国政府（`foreign/`） | recommendations が「結論（提言・方針）」を扱うのに対し「議論の過程」を扱う兄弟 |

いずれも同じ規律で足す＝評価セットと回帰ゲートを先に立て（fail-closed）、`release.sh` の同じ出口から配布し、
自動適用と切り戻しの同じ仕組みに乗る。要望の吸収先を「コード」でなく「データと評価問」に置く（複雑性を複利にしない）ことが、
サービス数が増えても保守が破綻しない理由（[docs/運用モデルと事業継続性.md](docs/運用モデルと事業継続性.md)）。

- 実装：Python 3.12／MCP 公式 SDK (FastMCP)・LlamaIndex・Qdrant・Sentence-Transformers・fugashi
- 稼働：AWS EC2（東京リージョン）＋ Cloudflare Tunnel。実行はリポジトリ root を Python の root として `python -m recommendations.…`

## ドキュメント（地図）

4 分類＝**正典**（生きている・更新はここだけ）／**設計**（安定リファレンス・判断が変わった時だけ更新）／**記録**（凍結・日付つき・更新しない）／**規約**（セッション作業規約＝各 `CLAUDE.md`）。
行は話題。**どの文書も冒頭の「状態：」で自分の分類を宣言する**。

| 話題 | 正典 | 設計 | 記録 |
|---|---|---|---|
| **総論**（何を・なぜ・次に何を） | [用語と役割](docs/用語と役割.md)＝文書共通の定義（役割・段階・用語はここだけ）／[残タスク](docs/残タスク.md)＝これからやることと対応履歴 | [長期開発計画](docs/長期開発計画.md)＝コーパス連邦・分割原理・拡張順序／[開発環境方針](docs/開発環境方針.md)＝PoC 維持と並行開発の規約／[文書管理ルール](docs/文書管理ルール.md)＝4 分類・地図・正典 1 箇所の規則（他プロジェクトへ持ち出し可） | [リファクタリング所見 2026-08-19](docs/リファクタリング所見_2026-08-19.md)＝配置の読み替え表 |
| **開発計画・評価**（品質をどう測り育てるか） | [評価設計と改善計画](docs/評価設計と改善計画.md)＝何をなぜ測るか（合格基準は本 README「品質の担保」） | [stats データ拡充計画](stats/docs/データ拡充計画.md)・[第7弾](stats/docs/第7弾_ドッグフーディング反映計画.md)・[第9弾](stats/docs/第9弾_マクロ速報ドッグフーディング反映計画.md)＝弾ごとの計画／[companies 開発計画](companies/docs/開発計画.md)＝企業情報サービスの便の段組み・契約案・容量の見立て・[companies 第 1b 便 計画](companies/docs/第1b便_計画.md)＝セグメント別・地域別の実測と契約案／[実利用ログの提供](docs/実利用ログの提供_同意書ひな型と共有経路.md)＝燃料確保の同意書と共有経路 | [stats 開発経緯と設計判断 2026-08-25](stats/docs/記録/開発経緯と設計判断_2026-08-25.md)（**新しいセッションはまずこれ**）／[companies（EDINET）正規化検証 2026-08-22](companies/docs/正規化検証.md)・[実データ検証 2026-09-20](companies/docs/記録/実データ検証_2026-09-20.md)・[母集団の棚卸し 2026-09-21](companies/docs/記録/母集団の棚卸し_2026-09-21.md)・[全社の取込と棚卸し 2026-09-22](companies/docs/記録/全社の取込と棚卸し_2026-09-22.md)・[人手の目視 2026-09-23](companies/docs/記録/人手の目視_2026-09-23.md)・[セグメント別の取込と棚卸し 2026-09-23](companies/docs/記録/セグメント別の取込と棚卸し_2026-09-23.md)・[地域別の表の棚卸し 2026-09-23](companies/docs/記録/地域別の表の棚卸し_2026-09-23.md)・[地域別の PDF 照合 2026-09-26](companies/docs/記録/PDF照合_地域別_2026-09-26.md) |
| **サービス**（各 DB の入口と契約） | [recommendations/README](recommendations/README.md)／[stats/README](stats/README.md)／[polyarchy_common/README](polyarchy_common/README.md)＝**共通契約の正典**／各論＝[stats 共通契約](stats/docs/共通契約.md)・[recommendations 共通契約](recommendations/docs/共通契約.md)／[deploy/pages/README](deploy/pages/README.md)＝公開ページ | stats 設計 5 本（[指標棚卸し](stats/docs/指標棚卸し.md)・[データソース選定](stats/docs/データソース選定.md)・[参照粒度設計](stats/docs/参照粒度設計.md)・[再配布条件](stats/docs/再配布条件.md)・[業種分類対応表](stats/docs/業種分類対応表.md)）／recommendations（[コーパス収録範囲の明示](recommendations/docs/コーパス収録範囲の明示.md)・[再配布条件](recommendations/docs/再配布条件.md)）／[polyarchy_common/](polyarchy_common/__init__.py)＝共通契約パッケージ本体（docstring が詳細仕様） | [公式コネクタ要件](docs/公式コネクタ要件.md)（2026-08-18 調査） |
| **運用**（動かし続ける） | [運用設計](docs/運用設計.md)＝監視・更新・ロールの設計と実施状況／[deploy/README](deploy/README.md)＝AWS runbook（§9.5 が入口ガード/ログ保持の正典）／[deploy/RUNBOOK_OPS](deploy/RUNBOOK_OPS.md)＝障害対応・データ更新・切り戻し | [個人認証_案B設計](docs/個人認証_案B設計.md)＝外部 IdP・②名簿限定→③個人向け提供の段組み | — |
| **導入団体向け**（移行・合意・監査・対外説明） | [監査ガイド](docs/導入団体側_監査ガイド.md)＝公開リポジトリと CI で誰でも再現できる独立監査の仕組み | [PROD_MIGRATION](deploy/PROD_MIGRATION.md)＝導入団体 AWS への転写手順／[運用モデルと事業継続性](docs/運用モデルと事業継続性.md)＝なぜ依存しなくて済むか（対外説明）／[非公開コーパスの参照設計](docs/非公開コーパスの参照設計.md)＝導入団体が内部文書の検索を**別リポジトリで**作るときの型（不変条件・認可関門・検査。本リポジトリでは実装しない） | — |
| **規約**（セッション作業規約＝新しい開発者・AI が最初に読む） | [CLAUDE.md](CLAUDE.md)＝**リポジトリ root の規約（記憶ゼロのセッションはまずこれ**・読む順・性格・運用地雷）／[stats/CLAUDE.md](stats/CLAUDE.md)／[recommendations/CLAUDE.md](recommendations/CLAUDE.md)／[companies/CLAUDE.md](companies/CLAUDE.md) | — | — |

## 運用（人手の箱操作ゼロ・2026-09-03〜）

- **配布**＝`bash deploy/scripts/release.sh <env>`（ゲート 9 本〔companies を有効にした環境は 13 本〕全 PASS のときだけ S3 へ upload → マニフェスト）。提言の定型更新は `update.sh`、燃料の週次トリアージは `triage.sh`。
- **箱**＝15 分毎の自動適用（コード tar 再展開・データ同期・smoke・**失敗時は旧版へ自動切り戻し**）・毎日の更新チェック・毎時のダッシュボード（S3 `ops/dashboard/`）・アラーム→メール。
- **CI 化（計画）**＝ゲートを公開リポジトリの PR／main に紐づけ（GitHub Actions）、release も CI から行う＝運用者の仕事は「PR をマージ」になる。段階と現在地＝[docs/運用設計.md](docs/運用設計.md) §2.5。
- 手順＝[deploy/RUNBOOK_OPS.md](deploy/RUNBOOK_OPS.md) §5・設計＝[docs/運用設計.md](docs/運用設計.md) §0/§2.4・監査＝[docs/導入団体側_監査ガイド.md](docs/導入団体側_監査ガイド.md) §4b。

## ライセンス

- 本リポジトリ（コード・運用自動化・評価資産・設計文書）のライセンスは **Apache-2.0**＝[LICENSE](LICENSE)（標準全文・無改変）・© 表示は [NOTICE](NOTICE)。複製を受け取った者は誰でもこの条件で利用できる。初期開発（2026-08〜09）の履歴は本リポジトリに含めていない。
- 公開に含めないのは 3 種だけ＝**相手方（導入団体）の情報・個人情報と実利用ログ・秘密（鍵・トークン等）**。これらは開示範囲の問題ではなく、最初からリポジトリに含めない。
- 収録データ（PDF・統計値）は git 外＝各取得元の再配布条件に従う（[stats/docs/再配布条件.md](stats/docs/再配布条件.md)・[recommendations/docs/再配布条件.md](recommendations/docs/再配布条件.md)）。コードの公開とデータの再配布条件は別。

## 品質の担保（回帰ゲート）

構成変更のたびに、以下をリポジトリ root から実行して数値が不変であることを確認する。
合格基準と現在値の正典は本節だけ（他文書は写さずここへリンクする）。**公開リポジトリと CI で誰でも同じ数値を再現できる**形にする（CI 化＝[docs/運用設計.md](docs/運用設計.md) §2.5）。

### recommendations（API クレジット 0 で実行可）

（既定値が v7/Qdrant＝env 不要。切り戻しは Qdrant 内の `COLLECTION_NAME=policy_claims_v6`。
Chroma 経路はバッチ2 段4〔2026-08-28〕で全廃＝v5 データは S3 `data/chroma/` と退避先に保管のみ）：

| ゲート | 基準（v7） |
|---|---|
| `python -m recommendations.eval.eval --retrieval-only --eval-set both` | hit@5 86.2% / MRR 0.713 |
| `python -m recommendations.eval.eval --filter-eval` | フィルタ 27/27 ＋ 層ゲート PASS |
| `python -m recommendations.eval.multistage_eval` | 網羅 100% / 集約棄却 12/12 |
| `python -m recommendations.eval.mcp_smoke` | MCP 疎通・層公開固定・stdout クリーン |

### companies（2026-09-20〜。`release.sh` は env の `ENABLE_COMPANIES_APP=true` のとき 4 本を足して 13 本＝2026-09-22 配線）

| ゲート | 基準 |
|---|---|
| `python -m companies.eval.exact_match` | **原典完全一致**（`companies/data/eval/*.jsonl`）＝正例・負例とも全件 PASS（2026-09-23 時点 正例 371／負例 37。期待値は EDINET の公式 CSV と自前パーサの 2 経路一致。問は書類を固定して引く）＋**セグメント別（第 1b 便・`segments_exact` を続けて判定）**＝正例・負例とも全件 PASS（2026-09-23 時点 正例 134／負例 18）・値の置き場の全件で会社が定義した項目のラベルと区分のラベル（標準の区分は会社のラベルが無ければタクソノミの標準ラベル）と要素のラベル（標準要素は公式 CSV の項目名・無ければタクソノミの冗長ラベル）が空の値 0・返した kind はすべて `kind_note` に説明がある・found=false の区分にもラベル＋**地域別（第 1b 便②・`regions_exact` を続けて判定）**＝正例・負例とも全件 PASS（2026-09-25 時点 正例 291／負例 10。期待値は本文の inline XBRL から別のパーサで・合計のセル 40 問はタグの付いた売上高との 2 経路一致）・値の置き場の全件で原典の欄の表の数（入れ子を含む）と写した表の数が一致しない欄 0 |
| `python -m companies.eval.find_quality` | 発見層（企業の同定）の到達率＝全問 PASS（2026-09-21 時点 20 問＝旧社名・表記ゆれ・候補が複数のときは 1 社に決めない） |
| `python -m companies.eval.test_core` | 語彙と評価問の整合・問数の下限（ネットワーク不要） |
| `python -m companies.eval.mcp_smoke` | MCP 疎通・層公開固定・fail-closed・stdout クリーン |

### stats

| ゲート | 基準 |
|---|---|
| `python -m stats.eval.mcp_smoke` | MCP 疎通・fail-closed（該当なしは found=false＋理由）・stdout クリーン |
| `python -m stats.eval.exact_match` | **原典完全一致**（`stats/data/eval/*.jsonl`）＝正例・負例とも全件 PASS（評価セットは収録拡充とともに成長＝2026-09-15 時点 正例 312／負例 103） |
| `python -m stats.eval.test_core` | 期間表記・レジストリ検証・ingest 純関数（ネットワーク不要） |
| `python -m stats.eval.find_quality` | 発見層の到達率＝全問 PASS（2026-09-15 時点 227 問。下限は test_core が固定＝不退転） |

取込・更新時はデータセット固有の整合ゲートも通す（法人企業統計の合計整合・SNA/資金循環の恒等式 7 本など＝
`stats.ops.refresh` が実行）。詳細＝[stats/README.md](stats/README.md)「実行」節。

### 共通

共通契約の単体テスト：`python -m polyarchy_common.tests.test_common`

> 注（2026-08-28・バッチ2 段1）：検索系ゲート（--retrieval-only / --filter-eval）の測定経路を、
> **利用者が実際に通る本番経路＝`PolicySearchService`＋diversify=True**（MCP／chat_app の既定）に
> 一本化した（所見 2026-08-19 段 5-1 の決定を実施）。基準は 85.5%／0.709（eval 直組み・ミス21）から
> **86.9%／0.715（ミス19）**へ更新＝diversify の繰り上がり 2 問（140・150）のみで劣化ゼロ。
> 所見の試算 87.4%／0.718 は u005/u010 追加**前**（143問）の値であり、追加後 145 問では
> u005=hit・u010=miss が分母に入って 126/145＝86.9% になる（125+1 hits・18+1 misses＝算術整合を実測で確認）。
> あわせて f08（機密層指定で空を期待）は --filter-eval から外し（26→25 問）、層の排他は
> 層ゲート自己検証＋`mcp_layer_gate_test` に一本化した（サービス API は layer 引数を持たない＝物理遮断）。
>
> 注（2026-09-08・B19 遡及拡充）：経団連 1990〜2009 の +1,144 文書（チャンク +31,343）で基準を
> **86.9%／0.715（ミス19）→ 86.2%／0.713（ミス20）**へ更新。繰り下がりは 047（連合・相続税の基礎控除）の
> 1 問のみで、新収録 keidanren_2000_026「21世紀を展望した税制改革に向けて」が 3 位に割り込んだ
> 自然な順位変動（主題適合・他 19 ミスは前回ゲートログと同一）。1 問への検索側調整は過適合のため行わない。
