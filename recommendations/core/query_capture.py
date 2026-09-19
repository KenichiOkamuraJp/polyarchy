"""
Phase 12：実運用クエリの永続捕捉（質問→検証文パイプラインの「捕捉点」）。

MCP サーバ（`recommendations.serving.mcp_server` の search_policy_docs）が受けた実クエリを、揮発的な
stderr ログとは別に **追記専用 JSONL** へ残す。これが Phase 12 パイプラインの
「燃料タンク」＝トリアージ→ゴールド構築→ゲート→ユーザー由来 eval への原資になる。
※`recommendations.eval.eval` は search_api を直呼びするため、捕捉を MCP 側に置けば評価 197 問は混入しない。

設計上の鉄則：
1. **検索経路を絶対に壊さない**（fail-open）。捕捉は付随機能であり、書き込み失敗・
   ディスク不全・権限エラー等は全て握り潰して検索応答を続行させる。
2. **stdout を汚さない**（MCP stdio 安全）。書き込み先はファイルのみ。異常の通知も
   `polyarchy_common.logsetup` の日本語ロガー（stderr）に限る。
3. **ローカル完結・依存追加なし**（標準 json/os のみ）。

記録スキーマ（1行1レコード・JSON）：
  ts           : ISO8601 ローカル時刻（datetime.now）
  query        : 検索クエリ（日本語・原文）
  orgs/since/until/field/top_k : 適用条件（MCP 引数そのまま）
  result_count : 返却チャンク数（0＝棄却/未ヒット候補のシグナル）
  top_files    : 返却チャンクのファイル名（順序保持・重複排除）
  top_orgs     : 返却チャンクの団体（重複排除）
  source       : 捕捉元（既定 "mcp"）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence

from recommendations.core.config import DATA_DIR
from polyarchy_common.capture import append_record, dedup as _dedup, load_records

# 追記専用の永続クエリログ。トリアージ CLI（phase12_pipeline.py）が読む唯一の入力。
# 既定は data/query_log/queries.jsonl。環境変数 POLYARCHY_QUERY_LOG で上書き可
# （テストは本番ログを汚さぬよう別パスへ向ける／将来のデプロイでログ位置を設定する）。
QUERY_LOG_DIR = DATA_DIR / "query_log"
QUERY_LOG_PATH = Path(os.environ.get("POLYARCHY_QUERY_LOG") or (QUERY_LOG_DIR / "queries.jsonl"))


def capture_query(
    query: str,
    *,
    orgs: Optional[Sequence[str]] = None,
    since: Optional[int] = None,
    until: Optional[int] = None,
    field: Optional[str] = None,
    top_k: Optional[int] = None,
    result_count: Optional[int] = None,
    top_files: Optional[Sequence[str]] = None,
    top_orgs: Optional[Sequence[str]] = None,
    source: str = "mcp",
    path: Path = QUERY_LOG_PATH,
) -> bool:
    """実クエリを JSONL に1行 append する（best-effort・戻り値=成功可否）。

    ★この関数は**例外を投げない**（fail-open）。捕捉は検索の付随機能であり、失敗しても
      検索応答を妨げてはならない。異常は stderr の日本語ログに warning を残すだけ。
    """
    try:
        rec = {
            "query": query,
            "orgs": list(orgs) if orgs else None,
            "since": since,
            "until": until,
            "field": field,
            "top_k": top_k,
            "result_count": result_count,
            "top_files": _dedup(top_files),
            "top_orgs": _dedup(top_orgs),
            "source": source,
        }
    except Exception:  # fail-open：引数が不正でも検索を止めない
        return False
    return append_record(path, rec)  # ts は共通実装が先頭付与（従来と同じ列順）


def load_queries(path: Path = QUERY_LOG_PATH) -> list[dict]:
    """捕捉済みクエリをファイル順（=時系列）で読み出す（壊れ行はスキップ＝fail-open）。"""
    return load_records(path)
