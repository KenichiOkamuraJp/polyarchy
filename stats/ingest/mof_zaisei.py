"""
財務省「財政統計」（https://www.mof.go.jp/policy/budget/reference/statistics/data.htm）の定型 Excel からの取込。
設計は docs/データソース選定.md §1（財務省・予算＝財政統計 Excel の定型年のみ・表番号を固定）。

3つの表型：
- year_blocks  第1表（明治初年度以降 一般会計歳入歳出予算決算・円）：元号（列A）→年（列A '22年度'／'元年度'／'23'）→ 「計」行（列B）に 歳入予算/決算・歳出予算/決算
- year_rows    第3・4表（歳入主要科目別 予算／決算・百万円）：列A 元号（先頭行のみ）・列B 年・値は列記号で指定（セルは文字列）
- year_sheets  第19表(2)・第20表（主要経費別・千円）：シート名＝年度（'Ｓ60' 'Ｈ元' 'R6' '昭和42' '令和6'）・行＝経費。section の見出し行に値があればそれ、無ければ次の「計」行
値は公表どおりの文字列（文字列セルはそのまま・数値セルは整数文字列。単位換算しない）。原本は stats/data/cache/mof/<取得日>/。

実行（リポジトリ root）：  python -m stats.ingest.mof_zaisei --all
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.periods import ERA_BASE as ERA, from_wareki_fy
from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, _col_index, _norm, cache_rel, fetch, finish, is_numeric, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.mof_zaisei")
BASE = "https://www.mof.go.jp/policy/budget/reference/statistics/"


class MofZaiseiError(SourceError):
    pass


def _year_num(tok: str) -> Optional[int]:
    t = _norm(tok).replace("年度", "").replace("年", "")
    if t == "元":
        return 1
    return int(t) if re.match(r"^\d{1,2}$", t) else None


def sheet_to_fy(name: str) -> Optional[str]:
    """シート名 'Ｓ60'／'Ｈ元'／'R6'／'昭和42'／'令和6' → 'FY1985'…（「年度」は付かない）。"""
    return from_wareki_fy(name, require_suffix=False)


def cell_str(v: object) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        t = v.strip().replace(",", "")
        return t if is_numeric(t) else None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return None


def ingest_year_blocks(s: Series, wb, cp: Path, day: str) -> list[ValueRecord]:
    a = s.accessor
    col = _col_index(a["value_col"])
    recs: list[ValueRecord] = []
    for sn in a["sheets"]:
        ws = wb[sn]
        era_base: Optional[int] = None
        fy: Optional[str] = None
        for r in range(1, ws.max_row + 1):
            a1 = _norm(ws.cell(r, 1).value)
            b1 = _norm(ws.cell(r, 2).value)
            if a1 in ERA:
                era_base = ERA[a1]
                continue
            y = _year_num(a1) if a1 else None
            if y is not None and era_base:
                fy = f"FY{era_base + y}"
                continue
            if b1 == "計" and fy:
                v = cell_str(ws.cell(r, col).value)
                if v is not None:
                    recs.append(ValueRecord(series_id=s.series_id, period=fy, region="JP", value=v, status="", vintage=day, retrieved_at=day,
                                            accessor={"type": "mof_zaisei", "file": a["file"], "sheet": sn, "row": r, "col": a["value_col"],
                                                      "row_label": "計", "cache": cache_rel(cp)}))
    return recs


def ingest_year_rows(s: Series, wb, cp: Path, day: str) -> list[ValueRecord]:
    a = s.accessor
    ws = wb[a.get("sheet") or wb.sheetnames[0]]
    col = _col_index(a["value_col"])
    era_base: Optional[int] = None
    recs: list[ValueRecord] = []
    for r in range(int(a.get("first_row", 7)), ws.max_row + 1):
        a1 = _norm(ws.cell(r, 1).value)
        if a1 in ERA:
            era_base = ERA[a1]
        y = _year_num(ws.cell(r, 2).value) if ws.cell(r, 2).value is not None else None
        if y is None or not era_base:
            continue
        v = cell_str(ws.cell(r, col).value)
        if v is None:
            continue
        recs.append(ValueRecord(series_id=s.series_id, period=f"FY{era_base + y}", region="JP", value=v, status="", vintage=day, retrieved_at=day,
                                accessor={"type": "mof_zaisei", "file": a["file"], "sheet": ws.title, "row": r, "col": a["value_col"],
                                          "cache": cache_rel(cp)}))
    return recs


def ingest_year_sheets(s: Series, wb, cp: Path, day: str) -> list[ValueRecord]:
    a = s.accessor
    section = _norm(a["section"])
    want_hdr = _norm(a["value_header"])
    recs: list[ValueRecord] = []
    for sn in wb.sheetnames:
        fy = sheet_to_fy(sn)
        if fy is None:
            continue
        ws = wb[sn]
        # 値列＝見出し（当初予算／補正予算／計／予算現額／決算額）で確定（先頭8行）
        col = None
        for r in range(1, 9):
            for c in range(1, ws.max_column + 1):
                if _norm(ws.cell(r, c).value) == want_hdr:
                    col = c
                    break
            if col:
                break
        if not col:
            raise MofZaiseiError(f"{s.series_id}: {sn}: 見出し {a['value_header']!r} が無い")
        # 見出し行を探す（列B/C のラベル・括弧付きも許容）
        hit = None
        for r in range(1, ws.max_row + 1):
            lab = _norm(ws.cell(r, 2).value) or _norm(ws.cell(r, 3).value)
            lab = lab.strip("（）()")
            if lab == section:
                hit = r
                break
        if hit is None:
            continue  # その年度の表に無い経費（例：予備費の細目）＝値なし
        v = cell_str(ws.cell(hit, col).value)
        row_used = hit
        if v is None:
            # 次の「計」行（「小計」は飛ばす・次の見出しに当たったら中止）
            for r in range(hit + 1, ws.max_row + 1):
                lab = _norm(ws.cell(r, 2).value) or _norm(ws.cell(r, 3).value)
                if lab in ("計", "合計"):
                    v = cell_str(ws.cell(r, col).value); row_used = r
                    break
        if v is None:
            continue
        recs.append(ValueRecord(series_id=s.series_id, period=fy, region="JP", value=v, status="", vintage=day, retrieved_at=day,
                                accessor={"type": "mof_zaisei", "file": a["file"], "sheet": sn, "row": row_used, "col_header": a["value_header"],
                                          "section": a["section"], "cache": cache_rel(cp)}))
    return recs


LAYOUTS = {"year_blocks": ingest_year_blocks, "year_rows": ingest_year_rows, "year_sheets": ingest_year_sheets}


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    import openpyxl
    day = day or today()
    a = s.accessor
    if a.get("type") != "mof_zaisei":
        raise ValueError(f"{s.series_id}: accessor.type={a.get('type')} は mof_zaisei の対象外")
    cp, _ = fetch(BASE + a["file"], day=day, kind="mof", skip_if_exists=True)
    wb = openpyxl.load_workbook(cp, data_only=True)
    recs = LAYOUTS[a["layout"]](s, wb, cp, day)
    if not recs:
        raise MofZaiseiError(f"{s.series_id}: 値が0件")
    periods = [x.period for x in recs]
    log.info("%s: %s → 値 %d（%s〜%s）", s.series_id, a["file"], len(recs), min(periods), max(periods))
    return finish(s, recs, dry_run=dry_run, exc=MofZaiseiError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(("mof_zaisei",), ingest_series, "財務省 財政統計 Excel 取込", argv)


if __name__ == "__main__":
    sys.exit(main())
