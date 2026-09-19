# polyarchy_common — 共通契約（全サービスが受け取るもの・約束するもの）

> **状態：正典（共通契約の正典）。**

版: 2026-09-02（stats/docs/共通契約.md〔2026-08-15〕から共通部を昇格・一本化）／位置づけ: **正典**。ライセンス＝ルート README「ライセンス」（Apache-2.0）。
仕様の詳細は各モジュールの docstring（そちらが常に優先）。本書は「何があり・どう使うのが契約か」の1枚。
**各サービス固有の差分・名前・配信・契約値は各論**＝[stats/docs/共通契約.md](../stats/docs/共通契約.md)・
[recommendations/docs/共通契約.md](../recommendations/docs/共通契約.md)。

## 1. 提供モジュール（受け取るもの）

| モジュール | 提供物 | 全サービス共通の使い方（契約） |
|---|---|---|
| `metadata_core` | `COMMON_CORE`（corpus・org・title・date・date_int・source_url・**layer**・lang）／`ALLOWED_LAYERS`／`PUBLIC_LAYER`／`validate_payload(payload)->list[str]` | 発見層に載せる単位（文書チャンク・統計系列）のメタデータは**共通コア 8 欄を必ず持つ**。取込時に `validate_payload` で違反 0 を機械確認。**層（公開/機密）は全コーパス共通の不変条件** |
| `taxonomy` | `POLICY_TAGS`（分野タグ 21 分類）／`TAGS`（英字キー）／`is_valid_tag` | 各単位に 1〜3 個付与。**全サービス同じ語彙**＝「主張↔事実」の突き合わせ軸（コーパス連邦）。**語彙を各サービス側で複製しない**（英字キーで参照＝語彙に無いキーは起動時に KeyError） |
| `logsetup` | `configure_quiet_logging()`／`get_logger(name)`／`guard_stdout_for_stdio()`／`quiet_stdout()` | MCP stdio では **stdout がプロトコル線**。起動時に `guard_stdout_for_stdio()` を最初に呼ぶ。ログは全て日本語で stderr |
| `capture` | `append_record(path, record)`／`load_records(path)`／dedup | 実クエリの捕捉（評価の燃料）。レコード形と置き場所は各サービスが決める（各論参照）。**利用者キーは `user_hash`（メール sha256 先頭 16 桁・平文を残さない）を自動付与** |
| `access` | `access_jwt_middleware`（案 A・Cloudflare Access）／`oidc_bearer_middleware`＋PRM（**案 B・外部 IdP**＝2026-09-02 から本線） | 直接は使わない（`mcp_http` が env を見て自動装着） |
| `mcp_http` | `serve_streamable_http(mcp, host, port, path, tools_desc, health_check)` | HTTP 公開はこの 1 関数。env は全サービス同名。`mcp_http` が読むのは `MCP_AUTH_ISSUER/AUD/RESOURCE_URL`（案 B＝設定時は Bearer 検証＋PRM 配信・401 誘導）・`MCP_ACCESS_*`（案 A・温存）・`MCP_ALLOWED_HOSTS`。`MCP_HTTP_PATH`（秘密パス）は**各サービスの `mcp_server` 側が読んで path 引数に渡す**（bootstrap ⑧ が同じ env ファイルに書くため実務上は一体）。`GET /healthz` は認証不要で 200/503 |
| `httplog` | アクセスログ 1 行形式 | CloudWatch 設定と結合＝**形式変更は deploy と同時** |
| `usage_report` | 週次利用レポート生成 | 数字のみ・検索語なし（運用設計 §4.2） |

## 2. 入口の作法（全サービス共通の約束＝MCP サーバの不変条件）

- **検索・参照専用**：外部作用（送信・書込）を持つツールを公開しない。全ツールに `title`＋`ToolAnnotations(readOnlyHint=True)`。
- **層は公開固定**：ツールに `layer` 引数を作らない＝非公開データが混ざる余地を構造的に持たない（フェイルクローズ）。非公開文書を扱うサービスは Polyarchy の範囲外＝**境界の外は作らない**（長期開発計画 §2）。層の不変条件は公開 DB を守るための契約として残す。
- **fail-closed**：該当データが無ければ「ない」と返す（found=false／results 空＋理由）。近似・補間・隣接値の代用をしない。
- **stdout クリーン**（stdio）／**stateless HTTP**（配信）。ツール説明文は利用者 Claude のルーティング＝「何を返し・何を返さないか」を明記。
- **説明可能な出典**：返す内容には必ず出典を付ける（形は各論）。
- **認証**：配線は案 B（外部 IdP）＝`AUTH_AUD_<SVC>`（SSM `auth_aud_<svc>`）が**サービス毎の有効化スイッチ**（未設定なら authless で起動する＝切り戻し手段として仕様上残す）。**現行運用（2026-09-02〜）は全 HTTP 配信でスイッチ ON＝ログイン必須**が方針（WorkOS AuthKit・`docs/個人認証_案B設計.md`）。stdio（ローカル）は無認証のまま。
- **捕捉ログは 30 日で削除**（logprune timer＋S3 ライフサイクル＝両方で担保。プライバシーポリシー記載と一致させる）。

## 3. 依存の書き方

追加依存は `pyproject.toml`（リポ root）の `[project.optional-dependencies]` へ**サービス別に**書く
（共通＝mcp/PyJWT/uvicorn・`recommendations`＝検索スタック〔torch/ruri を引く重い側〕・`stats`＝軽量）。
stats 単独の箱は `pip install -e ".[stats]"` で torch を引かない。ローカルは `.[recommendations,stats]`。

## 4. 検証

- 共通契約の単体テスト：`python -m polyarchy_common.tests.test_common`（ゲートの一部＝ルート README「品質の担保」）。
- 共通部を変えたら**両サービスのゲートで不変を確認**してから合わせて各論を更新する。
