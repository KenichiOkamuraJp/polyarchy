# Polyarchy — AWS 運用 runbook（staging 稼働中 / prod 転写手順）

> **状態：正典（AWS runbook・§9.5 が入口ガード/ログ保持の正典）。** 障害対応の一次手順は RUNBOOK_OPS.md。

本書やコードのコメントにある「仕様§x」「ロードマップ§y」「§33.x」等は、初期開発時の設計文書（公開リポジトリに含めていない）の節番号。
公開リポジトリだけで読む人は、本書と [`PROD_MIGRATION.md`](PROD_MIGRATION.md) を自己完結の手順として読めばよい。

> **状態（2026-08-28）**：初代 staging（v5/Chroma・PoC 試行が乗っていた箱）は **2026-08-28 に破棄**（
> S3/SSM/Cloudflare/IAM ロールは保全）。建て直しから箱も**本線 v7/Qdrant**（`COLLECTION_NAME=policy_claims_v7`・
> `VECTOR_BACKEND=qdrant`＝terraform 既定）。bootstrap ⑥b が Qdrant（版・sha256 固定）を導入し qdrant.service(:6333) で常駐、
> `data/qdrant` は S3 の data 同期で配布。切り戻しは Qdrant 内の v6（`RECOMMENDATIONS_COLLECTION_NAME`）＝
> Chroma 経路はバッチ2 段4（2026-08-28）で全廃（S3 `data/chroma/` は v5 データ保管のみ）。
> `deploy/FREEZE` は 2026-08-28 に解除（PoC 停止・箱破棄に伴い削除）。
> stats（:8766）は**設置済**（2026-08-28・A1）。**2026-09-02 から両サービスとも個人認証（案 B・AuthKit）でログイン必須**（authless は 2026-08-28〜09-01 の暫定）。運用一式（health timer・CW Logs/アラーム・SNS・週次レポート）も稼働＝[`RUNBOOK_OPS.md`](RUNBOOK_OPS.md)。
> 合格ラインの正典は**ルート [README](../README.md)「品質の担保」**（本番経路＝service＋diversify・`--eval-set both`。数値は本書に写さない）。v5 を測る場合の旧ライン（88.7%／0.728・3.87）は §6 に注記。

---

## 0. 構成（何がどう動くか）

```
利用者(Claude) / ブラウザ
      │  https://recommendations.<domain><秘密パス>   https://stats.<domain><秘密パス>
      ▼  (IdP ログイン必須〔案B〕＋秘密パス＋レート制限。web は 2026-09-02 廃止)
  Cloudflare Edge ──(Tunnel: outbound QUIC 7844)──┐   ※インバウンドは一切開けない
                                                  ▼
  EC2 t3.xlarge (東京・Ubuntu22.04・gp3 100GB暗号化・IMDSv2)
    ├ systemd polyarchy-mcp   :8765  ← 主役（層公開固定・秘密ゼロ・秘密パスで待受）
    ├ systemd qdrant          :6333  ← ベクトルDB（v7 本線・127.0.0.1 のみ・storage=data/qdrant）
    ├ systemd polyarchy-web   :8502  ← **廃止（2026-09-02）**＝unit はリポ残置・箱には無い
    ├ systemd cloudflared            ← 入口（手動 start：カットオーバー時）
    ├ systemd polyarchy-stats :8766  ← 統計参照DB（認証必須・稼働中）
    ├ systemd polyarchy-companies :8767  ← 企業情報DB（opt-in＝ENABLE_COMPANIES_APP・モデル不要・2026-09-22 配線・有効化は RUNBOOK §5「サービスを足す」）
    ├ systemd polyarchy-fuelsync.timer → 捕捉ログ(燃料)を S3 へ（毎時・recommendations と stats と companies）
    ├ systemd polyarchy-logprune.timer → 捕捉ログの30日超過分を削除（毎日・recommendations と stats と companies）
    ├ systemd polyarchy-health.timer   → 毎分 /healthz → CloudWatch metric（運用設計 §1）
    ├ systemd polyarchy-usagereport.timer → 週次利用レポート → S3 ops/report/（月曜 09:20 JST）
    ├ systemd polyarchy-dashboard.timer   → 運用ダッシュボード → S3 ops/dashboard/（毎時）
    ├ systemd polyarchy-updatecheck.timer → 提言新着＋統計取得元の変化を検知（毎日 06:10 JST）
    └ systemd polyarchy-dataapply.timer   → S3 release/data.json を検知→自動適用・失敗時は自動切り戻し（15 分毎・RUNBOOK §5）
  IAM instance role: S3 は最小権限（data/・code/・release/ は読取のみ／書込は ops/ と query_log/ だけ・Delete 無し＝2026-09-04 B17 (2)）・SSM 読取・CloudWatch。秘密は SSM Parameter Store（.env は運ばない）
  S3: code/polyarchy.tar.gz ＋ data/{qdrant(v7 本線),chroma(v5 データ保管のみ・経路は全廃),bm25,pdfs,eval,catalog,query_log,stats/,companies/}
      ＋ release/data.json（リリースマニフェスト）＋ ops/{dashboard,report}/（運用の可視化）。監査用証跡は別バケット（trail.tf） ＋ data/stats/{registry,values,eval,query_log}（versioning ON）
```

ファイル：
- **`PROD_MIGRATION.md`** … 導入団体 AWS 本番移行の**独立手順書**（アカウント作成→Cloudflare→deploy→go-live→ロールバック）。
- **`env/*.env.example`** … 環境ごとの非秘密パラメータ（staging/prod の差はここだけ）。`cp` して使う。
- **`scripts/deploy.sh`** … operator 手順を1本化（secrets→bucket→upload→apply）。`bash scripts/deploy.sh <env> [phase]`。
- **`scripts/rollback.sh`** … ロールバック/現状復帰（status／entry-to-mac／entry-to-ec2／snapshot／destroy）。
- **`scripts/cloudflare-guard.sh`** … 入口ガード（秘密パス＋レート制限。IP 許可はオプション＝公開 DB では `IP_ALLOWLIST=off`・§9.5）を Cloudflare に冪等適用。
- **`scripts/prune_query_log.py`** … 捕捉ログの保持期間超過分を削除（30日・systemd タイマーから毎日・recommendations／stats／companies の 3 本）。
- **`scripts/access-oauth.sh`** …（案 A 資産・**現行未使用**＝2026-09-02 案 B 採択）Cloudflare Access アプリ（Managed OAuth）の作成/確認（公式コネクタ化）。
- **`RUNBOOK_OPS.md`** … 運用・障害対応の一次手順書（アラーム対応・復元・秘密再発行）。
- **`pages/`** … 公開ページ（利用規約・プライバシーポリシー・ドキュメント）＝Cloudflare Pages `docs.polyarchy.net`（旧 `docs.policy-database.com` エイリアスは 2026-09-02 撤去済）。
- `scripts/lib.sh` … env 読込・AWS/Terraform 橋渡し・各フェーズ関数（deploy.sh/rollback.sh/register-secrets.sh が source）。
- `scripts/register-secrets.sh` … §2 だけを単体実行する薄いラッパ。
- `scripts/upload_to_s3.sh` … コード tar ＋ データを S3 へ投入（deploy.sh の upload から呼ばれる／単体でも可）。
- `iam-operator-policy.json` … operator IAM ユーザに付ける絞り込みポリシー（PowerUser と併用・`<ACCOUNT_ID>` 置換）。
- `terraform/` … VPC/EC2/EBS/S3/IAM/SSM 権限（`*.tf`）＋ `user_data.sh.tftpl`（変数は env/*.env から `-var` で渡す＝tfvars は使わない）
- `bootstrap/bootstrap.sh` … conda/env・依存は**ロックから `--require-hashes`**（2026-09-04）・fugashi検証・HFモデル事前DL・S3データ取得・cloudflared・systemd
- **`requirements/lock-recommendations-stats.txt`・`lock-stats.txt`** … 依存ロック（版＋sha256・箱と同じ Linux x86_64／py3.12 で生成）。`build.in`＝ビルド時依存（setuptools/wheel）。**箱に入る Python 依存はここに書かれたものだけ**（bootstrap ③）。再生成＝`scripts/lock_deps.sh`（docker）→ ゲート → release.sh＝RUNBOOK §7
- `scripts/lock_deps.sh` … 依存ロックの（再）生成（`--upgrade` で更新・更新は PR＝ゲート経由）
- `bootstrap/prefetch_models.py` … `cl-nagoya/ruri-v3-310m` ＋ `hotchpotch/japanese-reranker-cross-encoder-xsmall-v1`
- `systemd/*.service|*.timer` … mcp / qdrant / web / stats / companies / cloudflared / fuelsync / logprune / health / usagereport
- `cloudflared/config.yml.example` … 入口設定の参考形（実体は bootstrap が SSM から生成）

---

## 1. 前提ツール（手元＝Mac）

```bash
brew install terraform awscli            # cloudflared は導入済（§33.3）
aws configure --profile polyarchy-staging   # 自分AWSのアクセスキー/リージョン ap-northeast-1
aws sts get-caller-identity --profile polyarchy-staging   # 疎通確認
```

**環境パラメータを用意**（スクリプトはこれを読む。staging と prod の差は原則このファイルだけ・秘密は含めない）：
```bash
cd deploy
cp env/staging.env.example env/staging.env
# 中身を確認（AWS_PROFILE / ENABLE_WEB_APP / TUNNEL_* / 秘密の在り処）。staging は既定値でほぼそのまま。
```
（変数の単一の真実は env ファイル。`deploy.sh` が `-var` で渡す。`terraform.tfvars` は使わない＝二重管理しない。）

---

## ★実行＝スクリプト1本（推奨・staging も prod も同じ）

§2〜§5 の operator 手順を env ファイル駆動でまとめた [`scripts/deploy.sh`](scripts/deploy.sh) がある。
**bash で走るので zsh の単語分割問題は起きない**。転写（staging→prod）はこの1本で完結する。

```bash
cd deploy
bash scripts/deploy.sh staging all
```

`all` ＝ **秘密登録 → S3バケット作成 → コード/データ投入(約 7GB) →（確認）→ terraform 本適用** を順に実行。
段階実行も可（初回は分けて様子を見ると良い）：

```bash
bash scripts/deploy.sh staging secrets   # §2 SSM 秘密だけ
```
```bash
bash scripts/deploy.sh staging upload    # §3 バケット作成＋S3投入だけ（時間の山）
```
```bash
bash scripts/deploy.sh staging apply     # §4 本適用だけ（プラン確認あり）
```

**prod 昇格は転写1コマンド**：`cp env/prod.env.example env/prod.env` を編集（導入団体プロファイル・
導入団体ゾーンの新トンネル・`ENABLE_WEB_APP=false`）してから：

```bash
bash scripts/deploy.sh prod all
```

> 以降の **§2〜§5 は、この `deploy.sh` が内部でやっていること**（＝手動で1歩ずつ実行する時の同等コマンド）。
> 仕組みの理解・個別デバッグ用に残す。通常はスクリプトを使えばよい。§6 以降（回帰ゲート／カットオーバー）は手動。

---

## 2. 秘密を SSM Parameter Store に登録（.env は箱に運ばない・仕様§4.1）

**通常は `bash scripts/deploy.sh staging secrets` で足りる**（[lib.sh](scripts/lib.sh) の `do_register_secrets`）。
以下は同等の手動コマンド。**prod では anthropic を登録しない**（MCPのみ＝秘密ゼロ）。

```bash
# ★zsh は変数を単語分割しないので AWS="aws --profile …" 方式は不可。export で既定にする。
export AWS_PROFILE=polyarchy-staging
export AWS_DEFAULT_REGION=ap-northeast-1
```
```bash
# cloudflared：トンネルの UUID と資格情報 JSON の中身を SSM へ（資格情報は登録後に作業用 PC から消す＝ルート CLAUDE.md §5）
aws ssm put-parameter --overwrite --type String --name /polyarchy/staging/cloudflared_tunnel_id \
  --value <TUNNEL_UUID>
aws ssm put-parameter --overwrite --type SecureString --name /polyarchy/staging/cloudflared_credentials \
  --value "file://$HOME/.cloudflared/<TUNNEL_UUID>.json"
```
```bash
# staging web の生成用（prod は不要）。.env から直接読ませて履歴に残さない
aws ssm put-parameter --overwrite --type SecureString --name /polyarchy/staging/anthropic_api_key \
  --value "$(grep '^ANTHROPIC_API_KEY=' <REPO>/recommendations/.env | head -1 | cut -d= -f2- | tr -d '\r\n')"
```

> ★秘密の値を runbook やシェル履歴に残さない。cloudflared 資格情報は `file://` で中身を読む。

---

## 3. バケットだけ先に作ってコード/データを投入（鶏卵回避）

EC2 の初回起動（user_data）は S3 のコードを取りに行くので、**先に S3 を用意して中身を入れる**。

```bash
cd deploy/terraform
terraform init
terraform apply -target=aws_s3_bucket_versioning.data   # バケット＋versioning だけ先に作る
BUCKET=$(terraform output -raw s3_bucket)

# コード tar ＋ データ（Qdrant 2.0G/PDF 2.0G 等）を投入。初回は時間がかかる。
BUCKET=$BUCKET PROFILE=polyarchy-staging bash ../scripts/upload_to_s3.sh
# → 末尾に eval_set.json の sha256 が出る。4a51f5f5… であること（バイト不変・仕様§4.2）を確認。
```

---

## 4. 本 apply（EC2 が立ち上がり user_data→bootstrap が走る）

```bash
terraform apply           # VPC/SG/EC2/IAM/残りの S3 設定
terraform output          # instance_id / ssm_start_session / next_steps
```

---

## 5. bootstrap の監視（SSM Session Manager で箱に入る・SSHは無い）

```bash
aws --profile polyarchy-staging ssm start-session --target <instance_id>
# 箱の中で：
sudo tail -f /var/log/polyarchy-bootstrap.log /var/log/cloud-init-output.log
# 「[bootstrap] 完了」まで待つ（conda＋torch(CPU)＋モデルDL＋データ同期で初回は十数分）
systemctl status polyarchy-mcp --no-pager
```

---

## 6. 回帰ゲート4種を「箱の上で」PASS（クレジット0・仕様§4.2）

v7/Qdrant はサーバ型でロック競合は無く、**qdrant.service は動かしたまま**測る。
アプリ常駐（mcp/stats）は捕捉ログ汚染と CPU 取り合いを避けるため止める。捕捉は temp ログへ。

```bash
sudo systemctl stop polyarchy-mcp polyarchy-stats 2>/dev/null || true
systemctl is-active qdrant   # active であること（v7/Qdrant で測る前提）

sudo -u polyarchy -H bash -lc '
  cd /opt/polyarchy/polyarchy
  export PYTHONPATH=/opt/polyarchy/polyarchy HF_HOME=/opt/polyarchy/models
  export COLLECTION_NAME=policy_claims_v7 VECTOR_BACKEND=qdrant TOKENIZERS_PARALLELISM=false
  export POLYARCHY_QUERY_LOG=/tmp/gate.jsonl      # 燃料を汚さない
  # 検索ゲートは API キー不要（検索は ruri ローカル＝2026-09-19 にキーのガードを外した）。下の 2 行は、SSM にキーを登録している
  # 環境で回答まで含む評価（eval/full）も回す場合だけ要る＝無ければ空のままでよい。
  # ※HF_HUB_OFFLINE は付けない：ruri(SentenceTransformer) が完全オフラインだと config 解決に失敗する
  #   （重みはprefetch済で大半キャッシュ利用・箱は egress あり）。ゲートの実行は `scripts/release.sh` が自動化済。
  export OPENAI_API_KEY="$(aws ssm get-parameter --region ap-northeast-1 --with-decryption --name /polyarchy/staging/openai_api_key --query Parameter.Value --output text 2>/dev/null || true)"
  export ANTHROPIC_API_KEY="$(aws ssm get-parameter --region ap-northeast-1 --with-decryption --name /polyarchy/staging/anthropic_api_key --query Parameter.Value --output text 2>/dev/null || true)"
  P=/opt/miniconda/envs/polyarchy/bin/python
  echo "── retrieval-only ──"; $P -m recommendations.eval.eval --retrieval-only --eval-set both
  echo "── filter-eval ──";   $P -m recommendations.eval.eval --filter-eval
  echo "── multistage_eval ──"; $P -m recommendations.eval.multistage_eval
  echo "── mcp_smoke ──";      $P -m recommendations.eval.mcp_smoke
'

sudo systemctl start polyarchy-mcp polyarchy-stats
```

**合格ライン**：正典は**ルート README「品質の担保」**（v7/Qdrant・バッチ2 段1〔2026-08-28〕から
検索系は本番経路＝`PolicySearchService`＋diversify=True で測る。retrieval は `--eval-set both` が正典）：
- `--retrieval-only --eval-set both` … hit@5／MRR がアンカー非劣化
- `--filter-eval` … 全問正解 ＋ 層ゲート PASS
- `multistage_eval` … 網羅 / 集約棄却が基準どおり
（基準値はルート [README](../README.md)「品質の担保」と `scripts/release.sh` の既定値が正。）
- `mcp_smoke` … PASS（stdout クリーン・layer 引数なし）

（旧 v5/Chroma の測定線＝hit@5 88.7%／MRR 0.728 は歴史記録。Chroma 経路はバッチ2 段4〔2026-08-28〕で
全廃＝切り戻しは Qdrant 内 `COLLECTION_NAME=policy_claims_v6`。）

数値がズレたら**データ/モデル/依存の差**を疑う（HFモデルのリビジョン・fugashi辞書・torch版）。

---

## 7. Tunnel カットオーバー（Mac→EC2）＝安全順序を厳守

> **歴史的経緯（初期移行期の手順）。**作業用 PC が元ライブだった時期に、同じトンネルを EC2 へ移したときの記録。
> 現行の規約は「箱のトンネル資格情報を作業用 PC に置かない・PC で `cloudflared tunnel run` しない」（ルート CLAUDE.md §5）。
> 新しく環境を建てる人は PROD_MIGRATION.md §5.3（箱で cloudflared を起動するだけ）に従う。

**二重 origin（Mac と EC2 が同一トンネルに同時接続）を避ける**。順序（ロードマップ§2.4・§33.4）：

1. EC2 のアプリ面が健全なことを確認（§6 のゲート PASS＋`systemctl status polyarchy-mcp`）。
2. **Mac 側の cloudflared を停止**（`Ctrl+C` か `sudo cloudflared service uninstall`）。
3. EC2 で cloudflared を start（ここで初めて入口が EC2 に向く）：
   ```bash
   sudo systemctl enable --now cloudflared
   sudo journalctl -u cloudflared -f     # Registered tunnel connection が出れば接続
   ```
4. **公開URL越しに検証**（毎回やる。認証 ON では Bearer なし＝401 が「正」・手順の正典は PROD_MIGRATION §5.2）：
   ```bash
   curl -sS https://recommendations.polyarchy.net/healthz          # 200
   curl -sS https://recommendations.polyarchy.net/.well-known/oauth-protected-resource | head -c 200   # PRM 200
   curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://recommendations.polyarchy.net<秘密パス> \
     -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'   # 401 が正
   ```

---

## 8. Phase A 完了条件（仕様§4.2 / ロードマップ§2.6）

- [ ] 回帰ゲート4種が **AWS 上で** PASS（§6）
- [ ] 公開URLで実操作（MCP＝Claude Code から接続して `list_orgs`/`search_policy_docs`/`sweep_policy_docs`）
- [ ] **Mac を落としても稼働継続**（EC2＋Cloudflare だけで成立）
- [ ] 燃料バックアップ（`polyarchy-fuelsync.timer`）が S3 に効いている：
      `aws s3 ls s3://$BUCKET/data/query_log/`

---

## 9. prod 昇格（導入団体 AWS で MCP を再現）＝同一 .tf を転写

> **アカウント作成からの独立した完全手順は → [`PROD_MIGRATION.md`](PROD_MIGRATION.md)**。以下はその要約。

**差し替えるのは 3 点だけ**（仕様§3.1・ロードマップ§3）：アカウント/プロファイル、`enable_web_app=false`、
入口（導入団体ゾーン＋新トンネル＋秘密パス/レート制限）。検索の質は staging の同一 built index で担保済。

1. `aws configure --profile adopter-prod`（導入団体 AWS へのアクセス手段＝IAM ユーザー／SSO／ロール委任のどれか＝PROD_MIGRATION §1.1）。
2. `env/prod.env` を用意（`environment=prod` / `aws_profile=adopter-prod` /
   `enable_web_app=false` / `tunnel_hostname_mcp=recommendations.<導入団体の独自ドメイン>`）。
   **state を分ける**：`terraform workspace new prod` か、prod 用ディレクトリ/backend key で別 state に。
3. SSM（prod）に **cloudflared＋auth_issuer/auth_aud_***（案 B 認証）を登録。**anthropic/openai は
   serving 用には登録しない**（＝ランタイム秘密ゼロ。品質検査用キーは別途＝PROD_MIGRATION §5.2）。
4. `scripts/upload_to_s3.sh` を prod バケットへ（同一 built index を配る）。`terraform apply`。
5. **Cloudflare（導入団体ゾーン）側**で②③を設定（`cloudflare-guard.sh apply`・**`IP_ALLOWLIST=off`**）：
   - **② 秘密パス**：SSM `/polyarchy/prod/mcp_http_path` を発行し、既定パス以外をエッジ遮断（除外 3 種込み）。
   - **③ レート制限**：per-IP **600–1200 req/分**（多段は1問で10-30検索のため高め）。
   - ★IP 許可リストは作らない（2026-08-27/28 廃止＝PROD_MIGRATION §2.3。egress IP/VPN の事前確認も不要に）。
6. prod 検証：`mcp_smoke`（箱上）＋公開URL越し `list_orgs`/`search_policy_docs`＋**未認証/境界の curl**。

---

## 9.5 入口ガードとログ保持（PoC 運用の取り決め・2026-08-05 適用）

### 入口＝3層＋層公開固定
[`scripts/cloudflare-guard.sh`](scripts/cloudflare-guard.sh) が Cloudflare に冪等適用する（`status|apply [strict|connector]|remove|test`）。

| 層 | 内容 |
|---|---|
| ① 秘密パス | `MCP_HTTP_PATH=/mcp-<128bit乱数>` 以外のパスをエッジで block（値は SSM のみ・repo に持たない。除外＝/.well-known/・/cdn-cgi/・/healthz）|
| ② レート制限 | per-IP。無料プランは 10 秒窓のため 1200/分 → **200 req/10s** に自動換算 |
| ③ 層公開固定 | 従来どおり（機密は物理遮断・`search_api`）|
| （廃止）IP 許可 | **2026-08-27/28 に廃止**＝ChatGPT egress は列挙不能・OAuth のディスカバリ/ログインはレンジ外から来る。guard は `IP_ALLOWLIST=off` で適用（経緯の正典＝[`PROD_MIGRATION.md`](PROD_MIGRATION.md) §2.3）|

> **★ コネクタはベンダー（Anthropic/OpenAI）のサーバから接続される**＝送信元 IP で組織を絞れない。組織/個人限定が要る場合は**非公開ホスト＋Access OAuth**（2026-08-28 に全経路実証済み。現行は案 B＝[docs/個人認証_案B設計.md](../docs/個人認証_案B設計.md)）。導入団体へ必ず開示すること。

秘密パスの再発行（URL 漏洩時の締め出し）：
```bash
aws ssm put-parameter --overwrite --type SecureString --name /polyarchy/staging/mcp_http_path \
  --value "/mcp-$(openssl rand -hex 16)"
```
→ 箱で `bootstrap.sh` を再走行（または `/etc/polyarchy/mcp.env` を更新して `systemctl restart polyarchy-mcp`）
→ `bash scripts/cloudflare-guard.sh apply connector` で WAF ルールも更新 → 新URLを利用者へ再配布。

### 捕捉ログの保持＝30日で自動削除（**この節が正典**。他文書はここへリンク）
プライバシーポリシーの約束（30 日）を機械で担保する。**箱と S3 の両方・recommendations と stats の両方**で担保する。日数を揃える場所＝`systemd/polyarchy-logprune.service`（recommendations・stats の 2 行・`LOG_RETENTION_DAYS`）・`terraform/storage.tf`（`query-log-retention-30d`・`stats-query-log-retention-30d`）・`pages/privacy.html`（「30 日」の記述）・`PROD_MIGRATION.md` §6.5：
- **箱**：`polyarchy-logprune.timer`（毎日）→ [`scripts/prune_query_log.py`](scripts/prune_query_log.py) が
  JSONL の `ts` を見て 30 日超過行を削除（原子的差し替え・削除中の追記も保全）。日数は
  `systemd/polyarchy-logprune.service` の `LOG_RETENTION_DAYS`。
- **S3**：ライフサイクル `query-log-retention-30d`（`terraform/storage.tf`）＝ `data/query_log/` を
  30 日で失効（versioning ON のため**旧版も 30 日**）。

確認・手動実行：
```bash
systemctl list-timers polyarchy-logprune.timer          # 次回実行
sudo -u polyarchy env DRY_RUN=1 /opt/miniconda/envs/polyarchy/bin/python \
  /opt/polyarchy/polyarchy/deploy/scripts/prune_query_log.py    # 件数だけ確認
aws s3api get-bucket-lifecycle-configuration --bucket <bucket>   # S3 側の確認
```

> **★運用上の含意**：捕捉ログ（燃料）は**30日で消える**。Phase 12 の週次トリアージ
> （triage→gold→`evalset_gate`→ユーザー由来 eval へバイト不変追記）を**30日以内に回すこと**。
> 恒久的に残るのは eval セット（`data/eval/`）であり、生ログは一時的な原資と位置づける。

---

## 10. 落とし穴チェックリスト（ロードマップ§6）

- [ ] **fugashi 辞書**（unidic-lite）を導入・`Tagger()` 疎通（bootstrap ④で検証済）。
- [ ] **HFモデル事前DL**して EBS 固定（bootstrap ⑤。★`HF_HUB_OFFLINE` は units から撤去済＝完全オフラインは ruri の config 解決を壊す・§6 注記）。
- [ ] **CPU 版 torch**（GPU 無し箱。既定 linux wheel は CUDA 同梱で巨大）＝ロックが `+cpu` 版を指す（`lock_deps.sh` が download.pytorch.org/whl/cpu を extra index に）。
- [ ] **依存はロックからのみ**（`--require-hashes`・2026-09-04）＝pyproject を変えたら `lock_deps.sh` → ゲート → release。ロック無しの tar は bootstrap ③ で止まる。
- [ ] **Tunnel cred 移設で DNS 変更は不要**（UUID 向きのまま）。**Mac 側は必ず停止**（二重 origin 回避）。
- [ ] （web 廃止 2026-09-02＝以下は chat_app 残置コード向け）**`st.table`/`st.dataframe` 不可**（pyarrow/mimalloc が Streamlit スレッドで SIGSEGV・§32.3）→ Markdown 表（コード済）。
- [ ] MCP streamable-http の **`transport_security` off**（421＝DNSリバインディング保護・§33.5。既定 off）。
- [ ] **secrets を `.env` で運ばない**（SSM）。**フォルダ/tar に `.env` を混ぜない**（upload_to_s3.sh は除外済）。
- [ ] **正典 eval バイト不変**（sha256 `4a51f5f5…`）・**prod v5 不変**・**層公開固定**。

---

## 11. 日常運用（箱の中・SSM 経由）

```bash
systemctl status polyarchy-mcp polyarchy-stats qdrant cloudflared --no-pager
journalctl -u polyarchy-mcp -f            # MCP のログ（日本語）
journalctl -u cloudflared -f              # 入口
sudo systemctl restart polyarchy-mcp      # 非常時の手動復旧（定常のコード更新は release.sh → 自動適用。下の「コード/データ更新」）
systemctl list-timers polyarchy-fuelsync.timer   # 燃料バックアップの次回
```

**コード/データ更新（定常）**＝`bash deploy/scripts/release.sh <env>`（ゲート全 PASS → upload → マニフェスト）
だけ。箱の `polyarchy-dataapply.timer` が **code tar の再展開・データ同期・再起動・smoke・失敗時の自動切り戻し**まで
無人で行う（2026-09-03〜・RUNBOOK §5）。以下の手順は**非常時（timer が動かない・apply 自体が壊れた）**のみ：

手元で `upload_to_s3.sh` → 箱で **まずコード tar を再展開**（★bootstrap は
コードを更新しない＝tar 展開は user_data（初回起動）と apply のみ。2026-08-28 バッチ2 段3 で実測＝bootstrap
再走行だけだとデータのみ新しく、旧コードでゲートが走る）：
```bash
sudo aws s3 cp s3://<bucket>/code/polyarchy.tar.gz /tmp/ --region ap-northeast-1
sudo tar -xzf /tmp/polyarchy.tar.gz -C /opt/polyarchy && sudo chown -R polyarchy:polyarchy /opt/polyarchy/polyarchy
```
→ `sudo bash /opt/polyarchy/polyarchy/deploy/bootstrap/bootstrap.sh`（冪等・データ再 sync・依存再解決・
サービス再起動）。コードのみの更新なら bootstrap を省き `systemctl restart polyarchy-mcp` 等でも可。

**燃料の週次トリアージ**＝`bash deploy/scripts/triage.sh <env>`（S3 の燃料→両サービスの草稿・読み取りのみ・運用者）→
開発者が採否（stats `--accept`／recommendations scaffold→careful gold→`evalset_gate`→ユーザー由来へバイト不変追記）。
箱では `data/query_log/queries.jsonl` が原資、S3 に版付きで保護される（RUNBOOK §5）。
