"""第 1c 便（横断検索）の評価問の素材づくり（実装より先に立てる＝docs/第1c便_計画.md §3）。

  python -m companies.eval.make_screen_candidates
出力：companies/data/eval/screen.jsonl（値の正例）。**上書きする**＝人手の問は screen_fail_closed.jsonl・screen_queries.jsonl に置く（本ファイルは触らない）。

期待値の取り方＝式エンジン（core/screen.py）とは別の経路：
- 項目の値＝単社の参照層 `lookup_company_facts`（書類を固定＝各社の最新の書類）で 1 つずつ引き、式を本ファイルで Decimal で計算する。
- 海外売上比率＝本文の inline XBRL（`make_region_candidates.ix_blocks`＝取込とは別のファイル・別のパーサ）の表から、**形の単純な表だけ**
  （日本基準＝見出し 1 行・結合なし・値の行 1 つ／IFRS＝行が地域・本邦の行と合計の行が 1 つずつ）で本邦と合計のセルを取る。
  合計のセル×単位が、同じ書類のタグの付いた最上段の収益と一致したときだけ採る（合計は 2 経路・本邦のセルは同じ出所＝過信しない）。
  形の複雑な表（「うち」・結合・比率の行）は screen_queries.jsonl に人手で置く。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from decimal import Decimal

from companies.core import store
from companies.core.items import TOP_LINE
from companies.core.lookup import lookup_company_facts
from companies.eval.make_candidates import EVAL, TODAY
from companies.eval.make_region_candidates import ix_blocks

AS_OF = "2026-09-27"  # 問を固定する基準日（古い決算期の除外＝18 か月の判定に使う）
N_VALUE, N_OVERSEAS = 40, 40
HOME = re.compile(r"^(日本|本邦|国内|日本国内)([（(].*[)）])?$")
TOTAL = re.compile(r"^(合計|計|総計|連結|連結合計)([（(].*[)）])?$")
NUM = re.compile(r"^[\d,]+$")
UNITS = (Decimal(10**6), Decimal(10**3), Decimal(1))


def _latest(code: str) -> tuple[str, str, str]:
    c = store.registry()[code]
    doc = max(c["documents"], key=lambda d: (c["documents"][d]["submitted"], d))
    basis = "consolidated" if c["consolidated"] else "non_consolidated"
    return doc, basis, c["documents"][doc]["accounting_standard"]


def _periods(code: str, doc: str, basis: str) -> list[str]:
    return sorted({f["period"] for f in store.facts_of(code) if f["doc_id"] == doc and f["basis"] == basis and not f["dims"]}, reverse=True)


def _get(code: str, item: str, period: str, doc: str, basis: str, standard: str) -> dict | None:
    r = lookup_company_facts(code, item=item, period=period, doc_id=doc, basis=basis)
    if not r.get("found") or r["source"]["doc_id"] != doc or r.get("accounting_standard") != standard:
        return None
    return {"item": item, "element": r["element"], "period": period, "value": r["value"]}


def _top(code, period, doc, basis, standard):
    for k in TOP_LINE:
        g = _get(code, k, period, doc, basis, standard)
        if g:
            return g
    return None


def _months(a: str, b: str) -> int:
    return (int(a[:4]) - int(b[:4])) * 12 + int(a[5:]) - int(b[5:])


def value_questions() -> list[dict]:
    reg = store.registry()
    order = sorted(reg, key=lambda k: hashlib.sha1(f"screen-{k}".encode()).hexdigest())
    order = [k for k in order if reg[k]["accounting_standard"] != "Japan GAAP"][:12] + [k for k in order if reg[k]["accounting_standard"] == "Japan GAAP"]
    out = []
    for code in order:
        if len(out) >= N_VALUE * 4:
            break
        doc, basis, std = _latest(code)
        ps = _periods(code, doc, basis)
        if len(ps) < 3 or any(_months(ps[i], ps[i + 1]) != 12 for i in range(2)) or _months(AS_OF[:7], ps[0]) > 18:
            continue  # 変則決算・古い決算期は負例の側で扱う
        t, t1 = ps[0], ps[1]
        bottom_item = "profit_attributable_to_owners" if basis == "consolidated" else "net_income"
        top, top1 = _top(code, t, doc, basis, std), _top(code, t1, doc, basis, std)
        bot = _get(code, bottom_item, t, doc, basis, std)
        ta, ta1, ta2 = (_get(code, "total_assets", p, doc, basis, std) for p in ps[:3])
        name = reg[code]["name"]
        base = {"company": {"edinet_code": code, "name": name}, "as_of": AS_OF, "basis": basis, "doc_id": doc, "period": t,
                "checked_by": "単社の参照層 lookup_company_facts（書類を固定）で入力を引き、式は本ファイルで計算＝式エンジンとは別の経路",
                "checked_at": TODAY}

        def q(metric, inputs, value):
            out.append({"id": f"{code}-{metric}-{t}", "kind": "value", "metric": metric, **base,
                        "expected": {"inputs": inputs, "value": str(value)}})

        if top and bot and Decimal(top["value"]) > 0:
            q("net_margin", {"bottom_line[t]": bot, "top_line[t]": top}, Decimal(bot["value"]) / Decimal(top["value"]))
        if ta and ta1 and bot and Decimal(ta["value"]) + Decimal(ta1["value"]) > 0:
            q("roa", {"bottom_line[t]": bot, "total_assets[t]": ta, "total_assets[t-1]": ta1},
              Decimal(bot["value"]) / ((Decimal(ta["value"]) + Decimal(ta1["value"])) / 2))
        if top and top1 and top["item"] == top1["item"] and Decimal(top1["value"]) > 0:
            q("top_line_growth", {"top_line[t]": top, "top_line[t-1]": top1}, Decimal(top["value"]) / Decimal(top1["value"]) - 1)
        if std == "Japan GAAP" and top and Decimal(top["value"]) > 0:
            op = _get(code, "ordinary_profit", t, doc, basis, std)
            if op:
                q("ordinary_margin", {"ordinary_profit[t]": op, "top_line[t]": top}, Decimal(op["value"]) / Decimal(top["value"]))
        bot1 = _get(code, bottom_item, t1, doc, basis, std)
        if bot and bot1 and ta and ta1 and ta2 and min(Decimal(ta["value"]) + Decimal(ta1["value"]), Decimal(ta1["value"]) + Decimal(ta2["value"])) > 0:
            q("roa_change", {"bottom_line[t]": bot, "bottom_line[t-1]": bot1, "total_assets[t]": ta, "total_assets[t-1]": ta1,
                             "total_assets[t-2]": ta2},
              Decimal(bot["value"]) / ((Decimal(ta["value"]) + Decimal(ta1["value"])) / 2)
              - Decimal(bot1["value"]) / ((Decimal(ta1["value"]) + Decimal(ta2["value"])) / 2))
    return out


def _num(s: str) -> Decimal | None:
    s = s.replace("，", ",").strip()
    return Decimal(s.replace(",", "")) if NUM.fullmatch(s) else None


def _pair_jgaap(t: dict):
    rows = t["rows"]
    for i, r in enumerate(rows[:-1]):
        texts = [re.sub(r"\s", "", c["text"]) for c in r]
        if any(c.get("rowspan") or c.get("colspan") for c in r):
            continue
        h = [j for j, x in enumerate(texts) if HOME.match(x)]
        tt = [j for j, x in enumerate(texts) if TOTAL.match(x)]
        if len(h) == 1 and len(tt) == 1:
            vals = [x for x in rows[i + 1:] if sum(1 for c in x if _num(c["text"]) is not None) >= 2]
            if len(vals) == 1 and len(vals[0]) == len(r) and not any(c.get("rowspan") or c.get("colspan") for c in vals[0]):
                return vals[0][h[0]]["text"], vals[0][tt[0]]["text"]
    return None


def _pairs_ifrs(t: dict):
    rows = t["rows"]
    lab = [re.sub(r"\s", "", r[0]["text"]) if r else "" for r in rows]
    h = [i for i, x in enumerate(lab) if HOME.match(x)]
    tt = [i for i, x in enumerate(lab) if TOTAL.match(x)]
    if len(h) != 1 or len(tt) != 1 or any(c.get("rowspan") or c.get("colspan") for r in rows for c in r):
        return []
    hr, tr = rows[h[0]], rows[tt[0]]
    return [(hr[j]["text"], tr[j]["text"]) for j in range(1, min(len(hr), len(tr)))
            if _num(hr[j]["text"]) is not None and _num(tr[j]["text"]) is not None]


def overseas_questions() -> list[dict]:
    reg = store.registry()
    order = sorted(reg, key=lambda k: hashlib.sha1(f"overseas-{k}".encode()).hexdigest())
    out, n_ifrs = [], 0
    for code in order:
        if len(out) >= N_OVERSEAS:
            break
        doc, basis, std = _latest(code)
        if std == "US GAAP":
            continue
        ps = _periods(code, doc, basis)
        if not ps:
            continue
        top = _top(code, ps[0], doc, basis, std)
        if not top:
            continue
        tagged = Decimal(top["value"])
        try:
            blocks = ix_blocks(doc)
        except (FileNotFoundError, KeyError):
            continue
        want = "geographic_areas_ifrs" if std == "IFRS" else "revenue"
        blocks = [b for b in blocks if b["section"] == want and b["context"].startswith("CurrentYearDuration")
                  and ("NonConsolidated" in b["context"]) == (basis == "non_consolidated")]
        if len(blocks) != 1:
            continue
        cands = []
        for t in (x for x in blocks[0]["content"] if x["type"] == "table"):
            pairs = [_pair_jgaap(t)] if std != "IFRS" else _pairs_ifrs(t)
            for p in pairs:
                if not p:
                    continue
                hv, tv = _num(p[0]), _num(p[1])
                if hv is None or tv is None or tv == 0:
                    continue
                if any(abs(tv * u - tagged) < u for u in UNITS):
                    cands.append(p)
        if len(set(cands)) != 1:
            continue
        if std == "IFRS":
            if n_ifrs >= N_OVERSEAS // 4:
                continue
            n_ifrs += 1
        elif len(out) - n_ifrs >= N_OVERSEAS - N_OVERSEAS // 4:
            continue
        home, total = cands[0]
        hv, tv = _num(home), _num(total)
        out.append({"id": f"{code}-overseas_sales_ratio-{ps[0]}", "kind": "overseas", "metric": "overseas_sales_ratio",
                    "company": {"edinet_code": code, "name": reg[code]["name"]}, "as_of": AS_OF, "basis": basis, "doc_id": doc,
                    "period": ps[0],
                    "expected": {"home_text": home, "total_text": total, "value": str((tv - hv) / tv), "top_line": top},
                    "checked_by": "本文 inline XBRL の表（取込とは別のパーサ）で本邦と合計のセル＋合計×単位＝同じ書類のタグの付いた最上段の収益（2 経路）",
                    "checked_at": TODAY})
    return out


def main() -> int:
    qs = value_questions() + overseas_questions()
    (EVAL / "screen.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in qs))
    kinds = {}
    for q in qs:
        kinds[q["metric"]] = kinds.get(q["metric"], 0) + 1
    print(f"screen.jsonl: {len(qs)} 問 {kinds}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
