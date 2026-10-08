# stats — 統計参照DB（Polyarchy の兄弟サービス）

> **状態：規約（セッション作業規約）。**

このフォルダで作業するセッションへの規約。**目的＝把握すべき範囲を本サービスに閉じる**。

## 読む範囲（厳守）

- 読んでよい：`stats/` 配下すべて、`polyarchy_common/`（共通契約・各 docstring が仕様）、`deploy/`（配信）、`docs/`（全体計画）
- **読まない**：`recommendations/`（政策主張DB）のソース。必要なのは契約だけで、それは
  **`polyarchy_common/README.md`（共通部の正典・2026-09-02 再編）＋`stats/docs/共通契約.md`（stats の各論）**に書いてある
  ＝この2枚は読んでよい（どちらも許可範囲内。recommendations/docs/共通契約.md は読まなくてよい＝あちらの各論）。
  どうしても recommendations の実装を確認したいときは Explore サブエージェントに「関数の契約だけ返せ」と投げ、ソース本文を本セッションに流入させない。
  **例外**＝一部系列（H5 等）の取得元は recommendations の既収 PDF（`recommendations/data/pdfs/`）：**データファイルの読み取りのみ可**。
- 記憶（Claude memory）はディレクトリごとに別系統。共有すべき運用地雷は下記に転記済み。

## このサービスの性格（長期開発計画 §3 #6）

- **ベクトルDBではない**。2層構造＝**発見層**（どんな統計が存在するかのメタデータ意味検索）＋**参照層**（値の厳密参照ツール）。
- **値を近似で返してはならない**。該当データがなければ「ない」と返す（近い年・隣の指標を返すのは誤答ではなく捏造）。
- 評価は hit@5 ではなく**原典との完全一致**。
- ファセット：統計名・調査主体・期間・地域粒度＋**分野タグ21分類**（`polyarchy_common.taxonomy.POLICY_TAGS`＝recommendations と同じ語彙＝連邦の突き合わせ軸）。
- 一次の利用者は開発者自身（ドッグフーディング）。

## 利用セッションでの振る舞い（ドッグフーディング時の規約）

- **値の質問には lookup の結果を報告するに留める**。found=false（no_values・out_of_range 等）は**それ自体が正しい答え**＝「この系列は未取込／範囲外」と理由を伝えて止まる。勝手に取込を実装したり、他の情報源から値を持ってきて補ったりしない。
- 取込の実装・レジストリ変更・コード修正は**ユーザーの明示の指示があってから**。「〜が無いので取り込みましょうか？」と提案するのは可。
- found=false は捕捉ログ（`stats/data/query_log/queries.jsonl`）に残る＝取込優先順位の一次情報。潰すものではなく集めるもの。
- **利用セッションでは `git commit` しない**。取込を指示されて変更した場合も、未コミットのまま止めて差分とゲート結果を報告する（点検・ゲート再実行・コミットは開発セッションで行う＝品質の関門を素通りさせない）。

## 実行（リポジトリ root を cwd に）

```bash
python -m stats.serving.mcp_server            # stdio（.mcp.json から自動登録も可）
python -m stats.serving.mcp_server --http --port 8766   # Streamable HTTP（配信形）
python -m stats.eval.mcp_smoke                # スモーク（疎通・fail-closed・stdout クリーン）
python -m polyarchy_common.tests.test_common  # 共通契約の単体テスト
```

## 配信（B 案＝別プロセス・別ホスト名。設計は `docs/共通契約.md` §配信）

- systemd `polyarchy-stats.service`（:8766）・Cloudflare `stats.<domain>`・S3 `data/stats/`・tar はリポ全体（deploy/scripts/upload_to_s3.sh）
- 箱では 2026-08-28 から稼働中。箱への変更は `deploy/scripts/release.sh`（ゲート全 PASS → 自動適用）経由のみ＝手で触らない（`deploy/FREEZE` があれば deploy.sh は止まる）。

## 運用地雷（共有・recommendations 側の記憶から転記）

- ★ **Mac で `cloudflared tunnel run` を実行しない**：同じトンネルを 2 箇所で run すると本番トラフィックが分流する（箱のトンネル資格情報は作業用 PC に置かない）。公開実験が要るなら**新規トンネル＋新ホスト名**で。
- ★ **MCP の IP 許可リスト**：Claude のコネクタは Anthropic の IP（160.79.104.0/21）から接続してくる。組織 IP のみ許可だと利用者全員が弾かれる。
- 秘密は SSM Parameter Store。`.env` を箱に運ばない。runbook・シェル履歴に値を残さない。
- 層（公開/機密）は全コーパス共通の不変条件（`polyarchy_common.metadata_core`）。stats は公開データのみ扱うが、payload の `layer` は必ず刻む。

## 取込の地雷（第 12 弾＝財政の PL・BS・2026-10-02 に実際に踏んだもの。経緯は `docs/データ拡充計画.md` §4g）

- ★ **系列の `notes` に、その dataset の全系列に共通の語（部門名・科目名）を書かない**：発見層で全系列が当たり、集計の行（total）が細分より先に並んで上位 20 を占める（同じ弾で 2 回踏んだ）。説明は `core/dataset_notes.py` の analysis_notes へ。find_quality の問が検出する＝問を先に立てる。
- ★ **評価問は時間で壊れない形で作る**（2026-10-05 の定型更新で運用側が 2 問踏んだ）：
  - 負例で「収録の最終期より先」を問うときは `period` を **`@last+N`**（その系列・地域の収録の最終期から N 期先）で書く。絶対の期（例 `2026Q3`）は取得元が次の期を公表した日に落ちる＝`exact_match` が「最終期より先の絶対の期」を FAIL にする。提供開始より前・欠けた期・表記違いの負例は絶対の期のままでよい。
  - 正例の `source_locator` に、中身が時間で入れ替わるファイルを使わない（財務省 国債金利の当月分 `jgbcm.csv`＝月が替わると行が `jgbcm_all.csv` へ移る）。`exact_match.ROLLING_LOCATORS` に登録し、test_core が検査する。新しくそういう取得元を足したら登録する。
  - 値の改定（速報→確報・年次の改定）で正例が落ちるのは想定どおりの人手の分岐＝原典を確かめて期待値を書き換える（作り方の問題ではない）。
- ★ **評価セット（`data/eval/*.jsonl`）の書き換えは Python で**：macOS の BSD sed は `\|` を解さず、`/a\|b/s/todo/pass/` が**黙って 0 件**（昇格したつもりの問が todo のままコミットされた）。
- ★ **新しい表を足したら、既存の表と単位・合計を突き合わせる**：財政統計 第1表の単位を「円」と誤表示していた（正しくは千円）のを、第16表との突き合わせで 1.5 か月後に見つけた。突き合わせは test_core に固定する。
- 範囲の途中で特定の地域だけ値が無い（例：ある年度に未作成の都道府県・復帰前の沖縄）ときの応答は `not_published` ではなく **`out_of_range`**（提供範囲は地域ごと）。負例の期待値はこちら。
- 公表元の Excel は**見出しの位置・行の番号が年版や表で揺れる**（年見出しがブロックの 2 列目・行が挟まりローマ数字がずれる・A 列の合計行・末尾に別の表）。取込は「1 列／1 行に確定しなければエラー」で作る（黙って選ばない）＝今回の罠はすべてこれで止まった。
- 正例（exact_match）は**取込と別の経路**（セル番地を固定・系列コードで直接引く）で照合する。手で選んだセルの誤りもここで止まる。
- **直近 N 年の窓だけを公表する Excel は取り込まない**（再取込で古い期が消える＝値ストアは上書き）＝planned で取得元 URL を返す。機械可読の表が無い（PDF のグラフのみ）ものは `status=guide`＋`guide.reason`。
- 暗号化された Excel（OLE の EncryptedPackage）は解かない＝読めない年版として扱う（翌年版の前年度列で埋まる設計にする）。

## 配線の規則（2026-10-09＝複雑性の点検で、写しの漏れが黙って効いていたもの）

- ★ **取得元の型（accessor.type）を足したら**、`stats/ops/refresh.py` の `TYPE_TO_MODULE` と `stats/ops/freshness.py` の `build_probes` の両方に配線するか、`freshness.EXCLUDED_TYPES` に理由つきで載せる（test_core が「registered の全 type がどちらかにある」「probe が unsupported を出さない」を検査する）。漏れると月次の更新で「更新したつもりで古いまま」になる（`esri_xlsx_yearsheets` の 439 系列が漏れていた）。
- ★ **test_core の値ストアに依存する検査は `need(<代表系列>)` で囲む**（`has_data` で黙って飛ばさない）。release.sh は `STATS_TEST_STRICT=1` で呼ぶ＝スキップが 1 つでもあれば FAIL。
- ★ **法人企業統計の業種×規模のセルの定義は `hojin_industry_panel` が正**（古い層〔第2弾の PL・KEY10・第5弾の HOJIN_VA〕と同じ series_id は build() が位置を保って置き換える）。属性（提供開始・注記・分野）はパネルの側で直す。
- 系列 ID の分解は `Registry.split_id`（登録済みの系列の形に合わせる＝点を含む measure・地域の接尾辞も切れる）。位置（`split(".")[2]`）で切らない。
- dims に dataset 固有の語彙を足したら、`dim_vocab.dim_order` にも原表の順を足す（test_core が検査する）。
