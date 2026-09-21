"""母集団の棚卸し（取込のあとに回す）＝語彙・契約が 20 社の外でも成り立つかを数える。値は変えない。

  python -m companies.ops.population_report            # 標準出力に Markdown
見るもの：会計基準と連結の有無／最上段の収益をどのキーで開示しているか／同じ決算期に同じキーの要素が並ぶ会社／
語彙に無い標準要素（足す候補）／書類間で値が変わった点（遡及修正）／決算期の間隔が 12 か月でない会社／社名の衝突。
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict

from companies.core import store
from companies.core.items import ELEMENT_TO_KEY
from companies.core.lookup import _norm

TOP = ("net_sales", "revenue", "operating_revenue", "operating_receipts", "gross_operating_revenue", "ordinary_revenue", "net_premiums_written")


def main() -> int:
    reg = store.registry()
    std, cons, top, amb, unknown, restated, odd = Counter(), Counter(), Counter(), [], Counter(), [], []
    unknown_label = {}
    for code, co in reg.items():
        facts = store.facts_of(code)
        std[co.get("accounting_standard")] += 1
        cons["連結あり" if co["consolidated"] else "連結なし"] += 1
        basis = "consolidated" if co["consolidated"] else "non_consolidated"
        latest = max(f["period"] for f in facts) if facts else None
        cur = [f for f in facts if f["basis"] == basis and f["period"] == latest]
        keys = {ELEMENT_TO_KEY.get(f["element"].split(":")[1]) for f in cur if f["element"].startswith("jpcrp_cor:")}
        ext = any(not f["element"].startswith("jpcrp_cor:") for f in cur)
        kind = next((k for k in TOP if k in keys), "拡張要素のみ" if ext else "標準の収益項目なし")
        top[kind] += 1
        by = defaultdict(set)
        for f in facts:
            local = f["element"].split(":")[1]
            if f["element"].startswith("jpcrp_cor:"):
                k = ELEMENT_TO_KEY.get(local)
                if k:
                    by[(k, f["basis"], f["period"], f["doc_id"])].add(local)
                else:
                    unknown[local] += 0  # 初期化
        for f in {(f["element"]) for f in facts if f["element"].startswith("jpcrp_cor:") and f["element"].split(":")[1] not in ELEMENT_TO_KEY}:
            unknown[f.split(":")[1]] += 1
        a = sorted({(k, p) for (k, b, p, d), els in by.items() if len(els) > 1})
        if a:
            amb.append((co["name"], a[:3], len(a)))
        pts = defaultdict(dict)
        for f in facts:
            pts[(f["element"], f["basis"], f["period"])][f["doc_id"]] = f["value"]
        ch = [(k, v) for k, v in pts.items() if len(set(v.values())) > 1]
        if ch:
            restated.append((co["name"], len(ch), ch[0]))
        ends = sorted({f["period_end"] for f in facts if f["context"].endswith("Instant") or "Instant_" in f["context"]})
        gaps = {(int(b[:4]) * 12 + int(b[5:7])) - (int(a_[:4]) * 12 + int(a_[5:7])) for a_, b in zip(ends, ends[1:])}
        if gaps - {12}:
            odd.append((co["name"], ends))
    names = Counter(_norm(c["name"]) for c in reg.values())
    n = len(reg)
    out = [f"# 母集団の棚卸し（{n} 社）", "", f"- 会計基準：{dict(std)}", f"- 連結：{dict(cons)}", "",
           "## 最上段の収益をどのキーで開示しているか（主たる系列・最新の決算期）", ""]
    out += [f"- {k}: {c}（{c / n:.1%}）" for k, c in top.most_common()]
    out += ["", f"## 同じ決算期に同じキーの要素が並ぶ会社（ambiguous_item になる）：{len(amb)} 社", ""]
    out += [f"- {name}: {ex}（計 {cnt} 点）" for name, ex, cnt in amb[:15]]
    out += ["", "## 語彙に無い標準要素（社数の多い順＝足す候補。足すときは公式ラベルを確かめ、問を先に立てる）", ""]
    out += [f"- {el}: {c}" for el, c in unknown.most_common(40) if c]
    out += ["", f"## 書類間で値が変わった点がある会社（遡及修正・会計方針の変更 等）：{len(restated)} 社", ""]
    out += [f"- {name}: {cnt} 点（例 {ex[0][0].split(':')[1][:40]} {ex[0][2]} {ex[1]}）" for name, cnt, ex in restated[:15]]
    out += ["", f"## 決算期の間隔が 12 か月でない会社（決算期の変更 等）：{len(odd)} 社", ""]
    out += [f"- {name}: {ends}" for name, ends in odd[:15]]
    out += ["", f"## 社名の衝突（正規化後に同名）：{sum(1 for c in names.values() if c > 1)} 組", ""]
    out += [f"- {k}: {c}" for k, c in names.most_common(10) if c > 1]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
