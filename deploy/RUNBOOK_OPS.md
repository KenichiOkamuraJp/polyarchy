# RUNBOOK（運用・障害対応）— staging/prod 共通（作成 2026-08-28・A1 適用時）

> **状態：正典（運用の一次手順書＝障害対応・データ更新・依存更新）。**

分類＝正典（運用の一次手順書）。設計は [docs/運用設計.md](../docs/運用設計.md)。

**読者と使いどき**：日常の状態確認はダッシュボード（監査ガイド §4）で足りる。本書は **アラームが鳴ったとき**（§1）と
**データ更新を出すとき**（§5）に開く。役割＝**運用者**（定型更新＝S3 書込権限だけで足りる・§5）／**障害対応者**（SSM 権限で箱に入る・§1〜§4）／
**PdM**（意味の判断・評価問の採否）／**開発者**（PR を書きゲートを緑にする）。1 人が兼ねてもよい（兼務の可否は用語と役割 §1c）。役割と用語の定義＝[docs/用語と役割.md](../docs/用語と役割.md)。
ホスト名の例は staging（`*.polyarchy.net`）＝prod は `*.<導入団体ドメイン>` に読み替える。
箱への入り口は SSM のみ（SSH なし）：`aws ssm start-session --region ap-northeast-1 --target <instance_id>`
（instance_id＝`cd deploy/terraform && terraform output -raw instance_id`）。

## 0. 監視の全体像（何が鳴るか）

| 経路 | 内容 |
|---|---|
| 毎分 timer `polyarchy-health` | localhost の `/healthz`（:8765 recommendations・:8766 stats）→ CW metric `polyarchy/health`＋disk |
| CloudWatch Alarm → SNS → メール | health 3分連続 0/missing・EC2 StatusCheckFailed・disk>80%・5xx>5/5分・401>30/5分・**dataapply-rollback**（自動適用の切り戻し＝§5） |
| CW Logs `polyarchy/mcp`・`polyarchy/stats` | httplog（時刻・パス・status・ms・user_hash・cf-ray）。保持 30 日 |
| CW Logs `polyarchy/dataapply` | 自動適用の全出力（退避→反映→smoke→判定/切り戻し）＝データ更新の監査線（監査ガイド §4b）。保持 30 日 |
| 箱内のログファイル | `/var/log/polyarchy/*.log`（rsyslog）は logrotate 週次×4 世代（約 5 週）。捕捉ログ本体（query_log）は 30 日 prune |
| Cloudflare Tunnel Health 通知 | ダッシュボードで ON にする（無料・トンネル断） |

## 1. 症状 → 確認 → 復旧

### アラーム「health-mcp / health-stats / health-companies」
```bash
systemctl status polyarchy-mcp polyarchy-stats polyarchy-companies qdrant
curl -s localhost:8765/healthz; curl -s localhost:8766/healthz; curl -s localhost:8767/healthz
journalctl -u polyarchy-mcp -n 50 --no-pager     # or polyarchy-stats / polyarchy-companies
```
- unit が落ちている → `sudo systemctl restart polyarchy-mcp`（Restart=always なのに落ち続ける場合はログの Traceback を読む）
- recommendations の healthz が `ok:false`／chunks=0 → qdrant を疑う：`systemctl status qdrant`・`curl -s localhost:6333/collections`
- 箱ごと無応答（missing）→ EC2 再起動：`aws ec2 reboot-instances --instance-ids <id>`（systemd が全部 enable 済＝自動復帰）

### アラーム「5xx」
```bash
journalctl -u polyarchy-mcp --since "-30min" --no-pager | grep -E " 50[0-9] "
```
アプリ例外なら Traceback が直前に出ている。データ破損疑い（qdrant）なら §3 の復元。

### アラーム「401 急増」
- **認証必須（案 B・2026-09-02〜）のため 401 は正常系にも出る**（未認証プローブ・期限切れトークンの再取得前）。急増の主因候補＝①攻撃/スキャンの試行（レート制限が効いているか＝§1「公開 URL」の `cloudflare-guard.sh status`）②**設定壊れ＝aud 不一致**（利用者全員が繋がらない）。
- 案 B の照合（三点一致）：SSM `auth_issuer`／`auth_aud_<svc>` と WorkOS の Resource indicator と実 URL（ホスト＋秘密パス）が一致しているか。壊れていたら SSM を直して箱で `bootstrap.sh` 再走行 → restart。issuer 変更（テナント差し替え）時は利用者は再ログインになる。
- （旧・案 A の照合＝`access-oauth.sh status` は Access 併用時のみ・現行は未使用）。

### アラーム「dataapply-rollback」（自動適用が切り戻された）
- **可用性は落ちていない**（旧版で稼働中）。リリースが不採用になった事実の通知＝§5「切り戻し後」の手順へ。急がない。
- 例外＝ダッシュボード②の「切り戻し後 smoke」が FAILED のとき（旧版でも壊れている）→ 上の health の手順・§3 の S3 復元。

### アラーム「disk > 80%」
```bash
df -h /; du -xhd1 /opt/polyarchy | sort -h | tail
```
- 大抵は捕捉ログか /var/log。捕捉は 30 日 prune（`polyarchy-logprune.timer`）が動いているか確認：`systemctl list-timers | grep logprune`
- `/opt/polyarchy/rollback/`（約 2.5 GB）と `/opt/polyarchy/releases/` は**削除しない**（自動切り戻しの原資・§5）。PDF は追記専用で増える（2 GB〜）。

### 公開 URL が落ちている（origin は健全）
```bash
systemctl status cloudflared; journalctl -u cloudflared -n 30 --no-pager   # Registered tunnel connection があるか
curl -sI https://recommendations.<domain>/healthz | head -1   # staging は recommendations.polyarchy.net
```
- cloudflared 再起動 → ダメなら Cloudflare 側（DNS・トンネル・WAF）を疑う。WAF ルールの現状＝`bash deploy/scripts/cloudflare-guard.sh status`。
- ★WAF を再適用するとき：`ZONE_NAME`・`MCP_HOST`・`SERVICE`・`SSM_PATH_PARAM`（・prod は `AWS_PROFILE_CF`）を対象に合わせて上書きして apply（guard の既定値は staging 固定）。**IP_ALLOWLIST の既定は off**（2026-09-03 スクリプト側も off に変更＝IP 許可の復活事故は既定では起きない。on は明示時のみ・IP 方式は OAuth/コネクタと両立しない＝2026-08-27/28 実測）。秘密パスルールは /.well-known/・/cdn-cgi/・/healthz を除外済（PRM/認証ディスカバリの生命線）。

## 2. ユニット早見表（箱）

| unit | 役割 | 依存 |
|---|---|---|
| qdrant | ベクトルDB :6333（v7 本線） | mcp より先 |
| polyarchy-mcp | recommendations MCP :8765 | qdrant |
| polyarchy-stats | stats MCP :8766（ENABLE_STATS_APP=true の箱のみ） | — |
| polyarchy-companies | companies MCP :8767（ENABLE_COMPANIES_APP=true の箱のみ・モデル不要・ランタイム秘密なし） | — |
| polyarchy-web | **廃止（2026-09-02）**＝unit はリポ残置・箱には無い | — |
| cloudflared | 入口（トンネル） | 全 origin の後に start が安全 |
| polyarchy-health.timer | 毎分 healthz→CW metric | — |
| polyarchy-fuelsync.timer | 捕捉ログを毎時 S3 へ | — |
| polyarchy-logprune.timer | 捕捉ログ 30 日削除（毎日） | — |
| polyarchy-usagereport.timer | 週次利用レポート→S3 ops/report/（月曜 09:20 JST） | — |
| polyarchy-dashboard.timer | 運用ダッシュボード→S3 ops/dashboard/（毎時 05 分） | — |
| polyarchy-dataapply.timer | 15 分毎に S3 release/data.json を検知→自動適用（退避→コード反映→同期→smoke→切り戻し）＝§5 | root・qdrant/mcp/stats(/companies) を止める |
| polyarchy-updatecheck.timer | 更新チェック（freshness＋提言新着→ダッシュボード反映・毎日 06:10 JST。手動キック＝SSM ドキュメント check-updates） | — |

## 3. データ復元（S3 が原本）

```bash
# 例：qdrant ストレージを S3 から取り直す（破損・巻き戻し時）
sudo systemctl stop polyarchy-mcp polyarchy-stats qdrant
sudo aws s3 sync s3://<bucket>/data/qdrant/ /opt/polyarchy/polyarchy/recommendations/data/qdrant/ --delete --region ap-northeast-1
sudo chown -R polyarchy:polyarchy /opt/polyarchy/polyarchy/recommendations/data
sudo systemctl start qdrant polyarchy-mcp
```
- ★qdrant の S3 側を更新する（Mac→S3）のは**ローカル qdrant-dev を止めてから**（upload_to_s3.sh がガードする）。
- ★箱側で bootstrap を再走行すると ⑥ が data/ を sync する＝qdrant 稼働中に走るので、**qdrant データを S3 から入れ直したい時は上の手順（stop→sync→start）を使い、bootstrap 任せにしない**。
- stats データも同型：`s3://<bucket>/data/stats/` → `stats/data/`（`--delete` は registry/values のみに限定して使う。query_log を消さない）。
- companies データも同型：`s3://<bucket>/data/companies/` → `companies/data/`（store/ と eval/。query_log を消さない）。手元での復元も同じ sync＝原本の zip（`cache/`）は S3 の別 prefix（任意）か再取得（EDINET API・約 4,300 書類・約 3 時間）。
- 完全な作り直し＝`deploy.sh <env> apply` で EC2 を建て直し（S3/SSM/Cloudflare は残るので upload 不要・約 10 分）。

## 4. 秘密の再発行

| 何 | 手順 |
|---|---|
| 秘密パス（mcp/stats） | `aws ssm put-parameter --overwrite --type SecureString --name /polyarchy/<env>/{mcp,stats}_http_path --value "/mcp-$(openssl rand -hex 16)"` → 箱の `/etc/polyarchy/{mcp,stats}.env` の MCP_HTTP_PATH を更新（bootstrap 再走行 or sed）→ サービス restart → guard を該当 SERVICE・`IP_ALLOWLIST=off` で再適用 → 利用者に新 URL を配布。★WAF ルールはエッジ伝播に数十秒かかる（直後の 403 は慌てない）。★**案 B 認証ホストでは秘密パスは原則回転しない**＝PRM で公開されるため「漏洩時の締め出し」効果が無く、回すと三点一致（WorkOS Resource indicator・`AUTH_AUD_<SVC>`・SSM）の3か所同時更新＋全利用者再登録が要る（PROD_MIGRATION §2.3b）。回転が意味を持つのは authless ホストのみ。参考＝claude.ai は URL 単位で旧 OAuth 設定を記憶する（2026-08-28 実測）＝認証方式を変えるときは URL も変えるのが安全 |
| **案 B（auth_issuer/auth_aud_<svc>）** | IdP テナント差し替え・resource URL 変更時：`deploy/env/<env>.env` の `AUTH_*` を更新 → `deploy.sh <env> secrets` → 箱で bootstrap 再走行 → restart。WorkOS 側（Resource indicator・Default・JWT テンプレート）も同時に＝三点一致（PROD_MIGRATION §2.5） |
| Access AUD（旧・案 A） | Access アプリを作り直したら `access-oauth.sh status` で AUD → SSM `access_aud_stats` 更新 → bootstrap 再走行 |
| Cloudflare API トークン | ダッシュボードで再発行 → Keychain `cloudflare-api-token`（WAF）/`cloudflare-access-token`（Access/DNS/Pages）を更新 |
| cloudflared 資格情報 | トンネル作り直し時のみ。`deploy.sh <env> secrets` が SSM へ再登録 |

## 5. データ更新（公開ページの約束）

**何を更新すべきかは毎日の更新チェックが教える**＝ダッシュボード③の「提言の新着 N 件」「stats 取得元の変化 N 件」
（新着の実タイトルは箱の `recommendations/data/cache/update_check.json`）。以下は検知後の**取込の実行**手順。

```bash
# stats：鮮度確認 → 更新（ローカルで実施 → upload → 箱の bootstrap 再走行 or stats 再起動）
python -m stats.ops.freshness
python -m stats.ops.refresh --dataset <コード名>   # ゲート 3 本まで通る・自動コミットしない
```

### 企業情報（companies）の更新 — 有価証券報告書の提出に応じて・全てローカルで取り込んで箱へ配布

```bash
# 取込（作業用 PC・EDINET の API キーは companies/.env＝箱には運ばない）。日付の範囲＝前回以降の提出日。取得済みの書類は再取得しない
python -m companies.ingest.edinet --from 2026-10-01 --to 2026-10-31
python -m companies.ops.population_report | head -40      # 棚卸し＝「語彙に無い標準要素」「最上段の収益が見当たらない会社」が増えていないか
python -m companies.eval.exact_match && python -m companies.eval.find_quality && python -m companies.eval.test_core   # 既存の問は書類を固定して引く＝新しい書類で動かない
bash deploy/scripts/release.sh staging                     # ENABLE_COMPANIES_APP=true の env ならゲート 13 本→配布→自動適用
```
- 6 月（3 月決算の提出集中期）は約 2,400 書類＝約 2 時間。他の月は数十〜数百。
- ★上流の運営者（開発と運用を 1 人が兼ねる）は、取込を開発用フォルダで行い、値の置き場だけを配布用のクローンへ写してから release する（`data/` は git 外＝pull では届かない）：`rsync -a --delete <開発>/companies/data/store/ <クローン>/companies/data/store/`＋`eval/` も同様。原本の zip（cache/）は写さない。
- 語彙に無い標準要素が出たら、公式 CSV（API type=5）でラベルを確かめてから `companies/core/items.py` に足し、`make_candidates --docids=` で問を足す（companies/CLAUDE.md）。

### サービスを足す（companies を有効にする・2026-09-22）

配線はコードに入っている（`ENABLE_COMPANIES_APP` の opt-in・既定 false）。有効化は上流の運営者（または導入団体）の作業＝順に：
1. **IdP（WorkOS）**＝Resource indicator に `https://companies.<domain>/<秘密パス>` を追加（三点一致＝PROD_MIGRATION §2.5）。
2. **env**＝`deploy/env/<env>.env` に `ENABLE_COMPANIES_APP=true`・`TUNNEL_HOST_COMPANIES=companies.<domain>`・`AUTH_AUD_COMPANIES=<aud>`。
3. **SSM**＝`bash deploy/scripts/register-secrets.sh <env>`（`auth_aud_companies` を登録）＋秘密パス `companies_http_path` を `/mcp-<乱数>` で登録（§4 と同じ流儀）。
4. **Terraform**＝`bash deploy/scripts/deploy.sh <env> apply`（ロググループ・health アラーム・箱ロールの書込先・S3 ライフサイクル。EC2 の差分は `ignore_changes` で出ない）。
5. **箱の deploy.env**＝★稼働中の箱は user_data を再実行しない＝SSM Session で `/etc/polyarchy/deploy.env` に同じ 2 行（`ENABLE_COMPANIES_APP`・`TUNNEL_HOST_COMPANIES`）を足す。
6. **Cloudflare**＝DNS `companies.<domain>` → トンネル（CNAME・既存と同じ UUID）・guard を `SERVICE=companies` で apply（`cloudflare-guard.sh` 冒頭）。
7. **配布**＝配布用のクローンで `release.sh <env>`（ゲート 13 本）→ 自動適用が `bootstrap` 再走行で ⑥ データ同期・⑧ companies.env・⑩ ユニット設置・ingress 追記まで行う → ダッシュボードで版一致・`https://companies.<domain>/healthz` が 200。
8. **接続確認**＝claude.ai／Claude Code／ChatGPT の 3 経路（PROD_MIGRATION §2.5 と同じ）。公開ページ `companies.html` はこの後に Pages へ（先に出すと案内だけが先行する）。

### 配布用のクローンを開発用と分ける（推奨・2026-09-19）

`release.sh` が箱へ送るコードは **git の中身ではなく、作業フォルダをそのまま固めた tar** である（`upload_to_s3.sh`）。
開発しているフォルダから配布すると、コミットしていない変更や作業中のファイルも箱に入る。配布は**リポジトリをクローンした別フォルダ**から行う
＝箱に入るものが、リポジトリのコミットと必ず一致する（`VERSION` の刻印にも `-dirty` が付かない）。1 人で開発と運用を兼ねる場合も、フォルダを分ける。

| | 開発用のフォルダ | 配布用のクローン |
|---|---|---|
| やること | コード・評価問・文書 → ゲート → コミット → push | `git pull` → 新着取込（`update.sh`・`stats.ops.refresh`）→ `release.sh` |
| コミットするもの | コード・文書・評価問 | **データ更新だけ**（`catalog.csv`・`stats/data/registry/`・評価セットとアンカー記載）。コードはここで編集しない |
| git 外で置くもの | ゲート用のデータの写し（任意） | `deploy/env/<env>.env`・`recommendations/.env`・`stats/.env`・データの原本（`recommendations/data/{pdfs,bm25,qdrant}`・`stats/data/{values,cache}`）・terraform state（terraform を触るとき） |
| Python 環境 | 任意 | 下表のとおりロックから作る（環境は配布用のクローンを指す editable install にする） |

配布の前に確認する 3 点：
```bash
git status --short && git log origin/main..HEAD --oneline      # どちらも空＝リポジトリのコミットと一致
docker inspect qdrant-dev --format '{{json .HostConfig.Binds}}'  # マウント元が「配布用のクローン」の recommendations/data/qdrant であること
curl -s localhost:6333/collections/policy_claims_v7 | python3 -c "import sys,json;print(json.load(sys.stdin)['result']['points_count'])"
```
- ★ **`qdrant-dev` のマウント元は配布用のクローンに置く**：`release.sh` は `qdrant-dev` を止めて `recommendations/data/qdrant` を S3 へ**ミラー（--delete）**する。
  マウント元が別のフォルダ・空・古いと、その内容で箱の索引を上書きする。開発用のフォルダのゲートは同じ `qdrant-dev` に HTTP（:6333）でつながるので、Qdrant の実体は 1 つでよい。
- Python の editable install は**最後に `pip install -e` したフォルダ**を指す。リポジトリ root 以外を cwd にして動くスクリプトは、その向き先のコードを import する＝フォルダごとに env を分ける。

### 手元の製作環境（初回のみ・運用者を引き継ぐ人が最初に整える）

| 要素 | 内容 |
|---|---|
| Python | 配布用の env は**ロックファイルから**作る（§7 ②＝Mac は `lock_mac_variant.py` の変種を `--require-hashes` で入れ、本体は `-e . --no-deps`）＝箱と同じ版でゲートを回す。開発だけなら `pip install -e ".[recommendations,stats]"`（ルート CLAUDE.md §4）でよい。update.sh/release.sh/triage.sh はその env を PATH の先頭に通した shell で実行 |
| 検索 DB | Docker の `qdrant-dev`（ローカル :6333・`recommendations/data/qdrant` を S3 `data/qdrant/` から同期して起動）。release.sh は起動を確認し、upload 中だけ止める |
| モデル | ruri／リランカーは初回実行時に HF から取得（数 GB）。オフラインでは不可 |
| CLI | `aws`（プロファイル＝`deploy/env/<env>.env` の `AWS_PROFILE`）・`terraform`（バケット名の取得元＝環境変数 `BUCKET` があれば優先＝`deploy/env/<env>.env` に書いておける。無ければ `deploy/terraform/` の state）・`git`（タグ込み clone＝`git describe` が版を刻む） |
| git 外の設定 | `deploy/env/<env>.env`（例は `*.env.example`）・`recommendations/.env`（`ANTHROPIC_API_KEY`＝分野タグ判定／`OPENAI_API_KEY`＝チャンク境界検出）・`stats/.env`（`ESTAT_APP_ID`）・Keychain `cloudflare-api-token`（guard 用）・`~/.cloudflared/<UUID>.json` |
| 権限 | 定型更新＝[deploy/iam-data-operator-policy.json](iam-data-operator-policy.json)（S3 のみ）。障害対応＝SSM・EC2・CloudWatch Logs の読み書き（PROD_MIGRATION §1.2） |
| git 運用 | update.sh／refresh は `catalog.csv`・registry の差分を表示して止まる＝**レビューしてコミット**（PR → PdM の承認でマージ＝運用設計 §0。1 人運用のあいだは本人がレビュー）。評価セットを足したらアンカー記載も同一コミット |

### 提言（recommendations コーパス）の新規取込 — 公表に応じて・全てローカルで作って箱へ配布

**定型更新は 1 コマンド（運用者ロール・2026-09-03）**：

```bash
bash deploy/scripts/update.sh staging      # 新着検出→収集→追い判定→取込→release.sh（--no-release で配布前停止）
```

以下は工程の内訳（個別に実行する場合・トラブル時の切り分け用）：

```bash
# ①収集：5 団体アダプタで PDF 取得＋catalog.csv 登録（org= keidanren|doyukai|nissho|rengo|gov）
python -m recommendations.ingest.collect <org> --skip-existing   # gov は --source で審議会を指定
# ②分野タグ・文書性格の追い判定（新規行だけ判定・--dry-run で対象確認から）
python -m recommendations.ingest.policy_tagger --dry-run
python -m recommendations.ingest.policy_tagger
# ③取込（v7/Qdrant 本線・doc 単位増分・冪等。ローカル qdrant-dev を起動しておく）
COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.qdrant_ingest ingest
# ④⑤ ゲート→配布→マニフェスト＝release.sh に一本化（2026-09-03。手順を飛ばす余地を作らない）
bash deploy/scripts/release.sh <staging|prod>
```

**release.sh がやること**＝ゲート 9 本（recommendations 4＝retrieval アンカー非劣化・filter 全問・多段・smoke（基準値はルート README「品質の担保」）／stats 4＝smoke・exact_match・test_core・find_quality／共通テスト 1）を実行し**全 PASS のときだけ** upload → 最後に S3 `release/data.json`
（リリースマニフェスト＝出荷時のゲート数値入り）を書く。**1 つでも FAIL なら S3 に何も置かれない**。

**箱側の反映は自動**＝`polyarchy-dataapply.timer`（15 分毎）がマニフェストの変化を検知し、
退避（rsync）→ サービス停止 → **コード tar 再展開**（2026-09-03〜＝コード変更もこの経路で自動反映・tar 再展開の手作業は廃止）
→ qdrant ミラー同期 → bootstrap（データ差分 sync・依存再解決）→ 再起動 → 箱上 smoke → ダッシュボード、まで
無人で行う（全工程 journal→CW Logs `polyarchy/dataapply`・**人手の箱操作ゼロ**）。反映確認＝ダッシュボード②の
データ版が released/applied 一致・APPLIED・smoke PASS になっていること。

**smoke FAIL 時は自動切り戻し**（2026-09-03・B15 論点1＝運用者ロール移譲の前提 (2)）＝停止 → 退避したデータを
書き戻し（qdrant はミラー）→ 前回の code tar を再展開＋pip → 再起動 → smoke 再実行 → マーク status=ROLLED_BACK →
メトリクス dataapply=0 → アラーム `dataapply-rollback` → メール。**不採用のマニフェストは再試行しない**（15 分毎の
再適用ループを防ぐ）。qdrant が起動しない・bootstrap が落ちる等、反映途中の失敗も同じ経路で戻る。
退避先＝箱の `/opt/polyarchy/rollback/`（データ）と `/opt/polyarchy/releases/{current,prev}.tar.gz`（コード）。
**リリース中は health アラームが ALARM→OK と 1 往復する（想定内）**＝停止→bootstrap→再起動が 3 分を超えるため
（2026-09-03 実測：2 回とも ALARM→OK）。dataapply の直後（CW Logs `polyarchy/dataapply` に同時刻の適用がある）なら正常。
それ以外の時刻の ALARM は §1 の手順へ。退避段（①・サービス無停止）で失敗したときも dataapply=0 を送る＝同じアラームで気づく。

### 切り戻し後（アラーム「dataapply-rollback」を受けたら）
1. ダッシュボード②＝「ROLLED_BACK … ⏪ 旧版で稼働中 … 切り戻し後 smoke PASS」を確認（PASS なら利用者影響なし）。
   切り戻し後 smoke が FAILED なら §1 health の手順（旧版でも壊れている＝§3 の S3 復元へ）。
2. 原因は CW Logs `polyarchy/dataapply`（smoke の出力・qdrant 起動ログ）で読む＝箱に入らずに済む。
3. ローカルで直して `release.sh` を再実行＝マニフェストが新しくなれば自動で再適用される（箱操作は不要）。
   コードの修正だけでも同じ（tar が同梱・箱が自動展開）。
4. **演習**（移譲基準 (2)(3) の実績づくり・staging で）＝`sudo touch /etc/polyarchy/apply_drill_fail` →
   `sudo polyarchy-data-apply --force` → ダッシュボードで ROLLED_BACK を確認 → `sudo rm /etc/polyarchy/apply_drill_fail` →
   `sudo polyarchy-data-apply --force` で APPLIED に戻す。（`--force` は同じマニフェストを再適用する非常口＝通常運用では使わない。
   ★手動実行の出力を CW Logs `polyarchy/dataapply` に残すには `sudo systemd-cat -t polyarchy-dataapply polyarchy-data-apply --force` と journald を経由させる＝
   SSM Run Command から直接呼ぶと出力は SSM 側にしか残らない。初回演習 2026-09-03＝FAIL 判定から復帰まで約 2.5 分・旧版 smoke PASS）

**prod の認証（権限 2 段・2026-09-03 設計）**＝日常のデータ更新に要るのは
**S3 `data/`・`release/`・`code/` への Put/Delete＋`ops/` の読み取り＋ListBucket だけ**（定義＝[deploy/iam-data-operator-policy.json](iam-data-operator-policy.json)・
箱・EC2・SSM に触れない・捕捉ログは読めない。upload は CloudTrail データイベントに実行者 ID 付きで記録）。SSM send-command 等の管理系権限は障害対応時のみ。
release.sh の前提（手元）＝conda env `polyarchy`・Docker の `qdrant-dev`・aws/terraform/git・`deploy/env/<env>.env`・`recommendations/.env`（Anthropic＝分野タグ判定／OpenAI）・
`stats/.env`（e-Stat appId）・`deploy/terraform/terraform.tfstate`（バケット名の取得元＝無ければ環境変数 `BUCKET` を指定）。

- ★コード変更を伴うリリースも release.sh で足りる（upload が tar を同梱・**箱の apply が tar を再展開**＝2026-09-03〜。
  「bootstrap はコードを更新しない」地雷は apply 側で解消。手動の tar 再展開は非常時のみ＝deploy/README）。
- 取込後の団体別鮮度（`last_ingested`）は list_orgs／検索応答の freshness で利用者にも見える＝反映確認に使える。

### 燃料の週次トリアージ（運用者＝草稿生成・PdM＝採否・2026-09-03 1 コマンド化）

```bash
bash deploy/scripts/triage.sh staging        # S3 の燃料＋ローカル捕捉分 → ops/triage/<日付>/ に両サービスの草稿（読み取りのみ）
```
- 運用者はここまで（評価セットには一切書かない）。出力＝stats の find_quality 草稿（todo/pass）と recommendations の
  Phase 12 候補（zero/low/frequent・各行に scaffold コマンド付き）。
- PdM＝採否（コマンドの実行は開発者でよい）：stats は `python -m stats.ops.quality_candidates --accept <草稿> --ids <id,id>`（追記→ゲート）、
  recommendations は各草稿の `next`（scaffold → expected_keywords を本文 verbatim で埋める → append）。
  検索型を昇華したらアンカー記載（README「品質の担保」）を同一コミットで更新。
- 評価セットに触れた場合はアンカー数値の更新規律に従う（ルート README「品質の担保」が正典）。

## 6. アラーム対応の心得

- 誤報でメールを無視し始めるのが最悪（運用設計 §1.2）。鳴りすぎるなら閾値を上げる変更を terraform（observability.tf）で行う。
- SNS 購読はメールの Confirm リンクを踏むまで有効にならない（apply 直後に届く）。
- **運用ダッシュボード＝S3 `ops/dashboard/index.html`（毎時）**：稼働・バージョン・鮮度・直近利用の1枚。まずこれを見る。
- 週次レポートは S3 `ops/report/usage_report.html`（数字のみ・検索語なし）。手元閲覧＝`aws s3 cp` で取得。
  Access 内配信（URL で見る）は外部提示が必要になった時に stats ホストへ静的パスを追加する（運用設計 §4.2 の配信手段決定・2026-08-28）。

## 7. 依存の更新（ロック再生成 → PR → ゲート・2026-09-04 B17 (1)）

箱に入る Python 依存は `deploy/requirements/lock-<extras>.txt`（版＋sha256）だけ＝bootstrap ③が `pip install --require-hashes -r` で導入し、
本体は `-e . --no-deps`。**pyproject を変えても、ロックを作り直さない限り箱は変わらない**（ロックに無い版は入らない・ハッシュ不一致は落ちる）。
ロックは箱と同じ Linux x86_64／py3.12 で解決する必要があるため docker（`python:3.12-slim` ダイジェスト固定）で生成する。

```bash
# ① 再生成（既存の版は保つ。全更新は --upgrade・1 つだけは --upgrade-package <name>）。docker が要る
bash deploy/scripts/lock_deps.sh [--upgrade]
# ② ローカルで同じロックから env を作り直す。★Mac（arm64）には torch +cpu が無い＝torch だけ PyPI の同版（ハッシュ付き）に
#    置き換えた変種を派生して入れる（他のピンとハッシュは箱のロックと同一）
conda create -y -n polyarchy-lock -c conda-forge --override-channels python=3.12
python deploy/scripts/lock_mac_variant.py                     # → /tmp/lock-mac.txt
P=$HOME/miniconda3/envs/polyarchy-lock/bin            # 自分の conda の envs パスに読み替え
$P/pip install --require-hashes -r /tmp/lock-mac.txt
$P/pip install --no-deps --no-build-isolation -e .
# ③ ゲート 9 本＋配布＝release.sh を「PATH の python をロック env に向けて」回す。数値が README「品質の担保」と一致することが採否の基準。
#    依存を上げて数値が動いたら**上げずに現行版で固定**（目的は固定であって更新ではない）
PATH=$P:$PATH bash deploy/scripts/release.sh staging
```
- 変更は PR（ロック 2 本＋pyproject）でレビュー＝ゲート全 PASS が採否（CI 化 §2.5 で自動化）。箱は release.sh → 自動適用でロックどおりに入れ替わる。
- 切り戻し（apply の自動 fail-back）も前回 tar のロックで再解決する。ロック導入前（2026-09-04 以前）の tar へ戻る場合だけ下限指定の解決になる。
- 出所の固定は Qdrant バイナリ（bootstrap ⑥b の sha256）と同じ思想。モデルの重み（HF）は未固定＝残タスク B17 (3)。
