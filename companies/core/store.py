"""値の置き場（登録簿＋値）。1 社 1 ファイルの JSON＝モデルも DB も要らない（常駐メモリ小）。

  data/store/companies.json          会社の登録簿（EDINET コード → 社名・証券コード・連結の有無・会計基準・収録した書類）
  data/store/facts/<EDINET コード>.json  値の列

値 1 行＝{element, label, basis, period, period_end, value, unit, decimals, context, dims, doc_id, submitted}
- value は公表どおりの文字列（換算・丸めなし）。
- basis＝consolidated／non_consolidated（連結と単体は別の系列）。
- dims＝連結・個別以外の次元（第 1 便は空。セグメント別〔第 1b 便〕はここに区分を刻む）。
- 同じ（element・basis・period・dims）が複数の書類に載る（5 期推移の再掲・遡及修正）＝全部持ち、参照は提出日が最新の書類の値。

  data/store/segments/<EDINET コード>.json  セグメント別の値（第 1b 便）＝{"docs": {書類: 書類の属性}, "facts": [値の列]}
    書類の属性＝{submitted, accounting_standard, periods: {current, prior}, notes: {basis: {present, quote}}}
    値 1 行＝{member, member_label, element, element_label, section, basis, period, period_end, value, unit, decimals, context, doc_id, submitted}

  data/store/regions/<EDINET コード>.json  地域別の欄（第 1b 便②）＝{"docs": {書類: {submitted, accounting_standard, periods}}, "sections": [欄の列]}
    欄 1 行＝{element, section, context, basis, period, period_end, source_tables, content, doc_id, submitted}
    content＝段落と表を順に（{"type": "text"|"omitted"|"table", ...}）。表はセル単位で公表どおり・結合セルは展開しない。
    欄の無い書類も docs には載る（欄が無い＝not_tagged と、収録外の期を分けるため）
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
    documents = {**prev.get("documents", {}), **{d: {"submitted": meta["submitted"], "accounting_standard": meta.get("accounting_standard"),
                                                     "fiscal_year_end": meta.get("fiscal_year_end")} for d in docs}}
    names = sorted({n for n in [*prev.get("names", []), meta.get("name"), prev.get("name")] if n})  # 社名変更＝旧社名でも引ける
    if meta["submitted"] >= prev.get("submitted", ""):  # 会社の属性は提出日が最新の書類のもの
        reg[meta["edinet_code"]] = {**meta, "names": names, "documents": documents, "docs": sorted(documents)}
    else:
        prev.update(names=names, documents=documents, docs=sorted(documents))
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


def write_segments(edinet_code: str, doc_id: str, doc: dict, facts: list[dict]) -> None:
    """セグメント別の値（第 1b 便）。同じ書類の再取込は置き換え。"""
    (STORE / "segments").mkdir(parents=True, exist_ok=True)
    fp = STORE / "segments" / f"{edinet_code}.json"
    old = json.loads(fp.read_text()) if fp.exists() else {"docs": {}, "facts": []}
    data = {"docs": {**old["docs"], doc_id: doc}, "facts": [f for f in old["facts"] if f["doc_id"] != doc_id] + facts}
    fp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    segments_of.cache_clear()


@lru_cache(maxsize=256)
def segments_of(edinet_code: str) -> dict:
    fp = STORE / "segments" / f"{edinet_code}.json"
    return json.loads(fp.read_text()) if fp.exists() else {"docs": {}, "facts": []}


def segment_codes() -> list[str]:
    return sorted(p.stem for p in (STORE / "segments").glob("*.json"))


def write_regions(edinet_code: str, doc_id: str, doc: dict, sections: list[dict]) -> None:
    """地域別の欄（第 1b 便②）。同じ書類の再取込は置き換え。"""
    (STORE / "regions").mkdir(parents=True, exist_ok=True)
    fp = STORE / "regions" / f"{edinet_code}.json"
    old = json.loads(fp.read_text()) if fp.exists() else {"docs": {}, "sections": []}
    data = {"docs": {**old["docs"], doc_id: doc}, "sections": [x for x in old["sections"] if x["doc_id"] != doc_id] + sections}
    fp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    regions_of.cache_clear()


@lru_cache(maxsize=256)
def regions_of(edinet_code: str) -> dict:
    fp = STORE / "regions" / f"{edinet_code}.json"
    return json.loads(fp.read_text()) if fp.exists() else {"docs": {}, "sections": []}


def region_codes() -> list[str]:
    return sorted(p.stem for p in (STORE / "regions").glob("*.json"))
