"""
polyarchy_common — Polyarchy 全サービス（recommendations / stats / …）が共有する**共通契約**。

ここに置くのは「兄弟サービスが同じ意味で持つべきもの」だけ（長期開発計画 §3・§6-2）：

- `metadata_core`  … メタデータ共通コア（8欄）と **公開層の不変条件**（唯一の定義場所）
- `taxonomy`       … 分野タグ 21 分類（連邦の突き合わせ軸）
- `logsetup`       … 日本語ログ・英語ノイズ抑制・**MCP stdio 保護**
- `capture`        … 捕捉ログ（追記専用 JSONL）の共通実装＝週次トリアージ→eval の燃料
- `access`         … HTTP 入口の認証の装着点 install_auth（現行＝外部 IdP の Bearer 検証・案 A の Access は温存）
- `mcp_http`       … MCP Streamable HTTP 待受の定型

運用の集約点（共通契約ではない）：`ops_dashboard`・`usage_report` は全サービスを横断して箱の状態と利用を
集計する運用の道具（サービスの名簿・ポート・捕捉ログの置き場を知っている）。共通契約ではないので、サービスは
ここを import しない（ops_dashboard が recommendations を遅延 import するのはこの向きだけ）。

置かないもの：検索スタック・設定（パス／コレクション名）・評価。これらはサービス固有。
依存は最小（標準 logging / json / pathlib。access のみ PyJWT、mcp_http のみ mcp/uvicorn を遅延 import。
capture は認証済み時の user_hash 付与のため access を遅延 import する＝失敗しても捕捉は続く）。
"""
