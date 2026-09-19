# deploy/pages — 公開ページ（Cloudflare Pages に置く静的サイト）

> **状態：正典（公開ページの原稿と配置）。**

公式コネクタ化（`docs/公式コネクタ要件.md` §2.4）で求められる **プライバシーポリシー・利用規約・ドキュメント（利用例 3 つ・
e-Stat API クレジット文・サポート連絡先）** の原稿。箱（EC2）とは独立に公開でき、FREEZE 中でも更新できる。

| ファイル | 公開 URL（Pages プロジェクト `polyarchy-docs`・カスタムドメイン `docs.polyarchy.net`＝2026-09-02 移行（旧 `docs.policy-database.com` エイリアスは 2026-09-02 撤去）・予備 URL https://polyarchy-docs.pages.dev/） | 用途 |
|---|---|---|
| `index.html` | `/` | 入口（サービス一覧・各ページへのリンク） |
| `terms.html` | `/terms` | 利用規約（提供範囲・免責・料金は別途・第三者データの条件継承） |
| `privacy.html` | `/privacy` | プライバシーポリシー（Listing 必須・不備は即却下）：捕捉ログの内容・30 日保持・user_hash・第三者提供なし・連絡先 |
| `stats.html` | `/stats` | stats（統計参照DB）のドキュメント：接続方法・ツール・**利用例 3 つ**・**e-Stat API クレジット文**・license の見方・取得元一覧 |
| `recommendations.html` | `/recommendations` | recommendations（政策主張DB）のドキュメント：接続方法・ツールの引数と返り値・収録範囲（発行体別文書数・分野タグ 21 分類・帰属の注意）・**利用例 5 つ**・結果の見方・収集する情報・制限・サポート（2026-08-19 claims セッションで作成・2026-08-27 改名） |
| `claims.html` | `/claims` | 旧 URL の転送スタブ（改名 claims → recommendations・2026-08-27）。`/recommendations` へ meta refresh |

## 埋めた値（2026-08-19）
- 運営者＝岡村 健一（個人運営）・連絡先＝kenichi.okamura.jp@gmail.com（サポート＝脆弱性報告窓口兼用）。変える場合は全 HTML のフッタと各ページの連絡先を一括置換。
- 料金＝「別途定める・当面無償」。
- 接続 URL（stats/recommendations とも）＝**ページに書かない**＝個別案内（秘密パス込み URL・ログイン必須〔案 B・2026-09-02〕）。コネクタのディレクトリ掲載をする段で正式 URL 記載を再検討。

## 配置（Cloudflare Pages・direct upload）＝2026-08-19 初回配置済
トークン＝Keychain `cloudflare-access-token`（Cloudflare Pages:Edit・Account Settings:Read・User Memberships:Read・Zone DNS:Edit ほか）。更新のたびに下を実行：
```bash
# wrangler が無ければ npx で（要 Node）。プロジェクト名 polyarchy-docs・本番ブランチ main。
export CLOUDFLARE_API_TOKEN="$(security find-generic-password -s cloudflare-access-token -w | tr -d '\r\n')" CLOUDFLARE_ACCOUNT_ID=<ACCOUNT_ID>
npx -y wrangler@latest pages deploy deploy/pages --project-name polyarchy-docs --branch main --commit-dirty=true
```
カスタムドメインは API で追加済み（Pages domains＋CNAME `docs` → `polyarchy-docs.pages.dev`・Proxied）。**クリーン URL**：Pages は `terms.html` を `/terms` でも配信する。

## 書き方の規約
- 依存ゼロ（外部 CSS/JS/フォントなし・1 ファイル完結。共通 CSS は各 HTML にインライン＝別ファイルを持たない）。
- 「何を返し・何を返さないか」を書く。誇張しない（値は保証しない・原典で確認）。
- 変更したら `更新日` を書き換える。
