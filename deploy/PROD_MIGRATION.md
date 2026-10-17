# 本番移行手順（導入団体 AWS / prod）— アカウント作成から go-live まで

> **状態：設計（安定リファレンス・prod 転写の手順正典）。** 公開リポジトリを複製した導入団体が、自分の AWS・Cloudflare・IdP で箱を建てる手順。
> 2026-10-15 全面改訂（4 サービス・データの受け取り・手元の準備・秘密パスとガードの順・SSM の一覧・稼働後の更新。それまでの版は 2 サービス前提で、staging からの転写を前提にしていた）。

**独立した実行手順書**。上流（公開リポジトリの開発者）が自分の staging で確立・検証した構成を、導入団体の AWS に**同じスクリプトで**建てる。
全体の runbook は [`README.md`](README.md)、運用（障害・配布・依存の更新）は [`RUNBOOK_OPS.md`](RUNBOOK_OPS.md)。本書は prod を建てるまでの手順で自己完結する。

> **prod の性格**：導入団体の箱は **MCP サーバ 4 本だけ**動けば良い（Web 不要）＝**箱に API キーを置かない**（検索はローカルのモデル・
> 文章の生成は各利用者の Claude／ChatGPT）。入口は**導入団体の独自ドメイン＋ログイン（外部 IdP）＋エッジのレート制限**（＋任意で秘密パス）。
> staging と prod の差は `env/prod.env` と SSM に集約され、建てるのは `deploy.sh prod all` の一発。

> **前提**：§0.5 の複製・§0.6 のデータの受け取り・§0.7 の手元の準備が済んでいること。手元の PC に
> `terraform` / `awscli` / `session-manager-plugin` / `cloudflared` / Docker / Python 3.12 環境（ロックから＝RUNBOOK §7）。

---

## 0. 全体像（prod で新しく要るもの）

> **凡例**：本書は提供の形②（導入団体の環境での運用）への展開手順。役割（導入団体・PdM・開発者・運用者・監査人）と用語（箱・燃料・ゲート・smoke・PRM・三点一致）の定義＝[docs/用語と役割.md](../docs/用語と役割.md)。

| 要素 | prod で用意するもの |
|---|---|
| AWS アカウント | 導入団体 AWS＋**operator IAM ユーザー**（`adopter-deployer`）へのアクセス（§1） |
| Cloudflare | **導入団体の独自ドメインのゾーン**＋**新トンネル**＋ホスト 4 本＋エッジの規則（レート制限・任意で秘密パス）（§2） |
| IdP（個人認証・案 B） | **導入団体自身の WorkOS アカウント**（名簿限定の入口）＝§2.5。**公開する全サービスに認証が必須**（無ければ箱が建たない） |
| 設定ファイル | `env/prod.env`（キーと意味は `env/prod.env.example` が正）＝§3 |
| SSM | トンネル・秘密パス・認証の値・e-Stat の appId（＋任意）＝§3.2。**API キー（Anthropic・OpenAI）は置かない** |
| データ | 政策主張DB・統計DB・企業情報DB・審議会DB の索引と値（約 10GB）＝§0.6 で受け取り、手元から S3 へ上げる |

トンネルを張らず中継の口だけで出す構成（§2.6）では、Cloudflare・IdP の行は要らない（SSM はトンネル・認証の値なし）。

**同じもの（複製に含まれる）**：Terraform 定義・bootstrap・systemd 一式（`deploy/systemd/`）・アプリのコード・検索の構成・評価問とゲートの基準値。

**サービスとホストの対応**（SSM・ガード・env の名前で使う識別子）：

| サービス | 識別子（SSM・ガード） | env のキー | ポート |
|---|---|---|---|
| 政策主張DB（recommendations） | `mcp` | `TUNNEL_HOST_MCP`・`AUTH_AUD_MCP` | 8765 |
| 統計DB（stats） | `stats` | `ENABLE_STATS_APP`・`TUNNEL_HOST_STATS`・`AUTH_AUD_STATS` | 8766 |
| 企業情報DB（companies） | `companies` | `ENABLE_COMPANIES_APP`・`TUNNEL_HOST_COMPANIES`・`AUTH_AUD_COMPANIES` | 8767 |
| 審議会DB（deliberations） | `deliberations` | `ENABLE_DELIBERATIONS_APP`・`TUNNEL_HOST_DELIBERATIONS`・`AUTH_AUD_DELIBERATIONS` | 8768 |

---

## 0.5 リポジトリの複製と作業環境（導入団体側・2026-09-17 追記）

prod の作業は**導入団体のアカウント・導入団体の PC・導入団体のリポジトリ**で行う（アカウント所有の原則＝AWS・Cloudflare・IdP と同じ）。
上流（公開リポジトリとその開発者の環境）とは、複製の 1 回（とデータの受け取り＝§0.6）を除いて交わらない。

**順序＝① 公開リポジトリを複製 → ② データを受け取る → ③ 手元の準備 → ④ 建てる**。

### 0.5.1 複製（1 回だけ）

- **複製元は公開リポジトリ**。誰でも LICENSE の条件で複製できるものを、導入団体が自分で複製する＝利用の根拠は LICENSE だけで足りる。
- **GitHub の fork 機能は使わない**（fork は元リポジトリとの関係が GitHub 上に残る）。独立した複製にする：
  ```bash
  git clone --mirror <公開リポジトリ>  polyarchy.git
  cd polyarchy.git && git push --mirror <導入団体の GitHub 組織の空の Private リポジトリ>
  ```
  公開リポジトリが持つ履歴をそのまま含める（公開時に履歴を畳んだ場合、文書中の「削除＝git 履歴で参照可」はその範囲でのみ有効）。
- 複製後、導入団体側のクローンに**元リポジトリを remote として登録しない**。以後は独立に保守する＝元リポジトリの更新の取り込みも、元リポジトリへの還元も**運用としては行わない**
  （将来取り込みたくなった場合は、公開リポジトリから LICENSE の条件で・導入団体の判断で。還元も任意）。
- 複製にリポジトリ root の `LICENSE`（Apache-2.0）と `NOTICE`（© 表示）が含まれていることを確認する。

### 0.5.2 導入団体の PC の準備

| 項目 | 内容 |
|---|---|
| アカウント | GitHub・AWS・Cloudflare・IdP・Claude Code とも導入団体名義（費用も導入団体）。個人のアカウントでログインしない |
| git の名義 | `git config user.name / user.email` を導入団体のアカウントに（コミットの名義が作業の帰属の記録になる） |
| OS | スクリプトは bash＋macOS／Linux 前提。Windows なら WSL2（Ubuntu）の中にクローンして作業する（Keychain が無い＝Cloudflare のトークンは `CF_API_TOKEN` で渡す） |
| ツール | 冒頭「前提」のとおり。Python 環境は依存のロックファイルから作る（RUNBOOK §7。Mac は §7 ② の変種＝`lock_mac_variant.py`）。`release.sh` はこの環境を PATH の先頭に通して実行 |
| フォルダ | **配布用のクローンを開発用と分ける**（RUNBOOK §5「配布用のクローンを開発用と分ける」）＝データを置き、`qdrant-dev` のマウント元にし、`release.sh` を流すのは配布用のクローン |
| `.mcp.json` | `.mcp.json.example` を複製して `<PYTHON>`・`<REPO>` を自分の環境のパスに（ローカル stdio で使う場合のみ・git 外） |
| 最初に読むもの | ルート [CLAUDE.md](../CLAUDE.md)（記憶ゼロのセッションの入口・運用地雷）→ 本書 → RUNBOOK §5「手元の製作環境」 |

### 0.5.3 持ち込まないもの（複製では運ばれない＝それで正しい）

| もの | 扱い |
|---|---|
| 秘密（AWS 資格情報・`~/.cloudflared`・Cloudflare／IdP のトークン・terraform state・e-Stat の appId・取り込みに使う API キー〔Anthropic・OpenAI・EDINET〕） | **コピーしない。導入団体名義で発行し直す**（§1・§2・§2.5・e-Stat と EDINET は導入団体で登録）。上流のトンネルの資格情報を持ち込むと上流の配信が分流する |
| データ（PDF・索引・統計値・企業の値＝約 10GB） | git 外。§0.6 で受け取る。以後の更新は導入団体の環境で |
| 開発者個人のメモ・AI の記憶 | git 外・PC ごと。運用に必要な事項は git の文書（ルート CLAUDE.md・RUNBOOK・本書）に書いてある |
| 上流の実利用ログ | 持ち込まない（30 日で消える燃料。必要な問は評価セットに昇華済み） |

---

## 0.6 データの受け取り（2026-10-15）

データ（PDF・索引・統計値・企業の値）は git の外にあり、複製では運ばれない。箱は S3 から受け取り、S3 には手元の配布用のクローンから上げる
（`deploy.sh`／`release.sh`）＝**まず手元の配布用のクローンにデータをそろえる**。

- **受け渡しの手段は上流と導入団体の間で決める**（例：上流が導入団体の用意した置き場へ 1 回だけ書き込む・媒体で渡す）。上流の S3 を導入団体が直接読む形は取らない（アカウントを交わらせない）。
- **渡す側の注意**（上流）：
  - **捕捉ログ（`*/data/query_log/`）と手元の写し（`s3_mirror/`）は入れない**（上流の利用者のログ＝渡してはいけない）。`.env` などの秘密も入れない。
  - Qdrant のストレージは、その Qdrant を止めてから固める（動いている間の写しは壊れ得る）。
  - 固めたファイルの sha256 を添え、受け取る側が照合する。共有の置き場は受け取る人だけに限り、受け取ったら共有を外す。
- 受け取るもの（サービスごとの `data/` の下・置き場の名前は RUNBOOK §3「データ復元」と同じ）：
  - 政策主張DB＝`recommendations/data/`（Qdrant のストレージ・PDF・BM25 の語彙・評価問・目録）
  - 統計DB＝`stats/data/`（系列の登録簿・値・評価問）
  - 企業情報DB＝`companies/data/`（値のストア・評価問）。有価証券報告書の原本（約 27GB）は任意＝EDINET の API は約 10 年より前の書類を取り直せない（RUNBOOK §3）
  - 審議会DB＝`deliberations/data/`（原資料・解析結果 `cache/`・BM25 の語彙・Qdrant のストレージ）
  - 検索のモデルの重みは受け取らなくてよい（§0.7 で固定の版を取り直す）
- 受け取った後は手元で持つ（S3 が原本になるのは §4 の後）。捕捉ログ（`*/data/query_log/`）は受け取らない（上流の利用者のログ）。

## 0.7 手元の準備（2026-10-15）

`deploy.sh prod all` の上り（`upload_to_s3.sh`）と、稼働後の `release.sh` が前提にするもの。手順の正典は RUNBOOK §5「手元の製作環境」と「配布用のクローンを開発用と分ける」。

1. ロックから作った Python 環境を PATH の先頭に（RUNBOOK §7 ②）。
2. Docker で `qdrant-dev`（:6333・マウント元＝配布用のクローンの `recommendations/data/qdrant`）と `qdrant-delib`（:6340・`deliberations/data/qdrant`）を起動する。
3. **ゲートを一度、手元で回す**（README「品質の担保」の基準値どおりに通るか＝受け取ったデータが壊れていない確かめ）。
   あわせて HF のキャッシュに検索のモデルの固定の版（`polyarchy_retrieval/models.py` の `PINNED_REVISIONS`）が入る＝§4 の上りが S3 `data/models/` に置く（箱は HF に接続しない）。
4. 審議会DB の束を書き出す：`python -m deliberations.ops.bundle export`（箱は束から Qdrant を復元する＝束が無いと審議会DB は空のまま起動する。稼働後は `release.sh` が毎回書き出す）。
5. **`docker stop qdrant-dev`**（Qdrant のストレージは止めている間にしか整合して写せない＝動いていると上りが止まる。§4 の後に `docker start qdrant-dev`）。

---

## 1. 導入団体 AWS アカウントへのアクセスを確立

### 1.1 アクセス手段（いずれか。導入団体の運用に合わせて選ぶ）
- **(A) IAM ユーザー＋アクセスキー**（最短）：導入団体アカウントの管理者に `adopter-deployer` を作ってもらう。
- **(B) IAM Identity Center (SSO)**：組織で SSO 運用なら `aws sso login` で短命の資格情報（推奨・鍵が手元に残らない）。
- → **どれで入るかは導入団体側の運用に合わせて確定**。以降は (A) を例に記述。

### 1.2 operator IAM ユーザーの権限（staging と同型）
`adopter-deployer` に付与：
1. AWS 管理ポリシー **`PowerUserAccess`**（IAM 以外を許可）。
2. インラインポリシー **`polyarchy-iam-manage`**＝[`iam-operator-policy.json`](iam-operator-policy.json) の内容。
   **`<ACCOUNT_ID>` を導入団体アカウントの12桁に置換**。
3. ルートユーザーには **MFA を必ず**（日常使いしない）。CLI 専用ユーザーの MFA は任意。

### 1.2b 日常のデータ更新用の権限（権限 2 段の下段・稼働後）
稼働後の定型更新（`release.sh`）に要るのは S3 だけ＝[`iam-data-operator-policy.json`](iam-data-operator-policy.json)
（`data/`・`release/`・`code/` の Put/Delete/Get＋`ops/` の Get＋ListBucket。捕捉ログは読めない＝`data/*query_log/*` の 1 本の Deny で全サービスを拒否）。
SSO の権限セットか IAM ユーザーのインラインに `<BUCKET>` を置換して貼る。上段（1.2＝PowerUser）は初回構築と障害対応のみ。

### 1.3 手元にプロファイルを設定
```bash
aws configure --profile adopter-prod       # 導入団体のアクセスキー / region= ap-northeast-1 / output= json
aws sts get-caller-identity --profile adopter-prod   # user/adopter-deployer とアカウントIDを確認
```
- ★**vCPU の上限を先に確かめる**（2026-10-15）＝導入の直後に箱の機種を上げる可能性があるなら（RUNBOOK §1「機種を上げる・戻す」）、上げたい機種の vCPU（8 vCPU・16 vCPU）がアカウントの上限の内か。作ったばかりのアカウントは低いことがあり、引き上げの申請は日数がかかる＝稼働の前に申請しておく（確かめ方は同じ節）。

---

## 2. Cloudflare（導入団体ゾーン）と IdP を用意 ＝ ①入口 ②パス ③レート制限 ④認証

**Cloudflare の設定**は裏の EC2 に依存しない。順＝2.1 → 2.2 → 2.3（SSM にパスの値）→ 2.4（ガード＝SSM の値を読む）→ 2.6（IdP）。

### 2.1 ① 入口＝導入団体の独自ドメインのゾーン
- 導入団体の独自ドメインを Cloudflare に載せる（ネームサーバを Cloudflare へ／既に載っていれば不要）。
- サブドメインを **4 本**決める：`recommendations.`・`stats.`・`companies.`・`deliberations.<導入団体ドメイン>`（Web は無し）。
  ★**公開した後は、ホスト名・パス・発行者（IdP）を変えない**（利用者の接続がそこに紐づく＝変えると全員がコネクタを作り直す・README §9.5）。

### 2.2 新トンネルを作る（上流のトンネルとは別 UUID）
```bash
cloudflared tunnel login                        # ブラウザで★導入団体ゾーンを承認
cloudflared tunnel create adopter-prod          # → 新UUID と資格情報 JSON のパスが出る（控える）
for h in recommendations stats companies deliberations; do
  cloudflared tunnel route dns adopter-prod "$h.<導入団体ドメイン>"   # CNAME をトンネル宛に
done
```
（`route dns` は `tunnel login` 直後＝cert.pem が導入団体ゾーンに紐づいている間だけ使える。後からホストを足すときはダッシュボードで CNAME `<name>` → `<UUID>.cfargotunnel.com`（プロキシ済み）＝RUNBOOK §5「サービスを足す」）
→ **新UUID** と `~/.cloudflared/<新UUID>.json` を控える（§3 で使う）。資格情報のファイルは §4 で SSM に登録した後、作業用 PC から消す（§4）。

### 2.3 ② パス＝秘密パスか既定の `/mcp` か（サービスごとに SSM へ）
各サービスの待受のパスは SSM `/polyarchy/prod/<識別子>_http_path` の値で決まる（bootstrap がサービスの env に書く・**リポジトリには値を持たない**）。2 通り：

- **秘密パス**（`/mcp-<128bit 乱数>`）：ホスト名と違い DNS／証明書に現れない＝無差別のスキャナを既定パスの遮断で落とす層。
  ただし認証を点けると、PRM（`/.well-known/oauth-protected-resource`）に秘密パス込みの URL が載る＝「秘密」ではなくなる（下の★）。
- **既定の `/mcp`**：接続 URL をディレクトリ等で公開する構成（README §9.5「秘密パスなしで公開する構成」）。値として `/mcp` を入れる（空にしない＝ガードが規則を作らない）。

```bash
for s in mcp stats companies deliberations; do
  aws ssm put-parameter --profile adopter-prod --overwrite --type SecureString \
    --name "/polyarchy/prod/${s}_http_path" --value "/mcp-$(openssl rand -hex 16)"    # 既定の /mcp で出すなら --value /mcp
done
```
発行した値は、§3 の `AUTH_AUD_<SVC>`（`https://<ホスト><パス>`）と §2.5 の IdP の Resource indicator に**同じ文字列で**写す（三点一致）。
★**一度決めたら変えない**（三点一致を初回に合わせたら触らない。変えるときは RUNBOOK §4 の手順＝SSM → `release.sh` → ガードの再適用・利用者はコネクタを作り直す）。

> **★認証ホストでの秘密パスの位置づけ（2026-09-02）**：認証を点けると PRM の `resource` に秘密パス込み URL が**公開される**
> （クライアントが接続 URL と resource の一致を検証する仕様上、隠せない）＝防壁の本体はログイン。残る効能は「PRM を読まない無差別スキャナを
> 既定パスの遮断でエッジで落とす」ノイズ低減層。利用者への URL の配布は平文でよい。

### 2.4 ③ エッジの規則＝ガード（パスの規則＋レート制限）
[`scripts/cloudflare-guard.sh`](scripts/cloudflare-guard.sh) を `IP_ALLOWLIST=off`（既定）で。**§2.3 の SSM の値を読んで**パスの規則を作る（値が無いと規則を作らない＝先に §2.3）。
除外 3 種（`/.well-known/`・`/cdn-cgi/`・`/healthz`）込み＝認証のディスカバリの生命線。
★guard の既定値は上流の staging＝**prod では必ず上書き**。★ガードの `apply`・`remove` は運用者がターミナルで（AI のセッションに任せない＝RUNBOOK §1）。
```bash
export ZONE_NAME=<導入団体ドメイン> AWS_PROFILE_CF=adopter-prod IP_ALLOWLIST=off \
       RL_HOSTS=recommendations.<導入団体ドメイン>,stats.<導入団体ドメイン>,companies.<導入団体ドメイン>,deliberations.<導入団体ドメイン>
for s in mcp stats companies deliberations; do
  h=$([[ $s == mcp ]] && echo recommendations || echo $s)
  SERVICE=$s MCP_HOST=$h.<導入団体ドメイン> SSM_PATH_PARAM=/polyarchy/prod/${s}_http_path bash scripts/cloudflare-guard.sh apply
done
bash scripts/cloudflare-guard.sh status   # 同じ環境変数で＝レート制限の式に 4 ホストが載っていること
```
- **レート制限**は Free プランの 1 本を全ホストで共有し、apply はそれを `RL_HOSTS` の内容で**作り直す**（既定＝10 秒で 200 本・`RATE_PER_MIN` で変えられる）。
  鍵は接続元 IP＝コネクタ経由の利用はベンダー（Anthropic・OpenAI）のサーバの IP にまとまる＝利用者ごとの上限にはならない（大人数で共有の上限に当たるかは導入後の利用で見る・1 人の大量の利用はアラーム top-user＝RUNBOOK §1）。
- WAF のカスタムルールは Free プランで 5 本まで（いまはサービスごとに 1 本＝4 本）。
- Cloudflare の API トークン（Zone:Read＋WAF:Edit）は `CF_API_TOKEN` か macOS の Keychain `cloudflare-api-token`（`cloudflare-guard.sh` 冒頭）。

> **★IP 許可リストは作らない（2026-08-27/28 に廃止）**：Claude・ChatGPT のコネクタは、利用者の PC ではなくベンダーのサーバから接続する
> （Claude は Anthropic の outbound レンジ・[公式](https://platform.claude.com/docs/en/api/ip-addresses)。OpenAI の egress は変動し列挙できない）。
> OAuth のディスカバリもログインも別経路で来る＝IP 方式と認証は両立しない。「組織の IP だけ許可」を素直に設定すると、コネクタの利用者は全員弾かれ、
> ベンダーのレンジを足せば第三者も到達し得る＝組織限定にならない。**組織・個人の限定は §2.5 のログインで行う。**
> この非自明な仕様は導入団体の情報システム部門へ必ず開示すること（「IP 許可＝組織限定」と誤解されるため）。

### 2.5 ④ 認証＝個人認証（案 B・外部 IdP）— 公開する全サービスに必須

正典＝[docs/個人認証_案B設計.md](../docs/個人認証_案B設計.md)。実装は複製したコードに含まれ、§3 の env（`AUTH_ISSUER`・`AUTH_AUD_<SVC>`）で有効化される。
★**公開ホスト（`TUNNEL_HOST_<SVC>`）があるのに認証の値が無いサービスは、bootstrap ⑧ が FAIL して箱が建たない**（利用者はログイン必須・ルート CLAUDE.md §3）＝§4 の前に済ませる。
prod で用意するのは IdP 側の設定：

1. **導入団体自身の WorkOS アカウントの Production 環境を使う**（アカウント所有の原則。AuthKit は無償 100 万 MAU）。
   Production 環境は issuer ドメインを自分で決められる＝ここで prod の issuer が本決まり。
2. **設定 3 つ**（★順序どおり）：
   - **JWT テンプレート `{"email": "{{user.email}}"}` を配布前に**（後から入れると user_hash が sub 代替→メール由来に切り替わり、捕捉ログ上は別利用者に見える）。
   - Connect で **DCR ON**。
   - **Resource indicator を登録し Default 指定**（resource パラメータを送らないクライアントがある＝Default 必須）。
3. **Resource indicator に登録する値＝各サービスの公開 URL（パス込み）の 4 本**：
   `https://recommendations.<導入団体ドメイン><パス>`・`https://stats.…`・`https://companies.…`・`https://deliberations.…`（パスは §2.3 の値）。
   Default 指定は 1 本（どれでもよい＝resource を送るクライアントは非 Default のサービスにも通る・2026-09-02 実測）。
4. **名簿限定の入口**：招待制／ドメイン制限／CSV 一括のどれで運用するかを導入団体側の名簿管理と合わせて決める。
   決まるまでは Production 環境をサインアップ制限なしで作らない（作る時点で制限を入れる）。
> **env の書き方（非自明）**：`AUTH_AUD_<SVC>` の値は**トークンの `aud` と完全一致**で書く。AuthKit は resource indicator の値を `aud` に載せる＝
> **AUD＝上記 3 の URL（パス込み）**。resource URL 自体は bootstrap が `TUNNEL_HOST_<SVC>＋SSM のパス`から合成する（env に書くのは AUD だけ）。
> Production 環境では登録時のメールアドレスの確認（コードの入力）が必須だった＝利用者への案内に入れる。

### 2.6 中継の口（任意・千人規模の問を 1 つの bot が中継するとき・2026-10-16）

各自のクライアント（Claude・ChatGPT）から使う利用者は §2.5 のログインで入る。チャットの bot のように **1 つのクライアントが多数の会員の問を中継する**ときは、
bot だけが使う**中継の口**を開ける。ルート CLAUDE.md §3「利用者はログイン必須」の唯一の例外で、次の条件をサーバと箱の網が守る：

- **既定は閉**。env の `RELAY_SOURCE_CIDRS` を書いたときだけ開く（空＝箱の網に規則が無く、サービスも待ち受けない）。
- **同じネットワークの中だけ**。送り元はプライベートの範囲（10/8・172.16/12・192.168/16 の内側）だけ＝箱の網の規則（terraform）とサーバの照合の二重。トンネル（公開の入口）には載せない。
- **会員の印が必須**。bot は HTTP の要求ごとに `X-Polyarchy-Member: <16 桁の小文字の 16 進>` を付ける。会員の ID から作る、元に戻せない値にする（例：bot だけが持つ秘密の塩つきの sha256 の先頭 16 桁）。同じ会員には毎回同じ値。会員の ID・メールそのものは送らない。印が無い・形が違う要求は 400、範囲の外からは 403。
- **公開の口では印を信じない**（印を付けてきても、ログインを求める）。

印は捕捉ログの利用者（`user_hash`）になり、経路は `relay`。top-user のメトリクス・週次レポートの利用者数・利用者ごとの上限（SSM `user_calls_per_hour`）は、そのまま会員ごとに数える（bot 全体を 1 人とは数えない）。上限に届いた会員の呼び出しは「上限・あと何分」のエラーの結果で返る＝bot はそれを会員に伝える。「混んでいる」の応答は公開の口と同じ形で返る（同時に動かす数も共有）。

**開け方**
1. bot を箱と同じ VPC に置く（`terraform output relay_vpc_id`・`relay_subnet_id`。箱の VPC は 10.0.0.0/16・サブネットは 10.0.1.0/24）。
2. env に `RELAY_SOURCE_CIDRS=<bot のプライベート IP>/32`（複数はカンマ区切り）。
3. `deploy.sh prod secrets`（SSM `relay_allow_cidrs` に同じ値）→ `deploy.sh prod plan` で差分が `aws_vpc_security_group_ingress_rule.relay` の追加だけ（と output）であることを確かめて → `apply-plan`。
4. 次の配布（`release.sh`）の自動適用で、bootstrap が各サービスの env に `MCP_RELAY_PORT`・`MCP_RELAY_ALLOW_CIDRS` を書き、口が開く（サービスの起動の行に「中継の口=有効」）。
5. bot から `http://<terraform output relay_private_ip>:<ポート><各サービスのパス>` を、印つきで 200・印なしで 400 になることを確かめる。
   ポート＝政策主張DB 8865・統計DB 8866・企業情報DB 8867・審議会DB 8868（公開の口＋100）。パスは SSM `<識別子>_http_path` と同じ。

**閉じ方**＝env の `RELAY_SOURCE_CIDRS` を空に → `deploy.sh prod secrets`（SSM が `off` に）→ plan → `apply-plan`（規則が消える）→ 次の配布でサービスの env から消える。

**トンネルを張らず中継の口だけで出す構成**（公開ホストを持たず・認証を置かず・bot からだけ使う導入団体のため・2026-10-17）

- env に**書かないもの**＝`TUNNEL_ID`・`CLOUDFLARED_CRED_FILE`・`TUNNEL_HOST_*`（4 本）・`AUTH_ISSUER`・`AUTH_AUD_*`（4 本）＝§2.1〜§2.5 は丸ごと要らない。**書くもの**＝`RELAY_SOURCE_CIDRS`。`MCP_HTTP_HOST` は既定の `127.0.0.1` のまま（公開の口は箱の中に閉じたまま・中継の口はサーバが別のソケットで開く）。
- `deploy.sh prod secrets` は、公開ホストが無く `TUNNEL_ID` も無いとき、トンネルの登録を**警告つきで飛ばして**残りの登録へ進む＝SSM の**必須は `relay_allow_cidrs` だけ**（`<識別子>_http_path` は入れなければ既定の `/mcp`・`estat_app_id` は推奨のまま）。箱の bootstrap は cloudflared の config を書かず（⚠「未登録」の行が 1 つ出る＝正）、公開ホストが無いので認証なしで建つ（公開しないから認証が要らない＝ルート CLAUDE.md §3 の例外の中）。
- 外からの到達の確認（edge・idp）は対象が無く、送らない＝アラーム `edge-<サービス>`・`idp` は鳴らない（INSUFFICIENT_DATA のまま）。ダッシュボード①に「外からの確認」の表は出ない（対象が無い箱では表ごと省かれる＝正）。入口の守りは箱の網の規則（送り元の範囲）とサーバの照合＝§2.4 のガードは無い。
- bot の側の約束は上と同じ＝**毎要求に会員の印**（`X-Polyarchy-Member`）を付ける・会員ごとに固定で元に戻せない値・会員の ID・メールは送らない。利用者ごとの上限（SSM `user_calls_per_hour`・RUNBOOK §1）を入れる。
- **確認**（§5.3 の代わり・bot の EC2 から・ポートとパスは「開け方」の 5）：`curl -sS http://<relay_private_ip>:<ポート>/healthz` が 200（印なしでよい＝届いている）→ `<パス>` への POST が印つきで 200・印なしで 400 → 許可の範囲の外（別のサブネット・手元の PC）からは届かない（網の規則で落ちる＝接続できない）。4 サービス分。

**プライバシーページ**：会員の印と経路 `relay` が利用記録に残る＝自団体のページ（SSM `privacy_url`）の「収集する情報」に、中継のクライアント経由の利用では会員ごとの印（元に戻せない値）を記録することを書く。

---

## 3. env/prod.env と SSM

### 3.1 env/prod.env
```bash
cd deploy
cp env/prod.env.example env/prod.env
```
**キーと意味は `env/prod.env.example` が正**（コメントに書いてある）。少なくとも次を埋める：

| キー | 値 |
|---|---|
| `ENVIRONMENT`・`AWS_PROFILE`・`AWS_REGION` | `prod`・`adopter-prod`・`ap-northeast-1` |
| `ENABLE_WEB_APP` | `false`（Web 無し） |
| `TUNNEL_ID`・`CLOUDFLARED_CRED_FILE` | §2.2 の新 UUID・`$HOME/.cloudflared/<新UUID>.json` |
| `TUNNEL_HOST_MCP`・`TUNNEL_HOST_STATS`・`TUNNEL_HOST_COMPANIES`・`TUNNEL_HOST_DELIBERATIONS` | §2.1 の 4 ホスト |
| `ENABLE_STATS_APP`・`ENABLE_COMPANIES_APP`・`ENABLE_DELIBERATIONS_APP` | `true`（4 サービスとも出す） |
| `AUTH_ISSUER`・`AUTH_AUD_MCP`・`AUTH_AUD_STATS`・`AUTH_AUD_COMPANIES`・`AUTH_AUD_DELIBERATIONS` | §2.5 の issuer・4 本の URL（パス込み・三点一致） |
| `ESTAT_SOURCE` | 導入団体が登録した e-Stat の appId の在りか（`file:<…>/stats/.env` など＝値は文書に書かない）。箱の更新チェックが使う |
| `ALERT_EMAIL` | アラームの宛先（§4 の後に AWS から確認のメールが届く＝Confirm を押すまで通知は来ない） |
| `BUCKET` | 配布用のクローンから `release.sh` を流すとき（terraform の state を持たないフォルダ）に要る＝`polyarchy-prod-<アカウントID>` |
| `INSTANCE_TYPE`・`TOP_USER_CALLS_THRESHOLD` | 空＝既定（t3.xlarge・200）。変えるときの手順は RUNBOOK §1 |
| `RELAY_SOURCE_CIDRS` | 空＝中継の口は閉。bot が中継するときだけ（§2.6） |

トンネルを張らず中継の口だけで出す構成（§2.6）では `TUNNEL_ID`・`CLOUDFLARED_CRED_FILE`・`TUNNEL_HOST_*`・`AUTH_*` を書かず、`RELAY_SOURCE_CIDRS` を書く。

### 3.2 SSM のパラメータ（`/polyarchy/prod/…`）

| 名前 | 必須 | 入れ方 | 用途 |
|---|---|---|---|
| `cloudflared_tunnel_id`・`cloudflared_credentials` | 必須 | `deploy.sh prod secrets`（env の `TUNNEL_ID`・`CLOUDFLARED_CRED_FILE` から） | 箱のトンネル |
| `<識別子>_http_path`（4 本） | 必須 | §2.3 の `put-parameter` | 待受のパス・ガードの規則 |
| `auth_issuer`・`auth_aud_<識別子>`（4 本） | 必須 | `deploy.sh prod secrets`（env の `AUTH_*` から） | 認証 |
| `estat_app_id` | 推奨 | `deploy.sh prod secrets`（env の `ESTAT_SOURCE` から） | 統計DB の更新チェック（e-Stat 系） |
| `privacy_url` | 推奨 | `put-parameter --type String --value https://<自団体のプライバシーポリシー>` | 政策主張DB のサーバの説明が案内するページ（無ければ上流のページが案内される＝§6） |
| `usage_operator_hashes` | 任意 | RUNBOOK §5「週次利用レポートで運営者自身の利用を除く」 | 週次レポートの除外 |
| `openai_apps_challenge_<識別子>` | 任意 | README §9.5 | OpenAI のディレクトリへの掲載の確認 |
| `user_calls_per_hour` | 任意 | `put-parameter --type String --value <数>`（RUNBOOK §1「利用者ごとの上限」） | 1 人が直近 1 時間に呼べるツールの数（サービスごと・無い＝上限なし） |
| `relay_allow_cidrs` | 任意 | `deploy.sh prod secrets`（env の `RELAY_SOURCE_CIDRS` から・空にしたら `off`） | 中継の口（§2.6） |
| `tool_offload_<識別子>` | 使わない | — | 上流の一時の事情のための切る口（RUNBOOK §1「busy」）。入れない＝既定のまま |

トンネルを張らず中継の口だけで出す構成（§2.6）では、「必須」のうち `cloudflared_*`・`auth_*` は要らず、`<識別子>_http_path` も任意（無ければ `/mcp`）＝必須は `relay_allow_cidrs` だけ。

API キー（Anthropic・OpenAI・EDINET）は SSM にも箱にも置かない。取り込みに使うキーは導入団体名義で手元の `.env`（git 外）に（RUNBOOK §5「手元の製作環境」）。

---

## 4. デプロイ（同一スクリプト）

```bash
cd deploy
bash scripts/deploy.sh prod all
```
`all` ＝ **秘密の登録（§3.2 の `deploy.sh prod secrets` の行）→ S3 バケットの作成 → コードとデータの上り（約 10GB）→（確認）→ terraform apply**。
- 上りは §0.7 の準備が前提（ロックの環境・HF のキャッシュの固定の版・審議会DB の束・`qdrant-dev` を止めておく）。終わったら `docker start qdrant-dev`。
- terraform の state は `deploy.sh` を流したフォルダの `deploy/terraform/terraform.tfstate`（git 外）＝**失うと後の apply と destroy ができない**。退避するか、`versions.tf` のコメントの S3 backend に移す。配布用のクローンには state が無い＝§3.1 の `BUCKET` で足りる。
- **IAM 反映待ち**で instance profile 作成が 403 になったら、少し待って**同じ apply を再実行**すれば通る。
- 4 サービスを最初から有効にして建てる箱は、配布を分ける必要は無い（RUNBOOK §5「サービスを足す」の 2 回に分ける手順は、稼働中の箱に足すときのもの）。
- **構築の途中でアラームが鳴る**（想定内）：health は値が来ないと鳴る＝bootstrap が終わるまで（十数分）。edge は §5.3 で入口を開くまで。§5.3 の後に OK へ戻ることを確かめる。
- 登録が済んだら、**トンネルの資格情報のファイルを作業用 PC から消す**（ルート CLAUDE.md §5＝同じトンネルを 2 か所で動かすと配信が分流する）。消した後も `deploy.sh prod secrets` は SSM の登録済みのものを使って流せる（2026-10-15〜）。

---

## 5. 起動確認

### 5.1 bootstrap 監視（SSM Session Manager・SSH は無い）
```bash
aws ssm start-session --profile adopter-prod --region ap-northeast-1 --target <instance_id>
sudo tail -f /var/log/polyarchy-bootstrap.log     # 「[bootstrap] 完了」まで（初回十数分）
systemctl is-active qdrant polyarchy-mcp polyarchy-stats polyarchy-companies qdrant-deliberations polyarchy-deliberations
systemctl list-timers 'polyarchy-*'                # health・fuelsync・logprune・usagereport・dashboard・updatecheck・dataapply・edgeprobe・topuser
```
（cloudflared は §5.3 で起動する。web のユニットは設置されない。ユニットの一覧と役割＝RUNBOOK §2。）

### 5.2 箱の上の smoke（キー不要・HF に接続しない）
自動適用が配布のたびに流すものと同じ（`apply_data_update.sh` の `smoke`）：
```bash
R=/opt/polyarchy/polyarchy; P=/opt/miniconda/envs/polyarchy/bin/python
sudo -u polyarchy bash -lc "cd $R; PYTHONPATH=$R HF_HOME=/opt/polyarchy/models HF_HUB_OFFLINE=1 COLLECTION_NAME=policy_claims_v7 VECTOR_BACKEND=qdrant $P -m recommendations.eval.mcp_smoke"
sudo -u polyarchy bash -lc "cd $R; PYTHONPATH=$R $P -m stats.eval.mcp_smoke"
sudo -u polyarchy bash -lc "cd $R; PYTHONPATH=$R $P -m companies.eval.mcp_smoke"
sudo -u polyarchy bash -lc "cd $R; PYTHONPATH=$R HF_HOME=/opt/polyarchy/models HF_HUB_OFFLINE=1 DELIB_QDRANT_URL=http://127.0.0.1:6340 $P -m deliberations.eval.mcp_smoke"
```
→ 4 本とも `PASS`。検索の質のゲートは §0.7 で手元で回した（箱の上では回さない・稼働後は `release.sh` が配るたびに回す）。

### 5.3 入口を開く（cloudflared の起動）
```bash
sudo systemctl enable --now cloudflared
sudo journalctl -u cloudflared -n 20 --no-pager        # Registered tunnel connection が出れば接続
```
手元から 4 ホストそれぞれ：
```bash
curl -sS https://<ホスト>/healthz                                     # 200
curl -sS https://<ホスト>/.well-known/oauth-protected-resource       # resource が §2.5 の URL・authorization_servers に IdP
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://<ホスト><パス> \
  -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'   # 401（トークン無し）＝正
```
- 秘密パスの構成なら、既定パス `/mcp` は 403（エッジで遮断）。
- 5 分ほど後に、ダッシュボード①（S3 `ops/dashboard/`）の「外からの確認」が 4 サービスとも ok・アラーム edge と health が OK に戻る。
- **認証の経路の確認**：Claude（claude.ai か Claude Code）と ChatGPT のコネクタで、ログイン → ツールの実行 → 捕捉ログにメール由来の `user_hash` が付く（翌時の S3 の同期の後に見える）。

---

## 6. go-live（利用者への配布）

- 各利用者の Claude／ChatGPT → コネクタを追加 → URL `https://<ホスト><パス>`（4 本）。**初回の接続でログインが挟まる**＝利用者向けの案内には「コネクタの追加 → ブラウザでログイン → 接続完了」を載せる。名簿にない利用者は IdP の入口で弾かれる＝これが名簿限定の実体。
- **ChatGPT**：利用者ごとの認可は「個人の設定 → アプリ → 接続」で行う（作成の画面では出ない）。Business ではコネクタを公開した後にツールの定義を更新できない＝定義が変わる配布の後は作り直し（ルート CLAUDE.md §5）。
- 生成は各利用者の Claude／ChatGPT＝導入団体の箱は生成の課金 0。層は公開固定（ツールに layer 引数は無い）。
- **配布前チェック**：
  - JWT テンプレート（email）が設定済み（§2.5 の順序）。
  - **自団体のプライバシーポリシーを公開し、SSM `privacy_url` に入れてから配布**（無いと政策主張DB のサーバの説明が上流のページを案内する）＝捕捉ログ（利用者キー user_hash・接続元のクライアントの種別・30 日削除）を載せる。`deploy/pages/` は上流のページの例。
  - Cloudflare のダッシュボードでトンネルの健全性の通知を ON（RUNBOOK §0）。
  - アラームのメールの Confirm を押した（§3.1 `ALERT_EMAIL`）。
- 運用の見方（ダッシュボード・週次レポート・監査の線）＝[docs/導入団体側_監査ガイド.md](../docs/導入団体側_監査ガイド.md)。

## 6.3 稼働後の更新（2026-10-15）

- **データとコードの更新は `release.sh prod` だけ**（配布用のクローンから・全ゲート PASS のときだけ配る）。配る前の確認は単体で先に回せる（`release_preflight.sh`）。箱は 15 分以内に自動で適用し、smoke が落ちれば自動で切り戻す（RUNBOOK §5）。
- **配布の時間帯**：自動適用は 4 サービスを数分止める（停止 → bootstrap 再走行 → 再起動 → smoke）。`release.sh` から最長 15 分後の任意の時刻に始まる（15 分の刻みの timer）＝**利用の少ない時間帯に流す**（止まる間の呼び出しは bot・コネクタ側でエラーになる）。
- 地雷（ルート CLAUDE.md §5）：bootstrap を再走行してもコードは更新されない／自動適用のスクリプト（`/usr/local/bin/polyarchy-*`）の変更は次の次の配布から効く／依存を変えたらロックを作り直す（RUNBOOK §7）。
- 箱の機種を上げる・戻す＝RUNBOOK §1「機種を上げる・戻す」（§1.3 の vCPU の上限）。

## 6.5 ログ保持期間（プライバシーポリシーの約束を機械で担保）

捕捉ログは **30日保持・以降自動削除**（プライバシーポリシーの約束）。仕組み・日数の式・サービスの一覧の正典は
[`README.md`「捕捉ログの保持」](README.md)（箱 30 日＋S3 の非現行 1 日＋ライフサイクルの遅れ＝S3 の日数を箱と同じ 30 に「揃えない」）。
prod でも同じ terraform・systemd で担保される（`terraform apply` でライフサイクルが入る）。
- ★含意：燃料は30日で消えるため、**週次トリアージを30日以内に**回す（恒久資産は `data/eval/`）。

## 7. ロールバック（失敗しても現状復帰）

- **prod のデプロイをやり直したい/失敗した**：破棄前スナップショット＋二重確認つきで
  ```bash
  bash scripts/rollback.sh destroy prod
  ```
  （S3 バケットは中身があると destroy が失敗＝データは保全。完全削除は手動で空にしてから）。
  ★箱を破棄して使い終えるときは、S3 の捕捉ログ（`data/*query_log/`）も消す（保持の約束＝README「捕捉ログの保持」）。
- **prod を触る前に保険**：`bash scripts/rollback.sh snapshot prod`（EBS スナップショット）。
- prod は上流の staging とは別ドメイン・別アカウント＝互いの稼働に影響しない。

---

## 8. 付録：開発中に潰した落とし穴（**すべて repo 修正済で prod では再発しない**＝手順としては読まなくてよい・原因調査時の参照用）

- [x] **conda ToS**：`conda tos accept` ＋ env 作成を conda-forge 固定（bootstrap ③）。
- [x] **データ同期の所有権**：sync を root で実行し取得後 chown（bootstrap ⑥）。
- [x] **検索のモデルの重み**：箱は HF に接続しない（ユニットは `HF_HUB_OFFLINE=1`）。重みは S3 `data/models/` から置き、SHA256SUMS で照合する（bootstrap ⑤）。版は `PINNED_REVISIONS` の commit で固定。
- [x] **IAM operator に `iam:TagInstanceProfile`**：[iam-operator-policy.json](iam-operator-policy.json) に反映済。
- [x] **`iam:ListInstanceProfilesForRole` は「ロール」側の文に置く**：アクション名に反して評価対象のリソースは**ロール**。instance-profile 側の文に置くと
      `terraform destroy` が IAM ロール削除で AccessDenied になる（apply には影響しない）。[iam-operator-policy.json](iam-operator-policy.json) の `ManageProjectRoles` に移動済。
- [x] **code tar は git の追跡ファイルだけ**（`git archive HEAD`＝未コミットの変更は配らない・コードの範囲が dirty なら `release.sh` が止まる）。
- [x] **S3→箱の同期は `--exact-timestamps`**（大きさが同じファイルを取り直さない既定の挙動で、束の目印が古いまま残った・ルート CLAUDE.md §5）。
- [x] **AWS SG description は ASCII 限定**：英語化済（terraform/network.tf）。
- [x] **bash 3.2 multibyte / zsh 単語分割**：スクリプトは `${var}`＋bash 実行で回避済。
- [x] **SSM の Linux 文書は `AWS-RunShellScript`**（RunShellCommand は存在しない）。

---

## 9. 参照
- 全体 runbook：[`README.md`](README.md)・運用：[`RUNBOOK_OPS.md`](RUNBOOK_OPS.md)（§1 アラーム・機種・利用者を止める／§3 データ復元／§5 配布と手元の製作環境／§7 ロック）
- 設計：[docs/運用設計.md](../docs/運用設計.md)・監査：[docs/導入団体側_監査ガイド.md](../docs/導入団体側_監査ガイド.md)・認証：[docs/個人認証_案B設計.md](../docs/個人認証_案B設計.md)
- ロールバック：[`scripts/rollback.sh`](scripts/rollback.sh)・IAM：[`iam-operator-policy.json`](iam-operator-policy.json)
- 入口・認証の実値：各環境の SSM Parameter Store（本リポジトリには置かない）
