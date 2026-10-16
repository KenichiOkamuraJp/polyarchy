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
| `access` | `access_jwt_middleware`（案 A・Cloudflare Access）／`oidc_bearer_middleware`＋PRM（**案 B・外部 IdP**＝2026-09-02 から本線） | 直接は使わない。**認証の装着点は `install_auth` の 1 関数だけ**（`mcp_http` が呼ぶ・env を読むのもここだけ＝`test_common` が検査）。導入団体が自前の認証に替えるときはこの関数の中身を替える（約束は docstring） |
| `mcp_http` | `serve_streamable_http(mcp, host, port, path, tools_desc, health_check, parallel_safe)` | HTTP 公開はこの 1 関数。env は全サービス同名。`mcp_http` が読むのは `MCP_ALLOWED_HOSTS` と、ツールの実行の `MCP_TOOL_OFFLOAD`（既定＝別のスレッドで動かす・`0` でイベントループの上）・`MCP_TOOL_CONCURRENCY`（同時に動かす数・既定 1。2 以上が効くのは `parallel_safe=True` を渡したサービスだけ＝同時に流しても結果が同じことを各サービスの `eval/concurrency` で確かめたもの・2026-10-14）・`MCP_TOOL_QUEUE_MAX`・`MCP_TOOL_WAIT_SECONDS`（並びすぎ・待ちすぎは「混んでいる」のエラーの結果＝「該当なし」と形を分ける・2026-10-14）、認証の env（`MCP_AUTH_*`＝案 B・`MCP_ACCESS_*`＝案 A）は `access.install_auth` が読む。待受アドレスは各 `mcp_server` が `MCP_HTTP_HOST`（既定 127.0.0.1・箱では deploy.env）から取る。`MCP_HTTP_PATH`（秘密パス）は**各サービスの `mcp_server` 側が読んで path 引数に渡す**（bootstrap ⑧ が同じ env ファイルに書くため実務上は一体）。`GET /healthz` は認証不要で 200/503 |
| `httplog` | アクセスログ 1 行形式／`client_label`・`current_client`（接続元のクライアントの種別＝User-Agent を決まったラベルに丸める） | CloudWatch 設定と結合＝**形式変更は deploy と同時**。ラベルは `capture` が捕捉ログの `client` に付ける＝足すときは実測してから・公開のプライバシーページも同じ変更で |
| `usage_report` | 週次利用レポート生成 | 数字のみ・検索語なし（運用設計 §4.2） |
| `edge_probe` | 外からの到達の確認（公開 URL の `/healthz`・守るパスの 401・ディスカバリの照合・IdP の公開鍵） | 資格情報なし・読み取りだけ。認証の env は `access.idp_config` 経由で読む。パスを出力に出さない（運用設計 §1.1） |
| `relay` | `relay_config(env, public_port, log)`／`relay_dispatch(...)`＝中継の口（bot 専用・ログインなし・既定は閉・2026-10-16） | 直接は使わない（`mcp_http` が env `MCP_RELAY_PORT`・`MCP_RELAY_ALLOW_CIDRS` を見て装着）。ルート CLAUDE.md §3 の唯一の例外＝条件（プライベートの範囲・会員の印が必須・公開の口では印を信じない）を緩めない。会員の印は `access.user_hash()`・経路は `httplog.current_client()`＝`relay` |
| `top_user` | 直近 1 時間の最多の 1 人の呼び出しの数（全サービスの捕捉ログを利用者ごとに数える＝箱の `polyarchy-topuser.timer` が `top_user_calls_1h` に送る・B44 第 1 便） | 箱から出すのは数だけ（利用者のハッシュは出さない）。`--show`（多い順のハッシュ）と `--hash-of`（メール → ハッシュ）は運営者が手元で誰かを確かめるときだけ |

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

**文書検索の部品は本パッケージに入れない**（2026-10-03）＝分かち書き・BM25・RRF・埋め込み/リランカーの登録・
Qdrant のストアは共有ライブラリ `polyarchy_retrieval`（[docstring](../polyarchy_retrieval/__init__.py) が仕様）。
torch・sentence-transformers を引くため、共通契約に入れると stats 単独の箱の依存の分離が崩れる。
使うのは文書検索のサービス（政策主張DB・審議会議事録DB）だけで、依存は extras `recommendations` に含まれる。
層の不変条件は `polyarchy_retrieval` の側でも守る（検索の Filter に `layer` の条件が無ければ拒む）。

## 4. 検証

- 共通契約の単体テスト：`python -m polyarchy_common.tests.test_common`（ゲートの一部＝ルート README「品質の担保」）。
- 共通部を変えたら**両サービスのゲートで不変を確認**してから合わせて各論を更新する。
