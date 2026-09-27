"""横断検索（第 1c 便）＝項目の語彙に閉じた式エンジン＋検証済みの型＝docs/第1c便_計画.md §2-5・§2-6。

式＝項目のキー（items.ITEMS）・仮の項目（top_line・bottom_line）・派生項目（overseas_sales_ratio）・検証済みの型（PRESETS）と、
四則演算・単項マイナス・数の定数・期のずれ（x[t]・x[t-1]〜x[t-4]）だけ。関数・属性・文字列・比較・条件分岐は受けない。
正しさは式ごとではなくエンジンの共通の規則で担保する：
  ① 入力はすべて同じ書類・同じ決算期の並び・同じ basis（書類が宣言する会計基準の値）
  ② x[t-n] は同じ書類の 5 期推移から（遡及修正の前後を混ぜない）
  ③ 入力が欠けた会社は除外の理由へ（近い項目・別の基準の値で埋めない）
  ④ 分母が 0 以下・決算期の長さが違う期（変則決算）・会計基準が変わった期は除外の理由へ
  ⑤ top_line（最上段の収益）は会社が開示している項目を行ごとに明示（期をまたいで項目が変わる会社は除外）
無い入力は 3 段で返す（unknown_item＝語彙に無い語／input_not_ingested＝未収録／input_not_disclosed＝会社によって開示が無い）。
派生値は計算するが、行ごとに式と入力の開示値（出典つき）を添える。単社の参照層（lookup.py・segments.py・regions.py）は派生値を返さないまま。
"""
from __future__ import annotations

import ast
import datetime as dt
import difflib
import json
import re
from decimal import Decimal, getcontext
from functools import lru_cache

from companies.core import store
from companies.core.industries import INDUSTRIES, MANUFACTURING, MANUFACTURING_NOTE, industry_of
from companies.core.items import ITEMS, TOP_LINE, standard_of
from companies.core.regions import _has_table, _is_value_table

getcontext().prec = 28
MAX_OFFSET = 4          # 5 期推移＝t〜t-4
MAX_LIMIT = 100
STALE_MONTHS = 18       # 基準日から 18 か月より前の決算期は「古い」
MAX_EXPR_LEN, MAX_TERMS = 300, 20
Q = Decimal("1e-12")

PSEUDO = {
    "top_line": "最上段の収益（会社が開示している売上高・売上収益・営業収益・経常収益 等のうち最初に見つかった項目＝行ごとに明示）",
    "bottom_line": "親会社株主に帰属する当期純利益（連結）／当期純利益（連結を作成していない会社＝単体）",
}
DERIVED = {
    "overseas_sales_ratio": "海外売上比率＝（合計 − 本邦）÷ 合計。地域別の表の本邦と合計の 2 セルから（当期だけ・合計はタグの最上段の収益と一致したセルだけ）",
}
# 検証済みの型（名前つきの指標）＝式・固有の注・評価問を持つ。ドッグフーディングと捕捉ログで育てる（計画 §2-5）
PRESETS = {
    "roa": ("bottom_line / ((total_assets[t] + total_assets[t-1]) / 2)", "ROA（総資産利益率）",
            "当期純利益（親会社株主帰属・連結なしは単体）÷ 総資産の期首と期末の平均（期首＝同じ書類の前期末）"),
    "net_margin": ("bottom_line / top_line", "当期純利益率", "当期純利益 ÷ 最上段の収益"),
    "ordinary_margin": ("ordinary_profit / top_line", "経常利益率", "経常利益 ÷ 最上段の収益（IFRS・米国基準には経常利益が無い＝除外）"),
    "roa_change": ("roa[t] - roa[t-1]", "ROA の変化（差）", "当期の ROA − 前期の ROA（差＝0.01 が 1 ポイント）"),
    "top_line_growth": ("top_line[t] / top_line[t-1] - 1", "売上の前年比（伸び率）", "当期の最上段の収益 ÷ 前期 − 1（項目が期で変わる会社は除外）"),
}
# 未収録の項目の目録＝改善の backlog を兼ねる（list_metrics で見える・input_not_ingested の件数で需要を測る）
NOT_INGESTED = {
    "gross_profit": ("売上総利益", "損益計算書"),
    "cost_of_sales": ("売上原価", "損益計算書"),
    "sga": ("販売費及び一般管理費", "損益計算書"),
    "depreciation": ("減価償却費", "キャッシュ・フロー計算書・セグメント情報"),
    "inventories": ("棚卸資産", "貸借対照表"),
    "current_assets": ("流動資産", "貸借対照表"),
    "current_liabilities": ("流動負債", "貸借対照表"),
    "interest_bearing_debt": ("有利子負債", "貸借対照表の借入金・社債・リース債務 等"),
    "research_and_development_expenses": ("研究開発費（会社全体）", "研究開発活動・損益計算書の注記"),
}
# 語彙にはあるが、大半の会社で値の置き場に取り込めていない項目（会社は別の場所で開示している＝「開示が無い」ではなく「未収録」）
PARTIAL = {
    "operating_profit": "営業利益は「主要な経営指標等の推移」に無い（米国基準の会社だけ）＝損益計算書の営業利益は未収録",
}
REASON_NOTE = {
    "input_not_disclosed": "入力の項目をこの会社はこの書類・決算期・basis で開示していない（近い項目では埋めない）",
    "input_not_ingested": "入力の項目が未収録（会社は開示しているが本サービスが取り込んでいない＝改善候補として記録）",
    "period_not_in_document": "式の期のずれが、この書類の 5 期推移の外",
    "irregular_period": "決算期の長さが違う期（変則決算）が入力に入る",
    "standard_changed": "入力の期に、書類が宣言する会計基準の値が無い（基準の違う値を混ぜない）",
    "nonpositive_denominator": "分母が 0 以下",
    "top_line_changed": "最上段の収益の項目が期で変わる（例＝売上高→売上収益）",
    "stale_period": f"最新の決算期が基準日から {STALE_MONTHS} か月より前",
    "no_period_in_window": "指定の期末の範囲に決算期が無い",
    "no_consolidated_statements": "連結財務諸表を作成していない会社（basis=non_consolidated で引く）",
    "regions_not_tagged": "地域別の欄が無い（米国基準・地域の要素でタグ付けしていない書類）",
    "regions_omitted": "地域別の欄に表が無く、会社の文から比率も判定できない（他の注記に記載・把握が困難 等）",
    "overseas_statement_only": "表が無く「本邦が 90% 超」と会社の文で書いている＝海外 10% 未満とだけ分かる（比率は出さない）",
    "home_not_in_table": "地域別の表に本邦の区分が無い＝海外 100% の可能性も、本邦が他地域と一体の可能性もある（比率は出さない・原典で確認）",
    "home_or_total_not_identified": "地域別の表で本邦と合計のセルを特定できない（本邦が他地域と一体・合計がタグの収益と一致しない 等）",
}


class ExprError(Exception):
    def __init__(self, reason: str, **extra):
        super().__init__(reason)
        self.reason, self.extra = reason, extra


# ── 式の受付 ─────────────────────────────────────────────
def _offset(sl) -> int:
    if isinstance(sl, ast.Name) and sl.id == "t":
        return 0
    if (isinstance(sl, ast.BinOp) and isinstance(sl.op, ast.Sub) and isinstance(sl.left, ast.Name) and sl.left.id == "t"
            and isinstance(sl.right, ast.Constant) and type(sl.right.value) is int and 1 <= sl.right.value <= MAX_OFFSET):
        return sl.right.value
    raise ExprError("bad_expression", hint=f"期のずれは [t]・[t-1]〜[t-{MAX_OFFSET}] だけ（未来の期・5 期推移の外は受けない）")


def _vocab() -> list[str]:
    return sorted([*ITEMS, *PSEUDO, *DERIVED, *PRESETS, *NOT_INGESTED])


def _tree(node, shift: int, depth: int = 0):
    if depth > 6:
        raise ExprError("bad_expression", hint="検証済みの型の入れ子が深すぎる")
    if isinstance(node, ast.Expression):
        return _tree(node.body, shift, depth)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        return ("bin", type(node.op).__name__, _tree(node.left, shift, depth), _tree(node.right, shift, depth))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return ("neg", _tree(node.operand, shift, depth))
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return ("num", Decimal(str(node.value)))
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        return _term(node.value.id, _offset(node.slice) + shift, depth)
    if isinstance(node, ast.Name):
        return _term(node.id, shift, depth)
    raise ExprError("bad_expression", hint="使えるのは項目のキー・数・四則演算（+ - * /）・括弧・期のずれ（x[t-1]）だけ（関数・文字列・比較は受けない）")


def _term(name: str, off: int, depth: int):
    if off > MAX_OFFSET:
        raise ExprError("bad_expression", hint=f"期のずれが 5 期推移の外（{name} を t-{off} で使う）")
    if name in PRESETS:
        return _tree(ast.parse(PRESETS[name][0], mode="eval"), off, depth + 1)
    if name in DERIVED:
        if off:
            raise ExprError("bad_expression", hint=f"派生項目 {name} は当期（[t]）だけ")
        return ("term", name, 0)
    if name in ITEMS or name in PSEUDO:
        return ("term", name, off)
    if name in NOT_INGESTED:
        label, where = NOT_INGESTED[name]
        raise ExprError("input_not_ingested", term=name,
                        hint=f"{label}（{name}）は未収録（出所＝{where}）。式ごと計算しない。未収録の項目は改善候補として記録した＝list_metrics の not_ingested")
    raise ExprError("unknown_item", term=name, candidates=difflib.get_close_matches(name, _vocab(), n=5, cutoff=0.6),
                    hint="語彙に無い語。使える項目・型は list_metrics で引く")


def parse(expr: str):
    """式を受け付けて木にする。受け付けない式は ExprError（reason＝bad_expression／unknown_item／input_not_ingested）。"""
    if not isinstance(expr, str) or not expr.strip() or len(expr) > MAX_EXPR_LEN:
        raise ExprError("bad_expression", hint=f"式は 1〜{MAX_EXPR_LEN} 字の文字列")
    try:
        node = ast.parse(expr.strip(), mode="eval")
    except SyntaxError:
        raise ExprError("bad_expression", hint="式として読めない（四則演算・括弧・x[t-1] の形）") from None
    tree = _tree(node, 0)
    if len(_terms(tree)) > MAX_TERMS:
        raise ExprError("bad_expression", hint=f"入力の項目は {MAX_TERMS} まで")
    return tree


def _terms(tree) -> list[tuple[str, int]]:
    if tree[0] == "term":
        return [(tree[1], tree[2])]
    if tree[0] == "num":
        return []
    return [x for sub in tree[1:] if isinstance(sub, tuple) for x in _terms(sub)]


# ── 値の索引（初回に 1 回・標準の項目だけ）────────────────────────
_EL_KEY = {f"jpcrp_cor:{el}": k for k, (_, els) in ITEMS.items() for el in els}


@lru_cache(maxsize=1)
def _index() -> dict:
    """{EDINET コード: {(doc, basis): {period: {item: [(standard, element, value, unit, context)]}}}}。"""
    idx = {}
    for code in store.registry():
        fp = store.STORE / "facts" / f"{code}.json"
        if not fp.exists():
            continue
        per = {}
        for f in json.loads(fp.read_text()):
            k = _EL_KEY.get(f["element"])
            if k and not f["dims"]:
                per.setdefault((f["doc_id"], f["basis"]), {}).setdefault(f["period"], {}).setdefault(k, []).append(
                    (standard_of(f["element"]), f["element"], f["value"], f["unit"], f["context"]))
        idx[code] = per
    return idx


def _months(a: str, b: str) -> int:
    return (int(a[:4]) - int(b[:4])) * 12 + int(a[5:7]) - int(b[5:7])


class Excluded(Exception):
    def __init__(self, reason: str, **extra):
        super().__init__(reason)
        self.reason, self.extra = reason, extra


def _context(code: str, basis: str | None, as_of: str, period_from: str | None, period_to: str | None):
    co = store.registry()[code]
    if basis == "consolidated" and not co["consolidated"]:
        raise Excluded("no_consolidated_statements")
    basis = basis or ("consolidated" if co["consolidated"] else "non_consolidated")
    per = _index().get(code, {})
    docs = sorted(((co["documents"][d]["submitted"], d) for d in co["documents"] if (d, basis) in per), reverse=True)
    for _, doc in docs:
        ps = sorted(per[(doc, basis)], reverse=True)
        t = ps[0]
        if (period_from and t < period_from) or (period_to and t > period_to):
            continue
        if not (period_from or period_to) and _months(as_of[:7], t) > STALE_MONTHS:
            raise Excluded("stale_period", period=t)
        return {"code": code, "co": co, "doc": doc, "basis": basis, "periods": ps, "values": per[(doc, basis)],
                "standard": co["documents"][doc]["accounting_standard"]}
    raise Excluded("no_period_in_window" if (period_from or period_to) else "input_not_disclosed")


def _item(ctx, key: str, off: int) -> dict:
    ps = ctx["periods"]
    if off >= len(ps):
        raise Excluded("period_not_in_document", term=f"{key}[t-{off}]")
    if off + 1 < len(ps) and _months(ps[off], ps[off + 1]) != 12:
        raise Excluded("irregular_period", periods=[ps[off + 1], ps[off]])
    p = ps[off]
    cands = ctx["values"][p].get(key, [])
    mine = [c for c in cands if c[0] == ctx["standard"]]
    if not mine:
        if cands:
            raise Excluded("standard_changed", term=f"{key}[t-{off}]" if off else f"{key}[t]", period=p)
        raise Excluded("input_not_ingested" if key in PARTIAL else "input_not_disclosed", term=key)
    std, el, value, unit, context = mine[0]
    return {"item": key, "label": ITEMS[key][0], "element": el, "period": p, "value": value, "unit": unit, "context": context,
            "doc_id": ctx["doc"]}


def _has(ctx, key: str, off: int) -> bool:
    ps = ctx["periods"]
    return off < len(ps) and bool(ctx["values"][ps[off]].get(key))


def _resolve(ctx, name: str, off: int, memo: dict) -> tuple[Decimal, dict]:
    if (name, off) in memo:
        return memo[(name, off)]
    if name == "top_line":
        fixed = ctx.get("top_line_item")
        g, changed = None, False
        for k in ([fixed] if fixed else TOP_LINE):
            try:
                g = _item(ctx, k, off)
                break
            except Excluded as e:
                if e.reason in ("period_not_in_document", "irregular_period") or (fixed and e.reason == "standard_changed"):
                    raise
        if g is None:
            others = [c for k in TOP_LINE if k != fixed and _has(ctx, k, off) for c in ctx["values"][ctx["periods"][off]][k]] if fixed else []
            if others and all(c[0] != ctx["standard"] for c in others):
                raise Excluded("standard_changed", term=f"top_line[t-{off}]", period=ctx["periods"][off])
            if others:
                raise Excluded("top_line_changed", items=[fixed])
            raise Excluded("input_not_disclosed", term="top_line")
        ctx["top_line_item"] = g["item"]
    elif name == "bottom_line":
        g = _item(ctx, "profit_attributable_to_owners" if ctx["basis"] == "consolidated" else "net_income", off)
    elif name == "overseas_sales_ratio":
        g = _overseas(ctx)
    else:
        g = _item(ctx, name, off)
    term = f"{name}[t-{off}]" if off else f"{name}[t]"
    out = (Decimal(g["value"]), {"term": term, **g})
    memo[(name, off)] = out
    return out


def _eval(tree, ctx, memo) -> Decimal:
    kind = tree[0]
    if kind == "num":
        return tree[1]
    if kind == "term":
        return _resolve(ctx, tree[1], tree[2], memo)[0]
    if kind == "neg":
        return -_eval(tree[1], ctx, memo)
    op, a, b = tree[1], _eval(tree[2], ctx, memo), _eval(tree[3], ctx, memo)
    if op == "Div":
        if b <= 0:
            raise Excluded("nonpositive_denominator")
        return a / b
    return a + b if op == "Add" else a - b if op == "Sub" else a * b


# ── 派生項目：海外売上比率（地域別の表の本邦と合計の 2 セル）─────────────────
HOME = re.compile(r"^(日本|本邦|国内|日本国内)([（(][^）)]*[）)])?$")
TOTAL = re.compile(r"^(合計|計|総計|連結|連結合計|連結計|合計額|売上収益|売上高|連結売上高|連結売上収益|外部顧客への売上高|外部顧客への売上収益)([（(][^）)]*[）)])?$")
NOTE_MARK = re.compile(r"[（(]?注[）)]?\d*|※\d*|\*\d*|[（(]\d[）)]$")
NONE_OUTSIDE = re.compile(r"本邦以外.{0,30}(ない|無い|ありません|存在しない|該当事項はありません)|(すべて|全て|全額)本邦|本邦のみ")
OVER_90 = re.compile(r"(90|９０)\s*[%％]")
UNITS = (Decimal(10**6), Decimal(10**3), Decimal(1))


def _label(text: str) -> str:
    return NOTE_MARK.sub("", re.sub(r"\s", "", text))


def _num(text: str) -> Decimal | None:
    s = re.sub(r"\s", "", text).replace("，", ",")
    s = re.sub(r"(百万円|千円|円)$", "", s)
    return Decimal(s.replace(",", "")) if re.fullmatch(r"\d{1,3}(,\d{3})+|\d+", s) else None


def _grid(t: dict) -> list[list]:
    """結合セルを位置に割り付けた格子（照合のためだけ＝返り値の表は展開しない）。格子の各位置＝(行・列の元のセルの番号, セル)。"""
    grid: dict[tuple[int, int], tuple] = {}
    for r, row in enumerate(t["rows"]):
        c = 0
        for k, cell in enumerate(row):
            while (r, c) in grid:
                c += 1
            for dr in range(cell.get("rowspan", 1)):
                for dc in range(cell.get("colspan", 1)):
                    grid[(r + dr, c + dc)] = ((r, k), cell)
            c += cell.get("colspan", 1)
    nr = 1 + max((r for r, _ in grid), default=-1)
    nc = 1 + max((c for _, c in grid), default=-1)
    return [[grid.get((r, c)) for c in range(nc)] for r in range(nr)]


def _pairs(t: dict) -> list[tuple]:
    """本邦のセルと合計のセルの組の候補（地域が列＝見出しの下の各行／地域が行＝見出しの右の各列）。どれが当期の売上かは呼び出し側が
    タグの最上段の収益との一致で決める（比率の行・前期の列・非流動資産の表はここで落ちる）。"""
    g = _grid(t)
    width = max((len(row) for row in g), default=0)
    cells = [(r, c, x) for r, row in enumerate(g) for c, x in enumerate(row) if x]
    homes = [(r, c) for r, c, x in cells if HOME.match(_label(x[1]["text"]))]
    totals = [(r, c) for r, c, x in cells if TOTAL.match(_label(x[1]["text"]))]
    out = []

    def add(a, b):
        if a and b and a[0] != b[0] and _num(a[1]["text"]) is not None and _num(b[1]["text"]) is not None:
            out.append((a, b))

    for hr, hc in homes:
        for tr, tc in totals:
            if hc != tc:
                for r in range(max(hr, tr) + 1, len(g)):
                    add(g[r][hc], g[r][tc])
            if hr != tr:
                for c in range(max(hc, tc) + 1, width):
                    add(g[hr][c], g[tr][c])
    return out


def _overseas(ctx) -> dict:
    data = store.regions_of(ctx["code"])
    want = "geographic_areas_ifrs" if ctx["standard"] == "IFRS" else "revenue"
    t = ctx["periods"][0]
    secs = [s for s in data["sections"] if s["doc_id"] == ctx["doc"] and s["basis"] == ctx["basis"] and s["section"] == want
            and s["context"].startswith("CurrentYear") and (s["period"] == t or want == "geographic_areas_ifrs")]
    if not secs:
        raise Excluded("regions_not_tagged")
    sec = secs[0]
    notes = [x["text"] for x in sec["content"] if x["type"] == "text" and re.search(r"基礎|分類|所在|仕向", x["text"])]
    if not _has_table(sec):
        text = " ".join(x.get("text", "") for x in sec["content"] if x["type"] == "text") + " " + " ".join(
            c["text"] for x in sec["content"] if x["type"] == "table" for row in x["rows"] for c in row)
        if NONE_OUTSIDE.search(text):
            return {"value": "0", "statement": "none_outside_japan", "quote": text.strip()[:200], "section": want, "doc_id": ctx["doc"],
                    "note": "表が無く、本邦以外に売上が無いと会社の文で書いている＝0（会社の文による）"}
        if OVER_90.search(text) and "本邦" in text:
            raise Excluded("overseas_statement_only", quote=text.strip()[:200], upper="0.1")
        raise Excluded("regions_omitted", quote=text.strip()[:200])
    top = None
    for k in TOP_LINE:
        try:
            top = _item(ctx, k, 0)
            break
        except Excluded:
            continue
    if top is None:
        raise Excluded("home_or_total_not_identified", why="照合に使う最上段の収益（タグ）が無い")
    tagged = Decimal(top["value"])
    found = {}
    for ti, tb in enumerate(x for x in sec["content"] if x["type"] == "table" and _is_value_table(x)):
        for a, b in _pairs(tb):
            tv = _num(b[1]["text"])
            for u in UNITS:
                if tv and abs(tv * u - tagged) < u:
                    found[(ti, a[0], b[0])] = (a[1]["text"], b[1]["text"], u, ti)
                    break
    vals = {(v[0], v[1]) for v in found.values()}
    tables = [x for x in sec["content"] if x["type"] == "table" and _is_value_table(x)]
    if not vals and not any(HOME.match(_label(c["text"])) for x in tables for row in x["rows"] for c in row):
        raise Excluded("home_not_in_table", cells=[c["text"] for x in tables for row in x["rows"] for c in row if c["text"].strip()][:12])
    if len(vals) != 1:
        raise Excluded("home_or_total_not_identified", why="本邦と合計の組が見つからない" if not vals else f"組が {len(vals)} 通り")
    home, total, unit, ti = next(iter(found.values()))
    hv, tv = _num(home), _num(total)
    return {"value": str((tv - hv) / tv), "home_text": home, "total_text": total, "unit_multiplier": str(unit), "table_index": ti,
            "section": want, "doc_id": ctx["doc"], "basis_notes": notes[:3],
            "total_check": {"item": top["item"], "element": top["element"], "value": top["value"],
                            "note": "合計のセル×単位＝同じ書類のタグの付いた最上段の収益（2 経路で一致したセルだけを使う）"}}


# ── 会社ごとの計算 ─────────────────────────────────────────────
def _definition(metric: str | None, expr: str | None) -> tuple[str, object, dict]:
    if metric is not None:
        if metric in PRESETS:
            e, label, note = PRESETS[metric]
            return metric, parse(metric), {"name": metric, "label": label, "expr": e, "note": note, "verified": True}
        if metric in DERIVED:
            return metric, parse(metric), {"name": metric, "label": "海外売上比率", "note": DERIVED[metric], "verified": True}
        if metric in ITEMS:
            return metric, parse(metric), {"name": metric, "label": ITEMS[metric][0], "note": "開示値（計算しない）", "verified": True, "disclosed": True}
        raise ExprError("unknown_item", term=metric, candidates=difflib.get_close_matches(metric, [*PRESETS, *DERIVED, *ITEMS], n=5, cutoff=0.6),
                        hint="metric は検証済みの型・派生項目・項目のキー（list_metrics）。自由な式は expr で渡す")
    return expr, parse(expr), {"name": None, "expr": expr, "verified": False,
                               "note": "利用者の式（未検証）＝エンジンの共通の規則（同じ書類・期のずれは 5 期推移・欠けた入力と分母 0 以下は除外）だけで計算した"}


def _as_of(as_of: str | None) -> str:
    return as_of or dt.date.today().isoformat()


def _fmt(v: Decimal) -> str:
    s = format(v.quantize(Q), "f") if abs(v) < 10**15 else format(v, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def evaluate_company(code: str, *, metric: str | None = None, expr: str | None = None, as_of: str | None = None,
                     basis: str | None = None, period_from: str | None = None, period_to: str | None = None, _parsed=None) -> dict:
    try:
        _, tree, _ = _parsed or _definition(metric, expr)
    except ExprError as e:
        return {"found": False, "reason": e.reason, **e.extra}
    try:
        ctx = _context(code, basis, _as_of(as_of), period_from, period_to)
        memo = {}
        value = _eval(tree, ctx, memo)
    except Excluded as e:
        return {"ok": False, "reason": e.reason, **e.extra}
    inputs = [v[1] for k, v in sorted(memo.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    return {"ok": True, "value": _fmt(value),
            "period": ctx["periods"][0], "doc_id": ctx["doc"], "basis": ctx["basis"], "accounting_standard": ctx["standard"],
            "top_line_item": ctx.get("top_line_item"), "inputs": inputs}


# ── 横断 ─────────────────────────────────────────────────
def _brief(code: str) -> dict:
    co = store.registry()[code]
    ind = industry_of(code) or {}
    return {"edinet_code": code, "sec_code": co.get("sec_code"), "name": co["name"], "accounting_standard": co.get("accounting_standard"),
            "industry": ind.get("industry"), "manufacturing": ind.get("manufacturing"), "listing": ind.get("listing")}


def _miss(reason: str, **extra) -> dict:
    return {"found": False, "reason": reason, **extra}


def screen_companies(conditions: list[dict], *, order_by: str | None = None, order: str = "desc", industries: list[str] | None = None,
                     manufacturing: bool | None = None, basis: str | None = None, period_from: str | None = None,
                     period_to: str | None = None, limit: int = 20, as_of: str | None = None) -> dict:
    if not isinstance(conditions, list) or not conditions or len(conditions) > 5:
        return _miss("bad_request", hint="conditions は 1〜5 個の {metric または expr, min?, max?}")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        return _miss("bad_request", hint=f"limit は 1〜{MAX_LIMIT}")
    if order not in ("desc", "asc"):
        return _miss("bad_request", hint="order は desc か asc")
    if basis not in (None, "consolidated", "non_consolidated"):
        return _miss("bad_request", hint="basis は consolidated か non_consolidated（省くと会社ごとに連結を優先）")
    if industries is not None and (not isinstance(industries, list) or set(industries) - set(INDUSTRIES)):
        return _miss("bad_request", hint="industries は EDINET の提出者業種（東証 33 業種・list_metrics の industries）", industries=list(INDUSTRIES))
    for p in (period_from, period_to):
        if p is not None and not re.fullmatch(r"\d{4}-\d{2}", p):
            return _miss("bad_period", hint="period_from／period_to は決算期末の YYYY-MM")
    defs, unavailable = [], []
    for c in conditions:
        if not isinstance(c, dict) or (("metric" in c) == ("expr" in c)):
            return _miss("bad_request", hint="条件は metric（検証済みの型・項目）か expr（式）のどちらか一方")
        for k in ("min", "max"):
            if k in c and not isinstance(c[k], (int, float)):
                return _miss("bad_request", hint="min／max は数（比率は 0.1 の形）")
        try:
            name, tree, d = _definition(c.get("metric"), c.get("expr"))
        except ExprError as e:
            if e.reason in ("input_not_ingested", "unknown_item"):
                unavailable.append({"term": e.extra.get("term"), "level": e.reason, "count": None})
            return _miss(e.reason, condition=c, unavailable=unavailable, **e.extra)
        defs.append((name, tree, d, c))
    names = [d[0] for d in defs]
    order_by = order_by or names[0]
    if order_by not in names:
        return _miss("bad_request", hint="order_by は条件の metric か expr のどれか（並べる値も条件に入れる）")
    a = _as_of(as_of)
    rows, excluded, judged, lack = [], {n: {} for n in names}, {}, {}
    population = 0
    for code in store.registry():
        ind = industry_of(code) or {}
        if industries is not None and ind.get("industry") not in industries:
            continue
        if manufacturing is not None and ind.get("manufacturing") is not manufacturing:
            continue
        population += 1
        vals, ins, ok, top_item, meta = {}, {}, True, None, None
        for name, tree, d, c in defs:
            r = evaluate_company(code, as_of=a, basis=basis, period_from=period_from, period_to=period_to, _parsed=(name, tree, d))
            if not r.get("ok"):
                ok = False
                reason = r["reason"]
                if reason == "overseas_statement_only" and c.get("min") is not None and c["min"] >= 0.1:
                    judged.setdefault(name, {"count": 0, "note": "会社の文で本邦 90% 超（海外 10% 未満）＝下限を満たさないと確定"})["count"] += 1
                    continue
                e = excluded[name].setdefault(reason, {"count": 0, "examples": [], "note": REASON_NOTE.get(reason, "")})
                e["count"] += 1
                if len(e["examples"]) < 5:
                    e["examples"].append(store.registry()[code]["name"])
                if reason in ("input_not_ingested", "input_not_disclosed") and r.get("term"):
                    lack[(r["term"], reason)] = lack.get((r["term"], reason), 0) + 1
                continue
            v = Decimal(r["value"])
            if ("min" in c and v < Decimal(str(c["min"]))) or ("max" in c and v > Decimal(str(c["max"]))):
                ok = False
                continue
            vals[name], ins[name] = r["value"], r["inputs"]
            top_item = top_item or r.get("top_line_item")
            meta = meta or r
        if ok:
            rows.append({"company": _brief(code), "period": meta["period"], "doc_id": meta["doc_id"], "basis": meta["basis"],
                         "accounting_standard": meta["accounting_standard"], "top_line_item": top_item, "values": vals, "inputs": ins,
                         "source": {"provider": "EDINET", "doc_type": "有価証券報告書", "doc_id": meta["doc_id"],
                                    "url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{meta['doc_id']}"}})
    rows.sort(key=lambda r: Decimal(r["values"][order_by]), reverse=order == "desc")
    unavailable += [{"term": t, "level": lv, "count": n} for (t, lv), n in sorted(lack.items(), key=lambda kv: -kv[1])]
    return {"found": True, "matched": len(rows), "rows": rows[:limit], "order_by": order_by, "order": order,
            "definitions": [d[2] for d in defs], "excluded": {n: e for n, e in excluded.items() if e}, "judged_by_statement": judged,
            "unavailable": unavailable, "population": population, "as_of": a,
            "industry_note": MANUFACTURING_NOTE,
            "note": "values は派生値（検証済みの型・利用者の式）または開示値。行ごとの inputs が入力の開示値と出典。"
                    "比較できない会社は excluded に理由と件数（黙って落とさない）。決算期は会社ごとに違う＝行の period を見る",
            "license": {"grade": "○", "terms": "公共データ利用規約（PDL1.0）＝出典の明記と加工の明記（派生値は加工）"}}


def list_metrics() -> dict:
    return {"items": {k: v[0] for k, v in ITEMS.items()}, "pseudo_items": PSEUDO, "derived_items": DERIVED,
            "presets": {k: {"expr": e, "label": label, "note": note} for k, (e, label, note) in PRESETS.items()},
            "not_ingested": {k: {"label": label, "source": where} for k, (label, where) in NOT_INGESTED.items()},
            "partially_ingested": PARTIAL, "industries": list(INDUSTRIES), "manufacturing": sorted(MANUFACTURING),
            "industry_note": MANUFACTURING_NOTE, "exclusion_reasons": REASON_NOTE,
            "grammar": f"式＝項目のキー・数・+ - * /・括弧・期のずれ x[t]〜x[t-{MAX_OFFSET}]（同じ書類の 5 期推移）。関数・文字列・比較は使えない。"
                       "比率は 0.1 の形（10% ではない）。派生項目は当期だけ"}
