"""第 1e 便（時系列の横断検索）の評価問の素材づくり（実装より先に立てる＝docs/第1e便_計画.md §3）。

  python -m companies.eval.make_trend_candidates
出力：companies/data/eval/trend.jsonl（系列と集約の正例）・trend_fail_closed.jsonl（会社ごとの除外）。**上書きする**
＝人手の問（横断の問・受付の誤り）は trend_queries.jsonl に置く（本ファイルは触らない）。

期待値の取り方＝式エンジン（core/trend.py）とは別の経路：
- 各年の値＝その会社の**全部の書類**の公式 CSV（type=5）と自前のパーサの 2 経路が一致した点だけを集め、
  §2-1 の規則（その年を載せた書類のうち提出日が最新＝案 B）で出所の書類を本ファイルで選ぶ。
  式の入力に前期がある型（ROA）は、その年の入力がすべて載る書類のうち最新。
- restated＝出所の書類と同じ期・同じ項目・同じ会計基準の値が違う他の書類（差が両書類の表示単位の粗い方の 1 単位以内は丸め＝数えない）。
- 集約（cagr・change・ratio・mean・min・max・streak_up・streak_down・years_meeting）は本ファイルで Decimal で計算する。
- 除外の問の期（会計基準の変わり目・変則決算・株式分割 等）は書類の構造（context・DEI）と公式 CSV から決める。
★公式 CSV は API から取る（data/cache/csv/ に置く・EDINET_API_KEY が要る）。API の取得範囲（提出日で約 10 年）を外れた書類は取れない。
"""
from __future__ import annotations

import hashlib
import json
import sys
from decimal import Decimal, getcontext

from companies.core import store
from companies.core.items import ITEMS, TOP_LINE, standard_of
from companies.eval.make_candidates import EVAL, NONCON, TODAY, load, period_of, plain
from companies.ingest import verify_xbrl as v

getcontext().prec = 28
AS_OF = "2026-09-30"
PER_SHARE = ("eps_basic", "eps_diluted", "net_assets_per_share", "dividend_per_share", "interim_dividend_per_share",
             "equity_per_share_attributable_to_owners")
_DOCS: dict[str, dict] = {}


# ── 書類ごとの値（公式 CSV と自前のパーサの 2 経路一致）────────────────
def doc(doc_id: str) -> dict:
    if doc_id not in _DOCS:
        c = load(doc_id)
        inst = v.parse_instance(v.fetch_zip(doc_id))
        dec = {(f"{f['prefix']}:{f['name']}", f["context"]): f["decimals"] for f in inst["facts"] if not f["nil"]}
        cur = c["contexts"]["CurrentYearInstant"][0][:7]
        vals = {}
        for (el, ctx), row in c["rows"].items():
            if not el.endswith("SummaryOfBusinessResults") or not plain(ctx) or row["value"] in ("", "－"):
                continue
            if c["mine"].get((el, ctx)) != row["value"]:
                print(f"  2 経路の不一致＝使わない: {c['name']} {doc_id} {el} {ctx}", file=sys.stderr)
                continue
            basis = "non_consolidated" if ctx.endswith(NONCON) else "consolidated"
            vals[(el, basis, period_of(c, ctx))] = (row["value"], dec.get((el, ctx)))
        _DOCS[doc_id] = {"doc_id": doc_id, "name": c["name"], "standard": c["standard"], "current": cur, "values": vals,
                         "periods": sorted({p for (_, _, p) in vals}, reverse=True)}
    return _DOCS[doc_id]


def docs_of(code: str) -> list[dict]:
    co = store.registry()[code]
    return [doc(d) | {"submitted": co["documents"][d]["submitted"]}
            for d in sorted(co["documents"], key=lambda d: (co["documents"][d]["submitted"], d))]


def _unit(dec) -> Decimal:
    try:
        return Decimal(10) ** (-int(dec))
    except (TypeError, ValueError):
        return Decimal(0)


def item_values(ds: list[dict], item: str, basis: str, std: str) -> dict:
    """{決算期: [(書類, 提出日, 要素, 値, decimals) …提出日の順]}"""
    out = {}
    for d in ds:
        for local in ITEMS[item][1]:
            el = f"jpcrp_cor:{local}"
            if standard_of(el) != std:
                continue
            for (e, b, p), (val, dec) in d["values"].items():
                if e == el and b == basis:
                    out.setdefault(p, []).append((d["doc_id"], d["submitted"], el, val, dec))
    return out


def _months(a: str, b: str) -> int:
    return (int(a[:4]) - int(b[:4])) * 12 + int(a[5:7]) - int(b[5:7])


def latest_std(ds: list[dict], period_to: str | None) -> str:
    return [d for d in ds if not period_to or d["current"] <= period_to][-1]["standard"]


def top_item(ds, basis, std, period_to) -> str | None:
    last = [d for d in ds if not period_to or d["current"] <= period_to][-1]
    for k in TOP_LINE:
        if item_values([last], k, basis, std).get(last["current"]):
            return k
    return None


# ── 系列（§2-1＝案 B）と集約 ─────────────────────────────
def series(code: str, metric: str, *, basis: str, period_from=None, period_to=None) -> tuple[list[dict], str, str | None]:
    ds = docs_of(code)
    std = latest_std(ds, period_to)
    item = top_item(ds, basis, std, period_to) if metric == "top_line" else metric
    vals = item_values(ds, item, basis, std)
    ps = sorted(p for p in vals if (not period_from or p >= period_from) and (not period_to or p <= period_to))
    for a, b in zip(ps, ps[1:]):
        assert _months(b, a) == 12, (code, a, b)  # 正例は 12 か月の並びだけ（変則決算は負例の側）
    # 期間を指定した正例は、期間の両端まで年がそろうこと（そろわなければ insufficient_history の側）
    assert ps and (not period_from or _months(ps[0], period_from) < 12) and (not period_to or _months(period_to, ps[-1]) < 12), (code, metric, ps[:1], ps[-1:])
    rows = []
    for p in ps:
        cand = vals[p]
        src = cand[-1]  # 提出日が最新
        u = _unit(src[4])
        restated = [{"doc_id": x[0], "value": x[3]} for x in cand[:-1]
                    if abs(Decimal(x[3]) - Decimal(src[3])) > max(u, _unit(x[4]))]
        rows.append({"period": p, "doc_id": src[0], "element": src[2], "value": src[3], **({"restated": restated} if restated else {})})
    return rows, std, item


def roa_series(code: str, *, basis: str, period_from=None, period_to=None) -> tuple[list[dict], str]:
    """ROA＝bottom_line / ((total_assets[t] + total_assets[t-1]) / 2)。その年の入力がすべて載る書類のうち最新の書類で計算する。"""
    ds = docs_of(code)
    std = latest_std(ds, period_to)
    bl = "profit_attributable_to_owners" if basis == "consolidated" else "net_income"
    rows = []
    for p in sorted({p for d in ds for p in d["periods"]}):
        if (period_from and p < period_from) or (period_to and p > period_to):
            continue
        for d in reversed(ds):
            b = item_values([d], bl, basis, std).get(p)
            ta = item_values([d], "total_assets", basis, std)
            prev = [q for q in d["periods"] if q < p]
            if b and p in ta and prev and prev[0] in ta and _months(p, prev[0]) == 12:
                t0, t1 = Decimal(ta[p][0][3]), Decimal(ta[prev[0]][0][3])
                rows.append({"period": p, "doc_id": d["doc_id"], "value": str(Decimal(b[0][3]) / ((t0 + t1) / 2)),
                             "inputs": {"bottom_line[t]": b[0][3], "total_assets[t]": ta[p][0][3], "total_assets[t-1]": ta[prev[0]][0][3]}})
                break
        else:
            raise AssertionError(f"ROA の入力がそろう書類が無い: {code} {p}")
    return rows, std


def aggregates(rows: list[dict], *, year_min=None) -> dict:
    x = [Decimal(r["value"]) for r in rows]
    n = len(x)
    out = {"change": x[-1] - x[0], "mean": sum(x) / n, "min": min(x), "max": max(x)}
    if x[0] > 0:
        out["ratio"] = x[-1] / x[0]
        if x[-1] >= 0:
            out["cagr"] = (x[-1] / x[0]) ** (Decimal(1) / (n - 1)) - 1
    for name, up in (("streak_up", True), ("streak_down", False)):
        k = 0
        for a, b in zip(reversed(x[:-1]), reversed(x[1:])):
            if (b > a) if up else (b < a):
                k += 1
            else:
                break
        out[name] = k
    if year_min is not None:
        out["years_meeting"] = sum(1 for y in x if y >= Decimal(str(year_min)))
    return {k: str(val) for k, val in out.items()}


# ── 正例 ─────────────────────────────────────────────
POSITIVE = [  # (EDINET コード, 呼び名, metric, basis, 期間（None＝既定＝収録の最古〜最新）, 注)
    ("E04506", "九州電力", "top_line", "consolidated", (None, None), "2022 年の書類が 2018〜2021 年 3 月期の売上高を約 3 割低く組み替えた（収益認識基準の遡及適用）＝その年に restated"),
    ("E03752", "野村ホールディングス", "total_assets", "consolidated", (None, None), "米国基準・2019 年の書類が過去の総資産を組み替えた"),
    ("E01808", "ヨコオ", "total_assets", "consolidated", (None, None), "2019 年の書類の総資産が 1000 倍（単位の誤り）＝後の書類に載る期は後の書類の値・2015-03 はその書類が最新"),
    ("E03606", "三菱ＵＦＪ", "top_line", "consolidated", (None, None), "銀行＝最上段の収益は経常収益"),
    ("E04397", "青森放送", "top_line", "non_consolidated", (None, None), "連結なし"),
    ("E04397", "青森放送", "total_assets", "non_consolidated", (None, None), "連結なし"),
    ("E00883", "花王", "top_line", "consolidated", ("2016-12", None), "IFRS・12 月決算"),
    ("E00883", "花王", "roe", "consolidated", ("2016-12", None), "IFRS・比率（開示値）＝years_meeting の素材"),
    ("E00011", "住友林業", "top_line", "consolidated", ("2021-12", "2025-12"), "3 月→12 月の変更の後だけ＝12 か月の並び"),
    ("E02144", "トヨタ", "total_assets", "consolidated", ("2020-03", None), "IFRS の後だけ（移行の書類が前期の IFRS の値も持つ）"),
    ("E00337", "六甲バター", "total_assets", "non_consolidated", (None, None), "単体を指定＝全年に単体の値"),
    ("E00558", "日本製麻", "total_assets", None, (None, None), "basis の指定なし＝最新の年に連結が無い＝単体の系列（全年に単体の値）"),
]
ROA = [("E00394", "アサヒ", "consolidated", ("2015-12", None))]  # IFRS の当期純利益は 2015-12 から（総資産は移行日の 2014-12 から）＝IFRS で計算できる最古の年から
YEAR_MIN = {"roe": 0.08}


def positive(code, name, metric, basis, window, note) -> dict:
    ds = docs_of(code)
    b = basis or ("consolidated" if item_values([ds[-1]], "total_assets", "consolidated", ds[-1]["standard"]).get(ds[-1]["current"])
                  else "non_consolidated")
    rows, std, item = series(code, metric, basis=b, period_from=window[0], period_to=window[1])
    return {"id": f"{code}-{metric}-{b[:3]}-{window[0] or 'min'}-{window[1] or 'max'}", "kind": "series", "metric": metric,
            "company": {"edinet_code": code, "name": name}, "basis": basis, "expected_basis": b,
            "period_from": window[0], "period_to": window[1], "as_of": AS_OF,
            "expected": {"accounting_standard": std, "top_line_item": item if metric == "top_line" else None, "series": rows,
                         "restated_years": [r["period"] for r in rows if "restated" in r],
                         "aggregates": aggregates(rows, year_min=YEAR_MIN.get(metric)), "year_min": YEAR_MIN.get(metric)},
            "checked_by": "全書類の公式 CSV と自前のパーサの 2 経路一致の値から、出所の書類（その年を載せた最新の書類）と集約を本ファイルで決めた＝エンジンとは別の経路",
            "checked_at": TODAY, "note": note}


def roa_positive(code, name, basis, window) -> dict:
    rows, std = roa_series(code, basis=basis, period_from=window[0], period_to=window[1])
    return {"id": f"{code}-roa-{basis[:3]}", "kind": "series_derived", "metric": "roa", "company": {"edinet_code": code, "name": name},
            "basis": basis, "expected_basis": basis, "period_from": window[0], "period_to": window[1], "as_of": AS_OF,
            "expected": {"accounting_standard": std, "series": rows, "aggregates": aggregates(rows)},
            "checked_by": "全書類の公式 CSV（2 経路一致）から、その年の入力（当期純利益・当期と前期の総資産）がすべて載る最新の書類で計算",
            "checked_at": TODAY,
            "note": "派生値の年ごとの計算は、その年の値を出した書類の中で（年をまたいで期首を取らない）＝古い年は「その年＋3 年」の書類"}


# ── 除外（会社ごと）──────────────────────────────────────
def neg(code, name, metric, reason, *, basis=None, window=(None, None), aggregate=None, note="") -> dict:
    return {"id": f"{code}-{metric}-{reason}-{window[0] or 'min'}-{window[1] or 'max'}", "kind": "excluded", "metric": metric,
            "aggregate": aggregate, "company": {"edinet_code": code, "name": name}, "basis": basis,
            "period_from": window[0], "period_to": window[1], "as_of": AS_OF, "reason": reason, "note": note}


def fixed_negatives() -> list[dict]:
    return [
        neg("E02144", "トヨタ", "total_assets", "standard_changed", note="米国基準→IFRS（2021 年 3 月期の書類から）＝既定の期間は両方の基準をまたぐ"),
        neg("E01777", "ソニー", "top_line", "standard_changed", note="米国基準→IFRS（2022 年 3 月期の書類から）"),
        neg("E00011", "住友林業", "top_line", "irregular_period", note="3 月→12 月の変更（2020-12 は 9 か月）"),
        neg("E00011", "住友林業", "top_line", "irregular_period", window=("2020-12", "2025-12"),
            note="期間の最初の年（2020-12）自体が 9 か月＝その年の前の決算期（2020-03）との間隔で判定する"),
        neg("E00337", "六甲バター", "total_assets", "basis_changed", note="basis の指定なし＝最新の年（2025-12）は連結・それより前は連結を作成していない"),
        neg("E00558", "日本製麻", "total_assets", "basis_changed", basis="consolidated", window=("2017-03", "2026-03"),
            note="連結を指定＝2026-03 は連結を作成していない"),
    ]


def insufficient_history() -> dict:
    reg = store.registry()
    for code in sorted(reg, key=lambda k: hashlib.sha1(f"trend-short-{k}".encode()).hexdigest()):
        co = reg[code]
        subs = sorted(x["submitted"] for x in co["documents"].values())
        if subs and subs[0] >= "2022-01-01" and subs[-1] >= "2025-01-01" and co["consolidated"]:
            return neg(code, co["name"], "top_line", "insufficient_history", window=("2016-01", "2025-12"),
                       note=f"最初の有報の提出が {subs[0]}＝期間の最初の年が無い（上場の前）")
    raise AssertionError("insufficient_history の会社が見つからない")


def top_line_changed() -> dict:
    """同じ会計基準・連結のまま、案 B の出所の書類の間で最上段の収益の標準の項目が変わる会社（例＝売上高→営業収益）。
    後の書類が古い年を新しい項目に組み替えて載せる会社（ＧＦＡ）は、案 B では変更にならない＝出所の書類の項目で確かめる。
    置き場の要素で候補を選び、公式 CSV で確かめる（12 か月の並び・全年に連結の値がある会社だけ）。"""
    reg = store.registry()
    keys = {f"jpcrp_cor:{e}": k for k in TOP_LINE for e in ITEMS[k][1]}
    for code in sorted(reg, key=lambda k: hashlib.sha1(f"trend-topline-{k}".encode()).hexdigest()):
        co = reg[code]
        if co["accounting_standard"] != "Japan GAAP" or not co["consolidated"] or len(co["documents"]) != 10:
            continue
        found = {}
        for f in store.facts_of(code):
            if f["basis"] == "consolidated" and not f["dims"] and f["element"] in keys:
                found.setdefault(f["period"], {})[f["doc_id"]] = (f["submitted"], keys[f["element"]])
        ps = sorted(found)
        if len(ps) < 12 or any(_months(b, a) != 12 for a, b in zip(ps, ps[1:])):
            continue
        items = {max(v.values())[1] for v in found.values()}  # 案 B＝その年を載せた最新の書類の項目
        if len(items) < 2:
            continue
        ds = docs_of(code)
        got = {}
        for k in TOP_LINE:
            for p, cand in item_values(ds, k, "consolidated", "Japan GAAP").items():
                got.setdefault(p, []).extend((x[1], x[0], k) for x in cand)
        if sorted(got) != ps:
            continue
        seq = [max(got[p])[2] for p in ps]
        if len(set(seq)) > 1:
            return neg(code, co["name"], "top_line", "top_line_changed",
                       note=f"その年を載せた最新の書類の最上段の収益の項目が年で変わる（{seq[0]}→{seq[-1]}）")
    raise AssertionError("top_line_changed の会社が見つからない")


def split_questions() -> tuple[dict, dict]:
    """株式分割＝隣り合う書類の重なる期で、1 株当たりの値だけが一定の比率でずれる。分割の後の書類の当期を Y とすると、
    案 B では Y−4 以降が分割の後の書類・Y−5 は分割の前の書類から＝[Y−5, Y] は stock_split・[Y−4, Y] は正例。"""
    reg = store.registry()
    for code in sorted(reg, key=lambda k: hashlib.sha1(f"trend-split-{k}".encode()).hexdigest()):
        co = reg[code]
        if co["accounting_standard"] != "Japan GAAP" or not co["consolidated"] or len(co["documents"]) != 10:
            continue
        by = {}
        for f in store.facts_of(code):
            if f["element"] == "jpcrp_cor:BasicEarningsLossPerShareSummaryOfBusinessResults" and f["basis"] == "consolidated" and not f["dims"]:
                by.setdefault(f["period"], {})[f["doc_id"]] = (f["submitted"], Decimal(f["value"]))
        docs = sorted(co["documents"], key=lambda d: co["documents"][d]["submitted"])
        for old, new in zip(docs, docs[1:]):
            rs = [x[old][1] / x[new][1] for x in by.values() if old in x and new in x and x[new][1] != 0]
            if len(rs) >= 3 and max(rs) / min(rs) < Decimal("1.02") and min(rs) > 1 and abs(min(rs) - 1) > Decimal("0.05"):
                ds = docs_of(code)
                d_new = next(d for d in ds if d["doc_id"] == new)
                y = d_new["current"]
                if int(y[:4]) < 2019:
                    continue
                if d_new is not ds[-1]:  # 分割の後の書類が最新でないと [Y−4, Y] に更に後の書類が入る＝最新の書類で分割した会社に限る
                    continue
                y5, y4 = f"{int(y[:4]) - 5}{y[4:]}", f"{int(y[:4]) - 4}{y[4:]}"
                q = neg(code, co["name"], "eps_basic", "stock_split", window=(y5, y), aggregate="cagr",
                        note=f"{old}→{new} で 1 株当たりの値が約 {min(rs):.2f} 倍ずれる（分割）＝{y5} は分割の前の書類の値")
                p = positive(code, co["name"], "eps_basic", "consolidated", (y4, y),
                             f"分割（{old}→{new}）の後の書類だけ＝段差なし（同じ会社の [{y5}, {y}] は stock_split）")
                return q, p
    raise AssertionError("株式分割の会社が見つからない")


def nonpositive_start() -> dict:
    """期間の最初の年の当期純利益がマイナス・最後の年がプラス（置き場の値で選び、公式 CSV で確かめる）。"""
    reg = store.registry()
    el = "jpcrp_cor:ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults"
    for code in sorted(reg, key=lambda k: hashlib.sha1(f"trend-neg-{k}".encode()).hexdigest()):
        co = reg[code]
        if co["accounting_standard"] != "Japan GAAP" or not co["consolidated"] or len(co["documents"]) != 10:
            continue
        cur = {f["period"]: Decimal(f["value"]) for f in store.facts_of(code)
               if f["element"] == el and f["basis"] == "consolidated" and not f["dims"] and "2017-01" <= f["period"] <= "2025-12"}
        ps = sorted(cur)
        if len(ps) != 9 or not (cur[ps[0]] < 0 < cur[ps[-1]]):
            continue
        rows, _, _ = series(code, "profit_attributable_to_owners", basis="consolidated", period_from="2017-01", period_to="2025-12")
        if Decimal(rows[0]["value"]) < 0 < Decimal(rows[-1]["value"]):
            return neg(code, co["name"], "bottom_line", "nonpositive_start", window=("2017-01", "2025-12"), aggregate="cagr",
                       note=f"期間の最初の年（{rows[0]['period']}）の当期純利益が {rows[0]['value']}＝年平均成長率を計算しない")
    raise AssertionError("nonpositive_start の会社が見つからない")


def main() -> int:
    pos = [positive(*x) for x in POSITIVE] + [roa_positive(*x) for x in ROA]
    negs = fixed_negatives() + [insufficient_history(), top_line_changed()]
    q, p = split_questions()
    negs.append(q)
    pos.append(p)
    negs.append(nonpositive_start())
    (EVAL / "trend.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in pos))
    (EVAL / "trend_fail_closed.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in negs))
    print(f"trend.jsonl: 正例 {len(pos)}（年の値 {sum(len(x['expected']['series']) for x in pos)}）・trend_fail_closed.jsonl: 負例 {len(negs)}"
          f"・読んだ書類 {len(_DOCS)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
