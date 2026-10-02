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


def dump_topk(topk: dict[str, list[str]], path) -> None:
    """問ごとの上位 k 件 {問ID: [file_name…]} を JSON に書く（`--dump-topk`）。

    検索側の変更（部品の切り出し等）の前後で同じ機械・同じデバイスで書き出し、完全一致を確かめる道具
    （審議会議事録DB_開発計画 §2.5）。Mac（MPS）と箱（CPU）ではリランカーのスコアが端数で変わり得る
    ＝機械をまたいで比べない。
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(topk, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"\n上位 k 件を書き出し: {path}（{len(topk)}問）")


def source_name(node) -> str:
    """source_node からファイル名を取り出す（file_name → file_path の basename → 不明）。"""
    metadata = node.node.metadata
    return (
        metadata.get("file_name")
        or Path(metadata.get("file_path", "")).name
        or "不明"
    )


