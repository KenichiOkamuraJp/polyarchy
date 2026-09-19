"""
日本銀行「主要時系列統計データ表」（mtshtml）の取込（第 10 弾 第 1 便・2026-08-29）。

- 取得元＝https://www.stat-search.boj.or.jp/ssi/mtshtml/<page>（例 fm08_m_1.html＝為替月次・fm02_m_1.html＝コールレート月次・
  ir01_m_1.html＝基準割引率）。安定 URL・Shift_JIS の HTML 表＝フラットファイル（boj_flat）に無い主要系列（為替・金利）の経路。
- 表の構造（決定論で解釈）：先頭行群＝「系列名称」「データコード」「単位」「収録開始期」「収録終了期」「最終更新日」の見出し行、
  以降＝期間行（th＝YYYY/MM・td＝値）。**列はデータコードの完全一致で 1 列に確定**（0・複数はエラー＝黙って選ばない）。
- 値は表示文字列のまま（ND・空欄は欠測＝取り込まない）。ページが表示する期間範囲だけを収録する
  （例 fm08 の収録開始期は 1973/01 だが表は 1980/01〜＝first_period は表の範囲・注記に明記）。
- accessor: {"type": "boj_mtshtml", "page": "fm08_m_1.html", "code": "FM08'FXERM07"}

実行（リポジトリ root）：
    python -m stats.ingest.boj_mtshtml --all
    python -m stats.ingest.boj_mtshtml --series boj.fx.usdjpy_avg.m
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, cache_rel, fetch, finish, is_numeric, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.boj_mtshtml")
BASE = "https://www.stat-search.boj.or.jp/ssi/mtshtml/"
TYPES = ("boj_mtshtml",)
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)
_TAG = re.compile(r"<[^>]+>")


class BojMtsError(SourceError):
    pass


def parse_table(html: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """HTML → (データコード行, [(期間ラベル, 値セル…)…])（純関数）。"""
    rows = []
    for tr in _TR.findall(html):
        rows.append([_TAG.sub("", c).replace("&nbsp;", " ").strip() for c in _CELL.findall(tr)])
    codes: list[str] = []
    data: list[tuple[str, list[str]]] = []
    for r in rows:
        if not r:
            continue
        if r[0] == "データコード":
            codes = r
        elif re.match(r"^\d{4}/\d{2}$", r[0]):
            data.append((r[0], r[1:]))
    if not codes:
        raise BojMtsError("「データコード」行が見つからない（ページ構成の変更を疑う）")
    return codes, data


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    a = s.accessor
    if a.get("type") != "boj_mtshtml":
        raise ValueError(f"{s.series_id}: accessor.type={a.get('type')} は対象外")
    page, code = str(a["page"]), str(a["code"])
    cp, published = fetch(BASE + page, day=day, kind="boj")
    html = cp.read_bytes().decode("shift_jis", errors="replace")
    codes, data = parse_table(html)
    cols = [i for i, c in enumerate(codes) if c == code]
    if len(cols) != 1:
        raise BojMtsError(f"{s.series_id}: データコード {code!r} が {len(cols)} 列（1 列に確定しない）")
    col = cols[0] - 1  # データコード行の先頭セルは行見出し＝値セルは 1 つずれる
    recs: list[ValueRecord] = []
    for label, cells in data:
        v = cells[col].strip() if col < len(cells) else ""
        if not is_numeric(v):  # 'ND'・空欄は欠測
            continue
        period = f"{label[:4]}-{label[5:7]}"
        recs.append(ValueRecord(series_id=s.series_id, period=period, region="JP", value=v, status="",
                                vintage=published or day, retrieved_at=day, published_at=published,
                                accessor={"type": "boj_mtshtml", "page": page, "code": code,
                                          "row_label": label, "cache": cache_rel(cp)}))
    if not recs:
        raise BojMtsError(f"{s.series_id}: 値が0件（code={code}）")
    periods = [x.period for x in recs]
    log.info("%s: %s code=%s → 値 %d（%s〜%s）", s.series_id, page, code, len(recs), min(periods), max(periods))
    return finish(s, recs, dry_run=dry_run, exc=BojMtsError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(TYPES, ingest_series, "日銀 主要時系列統計データ表（mtshtml）取込（為替・金利）", argv)


if __name__ == "__main__":
    sys.exit(main())
