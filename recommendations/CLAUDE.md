# recommendations — 政策主張DB（Polyarchy の基幹サービス）

> **状態：規約（セッション作業規約）。**

このフォルダで作業するセッションへの規約（2026-09-02 作成・stats/CLAUDE.md と対称。**現時点で固有の特記事項は最小**＝
recommendations の開発は主にリポ root で全体を見ながら行われてきたため。閉じたセッション運用を始めたら拡充する）。

## 読む範囲（厳守）

- 読んでよい：`recommendations/` 配下すべて、`polyarchy_common/`（共通契約＝**`polyarchy_common/README.md` が正典**・各 docstring が詳細仕様）、`deploy/`（配信）、`docs/`（全体計画）
- **読まない**：`stats/`（統計参照DB）のソース。必要なのは契約だけで、それは `polyarchy_common/README.md`（共通部）＋
  `recommendations/docs/共通契約.md`（本サービスの各論）に書いてある。stats/docs/共通契約.md はあちらの各論＝読まなくてよい。

## このサービスの性格

- 5 団体（経団連・同友会・日商・連合・政府）の公表文書を**抜粋単位**で返すハイブリッド検索。**生成・要約はしない**。
- **層は公開固定**（layer 引数を作らない＝物理遮断）・該当なしは results 空＋収録範囲の負のスコープ宣言。
- 評価は hit@5／MRR のアンカー（正典＝ルート README「品質の担保」）＋フィルタ・多段・smoke の4ゲート。
- 契約値（top_k・sweep 上限・diversify）＝ `recommendations/docs/共通契約.md` §5。

## 実行（リポジトリ root を cwd に）

```bash
python -m recommendations.serving.mcp_server                       # stdio
python -m recommendations.serving.mcp_server --http --port 8765    # Streamable HTTP（配信形）
python -m recommendations.eval.eval --retrieval-only --eval-set both   # ゲート（基準は README）
python -m recommendations.eval.mcp_smoke
```

新規文書の取込（収集→追い判定→増分取込→ゲート→配布）＝ `deploy/RUNBOOK_OPS.md` §5。

## 運用地雷（共有・stats/CLAUDE.md と共通）

- ★ **Mac で `cloudflared tunnel run` を実行しない**：同じトンネルを 2 箇所で run すると本番トラフィックが分流する（箱のトンネル資格情報は作業用 PC に置かない）。
- ★ **MCP の IP 許可リストは使えない**：コネクタは Anthropic/OpenAI のサーバ側 IP から接続してくる。
- ★ **bootstrap 再走行はコードを更新しない**：箱へのコード反映は tar 再展開が必須＝2026-09-03 から自動適用（`polyarchy-data-apply`）が tar を再展開する。手動は非常時のみ（deploy/README「コード/データ更新」）。
- 秘密は SSM。`.env` を箱に運ばない。qdrant の data sync は qdrant 停止中にのみ行う。
