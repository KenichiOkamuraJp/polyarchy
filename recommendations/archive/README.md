# recommendations/archive — 過去フェーズの記録（本線から未参照・実行保証なし）

> **状態：記録（過去フェーズのコードの退避先の説明・実行保証なし）。**

リファクタリング（2026-08-19・所見 `../../docs/リファクタリング所見_2026-08-19.md` 段 1）で
本線（`python -m recommendations.…` の入口・ゲート 5 本・README の実行例）から到達しないファイルを退避した。
旧コレクション（Chroma `policy_docs_v3`〜`v5`）や旧モジュール配置を前提にするものが多く、**そのままでは動かない**。
参照したくなったら git 履歴で元の位置と当時の README を確認すること。

| ファイル | 元の場所 | 内容 |
|---|---|---|
| `phase3.py` | `recommendations/eval/` | Phase 3 埋め込みモデル横断比較（構築＋評価） |
| `phase3_retest.py` | `recommendations/eval/` | Phase 3 の 78 問再テスト |
| `phase7_perf.py` | `recommendations/eval/` | Phase 7 レイテンシ計測（コレクション名引数） |
| `rerank_eval.py` | `recommendations/eval/` | Phase 5 リランカー比較（Chroma 直叩き） |
| `rerank_bench.py` | `recommendations/eval/` | リランカー比較を本番経路に載せ替えた版＋レイテンシ |
| `hybrid_eval.py` | `recommendations/eval/` | ハイブリッド（BM25＋ベクトル）の K 感度実験（Chroma 直叩き） |
| `qdrant_parity.py` | `recommendations/eval/` | Phase B：Chroma→Qdrant 移行時の検索結果パリティ確認 |
| `batch1_build.py` | `recommendations/ingest/` | Phase B1：v5→policy_claims_v6 クローン＋issuer 付与 |
| `dump_doctext.py` | `recommendations/ingest/` | 文書テキストのダンプ（eval/doctext 作成用） |
| `query.py` | `recommendations/serving/` | Chroma 専用の CLI 質問応答（本線 Qdrant では起動不能）。生成プロンプトは `recommendations/core/prompts.py` へ移した |
| `phase2.py` | `recommendations/eval/` | Phase 2 チャンク戦略の横断比較（Chroma へ戦略別コレクションを構築して比較＝旧 `ingest.build_index` 依存。バッチ2 段4〔2026-08-28〕の Chroma 全廃で退避） |
| `scratchpad/` | `recommendations/scratchpad/` | 各フェーズの使い捨てスクリプト・ログ（git 管理外・`.gitignore` の `scratchpad/`） |

削除したもの：`recommendations/eval/retrieval_only.py`（`python -m recommendations.eval.eval --retrieval-only` が上位互換）。
