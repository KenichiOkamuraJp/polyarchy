# stats — 統計参照DB（Polyarchy）

> **状態：正典（stats の入口）。** ゲート基準は写さずルート README を参照（公開リポジトリと CI で誰でも再現できる形にする＝運用設計 §2.5）。ライセンス＝ルート README「ライセンス」（Apache-2.0・公開する予定）。

「主張（recommendations）↔ 事実（統計）」の突き合わせを可能にする、**値の厳密参照**サービス（長期開発計画 §3 #6・§7 ※）。
ベクトルDBではなく2層：**発見層**（どんな統計系列があるか＝メタデータ検索）＋**参照層**（値の完全一致参照。無ければ「ない」）。

- 作業規約（何を読み・何を読まないか）：[CLAUDE.md](CLAUDE.md)
- データ拡充の順序：[docs/データ拡充計画.md](docs/データ拡充計画.md)
- 取得元別の再配布・商用利用条件：[docs/再配布条件.md](docs/再配布条件.md)
- 全体との契約（受け取るもの・約束するもの・名前・配信）：[docs/共通契約.md](docs/共通契約.md)

## 状態（2026-09-03 更新・稼働中）

- **配信**：`stats.polyarchy.net`（個人認証 案 B・ログイン必須）で 2026-08-28 から箱（EC2）で稼働。箱への反映は `deploy/scripts/release.sh`（ゲート全 PASS → S3 → 自動適用・失敗時は自動切り戻し＝`deploy/RUNBOOK_OPS.md` §5）。
- **収録**：レジストリ `data/registry/series.jsonl`（系列数・目録数は `list_datasets`／`list_sources` の返り値と週次ダッシュボードが正＝本書には写さない）。第 6〜11 弾までの拡充の経緯は [docs/データ拡充計画.md](docs/データ拡充計画.md)・[docs/記録/開発経緯と設計判断_2026-08-25.md](docs/記録/開発経緯と設計判断_2026-08-25.md)。
- **MCP ツール 5 本**（`serving/mcp_server.py`）：`list_datasets`／`find_statistics`／`lookup_statistic`／`lookup_panel`／`list_sources`（契約＝[docs/共通契約.md](docs/共通契約.md)）。
- **ゲート 4 本**：`stats.eval.mcp_smoke`・`exact_match`・`test_core`・`find_quality`（合格基準と現在値の正典＝ルート README「品質の担保」）。
- **運用**：更新チェック（毎日・箱）→ `stats.ops.refresh`／`qe_update`（更新）→ `release.sh`。燃料（捕捉ログ）→ `stats.ops.quality_candidates`（週次 `deploy/scripts/triage.sh`）→ `--accept`。
- 公開ドキュメント＝`https://docs.polyarchy.net/stats`（原稿 `deploy/pages/stats.html`）。

## 実行（リポジトリ root を cwd に）

```bash
python -m stats.eval.mcp_smoke                       # ゲート1：疎通・fail-closed（PASS が出れば環境は正しい）
python -m stats.eval.exact_match                     # ゲート2：原典完全一致（data/eval/*.jsonl）
python -m stats.eval.test_core                       # ゲート3：期間表記・レジストリ検証＋ingest 純関数（和暦・表示書式・行確定・数値判定。ネットワーク不要）
python -m stats.eval.find_quality                    # ゲート4：発見層の品質（この語で引いたら目的の系列に到達できるか）
python -m stats.ingest.seed_registry                 # レジストリ生成（定義は stats/ingest/seed_registry.py）
python -m stats.ingest.estat --all                   # e-Stat 取込（要 ESTAT_APP_ID＝stats/.env か env）
python -m stats.ingest.esri --all                    # 内閣府 SNA xlsx・QE CSV
python -m stats.ingest.boj_file --all                # 日銀 gap.xlsx
python -m stats.ingest.intl --all                    # 国際比較（IMF DataMapper・OECD SDMX・世銀）
python -m stats.ingest.mof --all                     # 財務省 国債金利 CSV
python -m stats.ingest.boj_flat --all                # 日銀 フラットファイル（国際収支・対外資産負債・資金循環）
python -m stats.ingest.mof_zaisei --all              # 財務省 財政統計 Excel
python -m stats.ingest.pdf_shunto --all              # 経団連 春季妥結 PDF（現在は status=guide＝既定では対象なし）
python -m stats.ingest.soumu_hakusho --all           # 総務省 地方財政計画（白書 資料編 CSV）
python -m stats.ops.refresh --help                   # 月次データ更新の 1 コマンド化（ほかの取込モジュールは下の構成を参照）
python -m stats.serving.mcp_server                   # stdio（.mcp.json.example を .mcp.json に複製して登録）
python -m stats.serving.mcp_server --http --port 8766
```

## 取得の作法

- 取得するのは**認証なしで公開されている原典**だけ。低頻度（手動バッチ＋日次の更新チェック）で、原本は `data/cache/` に保存して再取得を避ける。
- UA は **curl 相当が既定**（`ingest/_base.py` の `USER_AGENT`）。例外は 1 つだけ＝中小企業白書の PDF（`ingest/pdf_hakusho.py`）は curl 相当の UA を 403 で弾くため、
  一般的なブラウザ相当の UA を送る（2026-09-19 再実測。ほかの取得元はすべて既定の UA で取れる）。
- **突破しないもの**＝JS チャレンジ（WAF）・ログイン・レート制限（OECD SDMX は 1 時間 60 ダウンロード）。当たったら同日または過去のキャッシュ原本を使い、
  無ければ取り込まない（系列は planned のまま＝値は返さない）。
- 取得元ごとの再配布・商用利用条件は [docs/再配布条件.md](docs/再配布条件.md)。

## 構成

```
stats/
├── CLAUDE.md            作業規約（他サービスのソースは読まない・運用地雷）
├── docs/                共通契約.md・指標棚卸し.md・データソース選定.md・参照粒度設計.md・再配布条件.md・データ拡充計画.md・
│                        業種分類対応表.md・第7弾／第9弾の反映計画・記録/（開発経緯と設計判断）
├── core/
│   ├── paths.py         データ置き場の唯一の定義（DATA_DIR・REGISTRY_PATH・VALUES_DIR・CACHE_DIR・EVAL_DIR・QUERY_LOG_PATH＝env STATS_QUERY_LOG）
│   ├── registry.py      統計系列レジストリ（Series / Registry・共通コア＋命名規則検証・発見層の最小検索・目録）
│   ├── periods.py       期間表記の厳密解釈・取得元時間コードの決定論変換（CONVERTERS）・和暦→西暦（from_wareki_fy/from_wareki_date）
│   ├── values.py        値ストア（data/values/<series_id>.jsonl・完全一致 lookup）
│   ├── licenses.py      再配布・商用利用条件の表（PDL・LICENSES・license_for＝取得経路で決める）
│   ├── dataset_notes.py dataset ごとの「分析の定石」（式・系列 ID・定義＝値は含まない。list_datasets の analysis_notes）
│   └── hojin_vocab.py   法人企業統計の業種・規模スラグの語彙      dim_vocab.py  それ以外の dims（業種・活動・規模）の語彙
├── ingest/
│   ├── _base.py         取込の共通部品（fetch＝UA は curl 相当が既定＋Last-Modified→公表日＋指数バックオフ／is_numeric／assert_unique／
│   │                    表読みの小物 _norm・cell_text・_decimals・_pick_row・_col_letter・_col_index／run_cli／finish）。表の読み方は共通化しない
│   ├── seed_registry.py S 系列定義→registry（組合せ展開。分野タグは polyarchy_common.taxonomy.POLICY_TAGS を索引で参照）
│   ├── estat.py         e-Stat API v3（法人企業統計・CPI・労働力調査・人口推計・地方財政状況調査）
│   ├── esri.py          内閣府 ESRI 国民経済計算 xlsx／旧基準 xls／QE CSV
│   ├── boj_file.py      行＝期間・列＝項目の公表 xlsx（日銀 gap.xlsx・社人研 将来推計）
│   ├── boj_flat.py      日銀 時系列統計 フラットファイル zip（国際収支・対外資産負債・資金循環）
│   ├── mof.py           財務省 国債金利 CSV（和暦日付）      mof_zaisei.py  財務省 財政統計 Excel（3 表型・和暦シート名）
│   ├── soumu_hakusho.py 総務省 地方財政白書 資料編 CSV（地方財政計画・版を重ねる）
│   ├── intl.py          IMF DataMapper／OECD SDMX／世界銀行 API（ISO3・IMF は版年以降 kind=projection）
│   ├── boj_mtshtml.py   日銀 主要時系列統計データ表（HTML）      cao_gap.py  内閣府 GDP ギャップ・潜在成長率
│   ├── cao_mitoshi.py   内閣府 政府経済見通し（PDF・kind=projection）
│   ├── maikin.py        毎月勤労統計 長期時系列（e-Stat ファイル提供 CSV）      shokugyo.py  一般職業紹介状況（e-Stat カタログ → xlsx）
│   ├── bls.py           米 BLS Public Data API v1      eurostat.py  Eurostat 配信 API（JSON-stat）
│   ├── pdf_hakusho.py   中小企業白書 付属統計資料 PDF（開業率・廃業率）
│   └── pdf_shunto.py    経団連 春季妥結 PDF（決定論パーサ・現在は status=guide で既定対象なし）
├── serving/mcp_server.py MCP サーバ（発見層・参照層・fail-closed・理由コード・stdio/HTTP）
├── ops/                 freshness.py（取得元の更新の存否確認）・refresh.py（月次更新）・qe_update.py（QE の公表回更新）・
│                        quality_candidates.py（捕捉ログ→評価問の草稿）・series_quality.py・hojin_consistency.py（記録のみ）
├── eval/                _client.py（ゲート共通：サーバ起動パラメータ・payload 取り出し）・mcp_smoke.py・exact_match.py・test_core.py（core＋ingest 純関数）・find_quality.py
└── data/                registry/（git）・eval/（git）・values/ cache/ query_log/（git 外・S3 data/stats/）
```

取込モジュールの型：`ingest_series(s, *, dry_run, day) -> int` と `main(argv)`（`_base.run_cli` で `--series <id>`（複数可）／`--all`／`--dry-run`・終了コード 0/2）。
原本は `data/cache/<kind>/<取得日>/` に保存し、`accessor.cache` にその相対パスを刻む（exact_match の照合対象）。
