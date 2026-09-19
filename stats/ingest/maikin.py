"""
毎月勤労統計調査 全国調査 長期時系列表「実数・指数累積データ」（e-Stat ファイル提供・CSV）からの取込（第 11 弾 第 1 便・2026-09-15）。

- 取得元＝e-Stat の file-download エンドポイント（statInfId 固定・毎月更新＝第 10 弾 第 2 便で特定。2026-08-29 時点は 503 継続で保留、
  2026-09-15 に 200 を確認して取込）：
    000032189776＝「1_実数・指数累積データ 実数」（cp932 CSV・20MB・1970 年〜）
    000032189777＝「2_実数・指数累積データ 指数・伸び率」（cp932 CSV・16MB・1952 年〜。指数は 2020 年＝100）
  行の型（long）＝種別, 年, 月, 産業分類, 規模, 就業形態, <項目列…>。月列＝"01".."12" か "CY"（暦年＝年平均／前年比）。
  規模コード＝T（事業所規模 5 人以上）・0（30 人以上）・4/5/7/9（500 人以上／100〜499／30〜99／5〜29＝未収録）。
  就業形態＝0（就業形態計）・1（一般労働者）・2（パートタイム労働者）。産業分類 TL＝調査産業計（他は JSIC 中分類＝未収録）。
  規模 T（5 人以上）は 1990 年〜・0（30 人以上）は 1970 年〜・就業形態別は 1993 年〜（コード表の意味は 2024〜2025 年の公表値
  〈2025 年 実質賃金 5 人以上 −1.3％・30 人以上 −1.0％〉との一致で確認）。
- 値は CSV の文字列そのまま（丸め・換算なし）。空欄＝欠測（値を作らない）。
- accessor: {"type": "maikin_csv", "statInfId": "000032189777", "kind": "指数"|"伸び率"|"実数", "col": "<列見出し>",
             "industry": "TL", "size": "T"|"0", "emp": "0"|"1"|"2"}   period は freq（m＝月列／a＝CY 列）で決まる。
- 原本は stats/data/cache/mhlw/<取得日>/maikin_<statInfId>.csv。

実行（リポジトリ root）：  python -m stats.ingest.maikin --all
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, cache_rel, fetch, finish, is_numeric, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.maikin")
BASE = "https://www.e-stat.go.jp/stat-search/file-download?statInfId={sid}&fileKind=1"
TYPES = ("maikin_csv",)
FILE_ACTUAL, FILE_INDEX = "000032189776", "000032189777"
_KIND_PREFIX = {"実数": "実数", "指数": "指数", "伸び率": "伸び率"}  # 伸び率の種別ラベルは「伸び率、日数差、ポイント差」
_tables: dict[tuple[str, str], tuple[Path, list[str], list[list[str]]]] = {}


class MaikinError(SourceError):
    pass


def url_of(sid: str) -> str:
    return BASE.format(sid=sid)


def load_table(sid: str, day: str) -> tuple[Path, list[str], list[list[str]]]:
    """CSV を取得（同日キャッシュがあれば再取得しない）して (キャッシュ先, 見出し, 行) を返す。cp932。"""
    key = (sid, day)
    if key not in _tables:
        cp, _ = fetch(url_of(sid), day=day, kind="mhlw", timeout=300, skip_if_exists=True, name=f"maikin_{sid}.csv")
        rows = list(csv.reader(cp.read_text(encoding="cp932").splitlines()))
        if not rows or rows[0][:6] != ["種別", "年", "月", "産業分類", "規模", "就業形態"]:
            raise MaikinError(f"{sid}: 先頭 6 列が想定と違う（{rows[0][:6] if rows else '空'}）＝レイアウト変更")
        _tables[key] = (cp, rows[0], rows[1:])
    return _tables[key]


def period_of(year: str, month: str, freq: str) -> Optional[str]:
    if freq == "a":
        return year if month == "CY" else None
    if freq == "m":
        return f"{year}-{month}" if month.isdigit() and 1 <= int(month) <= 12 else None
    raise MaikinError(f"freq={freq} は対象外（m／a）")


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    a = s.accessor
    if a.get("type") != "maikin_csv":
        raise ValueError(f"{s.series_id}: accessor.type={a.get('type')} は対象外")
    sid, kind, col = str(a["statInfId"]), str(a["kind"]), str(a["col"])
    ind, size, emp = str(a.get("industry", "TL")), str(a["size"]), str(a["emp"])
    cp, header, rows = load_table(sid, day)
    if col not in header:
        raise MaikinError(f"{s.series_id}: 列「{col}」が {sid} に無い（列＝{header[6:]}）")
    ci = header.index(col)
    prefix = _KIND_PREFIX[kind]
    recs: list[ValueRecord] = []
    for r in rows:
        if not r[0].startswith(prefix) or r[3].strip() != ind or r[4].strip() != size or r[5].strip() != emp:
            continue
        p = period_of(r[1].strip(), r[2].strip(), s.freq)
        if p is None:
            continue
        v = r[ci].strip() if ci < len(r) else ""
        if not v:
            continue
        if not is_numeric(v):
            raise MaikinError(f"{s.series_id}: {p} の値「{v}」が数値でない")
        recs.append(ValueRecord(series_id=s.series_id, period=p, region="JP", value=v, status="", vintage=day,
                                retrieved_at=day,
                                accessor={"type": "maikin_csv", "statInfId": sid, "kind": kind, "col": col,
                                          "industry": ind, "size": size, "emp": emp, "year": r[1].strip(),
                                          "month": r[2].strip(), "cache": cache_rel(cp)}))
    if not recs:
        raise MaikinError(f"{s.series_id}: 値が0件（{sid} kind={kind} col={col} size={size} emp={emp}）")
    periods = [x.period for x in recs]
    log.info("%s: 値 %d（%s〜%s）", s.series_id, len(recs), min(periods), max(periods))
    return finish(s, recs, dry_run=dry_run, exc=MaikinError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(TYPES, ingest_series, "毎月勤労統計 長期時系列表 実数・指数累積データ（e-Stat CSV）取込", argv)


if __name__ == "__main__":
    sys.exit(main())
