"""
eval 共通の入出力（`full.py`・`retrieval.py`・`filters.py` が共用）：評価セットのパス・読み込み、
source_node → ファイル名。採点ヘルパは `_scoring.py`。
バッチ2 段4（2026-08-28・Chroma 全廃）：chromadb 依存と評価用コレクション参照
（_eval_collection_and_index）を削除＝各ハーネスは PolicySearchService／Qdrant を直接使う。
"""

import json
from pathlib import Path

from recommendations.core.config import DATA_DIR

EVAL_SET_PATH = DATA_DIR / "eval" / "eval_set.json"

# Phase 12：ユーザー由来ゴールドの別名前空間（正典とは別扱い＝provenance 分離）。
USERDERIVED_SET_PATH = DATA_DIR / "eval" / "eval_set_userderived.json"
FILTER_EVAL_PATH = DATA_DIR / "eval" / "eval_filter.json"
RESULTS_DIR = DATA_DIR / "eval" / "results"


def load_eval_set(which: str = "canonical") -> list[dict]:
    """評価セットを選んで読む。Phase 12 で正典とユーザー由来を別扱いにするための入口。

    which="canonical"（既定）＝正典のみ＝**従来と完全に同一の挙動**（確定的アンカーの母集団）。
    which="user"  ＝ユーザー由来のみ。 which="both" ＝正典＋ユーザー由来を連結。
    未存在ファイルは空リスト扱い（後方互換）。
    """
    canon = json.load(open(EVAL_SET_PATH, encoding="utf-8")) if EVAL_SET_PATH.exists() else []
    user = (json.load(open(USERDERIVED_SET_PATH, encoding="utf-8"))
            if USERDERIVED_SET_PATH.exists() else [])
    if which == "user":
        return user
    if which == "both":
        return canon + user
    return canon


def source_name(node) -> str:
    """source_node からファイル名を取り出す（file_name → file_path の basename → 不明）。"""
    metadata = node.node.metadata
    return (
        metadata.get("file_name")
        or Path(metadata.get("file_path", "")).name
        or "不明"
    )


