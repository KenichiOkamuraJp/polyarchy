"""
Eurostat 配信 API（statistics/1.0・JSON-stat 2.0）からの取込（第 10 弾 第 2 便・2026-08-29）。

- 取得元＝https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/<dataset>?format=JSON&lang=en&…（キー不要）。
- 国は ISO2（DE/FR/IT/ES）で問い合わせ、**ISO3（DEU/FRA/ITA/ESP）に写して region に刻む**（国際系列の作法＝ISO3 のみ。
  EA20/EU27 等の集計値は ISO3 でないため収録しない＝WEO と同じ判断）。
- JSON-stat の展開＝id/size の次元順で線形 index を復元（geo×time 以外の次元はクエリで 1 値に固定する）。
- 値は JSON の数値を str() で文字列化（Eurostat は丸め済みの表示値を返す＝小数 1 桁等）。
- **実測（2026-08-29）**：prc_hicp_manr（HICP 前年比）は 2025-12 で止まっている・une_rt_m（失業率）は 2026-06/07 まで現行＝
  提供の鮮度がデータセットで違う（notes に明記・埋めない）。
- accessor: {"type": "eurostat_api", "dataset": "prc_hicp_manr", "params": {"coicop": "CP00", "unit": "RCH_A"}, "geo": ["DE","FR","IT","ES"]}

実行（リポジトリ root）：
    python -m stats.ingest.eurostat --all
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import USER_AGENT, SourceError, cache_path, finish, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.eurostat")
API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
TYPES = ("eurostat_api",)
ISO2TO3 = {"DE": "DEU", "FR": "FRA", "IT": "ITA", "ES": "ESP", "NL": "NLD", "BE": "BEL", "AT": "AUT",
           "PT": "PRT", "IE": "IRL", "FI": "FIN", "EL": "GRC", "SE": "SWE", "DK": "DNK", "PL": "POL"}


class EurostatError(SourceError):
    pass


def unpack(d: dict) -> list[tuple[str, str, float]]:
    """JSON-stat 2.0 → [(geo(ISO2), period, value)…]（純関数・geo/time 以外の次元は 1 値前提＝多値ならエラー）。"""
    ids, sizes = d["id"], d["size"]
    for dim, n in zip(ids, sizes):
        if dim not in ("geo", "time") and n != 1:
            raise EurostatError(f"次元 {dim} が {n} 値（クエリで 1 値に固定する）")
    geo_idx = {v: k for k, v in d["dimension"]["geo"]["category"]["index"].items()}
    time_idx = {v: k for k, v in d["dimension"]["time"]["category"]["index"].items()}
    # 線形 index の復元：後ろの次元ほど早く回る（row-major）
    strides: dict[str, int] = {}
    acc = 1
    for dim, n in reversed(list(zip(ids, sizes))):
        strides[dim] = acc
        acc *= n
    out = []
    for k, v in d.get("value", {}).items():
        i = int(k)
        g = geo_idx[(i // strides["geo"]) % len(geo_idx)]
        t = time_idx[(i // strides["time"]) % len(time_idx)]
        out.append((g, t, v))
    return out


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    a = s.accessor
    if a.get("type") != "eurostat_api":
        raise ValueError(f"{s.series_id}: accessor.type={a.get('type')} は対象外")
    ds = str(a["dataset"])
    q = [("format", "JSON"), ("lang", "en")] + sorted((k, str(v)) for k, v in (a.get("params") or {}).items())
    q += [("geo", g) for g in a.get("geo", [])]
    url = API + ds + "?" + urllib.parse.urlencode(q)
    cp = cache_path("eurostat", day, f"{ds}_{s.measure}.json")
    if not cp.exists():
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=180) as r:
            cp.write_bytes(r.read())
    d = json.loads(cp.read_text(encoding="utf-8"))
    if "error" in d and not d.get("value"):
        raise EurostatError(f"{s.series_id}: API エラー {str(d['error'])[:200]}")
    recs: list[ValueRecord] = []
    for g2, t, v in unpack(d):
        iso3 = ISO2TO3.get(g2)
        if iso3 is None or v is None:
            continue
        if not (len(t) == 7 and t[4] == "-"):  # 月次（YYYY-MM）以外は対象外
            continue
        recs.append(ValueRecord(series_id=s.series_id, period=t, region=iso3, value=str(v), status="",
                                vintage=day, retrieved_at=day,
                                accessor={"type": "eurostat_api", "dataset": ds, "geo": g2, "time": t,
                                          "url": url, "cache": f"cache/eurostat/{day}/{ds}_{s.measure}.json"}))
    if not recs:
        raise EurostatError(f"{s.series_id}: 値が0件（dataset={ds}）")
    periods = [x.period for x in recs]
    log.info("%s: %s → 値 %d（%s〜%s・%d か国）", s.series_id, ds, len(recs), min(periods), max(periods),
             len({x.region for x in recs}))
    return finish(s, recs, dry_run=dry_run, exc=EurostatError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(TYPES, ingest_series, "Eurostat 配信 API 取込（HICP・失業率＝JSON-stat・キー不要）", argv)


if __name__ == "__main__":
    sys.exit(main())
