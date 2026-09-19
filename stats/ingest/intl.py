"""
国際比較の取込（IMF DataMapper／OECD SDMX／世界銀行 API）。設計は docs/データソース選定.md §1・データ拡充計画.md 第3弾。

- 値は各 API の**原文の数値文字列**をそのまま保持（IMF/世銀 JSON は parse_float=str、OECD CSV は OBS_VALUE 列そのまま）。
- 地域は ISO3（region_level=cty）。系列の region_codes に含まれる国だけ保存。
- IMF WEO：版名（例 "World Economic Outlook (April 2026)"）を indicators から取り vintage/edition に刻む。
  **版年以降の年は値単位で kind=projection**（DataMapper は国別の「推計開始年」を出さないため、版年−1 年以前は観測値扱い。
  IMF スタッフ推計が混じり得る旨は series.notes に明記）。
- 原本は stats/data/cache/<imf|oecd|wb>/<取得日>/ に保存。

実行（リポジトリ root）：
    python -m stats.ingest.intl --all            # status=registered かつ accessor.type ∈ {imf_dm, oecd_sdmx, wb_api}
    python -m stats.ingest.intl --series <id>
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, cache_path, cache_rel, finish, http_get, is_numeric, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.intl")


def _fetch(url: str, timeout: int = 300) -> bytes:
    """GET（UA curl 相当・指数バックオフ再試行は _base.http_get）。"""
    body, _ = http_get(url, timeout=timeout)
    return body


def _cache(kind: str, day: str, name: str, data: bytes) -> Path:
    p = cache_path(kind, day, name)
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------- IMF DataMapper
IMF_BASE = "https://www.imf.org/external/datamapper/api/v1/"
_imf_meta: Optional[dict] = None


def imf_indicator_meta(indicator: str) -> dict:
    global _imf_meta
    if _imf_meta is None:
        _imf_meta = json.loads(_fetch(IMF_BASE + "indicators").decode("utf-8"))["indicators"]
    return _imf_meta.get(indicator) or {}


def _edition_year(source: str) -> Optional[int]:
    m = re.search(r"\((?:April|October|July|January)\s+(\d{4})\)", source or "")
    return int(m.group(1)) if m else None


def ingest_imf(s: Series, day: str) -> list[ValueRecord]:
    ind = s.accessor["indicator"]
    raw = _fetch(IMF_BASE + ind)
    cp = _cache("imf", day, f"{ind}.json", raw)
    d = json.loads(raw.decode("utf-8"), parse_float=str, parse_int=str)
    meta = imf_indicator_meta(ind)
    source = meta.get("source", "")
    ed_year = _edition_year(source)
    values = d.get("values", {}).get(ind, {})
    recs: list[ValueRecord] = []
    for iso3 in s.region_codes:
        for year, val in (values.get(iso3) or {}).items():
            v = str(val)
            if not is_numeric(v):
                continue
            kind = "projection" if (ed_year and int(year) >= ed_year) else ""
            recs.append(ValueRecord(series_id=s.series_id, period=year, region=iso3, value=v, status="",
                                    vintage=source, retrieved_at=day, published_at="",
                                    accessor={"type": "imf_dm", "indicator": ind, "iso3": iso3, "year": year,
                                              "edition": source, "cache": cache_rel(cp)}, kind=kind))
    return recs


# ---------------------------------------------------------------- OECD SDMX
# ★ レート制限（2026-08-22 実測・OECD 公式 FAQ）：**1 時間あたり 60 ダウンロード**。API も Data Explorer の CSV も同じ枠。
#   解除手段は無い（API キー・上位プラン・申請窓口いずれも存在しない）＝全員一律。超過は 429。
#   したがって「全件を毎回取り直す」設計は取れない。系列を分割して時間をまたぐか、差分同期（後述）にする。
#   429 は _base.http_get が 30 秒以上待って再試行し、それでも駄目なら SourceError で記録して続行する（他系列は止めない）。
# ★ 箱（EC2）から叩くときの注意：OECD は VPN・匿名化経路からのトラフィックを許可しておらず、
#   データセンター IP がそれに該当し得る。`stats.ops.refresh` を箱で回す前に、箱から 1 本だけ叩いて 429/403 を確認すること
#   （Mac では通るのに箱で全滅、という壊れ方をする）。
# ★ 本筋の対応＝差分同期：availability constraints を問い合わせ、前回同期以降に更新された dataflow だけ取りに行く。
#   OECD のデータセットは高頻度指標を除き年 1〜2 回しか改定されない（SDBS は年 1 回）＝制限に恒常的に触れなくなる。未実装。
OECD_BASE = "https://sdmx.oecd.org/public/rest/data/"
_OECD_MEMO: dict = {}  # 1 回の CLI 実行内の URL→(キャッシュ先, 行) メモ
# OBS_STATUS（SDMX 共通コード）：A＝通常値は空にする（全セルに付くと cell_flags が雑音になる）。それ以外は意味を付けて残す＝利用側が値の性格を判別できる
OBS_STATUS_LABEL = {"B": "B=系列断層", "D": "D=定義が異なる", "E": "E=推計値（OECD 等による推計）", "F": "F=予測値", "G": "G=試験的な値",
                    "I": "I=受領機関による補完値", "K": "K=他区分に含む", "W": "W=他区分を含む", "P": "P=暫定値", "U": "U=信頼性が低い", "V": "V=未検証"}


def _obs_status(code: str) -> str:
    code = (code or "").strip()
    if code in ("", "A"):
        return ""
    return OBS_STATUS_LABEL.get(code, code)


def ingest_oecd(s: Series, day: str) -> list[ValueRecord]:
    a = s.accessor
    df = a["dataflow"]  # 例 "OECD.SDD.TPS,DSD_PDB@DF_PDB,"
    # key＝SDMX のキー（次元を "." 区切り・空は全件・"+" で複数）。SDBS のように全件が 1 国 17MB の表は key で絞る（第 7 弾 B-3・2026-08-22）
    url = f"{OECD_BASE}{df}/{a.get('key', 'all')}?startPeriod={a.get('startPeriod', '1970')}&format=csvfilewithlabels"
    # 同じ URL（＝同じ key）を複数系列が共有するときは 1 回だけ取得（1 時間 60 本の制限対策。STAN は measure ごとに 1 本の key を 36 活動で共有）
    if url in _OECD_MEMO:
        cp, rows = _OECD_MEMO[url]
    else:
        import urllib.error
        try:
            raw = _fetch(url, timeout=300)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # SDMX の NoResultsFound＝取得元にその組合せが無い（「ない」＝登録を見直す側）
                raise SourceError(f"{s.series_id}: OECD に該当データなし（404 NoResultsFound）: {a.get('key', 'all')}") from e
            if e.code == 429:  # レート制限が再試行後も続く＝この系列は記録して続行（後で再実行）
                raise SourceError(f"{s.series_id}: OECD レート制限（429）＝時間を置いて再実行") from e
            raise
        h = hashlib.sha1(url.encode()).hexdigest()[:8]
        cp = _cache("oecd", day, f"{df.replace(',', '_').replace('@', '-')}_{h}.csv", raw)
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
        _OECD_MEMO[url] = (cp, rows)
    if not rows:
        raise SourceError(f"{s.series_id}: OECD 応答が空")
    filt: dict = a.get("filter", {})
    recs: list[ValueRecord] = []
    for r in rows:
        if r.get("REF_AREA") not in s.region_codes:
            continue
        if any(r.get(k) != v for k, v in filt.items()):
            continue
        val = r.get("OBS_VALUE", "")
        if not is_numeric(val or ""):
            continue
        period = r.get("TIME_PERIOD", "")
        recs.append(ValueRecord(series_id=s.series_id, period=period, region=r["REF_AREA"], value=val,
                                status=_obs_status(r.get("OBS_STATUS", "")), vintage=day, retrieved_at=day,
                                accessor={"type": "oecd_sdmx", "dataflow": df, "REF_AREA": r["REF_AREA"],
                                          **{k: r.get(k) for k in filt}, "TIME_PERIOD": period,
                                          **({"EST_METHOD": r["EST_METHOD"]} if r.get("EST_METHOD") else {}),  # STAN：OECD 推計（SUT/SBS 等）か原典か
                                          "cache": cache_rel(cp)}))  # 同一 (period, REF_AREA) の別値＝filter 不足は finish の assert_unique がエラーにする
    return recs


# ---------------------------------------------------------------- World Bank
WB_BASE = "https://api.worldbank.org/v2/country/{iso3}/indicator/{ind}?format=json&per_page=200"


def ingest_wb(s: Series, day: str) -> list[ValueRecord]:
    ind = s.accessor["indicator"]
    recs: list[ValueRecord] = []
    for iso3 in s.region_codes:
        raw = _fetch(WB_BASE.format(iso3=iso3, ind=ind))
        cp = _cache("wb", day, f"{ind}_{iso3}.json", raw)
        d = json.loads(raw.decode("utf-8"), parse_float=str, parse_int=str)
        if not isinstance(d, list) or len(d) < 2 or not d[1]:
            continue
        for row in d[1]:
            v = row.get("value")
            if v is None or not is_numeric(str(v)):
                continue
            recs.append(ValueRecord(series_id=s.series_id, period=str(row["date"]), region=iso3, value=str(v),
                                    status="", vintage=str(d[0].get("lastupdated", day)), retrieved_at=day,
                                    accessor={"type": "wb_api", "indicator": ind, "iso3": iso3, "date": str(row["date"]),
                                              "cache": cache_rel(cp)}))
    return recs


HANDLERS = {"imf_dm": ingest_imf, "oecd_sdmx": ingest_oecd, "wb_api": ingest_wb}


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    fn = HANDLERS.get(s.accessor.get("type", ""))
    if fn is None:
        raise ValueError(f"{s.series_id}: accessor.type={s.accessor.get('type')} は intl 取込の対象外")
    recs = fn(s, day)
    if not recs:
        raise SourceError(f"{s.series_id}: 値が 0 件（取得元・フィルタを確認）")
    log.info("%s: 値 %d（地域 %d）", s.series_id, len(recs), len({r.region for r in recs}))
    return finish(s, recs, dry_run=dry_run)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(tuple(HANDLERS), ingest_series, "国際比較の取込（IMF/OECD/世銀）", argv)


if __name__ == "__main__":
    sys.exit(main())
