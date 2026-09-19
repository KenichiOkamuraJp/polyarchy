# 個人認証（案 B＝外部 IdP）設計 — stats / recommendations 共通・数千人規模

> **状態：設計（安定リファレンス）。** 起案 2026-09-02・2026-09-04 改訂（段4＝個人向けのみ・issuer をプレースホルダに）。進捗は [残タスク.md](残タスク.md) A6。
> 前史＝案 A（Cloudflare Access の OAuth）から案 B への段組みと 2026-08-28 の authless 決定（当時の設計文書は公開リポジトリに含めていない）。
> 本書はその「案 B」を数千人規模（②名簿限定 → ③個人向け提供・②③両立）向けに具体化する。

## 0. 決定（2026-09-02）

- **案 B を採用**：外部 IdP を認可サーバ・Polyarchy を Resource Server とする。案 A（Cloudflare Access）は
  Zero Trust Free 50 席の崖（超過は席単価課金）で数千人規模には不成立。
- **IdP 第一候補＝WorkOS AuthKit**（無償 100 万 MAU・MCP 認可サーバ用途に注力・DCR 対応）。対抗＝Auth0
  （無償 25,000 MAU・DCR 要有効化）。どちらも数千人規模なら無償枠内。利用者キーは「IdP 検証済みメール」なので
  後から乗り換えても利用者は変わらない。
- **テナントは②（導入団体）と③（個人向け）で分離**：名簿・顧客データが物理的に混ざらない＝組織境界
  （コーパス連邦の設計思想と整合）。②は導入団体 AWS の prod 箱・③は運営者の自前環境に対応させる。③は個人向けの提供形態（定義＝[用語と役割.md](用語と役割.md)）。
- **③の律速は認証でなくデータ再配布条件**（取得元ごとの商用利用条件＝[stats/docs/再配布条件.md](../stats/docs/再配布条件.md)）。認証基盤は②で先に実証する。

## 1. 構成（実装済み・2026-09-02）

```
Claude / ChatGPT / Claude Code
  └─ 401 + WWW-Authenticate: Bearer resource_metadata=… を受けて
     /.well-known/oauth-protected-resource（PRM・RFC 9728）→ authorization_servers=[IdP]
     → IdP で DCR → ログイン → Bearer トークン
Cloudflare エッジ（変更なし：レート制限＋秘密パス。IP 許可は使わない＝OAuth と両立しない実測）
  └─ Tunnel → 箱
箱（origin）: polyarchy_common.mcp_http.serve_streamable_http
  └─ oidc_bearer_middleware（polyarchy_common/access.py）
     ＝IdP の JWKS で署名/iss/aud/期限を検証 → email/sub を contextvar へ
     → capture が user_hash（メール sha256 先頭 16 桁・メール無しトークンは sub 代替）を捕捉ログに自動付与
stats(:8766) / recommendations(:8765) … コード変更なし（env だけで両サービスに装着＝共通化は構造で担保）
```

### env 契約（サービス横断で同名・`mcp_http.py` docstring が正）

| env | 意味 |
|---|---|
| `MCP_AUTH_ISSUER` | IdP の issuer URL。**トークンの `iss` と完全一致**で書く（Auth0 は末尾スラッシュ付き） |
| `MCP_AUTH_AUD` | 期待する `aud`。設定時のみ検証（IdP のトークン仕様を dev 検証で確定させ、載るなら必ず設定） |
| `MCP_AUTH_RESOURCE_URL` | PRM の `resource`＝公開 MCP URL（例 `https://stats.<domain><秘密パス>`） |

deploy 側の一本道：`deploy/env/*.env` に `AUTH_ISSUER`・`AUTH_AUD_STATS`・`AUTH_AUD_MCP` →
`deploy.sh <env> secrets` が SSM `auth_issuer`・`auth_aud_<svc>` に登録 → bootstrap ⑧が該当サービスの
`/etc/polyarchy/<svc>.env` に `MCP_AUTH_*` を書く（resource URL は `TUNNEL_HOST_<SVC>`＋秘密パスから合成）。
`AUTH_AUD_<SVC>` が**サービス毎の有効化スイッチ**（空なら従来どおり）。切り戻し＝SSM の `auth_aud_<svc>`
削除 → bootstrap 再走行 → restart（案 A と同じ作法）。

## 2. 作業リスト（全体）

### 段1 ローカル実装（済・2026-09-02）
- [x] `polyarchy_common/access.py`：`make_oidc_verifier`（ディスカバリ→JWKS・iss/aud/期限）＋
      `oidc_bearer_middleware`（PRM 配信・401 に `WWW-Authenticate`・contextvar）＋ `user_hash` の sub 代替
- [x] `polyarchy_common/mcp_http.py`：`MCP_AUTH_*` で自動装着（案 A と独立・併用可）
- [x] 単体テスト（`test_oidc_middleware_user_context`）＝PRM 200／欠如・不正 401＋ヘッダ／成功時 contextvar／sub 代替
- [x] deploy 配線：`lib.sh do_register_secrets`・`bootstrap.sh ⑧`・env example 2 本
- [x] 本設計書の起案・残タスク A6 登録

### 段2 IdP テナントと dev 検証（済 2026-09-02）
- [x] **（開発者の手作業）** WorkOS アカウント作成＝Project `Polyarchy.net`・環境 Staging・
      **issuer=`https://<staging-issuer>.authkit.app`**（Staging はドメイン名変更不可・実値は env／SSM）。
      Connect で DCR ON（`registration_endpoint` 開通・CIMD 対応も宣言）＋Resource indicator=`https://stats-dev.<domain>/mcp`。
- [x] stats-dev を案 B env で起動（`MCP_AUTH_ISSUER/AUD/RESOURCE_URL`）。
      ★片付け＝案 A 検証の Access アプリ `polyarchy-dev` がエッジで PRM/401 を横取りしていたため削除（2026-09-02。
      再作成は access-oauth.sh apply）。
- [x] 機械検証 PASS：公開 URL の PRM 200（authorization_servers=AuthKit）・未認証 401＋`WWW-Authenticate`・healthz 200。
- [x] **経路① claude.ai カスタムコネクタ PASS（2026-09-02）**＝DCR→AuthKit サインアップ→ログイン→
      `list_datasets`/`lookup_panel` 実行・捕捉ログに `user_hash` 付与。
      **実測**：`aud` はトークンに載り検証通過（`MCP_AUTH_AUD`=resource URL で確定）。**`email` は既定では載らない**
      → `user_hash` は sub 代替（`sub:` 接頭辞）で稼働。メールを利用者キーにするには **JWT テンプレート**
      （Dashboard → Authentication → JWT templates に `{"email": "{{user.email}}"}`）を設定する。
- [x] JWT テンプレート（`{"email": "{{user.email}}"}`）設定後、メール由来 `user_hash` へ切り替わりを実測（2026-09-02
      09:46 のリクエストから。sub 代替→メール由来の切替は捕捉ログ上は別利用者に見える＝本番導入時はテンプレートを先に設定してから配布する）。
- [x] refresh 確認＝再ログインなしで新トークン（email クレーム入り）に切り替わっている＝refresh_token による無言更新が機能。
- [x] **経路② Claude Code PASS（2026-09-02）**。★実測＝Claude Code は認可要求に `resource` パラメータを
      付けず AuthKit が `invalid_target` で拒否 → **WorkOS の Resource indicator を Default 指定**して解決
      （resource 省略クライアントに既定値を適用。本番テナントでも必須設定）。
      ★副作用＝PRM の `resource` が公開 URL になるため、**localhost 直結の HTTP 接続（127.0.0.1:8766 直指定）は
      クライアント側の resource 検証で弾かれる**＝ローカル利用は stdio 経路（無変更）か公開 URL 経由に寄せる。
- [x] **経路③ ChatGPT カスタムコネクタ PASS（2026-09-02）**＝DCR→AuthKit ログイン→`list_datasets` 実行・
      メール由来 `user_hash` を捕捉（OpenAI エッジ ICN 経由を実測）＝**authless 時代の KPI 盲点（ChatGPT バイパス）が閉じた**。
      **＝段2 完了（3 経路すべて合格・2026-09-02）**。
- [x] `mcp_smoke` PASS（2026-09-02・stdio 経路は無変更）。

### 段3 ②導入団体展開（prod 転写と同時）
- [ ] **（導入団体）** 導入団体自身の WorkOS アカウント作成＝prod の IdP テナント（★アカウント所有の原則・2026-09-02：
      導入団体展開は AWS・Cloudflare・WorkOS・Anthropic・OpenAI の**全外部サービスを導入団体自身のアカウントで**運用し
      開発者個人のアカウントに依存しない）。設定は要点 3 つ＝JWT テンプレート先行・
      DCR ON・Resource indicator＋Default を staging リハーサルどおりに再現（PROD_MIGRATION §2.5）。
- [ ] **staging リハーサル（2026-09-02 方針）**＝staging の URL は誰にも周知していない（利用者は開発者のみ）と
      確認できたため、**staging の stats/recommendations 両方に AUTH を点けて「prod にそのまま載せる完成形」を
      事前検証**する。手順と env の書き方＝deploy/PROD_MIGRATION.md §2.5（AUD＝resource URL＝秘密パス込みの
      三点一致に注意）。★実測済（2026-09-02）＝**2 サービス×Default 1 つでも Claude Code は非 Default 側に
      認証成功**（v2.1.258・resource パラメータ送出に対応した模様＝8 月の invalid_target は旧版。Default 指定は
      旧クライアント保険として維持）。副作用＝開発者自身のコネクタ登録は全部再登録・再認証（stdio は無影響）。
      **進捗**：ドメイン移行（polyarchy.net）→ ホスト名整理 → 両サービス認証点灯 → 機械検証 PASS →
      **3 経路 PASS（2026-09-02 全て・claude.ai／Claude Code〔両サービス〕／ChatGPT・新ドメイン×認証）**。
      残＝箱の mcp_smoke ゲートのみ＝**リハーサル実質完了**（導入団体 prod は同じ手順の転写だけ）。
- [ ] 名簿限定の入口方式の決定（招待制 or ドメイン制限 or CSV 一括・導入団体の名簿管理と合わせて合意）。
- [ ] プライバシーポリシー改訂（`deploy/pages/`）：個人認証の導入・利用者キー（user_hash）・30 日削除は既存の約束どおり。
- [ ] prod.env に `AUTH_*` を設定して転写手順（`deploy/PROD_MIGRATION.md`）に載せる。stats・recommendations の両方に点ける。
- [ ] 名簿運用の定常化（利用者の追加・削除の反映手順を導入団体側の窓口と決める）。

### 段4 ③運営者による個人向け提供（時期未定）
- 認証の仕組みは②と同じ（別テナント）。契約状態を持つ場合は IdP のクレームで持ち、サーバは JWT を見るだけにする。
- 提供者側の準備（取得元ごとの商用利用条件の確認・利用規約・課金）は本リポジトリの外。取得元の条件＝[stats/docs/再配布条件.md](../stats/docs/再配布条件.md)・[recommendations/docs/再配布条件.md](../recommendations/docs/再配布条件.md)。

## 3. 地雷（実測済み・再掲）

- **IP 許可は OAuth と両立しない**（ディスカバリは Anthropic 公表レンジ外・IdP ログインは外部ドメイン）。
  guard は `IP_ALLOWLIST=off`＋除外（`/.well-known/` `/cdn-cgi/` `/healthz`）を維持。`SERVICE=mcp` で apply しない。
- **guard の `/.well-known/` 除外が PRM 配信の生命線**（塞ぐとコネクタ登録が失敗＝案 A で実測）。
- **bootstrap 再走行はコードを更新しない**＝本実装を箱に届けるには tar 再展開（`deploy.sh <env> upload`）が必須。
- IdP 一時不達でもサービス起動は落ちない（JWKS は初回検証時に取得）が、その間の認証付きリクエストは 401 になる。
