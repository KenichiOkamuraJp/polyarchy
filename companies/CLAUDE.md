# companies — 企業情報サービス（Polyarchy の兄弟サービス）

> **状態：規約（セッション作業規約）。** 起案 2026-09-20。

このフォルダで作業するセッションへの規約。**目的＝把握すべき範囲を本サービスに閉じる**。
何を・どの順で作るかは [docs/開発計画.md](docs/開発計画.md)、進捗は [残タスク](../docs/残タスク.md) B25（ここへ写さない）。

## 読む範囲（厳守）

- 読んでよい：`companies/` 配下すべて、`polyarchy_common/`（共通契約・各 docstring が仕様）、`deploy/`（配信）、`docs/`（全体計画）
- **読まない**：`stats/`・`recommendations/` のソース。数表の保持形は stats と同じ考え方を採るが、**`stats.core` を import しない**＝`companies/core` に小さく持つ。
  参考にしてよいのは設計文書だけ＝[stats/docs/参照粒度設計.md](../stats/docs/参照粒度設計.md)（値の同定・版・出典の刻み方）と `polyarchy_common/README.md`。
  実装を確認したいときは Explore サブエージェントに「関数の契約だけ返せ」と投げ、ソース本文を本セッションに流入させない。
- 記憶（Claude memory）はディレクトリごとに別系統。共有すべき運用地雷は下記に転記済み。

## このサービスの性格

- **名前は内容（企業）であって取得元ではない**。EDINET は最初の取得元（`ingest/`）。取得元を足すのは、再配布条件を確認し評価問を立ててから。株価・ニュースは範囲外。
- **取得元は値ごとに必ず刻む**（どの書類の値か＝docID・提出日・要素名・context）。有価証券報告書・決算短信・統合報告書は信頼の水準が違う＝黙って混ぜない。
- **DB は事実（FACT）を返すことに徹する**（2026-09-20 決定）。意味の寄せ・比較・解釈は利用側の Claude／ChatGPT の仕事。正規化キーは標準の要素だけ＝各社の拡張要素（大企業の最上段の収益・セグメントの区分 等）は「会社が定義した項目」としてその会社のラベルつきで返し、ラベルの文言から正規化キーへ寄せない。例外は横断検索の `top_line` だけ＝標準の最上段の収益が無い会社に限り、経営指標の表の拡張要素で収益の語を含むラベルが 1 つだけのとき、キーに寄せずに要素とラベルを行に明示して使う（候補が複数なら除外・2026-09-27 本人決定）。
- **値を近似で返してはならない**。値は公表どおりの文字列（換算・丸め・補完なし。比率は `0.444` のまま・`unit`／`decimals` を添える）。
  - 企業が同定できない・候補が複数→値を返さず、候補と理由を返す（推測で 1 社に決めない）。
  - その会社に無い項目→`found=false`＋理由。**隣の項目で埋めない**（銀行の経常収益を売上高として返すのは捏造）。
  - 連結と単体は別の系列。単体へ落ちてよいのは連結財務諸表を作成していない会社だけ＝**項目単位では落とさない**（持株会社で「売上高」に単体の営業収益が黙って入る＝[実データ検証](docs/記録/実データ検証_2026-09-20.md) §2）。
  - **連結の有無は決算期ごと**（2026-09-28）＝会社の最新の書類の宣言（DEI）ではなく、その期に連結の値があるか（`lookup.consolidated_at`・横断検索は選んだ書類の当期）。連結をやめた会社の 5 期推移には連結の期が残る（日本製麻）・始めた会社の古い期は単体だけ（六甲バター）＝いまの置き場で 60 社と 291 社。
- **派生値を計算するのは横断検索（`core/screen.py`・`screen_companies`）と時系列（`core/trend.py`＝第 1e 便・年ごとの値はその年を載せた最新の書類・集約は固定の語彙）だけ**（2026-09-27・2026-09-30）＝式は項目の語彙に閉じる（四則演算と期のずれ）・正しさはエンジンの共通の規則（同じ書類・5 期推移・欠けた入力と分母 0 以下と変則決算と基準の変更は除外）・行ごとに式と入力の開示値と出典・比較できない会社は除外の理由と件数・未収録の入力は `input_not_ingested`＝[第 1c 便 計画](docs/第1c便_計画.md)。単社の参照層は派生値を返さない。検証済みの型・未収録の目録を足すときは評価問（`screen*.jsonl`）を同じ変更に。
- 期間キーは決算期末（`YYYY-MM`）。同じ決算期の値は後年の書類で遡及修正され得る＝出所の書類を値に刻む。
- 評価は hit@5 ではなく**原典との完全一致**（`companies.eval.exact_match`）。別名（社名のゆれ・業種別の要素名）は**問を立ててから足す**。
- 原文は丸ごと再配布しない＝値＋引用 1 行＋出典 URL。EDINET タクソノミ自体は再配布しない（値・要素名の利用は可）。
- 公開データのみ・参照専用・サーバ側で生成 AI を呼ばない・層は公開固定（ツールに `layer` 引数を作らない）。

## 取得の作法（EDINET）

- **EDINET API v2 だけを使う**（画面のスクレイピングはしない）。1 リクエスト／秒（`WAIT_SEC`）・取得した zip は `data/cache/` に置き、同じ書類を二度取りに行かない。
- API キーは `EDINET_API_KEY`（環境変数か `companies/.env`・git 外）。**取込側（作業用 PC）だけで使い、箱には運ばない**＝箱のランタイム秘密ゼロを保つ。ログ・文書・コミットに値を残さない。
- `data/`（原本 zip・検証出力・登録簿と値）は git 外。

## 実行（リポジトリ root を cwd に）

```bash
python -m companies.ingest.verify_xbrl --from 2025-06-20 --to 2025-06-30 --n 10   # 着手前検証（被覆率・未採用要素・容量の見立て）
python -m companies.ingest.verify_xbrl --docids S100XXXX,S100YYYY               # 書類を指定（銀行・証券・保険・IFRS・連結なしを含める）
python -m companies.eval.make_candidates    # 評価問の素材づくり（既存の問の書類が対象・--docids=… で足す。★exact_match／fail_closed を上書きする）
python -m companies.eval.test_core          # 語彙と評価問の整合（ネットワーク不要）
python -m companies.ingest.edinet --cached   # 取得済みの書類を値の置き場（data/store/）へ取り込む（--docids／--from --to もある）。セグメント別（store/segments/）・地域別（store/regions/）も同時に
python -m companies.ingest.edinet --fetch-only --from 2016-09-29 --to 2025-06-15   # 有報の zip を取るだけ（古い日から＝API の取得範囲は提出日で約 10 年前まで・古い側から毎日消える＝第 1d 便）
python -m companies.ingest.edinet --build-index   # 横断検索の索引（store/screen_index/＝項目ごと）だけを作り直す（取込の最後にも自動で作る・合わないと screen_exact が FAIL）
python -m companies.ops.build_segment_labels  # 取込の後＝セグメントに出る標準要素の公式ラベル表（core/segment_labels.json・git 追跡）を公式 CSV から作り直す
python -m companies.eval.make_backfill_candidates   # 第 1d 便の評価問（★backfill*・period_basis*・screen_backfill* を上書き・遡り〔D・E〕は全量の置き場から選ぶ）
python -m companies.eval.make_segment_candidates  # 第 1b 便の評価問の素材づくり（★segments*.jsonl を上書きする）
python -m companies.eval.make_region_candidates   # 第 1b 便②（地域別）の評価問の素材づくり（★regions*.jsonl を上書きする）
python -m companies.ops.build_industries    # 業種（EDINET コードリスト＝data/cache/codelist/ に置いてから・RUNBOOK §5）
python -m companies.eval.make_screen_candidates  # 第 1c 便（横断検索）の値の正例の素材づくり（★screen.jsonl を上書きする・人手の問は screen_manual／screen_fail_closed／screen_queries）
python -m companies.eval.make_trend_candidates  # 第 1e 便（時系列）の評価問（★trend.jsonl・trend_fail_closed.jsonl を上書き・全書類の公式 CSV を API から取る・人手の問は trend_queries）
python -m companies.eval.exact_match        # 原典完全一致＝正例・負例とも全問 PASS（第 1b 便の segments_exact・regions_exact・第 1c 便の industries_exact・screen_exact も続けて判定）
python -m companies.eval.find_quality       # 発見層（企業の同定）＝全問 PASS
python -m companies.ops.population_report   # 母集団の棚卸し（取込のあとに回す＝語彙・契約が標本の外でも成り立つか）
python -m companies.eval.mcp_smoke          # MCP 疎通・ツール定義・fail-closed・stdout クリーン
python -m companies.serving.mcp_server      # stdio（--http --port 8767 で配信形）
```

出力＝`companies/data/verify/<実行日時>/coverage.md`。ゲートの基準はルート README「品質の担保」。

- ★ **標本で決めた語彙・契約は、取込のあとに母集団で洗う**（`population_report`）。20 社では見えなかった型が 2,400 社で 6 つ、4,100 社でさらに 2 つ出た＝[母集団の棚卸し](docs/記録/母集団の棚卸し_2026-09-21.md)・[全社の取込と棚卸し](docs/記録/全社の取込と棚卸し_2026-09-22.md)。取込の漏れは「最上段の収益が見当たらない会社」の一覧で見つかる。
- ★ **HTML の `<table>` があることを「表がある」としない**＝省略・該当なしの文を枠なしの 1 セルの表に入れる会社がある（PDF では段落に見える・地域別で 6 社）。値の表かはセルの中身で決める（`core/regions.py` の `_is_value_table`）。
- ★ **XBRL のリンクベースは名前を決め打ちしない**＝ラベルは loc（要素の id）→ labelArc → label の順にたどる。`xlink:label` の名前は書類ごとに任意（`<接頭辞>_<要素>_label` と `label_<要素>` の 2 通りを実測）＝決め打ちで 243 社の拡張要素のラベルが空だった（2026-09-23・[人手の目視](docs/記録/人手の目視_2026-09-23.md) §3）。公式 CSV の「項目名」も拡張要素で空になる書類がある＝ラベルの突き合わせ先は本文（`0101010_honbun_*.htm`）。
- ★ **既存の `exact_match.jsonl`／`fail_closed.jsonl` を `make_candidates` で再生成しない**＝人手で足した問がある（ソニーの接頭辞なし・E03160 の ROE 等＝再生成すると消えることを 2026-09-28 に確かめた）。新しい便の問は別のファイル（`period_basis*`・`backfill*`・`*_backfill`）に置き、ゲートが続けて読む。
- ★ **横断検索の索引は「問の間は手放さない」**＝項目ごとのファイルを LRU だけで持つと、最上段の収益の判定だけで 9 項目を見るため問の途中で追い出しと読み直しが起き、現状の問が 0.05 秒→12〜22 秒になった（ゲートは通る＝速さは測らないと気づかない・2026-09-28）。索引のファイルは 1 行 1 社（1 つの JSON にすると読み込みの一時的なメモリが 4 倍）。
- ★ **横断検索の索引の形式を変えたら `core/screen.py` の `INDEX_VERSION` を上げる**＝古い形式の索引を新しいコードが黙って読まない（ゲートの `index_status` が版の違いで FAIL・2026-09-30 に行へ decimals を足して版 2）。
- ★ **書類一覧の `xbrlFlag=1` でも XBRL が無い書類がある**（HTML だけ＝テムザックの 2018 年提出の古い有報 4 本）・**PublicDoc のインスタンスが 2 個の書類がある**（参天製薬 2017 年＝jpcrp030000 と ifrs-asr）＝`parse_instance` は両方を合わせる・無ければ `NoInstance`（失敗に数えない）。
- ★ **値の置き場の退避・一時ファイルは `data/cache/` の下に**＝`upload_to_s3.sh` は `data/` を `cache/`・`verify/`・`logs/`・`query_log/` 以外すべて S3（→箱）へ運ぶ。
- ★ **配信側（`core/`・`serving/`）に lxml など取込用の依存を import しない**＝箱のロックに入っていない（取込は作業用 PC だけ・`pip install -e ".[companies]"`）。
- ★ **stdio では `guard_stdout_for_stdio()` が返す実 stdout を `stdio_server(stdout=…)` に渡す**（`mcp.run()` をそのまま呼ぶと、差し替え後の stdout＝stderr にプロトコルが流れてクライアントが無応答で止まる＝2026-09-20 に実際に踏んだ）。

## 運用地雷（共有・ルート CLAUDE.md §5 から転記）

- ★ **作業用 PC で `cloudflared tunnel run` を実行しない**：同じトンネルを 2 箇所で run すると本番トラフィックが分流する。
- ★ **IP 許可リストは使えない**：コネクタは Anthropic／OpenAI のサーバ側から接続してくる。入口の防御＝ログイン＋秘密パス＋レート制限。
- ★ **認証の三点一致**：IdP の Resource indicator・`AUTH_AUD_COMPANIES`・実 URL（`companies.<domain>`＋秘密パス）が同一文字列。認証ホストの秘密パスは原則回転しない。
- ★ **依存を足したらロックを再生成**（`deploy/scripts/lock_deps.sh`→RUNBOOK §7）。`pyproject.toml` のパッケージ対象（`include`）に `companies*` を足すのを忘れない。
- ★ **配布は開発用フォルダからではなく、配布用のクローンから**（RUNBOOK §5）。指示なく `git commit`・`git push`・`release.sh` を実行しない。
