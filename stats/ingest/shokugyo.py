"""
一般職業紹介状況（職業安定業務統計）長期時系列表（e-Stat ファイル提供・xlsx）からの取込（第 11 弾 第 6 便・2026-09-15）。

- 取得元＝e-Stat のデータカタログ API（getDataCatalog）で「一般職業紹介状況_～令和N年M月」の最新データセットを見つけ、
  RESOURCE の URL（file-download?&statInfId=…&fileKind=0）から xlsx を取る。**statInfId は毎月変わる**（毎勤の固定 ID とは違う）＝
  カタログ経由が唯一の決定論の経路（一覧ページは JS 描画で statInfId が取れない・DB（API）には未収録＝第 5 弾の調査記録）。
  取得は 1 日 1 回（同日キャッシュ）。
- 表＝第 1 表「労働市場関係指標（パートタイムを含む常用）」1 シート：列＝西暦・和暦・期・実数 9 指標（新規求人倍率・有効求人倍率・
  就職率(対新規)・充足率(対新規)・新規求人数・新規求職申込件数・有効求人数・有効求職者数・就職件数）＋季節調整値 4 指標（倍率・率のみ）。
  行＝暦年（「1963年」〜）→ 年度（「1963年度」〜）→ 四半期（西暦＋「1-3月」…）→ 月（西暦＋「１月」…）の順。**行の種類は 1 列目と 3 列目の表記で決める**。
  `***`＝該当なし（季節調整値は 2002 年以降のみ 等）＝値を作らない。
- 値は xlsx のセル値の文字列表現そのまま（丸めない。xlsx の数値セルは Python の float で読めるので `1.2` のように末尾 0 を落とさず
  **セルの表示書式ではなく値**を採る＝原典 xlsx の値どおり）。
- accessor: {"type": "estat_catalog_xlsx", "catalog_word": "一般職業紹介状況 長期時系列表", "title_prefix": "一般職業紹介状況_～",
             "table_no": 1, "sheet": "第１表", "col": "有効求人倍率", "adjusted": false}   period は freq（m／q／a／fy）と行の種類で決まる。
- 原本は stats/data/cache/mhlw/<取得日>/shokugyo_t<table_no>.xlsx。

実行（リポジトリ root）：  python -m stats.ingest.shokugyo --all
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, cache_path, cache_rel, fetch, finish, http_get, is_numeric, run_cli, today
from stats.ingest.estat import load_app_id

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.shokugyo")
CATALOG_API = "https://api.e-stat.go.jp/rest/3.0/app/json/getDataCatalog"
TYPES = ("estat_catalog_xlsx",)
_MONTH = {"１月": 1, "２月": 2, "３月": 3, "４月": 4, "５月": 5, "６月": 6, "７月": 7, "８月": 8, "９月": 9, "10月": 10, "11月": 11, "12月": 12}
_QUARTER = {"1-3月": 1, "4-6月": 2, "7-9月": 3, "10-12月": 4}
_catalog_cache: dict[tuple[str, str], dict] = {}
_book_cache: dict[tuple[str, str, int], tuple[Path, list[tuple]]] = {}


class ShokugyoError(SourceError):
    pass


def discover(catalog_word: str, title_prefix: str, table_no: int, app_id: Optional[str] = None) -> dict:
    """カタログ API で最新データセット（title_prefix で始まり「旧様式」を含まない・SURVEY_DATE 最大）を見つけ、
    表番号 table_no の RESOURCE を返す：{"dataset_id","title","survey_date","release_date","statInfId","url"}。"""
    key = (catalog_word, title_prefix)
    if key not in _catalog_cache:
        q = urllib.parse.urlencode({"appId": app_id or load_app_id(), "searchWord": catalog_word, "limit": 100})
        body, _ = http_get(f"{CATALOG_API}?{q}")
        d = json.loads(body.decode("utf-8"))
        items = d.get("GET_DATA_CATALOG", {}).get("DATA_CATALOG_LIST_INF", {}).get("DATA_CATALOG_INF", [])
        items = items if isinstance(items, list) else [items]
        cands = []
        for it in items:
            ds = it.get("DATASET", {})
            name = str(ds.get("TITLE", {}).get("NAME", ""))
            if not name.startswith(title_prefix) or "旧様式" in name:
                continue
            cands.append((int(ds.get("SURVEY_DATE") or 0), name, it))
        if not cands:
            raise ShokugyoError(f"カタログに {title_prefix!r} のデータセットが無い（searchWord={catalog_word!r}）")
        cands.sort(key=lambda x: x[0])
        _catalog_cache[key] = cands[-1][2]
    it = _catalog_cache[key]
    ds = it["DATASET"]
    res = it.get("RESOURCES", {}).get("RESOURCE", [])
    res = res if isinstance(res, list) else [res]
    hit = [r for r in res if int(r.get("TITLE", {}).get("TABLE_NO") or -1) == table_no]
    if len(hit) != 1:
        raise ShokugyoError(f"{ds.get('TITLE', {}).get('NAME')}: 表 {table_no} の RESOURCE が {len(hit)} 件（1 件に確定しない）")
    r = hit[0]
    url = str(r.get("URL", ""))
    m = re.search(r"statInfId=(\d+)", url)
    return {"dataset_id": it.get("@id"), "title": ds.get("TITLE", {}).get("NAME", ""), "survey_date": str(ds.get("SURVEY_DATE", "")),
            "release_date": str(r.get("RELEASE_DATE") or ds.get("RELEASE_DATE") or ""), "statInfId": m.group(1) if m else "", "url": url,
            "table_title": r.get("TITLE", {}).get("NAME", "")}


def _cell(v) -> str:
    """セル値→文字列（数値は末尾 0 を落とさず・整数値は整数表記）。None／`***`／空は ''。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return ""
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    s = str(v).strip()
    return "" if s in ("***", "-", "…", "") else s


def load_book(a: dict, day: str, app_id: Optional[str] = None) -> tuple[Path, list[tuple], dict]:
    """xlsx を取得（同日キャッシュ）して (キャッシュ先, 行の列, カタログ情報) を返す。"""
    table_no = int(a.get("table_no", 1))
    key = (str(a.get("catalog_word")), str(a.get("title_prefix")), table_no)
    info = discover(key[0], key[1], table_no, app_id)
    if key not in _book_cache:
        cp, _ = fetch(info["url"], day=day, kind="mhlw", timeout=300, skip_if_exists=True, name=f"shokugyo_t{table_no}.xlsx")
        import openpyxl
        wb = openpyxl.load_workbook(cp, read_only=True, data_only=True)
        sheet = str(a.get("sheet", ""))
        if sheet not in wb.sheetnames:
            raise ShokugyoError(f"シート {sheet!r} が無い（{wb.sheetnames}）")
        rows = [tuple(r) for r in wb[sheet].iter_rows(values_only=True)]
        _book_cache[key] = (cp, rows)
    cp, rows = _book_cache[key]
    return cp, rows, info


def find_column(rows: list[tuple], col: str, adjusted: bool) -> tuple[int, int]:
    """(見出し行の index, 列 index)。見出し行＝「西暦」で始まる行。実数／季節調整値の区別はその 1 行上の帯で決める。"""
    for hi, r in enumerate(rows[:10]):
        if _cell(r[0]) == "西暦":
            band = rows[hi - 1]
            want = "季節調整値" if adjusted else "実数"
            hits = [ci for ci, v in enumerate(r) if _cell(v) == col and ci < len(band) and _cell(band[ci]) == want]
            if len(hits) != 1:
                raise ShokugyoError(f"列 {col!r}（{want}）が {len(hits)} 列（1 列に確定しない）")
            return hi, hits[0]
    raise ShokugyoError("見出し行（西暦）が見つからない")


def period_of(row: tuple, freq: str) -> Optional[str]:
    """行の種類を 1 列目（西暦）と 3 列目（期）で判定して period を返す。系列の freq と合わない行は None。"""
    y = _cell(row[0]); sub = _cell(row[2]) if len(row) > 2 else ""
    m_a = re.fullmatch(r"(\d{4})年", y)
    m_fy = re.fullmatch(r"(\d{4})年度", y)
    if m_fy:
        return f"FY{m_fy.group(1)}" if freq == "fy" and not sub else None
    if not m_a:
        return None
    yy = m_a.group(1)
    if not sub:
        return yy if freq == "a" else None
    if sub in _QUARTER:
        return f"{yy}Q{_QUARTER[sub]}" if freq == "q" else None
    if sub in _MONTH:
        return f"{yy}-{_MONTH[sub]:02d}" if freq == "m" else None
    return None


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    a = s.accessor
    if a.get("type") not in TYPES:
        raise ValueError(f"{s.series_id}: accessor.type={a.get('type')} は対象外")
    cp, rows, info = load_book(a, day)
    hi, ci = find_column(rows, str(a["col"]), bool(a.get("adjusted")))
    recs: list[ValueRecord] = []
    for ri in range(hi + 1, len(rows)):
        r = rows[ri]
        p = period_of(r, s.freq)
        if p is None:
            continue
        v = _cell(r[ci]) if ci < len(r) else ""
        if not v:
            continue
        if not is_numeric(v):
            raise ShokugyoError(f"{s.series_id}: {p} の値「{v}」が数値でない")
        recs.append(ValueRecord(series_id=s.series_id, period=p, region="JP", value=v, status="", vintage=info["release_date"] or day,
                                retrieved_at=day, published_at=info["release_date"],
                                accessor={"type": "estat_catalog_xlsx", "statInfId": info["statInfId"], "table_no": int(a.get("table_no", 1)),
                                          "sheet": str(a.get("sheet", "")), "col": str(a["col"]), "adjusted": bool(a.get("adjusted")),
                                          "row": ri + 1, "cache": cache_rel(cp)}))
    if not recs:
        raise ShokugyoError(f"{s.series_id}: 値が0件（col={a.get('col')} adjusted={a.get('adjusted')} freq={s.freq}）")
    periods = [x.period for x in recs]
    log.info("%s: %s（%s）→ 値 %d（%s〜%s）", s.series_id, info["title"], info["statInfId"], len(recs), min(periods), max(periods))
    return finish(s, recs, dry_run=dry_run, exc=ShokugyoError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(TYPES, ingest_series, "一般職業紹介状況 長期時系列表（e-Stat カタログ→xlsx）取込", argv)


if __name__ == "__main__":
    sys.exit(main())
