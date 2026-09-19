# Polyarchy — リポジトリ root の作業規約

> **状態：規約（セッション作業規約）。** 起案 2026-09-17。**新しい開発者・AI セッション（記憶ゼロ）が最初に読む 1 枚**。
> フォルダ単位の規約＝[stats/CLAUDE.md](stats/CLAUDE.md)・[recommendations/CLAUDE.md](recommendations/CLAUDE.md)（そのフォルダで閉じて作業するときはそちらが優先）。
> 本書は地図と地雷だけを持つ。定義・数値・タスク・手順は写さず、下の入口へリンクする。

## 1. 最初に読む順（30 分）

1. [README.md](README.md)＝何であるか・構成・**文書の地図**・**品質の担保（ゲートの基準値はここだけ）**
2. [docs/用語と役割.md](docs/用語と役割.md)＝役割（導入団体・PdM・開発者・運用者・監査人）と用語（箱・燃料・ゲート・三点一致）
3. [docs/残タスク.md](docs/残タスク.md)＝いま何が済み、何が残っているか（**進捗の正典はここだけ**）
4. [docs/運用設計.md](docs/運用設計.md) §0＝誰が何をするか（層①〜⑥）
5. 運用するなら [deploy/RUNBOOK_OPS.md](deploy/RUNBOOK_OPS.md)（障害対応・データ更新・依存更新）／環境を建てるなら [deploy/README.md](deploy/README.md)・[deploy/PROD_MIGRATION.md](deploy/PROD_MIGRATION.md)
6. stats の経緯を知るなら [stats/docs/記録/開発経緯と設計判断_2026-08-25.md](stats/docs/記録/開発経緯と設計判断_2026-08-25.md)

## 2. 文書の規則（[docs/文書管理ルール.md](docs/文書管理ルール.md) の短縮版）

- 文書は 4 分類（正典／設計／記録／規約）＋社内ノート（git 外）。冒頭の「状態：」で宣言し、ルート README の地図に登録する。
- 定義は用語集・数値はルート README「品質の担保」・タスクは残タスク＝正典は 1 箇所。他の文書は写さずリンクする。
- 記録（日付つき）は更新しない（古い名前・数値が残っていても直さない）。進捗は残タスクのステータス列（1〜2 行）、経緯は §E 対応履歴（追記のみ）。
- 相手方情報・個人情報／実利用ログ・秘密・内部事情は git に書かない＝`社内ノート/`（`.gitignore` と配布 tar の除外の両方で守られている）。
- 対外文書は内部語を定義してから使い、到達目標を現状として書かない。

## 3. このソフトウェアの性格（変えてはいけないもの）

- **公開データのみ・参照専用・サーバ側で生成 AI を呼ばない。** ツールは読み取り専用。文章の生成は利用者の Claude／ChatGPT。
- **層は公開固定**：ツールに `layer` 引数を作らない。非公開文書を扱うモジュールは**本ソフトウェアの範囲外**（作るなら別リポジトリ＝型は [docs/非公開コーパスの参照設計.md](docs/非公開コーパスの参照設計.md)）。
- **fail-closed**：無ければ「ない」と返す。stats は値を近似・補間しない（近い年・隣の指標を返すのは捏造）。
- **ゲート全 PASS のときだけ配布**（`deploy/scripts/release.sh` が唯一の出口）。アンカー（hit@5／MRR）は回帰を検知するトリップワイヤであって最大化する KPI ではない＝固定の評価問に合わせた検索側の調整（過適合）をしない。基準値を変える変更は README「品質の担保」・`release.sh` の既定値・関連文書を**同一コミット**で直す。
- **サービスは互いのソースを読まない**（共有は `polyarchy_common` だけ）。データ刻印 `policy_claims*`・systemd 名 `polyarchy-mcp` は歴史的な名前のまま据え置き（改名対象外）。
- **利用者はログイン必須**（外部 IdP）。無認証には戻さない。

## 4. 実行の要点（リポジトリ root を cwd に）

```bash
pip install -e ".[recommendations,stats]"          # ローカル（1 env に全部）。箱はロックファイルから（RUNBOOK §7）
python -m recommendations.eval.eval --retrieval-only --eval-set both   # ゲート（基準は README）
python -m stats.eval.exact_match
python -m polyarchy_common.tests.test_common
bash deploy/scripts/release.sh <env>                # ゲート 9 本→S3 配布→箱が 15 分以内に自動適用（失敗時は自動切り戻し）
```

- ★ `release.sh` は**依存の入った Python 環境を PATH の先頭に通して**実行する（素の `python3` だと llama_index 不在で即 FAIL）。
- ローカル stdio で使うなら `.mcp.json.example`（`stats/` にも同名）を `.mcp.json` に複製し、`<PYTHON>`・`<REPO>` を自分の環境のパスに書き換える（`.mcp.json` は git 外）。
- データ（PDF・索引・統計値）は git 外＝S3 が原本。クローンしただけではゲートは回らない（RUNBOOK §3 データ復元）。e-Stat の appId は各自が取得して `stats/.env`（git 外）か環境変数へ。

## 5. 運用地雷（実際に踏んだもの。詳細はリンク先）

- ★ **箱のトンネル資格情報を作業用 PC に置かない・PC で `cloudflared tunnel run` しない**：同じトンネルを 2 箇所で run すると本番トラフィックが分流する。公開実験が要るなら新規トンネル＋新ホスト名で。
- ★ **IP 許可リストは使えない**：Claude／ChatGPT のコネクタは利用者の PC ではなく Anthropic／OpenAI のサーバ側から接続してくる。OAuth のディスカバリとログインも別経路で来る。入口の防御＝ログイン＋秘密パス＋レート制限（`cloudflare-guard.sh` は `IP_ALLOWLIST=off` が既定。秘密パスルールは `/.well-known/`・`/cdn-cgi/`・`/healthz` を除外＝認証の生命線）。
- ★ **認証の三点一致**：IdP の Resource indicator・env の `AUTH_AUD_*`・実 URL（ホスト＋秘密パス）が同一文字列。認証ホストの秘密パスは原則回転しない（RUNBOOK §4）。IdP 側は DCR 有効＋Resource indicator を Default 指定＋JWT に email クレーム（PROD_MIGRATION §2.5）。
- ★ **bootstrap の再走行はコードを更新しない**（コード反映は tar 再展開＝通常は自動適用がやる）。箱を手で触らない。どうしても手動 apply するときは `systemd-cat -t polyarchy-dataapply` を挟む（監査線に残すため・RUNBOOK §5）。
- ★ **依存を変えたらロックを再生成**（`deploy/scripts/lock_deps.sh`→PR→ゲート＝RUNBOOK §7）。`mcp` は `<2` に固定（2.x は FastMCP が無い）。箱の Qdrant は musl ビルド必須。
- ★ **ChatGPT（Business）のコネクタ**：利用者ごとの認可は「個人の設定→アプリ→接続」で行う（作成画面では出ない）。公開後にツール定義を更新できない＝変えたら作り直し。
- ★ **OECD SDMX は 1 時間 60 ダウンロードの制限**（解除手段なし）。取得の UA は curl 相当が既定（IMF／OECD の WAF）。例外は UA 文字列で弾く中小企業白書 PDF だけ＝方針は [stats/README.md](stats/README.md)「取得の作法」（JS チャレンジ・ログイン・レート制限は突破しない）。
- 秘密は SSM Parameter Store。`.env` を箱に運ばない。runbook・シェル履歴・コミットに値を残さない。Qdrant のデータ同期は Qdrant 停止中にのみ行う。
- 捕捉ログは 30 日で自動削除（箱の timer と S3 ライフサイクルの両方）。残したい問は期限内に評価セットへ昇華する（`deploy/scripts/triage.sh`）。

## 6. セッションの振る舞い

- 指示なく `git commit`・`git push`・`release.sh`・`terraform apply`・`deploy.sh` を実行しない。利用（ドッグフーディング）セッションでは実装もしない＝気づきは残タスクへ。
- found=false・results 空は「正しい答え」であって直す対象ではない。勝手に取込を実装したり他の情報源で補ったりしない。
- 新しい収録・機能は「計画＋評価問＋実装」を 1 つの変更にする（評価問を先に立てる）。
- AI の記憶（memory）は PC とフォルダごとに別系統で、引き継がれない。**次の人に残すべき地雷は本書か各フォルダの CLAUDE.md に書く**（地雷に限り、複数の規約への重複を許す）。
