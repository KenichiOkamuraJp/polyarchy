# 本番移行手順（導入団体 AWS / prod）— アカウント作成から go-live まで

> **状態：設計（安定リファレンス・prod 転写の手順正典）。** 稼働中の staging を導入団体 AWS へ転写する手順。

**独立した実行手順書**。staging（自分AWS）で確立・検証した構成を、導入団体 AWS へ**同一スクリプトで転写**する
（本書の「仕様§x」は初期開発時の設計文書の節番号＝公開リポジトリに含めていない。全体の runbook は [`README.md`](README.md)、本書は prod 専用の手順で自己完結）。

> **prod の性格（仕様§2.3）**：導入団体の箱 は **MCP サーバだけ**動けば良い（Web 不要）＝**ランタイム秘密ゼロ**
> （検索は ruri ローカル・生成は各利用者の Claude）。入口は**導入団体の独自ドメイン＋秘密パス＋レート制限**（★IP 許可方式は 2026-08-27/28 に廃止＝§2.3）。
> staging と prod の差は `env/prod.env` に集約され、実行は `deploy.sh prod all` の一発。

> **前提**：staging が動いていること（回帰ゲートで検索の質は担保済＝prod では再測不要）。手元の Mac に
> `terraform` / `awscli` / `session-manager-plugin` 導入済（[README §1](README.md)）。cloudflared も導入済。

---

## 0. 全体像（prod で新しく要るものだけ）

> **凡例**：本書は提供の形②（導入団体の環境での運用）への展開手順。役割（導入団体・PdM・開発者・運用者・監査人）と用語（箱・燃料・ゲート・smoke・PRM・三点一致）の定義＝[docs/用語と役割.md](../docs/用語と役割.md)。

| 要素 | prod で用意するもの |
|---|---|
| AWS アカウント | 導入団体 AWS＋**operator IAM ユーザー**（`adopter-deployer`）へのアクセス |
| Cloudflare | **導入団体の独自ドメインのゾーン**＋**新トンネル**＋**秘密パス**＋**レート制限**（IP 許可は廃止済） |
| IdP（個人認証・案 B） | **導入団体自身の WorkOS アカウント**（名簿限定の入口）＝§2.5。②導入団体展開は**認証必須**（2026-09-02 段3方針＝stats/recommendations 両方に点ける） |
| 設定ファイル | `env/prod.env`（profile / 新トンネル / MCP・stats ホスト名 / `ENABLE_WEB_APP=false` / `AUTH_*` / `ESTAT_SOURCE`） |
| 秘密（SSM） | serving 用は **cloudflared 資格情報のみ**（サービングはキーレス維持）。**品質保持（データ追加時の eval）用に導入団体名義の OpenAI/Anthropic キー**を別途 SSM へ（serving ユニットは読まない・§5.2） |
| データ | staging と**同一 built index**（S3 経由） |

**同一なもの（転写）**：Terraform 定義・bootstrap・systemd(mcp/cloudflared)・アプリコード・検索構成・eval 正典。

---

## 0.5 リポジトリの複製と作業環境（導入団体側・2026-09-17 追記）

prod の作業は**導入団体のアカウント・導入団体の PC・導入団体のリポジトリ**で行う（アカウント所有の原則＝AWS・Cloudflare・IdP と同じ）。
上流（公開リポジトリとその開発者の環境）とは、複製の 1 回を除いて交わらない。

**順序＝① 公開リポジトリを複製 → ② prod 転写**。

### 0.5.1 複製（1 回だけ）

- **複製元は公開リポジトリ**（①で公開したもの）。誰でも LICENSE の条件で複製できるものを、導入団体が自分で複製する＝利用の根拠は LICENSE だけで足りる。
- **GitHub の fork 機能は使わない**（fork は元リポジトリとの関係が GitHub 上に残る）。独立した複製にする：
  ```bash
  git clone --mirror <公開リポジトリ>  polyarchy.git
  cd polyarchy.git && git push --mirror <導入団体の GitHub 組織の空の Private リポジトリ>
  ```
  公開リポジトリが持つ履歴をそのまま含める（公開時に履歴を畳んだ場合、文書中の「削除＝git 履歴で参照可」はその範囲でのみ有効）。
- 複製後、導入団体側のクローンに**元リポジトリを remote として登録しない**。以後は独立に保守する＝元リポジトリの更新の取り込みも、元リポジトリへの還元も**運用としては行わない**
  （将来取り込みたくなった場合は、公開リポジトリから LICENSE の条件で・導入団体の判断で。還元も任意）。
- 複製にリポジトリ root の `LICENSE`（Apache-2.0）と `NOTICE`（© 表示）が含まれていることを確認する（2026-09-17 設置）。

### 0.5.2 導入団体の PC の準備

| 項目 | 内容 |
|---|---|
| アカウント | GitHub・AWS・Cloudflare・IdP・Claude Code とも導入団体名義（費用も導入団体）。個人のアカウントでログインしない |
| git の名義 | `git config user.name / user.email` を導入団体のアカウントに（コミットの名義が作業の帰属の記録になる） |
| OS | スクリプトは bash＋macOS／Linux 前提。Windows なら WSL2（Ubuntu）の中にクローンして作業する |
| ツール | 冒頭「前提」のとおり（terraform・awscli・session-manager-plugin・cloudflared）＋Python 3.12 環境（依存はロックファイルから＝RUNBOOK §7。Mac は §7 ② の変種＝`lock_mac_variant.py`）。配布は開発用と分けたクローンから（RUNBOOK §5「配布用のクローンを開発用と分ける」）。`release.sh` はこの環境を PATH の先頭に通して実行 |
| `.mcp.json` | `.mcp.json.example` を複製して `<PYTHON>`・`<REPO>` を自分の環境のパスに（ローカル stdio で使う場合のみ・git 外） |
| 最初に読むもの | ルート [CLAUDE.md](../CLAUDE.md)（記憶ゼロのセッションの入口・運用地雷）→ 本書 |

### 0.5.3 持ち込まないもの（複製では運ばれない＝それで正しい）

| もの | 扱い |
|---|---|
| 秘密（AWS 資格情報・`~/.cloudflared`・Cloudflare／IdP のトークン・terraform state・`stats/.env` の e-Stat appId） | **コピーしない。導入団体名義で発行し直す**（§1・§2・§2.5・e-Stat は本人登録）。staging のトンネル資格情報を持ち込むと staging の配信が分流する |
| データ（PDF・索引・統計値＝約 7GB） | git 外。§0 の表のとおり built index を S3 経由で渡す（§4）。以後の更新は導入団体の環境で |
| 開発者個人のメモ・AI の記憶 | git 外・PC ごと。運用に必要な事項は git の文書（ルート CLAUDE.md・RUNBOOK・本書）に書いてある |
| staging の実利用ログ | 持ち込まない（30 日で消える燃料。必要な問は評価セットに昇華済み） |

---

## 1. 導入団体 AWS アカウントへのアクセスを確立

### 1.1 アクセス手段（いずれか。導入団体の運用に合わせて選ぶ）
- **(A) IAM ユーザー＋アクセスキー**（最短）：導入団体アカウントの管理者に `adopter-deployer` を作ってもらう。
- **(B) IAM Identity Center (SSO)**：組織で SSO 運用なら `aws sso login` で短命credential（推奨・鍵が手元に残らない）。
- **(C) クロスアカウント・ロール委任**：自分のアカウントから導入団体のデプロイ用ロールを AssumeRole。
- → **どれで入るかは導入団体側の運用に合わせて確定**。以降は (A) を例に記述。

### 1.2 operator IAM ユーザーの権限（staging と同型）
`adopter-deployer` に付与：
1. AWS 管理ポリシー **`PowerUserAccess`**（IAM 以外を許可）。
2. インラインポリシー **`polyarchy-iam-manage`**＝[`iam-operator-policy.json`](iam-operator-policy.json) の内容。
   **`<ACCOUNT_ID>` を導入団体アカウントの12桁に置換**（`iam:TagInstanceProfile` 等を含む・staging で不足して
   apply が止まった教訓を反映済）。
3. ルートユーザーには **MFA を必ず**（日常使いしない）。CLI 専用ユーザーの MFA は任意。

### 1.2b 日常のデータ更新用の権限（権限 2 段の下段・稼働後）
稼働後の定型更新（`release.sh`）に要るのは S3 だけ＝[`iam-data-operator-policy.json`](iam-data-operator-policy.json)
（`data/`・`release/`・`code/` の Put/Delete/Get＋`ops/` の Get＋ListBucket。捕捉ログ `query_log/` は読めない）。
SSO の権限セットか IAM ユーザーのインラインに `<BUCKET>` を置換して貼る。上段（1.2＝PowerUser）は初回構築と障害対応のみ。

### 1.3 手元にプロファイルを設定
```bash
aws configure --profile adopter-prod       # 導入団体のアクセスキー / region= ap-northeast-1 / output= json
aws sts get-caller-identity --profile adopter-prod   # user/adopter-deployer とアカウントIDを確認
```

---

## 2. Cloudflare（導入団体ゾーン）を用意 ＝ ①入口 ②認証 ③レート制限

**すべて Cloudflare 側の設定**で、裏の EC2 に依存しない（仕様§2.5）。

### 2.1 ① 入口＝導入団体の独自ドメインのゾーン
- 導入団体の独自ドメインを Cloudflare に載せる（ネームサーバを Cloudflare へ／既に載っていれば不要）。
- MCP 用サブドメインを決める（例 `recommendations.<導入団体ドメイン>`）。**Web は無し**（prod は MCP のみ）。

### 2.2 新トンネルを作る（staging のトンネルとは別UUID）
```bash
cloudflared tunnel login                        # ブラウザで★導入団体ゾーンを承認
cloudflared tunnel create adopter-prod          # → 新UUID と資格情報 JSON のパスが出る（控える）
cloudflared tunnel route dns adopter-prod recommendations.<導入団体ドメイン>   # CNAME をトンネル宛に（stats.<導入団体ドメイン> も同様）
```
→ **新UUID**（例 `xxxxxxxx-...`）と `~/.cloudflared/<新UUID>.json` を控える（§4/§5 で使う）。

### 2.3 ② 入口の絞り＝秘密パス＋レート制限（★IP 許可方式は廃止・2026-08-27/28）
**IP 許可リストは作らない**。適用は [`scripts/cloudflare-guard.sh`](scripts/cloudflare-guard.sh) を
`IP_ALLOWLIST=off` で（秘密パス＋レート制限だけを冪等適用。除外 3 種＝/.well-known/・/cdn-cgi/・/healthz 込み）。
★guard の既定値は staging（`polyarchy.net`・`/polyarchy/staging/...`・profile `polyarchy-staging`）＝**prod では必ず上書き**：
```bash
export ZONE_NAME=<導入団体ドメイン> AWS_PROFILE_CF=adopter-prod \
       RL_HOSTS=recommendations.<導入団体ドメイン>,stats.<導入団体ドメイン>
SERVICE=mcp   MCP_HOST=recommendations.<導入団体ドメイン> SSM_PATH_PARAM=/polyarchy/prod/mcp_http_path   bash scripts/cloudflare-guard.sh apply
SERVICE=stats MCP_HOST=stats.<導入団体ドメイン>           SSM_PATH_PARAM=/polyarchy/prod/stats_http_path bash scripts/cloudflare-guard.sh apply
bash scripts/cloudflare-guard.sh status   # 同じ環境変数で
```
（Cloudflare API トークンは Keychain `cloudflare-api-token`＝README §9）

> **★廃止の経緯（実測 2 件）**：①ChatGPT コネクタ対応＝OpenAI の egress は 261 プレフィックスで変動し
> 列挙不能（2026-08-27）。②OAuth 化したサービスでは、ディスカバリ取得が Anthropic の公表レンジ外の
> egress から来て、ログインはブラウザが app ホストの /cdn-cgi/ を経由する＝IP 方式と OAuth は両立しない
> （2026-08-28・stats で実測）。→ 公開 DB の防御は**秘密パス＋レート制限**、組織/個人限定が要る場合は
> **外部 IdP による個人認証（案 B・§2.5）**で行う（Access OAuth＝案 A は 2026-09-02 に不採用・資産温存）。
>
> 以下の★は歴史的経緯（なぜ IP で絞れないか）として残す＝導入団体の情報システム部門への説明にそのまま使える。
> **★ブロック内の「Cloudflare Access の OAuth が本筋」は当時の判断＝現行の答えは §2.5（案 B）。**

> ### ★★ 重大な前提（2026-08-05 判明・設計変更あり）
> **Claude の「カスタムコネクタ」(claude.ai / Claude Desktop) は、利用者PCからではなく
> Anthropic のサーバから MCP サーバへ接続する**（[公式](https://platform.claude.com/docs/en/api/ip-addresses)：
> "Your MCP server must be reachable ... from Anthropic's IP ranges"）。
> → **送信元IPは利用者/組織のIPにならず、Anthropic の outbound レンジ `160.79.104.0/21` になる**。
>
> したがって「組織IPのみ許可」を素直に設定すると、**カスタムコネクタ利用者は全員 block される**。
> - `strict`（組織IPのみ）… **Claude Code など端末から直接叩く場合のみ**疎通。claude.ai/Desktop は不可。
> - `connector`（組織IP＋Anthropic レンジ）… claude.ai/Desktop で疎通するが、**Claude 経由なら
>   第三者も到達しうる**＝組織限定にはならない（非 Claude の直接アクセスは遮断できる）。
> - 真の組織/個人限定が要るなら **Cloudflare Access の OAuth（メール許可リスト）** が本筋
>   （過去に Claude 側 DCR 不具合 #410 で見送り → **2026-08-19 再検証で解消を確認**：Access「Managed OAuth」（2026-03 GA）＋
>   Claude `oauth_dcr` で DCR→同意→接続→refresh まで通った。当時の設計・適用記録は公開リポジトリに含めていない）。
> **この非自明な仕様は導入団体の情報システム部門へ必ず開示すること**（「IP 許可＝組織限定」と誤解されるため）。

- （経緯）当時は per-user ログイン無し。**現行（2026-09-02〜）は案 B で per-user ログイン必須**＝Claude Desktop／Claude Code／ChatGPT の 3 経路で検証済（§2.5）。
- ~~go-live 前提：導入団体の egress 固定 IP・VPN が full-tunnel か~~ → **不要（2026-08-28・IP 方式廃止に伴い消滅）**。

### 2.3b ★秘密パス（IP で組織を絞れないことの補完）
接続URLに推測不可能な文字列を入れる（`MCP_HTTP_PATH=/mcp-<128bit乱数>`）。**ホスト名と違い DNS/証明書に
現れない**ため探索されない＝実質的な合言葉。値は SSM `/{project}/{env}/mcp_http_path`(SecureString) に置き、
bootstrap が `/etc/polyarchy/mcp.env`(600) を生成、systemd が読む（**repo には値を持たない**）。

```bash
aws ssm put-parameter --overwrite --type SecureString \
  --name /polyarchy/prod/mcp_http_path --value "/mcp-$(openssl rand -hex 16)"
```
```bash
# ★stats も別の秘密パスを発行する（未登録だと stats が既定 /mcp で上がり、AUTH_AUD_STATS〔秘密パス込み〕と食い違って認証が通らない）
aws ssm put-parameter --overwrite --type SecureString \
  --name /polyarchy/prod/stats_http_path --value "/mcp-$(openssl rand -hex 16)"
```
発行した 2 つの値は env の `AUTH_AUD_MCP`／`AUTH_AUD_STATS`（`https://<ホスト><秘密パス>`）と WorkOS の Resource indicator に
**同じ文字列で**写す（三点一致＝§2.5）。e-Stat の appId（導入団体が本人登録で取得）は env の `ESTAT_SOURCE` から
`deploy.sh prod secrets` が SSM `estat_app_id` に登録し、bootstrap が箱の更新チェックに渡す（値は文書に書かない）。
→ `deploy.sh prod all` で反映。漏洩時は同じコマンドで**再発行するだけ**で締め出せる（要 URL 再配布）。
利用者への配布＝案 B 適用後は URL（秘密パス込み）が PRM にも載るため「合言葉」ではなく「探索されない住所」の位置づけ（下記★）＝名簿限定はログインが守る。配布手順書に URL を載せて構わない。

> **★認証（案 B）ホストでの位置づけの変化（2026-09-02）**：案 B を点けると、PRM
> （`/.well-known/oauth-protected-resource`・固定パス・認証不要）の `resource` に秘密パス込み URL が
> **公開される**（クライアントが接続 URL と resource の一致を検証する仕様上、隠せない）＝**もはや「秘密」ではない**。
> それでも維持する（2026-09-02 判断）——残る効能は「PRM を読まない無差別スキャナを既定パス 403 でエッジで
> 落とす」ノイズ低減層＋pre-auth コードへの露出減。防壁の本体はログイン（IdP 認証）。
> 含意 2 つ：①漏洩時ローテーションは無意味化＝**一度決めたら固定**（三点一致〔WorkOS Resource indicator・
> `AUTH_AUD_<SVC>`・SSM〕を初回に合わせたら触らない）。②利用者への URL 配布も平文可（秘密パスを別経路で配るのは
> authless ホスト用の作法）。

### 2.4 ③ レート制限（多め・anti-flood）
`recommendations.<導入団体ドメイン>` に **per-IP 600〜1200 req/分**のレート制限を追加（オフィス＋在宅VPN が1 IP に
集約＋多段は1問で10〜30検索のため高め）。明らかな洪水だけ止める。

> prod は Web が無いので **Access ログインアプリ（自己ホスト）は作らない**。秘密パス＋レート制限が第一線。
> ~~MCP サーバ自体は authless~~ → **②導入団体展開では個人認証（案 B）を必須にする（2026-09-02 段3方針・§2.5）**。
> 秘密パス＋レート制限は認証の下の防御層としてそのまま維持（多層防御）。

### 2.5 ② 認証＝個人認証（案 B・外部 IdP）— stats / recommendations の両方に適用

正典＝[docs/個人認証_案B設計.md](../docs/個人認証_案B設計.md)。実装は転写されるコードに含まれ、
**env の `AUTH_*` を書くだけで有効化**される（空なら従来どおり authless）。prod で用意するのは IdP 側の設定のみ：

1. **導入団体自身の WorkOS アカウントを作成してもらい、その Production 環境を使う**
   （★アカウント所有の原則＝2026-09-02：導入団体展開は AWS・Cloudflare・WorkOS・Anthropic・OpenAI の**全外部サービスを
   導入団体自身のアカウントで**運用し、開発者個人のアカウントに依存しない。AuthKit は無償 100 万 MAU＝数千人規模でも無償枠内）。
   Production 環境は issuer ドメインを自分で決められる＝ここで prod の issuer が本決まり。設定は staging リハーサルの再現（要点は次項）。
2. **段2 の要点 3 つを Production 側で再設定**（★順序どおり）：
   - **JWT テンプレート `{"email": "{{user.email}}"}` を配布前に**（後から入れると user_hash が sub 代替→メール由来に
     切り替わり、捕捉ログ上は別利用者に見える）。
   - Connect で **DCR ON**。
   - **Resource indicator を登録し Default 指定**（Claude Code は resource パラメータを送らない＝Default 必須）。
3. **Resource indicator に登録する値＝各サービスの公開 URL（秘密パス込み）**：
   `https://stats.<導入団体ドメイン><秘密パス>` と `https://recommendations.<導入団体ドメイン><秘密パス>` の 2 本。
   ★**2 サービス×Default 1 つ問題は解消済み（staging リハーサル実測 2026-09-02）**＝Claude Code v2.1.258 は
   **非 Default 側のサービスにも認証成功**（resource パラメータを送るようになった＝RFC 8707 対応が入った模様。
   8 月の invalid_target 失敗は旧版）。Default 指定は旧クライアント保険として引き続き設定しておく（stats 推奨）。
4. **名簿限定の入口**：招待制／ドメイン制限／CSV 一括のどれで運用するかを導入団体側の名簿管理と合わせて決める。
   決まるまでは Production 環境をサインアップ制限なしで作らない（作る時点で制限を入れる）。
> **env の書き方（非自明・段2実測）**：`AUTH_AUD_<SVC>` の値は**トークンの `aud` と完全一致**で書く。
> AuthKit は resource indicator の値を `aud` に載せるため、**AUD＝上記 3 の resource URL（秘密パス込み）**になる。
> resource URL 自体は bootstrap が `TUNNEL_HOST_<SVC>＋SSM の秘密パス`から合成する（env に書くのは AUD だけ）。
> 秘密パスを差し替えたら **WorkOS の Resource indicator と `AUTH_AUD_<SVC>` も同時に更新**すること（三点一致）。

---

## 3. env/prod.env を用意

```bash
cd deploy
cp env/prod.env.example env/prod.env
```
編集（例）：
```bash
ENVIRONMENT=prod
AWS_PROFILE=adopter-prod
AWS_REGION=ap-northeast-1
ENABLE_WEB_APP=false                       # ★MCPのみ＝Anthropic 不要＝ランタイム秘密ゼロ
TUNNEL_ID=<§2.2 の新UUID>
TUNNEL_HOST_MCP=recommendations.<導入団体ドメイン>
TUNNEL_HOST_WEB=                            # 未使用
ENABLE_STATS_APP=true                       # ②導入団体展開は stats も出す（2026-09-02 段3方針）
TUNNEL_HOST_STATS=stats.<導入団体ドメイン>
CLOUDFLARED_CRED_FILE=$HOME/.cloudflared/<新UUID>.json
# OPENAI_SOURCE は書かない（prod は eval ゲートを常用しない＝キーレス維持）
# 個人認証（案 B）＝§2.5。AUD は aud と完全一致＝resource URL（秘密パス込み・三点一致に注意）
AUTH_ISSUER=<WorkOS Production の issuer URL>
AUTH_AUD_STATS=https://stats.<導入団体ドメイン><秘密パス>
AUTH_AUD_MCP=https://recommendations.<導入団体ドメイン><秘密パス>
```

---

## 4. デプロイ（同一スクリプトの転写）

```bash
cd deploy
bash scripts/deploy.sh prod all
```
`all` ＝ **秘密登録（cloudflared＋auth_issuer/auth_aud_*。Anthropic/OpenAI は登録しない）→ S3 バケット作成 → コード/データ投入(約 7GB) →（確認）→ terraform apply**。
- terraform state は **staging と分離**：`prod.env` は別アカウント＝別バケット/別ローカル state になるので基本は分離
  されるが、明示するなら prod 用に **`terraform workspace new prod`** か別ディレクトリで運用（README §9）。
- **IAM 反映待ち**で instance profile 作成が 403 になったら、少し待って **同じ apply を再実行**すれば通る。

> データ（built index）は staging と同一。`deploy.sh` の upload は手元の `data/` から S3 へ上げる。厳密に
> staging と同一版を配りたい場合は、staging S3 → prod S3 を `aws s3 sync`（別プロファイル間）でコピーしてもよい。

---

## 5. 起動確認とゲート

### 5.1 bootstrap 監視（SSM Session Manager・SSH は無い）
```bash
aws ssm start-session --profile adopter-prod --region ap-northeast-1 --target <instance_id>
sudo tail -f /var/log/polyarchy-bootstrap.log     # 「[bootstrap] 完了」まで（初回十数分）
systemctl is-active polyarchy-mcp polyarchy-stats  # active（cloudflared はカットオーバー時に手動 start＝§5.3）
```
> prod は `enable_web_app=false` ゆえ **web ユニットは設置されない**。設置されるのは mcp・stats・qdrant・cloudflared＋運用 timer（fuelsync/logprune/health/usagereport/dashboard/updatecheck/dataapply）。

### 5.2 検証（prod では mcp_smoke ＋ 公開URL＋秘密パス挙動）
- **箱で mcp_smoke**（キー不要）：
  ```bash
  sudo -u polyarchy bash -lc 'cd /opt/polyarchy/polyarchy; PYTHONPATH=/opt/polyarchy/polyarchy HF_HOME=/opt/polyarchy/models COLLECTION_NAME=policy_claims_v7 VECTOR_BACKEND=qdrant /opt/miniconda/envs/polyarchy/bin/python -m recommendations.eval.mcp_smoke'
  ```
  → `総合: PASS ✅`（層公開固定・layer 引数なし）。
- **回帰ゲート（--retrieval-only 等）は初回転写では不要**（検索の質は staging で担保済＝同一 built index）。
  **★以後のデータ追加時の品質ゲートは導入団体アカウントで回す（2026-09-02 方針）**＝継続的なコーパス/統計の
  追加に伴う eval・回帰ゲートの API 利用（OpenAI・必要なら Anthropic）は**導入団体自身の API アカウント**のキーで
  実行する（アカウント所有の原則＝品質保持の運転コストも導入団体帰属）。キーは導入団体 SSM に SecureString で置き
  **serving のユニットは読まない**＝「ランタイム秘密ゼロ」（サービングはキーレス）は維持される。
  手順は README §6 と同じ（staging でやっていることの転写）。
- **公開URL越し**（どこからでも可・秘密パスで）：
  ```bash
  curl -sS -X POST https://recommendations.<導入団体ドメイン><秘密パス> -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
  ```
  → `search_policy_docs` / `sweep_policy_docs` / `list_orgs` が返る。
- **秘密パスの実挙動**：**既定パス `/mcp` は 403（エッジ遮断）**・**秘密パスは 200** を確認（IP には依存しない）。
- **認証（案 B）の機械検証**（stats・mcp の両ホストで）：
  ```bash
  # PRM が配信され authorization_servers に IdP が載る（guard の /.well-known/ 除外が生命線）
  curl -sS https://stats.<導入団体ドメイン>/.well-known/oauth-protected-resource
  # 未認証は 401＋WWW-Authenticate（Bearer resource_metadata=…）
  curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://stats.<導入団体ドメイン><秘密パス> \
    -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
  ```
  ※認証 ON のため、上の tools/list 素朴 curl は **401 が正**（Bearer なしで 200 が返ったら AUTH 配線漏れ）。
- **認証の経路検証**：claude.ai／Claude Code／ChatGPT のカスタムコネクタで DCR→AuthKit ログイン→ツール実行
  →捕捉ログにメール由来 `user_hash` が付くこと（手順詳細＝docs/個人認証_案B設計.md 段2 の再現）。
  ★staging リハーサルで全経路 PASS 済（2026-09-02）＝prod では claude.ai の 1 経路＋機械検証で足りる（構成同一のため）。

### 5.3 入口を開く（cloudflared の起動＝prod にはこの手順が要る。README §7 は staging の Mac→EC2 移設用）
```bash
sudo systemctl enable --now cloudflared
sudo journalctl -u cloudflared -n 20 --no-pager        # Registered tunnel connection が出れば接続
curl -sS https://recommendations.<導入団体ドメイン>/healthz   # 200（stats.<導入団体ドメイン> も）
```
→ 以後は §5.2 の公開 URL 越し検証 → 配布手順書の配布（§6）。

---

## 6. go-live（利用者への配布）

- 各利用者の Claude（Claude Code / Desktop）→ カスタムコネクタ → URL `https://recommendations.<導入団体ドメイン><秘密パス>`（値は SSM。認証ホストでは平文配布で可＝§2.3 ★）。
- **初回接続時に AuthKit のログインが挟まる**（②は認証必須・§2.5）。利用者向け案内には「コネクタ追加→ブラウザで
  ログイン→接続完了」の 3 手順を載せる。名簿にない利用者は IdP の入口（招待制等）で弾かれる＝これが名簿限定の実体。
- 場所を問わず疎通（Claude/ChatGPT のコネクタはベンダーのサーバ経由で来る＝IP では絞れない・§2.3）。**生成は各利用者の Claude＝導入団体の箱 は課金0**。
- 層は公開固定・機密は物理遮断（検索ツール `search_policy_docs`・`sweep_policy_docs` に layer 引数は存在しない）。
- **配布前チェック**：JWT テンプレート（email）が設定済みであること（§2.5 の順序）・プライバシーポリシー改訂が
  公開済みであること（利用者キー user_hash・30 日削除）。

---

## 6.5 ログ保持期間（プライバシーポリシーの約束を機械で担保）

捕捉ログは **30日保持・以降自動削除**（プライバシーポリシーの約束）。仕組みの正典は [`README.md` §9.5](README.md)。prod でも同じ2点で担保する：
- **箱**：`polyarchy-logprune.timer`（毎日）→ `scripts/prune_query_log.py`。日数は
  `systemd/polyarchy-logprune.service` の `LOG_RETENTION_DAYS`（既定30）。
- **S3**：`terraform/storage.tf` のライフサイクル **2 本**＝`query-log-retention-30d`（`data/query_log/`）と `stats-query-log-retention-30d`（`data/stats/query_log/`）（30日で失効・
  versioning ON のため旧版も30日）。
- 両者の日数は**必ず揃える**。合意が変われば両方を直す（片方だけだと約束を守れない）。
- ★含意：燃料は30日で消えるため、**週次トリアージを30日以内に**回す（恒久資産は `data/eval/`）。

## 7. ロールバック（失敗しても現状復帰）

- **prod のデプロイをやり直したい/失敗した**：破棄前スナップショット＋二重確認つきで
  ```bash
  bash scripts/rollback.sh destroy prod
  ```
  （S3 バケットは中身があると destroy が失敗＝データは保全。完全削除は手動で空にしてから）。
- **prod を触る前に保険**：`bash scripts/rollback.sh snapshot prod`（EBS スナップショット）。
- **prod は staging とは別ドメイン/別アカウント**＝**prod の失敗は staging（開発者の検証環境）の稼働に影響しない**。
  （staging 固有＝入口を Mac へ戻す `rollback.sh entry-to-mac` は prod には無関係。）

---

## 8. 付録：開発中に潰した落とし穴（**すべて repo 修正済で prod では再発しない**＝手順としては読まなくてよい・原因調査時の参照用）

デプロイ実行で実機でしか出なかった問題。**下記は既に bootstrap/units/scripts に反映済**なので、prod は
これらを踏まずに通るはず。念のための確認用：
- [x] **conda ToS**：`conda tos accept` ＋ env 作成を conda-forge 固定（bootstrap ③）。
- [x] **データ同期の所有権**：sync を root で実行し取得後 chown（bootstrap ⑥）。
- [x] **HF 完全オフラインが ruri を壊す**：units から `HF_HUB_OFFLINE` 撤去（重みは prefetch キャッシュ・箱は
      egress あり）。**★prod で egress が塞がれている場合はここが効く**＝Cloudflare は許可でも HF への
      **egress は SG で全許可**なので通常OK。完全遮断要件が出たら Phase B で local snapshot パス直指定に。
- [x] **eval の OPENAI ガード**：prod は eval を常用しない＝無関係（serving は ruri でキーレス）。
- [x] **IAM operator に `iam:TagInstanceProfile`**：[iam-operator-policy.json](iam-operator-policy.json) に反映済。
- [x] **`iam:ListInstanceProfilesForRole` は「ロール」側の文に置く**（2026-08-28・staging の destroy で判明）：アクション名に反して
      評価対象のリソースは**ロール**（`role/polyarchy-*`）。instance-profile 側の文に置くと決して合致せず、`terraform destroy` が
      IAM ロール削除で AccessDenied になり**ロールだけ消え残る**（apply には影響しない＝destroy のときだけ出る）。
      [iam-operator-policy.json](iam-operator-policy.json) の `ManageProjectRoles` に移動済。
      ★**repo の修正だけでは効かない**＝IAM 上の operator ユーザーのポリシーを更新して初めて有効（prod は §1.2 の手順で最初から反映される）。
- [x] **code tar が `.terraform`(648MB) を巻込む**：exclude 済（scripts/upload_to_s3.sh）。
- [x] **AWS SG description は ASCII 限定**：英語化済（terraform/network.tf）。
- [x] **bash 3.2 multibyte / zsh 単語分割**：スクリプトは `${var}`＋bash 実行で回避済。
- [x] **SSM の Linux 文書は `AWS-RunShellScript`**（RunShellCommand は存在しない）。

---

## 9. 参照
- 全体 runbook：[`README.md`](README.md)（§9 prod 昇格の要約）
- ロールバック：[`scripts/rollback.sh`](scripts/rollback.sh)
- IAM：[`iam-operator-policy.json`](iam-operator-policy.json)
- 入口の実値：各環境の SSM Parameter Store（本リポジトリには置かない）
