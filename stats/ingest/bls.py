"""
米 BLS（労働統計局）Public Data API v1 からの取込（第 10 弾 第 2 便・2026-08-29）。

- 取得元＝https://api.bls.gov/publicAPI/v1/timeseries/data/（POST・JSON・**キー不要**）。
  v1 の制約＝1 リクエスト 10 年まで・日次 25 リクエストまで（キー不要の枠）＝**10 年窓で 1970 年から刻んで全期間を取る**
  （3 系列 × 6 窓 ＝ 18 リクエスト／回＝枠内。--all の再取込は日をまたがない限り 1 日 1 回まで）。
- 値は API の文字列のまま（CPI は小数 3 桁・失業率は 1 桁＝公表どおり）。period "M01".."M12"＝月次（"M13"＝年平均は取らない）。
- region＝"USA" 固定（region_level=cty＝国際系列の作法）。ライセンス＝米連邦政府の著作物（パブリックドメイン）。
- accessor: {"type": "bls_api", "series": "CUUR0000SA0", "start": 1970}

実行（リポジトリ root）：
    python -m stats.ingest.bls --all
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import USER_AGENT, SourceError, cache_path, finish, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.bls")
API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
TYPES = ("bls_api",)
WINDOW = 10  # v1 の 1 リクエスト上限（年数）


class BlsError(SourceError):
    pass


def fetch_window(series: str, y0: int, y1: int, day: str) -> list[dict]:
    """1 窓（最大 10 年）の取得。原本 JSON はキャッシュ（同日ならリクエストを消費しない）。"""
    cp = cache_path("bls", day, f"{series}_{y0}_{y1}.json")
    if not cp.exists():
        req = urllib.request.Request(API, method="POST",
                                     data=json.dumps({"seriesid": [series], "startyear": str(y0), "endyear": str(y1)}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
        d = json.loads(body)
        if d.get("status") != "REQUEST_SUCCEEDED":
            raise BlsError(f"{series} {y0}-{y1}: {d.get('status')} {d.get('message')}")
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(body)
    d = json.loads(cp.read_text(encoding="utf-8"))
    ser = d.get("Results", {}).get("series", [])
    return ser[0].get("data", []) if ser else []


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    a = s.accessor
    if a.get("type") != "bls_api":
        raise ValueError(f"{s.series_id}: accessor.type={a.get('type')} は対象外")
    code, start = str(a["series"]), int(a.get("start", 1970))
    recs: list[ValueRecord] = []
    thisyear = int(day[:4])
    for y0 in range(start, thisyear + 1, WINDOW):
        y1 = min(y0 + WINDOW - 1, thisyear)
        for x in fetch_window(code, y0, y1, day):
            p = x.get("period", "")
            if not (p.startswith("M") and p != "M13"):
                continue  # M13＝年平均は取らない（月次系列）
            v = str(x.get("value", "")).strip()
            if not v or v == "-":
                continue
            recs.append(ValueRecord(series_id=s.series_id, period=f"{x['year']}-{p[1:]}", region="USA", value=v,
                                    status="", vintage=day, retrieved_at=day,
                                    accessor={"type": "bls_api", "series": code, "year": x["year"], "period": p,
                                              "cache": f"cache/bls/{day}/{code}_{y0}_{y1}.json"}))
    if not recs:
        raise BlsError(f"{s.series_id}: 値が0件（series={code}）")
    periods = [x.period for x in recs]
    log.info("%s: %s → 値 %d（%s〜%s）", s.series_id, code, len(recs), min(periods), max(periods))
    return finish(s, recs, dry_run=dry_run, exc=BlsError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(TYPES, ingest_series, "米 BLS Public API v1 取込（CPI・失業率＝10 年窓・キー不要）", argv)


if __name__ == "__main__":
    sys.exit(main())
