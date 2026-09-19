"""
日本銀行 時系列統計データ検索サイトの**フラットファイル**（zip）からの取込。設計は docs/データソース選定.md §1（日本銀行）。

- 取得元 https://www.stat-search.boj.or.jp/info/<zip>（国際収支 bp_m_en.zip・対外資産負債残高 iip_cy_en.zip・資金循環 fof.zip 等）。
- 形式は2種：
  wide＝1行1系列（code, 統計名, 系列名, 単位, 期間…）で列見出しが期間（YYYYMM／YYYY）……国際収支・対外資産負債残高
       資金循環の**系列名称入り**版（fof2_jp.zip の ff_dl_fof_fiscal-year_jp.csv＝**年度** 1979〜／ff_dl_fof_quarterly_jp.csv）も wide。
       こちらは先頭 3 列が（code, 統計名, 系列名）で 4 列目から年＝`first_col=3`・`period_kind="fy_yyyy"`・cp932。系列名は「項目／部門／ストック・フロー」。
  long＝1行1値（code, 期種, 期間, 値）……資金循環 ff_value.csv（Q=四半期 'YYYYQQ'。名称は入っていない）
- 値は**フラットファイルの文字列そのまま**（国際収支は未丸めの小数。財務省・日銀の公表表は億円整数に丸めているため見た目が異なる旨を notes に明記）。
- accessor: {"type": "boj_flat", "zip": "bp_m_en.zip", "file": "bp_m_en.csv", "layout": "wide"|"long", "code": "BPBP6JYNCB", "period_kind": "yyyymm"|"yyyy"|"yyyyqq"}
- 原本 zip は stats/data/cache/boj/<取得日>/。

実行（リポジトリ root）：  python -m stats.ingest.boj_flat --all
"""
from __future__ import annotations

import csv
import io
import re
import sys
import zipfile
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, cache_rel, fetch, finish, is_numeric, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.boj_flat")
BASE = "https://www.stat-search.boj.or.jp/info/"
_zip_cache: dict[str, Path] = {}


class BojFlatError(SourceError):
    pass


def fetch_zip(name: str, day: str) -> Path:
    if name not in _zip_cache:
        _zip_cache[name], _ = fetch(BASE + name, day=day, kind="boj", timeout=300, skip_if_exists=True)
    return _zip_cache[name]


def period_of(kind: str, code: str) -> Optional[str]:
    c = (code or "").strip()
    if kind == "yyyymm" and re.match(r"^\d{6}$", c) and 1 <= int(c[4:]) <= 12:
        return f"{c[:4]}-{c[4:]}"
    if kind == "yyyy" and re.match(r"^\d{4}$", c):
        return c
    if kind == "yyyyqq" and re.match(r"^\d{4}0[1-4]$", c):
        return f"{c[:4]}Q{int(c[4:])}"
    if kind == "fy_yyyy" and re.match(r"^\d{4}$", c):  # 資金循環（年度版）の列見出しは西暦だけ＝年度
        return f"FY{c}"
    return None


_csv_cache: dict[tuple[str, str], list[list[str]]] = {}


def read_csv(zp: Path, member: str) -> list[list[str]]:
    """zip 内 CSV を行列で返す。**1 回の実行内はメモ化**（資金循環の年度表は 2.5MB×276 系列＝毎回パースすると遅い）。"""
    key = (str(zp), member)
    if key in _csv_cache:
        return _csv_cache[key]
    with zipfile.ZipFile(zp) as z:
        raw = z.read(member)
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            _csv_cache[key] = list(csv.reader(io.StringIO(raw.decode(enc))))
            return _csv_cache[key]
        except UnicodeDecodeError:
            continue
    raise BojFlatError(f"{member}: 文字コードを判別できない")


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    a = s.accessor
    if a.get("type") != "boj_flat":
        raise ValueError(f"{s.series_id}: accessor.type={a.get('type')} は boj_flat の対象外")
    zp = fetch_zip(a["zip"], day)
    rows = read_csv(zp, a["file"])
    code, kind, layout = a["code"], a["period_kind"], a.get("layout", "wide")
    recs: list[ValueRecord] = []
    if layout == "wide":
        hdr = rows[0]
        hit = [r for r in rows if r and r[0] == code]
        if len(hit) != 1:
            raise BojFlatError(f"{s.series_id}: 系列コード {code} が {len(hit)} 行（1行に確定しない）")
        r = hit[0]
        first = int(a.get("first_col", 4))
        for i in range(first, min(len(hdr), len(r))):
            per = period_of(kind, hdr[i])
            if per is None:
                continue
            v = r[i].strip()
            if not is_numeric(v):
                continue
            recs.append(ValueRecord(series_id=s.series_id, period=per, region="JP", value=v, status="", vintage=day, retrieved_at=day,
                                    accessor={"type": "boj_flat", "zip": a["zip"], "file": a["file"], "code": code, "col": hdr[i],
                                              "series_name": r[2] if len(r) > 2 else "", "unit": r[3] if len(r) > 3 else "",
                                              "cache": cache_rel(zp)}))
    else:  # long: code, freq, period, value
        for r in rows:
            if len(r) < 4 or r[0] != code:
                continue
            per = period_of(kind, r[2])
            v = r[3].strip()
            if per is None or not is_numeric(v):
                continue
            recs.append(ValueRecord(series_id=s.series_id, period=per, region="JP", value=v, status="", vintage=day, retrieved_at=day,
                                    accessor={"type": "boj_flat", "zip": a["zip"], "file": a["file"], "code": code, "freq": r[1], "period_code": r[2],
                                              "cache": cache_rel(zp)}))
    if not recs:
        raise BojFlatError(f"{s.series_id}: 値が0件（code={code}）")
    periods = [x.period for x in recs]
    log.info("%s: %s/%s code=%s → 値 %d（%s〜%s）", s.series_id, a["zip"], a["file"], code, len(recs), min(periods), max(periods))
    return finish(s, recs, dry_run=dry_run, exc=BojFlatError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(("boj_flat",), ingest_series, "日銀 フラットファイル取込", argv)


if __name__ == "__main__":
    sys.exit(main())
