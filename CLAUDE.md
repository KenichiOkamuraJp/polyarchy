# Polyarchy — リポジトリ root の作業規約

> **状態：規約（セッション作業規約）。** 起案 2026-09-17。**新しい開発者・AI セッション（記憶ゼロ）が最初に読む 1 枚**。
> フォルダ単位の規約＝[stats/CLAUDE.md](stats/CLAUDE.md)・[recommendations/CLAUDE.md](recommendations/CLAUDE.md)・[companies/CLAUDE.md](companies/CLAUDE.md)・[deliberations/CLAUDE.md](deliberations/CLAUDE.md)（そのフォルダで閉じて作業するときはそちらが優先）。
> 本書は地図と地雷だけを持つ。定義・数値・タスク・手順は写さず、下の入口へリンクする。

## 1. 最初に読む順（30 分）

1. [README.md](README.md)＝何であるか・構成・**文書の地図**・**品質の担保（ゲートの基準値はここだけ）**
2. [docs/用語と役割.md](docs/用語と役割.md)＝役割（導入団体・PdM・開発者・運用者・監査人）と用語（箱・燃料・ゲート・三点一致）
3. [docs/残タスク.md](docs/残タスク.md)＝いま何が済み、何が残っているか（**進捗の正典はここだけ**）
4. [docs/運用設計.md](docs/運用設計.md) §0＝誰が何をするか（層①〜⑥）
5. 運用するなら [deploy/RUNBOOK_OPS.md](deploy/RUNBOOK_OPS.md)（障害対応・データ更新・依存更新）／環境を建てるなら [deploy/README.md](deploy/README.md)・[deploy/PROD_MIGRATION.md](deploy/PROD_MIGRATION.md)
6. stats の経緯を知るなら [stats/docs/記録/開発経緯と設計判断_2026-08-25.md](stats/docs/記録/開発経緯と設計判断_2026-08-25.md)

## 2. 文書の規則（[docs/文書管理ルール.md](docs/文書管理ルール.md) の短縮版）

- 文書は 4 分類（正典／設計／記録／規約）＋社内ノート（git 外）。冒頭の「状態：」で宣言し、ルート README の地図に登録する。
- 定義は用語集・数値はルート README「品質の担保」・タスクは残タスク＝正典は 1 箇所。他の文書は写さずリンクする。
- 記録（日付つき）は更新しない（古い名前・数値が残っていても直さない）。進捗は残タスクのステータス列（1〜2 行）、経緯は §E 対応履歴（追記のみ）。
- 相手方情報・個人情報／実利用ログ・秘密・内部事情は git に書かない＝`社内ノート/`（`.gitignore` と配布 tar の除外の両方で守られている）。
- 対外文書は内部語を定義してから使い、到達目標を現状として書かない。

## 3. このソフトウェアの性格（変えてはいけないもの）

- **公開データのみ・参照専用・サーバ側で生成 AI を呼ばない。** ツールは読み取り専用。文章の生成は利用者の Claude／ChatGPT。
- **層は公開固定**：ツールに `layer` 引数を作らない。非公開文書を扱うモジュールは**本ソフトウェアの範囲外**（作るなら別リポジトリ＝型は [docs/非公開コーパスの参照設計.md](docs/非公開コーパスの参照設計.md)）。
- **fail-closed**：無ければ「ない」と返す。stats は値を近似・補間しない（近い年・隣の指標を返すのは捏造）。
- **ゲート全 PASS のときだけ配布**（`deploy/scripts/release.sh` が唯一の出口）。アンカー（hit@5／MRR）は回帰を検知するトリップワイヤであって最大化する KPI ではない＝固定の評価問に合わせた検索側の調整（過適合）をしない。基準値を変える変更は README「品質の担保」・`release.sh` の既定値・関連文書を**同一コミット**で直す。
- **サービスは互いのソースを読まない**（共有は共通契約 `polyarchy_common` と、文書検索の部品の共有ライブラリ `polyarchy_retrieval` だけ）。データ刻印 `policy_claims*`・systemd 名 `polyarchy-mcp` は歴史的な名前のまま据え置き（改名対象外）。
- **利用者はログイン必須**（外部 IdP）。無認証には戻さない。**例外は中継の口の 1 つだけ**（2026-10-16・千人規模の問を 1 つの bot が中継する導入団体のため）＝既定は閉・開けても同じネットワークの中の登録した送り元だけ・会員の印が必須・公開の口では印を信じない（`polyarchy_common/relay.py`・手順は [deploy/PROD_MIGRATION.md](deploy/PROD_MIGRATION.md) §2.6）。この条件を緩めない・2 つ目の例外を作らない。

## 4. 実行の要点（リポジトリ root を cwd に）

```bash
pip install -e ".[all]"                            # ローカル（1 env に全部＝取込の extras も）。箱・配布の env はロックファイルから（RUNBOOK §7）
python -m recommendations.eval.eval --retrieval-only --eval-set both   # ゲート（基準は README）
python -m stats.eval.exact_match
python -m polyarchy_common.tests.test_common
bash deploy/scripts/release.sh <env>                # 配る前の確認→全ゲート（一覧は README「品質の担保」）→S3 配布→箱が 15 分以内に自動適用（失敗時は自動切り戻し）
```

- ★ `release.sh` は**ロックから作った Python 環境（RUNBOOK §7）を PATH の先頭に通して**実行する＝配る前の確認が `deploy/scripts/check_lock.py` でロックとの一致を照合し、違えば止まる（開発用の env は版がずれている＝基準値を測る前にも手で回す）。
- ★ **配布は開発用のフォルダからではなく、リポジトリをクローンした別フォルダから**（ゲートが測る `qdrant-dev` のマウント元と配る索引を同じフォルダにする＝`release.sh` が照合して止める。code tar は `git archive HEAD`＝未コミットのコードは配らない・コードの範囲が dirty なら止まる。手順＝RUNBOOK §5「配布用のクローンを開発用と分ける」）。
- ローカル stdio で使うなら `.mcp.json.example`（`stats/` にも同名）を `.mcp.json` に複製し、`<PYTHON>`・`<REPO>` を自分の環境のパスに書き換える（`.mcp.json` は git 外）。
- データ（PDF・索引・統計値）は git 外＝S3 が原本。クローンしただけではゲートは回らない（RUNBOOK §3 データ復元）。e-Stat の appId は各自が取得して `stats/.env`（git 外）か環境変数へ。

## 5. 運用地雷（実際に踏んだもの。詳細はリンク先）

- ★ **箱のトンネル資格情報を作業用 PC に置かない・PC で `cloudflared tunnel run` しない**：同じトンネルを 2 箇所で run すると本番トラフィックが分流する。公開実験が要るなら新規トンネル＋新ホスト名で。
- ★ **IP 許可リストは使えない**：Claude／ChatGPT のコネクタは利用者の PC ではなく Anthropic／OpenAI のサーバ側から接続してくる。OAuth のディスカバリとログインも別経路で来る。入口の防御＝ログイン＋秘密パス＋レート制限（`cloudflare-guard.sh` は `IP_ALLOWLIST=off` が既定。秘密パスルールは `/.well-known/`・`/cdn-cgi/`・`/healthz` を除外＝認証の生命線）。
- ★ **認証の三点一致**：IdP の Resource indicator・env の `AUTH_AUD_*`・実 URL（ホスト＋秘密パス）が同一文字列。認証ホストの秘密パスは原則回転しない（RUNBOOK §4）。IdP 側は DCR 有効＋Resource indicator を Default 指定＋JWT に email クレーム（PROD_MIGRATION §2.5）。
- ★ **bootstrap の再走行はコードを更新しない**（コード反映は tar 再展開＝通常は自動適用がやる）。箱を手で触らない。どうしても手動 apply するときは `systemd-cat -t polyarchy-dataapply` を挟む（監査線に残すため・RUNBOOK §5）。
- ★ **自動適用のスクリプト（`deploy/bootstrap/apply_data_update.sh` ほか `/usr/local/bin/polyarchy-*` に入るもの）の変更は、次の次の配布から効く**：自動適用の中の bootstrap 再走行で入れ替わる＝その配布自体は旧スクリプトで走る。直したことの確認は 2 回目の配布で行う。
- ★ **箱で root が動くもの（自動適用・bootstrap）は、polyarchy が書く場所を `mkdir` で作らない**＝`install -d -o polyarchy -g polyarchy`（親から順に並べる＝子だけ渡すと親は root のまま）。`ops/` は git 外＝配る tar に入らない＝最初に作ったプロセスが持ち主になる。staging は配布のたびのコードの展開（`chown -R`）で隠れ、**まだ一度も配っていない新しい環境の初回構築でだけ**ダッシュボード・捕捉ログの同期・週次レポートが Permission denied で止まる（2026-10-28・B47）。`ops/` の下に書き先を足したら bootstrap の `install -d` にも足す（`test_common` が突き合わせる）。
- ★ **S3→箱の `aws s3 sync` は `--exact-timestamps` を付ける**：既定は大きさが同じファイルを取り直さない＝中身が変わっても古いまま残る（2026-10-07：審議会DB の束の目印 `bundle.json` が同じ 614 バイトで、10-04 以降の配布で束が復元されなかった）。`deploy/bootstrap/bootstrap.sh` のデータ同期と `apply_data_update.sh` の Qdrant の写し（2026-10-09・次の次の配布から効く）は対応済み＝`test_common` が S3→箱の全同期を検査する。
- ★ **文書検索の 2 サービス（政策主張DB・審議会DB）はツールを同時に動かす**（unit の `MCP_TOOL_CONCURRENCY`・2026-10-14）：検索の部品に、呼び出しをまたいで書き換える状態（共有の検索器の条件の差し替え・スレッドで共有する解析器）を足さない＝条件は呼び出しごとに渡す。取り違えは黙って別の結果になる＝各 `eval/concurrency`（ゲート）が同時に流して 1 本ずつと突き合わせる。ほかのサービスで同時に動かすなら、同じゲートを作ってから `parallel_safe=True`。
- ★ **公開面のサービスと Qdrant の unit は封じ込めてある**（`ProtectSystem=strict` ほか・2026-10-19）＝書けるのは unit の `ReadWritePaths` だけ。サービスが実行中に書く先を足したら、`ReadWritePaths` と bootstrap の `mkdir` の両方に足す（足さないと Read-only file system＝捕捉ログのように fail-open の書き込みは黙って止まる・`test_common` が突き合わせる）。
- ★ **terraform の state は S3**（2026-10-19）＝`deploy.sh` を通す（backend の値は `lib.sh` の `tf_init` が渡す）。直に `terraform init` しない・`-reconfigure` しない。local の state を持つフォルダは 1 回だけ `deploy.sh <env> state-migrate`（PROD_MIGRATION §4）。
- ★ **依存を変えたらロックを再生成**（`deploy/scripts/lock_deps.sh`→PR→ゲート＝RUNBOOK §7）。`mcp` は `>=2.3,<3`（2.x＝`MCPServer`・2026-10-20〜＝B36）。SDK の内側の名前に頼るのは `polyarchy_common/mcp_http.py` だけ。サーバは `MCPServer(..., subscriptions=False)`（一覧の変化を知らせる口を開けない・`test_common` が 4 サービスを検査＝サービスを足したら `SERVICES` にも）。★ツールの定義（名前・説明・引数・注釈）とサーバの説明は `polyarchy_common/tests/tool_definitions/` の記録と 1 文字も違わないこと（`test_common`）＝変わると ChatGPT のコネクタは作り直し。SDK を上げて落ちたら記録を取り直さず SDK 側の差を吸収する（取り直すのは定義を意図して変えるときだけ）。箱の Qdrant は musl ビルド必須。
- ★ **箱で動くもの（4 サービス・更新チェック・ダッシュボードほか `deploy/bootstrap/*.sh` が呼ぶもの）の依存は箱の extras（`recommendations`・`stats`）に置く**：作業用 PC だけの extras（`ingest`・`companies`）に移すと、bootstrap ③ が箱から外し、best-effort の処理は黙って止まる（2026-10-30・B48＝新着チェックが bs4 の import で 10-20 の配布から止まっていたはず）。`test_common` が入口から関数の中の import まで辿って箱のロックと照合する＝落ちたら依存を箱の extras に戻す（例外を足すのは箱では呼ばれないと示せるときだけ）。
- ★ **Linux の作業用 PC では、Docker の Qdrant（root）が書いた索引のファイルが root の持ち物・0600＝作業用のユーザーが読めない**（Mac の Docker では表に出ない・2026-10-30・B49）：`aws s3 sync` は読めないファイルを飛ばし、ほかの上りと `--delete` を済ませてから失敗する＝S3 が欠けた索引で残る。`upload_to_s3.sh` は S3 に何か書く前に上る元が全部読めるかを確かめ、`release.sh` はコンテナの中から読み取りの権限を足す。★上りの同期を足すときは `data_syncs` の一覧に足す（素の `s3 sync` で上げない＝確認を通らない・`test_common` が見る）。
- ★ **ChatGPT（Business）のコネクタ**：利用者ごとの認可は「個人の設定→アプリ→接続」で行う（作成画面では出ない）。公開後にツール定義を更新できない＝変えたら作り直し。
- ★ **OECD SDMX は 1 時間 60 ダウンロードの制限**（解除手段なし）。取得の UA は curl 相当が既定（IMF／OECD の WAF）。例外は UA 文字列で弾く中小企業白書 PDF だけ＝方針は [stats/README.md](stats/README.md)「取得の作法」（JS チャレンジ・ログイン・レート制限は突破しない）。
- 秘密は SSM Parameter Store。`.env` を箱に運ばない。runbook・シェル履歴・コミットに値を残さない。Qdrant のデータ同期は Qdrant 停止中にのみ行う。
- 捕捉ログは 30 日で自動削除（箱の timer と S3 ライフサイクルの両方）。残したい問は期限内に評価セットへ昇華する（`deploy/scripts/triage.sh`）。★**S3 の非現行の日数を箱の 30 日に「揃えない」**（S3 は版の年齢で数える＝箱 30 日＋非現行 N 日が最長＝30 に揃えると約 60 日残る・2026-10-09 是正）＝正典は deploy/README「捕捉ログの保持」。
- ★ **一覧の写し（サービス・捕捉ログを持つサービス・取得元の型）は静的検査が突き合わせる**（`test_common`＝箱の配線・保持・権限／stats の `test_core`＝refresh と freshness の配線）。サービス・取得元を足して検査が落ちたら、落ちた箇所に足す（文書にはサービス名・本数を列挙しない＝正典へリンク）。

## 6. セッションの振る舞い

- 指示なく `git commit`・`git push`・`release.sh`・`terraform apply`・`deploy.sh`・`cloudflare-guard.sh apply`／`remove` を実行しない（ガードの規則の変更は運営者がターミナルで＝RUNBOOK §1「公開 URL」。`status`・`test` は読み取り）。利用（ドッグフーディング）セッションでは実装もしない＝気づきは残タスクへ。
- found=false・results 空は「正しい答え」であって直す対象ではない。勝手に取込を実装したり他の情報源で補ったりしない。
- 新しい収録・機能は「計画＋評価問＋実装」を 1 つの変更にする（評価問を先に立てる）。
- AI の記憶（memory）は PC とフォルダごとに別系統で、引き継がれない。**次の人に残すべき地雷は本書か各フォルダの CLAUDE.md に書く**（地雷に限り、複数の規約への重複を許す）。
