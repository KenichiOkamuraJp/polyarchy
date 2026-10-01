"""時系列の横断検索（第 1e 便）＝年ごとの値をつないだ系列と、その集約（固定の語彙）＝docs/第1e便_計画.md §2。

式・項目・検証済みの型・除外の規則は第 1c 便の式エンジン（core/screen.py）をそのまま使い、年をまたぐ規則だけを足す：
  ① 各年の値＝その年の入力がすべて載る書類のうち提出日が最新の書類（案 B＝単社の参照の既定と同じ）。年ごとの式は、その書類の中で
     第 1c 便の規則どおりに計算する（年をまたいで期首を取らない）
  ② つなぎ目は除外せず事実として返す＝出所の書類と同じ期・同じ項目・同じ会計基準の値が違う他の書類を restated に（表示単位の粗い方の
     1 単位以内の差は丸め＝数えない）。修正で除外すると除外が多すぎる（総資産でずれ 0.1% 以上が約 4 割＝記録/書類をまたぐ修正の実測）
  ③ 系列が途切れる会社は黙ってつながず除外の理由へ＝standard_changed（系列の会計基準＝期間の最新の書類の宣言）・irregular_period
     （その年と前の決算期の間隔が 12 か月でない）・basis_changed（期間の途中で連結を作成していない年がある）・insufficient_history
     （指定した期間の端まで年がそろわない）・top_line_changed（最上段の収益の項目が年で変わる）・stock_split（1 株当たりの項目で、
     出所の書類が株式分割の前と後にまたがる）
  ④ 集約は固定の語彙（AGGREGATES）＝派生値と明記し、年ごとの系列（値・出所の書類・入力の開示値）を添える
基準日（as_of）より後に提出された書類は使わない（問の答えを基準日で固定する）。
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from companies.core import store
from companies.core.industries import INDUSTRIES, MANUFACTURING_NOTE, industry_of
from companies.core.items import RATIO_NOTE, TOP_LINE
from companies.core.screen import (COMPANY_TOP, MAX_LIMIT, NOT_INGESTED, REASON_NOTE, Excluded, ExprError, _brief,
                                   _column, _definition, _eval, _fmt, _is_ratio, _months, _periods, _Values)

PER_SHARE = {"eps_basic", "eps_diluted", "net_assets_per_share", "dividend_per_share", "interim_dividend_per_share",
             "equity_per_share_attributable_to_owners"}
AGGREGATES = {
    "cagr": "年平均成長率＝(終わりの年の値 ÷ 始めの年の値)^(1 ÷ (年数 − 1)) − 1（始めが 0 以下・終わりがマイナスは除外）",
    "change": "終わりの年の値 − 始めの年の値",
    "ratio": "終わりの年の値 ÷ 始めの年の値（始めが 0 以下は除外）",
    "mean": "期間の各年の値の平均",
    "min": "期間の各年の値の最小",
    "max": "期間の各年の値の最大",
    "streak_up": "期間の終わりから数えて、前年より増えた年が続いた回数（10 年連続増収＝10）",
    "streak_down": "期間の終わりから数えて、前年より減った年が続いた回数",
    "years_meeting": "各年の値が year_min 以上（year_max 以下）だった年の数",
}
TREND_REASONS = {
    "basis_changed": "期間の途中で連結財務諸表を作成していない年がある（連結と単体の系列をつながない）",
    "insufficient_history": "指定した期間の端まで年がそろわない（上場の前・有報の提出が無い年・期のずれのある式の入力がそろわない年）",
    "stock_split": "1 株当たりの項目で、系列の出所の書類が株式分割の前と後にまたがる（段差が数倍規模＝期間を分割の後にすれば引ける）",
    "nonpositive_start": "始めの年の値が 0 以下＝比・年平均成長率を計算しない",
    "negative_end": "終わりの年の値がマイナス＝年平均成長率を計算しない",
    "irregular_period": "期間の中に、前の決算期との間隔が 12 か月でない年（変則決算・決算期の変更）がある",
    "regions_not_in_document": "期間の中に、地域別の欄が載る書類の無い年がある（最古の書類の前期より前＝期間を後ろにずらせば引ける）",
    "standard_changed": "期間の中に、系列の会計基準（期間の最新の書類が宣言する基準）の値が無い年がある（基準の違う値をつながない）",
}
NOTE = ("values は派生値（集約）。各年の値は、その年の入力がすべて載る書類のうち提出日が最新の書類の値（単社の参照の既定と同じ）＝series の "
        "doc_id が出所。後年の書類で同じ期の値が組み替えられた年は、系列の行の restated に他の書類の値が並ぶ（除外はしない＝段差は "
        "restated_years で確かめる）。★売上系は収益認識基準の適用（2022 年 3 月期前後）で不連続になる会社がある（遡及適用した会社は "
        "restated に出る・しなかった会社は出ない）＝その年をまたぐ集約は系列の行を見る。海外売上比率の各年は、その年の地域別の表が載る最新の書類"
        "（翌年の書類の前期の欄・IFRS は表の前期の列）の本邦と合計の 2 セルから。比較できない会社は excluded に理由と件数。")
_SPLIT_MIN, _SPLIT_SPREAD = Decimal("0.05"), Decimal("1.02")


def _unit(dec) -> Decimal:
    try:
        return Decimal(10) ** (-int(dec))
    except (TypeError, ValueError):
        return Decimal(0)


def _miss(reason: str, **extra) -> dict:
    return {"found": False, "reason": reason, **extra}


def _as_of(as_of: str | None) -> str:
    return as_of or dt.date.today().isoformat()


def _request(aggregate, year_min, year_max, period_from, period_to, metric) -> dict | None:
    if aggregate is not None and aggregate not in AGGREGATES:
        return _miss("unknown_aggregate", aggregates=AGGREGATES)
    if aggregate == "years_meeting" and year_min is None and year_max is None:
        return _miss("bad_request", hint="years_meeting は year_min か year_max（各年の値の条件）が要る")
    for x in (year_min, year_max):
        if x is not None and not isinstance(x, (int, float)):
            return _miss("bad_request", hint="year_min／year_max は数（比率は 0.1 の形）")
    for p in (period_from, period_to):
        if p is not None and not (isinstance(p, str) and re.fullmatch(r"\d{4}-\d{2}", p)):
            return _miss("bad_period", hint="period_from／period_to は決算期末の YYYY-MM")
    return None


# ── 1 社の系列 ───────────────────────────────────────────
class _Company:
    """1 社・1 つの問の間の材料＝基準日までの書類（新しい順）と、書類ごとの決算期。"""

    def __init__(self, code: str, co: dict, as_of: str, cols: dict):
        self.code, self.co, self.cols = code, co, cols
        per = _periods().get(code, {})
        docs = self.co["documents"]
        self.docs = sorted((d for d in docs if docs[d]["submitted"] <= as_of and any((d, b) in per for b in ("consolidated", "non_consolidated"))),
                           key=lambda d: (docs[d]["submitted"], d), reverse=True)
        self.per = {k: v for k, v in per.items() if k[0] in self.docs}
        self.all_periods = sorted({p for ps in self.per.values() for p in ps})

    def current(self, doc: str) -> str:
        return max(self.per[(doc, b)][0] for b in ("consolidated", "non_consolidated") if (doc, b) in self.per)

    def has(self, doc: str, basis: str, period: str) -> bool:
        return period in self.per.get((doc, basis), ())

    def ctx(self, doc: str, basis: str, period: str, standard: str, top_item) -> dict:
        ps = list(self.per[(doc, basis)])
        return {"code": self.code, "co": self.co, "doc": doc, "basis": basis, "periods": ps[ps.index(period):],
                "values": _Values(self.code, doc, basis, self.cols), "standard": standard, "trend": True,
                **({"top_line_item": top_item} if top_item else {})}


def _year(c: _Company, tree, basis: str, p: str, std: str, top_item) -> tuple[Decimal, dict, str]:
    """その年の値＝入力がすべて載る書類のうち提出日が最新の書類で計算する。どの書類でも計算できなければ、最新の書類での理由で除外。"""
    first, tagged = None, False
    for doc in c.docs:
        if not c.has(doc, basis, p):
            continue
        ctx, memo = c.ctx(doc, basis, p, std, top_item), {}
        try:
            v = _eval(tree, ctx, memo)
        except Excluded as e:
            if e.reason == "regions_not_in_document":
                tagged = tagged or e.extra.get("tagged", False)
                continue
            if _uses_regions(tree):  # 海外売上比率＝その年の欄が載る最新の書類の判定で決める（読めなければ古い書類の表へ落ちない）
                raise
            if first is None or (first.reason == "period_not_in_document" and e.reason != "period_not_in_document"):
                first = e
            continue
        return v, memo, ctx.get("top_line_item")
    if first is None and _uses_regions(tree):
        raise Excluded("regions_not_in_document" if tagged else "regions_not_tagged", period=p)
    raise first or Excluded("input_not_disclosed", period=p)


def _uses_regions(tree) -> bool:
    return "overseas_sales_ratio" in repr(tree)


def _key_of(inp: dict) -> str:
    return inp["item"] if inp.get("item") else COMPANY_TOP


def _col(c: _Company, key: str) -> dict:
    """1 項目の索引のこの会社の分（問の間は c.cols に持ち続ける＝screen の _Values と同じ入れ物）。"""
    if key not in c.cols:
        c.cols[key] = _column(key)
    return c.cols[key].get(c.code, {})


def _restated(c: _Company, inp: dict, basis: str, std: str) -> list[dict]:
    rows = _col(c, _key_of(inp))
    src = rows.get((inp["doc_id"], basis, inp["period"]), ())
    dec = next((r[6] for r in src if r[1] == inp["element"]), None)
    val, out = Decimal(inp["value"]), []
    for doc in c.docs:
        if doc == inp["doc_id"]:
            continue
        for r in rows.get((doc, basis, inp["period"]), ()):
            same = r[1] == inp["element"] if not inp.get("item") else r[0] == std
            if same and abs(Decimal(r[2]) - val) > max(_unit(dec), _unit(r[6])):
                out.append({"term": inp["term"], "doc_id": doc, "value": r[2]})
                break
    return out


def _split(c: _Company, old: str, new: str, basis: str, std: str) -> bool:
    """2 つの書類の重なる期で、1 株当たりの項目の値だけが一定の比率でずれるか（株式分割・併合）。"""
    rs = []
    for k in PER_SHARE:
        rows = _col(c, k)
        for p in set(c.per.get((old, basis), ())) & set(c.per.get((new, basis), ())):
            a = next((Decimal(r[2]) for r in rows.get((old, basis, p), ()) if r[0] == std), None)
            b = next((Decimal(r[2]) for r in rows.get((new, basis, p), ()) if r[0] == std), None)
            if a and b:
                rs.append(a / b)
    if not rs or min(rs) <= 0:
        return False
    return max(rs) / min(rs) < _SPLIT_SPREAD and abs(min(rs) - 1) > _SPLIT_MIN


DEFAULT_YEARS, FILING_LAG_MONTHS = 13, 4
DEFAULT_YEARS_REGIONS = 11  # 地域別の欄は最古の書類（提出日で約 10 年前）の前期から＝海外売上比率の横断の既定は 11 年


def _shift(ym: str, months: int) -> str:
    n = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + months
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def default_window(as_of: str, years: int = DEFAULT_YEARS) -> tuple[str, str]:
    """横断の期間の既定＝基準日の 4 か月前（有報の提出期限は決算期末から 3 か月）までの 13 年＝決算月によらず全社で同じ 13 年。
    置き場で最も古い決算期は外れ値（2008-03 等）があり、基準日を終わりにすると未提出の決算月の会社が足りない扱いになる（2026-09-30 実装時）。"""
    to = _shift(as_of[:7], -FILING_LAG_MONTHS)
    return _shift(to, -12 * years + 1), to


def _series(c: _Company, tree, d: dict, *, basis, period_from, period_to, trim: bool = False) -> dict:
    ends = [doc for doc in c.docs if not period_to or c.current(doc) <= period_to]
    if not ends:
        raise Excluded("no_period_in_window")
    last = ends[0]  # 期間の最新の書類＝系列の会計基準と basis の既定を決める
    std = c.co["documents"][last]["accounting_standard"]
    t_last = c.current(last)
    b = basis or ("consolidated" if c.has(last, "consolidated", t_last) else "non_consolidated")
    years = [p for p in c.all_periods if (not period_from or p >= period_from) and p <= (period_to or t_last)]
    if not years:
        raise Excluded("no_period_in_window")
    if period_from and _months(years[0], period_from) >= 12:
        raise Excluded("insufficient_history", first_period=years[0])
    if period_to and _months(period_to, years[-1]) >= 12:
        raise Excluded("insufficient_history", last_period=years[-1])
    for p in years:  # その年の前の決算期（どの書類・basis でも）との間隔
        prev = [q for q in c.all_periods if q < p]
        if prev and _months(p, prev[-1]) != 12:
            raise Excluded("irregular_period", periods=[prev[-1], p])
    with_basis = [p for p in years if any(c.has(doc, b, p) for doc in c.docs)]
    if not with_basis:
        raise Excluded("no_consolidated_statements" if b == "consolidated" else "input_not_disclosed")
    if len(with_basis) != len(years):
        raise Excluded("basis_changed", basis=b, missing=[p for p in years if p not in with_basis][:5])
    rows, top_item = [], None
    for i, p in enumerate(reversed(years)):  # 新しい年から＝最上段の収益の項目を最新の年で決めて他の年に固定する
        try:
            v, memo, top_item = _year(c, tree, b, p, std, top_item)
        except Excluded as e:
            # 期間の指定が無い＝入力がそろう最古の年から。横断の既定の期間（trim）で切り詰めてよいのは期のずれ（全社で同じ 1 年）だけ＝
            # 地域別の欄の有無は会社で違う（IFRS のタグは古い書類に無い）＝切り詰めると年数の違う集約が並ぶ（2026-09-30 実装時）
            if i > 0 and (not period_from or (trim and e.reason == "period_not_in_document")) and e.reason in (
                    "period_not_in_document", "regions_not_in_document"):
                break
            if e.reason == "period_not_in_document":
                raise Excluded("insufficient_history", period=p, term=e.extra.get("term"))
            if e.reason == "regions_not_in_document":
                raise Excluded("regions_not_in_document", period=p)
            if e.reason == "top_line_changed" and _other_standard_only(c, b, p, std):
                raise Excluded("standard_changed", period=p, term="top_line")
            raise Excluded(e.reason, **{"period": p, **e.extra})
        inputs = [x[1] for _, x in sorted(memo.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
        restated = [r for inp in inputs if inp.get("element") for r in _restated(c, inp, b, std)]  # 海外売上比率は表のセル＝突き合わせない
        disclosed = d.get("disclosed") and len(inputs) == 1
        rows.append({"period": p, "value": inputs[0]["value"] if disclosed else _fmt(v), "_raw": v, "doc_id": inputs[0]["doc_id"],
                     "inputs": inputs, **({"restated": restated} if restated else {})})
    rows.reverse()
    if any(inp.get("item") in PER_SHARE for r in rows for inp in r["inputs"]):
        srcs = []
        for r in rows:
            if not srcs or srcs[-1] != r["doc_id"]:
                srcs.append(r["doc_id"])
        for old, new in zip(srcs, srcs[1:]):
            if _split(c, old, new, b, std):
                raise Excluded("stock_split", documents=[old, new])
    return {"basis": b, "accounting_standard": std, "top_line_item": top_item, "series": rows,
            "restated_years": [r["period"] for r in rows if "restated" in r]}


def _other_standard_only(c: _Company, basis: str, p: str, std: str) -> bool:
    """その年の最上段の収益（標準の項目）が、系列の会計基準ではない値しか無いか＝項目の変更ではなく基準の変更
    （ソニー＝新しい年は会社が定義した IFRS の項目・2020 年 3 月期以前は米国基準の売上高だけ）。"""
    rows = [r for k in TOP_LINE for doc in c.docs for r in _col(c, k).get((doc, basis, p), ())]
    return bool(rows) and all(r[0] != std for r in rows)


def _aggregate(rows: list[dict], agg: str, year_min, year_max) -> str:
    x = [r["_raw"] for r in rows]  # 丸める前の値（表示の value は 1e-12 で丸めてある）
    if len(x) < 2 and agg in ("cagr", "change", "ratio", "streak_up", "streak_down"):
        raise Excluded("insufficient_history", years=len(x))
    if agg == "change":
        return _fmt(x[-1] - x[0])
    if agg in ("ratio", "cagr"):
        if x[0] <= 0:
            raise Excluded("nonpositive_start", start=rows[0]["value"], period=rows[0]["period"])
        if agg == "ratio":
            return _fmt(x[-1] / x[0])
        if x[-1] < 0:
            raise Excluded("negative_end", end=rows[-1]["value"], period=rows[-1]["period"])
        return _fmt((x[-1] / x[0]) ** (Decimal(1) / (len(x) - 1)) - 1)
    if agg == "mean":
        return _fmt(sum(x) / len(x))
    if agg in ("min", "max"):
        return _fmt(min(x) if agg == "min" else max(x))
    if agg in ("streak_up", "streak_down"):
        k = 0
        for a, b in zip(reversed(x[:-1]), reversed(x[1:])):
            if (b > a) if agg == "streak_up" else (b < a):
                k += 1
            else:
                break
        return str(k)
    lo = None if year_min is None else Decimal(str(year_min))
    hi = None if year_max is None else Decimal(str(year_max))
    return str(sum(1 for y in x if (lo is None or y >= lo) and (hi is None or y <= hi)))


def trend_company(code: str, *, metric: str | None = None, expr: str | None = None, aggregate: str | None = None,
                  year_min=None, year_max=None, basis: str | None = None, period_from: str | None = None,
                  period_to: str | None = None, as_of: str | None = None, _parsed=None, _cols: dict | None = None,
                  _reg: dict | None = None, _trim: bool = False) -> dict:
    if _parsed is None:
        bad = _request(aggregate, year_min, year_max, period_from, period_to, metric)
        if bad:
            return bad
        if basis not in (None, "consolidated", "non_consolidated"):
            return _miss("bad_request", hint="basis は consolidated か non_consolidated")
        try:
            _parsed = _definition(metric, expr)
        except ExprError as e:
            return _miss(e.reason, **e.extra)
    _, tree, d = _parsed
    reg = _reg or store.registry()
    if code not in reg:
        return _miss("company_not_found")
    try:
        c = _Company(code, reg[code], _as_of(as_of), {} if _cols is None else _cols)
        s = _series(c, tree, d, basis=basis, period_from=period_from, period_to=period_to, trim=_trim)
        value = _aggregate(s["series"], aggregate, year_min, year_max) if aggregate else None
        for r in s["series"]:
            del r["_raw"]
        out = {"ok": True, **s}
        if aggregate:
            out.update(aggregate=aggregate, value=value)
        return out
    except Excluded as e:
        return {"ok": False, "reason": e.reason, **e.extra}


# ── 横断 ─────────────────────────────────────────────────
MAX_COMPANIES = 10


def _compact(series: list[dict], disclosed: bool) -> tuple[dict, dict]:
    """横断の行の系列を細い列の形に（返り値は利用側のモデルの文脈に入る＝各年に出典一式を付けると上位 20 社で約 100〜250KB、
    各年の辞書のままでも ROA で約 66KB だった・2026-10-01）。
    {"periods": [期], "values": [値], "doc_ids": [出所の書類], "inputs": {項目: [各年の開示値]}（開示値の条件は省く）,
     "restated": [{"period", "term", "doc_id", "value"}]}。要素は行に 1 回（最新の年の入力から）。"""
    out = {"periods": [y["period"] for y in series], "values": [y["value"] for y in series], "doc_ids": [y["doc_id"] for y in series]}
    if not disclosed:
        ins: dict[str, list] = {}
        for k, y in enumerate(series):
            for i in y["inputs"]:
                pairs = (("home", i["home_text"]), ("total", i["total_text"])) if "home_text" in i else ((i["term"], i["value"]),)
                for term, v in pairs:
                    ins.setdefault(term, [None] * len(series))[k] = v
        out["inputs"] = ins
    rest = [{"period": y["period"], "term": r["term"], "doc_id": r["doc_id"], "value": r["value"]} for y in series for r in y.get("restated", [])]
    if rest:
        out["restated"] = rest
    last = series[-1]["inputs"] if series else []
    elements = {i["term"]: (i.get("label") if i.get("company_defined") else None) or i.get("element") or "本邦と合計のセル（地域別の表）"
                for i in last}
    return out, elements


def _name(c: dict) -> str:
    return f"{c['aggregate']}:{c.get('metric') or c.get('expr')}"


def screen_trend(conditions: list[dict], *, order_by: str | None = None, order: str = "desc", industries: list[str] | None = None,
                 manufacturing: bool | None = None, basis: str | None = None, period_from: str | None = None,
                 period_to: str | None = None, limit: int = 20, as_of: str | None = None, companies: list[str] | None = None,
                 detail: bool = False) -> dict:
    if not isinstance(conditions, list) or not conditions or len(conditions) > 5:
        return _miss("bad_request", hint="conditions は 1〜5 個の {metric または expr, aggregate, min?, max?, year_min?, year_max?}")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        return _miss("bad_request", hint=f"limit は 1〜{MAX_LIMIT}")
    if order not in ("desc", "asc"):
        return _miss("bad_request", hint="order は desc か asc")
    if basis not in (None, "consolidated", "non_consolidated"):
        return _miss("bad_request", hint="basis は consolidated か non_consolidated（省くと会社ごとに期間の最新の年で連結を優先）")
    if industries is not None and (not isinstance(industries, list) or set(industries) - set(INDUSTRIES)):
        return _miss("bad_request", hint="industries は EDINET の提出者業種（list_metrics の industries）", industries=list(INDUSTRIES))
    if companies is not None and (not isinstance(companies, list) or not 1 <= len(companies) <= MAX_COMPANIES
                                  or not all(isinstance(x, str) for x in companies)):
        return _miss("bad_request", hint=f"companies は EDINET コードの配列（1〜{MAX_COMPANIES} 社＝find_company で同定してから）")
    if not isinstance(detail, bool):
        return _miss("bad_request", hint="detail は true か false")
    defs = []
    for c in conditions:
        if not isinstance(c, dict) or (("metric" in c) == ("expr" in c)):
            return _miss("bad_request", hint="条件は metric（検証済みの型・項目）か expr（式）のどちらか一方")
        if "aggregate" not in c:
            return _miss("bad_request", hint="時系列の条件は aggregate（集約）が要る＝1 年の条件は screen_companies", aggregates=AGGREGATES)
        for k in ("min", "max"):
            if k in c and not isinstance(c[k], (int, float)):
                return _miss("bad_request", hint="min／max は数（比率は 0.1 の形）")
        bad = _request(c["aggregate"], c.get("year_min"), c.get("year_max"), period_from, period_to, c.get("metric"))
        if bad:
            return bad
        try:
            parsed = _definition(c.get("metric"), c.get("expr"))
        except ExprError as e:
            un = [{"term": e.extra.get("term"), "level": e.reason, "count": None}] if e.reason in ("input_not_ingested", "unknown_item") else []
            return _miss(e.reason, condition=c, unavailable=un, **e.extra)
        defs.append((_name(c), parsed, c))
    names = [n for n, _, _ in defs]
    order_by = order_by or names[0]
    if order_by not in names:
        return _miss("bad_request", hint="order_by は条件の名前（<aggregate>:<metric または expr>）のどれか", names=names)
    a = _as_of(as_of)
    # 期間の既定＝全社で同じ 13 年（default_window）。会社ごとの最古からにすると、年数の違う集約を並べることになる（2〜3 年の会社が上位に並んだ）。
    # 期のずれのある式は入力がそろう最古の年から（trim）
    trim = period_from is None
    if period_from is None or period_to is None:
        regions = any(_uses_regions(p[1]) for _, p, _ in defs)
        d_from, d_to = default_window(a, DEFAULT_YEARS_REGIONS if regions else DEFAULT_YEARS)
        period_from, period_to = period_from or d_from, period_to or d_to
    cols = {}  # 読んだ項目の索引をこの問の間は手放さない
    reg = store.registry()
    defs_d = {n: p[2] for n, p, _ in defs}
    rows, excluded, population = [], {n: {} for n in names}, 0
    for code in (companies if companies is not None else reg):
        if code not in reg:
            continue
        ind = industry_of(code) or {}
        if industries is not None and ind.get("industry") not in industries:
            continue
        if manufacturing is not None and ind.get("manufacturing") is not manufacturing:
            continue
        population += 1
        vals, series, rest, ok = {}, {}, {}, True
        for name, parsed, c in defs:
            r = trend_company(code, aggregate=c["aggregate"], year_min=c.get("year_min"), year_max=c.get("year_max"), basis=basis,
                              period_from=period_from, period_to=period_to, as_of=a, _parsed=parsed, _cols=cols, _reg=reg, _trim=trim)
            if not r.get("ok"):
                ok = False
                e = excluded[name].setdefault(r["reason"], {"count": 0, "examples": [],
                                                            "note": TREND_REASONS.get(r["reason"]) or REASON_NOTE.get(r["reason"], "")})
                e["count"] += 1
                if len(e["examples"]) < 5:
                    e["examples"].append(reg[code]["name"])
                break
            v = Decimal(r["value"])
            if ("min" in c and v < Decimal(str(c["min"]))) or ("max" in c and v > Decimal(str(c["max"]))):
                ok = False
                break
            vals[name], series[name], rest[name] = r["value"], r["series"], r["restated_years"]
        if ok:
            row = {"company": _brief(code), "values": vals, "series": series, "restated_years": rest}
            if not detail:
                els = {}
                for name, _, c in defs:
                    row["series"][name], els[name] = _compact(series[name], bool(defs_d[name].get("disclosed")))
                row["elements"] = els
            rows.append(row)
    rows.sort(key=lambda r: Decimal(r["values"][order_by]), reverse=order == "desc")
    ratio = {"ratio_note": RATIO_NOTE} if any(_is_ratio(c.get("metric") or c.get("expr"), p[2]) for _, p, c in defs) else {}
    return {"found": True, **ratio, "matched": len(rows), "rows": rows[:limit], "order_by": order_by, "order": order,
            "definitions": [{"name": n, "aggregate": c["aggregate"], "aggregate_note": AGGREGATES[c["aggregate"]], **p[2]} for n, p, c in defs],
            "excluded": {n: e for n, e in excluded.items() if e}, "unavailable": [], "population": population, "as_of": a,
            "period_from": period_from, "period_to": period_to, "industry_note": MANUFACTURING_NOTE, "note": NOTE,
            "series_note": ("series は細い列の形＝periods・values・doc_ids（出所の書類）は同じ並び・inputs は項目ごとの各年の開示値・"
                            "restated は組み替えのあった年と他の書類の値。要素は行の elements。"
                            "入力の出典一式（要素・context・単位）は companies に EDINET コードを指定して detail=true で引き直す。"
                            "書類の URL＝https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?<doc_id>") if not detail else None,
            "not_ingested": {k: label for k, (label, _) in NOT_INGESTED.items()},
            "license": {"grade": "○", "terms": "公共データ利用規約（PDL1.0）＝出典の明記と加工の明記（集約は加工）"}}
