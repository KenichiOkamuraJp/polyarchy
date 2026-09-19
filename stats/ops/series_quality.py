"""
系列ごとの「ばらつき指標」＝生成物 `stats/data/registry/quality.json`（git 追跡・改善 3・2026-08-22）。

- 対象＝値を持つ全系列。指標＝対数差分の頑健な散らばり（MAD×1.4826＝正規なら σ 相当）と観測数。
  **データから計算した系列自身の性質**であり原典の主張ではない（breaks＝原典注記とは別物として quality.sample に載せる）。
- 利用側の判断材料：業種×規模の小さいセルは標本誤差で年々の振れが大きい（要件源＝情報通信業×中小の営業赤字が標本誤差か実勢か判断できなかった）。
- 閾値（flag の根拠）：
  volatile＝**同じ測度の上位集計（規模別セル→同業種の全規模／業種の全規模→親業種の全規模）に対する散らばりの比 ≥ 2 かつ dispersion ≥ 0.15**
  （景気変動は上位集計にも同じ程度に出るので、比を取ると標本誤差の分だけが残る。単独の閾値だと製造業全規模の営業利益まで flag が立つ）／
  small_cell＝母集団法人数（mof.hojin.population_count）の最新値 < 100 社。どちらも「怪しい」ではなく「単年の値より数年平均で見よ」の合図。

実行（リポジトリ root・取込後に）：  python -m stats.ops.series_quality
"""
from __future__ import annotations

import json
import math
import sys

from stats.core.paths import REGISTRY_DIR
from stats.core.registry import default_registry
from stats.core.values import ValueStore

QUALITY_PATH = REGISTRY_DIR / "quality.json"
DISPERSION_FLAG = 0.15
RELATIVE_FLAG = 2.0
SMALL_POPULATION = 100


def _log_diff_dispersion(xs: list[float]) -> float | None:
    ys = [math.log(x) for x in xs if x > 0]
    if len(ys) < 8:
        return None
    d = [ys[i] - ys[i - 1] for i in range(1, len(ys))]
    med = sorted(d)[len(d) // 2]
    mad = sorted(abs(x - med) for x in d)[len(d) // 2]
    return round(1.4826 * mad, 4)


def _parent_id(s) -> str | None:
    """散らばりの比較対象＝上位集計の系列 ID（法人企業統計のみ）：規模別セル→同業種の全規模、業種の全規模→親業種の全規模。"""
    if s.dataset != "hojin" or s.freq != "fy":
        return None
    from stats.core.hojin_vocab import BY_SLUG, BY_CODE
    from stats.core.registry import Registry
    ind, size = Registry.split_dims(s.dims)
    if size and size != "allsize":
        return f"mof.hojin.{s.measure}.{ind}-allsize.fy"
    parent = BY_SLUG.get(ind, (None, None, None, None))[3]
    if parent and parent in BY_CODE:
        return f"mof.hojin.{s.measure}.{BY_CODE[parent][0]}-allsize.fy"
    return None


def _values(store: ValueStore, sid: str, region: str) -> list[float]:
    vals = []
    for p in store.periods(sid, region):
        r = store.lookup(sid, p, region)
        try:
            vals.append(float(r.value))
        except (TypeError, ValueError, AttributeError):
            pass
    return vals


def build() -> dict:
    reg = default_registry()
    store = ValueStore()
    out: dict[str, dict] = {}
    disp_cache: dict[str, float | None] = {}

    def disp_of(sid: str, region: str = "JP") -> float | None:
        if sid not in disp_cache:
            disp_cache[sid] = _log_diff_dispersion(_values(store, sid, region)) if store.has_data(sid) else None
        return disp_cache[sid]

    for s in reg.series.values():
        if s.is_derived or not store.has_data(s.series_id):
            continue
        region = s.region_codes[0] if s.region_codes else "JP"
        vals = _values(store, s.series_id, region)
        q: dict = {"n_obs": len(vals)}
        disp = disp_of(s.series_id, region)
        if disp is not None:
            q["dispersion"] = disp
        pid = _parent_id(s)
        pdisp = disp_of(pid) if pid and pid != s.series_id else None
        if disp is not None and pdisp:
            q["dispersion_ratio"] = round(disp / pdisp, 2)
            q["dispersion_vs"] = pid
        # 法人企業統計：同じセルの母集団法人数（最新値）
        if s.dataset == "hojin" and s.measure != "population_count" and s.freq == "fy":
            pid = f"mof.hojin.population_count.{s.dims}.fy"
            pps = store.periods(pid, "JP") if store.has_data(pid) else []
            if pps:
                pr = store.lookup(pid, pps[-1], "JP")
                try:
                    q["population_latest"] = int(float(pr.value)); q["population_period"] = pps[-1]
                except (TypeError, ValueError):
                    pass
        flags = []
        if q.get("dispersion", 0) >= DISPERSION_FLAG and q.get("dispersion_ratio", 0) >= RELATIVE_FLAG:
            flags.append("volatile")
        if q.get("population_latest") is not None and q["population_latest"] < SMALL_POPULATION:
            flags.append("small_cell")
        if flags:
            q["flags"] = flags
        out[s.series_id] = q
    return out


def main() -> int:
    q = build()
    QUALITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUALITY_PATH.write_text(json.dumps(q, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    n_v = sum(1 for x in q.values() if "volatile" in x.get("flags", []))
    n_s = sum(1 for x in q.values() if "small_cell" in x.get("flags", []))
    print(f"quality.json: {len(q)} 系列（volatile {n_v}・small_cell {n_s}）→ {QUALITY_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
