"""業種（第 1c 便の横断検索の絞り込み）＝EDINET コードリストの「提出者業種」を事実として持つ＝docs/第1c便_計画.md §2-1。

  data/store/industries.json  {"source": {...}, "companies": {EDINET コード: {"industry", "listing"}}}（作り方＝ops/build_industries.py）
業種は**登録の業種**（事業の実態とずれる会社がある）＝寄せずに返す。語彙はコードリストの表記のまま（東証 33 業種と同じ並び・
表記の違いは「倉庫・運輸関連」〔東証は「倉庫・運輸関連業」〕の 1 語）。製造業＝東証の業種区分の製造業 16 業種。
"""
from __future__ import annotations

import json
from functools import lru_cache

from companies.core import store

INDUSTRIES = ("水産・農林業", "鉱業", "建設業", "食料品", "繊維製品", "パルプ・紙", "化学", "医薬品", "石油・石炭製品", "ゴム製品",
              "ガラス・土石製品", "鉄鋼", "非鉄金属", "金属製品", "機械", "電気機器", "輸送用機器", "精密機器", "その他製品",
              "電気・ガス業", "陸運業", "海運業", "空運業", "倉庫・運輸関連", "情報・通信業", "卸売業", "小売業", "銀行業",
              "証券、商品先物取引業", "保険業", "その他金融業", "不動産業", "サービス業")
MANUFACTURING = frozenset(INDUSTRIES[3:19])  # 食料品〜その他製品
MANUFACTURING_NOTE = "製造業＝東証の業種区分の製造業 16 業種（食料品〜その他製品）。業種は EDINET コードリストの提出者業種（登録の業種＝事業の実態とずれる会社がある）"


@lru_cache(maxsize=1)
def _data() -> dict:
    fp = store.STORE / "industries.json"
    return json.loads(fp.read_text()) if fp.exists() else {"source": None, "companies": {}}


def source() -> dict | None:
    return _data()["source"]


def industry_of(edinet_code: str) -> dict | None:
    r = _data()["companies"].get(edinet_code)
    return None if r is None else {**r, "manufacturing": r["industry"] in MANUFACTURING}


def unmatched_companies() -> list[str]:
    """登録簿の会社のうち、業種が無い・語彙の外の会社。0 社であること。"""
    comp = _data()["companies"]
    return [c for c in store.registry() if comp.get(c, {}).get("industry") not in INDUSTRIES]
