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
import sys
from decimal import Decimal, getcontext
from functools import lru_cache

from companies.core import store
from companies.core.industries import INDUSTRIES, MANUFACTURING, MANUFACTURING_NOTE, industry_of
from companies.core.items import ITEMS, RATIO_ITEMS, RATIO_NOTE, TOP_LINE, TOP_LINE_LABEL, standard_of
from companies.core.regions import _has_table, _is_value_table

getcontext().prec = 28
MAX_OFFSET = 4          # 5 期推移＝t〜t-4
MAX_LIMIT = 100
STALE_MONTHS = 18       # 基準日から 18 か月より前の決算期は「古い」
MAX_EXPR_LEN, MAX_TERMS = 300, 20
Q = Decimal("1e-12")

PSEUDO = {
    "top_line": "最上段の収益（会社が開示している売上高・売上収益・営業収益・経常収益 等のうち最初に見つかった項目＝行ごとに明示）。"
                "標準の項目が無い会社は、経営指標の表で会社が定義した収益の項目（候補が 1 つのときだけ＝例 トヨタの営業収益・完成工事高・保険収益）を"
                "要素とラベルつきで使う（候補が複数なら除外＝top_line_ambiguous）",
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
    "roe_change": ("roe[t] - roe[t-1]", "ROE の変化（差）",
                   "当期の ROE − 前期の ROE（どちらも会社が書いた開示値・同じ書類の 5 期推移。差＝0.01 が 1 ポイント）"),
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
    # 語彙（単社の lookup_company_facts）にはあるが、「主要な経営指標等の推移」に営業利益を書く会社は無い（米国基準の会社も書いていない＝
    # 2026-09-27 実測で最新期に値のある会社 0 社）＝横断検索では gross_profit と同じく式ごと計算しない（全社を回して 0 件にしない）
    "operating_profit": ("営業利益", "損益計算書"),
}
# 赤字の年は「－」と書いて開示しないことが多い項目（2026-09-27 実測＝ROE の開示が無い 233 社のうち 229 社が赤字の年）。
# 開示が無く同じ書類・同じ期の当期純利益がマイナスなら、理由を not_disclosed_loss_year に分ける（値は計算して埋めない）
LOSS_BLANK = {"roe"}
REASON_NOTE = {
    "input_not_disclosed": "入力の項目をこの会社はこの書類・決算期・basis で開示していない（近い項目では埋めない）",
    "not_disclosed_loss_year": "赤字の年で、会社がこの比率を開示していない（「－」と書く会社が多い）＝低い順の並びにはこの会社が出ない（値は計算して埋めない）",
    "input_not_ingested": "入力の項目が未収録（会社は開示しているが本サービスが取り込んでいない＝改善候補として記録）",
    "period_not_in_document": "式の期のずれが、この書類の 5 期推移の外",
    "irregular_period": "決算期の長さが違う期（変則決算）が入力に入る",
    "standard_changed": "入力の期に、書類が宣言する会計基準の値が無い（基準の違う値を混ぜない）",
    "nonpositive_denominator": "分母が 0 以下",
    "top_line_changed": "最上段の収益の項目が期で変わる（例＝売上高→売上収益）",
    "top_line_ambiguous": "標準の最上段の収益が無く、会社が定義した収益の項目が経営指標の表に複数ある（例＝保険料等収入と資産運用収益）＝どれが最上段か決めない",
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
    return sorted({*ITEMS, *PSEUDO, *DERIVED, *PRESETS, *NOT_INGESTED})


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
    if name in NOT_INGESTED:
        label, where = NOT_INGESTED[name]
        raise ExprError("input_not_ingested", term=name,
                        hint=f"{label}（{name}）は未収録（出所＝{where}）。式ごと計算しない。未収録の項目は改善候補として記録した＝list_metrics の not_ingested")
    if name in ITEMS or name in PSEUDO:
        return ("term", name, off)
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
# 会社が定義した最上段の収益の候補＝経営指標の表の拡張要素（接尾辞で表を限る）で、会社のラベルが収益の語を含むもの。
# 標準の最上段の収益が無い会社だけが使う（2026-09-27 実測＝52 社・候補 1 つが 47 社〔トヨタ 営業収益・完成工事高・保険収益 等〕・複数が 5 社）
COMPANY_TOP = "_company_top_line"
_TABLE_SUFFIX = ("SummaryOfBusinessResults", "KeyFinancialData")


def _company_top_candidate(f: dict) -> bool:
    return (not f["element"].startswith("jpcrp_cor:") and f["element"].endswith(_TABLE_SUFFIX) and f["unit"] == "JPY"
            and bool(f.get("label")) and any(w in f["label"] for w in TOP_LINE_LABEL))


# 索引は 2 種類（第 1d 便＝書類が約 9 倍になる・2026-09-28 本人指示「現状のスナップショットは極力早く・時系列は時間がかかってよい」）
#   ① 現状（期の指定なし）＝各社の最新の書類だけ・全項目を常駐（_latest.json）＝遡りの前と同じ速さ（2 回目以降 0.05 秒前後）
#   ② 過去の期・時系列（period_from／period_to）＝全書類の項目ごとのファイル（<項目>.json）を問ごとに使う項目だけ読む
#      （全項目を常駐させると全量で約 2.7GB の見込み＝2026-09-28 実測 66KB／書類）。1 つの問の間は読んだ項目を手放さない
# 索引は取込の側（作業用 PC）で作り、値の置き場と一緒に運ぶ
# ファイルは 1 行 1 社の JSON Lines（[EDINET コード, 中身]）＝1 つの JSON にすると読み込みの一時的なメモリが 4 倍になった
# （全量の現状の索引＝保持 153MB に対し一時 605MB・2026-09-30 実測）
#   store/screen_index/_periods.jsonl  中身＝{"<書類>|<basis>": [決算期（新しい順）]}
#   store/screen_index/_fingerprint.json  鮮度の照合＝{EDINET コード: [書類の一覧, 値のファイルの大きさ]}
#   store/screen_index/_latest.jsonl   中身＝{"<書類>|<basis>": {決算期: {項目: [[基準, 要素, 値, 単位, context, ラベル, decimals]]}}}（最新の書類だけ）
#   store/screen_index/<項目>.jsonl    中身＝{"<書類>|<basis>": {決算期: [[基準, 要素, 値, 単位, context, ラベル, decimals]]}}
#   （ラベルは会社が定義した最上段の収益の候補だけ・他は null。decimals は時系列のつなぎ目で丸めの差を修正と数えないため＝第 1e 便）
INDEX_DIR = store.STORE / "screen_index"
INDEX_VERSION = 2  # 2＝行に decimals を足した（2026-09-30・第 1e 便）。形式を変えたら上げる＝古い索引は index_status が検出する
_BASES = ("consolidated", "non_consolidated")


def build_index() -> dict:
    """値の置き場（facts/）から横断検索の索引を作り直す（取込のあと＝ingest.edinet が最後に呼ぶ）。"""
    periods, cols, fp = {}, {}, {}
    for code in sorted(store.registry()):
        path = store.STORE / "facts" / f"{code}.json"
        if not path.exists():
            continue
        per = {}
        for f in json.loads(path.read_text()):
            if f["dims"]:
                continue
            k = _EL_KEY.get(f["element"])
            if k:
                row = [standard_of(f["element"]), f["element"], f["value"], f["unit"], f["context"], None, f.get("decimals")]
            elif _company_top_candidate(f):
                k, row = COMPANY_TOP, [None, f["element"], f["value"], f["unit"], f["context"], f["label"], f.get("decimals")]
            else:
                continue
            db = f"{f['doc_id']}|{f['basis']}"
            per.setdefault(db, set()).add(f["period"])
            cols.setdefault(k, {}).setdefault(code, {}).setdefault(db, {}).setdefault(f["period"], []).append(row)
        periods[code] = {db: sorted(ps, reverse=True) for db, ps in per.items()}
        fp[code] = _fingerprint(code)
    tmp = INDEX_DIR.with_name(INDEX_DIR.name + ".tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    for old in tmp.glob("*.json*"):
        old.unlink()
    _write_lines(tmp / "_periods.jsonl", periods)
    (tmp / "_fingerprint.json").write_text(json.dumps({"__version__": INDEX_VERSION, **fp}, ensure_ascii=False, separators=(",", ":")))
    for k, col in cols.items():
        _write_lines(tmp / f"{k}.jsonl", col)
    latest = {}
    for code in periods:
        docs = store.registry()[code]["documents"]
        newest = max(docs, key=lambda d: (docs[d]["submitted"], d))
        for k, col in cols.items():
            for db, ps in col.get(code, {}).items():
                if db.split("|")[0] == newest:
                    for p, rows in ps.items():
                        latest.setdefault(code, {}).setdefault(db, {}).setdefault(p, {})[k] = rows
    _write_lines(tmp / "_latest.jsonl", latest)
    if INDEX_DIR.exists():
        for old in INDEX_DIR.glob("*.json*"):
            old.unlink()
        INDEX_DIR.rmdir()
    tmp.rename(INDEX_DIR)
    _periods.cache_clear(); _column.cache_clear(); _latest.cache_clear(); _OVERSEAS.clear()
    return {"companies": len(periods), "items": len(cols)}


def _write_lines(path, data: dict) -> None:
    with path.open("w") as fh:
        for code, v in data.items():
            fh.write(json.dumps([code, v], ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_lines(path):
    with path.open() as fh:
        for line in fh:
            yield json.loads(line)


def _fingerprint(code: str) -> list:
    """鮮度の照合＝登録簿の書類の一覧と値のファイルの大きさ（取り込み直すと変わる）。"""
    path = store.STORE / "facts" / f"{code}.json"
    return [sorted(store.registry()[code]["documents"]), path.stat().st_size if path.exists() else 0]


def index_status() -> list[str]:
    """索引が値の置き場と合っているか（ゲートで照合する）。合わない会社の EDINET コード（最大 20）。"""
    path = INDEX_DIR / "_fingerprint.json"
    if not path.exists():
        return ["索引が無い（python -m companies.ingest.edinet --build-index）"]
    fp = json.loads(path.read_text())
    if fp.get("__version__") != INDEX_VERSION:
        return [f"索引の形式が古い（版 {fp.get('__version__', 1)}→{INDEX_VERSION}・python -m companies.ingest.edinet --build-index）"]
    bad = [c for c in store.registry() if (store.STORE / "facts" / f"{c}.json").exists() and fp.get(c) != _fingerprint(c)]
    return bad[:20] + ([f"…ほか {len(bad) - 20} 社"] if len(bad) > 20 else [])


@lru_cache(maxsize=1)
def _periods() -> dict:
    """{EDINET コード: {(書類, basis): (決算期, …新しい順)}}。"""
    I = sys.intern
    return {I(code): {tuple(I(x) for x in db.split("|")): tuple(I(p) for p in ps) for db, ps in per.items()}
            for code, per in _read_lines(INDEX_DIR / "_periods.jsonl")}


@lru_cache(maxsize=1)
def _latest() -> dict:
    """① 現状＝{EDINET コード: {(書類, basis): {決算期: {項目: 値の組}}}}（最新の書類だけ・常駐）。"""
    I = sys.intern
    out = {}
    for code, per in _read_lines(INDEX_DIR / "_latest.jsonl"):
        m = out[I(code)] = {}
        for db, ps in per.items():
            doc, basis = db.split("|")
            m[(I(doc), I(basis))] = {I(p): {I(k): _rows(rows) for k, rows in items.items()} for p, items in ps.items()}
    return out


def _rows(rows: list) -> tuple:
    I = sys.intern
    return tuple(tuple(x if i == 2 or x is None else I(x) for i, x in enumerate(r)) for r in rows)


@lru_cache(maxsize=8)
def _column(key: str) -> dict:
    """1 項目の索引＝{EDINET コード: {(書類, basis, 決算期): ((基準, 要素, 値, 単位, context, ラベル, decimals), …)}}。
    値の文字列以外（書類・期・要素・単位・context・ラベル）は共有の文字列にする＝全量（約 4 万書類）で 1 項目 数十 MB に収める。"""
    path = INDEX_DIR / f"{key}.jsonl"
    if not path.exists():
        return {}
    I = sys.intern
    out = {}
    for code, per in _read_lines(path):
        m = out[I(code)] = {}
        for db, ps in per.items():
            doc, basis = db.split("|")
            for p, rows in ps.items():
                m[(I(doc), I(basis), I(p))] = _rows(rows)
    return out


class _Period:
    """② の ctx["values"][決算期]＝.get(項目) で、その書類・basis・期の値の組を返す。項目の索引は問ごとの cols に持つ
    （1 つの問の間は手放さない＝最上段の収益だけで 9 項目を見る・LRU で途中に追い出すと読み直しで 10 倍以上遅くなった＝2026-09-28 実測）。"""
    __slots__ = ("code", "key", "cols")

    def __init__(self, code: str, doc: str, basis: str, period: str, cols: dict):
        self.code, self.key, self.cols = code, (doc, basis, period), cols

    def get(self, item: str, default=None):
        col = self.cols.get(item)
        if col is None:
            col = self.cols[item] = _column(item)
        rows = col.get(self.code, {}).get(self.key)
        return rows if rows else default

    def __getitem__(self, item: str):
        return self.get(item, ())


class _Values:
    __slots__ = ("code", "doc", "basis", "cols")

    def __init__(self, code: str, doc: str, basis: str, cols: dict):
        self.code, self.doc, self.basis, self.cols = code, doc, basis, cols

    def __getitem__(self, period: str) -> _Period:
        return _Period(self.code, self.doc, self.basis, period, self.cols)


class _LatestValues:
    """① の ctx["values"]＝最新の書類の索引（常駐）をそのまま引く。"""
    __slots__ = ("per",)

    def __init__(self, per: dict):
        self.per = per

    def __getitem__(self, period: str) -> dict:
        return self.per.get(period, {})


def _months(a: str, b: str) -> int:
    return (int(a[:4]) - int(b[:4])) * 12 + int(a[5:7]) - int(b[5:7])


class Excluded(Exception):
    def __init__(self, reason: str, **extra):
        super().__init__(reason)
        self.reason, self.extra = reason, extra


def _context(code: str, basis: str | None, as_of: str, period_from: str | None, period_to: str | None, cols: dict | None = None):
    """書類を選ぶ＝期の指定が無ければ最新の書類・あれば当期がその範囲に入る最新の書類。連結の有無はその書類の当期で決める
    （会社の最新の宣言ではない＝連結をやめた・始めた会社がある・2026-09-28）。"""
    co = store.registry()[code]
    per = _periods().get(code, {})
    docs = sorted(((co["documents"][d]["submitted"], d) for d in co["documents"] if any((d, b) in per for b in _BASES)), reverse=True)
    for _, doc in docs:
        t = max(per[(doc, b)][0] for b in _BASES if (doc, b) in per)  # 書類の当期
        if (period_from and t < period_from) or (period_to and t > period_to):
            continue
        con = t in per.get((doc, "consolidated"), ())
        if basis == "consolidated" and not con:
            raise Excluded("no_consolidated_statements")
        if not (period_from or period_to) and _months(as_of[:7], t) > STALE_MONTHS:
            raise Excluded("stale_period", period=t)
        b = basis or ("consolidated" if con else "non_consolidated")
        if (doc, b) not in per:
            break
        window = period_from or period_to
        vals = _Values(code, doc, b, {} if cols is None else cols) if window else _LatestValues(_latest().get(code, {}).get((doc, b), {}))
        return {"code": code, "co": co, "doc": doc, "basis": b, "periods": list(per[(doc, b)]), "values": vals,
                "standard": co["documents"][doc]["accounting_standard"]}
    raise Excluded("no_period_in_window" if (period_from or period_to) else "input_not_disclosed")


def _period(ctx, key: str, off: int) -> str:
    ps = ctx["periods"]
    if off >= len(ps):
        raise Excluded("period_not_in_document", term=f"{key}[t-{off}]")
    if off + 1 < len(ps) and _months(ps[off], ps[off + 1]) != 12:
        raise Excluded("irregular_period", periods=[ps[off + 1], ps[off]])
    return ps[off]


def _item(ctx, key: str, off: int) -> dict:
    p = _period(ctx, key, off)
    cands = ctx["values"][p].get(key, [])
    mine = [c for c in cands if c[0] == ctx["standard"]]
    if not mine:
        if cands:
            raise Excluded("standard_changed", term=f"{key}[t-{off}]" if off else f"{key}[t]", period=p)
        if key in LOSS_BLANK:
            bl = ctx["values"][p].get("profit_attributable_to_owners" if ctx["basis"] == "consolidated" else "net_income") or []
            mine_bl = [c for c in bl if c[0] == ctx["standard"]]
            if mine_bl and Decimal(mine_bl[0][2]) < 0:
                raise Excluded("not_disclosed_loss_year", term=key, period=p, bottom_line=mine_bl[0][2])
        raise Excluded("input_not_disclosed", term=key)
    std, el, value, unit, context = mine[0][:5]
    return {"item": key, "label": ITEMS[key][0], "element": el, "period": p, "value": value, "unit": unit, "context": context,
            "doc_id": ctx["doc"]}


def _has(ctx, key: str, off: int) -> bool:
    ps = ctx["periods"]
    return off < len(ps) and bool(ctx["values"][ps[off]].get(key))


def _company_top(ctx, off: int, fixed: str | None) -> dict:
    """会社が定義した最上段の収益（標準の項目が無い会社だけ）。候補が 1 つのときだけ使い、期をまたいで同じ要素に限る。"""
    p = _period(ctx, "top_line", off)
    cands = {c[1]: c for c in ctx["values"][p].get(COMPANY_TOP, [])}
    if fixed:
        if fixed not in cands:
            raise Excluded("top_line_changed" if cands or any(_has(ctx, k, off) for k in TOP_LINE) else "input_not_disclosed",
                           term=f"top_line[t-{off}]" if off else "top_line[t]", items=[fixed])
        cands = {fixed: cands[fixed]}
    if not cands:
        raise Excluded("input_not_disclosed", term="top_line")
    if len(cands) > 1:
        raise Excluded("top_line_ambiguous", candidates=[{"element": c[1], "label": c[5]} for c in cands.values()])
    _, el, value, unit, context, label = next(iter(cands.values()))[:6]
    return {"item": None, "label": label, "element": el, "company_defined": True, "period": p, "value": value, "unit": unit,
            "context": context, "doc_id": ctx["doc"]}


def _top_line(ctx, off: int) -> dict:
    fixed = ctx.get("top_line_item")
    if fixed and fixed not in TOP_LINE:  # 会社が定義した項目で決めた会社＝他の期も同じ要素だけ
        return _company_top(ctx, off, fixed)
    g = None
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
        if others or (fixed and _has(ctx, COMPANY_TOP, off)):
            raise Excluded("top_line_changed", items=[fixed])
        if fixed:
            raise Excluded("input_not_disclosed", term="top_line")
        g = _company_top(ctx, off, None)
    ctx["top_line_item"] = g["item"] or g["element"]
    return g


def _resolve(ctx, name: str, off: int, memo: dict) -> tuple[Decimal, dict]:
    if (name, off) in memo:
        return memo[(name, off)]
    if name == "top_line":
        g = _top_line(ctx, off)
    elif name == "bottom_line":
        g = _item(ctx, "profit_attributable_to_owners" if ctx["basis"] == "consolidated" else "net_income", off)
    elif name == "overseas_sales_ratio":
        g = _overseas_cached(ctx)
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


_OVERSEAS: dict = {}  # (会社, 書類, basis) → 値 か 除外（地域別の欄は書類ごとに決まる＝問をまたいで使い回す）


def _overseas_cached(ctx) -> dict:
    """地域別の欄のファイルは全書類ぶん（第 1d 便で約 9 倍）＝全社の欄を問のたびに読み直すと現状の問が 3 倍遅くなった（2026-09-30 実測）。"""
    key = (ctx["code"], ctx["doc"], ctx["basis"])
    if key not in _OVERSEAS:
        try:
            _OVERSEAS[key] = _overseas(ctx)
        except Excluded as e:
            _OVERSEAS[key] = e
    r = _OVERSEAS[key]
    if isinstance(r, Excluded):
        raise r
    return dict(r)


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
    try:  # 横断の top_line と同じ最上段の収益（会社が定義した項目を含む）。ctx の固定は変えない（写しで引く）
        top = _top_line({**ctx, "top_line_item": None}, 0)
    except Excluded:
        top = None
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
        if metric in ITEMS or metric in PSEUDO:
            label = ITEMS[metric][0] if metric in ITEMS else PSEUDO[metric]
            return metric, parse(metric), {"name": metric, "label": label, "note": "開示値（計算しない）", "verified": True, "disclosed": True}
        raise ExprError("unknown_item", term=metric, candidates=difflib.get_close_matches(metric, [*PRESETS, *DERIVED, *ITEMS, *PSEUDO], n=5, cutoff=0.6),
                        hint="metric は検証済みの型・派生項目・項目のキー・仮の項目（list_metrics）。自由な式は expr で渡す")
    return expr, parse(expr), {"name": None, "expr": expr, "verified": False,
                               "note": "利用者の式（未検証）＝エンジンの共通の規則（同じ書類・期のずれは 5 期推移・欠けた入力と分母 0 以下は除外）だけで計算した"}


def _as_of(as_of: str | None) -> str:
    return as_of or dt.date.today().isoformat()


def _fmt(v: Decimal) -> str:
    s = format(v.quantize(Q), "f") if abs(v) < 10**15 else format(v, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def evaluate_company(code: str, *, metric: str | None = None, expr: str | None = None, as_of: str | None = None,
                     basis: str | None = None, period_from: str | None = None, period_to: str | None = None, _parsed=None,
                     _cols: dict | None = None) -> dict:
    try:
        _, tree, d = _parsed or _definition(metric, expr)
    except ExprError as e:
        return {"found": False, "reason": e.reason, **e.extra}
    try:
        ctx = _context(code, basis, _as_of(as_of), period_from, period_to, _cols)
        memo = {}
        value = _eval(tree, ctx, memo)
    except Excluded as e:
        return {"ok": False, "reason": e.reason, **e.extra}
    inputs = [v[1] for k, v in sorted(memo.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    # 開示値の条件（計算しない）は開示の文字列のまま返す（2026-09-27 staging＝−8.400 を −8.4 と返し、利用側のモデルが「この社だけ桁が不自然」と読んだ）
    disclosed = d.get("disclosed") and len(inputs) == 1
    return {"ok": True, "value": inputs[0]["value"] if disclosed else _fmt(value),
            "period": ctx["periods"][0], "doc_id": ctx["doc"], "basis": ctx["basis"], "accounting_standard": ctx["standard"],
            "top_line_item": ctx.get("top_line_item"), "inputs": inputs}


# ── 横断 ─────────────────────────────────────────────────
def _brief(code: str) -> dict:
    co = store.registry()[code]
    ind = industry_of(code) or {}
    return {"edinet_code": code, "sec_code": co.get("sec_code"), "name": co["name"], "accounting_standard": co.get("accounting_standard"),
            "industry": ind.get("industry"), "manufacturing": ind.get("manufacturing"), "listing": ind.get("listing")}


def _is_ratio(name: str, d: dict) -> bool:
    """1＝100% の形の値か（比率の項目・検証済みの型・海外売上比率・割り算か比率の項目を含む式）＝ratio_note を付ける範囲"""
    if d.get("name"):
        return name in RATIO_ITEMS or name in PRESETS or name in DERIVED
    terms = set(re.findall(r"[A-Za-z_]\w*", name))
    return "/" in name or bool(terms & (RATIO_ITEMS | set(PRESETS) | set(DERIVED)))


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
    cols = {}  # ② 過去の期の問＝読んだ項目の索引をこの問の間は手放さない
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
            r = evaluate_company(code, as_of=a, basis=basis, period_from=period_from, period_to=period_to, _parsed=(name, tree, d),
                                 _cols=cols)
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
    ratio = {"ratio_note": RATIO_NOTE} if any(_is_ratio(n, d) for n, _, d, _ in defs) else {}
    return {"found": True, **ratio, "matched": len(rows), "rows": rows[:limit], "order_by": order_by, "order": order,
            "definitions": [d[2] for d in defs], "excluded": {n: e for n, e in excluded.items() if e}, "judged_by_statement": judged,
            "unavailable": unavailable, "population": population, "as_of": a,
            "industry_note": MANUFACTURING_NOTE,
            "note": "values は派生値（検証済みの型・利用者の式）または開示値。行ごとの inputs が入力の開示値と出典。"
                    "比較できない会社は excluded に理由と件数（黙って落とさない）。決算期は会社ごとに違う＝行の period を見る。",
            # 未収録の項目＝使える語の外（2026-09-27 staging＝返り値に案内が無い問で、利用側のモデルが「営業利益で並べ直せます」と提案した）
            "not_ingested": {k: label for k, (label, _) in NOT_INGESTED.items()},
            "not_ingested_note": "未収録の項目（横断検索の条件に使えない＝使うと input_not_ingested）。並べ直しの提案に使わない",
            "license": {"grade": "○", "terms": "公共データ利用規約（PDL1.0）＝出典の明記と加工の明記（派生値は加工）"}}


def list_metrics() -> dict:
    return {"items": {k: v[0] for k, v in ITEMS.items() if k not in NOT_INGESTED}, "pseudo_items": PSEUDO, "derived_items": DERIVED,
            "presets": {k: {"expr": e, "label": label, "note": note} for k, (e, label, note) in PRESETS.items()},
            "not_ingested": {k: {"label": label, "source": where} for k, (label, where) in NOT_INGESTED.items()},
            "industries": list(INDUSTRIES), "manufacturing": sorted(MANUFACTURING),
            "industry_note": MANUFACTURING_NOTE, "exclusion_reasons": REASON_NOTE,
            "grammar": f"式＝項目のキー・数・+ - * /・括弧・期のずれ x[t]〜x[t-{MAX_OFFSET}]（同じ書類の 5 期推移）。関数・文字列・比較は使えない。"
                       "比率は 0.1 の形（10% ではない）。派生項目は当期だけ", "ratio_note": RATIO_NOTE}
