# deliberations — 審議会議事録DB

> **状態：規約（セッション作業規約）。**

このフォルダで作業するセッションへの規約（2026-10-03 作成）。設計＝[docs/審議会議事録DB_開発計画.md](../docs/審議会議事録DB_開発計画.md)、進捗＝[docs/残タスク.md](../docs/残タスク.md) D。

## 読む範囲（厳守）

- 読んでよい：`deliberations/` 配下すべて、`polyarchy_common/`（共通契約）、`polyarchy_retrieval/`（検索部品の共有ライブラリ＝政策主張DB も使う。変えたら両方のゲートで不変を確認）、`deploy/`、`docs/`。
- **読まない**：`recommendations/`・`stats/`・`companies/` のソース。

## このサービスの性格

- 政府の会議体の**過程文書**（回ごとの配布資料・議事録・議事要旨）。会議の**決定文書は政策主張DB の gov**（ここには入れない）。
- 返す断片には毎回、会議体・回次・開催日・資料番号・提出者／発言者と区分（政務／事務局／府省・会議体／構成員（政府外）／外部（ヒアリング）／不明）。構成員・外部・不明には「会議・政府の決定ではない」の注記（返り値の notes）。
- **推定しない**：発言者・提出者は記録と資料に書かれた文字列からだけ。匿名の要約の発言は不明。提出者の記載の無い資料は会議資料の慣行により事務局（presenter_basis＝既定で区別）。
- 層は公開固定（ツールに layer 引数を作らない）。ツールは 2 本（search_deliberations・list_meeting）。

## 文書

- [docs/再配布条件.md](docs/再配布条件.md)（設計）＝取得元の規約・`license` の判定・返す形の契約値
- [docs/収録範囲.md](docs/収録範囲.md)（正典）＝何が入っていて何が入っていないか（数は `core/search.coverage()`）
- `docs/deliberations.html`＝公開ページの原稿（**箱で有効にしてから `deploy/pages/` へ移して入口からリンク**。先に `deploy/pages/` に置くと運用側の配置で公開される）

## 実行（リポジトリ root を cwd に）

```bash
python -m deliberations.ingest.collect             # 収集（一覧→回→資料・記録。手元にあるファイルは取り直さない）
python -m deliberations.ingest.build               # 解析・帰属・検索の単位（data/cache/units.jsonl）
python -m deliberations.eval.attribution_gate      # 帰属の正しさ（取り違え 0）
python -m deliberations.ingest.qdrant_ingest       # Qdrant へ（専用の qdrant-delib・:6340）
python -m deliberations.eval.retrieval             # 検索のアンカー（基準はルート README「品質の担保」）
python -m deliberations.eval.layer_gate            # 層・索引の fail-closed
python -m deliberations.eval.mcp_smoke
python -m deliberations.ops.bundle export          # 箱へ運ぶ束（Qdrant のスナップショット＋語彙・目録・解析結果）＝M6 の下準備
```

## 地雷（実際に踏んだもの）

- ★ **手元の Qdrant は政策主張DB の `qdrant-dev` と分ける**：`qdrant-dev` のデータは配布（release.sh）で S3 経由で箱へ同期される＝評価前の審議会DB のコレクションを混ぜない。手元は専用コンテナ `qdrant-delib`（`-p 6340:6333`・`deliberations/data/qdrant` をマウント・再起動ポリシーなし）。env は `DELIB_` を付ける（箱の deploy.env の `COLLECTION_NAME` を拾わない）。
- ★ **cas.go.jp は curl の UA に 404**（UA の文字列で弾く型）＝cas.go.jp だけブラウザの UA（`ingest/sources.py`）。JS チャレンジ・ログイン・レート制限は突破しない。
- ★ **本文は NFKC で正規化**：PDF に「⼈⼯知能」のような康煕部首の字形が混じる（第 1 便で 55 本）。
- ★ **一覧の HTML の癖**：記録は回のページに無く一覧にだけある・記録のリンクの `</a>` が閉じていない・表が `<h1>` より前・資料番号が前のセル。
- ★ **長いページは分ける**：文章主体の資料は 1 ページが数千字＝そのまま埋め込むと詰め物で極端に遅い（第 1 便で 13 分 3,000 点）。800 字を超えるページは 600 字程度に分け、埋め込みは長さ順に束ねる。
- ★ **提出者の表示が画像の中のことがある**（束ねた構成員提出資料）＝文字のレイヤーに無い。推定で埋めず不明。
- ★ **箱でも Qdrant は政策主張DB と分ける**：箱の政策主張DB の Qdrant のストレージは自動適用のたびに S3 から `--delete` 付きでミラーされる＝同じ Qdrant に置くと配布のたびに消える。箱は専用の `qdrant-deliberations`（:6340）に束（スナップショット）から復元する。有効化は配布を 2 回に分ける（自動適用のスクリプトの変更は次の次の配布から効く・RUNBOOK §5）。
- ★ **標本のゲートだけで安心しない**：M0 の 40 件ではゲートが通っていたが、全件に当てると名簿の表紙の役割の行を提出者と読む誤りが出た＝規則を変えたらラベルの無い文書も機械で洗う。
