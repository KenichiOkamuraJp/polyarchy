"""
財務省の公表 CSV からの取込（国債金利情報 jgbcm.csv／jgbcm_all.csv）。設計は docs/データソース選定.md §1（財務省）。

- 国債金利情報：Shift_JIS・行＝基準日（和暦 'S49.9.24'／'H10.4.1'／'R8.7.31'）・列＝年限（1年…40年）。値は公表どおりの文字列（％）、'-' は値ではない。
- 全期間 = data/jgbcm_all.csv（1974-09-24〜前年度末）＋ jgbcm.csv（当年度分）。両方を取り、同一日は当年度分を優先（改定は無い前提・差があればエラー）。
- accessor: {"type": "mof_csv", "dataset": "jgbcm", "col": "10年"}
- 原本は stats/data/cache/mof/<取得日>/。

実行（リポジトリ root）：  python -m stats.ingest.mof --all
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.periods import from_wareki_date
from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, cache_rel, fetch, finish, is_numeric, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.mof")
JGB_ALL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
JGB_CUR = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"


class MofSourceError(SourceError):
    pass


def parse_jgbcm(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    """返り値：(年限見出し, {ISO日付: {年限: 値文字列}})。'-' や空は含めない。"""
    text = path.read_bytes().decode("shift_jis", errors="strict")
    rows = list(csv.reader(io.StringIO(text)))
    hdr_i = next(i for i, r in enumerate(rows) if r and r[0].strip() == "基準日")
    hdr = [h.strip() for h in rows[hdr_i]]
    out: dict[str, dict[str, str]] = {}
    for r in rows[hdr_i + 1:]:
        if not r or not r[0].strip():
            continue
        iso = from_wareki_date(r[0])
        if iso is None:
            continue  # 注記行など
        vals = {}
        for h, v in zip(hdr[1:], r[1:]):
            v = v.strip()
            if is_numeric(v):
                vals[h] = v
        out[iso] = vals
    return hdr[1:], out


def ingest_jgb(s: Series, *, dry_run: bool, day: str) -> int:
    col = s.accessor["col"]
    p_all, _ = fetch(JGB_ALL, day=day, kind="mof")
    p_cur, _ = fetch(JGB_CUR, day=day, kind="mof")
    _, d_all = parse_jgbcm(p_all)
    _, d_cur = parse_jgbcm(p_cur)
    merged: dict[str, tuple[str, str]] = {}
    for iso, vals in d_all.items():
        if col in vals:
            merged[iso] = (vals[col], "jgbcm_all.csv")
    for iso, vals in d_cur.items():
        if col in vals:
            if iso in merged and merged[iso][0] != vals[col]:
                raise MofSourceError(f"{s.series_id}: {iso} の値が全期間ファイルと当年度ファイルで異なる（{merged[iso][0]} / {vals[col]}）")
            merged[iso] = (vals[col], "jgbcm.csv")
    recs = [ValueRecord(series_id=s.series_id, period=iso, region="JP", value=v, status="", vintage=day, retrieved_at=day,
                        accessor={"type": "mof_csv", "file": f, "col": col, "row": iso, "cache": cache_rel(p_cur if f == 'jgbcm.csv' else p_all)})
            for iso, (v, f) in merged.items()]
    if not recs:
        raise MofSourceError(f"{s.series_id}: 値が0件（列 {col!r}）")
    log.info("%s: 値 %d（%s〜%s）", s.series_id, len(recs), min(merged), max(merged))
    return finish(s, recs, dry_run=dry_run, exc=MofSourceError)


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    a = s.accessor
    if a.get("type") != "mof_csv":
        raise ValueError(f"{s.series_id}: accessor.type={a.get('type')} は mof_csv の対象外")
    if a.get("dataset") == "jgbcm":
        return ingest_jgb(s, dry_run=dry_run, day=day)
    raise ValueError(f"{s.series_id}: 未対応の dataset {a.get('dataset')!r}")


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(("mof_csv",), ingest_series, "財務省 CSV 取込", argv)


if __name__ == "__main__":
    sys.exit(main())
