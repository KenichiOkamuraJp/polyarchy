"""
法人企業統計 業種別パネルの**合計整合チェック＝記録のみ**（第 6 弾・データ拡充計画.md §4a）。

- Σ子業種 ＝ 親業種（親子は core/hojin_vocab.py の parent）を、値ストアの収録値で年度ごとに検算する。
- 結果は登録を止めない（FY2008 以前は旧分類の細分が別コード／未集計のため不成立が正常）。
  不一致は「どの年度から整合するか」を系列ごとに記録する＝利用側（シフトシェア分解）の usable_from の根拠。
- 会計恒等式（売上高 − 売上原価 − 販管費 ＝ 営業利益）も全セルで検算する（値の取り違えの検出）。

実行（リポジトリ root）：  python -m stats.ops.hojin_consistency [--measure sales] [--json]
- 規模整合（Tier 2）：Σ4 区分＝全規模を FY1975 以降で検算（記録のみ）。恒等式は全規模＋4 区分で検算。
終了コード：0＝恒等式が全セルで成立／1＝恒等式の不成立あり（合計整合の不成立は終了コードに含めない＝記録）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from stats.core.registry import default_registry
from stats.core.values import ValueStore
from stats.core.hojin_vocab import HOJIN_INDUSTRIES

MEASURES = ("sales", "cogs", "sga", "operating_profit", "employee_wages", "employee_bonus", "officer_compensation",
            "officer_bonus", "welfare_costs", "depreciation", "employees_avg")


def _series_values(store: ValueStore, sid: str) -> dict[str, float]:
    out = {}
    for p in store.all_periods(sid):
        r = store.lookup(sid, p)
        if r is not None:
            try:
                out[p] = float(r.value)
            except ValueError:
                pass
    return out


def check(measures=MEASURES, size: str = "allsize") -> dict:
    reg = default_registry()
    store = ValueStore()
    children: dict[str, list[str]] = defaultdict(list)
    for slug, _code, _name, _fy, parent in HOJIN_INDUSTRIES:
        if parent:
            pslug = next(s for s, c, *_ in HOJIN_INDUSTRIES if c == parent)
            children[pslug].append(slug)
    report = {"sum_checks": [], "identity_failures": []}
    for m in measures:
        vals = {slug: _series_values(store, f"mof.hojin.{m}.{slug}-{size}.fy")
                for slug, *_ in HOJIN_INDUSTRIES if reg.get(f"mof.hojin.{m}.{slug}-{size}.fy")}
        for parent, kids in children.items():
            if parent not in vals:
                continue
            per_year = []
            span = {k: (min(vals[k]), max(vals[k])) for k in kids if vals.get(k)}
            for p, pv in sorted(vals[parent].items()):
                ks = []
                for k in kids:
                    v = vals.get(k, {}).get(p)
                    if v is None and k in span and span[k][0] <= p <= span[k][1]:
                        ks.append(None)  # 収録期間内なのに欠け＝不成立
                    else:
                        ks.append(v or 0.0)  # 収録期間外（旧分類の終了後・新分類の開始前）＝その年に存在しない区分
                if any(v is None for v in ks):
                    per_year.append((p, None))
                    continue
                per_year.append((p, abs(sum(ks) - pv) <= max(1.0, abs(pv) * 1e-6)))
            ok_from = next((p for p, ok in per_year if ok and all(o for _, o in per_year if _ >= p)), None)
            report["sum_checks"].append({"measure": m, "parent": parent, "children": kids,
                                         "consistent_from": ok_from, "years": len(per_year),
                                         "inconsistent_years": [p for p, ok in per_year if not ok]})
    # 規模整合：Σ4 区分（10億以上・1億〜10億・1千万〜1億・1千万未満）＝全規模（FY1975 以降＝1千万未満の表章開始後）
    SIZES = ("cap1b", "cap100m-1b", "cap10m-100m", "capu10m")
    for m in measures:
        for slug, *_ in HOJIN_INDUSTRIES:
            tot = _series_values(store, f"mof.hojin.{m}.{slug}-allsize.fy")
            parts = {z: _series_values(store, f"mof.hojin.{m}.{slug}-{z}.fy") for z in SIZES}
            if not tot or not all(parts.values()):
                continue
            bad = []
            for p, pv in tot.items():
                if p < "FY1975":
                    continue
                ks = [parts[z].get(p) for z in SIZES]
                if any(v is None for v in ks):
                    bad.append(p); continue
                if abs(sum(ks) - pv) > max(1.0, abs(pv) * 1e-6):
                    bad.append(p)
            report.setdefault("size_checks", []).append({"measure": m, "industry": slug, "years": sum(1 for p in tot if p >= "FY1975"), "inconsistent_years": bad})
    # 会計恒等式
    if all(x in measures for x in ("sales", "cogs", "sga", "operating_profit")):
        for slug, *_ in HOJIN_INDUSTRIES:
            for z in ("allsize",) + SIZES:
                v = {m: _series_values(store, f"mof.hojin.{m}.{slug}-{z}.fy") for m in ("sales", "cogs", "sga", "operating_profit")}
                for p in v["sales"]:
                    if all(p in v[m] for m in ("cogs", "sga", "operating_profit")):
                        d = v["sales"][p] - v["cogs"][p] - v["sga"][p] - v["operating_profit"][p]
                        if abs(d) > 1.0:
                            report["identity_failures"].append({"industry": slug, "size": z, "period": p, "diff": d})
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="append")
    ap.add_argument("--size", default="allsize")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = check(tuple(a.measure) if a.measure else MEASURES, a.size)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print("=== 法人企業統計 業種別 合計整合（記録のみ）===")
        by_from = defaultdict(int)
        for c in rep["sum_checks"]:
            by_from[c["consistent_from"]] += 1
            late = [p for p in c["inconsistent_years"] if p >= "FY2009"]
            if late:
                print(f"  ! {c['measure']} {c['parent']}: FY2009 以降に不一致 {late}")
        for k, n in sorted(by_from.items(), key=lambda x: str(x[0])):
            print(f"  整合開始 {k}: {n} 組（measure×親）")
        sc = rep.get("size_checks", [])
        nbad = [c for c in sc if c["inconsistent_years"]]
        print(f"  規模整合（Σ4 区分＝全規模・FY1975〜）: {len(sc)} 組中 不一致あり {len(nbad)} 組" + (f"  例 {[(c['measure'], c['industry'], c['inconsistent_years'][:3]) for c in nbad[:5]]}" if nbad else ""))
        print(f"  会計恒等式（売上高−売上原価−販管費＝営業利益）不成立: {len(rep['identity_failures'])} セル")
        for f in rep["identity_failures"][:10]:
            print(f"    {f}")
    return 1 if rep["identity_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
