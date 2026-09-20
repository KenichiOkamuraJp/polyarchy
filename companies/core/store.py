"""値の置き場（登録簿＋値）。1 社 1 ファイルの JSON＝モデルも DB も要らない（常駐メモリ小）。

  data/store/companies.json          会社の登録簿（EDINET コード → 社名・証券コード・連結の有無・会計基準・収録した書類）
  data/store/facts/<EDINET コード>.json  値の列

値 1 行＝{element, label, basis, period, period_end, value, unit, decimals, context, dims, doc_id, submitted}
- value は公表どおりの文字列（換算・丸めなし）。
- basis＝consolidated／non_consolidated（連結と単体は別の系列）。
- dims＝連結・個別以外の次元（第 1 便は空。セグメント別〔第 1b 便〕はここに区分を刻む）。
- 同じ（element・basis・period・dims）が複数の書類に載る（5 期推移の再掲・遡及修正）＝全部持ち、参照は提出日が最新の書類の値。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

STORE = Path(os.getenv("COMPANIES_STORE") or Path(__file__).resolve().parent.parent / "data" / "store")


def write_company(meta: dict, facts: list[dict]) -> None:
    (STORE / "facts").mkdir(parents=True, exist_ok=True)
    fp = STORE / "facts" / f"{meta['edinet_code']}.json"
    old = json.loads(fp.read_text()) if fp.exists() else []
    docs = {f["doc_id"] for f in facts}
    merged = [f for f in old if f["doc_id"] not in docs] + facts  # 同じ書類の再取込は置き換え
    fp.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":")))
    rp = STORE / "companies.json"
    reg = json.loads(rp.read_text()) if rp.exists() else {}
    prev = reg.get(meta["edinet_code"], {})
    if meta["submitted"] >= prev.get("submitted", ""):  # 会社の属性は提出日が最新の書類のもの
        reg[meta["edinet_code"]] = {**meta, "docs": sorted(set(prev.get("docs", [])) | docs)}
    else:
        prev["docs"] = sorted(set(prev.get("docs", [])) | docs)
    rp.write_text(json.dumps(reg, ensure_ascii=False, indent=1, sort_keys=True))
    registry.cache_clear(); facts_of.cache_clear()


@lru_cache(maxsize=1)
def registry() -> dict[str, dict]:
    rp = STORE / "companies.json"
    return json.loads(rp.read_text()) if rp.exists() else {}


@lru_cache(maxsize=256)
def facts_of(edinet_code: str) -> tuple[dict, ...]:
    fp = STORE / "facts" / f"{edinet_code}.json"
    return tuple(json.loads(fp.read_text())) if fp.exists() else ()
