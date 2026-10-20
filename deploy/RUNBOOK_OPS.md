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
| 毎分 timer `polyarchy-health` | 各サービスの localhost の `/healthz` → CW metric `polyarchy/health`＋disk＋空きメモリ＋常駐のユニットの自動再起動の回数 |
| 5 分ごと timer `polyarchy-edgeprobe` | 箱自身が公開 URL を資格情報なしで叩く（`/healthz`・守るパスの 401・ディスカバリの照合・IdP の公開鍵）→ CW metric `polyarchy/edge`・`idp_reachable`。結果はダッシュボード①「外からの確認」 |
| 5 分ごと timer `polyarchy-topuser` | 全サービスの捕捉ログを利用者ごとに数え、直近 1 時間の最多の 1 人の呼び出しの数 → CW metric `polyarchy/top_user_calls_1h`（数だけ・利用者のハッシュは送らない）。記録はメトリクスが正（同じ数の行は箱の `/var/log/polyarchy/polyarchy-topuser.log` だけ＝CW Logs には送らない・edgeprobe と同じ扱い） |
| CloudWatch Alarm → SNS → メール | health 3分連続 0/missing・EC2 StatusCheckFailed・disk>80%・空きメモリ<10% が 15 分・自動再起動・定期実行の失敗・外からの到達の確認が 2 回続けて失敗（edge・idp）・1 人が直近 1 時間に閾値以上の呼び出し（top-user）・利用者ごとの上限に届いた（limited）・5xx>5/5分・トークンの検証の失敗≧10/5分・**dataapply-rollback**（自動適用の切り戻し＝§5）＝一覧と意味は [運用設計](../docs/運用設計.md) §1.2 |
| CW Logs（サービスごとのロググループ＝`polyarchy/mcp` ほか） | httplog（時刻・パス・status・ms・user_hash・cf-ray）と認証の警告の行。保持 30 日 |
| CW Logs `polyarchy/dataapply` | 自動適用の全出力（退避→反映→smoke→判定/切り戻し）＝データ更新の監査線（監査ガイド §4b）。保持 30 日 |
| 箱内のログファイル | `/var/log/polyarchy/*.log`（rsyslog）は logrotate 日次×30 世代（30 日）・journal は `MaxRetentionSec=30day`（2026-10-09 の版の配布から＝bootstrap の変更はその配布で効く。それまでは週次×4 で約 5 週・journal は期限なし）。捕捉ログ本体（query_log）は 30 日 prune＝保持の正典は deploy/README「捕捉ログの保持」 |
| Cloudflare Tunnel Health 通知 | ダッシュボードで ON にする（無料・トンネル断） |

## 1. 症状 → 確認 → 復旧

### アラーム「health-mcp / health-stats / health-companies」
```bash
systemctl status polyarchy-mcp polyarchy-stats polyarchy-companies qdrant
curl -s localhost:8765/healthz; curl -s localhost:8766/healthz; curl -s localhost:8767/healthz
journalctl -u polyarchy-mcp -n 50 --no-pager     # or polyarchy-stats / polyarchy-companies
```
- unit が落ちている → `sudo systemctl restart polyarchy-mcp`（Restart=always なのに落ち続ける場合はログの Traceback を読む）
- recommendations の healthz が `ok:false`／chunks=0 → qdrant を疑う：`systemctl status qdrant`・`curl -s localhost:6333/collections`。deliberations の `points=0`・`error` → 専用の qdrant（:6340・unit は `qdrant-deliberations`）を同じ形で（2026-10-17 の版から Qdrant へ live に count を取る＝それより前の版は起動時の点数で、Qdrant が落ちても ok のままだった）
- `checks.error` が `timeout(3s)` → 検査（Qdrant の count）が 3 秒で終わらない＝Qdrant が固まっている（サービス本体は応えている＝待受は止まらない・2026-10-17）。`systemctl restart qdrant`（審議会DB は `qdrant-deliberations`）の前に `journalctl -u qdrant -n 50`
- 箱ごと無応答（missing）→ EC2 再起動：`aws ec2 reboot-instances --instance-ids <id>`（systemd が全部 enable 済＝自動復帰）
- unit は active のまま・いま叩くと `ok:true` なのに 0 の点が続いた → **重い要求の最中**を疑う。ツールの実行は別のスレッドで動く（2026-10-14 の版から＝ツールの実行中も `/healthz` は返る・政策主張DB と審議会DB は同時に unit の `MCP_TOOL_CONCURRENCY` 本まで・ほかは 1 本ずつ順に動く）。それより前の版と、切っているサービス（運営者の箱の SSM `tool_offload_<サービス>`＝`0`・2026-10-15〜＝企業情報DB は掲載の審査のあいだ）では、ツールの実行中は `/healthz` も待つ＝箱の確認は 5 秒で打ち切って 0 を送る（2026-10-12 staging＝運営者 1 人の検索の重なりで 1 分の点が 2 つ続けて 0）。同じ時刻のアクセスログに数秒〜数十秒の POST が並んでいれば、落ちたのではなく混んでいた。再起動はしない（処理中の要求を落とすだけ）。誰の量かは捕捉ログの側で読む（[残タスク](../docs/残タスク.md) B44）。★切っているサービスでは、アクセスログの所要時間に、処理の最中に届いた要求が受け取られるまでの待ちが写らない（手元の最小サーバの実測＝利用側で 8.5 秒待った要求の行が 4 ms）。別のスレッドで動くサービスでは、並んで待った時間も所要時間に入る。

### アラーム「5xx」
```bash
journalctl -u polyarchy-mcp --since "-30min" --no-pager | grep -E " 50[0-9] "
```
アプリ例外なら Traceback が直前に出ている。データ破損疑い（qdrant）なら §3 の復元。

### アラーム「auth-<サービス>」（トークンの検証の失敗の急増・旧「401 急増」）
- **数えているもの**＝「トークンを出したのに通らない」要求（警告の行「IdP トークン検証失敗」の目印 `auth_reject=invalid_token`）が 5 分で 10 件以上。トークン無しの 401（警告「Bearer 欠如」）は新しい接続の最初の 1 回に必ず出る＝数えない（2026-10-12・B35＝それまではアクセスログの 401 を数えていて、認証の拒否では鳴らなかった）。
- **鳴ったら**＝全員がつながらない疑い。原因は 2 つ＝①設定壊れ（aud・発行者の食い違い＝下の三点一致）②IdP の障害（公開鍵が取れない＝稼働中は覚えている鍵で検証が続くので、起きるのはサービスの再起動の直後〔鍵を 1 つも持たない〕か IdP が鍵を入れ替えた後だけ。取り直しの失敗は別の警告「IdP の公開鍵の取り直しに失敗」で出て、それだけでは auth は鳴らない・2026-10-17 の版から）。行を読む：`journalctl -u polyarchy-mcp --since "-30min" --no-pager | grep "IdP トークン検証失敗" | tail`（サービスごとのユニット名に読み替える・箱に入らないなら CW Logs のロググループで同じ文字列を探す）。
- **鳴らなくても起きている場合**＝利用が少ない時間帯は件数が届かない。利用者から「つながらない」と来たら、鳴るのを待たずに同じ行を数える。
- **トークン無しの 401 は正常系**（新しい接続の最初の 1 回・未認証の探り）＝アラームは数えない。探りの量が気になるときは警告「Bearer 欠如」の件数と、レート制限が効いているか（§1「公開 URL」の `cloudflare-guard.sh status`）を見る。でたらめなトークンを付けた探りは数に入る＝行の末尾の理由（署名・発行者）で設定壊れと見分ける。
- 案 B の照合（三点一致）：SSM `auth_issuer`／`auth_aud_<svc>` と WorkOS の Resource indicator と実 URL（ホスト＋秘密パス）が一致しているか。壊れていたら SSM を直して箱で `bootstrap.sh` 再走行 → restart。issuer 変更（テナント差し替え）時は利用者は再ログインになる。
- （旧・案 A の照合＝`access-oauth.sh status` は Access 併用時のみ・現行は未使用）。
- **`POST <パス>` の 400 は、SDK 2.x（2026-10-20〜・残タスク B36）の後は出ないのが正常**。それまでは、新しいプロトコル版（2026-07-28）のクライアントが接続の最初に送る発見の要求（`server/discover`）を、箱の MCP SDK（1.x）が「未対応のプロトコル版」として断り、クライアントは従来の `initialize` に落ちて続行していた（接続ごとに 400 が 1 本・利用者の画面に失敗は出ない）。2.x はこの要求に 200 で応じ、従来の`initialize` にも応じる。400 を数えるアラームは無い。★2.x の配布の後に 400 が出る・同じ接続で 400 が続く・ツール呼び出しが 400 になる＝別の事象＝応答の本文を見る（版の名乗りとヘッダの食い違いなど）。同じく、**ツールの行（`TOOL`）の無い長い `POST`**（数分）は、2.x の新しい版の「一覧の変化の知らせを待つ口」（`subscriptions/listen`）＝切ってある（B36・2026-10-20〜）＝出たら別の事象。

### アラーム「dataapply-rollback」（自動適用が切り戻された・または退避段で中止した）
- **可用性は落ちていない**（旧版で稼働中）。dataapply=0 は 2 つの場合に出る＝(a) 反映後の検証（smoke・稼働中の /healthz）が FAIL で
  自動切り戻し（ダッシュボード②が ROLLED_BACK）(b) 止める前の退避段で中止（ディスク・S3 権限＝マークは書かれず前回のマークのまま・15 分後に再試行）。
  見分けは CW Logs `polyarchy/dataapply` の「切り戻し完了」か「①退避段で失敗」の行。(a) は §5「切り戻し後」の手順へ・(b) は原因（ディスク・権限）を直す。急がない。
- 例外＝ダッシュボード②の「切り戻し後 smoke」が FAILED のとき（旧版でも壊れている）→ 上の health の手順・§3 の S3 復元。

### アラーム「mem」（箱の空きメモリが 15 分続けて 10% 未満）
- まずダッシュボード①のメモリの表（サービスごとの 現在・うちファイルキャッシュ・起動からの最大）で、どのユニットが増えているかを見る。プロセス自身の分は 現在 − ファイルキャッシュ が目安。
- 箱で見るなら：`free -m; systemctl status polyarchy-mcp polyarchy-stats qdrant --no-pager | grep -E "●|Memory"`
- 1 つのユニットが増え続けている → そのユニットを `sudo systemctl restart`（数秒の切れ目）。全体に足りない → インスタンスの大きさを上げる判断（terraform の `instance_type`）。
- このアラームと「restarts」が続けて鳴ったら OOM で落ちている：`journalctl -k --since "-1h" --no-pager | grep -i "out of memory"`

### アラーム「restarts」（常駐のユニットが落ちて自動で上げ直された）
- 可用性は数秒で戻っている（`Restart=always`）。落ちた瞬間の要求は失敗している＝原因を見る。どのユニットかはダッシュボード①の「自動再起動」の列（0 でないもの）。
- 箱で見るなら：`systemctl show 'polyarchy-*.service' 'qdrant*.service' cloudflared.service -p Id,NRestarts | paste - - -`／落ちた理由＝`journalctl -u <ユニット> --since "-1h" --no-pager | grep -B5 "Main process exited"`（Traceback か、OOM なら `journalctl -k | grep -i "out of memory"`）
- 配布（自動適用）の停止・起動は数えない＝配布の時刻に鳴ったら、反映の途中でユニットが自分で落ちている（`polyarchy/dataapply` のログと合わせて読む）。

### アラーム「unit-failed」（定期実行のユニットが失敗した）
- どのユニットかはダッシュボード①「失敗したユニット」（次の実行が成功すれば消える）。箱で見るなら：`systemctl --failed --no-pager; tail /var/log/polyarchy/polyarchy-unit-failed.log`
- 失敗の中身：`journalctl -u <ユニット> -n 50 --no-pager`。直したら手で 1 回流して確かめる：`sudo systemctl start <ユニット>.service`
- ★**`polyarchy-logprune` の失敗は急ぐ**＝捕捉ログの 30 日の削除が止まっている（プライバシーポリシーの約束）。箱の削除が止まっても S3 側はライフサイクルが消す＝外れるのは箱の原本。
- `polyarchy-fuelsync`＝S3 への保護コピーが止まっている（箱の原本は無事）・`polyarchy-updatecheck`／`polyarchy-dashboard`＝ダッシュボードが古いまま（冒頭の生成時刻で分かる）・`polyarchy-usagereport`＝週次の集計が 1 回抜ける（手で流せば埋まる）。
- 知らせの対象外＝`polyarchy-health`（止まれば health が missing で鳴る）・`polyarchy-dataapply`（切り戻し・中止は dataapply-rollback で鳴る）。

### アラーム「disk > 80%」
```bash
df -h /; du -xhd1 /opt/polyarchy | sort -h | tail
```
- 大抵は捕捉ログか /var/log。捕捉は 30 日 prune（`polyarchy-logprune.timer`）が動いているか確認：`systemctl list-timers | grep logprune`
- `/opt/polyarchy/rollback/`（約 2.5 GB）と `/opt/polyarchy/releases/` は**削除しない**（自動切り戻しの原資・§5）。PDF は追記専用で増える（2 GB〜）。

### アラーム「edge-<サービス>」（外からの到達の確認が 2 回続けて失敗）
箱の中は健全でも利用者がつなげていない疑い。まずダッシュボード①「外からの確認」の「失敗した確認」を読む（パスは出ない）：

| 失敗した確認 | 意味 | 見るところ |
|---|---|---|
| `healthz(届かない)`・`healthz(5xx)` | 公開ホストが箱に届かない・サービスが異常 | 下の「公開 URL が落ちている」（cloudflared・DNS・トンネル）。health も鳴っていればサービス側 |
| `mcp(403)` | エッジの規則が守るパスを止めている | `bash deploy/scripts/cloudflare-guard.sh status`／`test`（規則のパスと箱のパスの食い違い） |
| `mcp(200＝認証なしで通る)` | **認証が外れている**（急ぐ） | そのサービスの env（`/etc/polyarchy/<サービス>.env`）と起動ログの「IdP Bearer 検証=有効」の行 |
| `mcp(WWW-Authenticate)` | 401 は返るが、クライアントが IdP を見つける手がかりが無い・別のホストを指している | 同上（resource の値） |
| `prm(403)`・`prm(届かない)` | ディスカバリがエッジで止まっている＝新しく接続を足せない | ガードの規則が `/.well-known/` を除外しているか（`cloudflare-guard.sh status`） |
| `prm(resource)` | そのホストが別のサービスに届いている・resource の設定の誤り | cloudflared の ingress（ホスト → ポート）と env |
| `prm(issuer)` | 発行者の設定の食い違い | SSM `auth_issuer` |
| `aud≠resource` | パスだけ替えて aud を替え忘れた（三点一致の崩れ＝トークンが全部通らない） | SSM `auth_aud_<svc>` と `<svc>_http_path`・IdP の Resource indicator（RUNBOOK §4） |

- 手元からも同じ確認を流せる：`python -m polyarchy_common.edge_probe --resource-url https://<ホスト><パス> --issuer <発行者>`（資格情報なし・読み取りだけ）。手元では通るのに箱からは失敗する＝箱の外向きの経路の問題（利用者には影響していない）。
- **配布の時刻の 1 回の NG は、まず時刻を比べる**：確認は毎時 04 分から 5 分ごと＝自動適用の始まり（15 分の刻み）の 4 分後で、ふつうはサービスの起動（始まりの約 2 分後）より後に走る（2026-10-13〜。それまでは 2 分後で、起動の間際に当たって APPLIED の直後のダッシュボードに NG が載った）。停止が長い配布では 1 回 NG になり得る＝表の下の「最終確認」の時刻が、ユニットの表の「起動」より前なら止まっていた間の結果＝次の実行（5 分後）を見る。
- 配布の停止・起動の 1 回では鳴らない（2 回続けて＝約 10 分）。配布の時刻に鳴ったら、適用が 5 分を超えて止まっている（`polyarchy/dataapply` のログ）。
- この確認は 5 分ごとに守るパスへトークン無しの要求を 1 本送る＝警告「Bearer 欠如」がサービスごとに 1 日 288 件出る（定型・アラームは数えない）。探りの量を警告の件数で読むときは、この分を引く。

### アラーム「busy-<サービス>」（捌ききれない呼び出しに「混んでいる」と返した・2026-10-14）
**障害ではない。箱が足りていない合図。** 待受はツールを同時に unit の `MCP_TOOL_CONCURRENCY` 本まで（政策主張DB と審議会DB は 3・ほかは 1）動かし、実行中の呼び出しのほかに並んだ数が上限（既定 20）を超えた呼び出しと、
上限の秒数（既定 45）を超えて待った呼び出しを、黙って待たせずに、エラーの結果で「混んでいる・実行していない」と返す（「該当なし」とは形が違う）。
- 何回・どちらの理由か＝ログの警告 `混雑で断った [tool_busy=queue]`（並びすぎ）／`[tool_busy=wait]`（待ちすぎ）。メトリクスは `tool_busy_<サービス>`。
- 1 回きりで戻った → 短い山（告知の直後など）。何もしない。**何度も鳴る・戻らない → 機種を上げる判断**（[性能と同時実行の開発計画](../docs/性能と同時実行_開発計画.md)）。
  1 人の連打なら、量の側（[残タスク](../docs/残タスク.md) B44）。再起動はしない（並んでいる呼び出しを落とすだけ）。
- 上限は unit の `MCP_TOOL_QUEUE_MAX`・`MCP_TOOL_WAIT_SECONDS`、同時の数は `MCP_TOOL_CONCURRENCY`（どれも配布で変える・既定は暫定）。起動の行に「同時 N 本まで・計算のスレッド M」が出る（同時の数を上げても、箱のコアが足りなければ 1 本の実行が延びる＝`run` で見る）。切る口は SSM `tool_offload_<サービス>`＝`0`（bootstrap がそのサービスの env に `MCP_TOOL_OFFLOAD=0` を書く＝次の配布の適用から効く・unit には書かない＝複製に運ばない。運営者の箱は企業情報DB を掲載の審査のあいだ切っている）＝切ったサービスは起動の行が「イベントループの上」。
- **混み具合の読み方**（鳴っていないときも）＝実行した呼び出しは 1 回ごとに `TOOL <名前> wait_ms <待った時間> run_ms <実行の時間> queue <着いたときに並んでいた数>` を出す。
  CW Logs Insights（ロググループ `polyarchy/<svc>`）で 1 時間ごとに集計する：
```
fields @timestamp, @message
| parse @message /TOOL (?<tool>\S+) wait_ms (?<wait>\d+) run_ms (?<run>\d+) queue (?<queue>\d+)/
| filter ispresent(tool)
| stats count() as calls, pct(wait, 50) as wait_p50, pct(wait, 90) as wait_p90, max(wait) as wait_max, pct(run, 50) as run_p50, pct(run, 90) as run_p90, max(run) as run_max, max(queue) as queue_max by bin(1h)
```
  読み方＝`wait` が 0 に近いあいだは足りている。`wait_p90` が数秒〜十数秒に伸びてきたら混み始め、`wait_max` が 30 秒を超える時間帯が続けば、
  箱が足りていない。`run` はその箱での 1 回の重さ＝配布の前後で比べる（ツールごとに見るなら `by tool`）。同時の数を上げた後に `run` だけが
  伸びるなら、待ちが実行の延びに置き換わっただけ（コアが足りない）。
  **合格の線（計画 §2＝3 人の形の箱の分が 1 人の形の 2.5 倍以内）の測り方**＝3 人が同時に問を出した時間帯と、同じ問を 1 人で出した時間帯の
  それぞれで、ツールごとに 1 回あたりの平均を取り、`3 人の形の (wait+run) の平均 ÷ 1 人の形の run の平均` のツールのうち大きいほうを見る
  （1 つの問で利用側が呼ぶ回数は毎回違う＝合計ではなく 1 回あたりで比べる）：
```
fields @timestamp, @message
| parse @message /TOOL (?<tool>\S+) wait_ms (?<wait>\d+) run_ms (?<run>\d+) queue (?<queue>\d+)/
| filter ispresent(tool)
| fields wait + run as load
| stats count() as calls, avg(load) as load_avg, avg(run) as run_avg, max(wait) as wait_max by tool
```
  ★分母（1 人の形の団体横断の `run`）は 1 本ごとに 1 割ほど揺れる（2026-10-14 の 5 本＝7.6〜9.3 秒）＝1〜2 本では線の上下が入れ替わる。
  **1 人の形の団体横断を 8 本以上**（3 つの問を 1 人で 3 回ずつ等）集めてから割る。3 人の形も重なり方で揺れる＝2〜3 回出して並べる。
  ★同時に動いた呼び出しの `run` の合計は、重なった時間を本数ぶん数える＝CPU を使った量ではない（直列のときの合計と比べない）。
  捌いた速さは、呼び出しの続いた時間（最初の着から最後の終わりまで）で比べる。

### アラーム「limited-<サービス>」・利用者ごとの上限（2026-10-16・B44 第 2 便）
**障害ではない。** 1 人（公開の口＝ログインしたメール・中継の口＝会員の印）が、そのサービスで直近 1 時間に呼べるツールの数（SSM `user_calls_per_hour`）に届き、
超えた呼び出しを実行せずに「上限・あと何分」と返した（「該当なし」とも「混んでいる」とも形が違う）。ログの警告の目印は `tool_limited=hour`・メトリクスは `tool_limited_<サービス>`。
- **既定は上限なし**（SSM が無い・`0`＝bootstrap が env に書かない＝鳴らない）。入れ方＝`aws ssm put-parameter --overwrite --type String --name /polyarchy/<env>/user_calls_per_hour --value <数>`
  → 次の配布の適用から効く（4 サービスに同じ値・サービスの起動の行に「利用者ごとの上限=1 時間に N 回」）。外すときは値を `0` に（パラメータは消さない）。
- **値の決め方**：数はサービスごと（4 サービスの合計ではない）。アラーム top-user のメトリクス（全サービスの合計）で、ふつうの利用者の 1 時間の最大を見て、その数倍に置く
  （重い問 1 つで 5〜9 本＝[§E-B45](../docs/残タスク.md#e-b45)）。数はプロセスの中だけ＝サービスを起動し直すと 0 から。
- 鳴ったら：誰か＝同じ時刻のアクセスログの `user=`（または「top-user」の `--show`）。連打なら様子を見るか止める（「特定の利用者を止める」・中継の会員は bot の側で）。
  ふつうの使い方で届いているなら、上限を上げる。

### 機種を上げる・戻す（2026-10-15・計画の段 4）
上げるのは「混んでいる」が何度も出る・上の集計で `wait` の大きい時間帯が続く・山（導入の告知など）が前もって分かっているとき。判断は運営者。
効くのは呼び出しが重なったときだけ（1 人の検索はほとんど速くならない＝[性能と同時実行の開発計画](../docs/性能と同時実行_開発計画.md) §4）。

- **候補は一段ずつ**＝8 vCPU の `c7i.2xlarge`（16 GB）→ 16 vCPU の `c7i.4xlarge`（32 GB）。単価は計画 §4。**x86 の機種だけ**（Graviton＝世代の数字の後ろに `g` の機種は arm64＝AMI・ロック・Qdrant が起動しない＝`variables.tf` が plan の段で止める）。
- **前もって（アカウントごとに 1 度・導入の前に）**
  1. vCPU の上限＝Service Quotas の「Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances」（`L-1216C47A`）が、上げたい機種の vCPU 以上か。作ったばかりのアカウントは低いことがあり、引き上げの申請は日数がかかる。
     `aws service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A --profile <profile> --query Quota.Value`
  2. 箱のサブネットのアベイラビリティゾーンで、その機種が使えるか。
     `aws ec2 describe-instance-type-offerings --location-type availability-zone --filters Name=instance-type,Values=c7i.2xlarge --profile <profile>`
  3. 初回は staging で、上げて戻すのを 1 度通す（停止の長さと、下の起動の後の確認を記録する）。
- **上げる**
  1. 配布・自動適用が動いていない時間に（`release.sh` を流さない・直前の自動適用が終わっている）。
  2. `deploy/env/<env>.env` の `INSTANCE_TYPE=` に機種を書く（例 `c7i.2xlarge`・値の行の書き換えは運営者）。**env ファイルが正**＝シェルの変数で渡す形（`INSTANCE_TYPE=… deploy.sh …`）は、上げている間に変数を付けずに `deploy.sh` を流すと、黙って戻す差分が出る（plan では見える）。env ファイルに `INSTANCE_TYPE=` の行があると、シェルの変数は効かない（ファイルの空の値が勝つ）。
  3. `deploy.sh <env> plan` → 差分が **`aws_instance.app` の `instance_type` の 1 か所だけ・update in-place**であることを読む。replace（作り直し）や、ほかの差分が出たら止める。
  4. `deploy.sh <env> apply-plan` → 箱は停止 → 機種の変更 → 起動。**その間は全サービスが止まる**＝staging の実測は上げても戻しても約 2 分（terraform 30〜45 秒＋起動からモデルの読み込みまで約 1 分半・2026-10-15）。health（3 分続けて）・edge（2 回続けて）の線に届かず、**アラームは鳴らないのがふつう**。長引いて鳴っても、戻れば障害ではない。
     止まらないもの＝ルートのディスクとデータ・インスタンス ID（アラームはそのまま）。変わるもの＝パブリック IP（入口はトンネル＝箱から外へつなぐ形＝影響なし）。
  5. 起動の後に確かめる：ダッシュボード①（全サービスが起動・メモリ・外からの確認が ok・毎時の生成を待つ）・起動の行の「計算のスレッド M」が新しい機種の物理コアの数（8 vCPU なら 4）・各クライアントからツールを 1 回ずつ呼べる。起動し直した後の最初の呼び出しは遅い（索引とモデルが温まっていない・2 サービスに同時に来ると CPU を取り合う＝10 秒前後〜）＝2 本目から見る。
- **戻す**＝env ファイルの `INSTANCE_TYPE=` を空に（既定の `t3.xlarge`）して、同じ plan → apply-plan（止まる長さは上げたときと同じ）。戻す目安は、山が過ぎて `wait` が 0 近くに戻ったとき。上げたままにすると費用が続く。
- 変えないもの＝同時に動かす数（unit の `MCP_TOOL_CONCURRENCY`）。変えるなら配布で（機種と同時に変えない＝どちらが効いたか分からなくなる）。

### アラーム「top-user」（1 人が直近 1 時間に閾値以上ツールを呼んだ・2026-10-15）
**障害ではない。** 箱が 5 分ごとに全サービスの捕捉ログを利用者ごとに数え、最多の 1 人の数（`top_user_calls_1h`）が閾値（既定 200・
env の `TOP_USER_CALLS_THRESHOLD`→`deploy.sh plan`→`apply-plan`）以上になった。数える単位はツールの呼び出し（捕捉ログの行＝POST の本数ではない）。
- まず、ふつうの重い利用か、機械的な連打かを分ける。重い問 1 つで 5〜9 本＝200 本は 1 時間に重い問 20〜40 個。
  1 つのクライアントが多数の人の問を 1 つのアカウントで中継している形（チャットの bot 等）は、全員が 1 人に見える＝その経路は別に扱う（[残タスク](../docs/残タスク.md) B44）。
- **誰か**（運営者が手元で・利用者のハッシュは箱からは出ない）：捕捉ログは毎時 S3 に写る（最長 1 時間の遅れ）＝アラームの時刻を窓の終わりにして数える。
```
T=$(mktemp -d)                                                              # 手元の一時フォルダ（確かめ終わったら消す）
for p in query_log stats/query_log companies/query_log deliberations/query_log; do
  aws s3 cp "s3://<BUCKET>/data/$p/queries.jsonl" "$T/${p%%/*}.jsonl" --profile <profile> || true   # 無いサービスは飛ばす
done
python -m polyarchy_common.top_user $(for f in "$T"/*.jsonl; do echo --log "$f"; done) --at 2026-10-15T21:00:00+09:00 --show 5   # 窓の終わり（JST）→ 多い順に「ハッシュ 回数」
printf "メール: "; read -r EMAIL                                             # IdP の利用者一覧の候補のメール（履歴に残さない）
python -m polyarchy_common.top_user --hash-of "$EMAIL"; unset EMAIL         # → 上のハッシュと同じか
rm -rf "$T"
```
  **`--show` には `--at` が要る**（無いと止まる）＝捕捉ログの `ts` は箱の時計＝**UTC**（時差の表記なし）。`+09:00` を付ければ JST のまま書ける（付けなければ UTC として読む）。日をまたいで確かめるときも、照合したい呼び出しの時刻を窓の終わり（窓は 1 時間）に。`--show` を付けると最後の `top_user N` の行は出ない。運営者自身のハッシュは週次レポートの除外に使うもの（SSM `usage_operator_hashes`）と同じ。
  写しは確かめ終わったら消す（捕捉ログの保持の約束＝30 日の外に手元の写しを残さない）。
- 止めるか決めたら → 下の「特定の利用者を止める」。決めるのは運営者（利用規約の停止の条項）。止めない（正当な重い利用）なら何もしない＝閾値を上げるかを見直す。

### 特定の利用者を止める（2026-10-15）
**止めるのは IdP の画面だけ**（決定・残タスク §E-B44）＝箱に利用者の拒否リストは持たない。
1. IdP の管理画面の利用者一覧で、そのメールの利用者を止める（削除または無効化）。以後、新しいサインインとトークンの更新は通らない。
2. **発行済みのアクセストークンは寿命のあいだ通る**（寿命は IdP の設定・未確認＝§E-B44 の未了）。急ぐときは、その間の呼び出しを捕捉ログで見続ける。
3. 別のメールでの登録し直しは止まらない（箱の拒否リストでも同じ）＝続くなら、登録の入口の設定（IdP の側）を見直す。
4. 何をいつ止めたかは社内ノートに残す（メールは git に書かない）。

### 中継の口（bot 専用・既定は閉・2026-10-16）
開ける・閉じる手順と bot の側の約束は [PROD_MIGRATION](PROD_MIGRATION.md) §2.6（運営者の箱は閉じたまま）。開いているかは、サービスの起動の行「中継の口=有効」か、そのサービスの env の `MCP_RELAY_PORT`。
- 断った要求＝ログの警告の目印 `relay_reject=source`（送り元が許可の範囲の外＝bot の IP が変わった・範囲の書き損じ）／`relay_reject=member`（会員の印が無い・形が違う＝bot の側の不具合）。アラームは無い。
- 中継の呼び出しは、捕捉ログの `client` が `relay`・利用者は会員の印。top-user が鳴ったら、手元の `--show` で多い順の印を見る（誰かの特定は bot の側の対応表で＝箱は会員の ID を知らない）。会員を止めるのは bot の側（IdP では止められない＝bot はログインしていない）。

### アラーム「idp」（IdP のディスカバリか公開鍵が取れない）
- 稼働中のサービスは公開鍵を 24 時間覚え、取り直せないあいだは覚えている鍵で検証を続ける（2026-10-17 の版から。それより前の版は 5 分で忘れ、取り直せないと全員が invalid token になった）＝すぐには落ちない。落ちるのはサービスの再起動の後（鍵を 1 つも持たない）と IdP が鍵を入れ替えた後（auth のアラームが続けて鳴る）＝**鳴っている間は配布と再起動を控える**。取り直しの失敗はサービスのログに警告「IdP の公開鍵の取り直しに失敗」で並ぶ（60 秒に 1 回まで）。
- IdP の側の障害か（IdP の稼働状況のページ）、発行者の設定の誤りか（SSM `auth_issuer`＝直近に替えたか）を見分ける。ダッシュボード①の IdP の行＝`discovery(…)` は発行者の URL が引けない・`jwks(…)` は公開鍵が取れない。

### 公開 URL が落ちている（origin は健全）
```bash
systemctl status cloudflared; journalctl -u cloudflared -n 30 --no-pager   # Registered tunnel connection があるか
curl -sI https://recommendations.<domain>/healthz | head -1   # staging は recommendations.polyarchy.net
```
- cloudflared 再起動 → ダメなら Cloudflare 側（DNS・トンネル・WAF）を疑う。WAF ルールの現状＝`bash deploy/scripts/cloudflare-guard.sh status`。
- ★ingress を変えた直後（サービスを足した・外した）に**そのホストだけ** 404 なら cloudflared の未再読込を疑う＝cloudflared は起動時にしか config.yml を読まない。自動適用は config が変わったときだけ再起動する（2026-09-22〜）が、bootstrap の手動再走行では再起動されない。
- ★**ガードの `apply`・`remove` は運営者がターミナルで実行する**（共有の入口の規則を変える＝AI セッションには任せない。`status`・`test` は読み取りと探りの要求だけ＝セッションで流してよい）。`deploy/env/<env>.env` の値の行の書き換えも同じ扱い。WAF カスタムルールは Free プランで 5 本まで＝いまはサービスごとに 1 本（残タスク B37）。
- ★WAF を再適用するとき：`ZONE_NAME`・`MCP_HOST`・`SERVICE`・`SSM_PATH_PARAM`（・prod は `AWS_PROFILE_CF`）を対象に合わせて上書きして apply（guard の既定値は staging 固定）。**IP_ALLOWLIST の既定は off**（2026-09-03 スクリプト側も off に変更＝IP 許可の復活事故は既定では起きない。on は明示時のみ・IP 方式は OAuth/コネクタと両立しない＝2026-08-27/28 実測）。秘密パスルールは /.well-known/・/cdn-cgi/・/healthz を除外済（PRM/認証ディスカバリの生命線）。

## 2. ユニット早見表（箱）

| unit | 役割 | 依存 |
|---|---|---|
| qdrant | ベクトルDB :6333（v7 本線） | mcp より先 |
| polyarchy-mcp | recommendations MCP :8765 | qdrant |
| polyarchy-stats | stats MCP :8766（ENABLE_STATS_APP=true の箱のみ） | — |
| polyarchy-companies | companies MCP :8767（ENABLE_COMPANIES_APP=true の箱のみ・モデル不要・ランタイム秘密なし） | — |
| qdrant-deliberations | 審議会議事録DB 専用の Qdrant :6340（ENABLE_DELIBERATIONS_APP=true の箱のみ・ストレージ deliberations/data/qdrant＝政策主張DB の qdrant ミラーとは別） | deliberations より先 |
| polyarchy-deliberations | deliberations MCP :8768（同上・モデルは recommendations と同じ ruri＋リランカー・ランタイム秘密なし） | qdrant-deliberations |
| polyarchy-web | **廃止（2026-09-02）**＝unit はリポ残置・箱には無い | — |
| cloudflared | 入口（トンネル） | 全 origin の後に start が安全 |
| polyarchy-health.timer | 毎分 healthz・ディスク・空きメモリ・自動再起動の回数→CW metric | — |
| polyarchy-fuelsync.timer | 捕捉ログを 1 時間ごとに S3 へ（前の実行から 1 時間・時刻は固定でない・本体は `/usr/local/bin/polyarchy-fuelsync`＝サービスごとの結果をダッシュボード①へ） | — |
| polyarchy-logprune.timer | 捕捉ログ 30 日削除（毎日・本体は `/usr/local/bin/polyarchy-logprune`＝サービスごとに削除し、1 つでも失敗すれば unit が failed になる） | — |
| polyarchy-unit-failed@.service | 定期実行のユニットの失敗をメトリクス `unit_failed` へ送る（各ユニットの `OnFailure` から起動＝手では起動しない） | — |
| polyarchy-usagereport.timer | 週次利用レポート→S3 ops/report/（月曜 09:20 JST） | — |
| polyarchy-dashboard.timer | 運用ダッシュボード→S3 ops/dashboard/（毎時 05 分） | — |
| polyarchy-dataapply.timer | 15 分毎に S3 release/data.json を検知→自動適用（退避→コード反映→同期→smoke→切り戻し）＝§5 | root・qdrant/mcp/stats(/companies) を止める |
| polyarchy-edgeprobe.timer | 外からの到達の確認（5 分ごと・本体は `/usr/local/bin/polyarchy-edge-probe`＝公開 URL を資格情報なしで叩き、結果をメトリクスとダッシュボード①へ） | — |
| polyarchy-topuser.timer | 1 人の大量の利用（5 分ごと・区切りの 2 分後・本体は `/usr/local/bin/polyarchy-top-user`＝直近 1 時間の最多の 1 人の数をメトリクス `top_user_calls_1h` へ＝§1「top-user」） | — |
| polyarchy-updatecheck.timer | 更新チェック（freshness＋提言新着→ダッシュボード反映・毎日 06:10 JST。手動キック＝SSM ドキュメント check-updates） | — |

**封じ込め（2026-10-19・B46 (a)）**＝上の qdrant・polyarchy-mcp・polyarchy-stats・polyarchy-companies・qdrant-deliberations・polyarchy-deliberations は、コード・Python 環境・設定・モデルを読むだけで、書けるのは unit の `ReadWritePaths`（捕捉ログのフォルダ・Qdrant の索引と写し）だけ（`ProtectSystem=strict`・`NoNewPrivileges`・`PrivateTmp`＝/tmp はユニット専用・`ProtectHome`）。症状と見分け＝unit が起動しない（`journalctl -u <unit>` に `Read-only file system`・`Failed to set up mount namespacing`＝`ReadWritePaths` のフォルダが無い）・捕捉ログが増えない（`クエリ捕捉に失敗`）。自動適用の切り戻しは unit も旧版に戻す（`apply_data_update.sh` の `units_restore`）。箱で unit を手で書き換えない＝直すのはリポジトリの `deploy/systemd/`。

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
- companies データも同型：`s3://<bucket>/data/companies/` → `companies/data/`（store/ と eval/。query_log を消さない）。手元での復元も同じ sync。
- companies の原本（`companies/data/cache/`＝有報の zip・書類一覧・`csv/`・`codelist/`・`taxonomy/`・約 27GB）は `s3://<bucket>/archive/companies/edinet/`（STANDARD_IA）に退避してある＝`data/` の外なので箱の同期は取りに行かない・ライフサイクルの自動削除の対象外。手元へ戻すときは `aws s3 sync s3://<bucket>/archive/companies/edinet/ companies/data/cache/`。★**EDINET API は提出日で約 10 年前までしか取れない（古い側から毎日消える）**＝範囲の外に出た書類は再取得できず、値の置き場を作り直せる根拠はこの退避だけ。再取得で足りるのは API の範囲内の書類だけ（全量なら約 41,500 書類・1 リクエスト／秒で約 1 日）。★退避先は環境のバケットの中にある＝**その環境を撤去する前に、`archive/` を残す環境の置き場へ移す**（取込を足したら退避も足す＝`aws s3 sync` の差分送信）。
- 完全な作り直し＝下の「箱ごと建て直す」。

### 箱ごと建て直す（EC2 を失った・壊れたまま動いている・2026-10-18）

**前提＝箱は使い捨て。** 原本は全部箱の外にある＝S3（データ・コード tar・`release/data.json`・捕捉ログの保護コピー・週次レポートの写し）・SSM（秘密パス・認証・中継の口・上限）・Cloudflare（トンネル・DNS・エッジの規則）・IdP。設計は単一箱・単一 AZ・EBS の定期スナップショット無し（[性能と同時実行の開発計画](../docs/性能と同時実行_開発計画.md) §8）＝複数台や自動の切り替えは持たない。変更の前の保険は `rollback.sh snapshot <env>`（手で）。

| 段 | やること | 目安 |
|---|---|---|
| 0 | **建て直しが要るか**＝`bash deploy/scripts/rollback.sh status <env>`（インスタンスの状態・公開 URL の `/healthz`）。EC2 が止まっているだけ・OS が固まっただけなら `aws ec2 reboot-instances`／`start-instances`（§1「health」の「箱ごと無応答」）＝建て直し不要。ディスクが壊れた・bootstrap が何度やっても通らない・何が変わったか分からない、なら建て直す | 10 分 |
| 1 | **作り直す**＝壊れた箱を terminate（`aws ec2 terminate-instances --instance-ids <id>`。壊れたまま動いていても同じ）→ `bash deploy/scripts/deploy.sh <env> plan` が **1 to add**（EC2 だけ。S3・IAM・VPC・アラームは残る）であることを読む → `apply-plan`。user_data が S3 からコード tar（最後の配布）を取り、bootstrap が依存・重み・データ（約 10 GB）を S3 から入れる。**upload は要らない**（S3 の中身は最後の `release.sh` のまま） | apply 2 分＋bootstrap 15〜25 分 |
| 2 | **見届ける**＝SSM で入り `sudo tail -f /var/log/polyarchy-bootstrap.log` が「完了」・`systemctl is-active qdrant polyarchy-mcp polyarchy-stats polyarchy-companies qdrant-deliberations polyarchy-deliberations`・`systemctl list-timers 'polyarchy-*'`（PROD_MIGRATION §5.1） | 5 分 |
| 3 | **箱の上の smoke と healthz**＝PROD_MIGRATION §5.2 の 4 本と `curl -s localhost:876{5,6,7,8}/healthz` | 5 分 |
| 4 | **入口を開く**＝`sudo systemctl enable --now cloudflared`（bootstrap は start しない＝手で）。★前の箱がまだ動いていたら先に止める（同じトンネルを 2 か所で run すると配信が分流する）。外からの確認＝PROD_MIGRATION §5.3（4 ホストの `/healthz` 200・守るパスは 401・PRM 200）。edge のアラームが OK に戻る | 5 分 |
| 5 | **後始末**＝(a) 最初の自動適用（15 分以内）は、マークが無いので最後のマニフェストをもう一度適用する（数分の停止・想定内・APPLIED で終わる） (b) 週次レポートの蓄積を戻す＝`sudo -u polyarchy aws s3 cp s3://<bucket>/ops/report/weekly.jsonl /opt/polyarchy/polyarchy/ops/usage/weekly.jsonl`（箱で恒久蓄積していたものは EBS と一緒に消える＝S3 に毎週の写しがある） (c) 鳴ったアラーム（health・edge・restarts）が全部 OK に戻ったことを見る (d) 古い EBS・スナップショット・terminate した箱の整理は本人 | 10 分 |

- **RTO の目安＝およそ 1 時間**（判断 10 分＋作り直し 20〜30 分＋確認と入口 15 分＋後始末）。bootstrap の所要は staging の初回構築で十数分（2026-08-28）＝機種・S3 の転送で前後する。
- **失うもの（RPO）**＝捕捉ログの最長 1 時間分（fuelsync は毎時・新しい箱は `restore_fuel` が S3 の保護コピーから 1 回だけ戻す）。切り戻し先（`/opt/polyarchy/rollback`・`releases/prev.tar.gz`）と `applied_data_release.json`（マーク）は箱の中だけのもの＝新しい箱では最初の自動適用が作り直す。それ以外（索引・値・重み・設定）は S3 と SSM が原本＝0。
- **戻ってくるもの**＝`/etc/polyarchy/*.env`（SSM から bootstrap が書く）・秘密パス・認証・中継の口・利用者ごとの上限・e-Stat の appId・運営者のハッシュ。**手で入れ直すものは無い**。
- **変わるもの**＝インスタンス ID（SSM の接続先・アラーム `ec2-status` の dimension は terraform が新しい ID に付け替える）・プライベート IP（中継の口を開けている箱では、bot の側の接続先 `relay_private_ip` が変わる＝`terraform output` で取り直して bot に渡す。許可の範囲 `RELAY_SOURCE_CIDRS` は bot の側の値なので変わらない）。
- **やらないこと**＝EBS のスナップショットから起動する復元（bootstrap のほうが確実で、古い状態を引きずらない）・箱の中の手直し（bootstrap が通らない原因は複製元のコードか S3 の中身にある＝そちらを直して配る）。

## 4. 秘密の再発行

| 何 | 手順 |
|---|---|
| 秘密パス（サービスごと＝mcp・stats・companies・deliberations） | `aws ssm put-parameter --overwrite --type SecureString --name /polyarchy/<env>/<識別子>_http_path --value "/mcp-$(openssl rand -hex 16)"` → `release.sh <env>`（自動適用の bootstrap がサービスの env を書き直して再起動）→ guard を該当 SERVICE・`IP_ALLOWLIST=off` で再適用 → 利用者に新 URL を配布。★WAF ルールはエッジ伝播に数十秒かかる（直後の 403 は慌てない）。★**案 B 認証ホストでは秘密パスは原則回転しない**＝PRM で公開されるため「漏洩時の締め出し」効果が無く、回すと三点一致（WorkOS Resource indicator・`AUTH_AUD_<SVC>`・SSM）の3か所同時更新＋全利用者再登録が要る（PROD_MIGRATION §2.3）。回転が意味を持つのは authless ホストのみ。参考＝claude.ai は URL 単位で旧 OAuth 設定を記憶する（2026-08-28 実測）＝認証方式を変えるときは URL も変えるのが安全 |
| **案 B（auth_issuer/auth_aud_<svc>）** | IdP テナント・環境の差し替え、resource URL 変更時：`deploy/env/<env>.env` の `AUTH_*` を更新 → `deploy.sh <env> secrets`（`auth_*` 以外の秘密も同じ値で書き直す＝SSM の版が 1 つ進むだけ）→ **箱への反映は配布に乗せる**（`release.sh <env>`＝自動適用の中の bootstrap 再走行が `/etc/polyarchy/*.env` を書き直し、再起動・smoke・`/healthz` まで行って監査線に残る。2026-10-10 にこの経路で発行者とパスを同時に切り替えた）。急ぐときだけ箱で bootstrap 再走行 → restart（`systemd-cat -t polyarchy-dataapply` を挟む＝§5）。WorkOS 側（Resource indicator・Default・JWT テンプレート）も同時に＝三点一致（PROD_MIGRATION §2.5）。★切り替えの間は接続が切れる（自動適用の再起動の数分）。★発行者か resource URL が変わると、利用者はコネクタを作り直す（発行者が変われば新規登録から＝PROD_MIGRATION §2.5 の注） |
| Access AUD（旧・案 A） | Access アプリを作り直したら `access-oauth.sh status` で AUD → SSM `access_aud_stats` 更新 → bootstrap 再走行 |
| Cloudflare API トークン | ダッシュボードで再発行 → Keychain `cloudflare-api-token`（WAF）/`cloudflare-access-token`（Access/DNS/Pages）を更新 |
| cloudflared 資格情報 | トンネル作り直し時のみ。`deploy.sh <env> secrets` が SSM へ再登録 |

### 秘密パスを外す（既定の `/mcp` で公開する構成へ・2026-10-10 に staging で実走）

いつ使うか＝接続 URL を公開する前提に変えるとき（例＝コネクタのディレクトリへの掲載。URL が公開されるので秘密パスは意味を失う）。構成の説明と、入口の防御がどう変わるかは [README §9.5「秘密パスなしで公開する構成」](README.md)。
★**URL（ホスト＋パス）が変わる＝利用者は全員コネクタを作り直す**（三点一致の文字列が変わるため。上の表の案 B の行と同じ）。全サービスを同じ日に替える。★**順を守る＝箱が `/mcp` で待ち受けてからガードをかけ直す**（先にガードをかけ直すと、エッジが `/mcp` 以外を止める＝まだ秘密パスで待ち受けている箱への正規の要求が全部 403 になる）。

1. **IdP**：各サービスの新しい URL `https://<ホスト>/mcp` を Resource indicator に登録する（Default の指定も＝PROD_MIGRATION §2.5 の 2・3）。
2. **env と SSM**：`deploy/env/<env>.env` の `AUTH_AUD_<SVC>` を同じ文字列 `https://<ホスト>/mcp` に書き換え、`deploy.sh <env> secrets`。パスは SSM に**値として `/mcp` を入れる**（パラメータを消さない）：
   ```bash
   aws ssm put-parameter --overwrite --type SecureString --name /polyarchy/<env>/<svc>_http_path --value "/mcp"   # recommendations は mcp_http_path
   ```
   - 消すと、箱は既定の `/mcp` で起動するが、ガードは値を読めず**パスの規則を作らない**（無関係なパスが箱まで届く。値が無いときに「`/mcp` 以外を遮断」を作らないのは、秘密パスで動いている箱を止めないため）。
   - SSM の値は次の自動適用で箱に入る＝書いたら同じ作業の中で 3 まで進める。
3. **配布**：`release.sh <env>` → 自動適用の中の bootstrap 再走行が `/etc/polyarchy/*.env` を書き直して再起動する。APPLIED と箱上 smoke PASS を待つ。この時点で箱は `/mcp` で待ち受けるが、**エッジには旧い秘密パスの規則が残っている＝`/mcp` は 403**（次の 4 まで外からつながらない＝続けて行う）。
4. **ガードのかけ直し**（運営者がターミナルで＝§1「公開 URL」）：全サービスで `cloudflare-guard.sh apply` → `test`。正＝`/mcp` は 401（箱まで届き、ログインを求められる）・それ以外（`/mcpx`・旧い秘密パス・`/`）はエッジで 403・`/.well-known/` と `/healthz` は 200。直後の食い違いはエッジの伝播（数十秒）を待つ。
5. **確認**：使っている経路ごとに、新しい URL でコネクタを作り直す → ログイン → ツール実行 → 捕捉ログに利用者キーつきの行。Default でない Resource indicator のサービスでも 1 回通す。
6. **案内**：利用者へ新しい URL と作り直しの手順を伝え、公開ページの接続の説明を新しい構成に揃える。

- 箱は `/mcp` で始まるパスすべてに、トークンなしで 401 を返す（守る範囲が前方一致＝意図どおり。`/mcpx` などはエッジが先に止める）。
- IdP の環境も同じ日に替えるなら、発行者（`AUTH_ISSUER`）も 2 で一緒に書き換える＝利用者は新規登録から（PROD_MIGRATION §2.5 の注）。
- 秘密パスを付け直すときも順は同じ（箱が新しいパスで待ち受けてからガード）。★ただし URL を公開した後（掲載の後）は、発行者・ホスト名・パスを変えない＝掲載の URL と利用者の接続がそこに紐づく。

## 5. データ更新（公開ページの約束）

**何を更新すべきかは毎日の更新チェックが教える**＝ダッシュボード③の「提言の新着 N 件」「stats 取得元の変化 N 件」「stats 取込待ち N 件」
（新着の実タイトルは箱の `recommendations/data/cache/update_check.json`）。以下は検知後の**取込の実行**手順。

- ★ **「変化 N 件」は日次の差分**（前回の確認からの Last-Modified／ETag 等の変化）＝取り込まないまま翌日を迎えると数えられなくなる。手元の `freshness` も実行のたびに状態を上書きする（溜まっている分は差分では拾えない）。**溜まっている分は「取込待ち」で見る**＝検知した日を状態に残し、その日以降にその dataset が取り込まれる（値の `retrieved_at` が追いつく）まで消えない。`refresh`（`--dataset` なし）は「変化」と「取込待ち」の両方を取り込む。収録と取得元を直接比べる型（QE・政府経済見通し・総務省の次版・国際機関の最新年）は差分ではないので、もともと溜まっても消えない。
- ★ **2026-10-09 の版を配った直後の 1 回だけ、Eurostat（`eu`）が「変化」になる**（確認する対象を全 dataset に広げた＝signal の形が変わった。取得元が実際に変わったのではない）。Eurostat は検知日を実行した日にする型＝**取り込むまで「取込待ち」に残る**（`refresh --dataset eu` で消える）。SNA 経済活動別（`sna_activity`）も年版のシートの Excel を確認に足したが、足したファイルの Last-Modified が既存のファイルと同じなら「変化」にも出ない。出ても検知日は Last-Modified の日まで遡る＝最終取込がそれより後なら「取込待ち」には入らない（取り込まなくてよい）。★箱は UTC＝JST の朝 9 時より前の実行は、前日の日付で検知日を刻む。freshness の失敗の数え方も変わった＝中小企業白書（`hakusho_sme`）は「対象外」（理由つき・失敗に数えない）・年版のシートだけの `sna_fcs` は初回だけ「初回記録」＝変化も取込待ちも無い日は rc=0（それまでは、その日は ⚠ rc=2）。★終了コードは変化か取込待ちが 1 件でもあると 1 になり、確認の失敗（2）を隠す＝失敗の有無は `総合:` の行の「probe 不能」（箱では `stats/data/cache/freshness_last_run.json` の `errors`）で見る。
  - 取込待ちが始まるのは、この仕組みが入った版（2026-10-06）を配布した後に検知したものから＝それ以前に見逃した分は出ない。初回は手元で `python -m stats.ops.refresh --dataset <コード名>` を名指しで（今回の 12 件と同じ）。
  - 取得元のファイルが中身の同じまま置き直された（Last-Modified だけ変わった）ときも取込待ちになる＝再取込すれば消える（差分なしで終わる）。
  - ★ **取込待ちの判定は日付の単位**（値の `retrieved_at` が日付だけ）＝取込の日のうちに、取込の**後**で取得元が更新されたものは取込待ちに入らない（同じ日の取込を取り込み済みとみなす）。日次の取得元（財務省の国債金利・日銀）は翌日の「変化」で拾い直されるが、月次の取得元は次の更新まで見えない。**取込の日と翌朝の「変化」に出た dataset は、取得元の Last-Modified（`curl -sI` の HEAD）と取込の時刻を照らす**＝取込より前の更新なら取り込み済み（何もしない）・後の更新なら次の定型更新でその dataset を取り込む（後の更新だった件数は開発側へ知らせる＝頻発するなら freshness が取込の時刻を持つように直す・残タスク §E-B34）。

```bash
# stats：鮮度確認 → 更新（ローカルで実施 → upload → 箱の bootstrap 再走行 or stats 再起動）
python -m stats.ops.freshness
python -m stats.ops.refresh --dataset <コード名>   # ゲート 3 本まで通る・自動コミットしない
```

### 企業情報（companies）の更新 — 有価証券報告書の提出に応じて・全てローカルで取り込んで箱へ配布

```bash
# 取込（作業用 PC・EDINET の API キーは companies/.env＝箱には運ばない）。日付の範囲＝前回以降の提出日。取得済みの書類は再取得しない
python -m companies.ingest.edinet --from 2026-10-01 --to 2026-10-31
curl -sS -o companies/data/cache/codelist/Edinetcode.zip https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip \
  && unzip -o -q companies/data/cache/codelist/Edinetcode.zip -d companies/data/cache/codelist/ && python -m companies.ops.build_industries   # 業種（EDINET コードリスト）＝新しい会社が入ったら必須（無いと exact_match の業種の判定が FAIL）
python -m companies.ops.population_report | head -40      # 棚卸し＝「語彙に無い標準要素」「最上段の収益が見当たらない会社」が増えていないか
python -m companies.eval.exact_match && python -m companies.eval.find_quality && python -m companies.eval.test_core   # 既存の問は書類を固定して引く＝新しい書類で動かない
bash deploy/scripts/release.sh staging                     # 配る前の確認→全ゲート（一覧はルート README「品質の担保」）→配布→自動適用
```
- 6 月（3 月決算の提出集中期）は約 2,400 書類＝約 2 時間。他の月は数十〜数百。
- ★上流の運営者（開発と運用を 1 人が兼ねる）は、取込を開発用フォルダで行い、値の置き場だけを配布用のクローンへ写してから release する（`data/` は git 外＝pull では届かない）：`rsync -rlp --checksum --delete <開発>/companies/data/store/ <クローン>/companies/data/store/`（中身で比べ、時刻は写さない＝中身の同じファイルの時刻が変わらず、`release.sh` の S3 同期〔サイズと時刻で比べる〕が変わったファイルだけを送る。`-a` だと中身が同じでも時刻が開発側に揃い、置き場の全体〔数百 MB〕を送り直す。`-a --checksum` でも時刻は揃えられる）。★写す前の dry-run（`--dry-run --itemize-changes`）の判定は**転送（`>f`・`cd`）と削除（`*deleting`）の行が 0 件**で見る＝時刻を写さないので、一度写したファイルは中身が同じでも毎回 `.f..T....`（時刻だけの差）の行として出る（転送はされない・`-t` を足して消さない＝上の理由で時刻は写さない）。★`eval/` は git 追跡＝写さない（先に置くと `git pull` が上書きを拒む）。原本の zip（cache/）も写さない。
- 語彙に無い標準要素が出たら、公式 CSV（API type=5）でラベルを確かめてから `companies/core/items.py` に足し、`make_candidates --docids=` で問を足す（companies/CLAUDE.md）。

### サービスを足す（companies を有効にする・2026-09-22）

配線はコードに入っている（`ENABLE_COMPANIES_APP` の opt-in・既定 false）。有効化は上流の運営者（または導入団体）の作業＝順に
（★順序は 2026-09-22 の staging 実走で並べ替えた＝IdP の Resource indicator は秘密パスを含むので、秘密パスを先に決める）：

1. **秘密パス**＝`aws ssm put-parameter --overwrite --type SecureString --name /polyarchy/<env>/companies_http_path --value "/mcp-$(openssl rand -hex 16)"`（§4 と同じ流儀・値は控えない＝SSM が唯一の置き場）。
2. **env**＝`deploy/env/<env>.env` に 3 行＝`ENABLE_COMPANIES_APP=true`・`TUNNEL_HOST_COMPANIES=companies.<domain>`・`AUTH_AUD_COMPANIES=https://companies.<domain>/<1 の秘密パス>`。
   ★編集前の控えは**リポジトリの外**に取る（例 `~/polyarchy-env-backup/`）。`deploy/env/` に置いた `<env>.env.bak-<日付>` は、2026-10-04 より前の版では tar の除外（`*.env`）にも `.gitignore` にも当たらず配布で箱に入った（いまは `*.env.*` も除外＝念のため）。
3. **SSM**＝`bash deploy/scripts/register-secrets.sh <env>`（`auth_aud_companies` を登録）。三点一致の確認＝値を表示せずに比べる：
   ```bash
   P=/polyarchy/<env>; H=companies.<domain>
   [[ "$(aws ssm get-parameter --name $P/auth_aud_companies --query Parameter.Value --output text)" == \
      "https://$H$(aws ssm get-parameter --name $P/companies_http_path --with-decryption --query Parameter.Value --output text)" ]] && echo "三点一致 OK（aud＝https://host＋秘密パス）" || echo "不一致"
   ```
4. **IdP（WorkOS）**＝Resource indicator に 2 と同じ文字列 `https://companies.<domain>/<秘密パス>` を追加（三点一致＝PROD_MIGRATION §2.5）。
   ★**保存の後、Connect → Configuration → MCP resource indicators の一覧に行が出ていることを確かめる**（2026-10-04 の deliberations は行が保存されておらず、claude.ai が「認証に失敗」になった）。
   見分けの目印＝箱の CW Logs `polyarchy/<svc>` に「Bearer 欠如」の 401 しか無く、トークン付きの要求が 1 件も無ければ IdP 側（サーバ側のディスカバリ `/.well-known/oauth-protected-resource` が正しくても起きる）。
5. **Terraform**＝`bash deploy/scripts/deploy.sh <env> apply`（対話で `yes` を待つ＝端末で流す。端末の対話なしで流すときは `deploy.sh <env> plan`〔読み取りだけ・plan を保存〕→ 差分を読む → `deploy.sh <env> apply-plan`〔保存した plan をそのまま適用し、直後の plan が `No changes` であることまで確かめる。保存した plan が無ければ止まる。差分の無い plan は保存されない＝`No changes` の後の `apply-plan` は「plan が無い」で止まる〕＝2026-10-13〜。本書のほかの「`deploy.sh <env> apply`」も同じ）（ロググループ・health/401/5xx アラーム・箱ロールの書込先・S3 ライフサイクル。EC2 の差分は `ignore_changes` で出ない）。
   ★**順序＝配布（8）より前**に固定する（2026-10-04 に整理）。理由はロググループ＝箱の CW agent はサービスのログが出た時点でロググループを自分で作る＝配布の後に apply すると作成が `ResourceAlreadyExists` でぶつかる（保持 30 日を terraform 側で持つ設計＝先に作る）。
   その代わり、apply から配布の適用までの間は health アラームが ALARM になる（`treat_missing_data = breaching`＝サービスが立つまで値が来ない）＝**想定内**。通知は状態が変わったときだけ＝ALARM と OK の 1 通ずつで、適用の 3 分ほど後に OK に戻ることを確かめる。鳴る時間を短くするには、apply を配布（`release.sh`）の直前に回す。
   ぶつかってしまったら＝`deploy/scripts/lib.sh` の `TF_ARGS` と同じ `-var` を付けて `terraform import 'aws_cloudwatch_log_group.<svc>[0]' polyarchy/<svc>` で取り込んでから apply し直す（保持は CW agent 側も 30 日で一致・差分はタグだけ）。
   ★フラグが false のままでも「変更 2」（箱ロールの `data/companies/query_log/*` 書込先・S3 ライフサイクル `companies-query-log-retention-30d`）は出る＝コミット済みのコード由来で想定内。
   apply の後に出る terraform の `next_steps`（秘密登録・upload・bootstrap の監視…）と「次（初回構築のときの手順…）」は**初回構築の手順**＝この手順のように既存の箱へ足す apply（ほかに S3 のライフサイクル・アラーム・IAM だけの変更も同じ）では読み飛ばす。確かめるのは plan の差分が期待どおりであることと、apply の後の plan が `No changes.` であること。
6. **箱の deploy.env**＝★稼働中の箱は user_data を再実行しない＝`/etc/polyarchy/deploy.env` に同じ 2 行（`ENABLE_COMPANIES_APP`・`TUNNEL_HOST_COMPANIES`）を足す。
   SSM Session で編集してもよいが、`send-command` なら SSM のコマンド履歴に残る（監査線）。★`--parameters` はインライン JSON だと `$`・`\n`・`(` のエスケープで壊れる＝**ファイルに書いて `file://` で渡す**：
   ```bash
   cat > /tmp/enable_companies.json <<'EOF'
   {"commands":[
    "F=/etc/polyarchy/deploy.env", "cp -p $F $F.bak-$(date +%Y%m%d)",
    "grep -q '^ENABLE_COMPANIES_APP=' $F || printf 'ENABLE_COMPANIES_APP=true\\nTUNNEL_HOST_COMPANIES=companies.<domain>\\n' >> $F",
    "logger -t polyarchy-dataapply '[manual] deploy.env: companies enabled'",
    "grep -e '^ENABLE_' -e '^TUNNEL_HOST_' $F"]}
   EOF
   aws ssm send-command --instance-ids <instance-id> --document-name AWS-RunShellScript --parameters file:///tmp/enable_companies.json --query Command.CommandId --output text
   aws ssm get-command-invocation --command-id <上の ID> --instance-id <instance-id> --query StandardOutputContent --output text
   ```
   （`logger -t polyarchy-dataapply` を挟むと CW Logs `polyarchy/dataapply` の線にも残る＝§5「切り戻し後」の `systemd-cat` と同じ意図）
7. **Cloudflare**＝
   - DNS＝**ダッシュボードで CNAME**（名前 `companies`・ターゲット `<TUNNEL_ID>.cfargotunnel.com`・プロキシ済み）。一覧では既存の stats/recommendations と同じ「トンネル」型で表示される。API で足すには DNS:Edit を持つトークンが要る（現行の guard 用・Access 用の 2 トークンには無い）。`cloudflared tunnel route dns` は cert.pem が対象ゾーンに紐づく新トンネル作成直後だけ使える（PROD_MIGRATION §2.2）。
   - guard＝★レート制限は Free プラン 1 本を全ホストで共有し、apply はそれを**作り直す**。`RL_HOSTS` を省けば既存の式のホストを引き継ぐ（2026-09-22〜）が、明示するなら**全ホスト**を渡す：
     ```bash
     SERVICE=companies MCP_HOST=companies.<domain> SSM_PATH_PARAM=/polyarchy/<env>/companies_http_path \
     RL_HOSTS=recommendations.<domain>,stats.<domain>,companies.<domain> IP_ALLOWLIST=off \
     bash deploy/scripts/cloudflare-guard.sh apply
     bash deploy/scripts/cloudflare-guard.sh status   # レート制限の式に stats/recommendations が残っていること
     ```
8. **配布**＝配布用のクローンで `release.sh <env>`（全ゲート）→ 自動適用が `bootstrap` 再走行で ⑥ データ同期・⑧ companies.env・⑩ ユニット設置・ingress 追記まで行い、**ingress が変わったので cloudflared も再起動する**（2026-09-22〜・数秒の断＝stats/recommendations も一瞬切れる。config が変わらない通常のデータ更新では再起動しない）→ ダッシュボードで版一致・①に `polyarchy-companies` active と `:8767` ok・②に companies 収録 N 社（ダッシュボードは `ENABLE_COMPANIES_APP=true` の箱だけ companies の行を出す）。公開側の入口まで含めて見るときは `https://companies.<domain>/healthz` が 200 も補助に。
   ★それ以前の版の箱、または bootstrap を手で再走行したときは cloudflared が旧 ingress のまま＝公開側は 404（トンネルの catch-all）が続く。`send-command` で `logger -t polyarchy-dataapply '[manual] restart cloudflared'; systemctl restart cloudflared` を流してから healthz を確認する。
9. **接続確認**＝claude.ai／Claude Code／ChatGPT の 3 経路（PROD_MIGRATION §2.5 と同じ）。公開ページ `companies.html` はこの後に Pages へ（先に出すと案内だけが先行する）。

### サービスを足す（deliberations を有効にする・2026-10-04 配線）

配線はコードに入っている（`ENABLE_DELIBERATIONS_APP` の opt-in・既定 false）。手順は上の companies と同じ型で、名前を置き換える（`companies`→`deliberations`・ポート :8767→:8768・SSM `deliberations_http_path`／`auth_aud_deliberations`・env `ENABLE_DELIBERATIONS_APP`／`TUNNEL_HOST_DELIBERATIONS`／`AUTH_AUD_DELIBERATIONS`・公開ページ `deliberations.html`）。違うところだけ：

- **配布は 2 回に分ける**：自動適用のスクリプト（`/usr/local/bin/polyarchy-data-apply`）の変更は次の次の配布から効く＝**フラグを false のまま配線入りのコードを 1 回配布**し、箱のスクリプトが新しくなってから（ダッシュボードで版一致を確認してから）フラグを立てて 2 回目を配布する。1 回目で立てると、旧スクリプトは審議会DB の停止・起動・smoke・退避を知らない（bootstrap は新しいので設置・起動はされるが、切り戻しに入らない）。
- **terraform（手順 5）は 1 回目と 2 回目の配布の間＝2 回目の `release.sh` の直前**：フラグ false の 1 回目の後はサービスが無い＝ログが出ない＝CW agent はまだロググループを作らない（ぶつからない）。health-deliberations の ALARM は 2 回目の適用までの間だけ（想定内）。2026-10-04 の staging は 2 回目の後に回して import で解消した。
- **配布用のクローンに審議会DB のデータと専用 Qdrant が要る**：`release.sh` は手元の審議会DB の Qdrant（`DELIB_QDRANT_URL`・既定 `http://localhost:6340`）から束を書き出す（4 ゲート＋語彙と索引の一致の検査つき）。配布用のクローンの `deliberations/data/`（raw・cache・bm25）と、その Qdrant のストレージが同じ時点のものであること。開発用と配布用で 1 つの Qdrant を共有するなら、`qdrant-dev` と同じく**マウント元を配布用のクローンに置く**（政策主張DB と同じ流儀＝データの原本は配布用）。最初は開発用の `deliberations/data/` を配布用へ写すか、配布用で収集から取り込みまで回す（deliberations/CLAUDE.md）。
- **データは束で運ぶ**：S3 `data/deliberations/bundle/`（Qdrant のスナップショット＋語彙・目録・解析結果・第 1 便 約 209MB）。箱の bootstrap が専用 Qdrant を上げた後、束が前回の復元から変わったか、コレクションが欠けたときだけ復元する（`deliberations.ops.bundle restore --if-changed`）。政策主張DB の `data/qdrant/` ミラーには混ぜない（bootstrap ⑥ はフラグに関係なく `deliberations/*` を除外）。
- **箱のメモリ**：MCP（手元の目安 約 1.3GB）＋専用 Qdrant（小さい）。2026-10-04 の staging（t3.xlarge・使用 2.74／15.42GB）で同居可と判断（社内ノートの返答）。有効化の後はダッシュボード①のメモリに 2 行（polyarchy-deliberations・qdrant-deliberations）が出る。
- **guard**＝`RL_HOSTS` に既存の全ホストと `deliberations.<domain>` を並べる（`cloudflare-guard.sh` 冒頭の例）。
- **公開ページ**＝`deploy/pages/deliberations.html`（2026-10-04 に原稿から移し、入口・利用規約・プライバシーポリシーに足した）。接続確認の後に Pages へ（deploy/pages/README.md）。

### 配布用のクローンを開発用と分ける（推奨・2026-09-19）

`release.sh` が箱へ送るコードは **git の追跡ファイルだけ（`git archive HEAD`）**（2026-10-09〜・`upload_to_s3.sh`。それまでは作業フォルダを
除外リストで固めた tar＝未コミットの変更や作業中のファイルも箱に入った）。コードの範囲に未コミットの差分があれば配る前の確認で止まる
（測ったコードと配るコードを揃える）。データ（`*/data/`）は tar に入らず S3 経由。配布は**リポジトリをクローンした別フォルダ**から行う
＝ゲートが測る索引（`qdrant-dev` のマウント元）と配る索引を同じフォルダにする。1 人で開発と運用を兼ねる場合も、フォルダを分ける。

| | 開発用のフォルダ | 配布用のクローン |
|---|---|---|
| やること | コード・評価問・文書 → ゲート → コミット → push | `git pull` → 新着取込（`update.sh`・`stats.ops.refresh`）→ `release.sh` |
| コミットするもの | コード・文書・評価問 | **データ更新だけ**（`catalog.csv`・`stats/data/registry/`・評価セットとアンカー記載）。コードはここで編集しない |
| git 外で置くもの | ゲート用のデータの写し（任意） | `deploy/env/<env>.env`・`recommendations/.env`・`stats/.env`・データの原本（`recommendations/data/{pdfs,bm25,qdrant}`・`stats/data/{values,cache}`）・terraform state（terraform を触るとき） |
| Python 環境 | 任意 | 下表のとおりロックから作る（環境は配布用のクローンを指す editable install にする） |

配布の前の確認は **`release.sh` が最初に機械で行う**（`deploy/scripts/release_preflight.sh`・2026-10-09〜。1 つでも外れたら何も配らない）：
- `qdrant-dev`（審議会DB 有効時は `qdrant-delib` も）のマウント元＝このフォルダの `*/data/qdrant`（測る索引＝配る索引）と、その Qdrant が応答する（2026-10-13〜＝止まったコンテナでもマウント元は読める。それまでは、ゲートが全部通った後で審議会DB の手前で止まった）
- `deploy/env/<env>.env` の `AWS_PROFILE` で `aws sts get-caller-identity` が通る
- `COLLECTION_NAME`＝env ファイルの `RECOMMENDATIONS_COLLECTION_NAME`（シェルに別の値があれば中止）・`DELIB_COLLECTION` は未設定か `deliberations_v1`
- 検索の調整値（`RERANK_RETRIEVE_K`・`HYBRID_KV` 等＝recommendations／deliberations の config が読む名前）がシェルに export されていない
- ゲートを回す python の依存＝箱のロック（`deploy/scripts/check_lock.py`＝開発側で基準値を測る前にも手で回せる）
- 政策主張DB の BM25 語彙の `n_docs`＝`qdrant-dev` の点数（語彙と索引が同じ時点）
- コードの範囲（`*/data/` の外）に未コミットの差分が無い（tar は HEAD から作る）

この確認は**単体で先に回してよい**（読み取りだけ・何も配らない・`qdrant-dev` も止めない）＝配布用のクローンの root で
`bash -c 'source deploy/env/<env>.env; REPO_DIR=$PWD; source deploy/scripts/release_preflight.sh; preflight'`。
env ファイルを読ませるのは `release.sh` と同じ変数で走らせるため（変数を手で渡すと `ENABLE_DELIBERATIONS_APP` の書き忘れで `qdrant-delib` の確認が黙って抜ける）。
`release.sh` と同じ ✓ の行が出て終了コード 0 なら、本番の ⓪ では止まらない＝索引の作り直し・ログインのし直し・`check_lock` の不一致を、ゲート（十数分）の前に片づけられる（2026-10-09 staging で運用側が実施）。

**人が判断するもの**は 1 つだけ＝ログインからの経過時間（下）。手で確かめるなら `git log origin/main..HEAD --oneline`（push 前のコミットが無いか）。
- ★ **AWS のログインが配布の途中で切れないようにする**：`release.sh` はゲート（十数分）の後に upload とマニフェストの書き込みをする。途中で有効期限が切れると upload とマニフェストの間で止まり得る（2026-09-27 staging＝release の直後に切れた）。判断は `sts` が通ることと、ログインしてからの経過時間で行う。★ `sts` は **`deploy/env/<env>.env` の `AWS_PROFILE` を `--profile` に指定して**確かめる（`release.sh` が使うのはこのプロファイル）＝素の `aws sts get-caller-identity` は default のプロファイルを見るので、手元に別系統の資格（default のログインと、アクセスキーのプロファイル）があると誤判定する（2026-10-02 staging＝default で確かめて「切れている」と判断したが、配布に要るプロファイルは生きていた。なお `AWS_PROFILE` がアクセスキーのプロファイルを指したシェルでは `aws login` が拒否される）。ログインし直すかどうかは、そのプロファイルについて判断する＝ログインから長く経っていればログインし直してから始める。`aws configure export-credentials` の `Expiration` は判断材料にならない（手元の短期資格が自動更新されるため、ログインし直した直後も含めて常に十数分先を示す。実際に効くのはその下のログインのセッションの期限で、これは画面から見えない）。配布の後の見張り（新しい捕捉ログの同期の 1 回目＋次の毎時のダッシュボード）は 1 時間を超える＝見張りに入る前にもう一度ログインし直す（2026-10-01 staging＝見張りの途中で切れた）。
- 配布のたびに qdrant のファイル（約 100MB）が送り直しになる＝`qdrant-dev` の停止・再開でファイルの時刻が変わるため。索引を変えていなくても起きる＝害はない（S3 の同期はサイズと時刻で比べる）。
- 配布の後の確認＝ダッシュボードは APPLIED の直後に再生成して S3 へ上げる（2026-09-27 の版から。それより前の版の箱では次の毎時 05 分）。捕捉ログは箱から S3 へ 1 時間ごとの同期（`OnUnitActiveSec=1h`＝前の実行から 1 時間・**時刻は固定でない**＝箱の再起動・再設置でずれる。中身が変わらないサービスのオブジェクトは上げ直さない＝S3 の更新時刻が古く見えても止まってはいない・サービスごとに動く時刻が違う。止まったかどうかは次のダッシュボードの欄で読む）＝当日の問は最大 1 時間ほどで S3 の `query_log/` に見える。同期の結果と最終成功の時刻はダッシュボード①の「捕捉ログの S3 同期」（サービスごと・失敗は赤）で見る（2026-10-01 の版から＝それより前の箱では companies／stats の同期の失敗は無視され、運用側から見えなかった）。メモリはダッシュボード①（サービスごとの現在と起動からの最大・箱全体）＝箱を手で触らずに配布の後の山を確かめる。APPLIED の直後のダッシュボードの「提言の新着チェック」は、S3 に残った古い写しが箱の値を上書きして古い値を出すことがあった（数分後の更新チェックで戻る・2026-10-02 に upload と bootstrap の同期で `cache/` を除外＝bootstrap.sh は自動適用が展開した新しい tar の版で走る＝その配布から効く・S3 の `data/cache/` に残った古い写しは運用側で消す）。コネクタでの確認の前の Reconnect（Claude Code の `/mcp`）は、**ツールが増減したとき・ツールやサーバの説明が変わったときだけ**要る＝返り値だけの変更は Reconnect なしで新しい振る舞いが返る（ChatGPT のコネクタの作り直しも同じ条件）。
- ★ **`qdrant-dev` のマウント元は配布用のクローンに置く**：`release.sh` は `qdrant-dev` を止めて `recommendations/data/qdrant` を S3 へ**ミラー（--delete）**する。
  マウント元が別のフォルダ・空・古いと、その内容で箱の索引を上書きする。開発用のフォルダのゲートは同じ `qdrant-dev` に HTTP（:6333）でつながるので、Qdrant の実体は 1 つでよい。
- Python の editable install は**最後に `pip install -e` したフォルダ**を指す。リポジトリ root 以外を cwd にして動くスクリプトは、その向き先のコードを import する＝フォルダごとに env を分ける。
- ★ **`git pull` でパッケージが増えたら（`pyproject.toml` の `include`）、env に `pip install --no-deps --no-build-isolation -e .` を入れ直す**：editable install の対応づけは入れた時点のパッケージだけを持つ。ゲートはリポジトリ root を cwd にした `python -m` で回る＝対応づけが古くても cwd 経由で読めて通り、パス指定で起動するスクリプト（upload の重みの段）だけがゲートの後で止まる（2026-10-10 staging＝試走で検知）。配る前の確認が cwd を外して全パッケージの向き先を照合する＝古ければゲートの前に止まる（開発用の env・他のクローンでも同じ）。

### 手元の製作環境（初回のみ・運用者を引き継ぐ人が最初に整える）

| 要素 | 内容 |
|---|---|
| Python | 配布用の env は**ロックファイルから**作る（§7 ②＝作業用 PC は `lock-all.txt`・Mac は `lock_mac_variant.py` の変種を `--require-hashes` で入れ、本体は `-e . --no-deps`）＝箱と同じ版でゲートを回す。開発だけなら `pip install -e ".[all]"`（ルート CLAUDE.md §4）でよい。update.sh/release.sh/triage.sh はその env を PATH の先頭に通した shell で実行 |
| 検索 DB | Docker の `qdrant-dev`（ローカル :6333・`recommendations/data/qdrant` を S3 `data/qdrant/` から同期して起動）。release.sh は起動を確認し、upload 中だけ止める。審議会DB を有効にした環境は `qdrant-delib`（:6340・`deliberations/data/qdrant`）も。★どちらも再起動ポリシーなし＝Docker（の VM）を上げ直した後は止まっている＝2 つとも手で `docker start`（配る前の確認が応答を見る） |
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
# ②取込（v7/Qdrant 本線・doc 単位増分・冪等。ローカル qdrant-dev を起動しておく）
COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.qdrant_ingest ingest
# ③分野タグ・文書性格の追い判定（新規行だけ判定・--dry-run で対象確認から）
#   ★取込の後＝判定は本文を Qdrant から読む（取込の前だと新規文書は題名だけで判定される）。判定は catalog と payload の両方に書く
python -m recommendations.ingest.policy_tagger --dry-run
python -m recommendations.ingest.policy_tagger
# ④⑤ ゲート→配布→マニフェスト＝release.sh に一本化（2026-09-03。手順を飛ばす余地を作らない）
bash deploy/scripts/release.sh <staging|prod>
```

**release.sh がやること**＝配る前の確認（上の「配布用のクローン」）→ 全ゲート（一覧と基準はルート README「品質の担保」＝ここに本数を写さない。
アンカーは ALL 行の数値と基準値・それ以外の合否は終了コード）を実行し**全 PASS のときだけ** upload → 最後に S3 `release/data.json`
（リリースマニフェスト＝出荷時のゲート数値と測った構成入り）を書く。**1 つでも FAIL なら S3 に何も置かれない**。
配布の結果の確認は `release.sh` の出力に頼らなくてよい＝ゲートの結果は `release/data.json` の `code_version` と `gates`（全 PASS のときだけ書かれる）、
箱への反映は下のダッシュボード②で足りる（AI のセッションでは配布の出力の読み取りが権限の判定で拒否されることがある＝2026-10-04。S3 の読み取りは通る）。

**箱側の反映は自動**＝`polyarchy-dataapply.timer`（15 分毎）がマニフェストの変化を検知し、
退避（rsync）→ サービス停止 → **コード tar 再展開**（2026-09-03〜＝コード変更もこの経路で自動反映・tar 再展開の手作業は廃止）
→ qdrant ミラー同期 → bootstrap（データ差分 sync・依存再解決）→ 再起動 → 箱上 smoke＋稼働中の各サービスの /healthz（unit・env・認証の
配線を通るのはこちら・2026-10-09〜）→ ダッシュボード、まで
無人で行う（全工程 journal→CW Logs `polyarchy/dataapply`・**人手の箱操作ゼロ**）。反映確認＝ダッシュボード②の
データ版が released/applied 一致・APPLIED・smoke PASS になっていること。ログで確定するなら CW Logs `polyarchy/dataapply` の
「✅ APPLIED:」の行（smoke 3 本の PASS の直後に出る＝この版から。★それより前の版の箱では完了の行が更新チェックの終了後まで出ない＝
smoke の PASS が最後の行のまま止まって見える。そのときはダッシュボード②〔毎時 05 分前後に更新〕を待つ）。
CW Logs で適用の終わりを待つときの語は「✅ APPLIED」か「切り戻し完了」（★「rollback」で待たない＝退避の段の
`rsync → /opt/polyarchy/rollback` の行で早く止まる。「ROLLED_BACK」はマークにだけ書かれログには出ない＝2026-10-08 の B33 配布で運用側が指摘）。
ダッシュボードは APPLIED の後の更新チェックの最後（数分後＝CW Logs の「ダッシュボード更新済」の行）と毎時 05 分に生成される。
版を見るときは冒頭の「生成」の時刻（JST）が APPLIED より後であることを先に確かめる（それより前の生成なら古い版が出ていて正しい）。
★**配布の時間帯**＝自動適用は 4 サービスを数分止める（停止 → bootstrap 再走行 → 再起動 → smoke）。`release.sh` から最長 15 分後の任意の時刻に始まる（timer は 15 分の刻み・始まりを選べない）＝**利用の少ない時間帯に流す**。止まっている間の呼び出しは利用者側でエラーになる（下の「リリース中は health アラームが 1 往復」と同じ数分）。

**smoke（または /healthz）FAIL 時は自動切り戻し**（2026-09-03・B15 論点1＝運用者ロール移譲の前提 (2)）＝停止 → 退避したデータを
書き戻し（qdrant はミラー）→ 前回の code tar を再展開＋pip → 再起動 → smoke 再実行 → マーク status=ROLLED_BACK →
メトリクス dataapply=0 → アラーム `dataapply-rollback` → メール。**不採用のマニフェストは再試行しない**（15 分毎の
再適用ループを防ぐ）。qdrant が起動しない・bootstrap が落ちる等、反映途中の失敗も同じ経路で戻る。
退避先＝箱の `/opt/polyarchy/rollback/`（データ）と `/opt/polyarchy/releases/{current,prev}.tar.gz`（コード）。新しい箱は user_data が最初の
tar を `current` に置く（2026-10-09〜・それまでの箱は最初の適用でコードを戻せなかった＝既存の箱は 1 回目の APPLIED で `current` ができている）。
★自動適用のスクリプト（`apply_data_update.sh`）の変更は**次の次の配布から効く**（ルート CLAUDE.md §5）＝確かめは staging で演習フラグを使い 2 回目の配布で。
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
release.sh の前提（手元）＝conda env `polyarchy`・Docker の `qdrant-dev`（審議会DB 有効時は `qdrant-delib` も）・aws/terraform/git・`deploy/env/<env>.env`・`recommendations/.env`（Anthropic＝分野タグ判定／OpenAI）・
`stats/.env`（e-Stat appId）・terraform の state（S3・2026-10-19〜＝`deploy.sh` を流したことのあるフォルダでは `terraform output` がバケット名を読む・無ければ環境変数 `BUCKET` を指定）。

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

### 週次利用レポートで運営者自身の利用を除く（任意・2026-10-12）
運営者・開発者のドッグフーディングと、掲載の審査用アカウントの利用が混ざると、外の利用者の数字が読めない。除く利用者ハッシュを SSM に入れると、
週次レポートとダッシュボード④に「除:」の列（運営者を除いた件数・利用者数・新規）が並ぶ（既存の数字は含んだまま）。
1. ハッシュを得る（手元・リポジトリ root）：`python -c "from polyarchy_common.access import user_hash; print(user_hash('<ログインに使うメールアドレス>'))"`（16 桁の 16 進。メールアドレスは小文字にそろえて計算される）。除く利用者の分だけ繰り返す。
2. SSM に入れる（カンマ区切り・空白なし）：`aws ssm put-parameter --overwrite --type SecureString --name /polyarchy/<env>/usage_operator_hashes --value "<ハッシュ>,<ハッシュ>"`
3. 次の配布で箱に入る（bootstrap ⑪ が `/etc/polyarchy/usage.env` に書く・形が想定外なら使わず警告）。次の月曜の週次レポートから列が埋まる＝それより前の週は「—」のまま（遡って作らない）。
- 値はリポジトリ・文書・コミットに書かない。やめるときは SSM のパラメータを消して配布（箱の env も消える・それまでに数えた週の値は残る）。
- 手元で再生成するとき（`usage_report --s3-bucket …`）は、同じ値を環境変数 `USAGE_OPERATOR_HASHES` で渡す。渡さなくても、箱が数えた「除:」の値は消えない。

## 6. アラーム対応の心得

- 誤報でメールを無視し始めるのが最悪（運用設計 §1.2）。鳴りすぎるなら閾値を上げる変更を terraform（observability.tf）で行う。
- SNS 購読はメールの Confirm リンクを踏むまで有効にならない（apply 直後に届く）。
- **運用ダッシュボード＝S3 `ops/dashboard/index.html`（毎時）**：稼働・バージョン・鮮度・直近利用の1枚。まずこれを見る。
- 週次レポートは S3 `ops/report/usage_report.html`（数字のみ・検索語も利用者ハッシュもなし。利用者の表＝サービス別の利用者・新規と継続・偏り・運営者を除く数字）。手元閲覧＝`aws s3 cp` で取得。
  Access 内配信（URL で見る）は外部提示が必要になった時に stats ホストへ静的パスを追加する（運用設計 §4.2 の配信手段決定・2026-08-28）。

## 7. 依存の更新（ロック再生成 → PR → ゲート・2026-09-04 B17 (1)）

箱に入る Python 依存は `deploy/requirements/lock-<extras>.txt`（版＋sha256）だけ＝bootstrap ③が `pip install --require-hashes -r` で導入し、
本体は `-e . --no-deps`。**pyproject を変えても、ロックを作り直さない限り箱は変わらない**（ロックに無い版は入らない・ハッシュ不一致は落ちる）。
ロックは箱と同じ Linux x86_64／py3.12 で解決する必要があるため docker（`python:3.12-slim` ダイジェスト固定）で生成する。

```bash
# ① 再生成（既存の版は保つ。全更新は --upgrade・1 つだけは --upgrade-package <name>）。docker が要る
bash deploy/scripts/lock_deps.sh [--upgrade]
# ② ローカルで同じロックから env を作り直す。作業用 PC は lock-all.txt（箱のロック＋取込・回答まで含む評価の extras〔ingest・
#    companies〕・共通の版は箱のロックと同じ＝B46 (b)・2026-10-19）。★Mac（arm64）には torch +cpu が無い＝torch だけ PyPI の同版
#    （ハッシュ付き）に置き換えた変種を派生して入れる（他のピンとハッシュはロックと同一）
conda create -y -n polyarchy-lock -c conda-forge --override-channels python=3.12
python deploy/scripts/lock_mac_variant.py deploy/requirements/lock-all.txt    # → /tmp/lock-mac.txt
P=$HOME/miniconda3/envs/polyarchy-lock/bin            # 自分の conda の envs パスに読み替え
$P/pip install --require-hashes -r /tmp/lock-mac.txt    # 本体の依存の宣言が変わった後（例：mcp の版）は「polyarchy … requires …」の警告が出る＝次の行で消える
$P/pip install --no-deps --no-build-isolation -e .       # ★パッケージが増えた git pull の後も入れ直す（§5・配る前の確認が照合する）
# ③ 全ゲート＋配布＝release.sh を「PATH の python をロック env に向けて」回す（ロックとの一致は release.sh の配る前の確認が照合する）。数値が README「品質の担保」と一致することが採否の基準。
#    依存を上げて数値が動いたら**上げずに現行版で固定**（目的は固定であって更新ではない）
PATH=$P:$PATH bash deploy/scripts/release.sh staging
```
- 箱に入るのは箱のロック（lock-recommendations-stats.txt）だけ＝生成 AI の API のクライアント（anthropic・llama-index の LLM／OpenAI 系）は入らない。
  作業用 PC の env に ingest の extras が要るのは取込（分野タグ判定・チャンク境界検出）と回答まで含む評価（`eval/full.py`）だけ。
- 変更は PR（ロック 3 本＋pyproject）でレビュー＝ゲート全 PASS が採否（CI 化 §2.5 で自動化）。箱は release.sh → 自動適用でロックどおりに入れ替わる。
- 切り戻し（apply の自動 fail-back）も前回 tar のロックで再解決する。ロック導入前（2026-09-04 以前）の tar へ戻る場合だけ下限指定の解決になる。
- 出所の固定は Qdrant バイナリ（bootstrap ⑥b の sha256）と同じ思想。モデルの重み（HF）は commit を固定済み（`polyarchy_retrieval/models.py` の `PINNED_REVISIONS`・
  読み込みと事前 DL の両方・2026-10-09）。重みは配布物に含める（2026-10-10・B17 (3)）＝release.sh の上りが手元の HF キャッシュ（ゲートが測った重み）を
  S3 `data/models/<org>--<name>/<commit>/`＋SHA256SUMS に置き、箱は bootstrap ⑤ が照合して置く・サービスと箱上 smoke は `HF_HUB_OFFLINE=1`。
  モデルの版を上げるときも手順は同じ（`PINNED_REVISIONS` を直す→ゲートが新しい重みを手元に落として測る→上りが新しい commit を置く）。
  ★適用のログに「★S3 に … の重みが無い＝HF から取る」が出たら、上りが重みを置けていない（手元の HF キャッシュに無い等）＝上りのログを見る。
  ★最初の配布だけ、上りと箱の取得が約 1.6 GB 増える（同じ重みは 2 回目から送り直さない）。箱では旧い取得物（HF の blobs）が残る＝容量に余裕があるので消さない。
