"""
e-Stat API v3 からの取込（原本キャッシュ→値抽出→値ストア）。設計は docs/データソース選定.md §1・参照粒度設計.md。

- appId は env `ESTAT_APP_ID`（無ければ `stats/.env` を読む。所在の正典＝stats/docs/共通契約.md「秘密」）。**ログ・出力・キャッシュ名に appId を出さない**。
- 原本（getStatsData の JSON 応答）は `stats/data/cache/estat/<取得日>/<statsDataId>_<パラメータ hash>.json` に保存（完全一致テストの根拠）。
- 値は e-Stat の `$` をそのまま文字列で保持。`-`・`…`・`x` 等の非数値は値にしない（found=false 側＝記録しない）。
- 期間は series.period_converter で決定論変換。変換できない時間コードはエラー（黙って落とさない）。

実行（リポジトリ root）：
    python -m stats.ingest.estat --series mof.hojin.sales.allexfin-allsize.fy
    python -m stats.ingest.estat --all            # status=registered かつ accessor.type=estat の全系列
    python -m stats.ingest.estat --all --dry-run  # 取得だけして書かない
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.periods import convert, matches_freq
from stats.core.paths import ENV_PATH
from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, cache_path, cache_rel, finish, http_get, is_numeric, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.estat")

BASE = "https://api.e-stat.go.jp/rest/3.0/app/json/"
# 値の判定は _base.is_numeric（符号・小数・指数表記のみ。「-」「…」「x」「--」等の非数値記号は found=false 側＝値にしない。
# 旧実装 `^[\d\-\.]+$` は "-" や "..." を数値として通していた（2026-08-19 修正・test_core で固定））。


class EstatConfigError(RuntimeError):
    """appId 未設定など、取得元の設定不備（「ない」とは区別する）。"""


def load_app_id() -> str:
    v = os.environ.get("ESTAT_APP_ID", "").strip()
    if v:
        return v
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("ESTAT_APP_ID="):
                v = line.split("=", 1)[1].strip()
                if v:
                    return v
    raise EstatConfigError("ESTAT_APP_ID が未設定（env または stats/.env）。取得元未設定＝found=false ではなくエラー。")


def _get(endpoint: str, params: dict, app_id: str, timeout: int = 120) -> dict:
    q = dict(params)
    q["appId"] = app_id
    url = BASE + endpoint + "?" + urllib.parse.urlencode(q)
    body, _ = http_get(url, timeout=timeout)
    return json.loads(body.decode("utf-8"))


def _cache_path(stats_data_id: str, params: dict, day: str) -> Path:
    h = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:10]
    return cache_path("estat", day, f"{stats_data_id}_{h}.json")


def fetch_stats_data(accessor: dict, app_id: str, day: str) -> tuple[dict, list[dict], Path]:
    """getStatsData を全ページ取得。返り値：(TABLE_INF, VALUE 行の一覧, キャッシュ先)。"""
    params = {k: v for k, v in accessor.items() if k not in ("type",)}
    params.setdefault("metaGetFlg", "N")
    params.setdefault("cntGetFlg", "N")
    params.setdefault("limit", 100000)
    values: list[dict] = []
    table_inf: dict = {}
    pages: list[dict] = []
    start = 1
    while True:
        p = dict(params)
        p["startPosition"] = start
        d = _get("getStatsData", p, app_id)
        pages.append(d)
        gs = d.get("GET_STATS_DATA", {})
        res = gs.get("RESULT", {})
        if res.get("STATUS") != 0:
            raise SourceError(f"e-Stat エラー: {res.get('ERROR_MSG')}（statsDataId={accessor.get('statsDataId')}）")
        sd = gs.get("STATISTICAL_DATA", {})
        table_inf = sd.get("TABLE_INF", table_inf)
        vals = sd.get("DATA_INF", {}).get("VALUE", [])
        if isinstance(vals, dict):
            vals = [vals]
        values.extend(vals)
        ri = sd.get("RESULT_INF", {})
        nxt = ri.get("NEXT_KEY")
        if not nxt:
            break
        start = int(nxt)
    cp = _cache_path(str(accessor.get("statsDataId")), params, day)
    cp.write_text(json.dumps({"params": params, "pages": pages}, ensure_ascii=False), encoding="utf-8")
    return table_inf, values, cp


def fetch_time_names(stats_data_id: str, app_id: str) -> dict[str, str]:
    """getMetaInfo から時間軸コード→表示名（'2021年10月1日現在' 等）の対応を取る（コードが不規則な表用）。"""
    d = _get("getMetaInfo", {"statsDataId": stats_data_id}, app_id)
    objs = d["GET_META_INFO"]["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"]
    for c in objs:
        if c.get("@id") == "time":
            cls = c["CLASS"] if isinstance(c["CLASS"], list) else [c["CLASS"]]
            return {x["@code"]: x["@name"] for x in cls}
    return {}


def _period_from_time_name(name: str) -> Optional[str]:
    """時間軸の表示名から年を採る：'2021年10月1日現在'／'1990年' → '2021'／'1990'。月次名（'2024年3月'）は対象外（None）。"""
    n = (name or "").strip()
    m = re.match(r"^(\d{4})年度$", n)
    if m:
        return f"FY{m.group(1)}"
    m = re.match(r"^(\d{4})年(?:10月1日現在)?$", n)
    return m.group(1) if m else None


def _region_from_area(area_code: str, region_level: str = "") -> Optional[str]:
    """e-Stat 地域コード → 正規地域：'00000'→JP、'13000'→'13'（都道府県 JIS 2桁）。
    region_level="city" の系列では市区町村の JIS 5 桁（'13100' 東京都区部・'27100' 大阪市 等）をそのまま地域コードにする
    （第 11 弾 第 3 便・CPI 都道府県庁所在市）。'000xx'（都市階級・地方の集計区分）は JIS コードでないので None。
    それ以外（市区町村等）は None。"""
    a = (area_code or "").strip()
    if a == "00000":
        return "JP"
    if re.match(r"^\d{2}000$", a) and 1 <= int(a[:2]) <= 47:
        return a[:2]
    if region_level == "city" and re.match(r"^\d{5}$", a) and 1 <= int(a[:2]) <= 47:
        return a
    return None


def _published_at(table_inf: dict) -> str:
    v = table_inf.get("UPDATED_DATE") or table_inf.get("OPEN_DATE") or ""
    return str(v)


def ingest_series(s: Series, app_id: str, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    acc = s.accessor
    if acc.get("type") != "estat":
        raise ValueError(f"{s.series_id}: accessor.type={acc.get('type')} は estat 取込の対象外")
    parts = acc.get("parts") or [acc]  # 複数表を1系列に束ねる場合（人口推計＝国勢調査基準ごとの表）
    if not s.period_converter and not any(pt.get("time_from_name") for pt in parts):
        raise ValueError(f"{s.series_id}: period_converter が未設定")
    recs: list[ValueRecord] = []
    bad_time: list[str] = []
    skipped = 0
    skipped_freq = 0
    published = ""
    for pt in parts:
        pacc = {k: v for k, v in pt.items() if k not in ("time_from_name", "parts")}
        pacc.setdefault("type", "estat")
        table_inf, rows, cp = fetch_stats_data(pacc, app_id, day)
        pub = _published_at(table_inf)
        published = pub or published
        names = fetch_time_names(str(pacc.get("statsDataId")), app_id) if pt.get("time_from_name") else {}
        for v in rows:
            raw = str(v.get("$", "")).strip()
            if not raw or not is_numeric(raw):
                skipped += 1  # 「-」「…」等＝値ではない（found=false 側）
                continue
            tcode = str(v.get("@time", ""))
            period = _period_from_time_name(names.get(tcode, "")) if names else convert(s.period_converter, tcode)
            if period is None:
                bad_time.append(tcode)
                continue
            if s.freq and not matches_freq(period, s.freq):
                skipped_freq += 1  # 同一表に別粒度（年・年度・月）が混在する場合＝この系列の粒度だけ採る
                continue
            region = _region_from_area(str(v.get("@area", pacc.get("cdArea", "00000"))), s.region_level) if "cdArea" not in pacc or s.region_level else "JP"
            if region is None or region not in s.region_codes:
                continue
            recs.append(ValueRecord(series_id=s.series_id, period=period, region=region, value=raw,
                                    status="", vintage=pub or day, retrieved_at=day,
                                    accessor={**{k: pacc[k] for k in pacc}, "cdTime": tcode,
                                              **({"cdArea": v.get("@area")} if v.get("@area") else {}),
                                              "cache": cache_rel(cp)},
                                    published_at=pub))
    if bad_time:
        uniq = sorted(set(bad_time))
        raise SourceError(f"{s.series_id}: 変換できない時間コード {len(uniq)} 種（例 {uniq[:5]}）。period_converter={s.period_converter}")
    log.info("%s: 表 %d → 値 %d（非数値 %d・他粒度 %d）vintage=%s", s.series_id, len(parts), len(recs), skipped, skipped_freq, published)
    # 0 件・重複（同一 period/region に別値＝セル指定が甘い証拠）は finish がエラーにする。黙って上書きしない。
    return finish(s, recs, dry_run=dry_run)


def main(argv: Optional[list[str]] = None) -> int:
    app_id = functools.lru_cache(maxsize=None)(load_app_id)  # 対象の確定後・最初の系列で読む（未設定ならそこでエラー）
    return run_cli(("estat",), lambda s, **kw: ingest_series(s, app_id(), **kw), "e-Stat 取込（原本キャッシュ→値ストア）", argv)


if __name__ == "__main__":
    sys.exit(main())
