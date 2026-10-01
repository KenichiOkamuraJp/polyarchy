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
        # 株式併合・分割の前と後の値が 1 つの系列に混ざる（同じ書類の中の段差・restated の出ない段差＝2026-10-01 staging で年平均成長率の上位を占めた）
        *[neg(code, name, "eps_basic", "share_count_changed", window=("2016-01", "2025-12"), aggregate="cagr", note=note) for code, name, note in (
            ("E03861", "ダイビル株式会社", "上場廃止の前の株式併合（2021 年 3 月期の書類から 1 株当たりが 10 億円台）＝2020-03 以前の 45〜68 円とつながる。後年の書類の restated は出る"),
            ("E01635", "株式会社宇野澤組鐵工所", "2017 年の株式併合（10→1）＝2016-03 の 1.39 円は 2020 年の書類でも併合の前のまま・2017-03 以降は併合の後＝restated が出ない（隣の書類の比では見えない）"),
            ("E00400", "養命酒製造株式会社", "最新の書類が 2025-03 だけを併合の後の株数で載せ、2022〜2024-03 は前のまま＝同じ書類の中の段差"),
            ("E00693", "大日本印刷株式会社", "上場会社の株式分割（2024 年）＝最新の書類が 2024-03 以降だけを分割の後で載せ、2023-03 以前は前のまま＝同じ書類の中の段差（自己株式の取得と重なり比は約 1.8）"))],
    ]


ISSUED = "jpcrp_cor:TotalNumberOfIssuedSharesSummaryOfBusinessResults"


def share_count_comparable(q: dict) -> dict:
    """share_count_changed の負例に、比べられる事実の期待値を添える（2026-10-01 本人決定＝株数をそろえた EPS は作らない）：
    同じ期間・同じ basis の当期純利益（bottom_line）の同じ集約と、1 株当たりの系列の最初と最後の年の発行済株式総数（その年を載せた最新の書類）。
    期待値は公式 CSV の 2 経路一致の値から＝エンジンの索引とは別の経路。"""
    code = q["company"]["edinet_code"]
    ds = [d for d in docs_of(code) if d["submitted"] <= q["as_of"]]
    last = [d for d in ds if not q["period_to"] or d["current"] <= q["period_to"]][-1]
    b = q["basis"] or ("consolidated" if item_values([last], "total_assets", "consolidated", last["standard"]).get(last["current"])
                       else "non_consolidated")
    eps, _, _ = series(code, q["metric"], basis=b, period_from=q["period_from"], period_to=q["period_to"])
    bl, _, _ = series(code, "profit_attributable_to_owners" if b == "consolidated" else "net_income", basis=b,
                      period_from=q["period_from"], period_to=q["period_to"])
    shares = []
    for p in (eps[0]["period"], eps[-1]["period"]):
        cand = [(d["submitted"], d["doc_id"], val) for d in ds for (e, _b, pp), (val, _dec) in d["values"].items() if e == ISSUED and pp == p]
        sub, doc_id, val = max(cand)
        shares.append({"period": p, "value": val, "doc_id": doc_id})
    agg = aggregates(bl)
    return {**q, "expect_comparable": {"bottom_line": {"aggregate": q["aggregate"], "value": agg.get(q["aggregate"]),
                                                       "periods": [bl[0]["period"], bl[-1]["period"]]},
                                       "issued_shares": shares},
            "checked_by": q.get("checked_by") or "比べられる事実＝全書類の公式 CSV と自前のパーサの 2 経路一致の値から本ファイルで決めた"}


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


# ── 段②：海外売上比率の時系列 ─────────────────────────────
N_OVERSEAS_JGAAP, N_OVERSEAS_IFRS = 6, 3


def _top_at(d: dict, basis: str, std: str, t: str):
    for k in TOP_LINE:
        got = item_values([d], k, basis, std).get(t)
        if got:
            return k, Decimal(got[0][3])
    return None


def _overseas_year(d: dict, t: str, std: str):
    """その書類の地域別の表（本文 inline XBRL を別のパーサで）から、年 t の本邦と合計のセル。形の単純な表だけ・合計×単位＝その書類の
    公式 CSV の最上段の収益（t）。取れなければ None。"""
    from companies.eval.make_region_candidates import ix_blocks
    from companies.eval.make_screen_candidates import UNITS, _num, _pair_jgaap, _pairs_ifrs
    top = _top_at(d, "consolidated", std, t)
    if not top:
        return None
    ctx = "CurrentYearDuration" if d["current"] == t else "Prior1YearDuration"
    want = "geographic_areas_ifrs" if std == "IFRS" else "revenue"
    blocks = [b for b in ix_blocks(d["doc_id"]) if b["section"] == want and "NonConsolidated" not in b["context"]
              and b["context"].startswith("CurrentYearDuration" if std == "IFRS" else ctx)]
    if len(blocks) != 1:
        return None
    cands = set()
    for tb in (x for x in blocks[0]["content"] if x["type"] == "table"):
        for pr in ([_pair_jgaap(tb)] if std != "IFRS" else _pairs_ifrs(tb)):
            if not pr:
                continue
            hv, tv = _num(pr[0]), _num(pr[1])
            if hv is None or not tv:
                continue
            if any(abs(tv * u - top[1]) < u for u in UNITS):
                cands.add(pr)
    if len(cands) != 1:
        return None
    home, total = next(iter(cands))
    return {"home_text": home, "total_text": total, "value": str((_num(total) - _num(home)) / _num(total)), "top_line_item": top[0]}


def _run(code: str, co: dict, std: str, secs: list[dict]) -> list[str] | None:
    """絞り込みだけ（置き場の欄で見る）＝地域別の欄が形の単純な表で続いている、最新側の書類の並び（古い順）。
    IFRS の地域別の欄のタグは古い書類に無い（2026-09-30＝10 書類すべてにある IFRS の会社は 236 社のうち 1 社）＝並びは最新側から数える。"""
    from companies.eval.make_screen_candidates import _pair_jgaap, _pairs_ifrs
    want = "geographic_areas_ifrs" if std == "IFRS" else "revenue"
    ok = {}
    for x in secs:
        if x["section"] == want and x["basis"] == "consolidated":
            tabs = [c for c in x["content"] if c["type"] == "table"]
            ok[(x["doc_id"], x["context"][:6])] = any(_pairs_ifrs(t) if std == "IFRS" else _pair_jgaap(t) for t in tabs)
    docs = sorted(co["documents"], key=lambda d: co["documents"][d]["submitted"])
    run = []
    for d in reversed(docs):
        if ok.get((d, "Curren")) and (std == "IFRS" or ok.get((d, "Prior1"))):
            run.insert(0, d)
        else:
            break
    return run if len(run) >= 5 else None


def overseas_positives() -> list[dict]:
    """案 B＝年 t の表は、t の表が載る書類のうち最新＝翌年の書類（日本基準は前期の欄・IFRS は表の前期の列）。最新の年だけはその年の書類。"""
    reg = store.registry()
    out, n = [], {"Japan GAAP": 0, "IFRS": 0}
    for code in sorted(reg, key=lambda k: hashlib.sha1(f"trend-overseas-{k}".encode()).hexdigest()):
        co = reg[code]
        std = co["accounting_standard"]
        if std not in n or n[std] >= (N_OVERSEAS_IFRS if std == "IFRS" else N_OVERSEAS_JGAAP) or not co["consolidated"] or len(co["documents"]) < 8:
            continue
        if max(co["documents"][d]["submitted"] for d in co["documents"]) > AS_OF:
            continue
        run = _run(code, co, std, store.regions_of(code).get("sections", []))
        if not run:
            continue  # 置き場の欄の形で先に絞る（公式 CSV を取る前に＝外れる会社の取得で 10 分以上かかった）。期待値は別のパーサと公式 CSV で取る
        ds = [d for d in docs_of(code) if d["doc_id"] in run]
        if any(d["standard"] != std for d in ds) or any(_months(b["current"], a["current"]) != 12 for a, b in zip(ds, ds[1:])):
            continue
        years = [f"{int(ds[0]['current'][:4]) - 1}{ds[0]['current'][4:]}"] + [d["current"] for d in ds]
        rows = []
        for i, t in enumerate(years):
            src = ds[i] if i < len(ds) else ds[-1]  # years[i] の翌年の書類＝ds[i]（最新の年 years[-1]＝ds[-1] の当期は ds[-1] 自身）
            y = _overseas_year(src, t, std)
            if not y:
                rows = None
                break
            rows.append({"period": t, "doc_id": src["doc_id"], **y})
        if not rows or len({r["top_line_item"] for r in rows}) != 1:
            continue
        n[std] += 1
        out.append({"id": f"{code}-overseas_sales_ratio-{years[0]}-{years[-1]}", "kind": "series_overseas", "metric": "overseas_sales_ratio",
                    "company": {"edinet_code": code, "name": co["name"]}, "basis": None, "expected_basis": "consolidated",
                    "period_from": years[0], "period_to": years[-1], "as_of": AS_OF,
                    "expected": {"accounting_standard": std, "series": [{k: r[k] for k in ("period", "doc_id", "home_text", "total_text", "value")} for r in rows],
                                 "aggregates": aggregates(rows)},
                    "checked_by": "本文 inline XBRL の表（取込とは別のパーサ・形の単純な表だけ）で本邦と合計のセル＋合計×単位＝その書類の公式 CSV の最上段の収益（2 経路）。"
                                  "出所の書類（翌年の書類）と集約は本ファイルで決めた",
                    "checked_at": TODAY,
                    "note": "日本基準は翌年の書類の前期の欄・IFRS は翌年の書類の表の前期の列（最新の年だけはその年の書類）"
                            + ("・IFRS の地域別の欄のタグは古い書類に無い＝欄が続く最新側の書類から" if std == "IFRS" else "")})
        if all(n[k] >= (N_OVERSEAS_IFRS if k == "IFRS" else N_OVERSEAS_JGAAP) for k in n):
            break
    return out


def overseas_negatives() -> list[dict]:
    out = [neg("E03752", "野村ホールディングス", "overseas_sales_ratio", "regions_not_tagged", window=("2016-03", "2026-03"),
               note="米国基準＝地域別の要素でタグ付けしていない（第 1c 便と同じ）")]
    reg = store.registry()
    for code in sorted(reg, key=lambda k: hashlib.sha1(f"trend-overseas-stmt-{k}".encode()).hexdigest()):
        co = reg[code]
        if co["accounting_standard"] != "Japan GAAP" or not co["consolidated"] or len(co["documents"]) != 10:
            continue
        cur = {}
        for x in store.regions_of(code).get("sections", []):
            if x["section"] == "revenue" and x["context"] == "CurrentYearDuration" and x["basis"] == "consolidated":
                text = " ".join(c.get("text", "") for c in x["content"] if c["type"] == "text")
                cur[x["period"]] = "table" if any(c["type"] == "table" and len(c["rows"]) > 1 for c in x["content"]) else (
                    "stmt" if "本邦" in text and ("90" in text or "９０" in text) else "other")
        ps = sorted(cur)
        seq = [cur[p] for p in ps]
        if len(seq) >= 8 and seq[-1] == "table" and set(seq) == {"table", "stmt"}:
            k = seq.index("stmt")
            out.append(neg(code, co["name"], "overseas_sales_ratio", "overseas_statement_only", window=(ps[k], ps[-1]),
                           note=f"{ps[k]} は「本邦が 90% 超」の文だけ（その年の欄）＝その年の比率は出さない（期間を表のある年に絞れば引ける）"))
            break
    return out


# ── 段③：1 社の区分・地域別の年ごとの並び ───────────────────────
def _seg_rows(doc_id: str) -> dict:
    """公式 CSV のセグメント情報の値（会社が定義した区分・連結）＝{(区分, 要素, 当期か前期): 値}。区分は context の末尾から。"""
    from companies.core.segments import section_of
    from companies.eval.make_candidates import official_csv
    out = {}
    for el, _label, ctx, *_rest, value in official_csv(doc_id):
        for base in ("CurrentYearDuration_", "Prior1YearDuration_", "CurrentYearInstant_", "Prior1YearInstant_"):
            if ctx.startswith(base) and ctx.endswith("ReportableSegmentsMember") and "NonConsolidated" not in ctx:
                member = ctx[len(base):]
                if "_E" in member and section_of(el) == "segment_information" and value not in ("", "－"):
                    out[(member, el, base.split("Year")[0])] = value
    return out


def _member_key(member: str) -> str:
    """置き場の区分（接頭辞:要素）→ 公式 CSV の context の末尾（接頭辞と要素をつないだ形）。"""
    return member.replace(":", "")


def segments_range_positives(per_kind: int = 2) -> list[dict]:
    reg = store.registry()
    out, kinds = [], {"stable": 0, "regrouped": 0}
    for code in sorted(reg, key=lambda k: hashlib.sha1(f"trend-seg-{k}".encode()).hexdigest()):
        co = reg[code]
        if co["accounting_standard"] != "Japan GAAP" or not co["consolidated"] or len(co["documents"]) != 10:
            continue
        data = store.segments_of(code)
        docs = sorted(data["docs"], key=lambda d: data["docs"][d]["submitted"])
        if len(docs) != 10 or any(data["docs"][d]["submitted"] > AS_OF for d in docs):
            continue
        per_year = {}
        for f in data["facts"]:  # 絞り込みだけ（置き場）＝各年の出所の書類（その期を載せた最新の書類）の区分の組
            if f["basis"] == "consolidated" and f["section"] == "segment_information" and ":" in f["member"] and "_E" in f["member"]:
                per_year.setdefault(f["period"], {}).setdefault(f["doc_id"], set()).add(f["member"])
        years = sorted(per_year)
        if len(years) != 11 or any(_months(b, a) != 12 for a, b in zip(years, years[1:])):
            continue
        sets = [per_year[y][max(per_year[y], key=lambda d: data["docs"][d]["submitted"])] for y in years]
        changes = sum(1 for a, b in zip(sets, sets[1:]) if a != b)
        kind = "stable" if changes == 0 else "regrouped" if changes == 1 else None
        if kind is None or kinds[kind] >= per_kind:
            continue
        # 期待値＝公式 CSV（出所の書類＝その期を当期か前期として載せた最新の書類）
        csvs = {d: _seg_rows(d) for d in docs}
        cur = {d: data["docs"][d]["periods"] for d in docs}
        exp_years, series, members_by_year = [], {}, []
        el_count = {}
        for rows in csvs.values():
            for (m, el, _), _v in rows.items():
                el_count[el] = el_count.get(el, 0) + 1
        el = max(el_count, key=el_count.get) if el_count else None
        if not el:
            continue
        for y in years:
            cands = [(d, "Current" if cur[d].get("current") == y else "Prior1") for d in docs if y in cur[d].values()]
            cands = [(d, w) for d, w in cands if any(k[2] == w for k in csvs[d])]
            if not cands:
                break
            src, w = cands[-1]
            members = sorted({m for (m, e, ww) in csvs[src] if ww == w})
            exp_years.append({"period": y, "doc_id": src})
            members_by_year.append(members)
            for m in members:
                v = csvs[src].get((m, el, w))
                if v is None:
                    continue
                others = [{"doc_id": d, "value": csvs[d][(m, el, ww)]} for d, ww in
                          [(d, "Current" if cur[d].get("current") == y else "Prior1") for d in docs if y in cur[d].values() and d != src]
                          if (m, el, ww) in csvs[d] and csvs[d][(m, el, ww)] != v]
                series.setdefault(m, []).append({"period": y, "value": v, "doc_id": src, **({"restated": others} if others else {})})
        if len(exp_years) != len(years):
            continue
        regrouped = [{"period": y, "added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b))}
                     for y, a, b in zip(years[1:], members_by_year, members_by_year[1:]) if a != b]
        if (kind == "stable") != (not regrouped):
            continue  # 置き場の絞り込みと公式 CSV が食い違う会社は材料にしない
        kinds[kind] += 1
        out.append({"id": f"{code}-segments-{years[0]}-{years[-1]}", "kind": "segments_range", "company": {"edinet_code": code, "name": co["name"]},
                    "period_from": years[0], "period_to": years[-1], "element": el,
                    "expected": {"years": exp_years, "series": series, "regrouped": regrouped},
                    "checked_by": "公式 CSV（区分の context・会社が定義した区分）＝取込とは別の経路。出所の書類（その期を当期か前期として載せた最新の書類）・"
                                  "組み替え（年ごとの区分の組の増減）・restated は本ファイルで決めた",
                    "checked_at": TODAY,
                    "note": ("区分の組が全年同じ" if kind == "stable" else f"区分の組み替えが {regrouped[0]['period']} に 1 回（旧区分と新区分を対応づけない）")})
        if all(v >= per_kind for v in kinds.values()):
            break
    return out


def _cells(content: list[dict]) -> list[str]:
    return [c["text"].strip() for x in content if x["type"] == "table" for row in x["rows"] for c in row if c["text"].strip()]


def regions_range_positives() -> list[dict]:
    """地域別の欄を年ごとに＝その期の欄が載る最新の書類（日本基準は翌年の書類の前期の欄・IFRS は翌年の書類の表）の表のセル。
    期待値＝本文 inline XBRL（取込とは別のパーサ）の同じ欄の表のセルの文字列（並び順どおり）。"""
    from companies.eval.make_region_candidates import ix_blocks
    reg = store.registry()
    out, n = [], {"Japan GAAP": 0, "IFRS": 0}
    for code in sorted(reg, key=lambda k: hashlib.sha1(f"trend-reg-{k}".encode()).hexdigest()):
        co = reg[code]
        std = co["accounting_standard"]
        if std not in n or n[std] >= (1 if std == "IFRS" else 2) or not co["consolidated"] or len(co["documents"]) < 8:
            continue
        run = _run(code, co, std, store.regions_of(code).get("sections", []))
        if not run:
            continue
        ds = [(d, co["documents"][d]) for d in run]
        cur = {d: store.regions_of(code)["docs"][d]["periods"]["current"] for d in run}
        years = [store.regions_of(code)["docs"][run[0]]["periods"]["prior"]] + [cur[d] for d in run]
        want = "geographic_areas_ifrs" if std == "IFRS" else "revenue"
        exp = []
        for i, y in enumerate(years):
            src = run[i] if i < len(run) else run[-1]
            ctx = "CurrentYearDuration" if (std == "IFRS" or cur[src] == y) else "Prior1YearDuration"
            blocks = [b for b in ix_blocks(src) if b["section"] == want and b["context"] == ctx]
            if len(blocks) != 1:
                exp = None
                break
            exp.append({"period": y, "doc_id": src, "section": want, "cells": _cells(blocks[0]["content"])})
        if not exp:
            continue
        n[std] += 1
        out.append({"id": f"{code}-regions-{years[0]}-{years[-1]}", "kind": "regions_range", "company": {"edinet_code": code, "name": co["name"]},
                    "period_from": years[0], "period_to": years[-1], "expected": {"years": exp},
                    "checked_by": "本文 inline XBRL の欄（取込とは別のパーサ）の表のセルの文字列・出所の書類は本ファイルで決めた", "checked_at": TODAY,
                    "note": "日本基準は翌年の書類の前期の欄・IFRS は翌年の書類の表（前期の列を含む＝period_in_columns）・最新の年はその年の書類"})
        if all(n[k] >= (1 if k == "IFRS" else 2) for k in n):
            break
    return out


LOOKUP_NEGATIVES = [
    {"id": "seg-range-and-period", "tool": "segments", "company": "E00345", "period": "2025-03", "period_from": "2020-03", "period_to": "2025-03",
     "reason": "bad_request", "note": "period と期間（period_from／period_to）は同時に指定しない"},
    {"id": "seg-range-doc", "tool": "segments", "company": "E00345", "doc_id": "S100YIC9", "period_from": "2020-03", "period_to": "2025-03",
     "reason": "bad_request", "note": "期間の指定では doc_id を受けない（年ごとに出所の書類が違う）"},
    {"id": "seg-range-reversed", "tool": "segments", "company": "E00345", "period_from": "2025-03", "period_to": "2020-03",
     "reason": "bad_period", "note": "period_from が period_to より後"},
    {"id": "seg-range-out", "tool": "segments", "company": "E00345", "period_from": "2000-03", "period_to": "2005-03",
     "reason": "out_of_range", "note": "収録の無い期間＝近い期で埋めない"},
    {"id": "reg-range-and-period", "tool": "regions", "company": "E00345", "period": "2025-03", "period_from": "2020-03", "period_to": "2025-03",
     "reason": "bad_request", "note": "period と期間は同時に指定しない"},
    {"id": "reg-range-reversed", "tool": "regions", "company": "E00345", "period_from": "2025-03", "period_to": "2020-03",
     "reason": "bad_period", "note": "period_from が period_to より後"},
    {"id": "reg-range-out", "tool": "regions", "company": "E00345", "period_from": "2000-03", "period_to": "2005-03",
     "reason": "out_of_range", "note": "収録の無い期間"},
]


def main() -> int:
    pos = [positive(*x) for x in POSITIVE] + [roa_positive(*x) for x in ROA]
    negs = [share_count_comparable(q) if q["reason"] == "share_count_changed" else q for q in fixed_negatives()]
    negs += [insufficient_history(), top_line_changed()]
    q, p = split_questions()
    negs.append(q)
    pos.append(p)
    negs.append(nonpositive_start())
    pos += overseas_positives()
    negs += overseas_negatives()
    lookup = segments_range_positives() + regions_range_positives()
    (EVAL / "trend_lookup.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lookup + [
        {**q, "kind": "lookup_excluded"} for q in LOOKUP_NEGATIVES]))
    print(f"trend_lookup.jsonl: 正例 {len(lookup)}（{[x['company']['name'] + ':' + x['kind'] for x in lookup]}）・負例 {len(LOOKUP_NEGATIVES)}", file=sys.stderr)
    (EVAL / "trend.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in pos))
    (EVAL / "trend_fail_closed.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in negs))
    print(f"trend.jsonl: 正例 {len(pos)}（年の値 {sum(len(x['expected']['series']) for x in pos)}）・trend_fail_closed.jsonl: 負例 {len(negs)}"
          f"・読んだ書類 {len(_DOCS)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
