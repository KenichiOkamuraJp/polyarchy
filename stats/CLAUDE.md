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
