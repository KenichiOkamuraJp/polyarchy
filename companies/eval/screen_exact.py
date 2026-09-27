"""第 1c 便（横断検索）の判定＝式エンジンの規則・無い入力の 3 段・検証済みの型の値・横断の問（docs/第1c便_計画.md §3）。

  python -m companies.eval.screen_exact
問：
  screen.jsonl（値の正例・make_screen_candidates.py が作る）・screen_manual.jsonl（形の複雑な表の正例＝人手）
  screen_fail_closed.jsonl（会社ごとの除外の理由・式の受付の誤り）・screen_queries.jsonl（横断の問＝絞り込み・並べ方・無い入力の記録）
参照する契約（core/screen.py）：
  evaluate_company(code, *, metric=None, expr=None, as_of, basis=None)
    -> {"ok": True, "value": str, "period", "doc_id", "inputs": [{"term", "item"?, "element"?, "value"?, "home_text"?, "total_text"?, "statement"?}]}
     | {"ok": False, "reason": str}（会社ごとの除外）| {"found": False, "reason": str}（式の受付の誤り）
  screen_companies(conditions, *, order_by=None, order="desc", industries=None, manufacturing=None, basis=None,
                   period_from=None, period_to=None, limit=20, as_of=None)
    -> {"found": True, "rows": [{"company": {..., "industry", "manufacturing"}, "values": {条件の名前: str}, "top_line_item"?}],
        "excluded": {条件の名前: {理由: {"count", "examples"}}}, "judged_by_statement": {条件の名前: {"count"}},
        "unavailable": [{"term", "level", "count"}], ...} | {"found": False, "reason": str}
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal

from companies.eval.exact_match import EVAL

TOL = Decimal("1e-12")


def _load(name: str) -> list[dict]:
    p = EVAL / name
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def _close(a: str, b: str) -> bool:
    return abs(Decimal(a) - Decimal(b)) <= TOL


def check_value(q: dict, ev) -> str | None:
    r = ev(q["company"]["edinet_code"], metric=q["metric"], as_of=q["as_of"], basis=q.get("basis"))
    if not r.get("ok"):
        return f"計算されなかった: {r.get('reason')}"
    if r["doc_id"] != q["doc_id"] or r["period"] != q["period"]:
        return f"書類・決算期が違う: {r['doc_id']} {r['period']}（期待 {q['doc_id']} {q['period']}）"
    e = q["expected"]
    got = {i["term"]: i for i in r["inputs"]}
    if q["kind"] == "value":
        for term, want in e["inputs"].items():
            g = got.get(term)
            if not g or g.get("value") != want["value"] or g.get("element") != want["element"]:
                return f"入力 {term} が違う: {g}（期待 {want['element']} {want['value']}）"
        if len(got) != len(e["inputs"]):
            return f"入力の数が違う: {sorted(got)}（期待 {sorted(e['inputs'])}）"
    elif q["kind"] == "overseas":
        g = got.get("overseas_sales_ratio[t]") or {}
        if g.get("home_text") != e["home_text"] or g.get("total_text") != e["total_text"]:
            return f"本邦と合計のセルが違う: {g.get('home_text')}／{g.get('total_text')}（期待 {e['home_text']}／{e['total_text']}）"
    elif q["kind"] == "overseas_statement":
        g = got.get("overseas_sales_ratio[t]") or {}
        if g.get("statement") != e["statement"]:
            return f"会社の文による判定が違う: {g.get('statement')}（期待 {e['statement']}）"
    if not _close(r["value"], e["value"]):
        return f"値が違う: {r['value']}（期待 {e['value']}）"
    return None


def check_negative(q: dict, ev) -> str | None:
    if q["kind"] == "request":
        r = ev("E02144", metric=q.get("metric"), expr=q.get("expr"), as_of="2026-09-27")  # 式の受付の誤りは会社によらない
        if r.get("found") is not False or r.get("reason") != q["reason"]:
            return f"受付の誤りにならない: {r.get('reason')}（期待 {q['reason']}）"
        return None
    r = ev(q["company"]["edinet_code"], metric=q.get("metric"), expr=q.get("expr"), as_of=q["as_of"])
    if r.get("ok"):
        return f"除外されずに値を返した: {r.get('value')}（期待 {q['reason']}）"
    if r.get("reason") != q["reason"]:
        return f"除外の理由が違う: {r.get('reason')}（期待 {q['reason']}）"
    return None


def _key(c: dict) -> str:
    return c.get("metric") or c.get("expr")


def check_query(q: dict, sc) -> str | None:
    kw = {k: q[k] for k in ("order_by", "order", "industries", "manufacturing", "basis", "period_from", "period_to", "limit", "as_of") if k in q}
    r = sc(q["conditions"], **kw)
    e = q["expect"]
    if "reason" in e:
        if r.get("found") is not False or r.get("reason") != e["reason"]:
            return f"受付の誤りにならない: {r.get('reason')}（期待 {e['reason']}）"
    elif not r.get("found"):
        return f"found=false: {r.get('reason')}"
    rows = r.get("rows", [])
    if e.get("rows_satisfy"):
        for row in rows:
            for c in q["conditions"]:
                v = row["values"].get(_key(c))
                if v is None:
                    return f"{row['company']['name']}: 条件 {_key(c)} の値が無い"
                if ("min" in c and Decimal(v) < Decimal(str(c["min"]))) or ("max" in c and Decimal(v) > Decimal(str(c["max"]))):
                    return f"{row['company']['name']}: 条件 {_key(c)} を満たさない値 {v}"
    if e.get("no_manufacturing") and any(row["company"]["manufacturing"] for row in rows):
        return "非製造業の絞り込みに製造業の会社が入った"
    if "industries_only" in e and any(row["company"]["industry"] not in e["industries_only"] for row in rows):
        return "業種の絞り込みの外の会社が入った"
    if "top_line_items_only" in e and any(row.get("top_line_item") not in e["top_line_items_only"] for row in rows):
        return f"最上段の収益の項目が期待と違う: {sorted({row.get('top_line_item') for row in rows})}"
    if "note_includes" in e and e["note_includes"] not in r.get("note", ""):
        return f"返り値の note に「{e['note_includes']}」が無い"
    if "max_rows" in e and len(rows) > e["max_rows"]:
        return f"行が多い: {len(rows)}"
    if e.get("sorted"):
        vals = [Decimal(row["values"][q["order_by"]]) for row in rows]
        if vals != sorted(vals, reverse=q.get("order", "desc") == "desc"):
            return "並び順が違う"
    for reason in e.get("excluded_include", []):
        if not any(reason in d for d in r["excluded"].values()):
            return f"除外の理由 {reason} が返らない: {r['excluded']}"[:300]
    for cond, reasons in (e.get("excluded_count_min") or {}).items():
        for reason, n in reasons.items():
            got = ((r["excluded"].get(cond) or {}).get(reason) or {}).get("count", 0)
            if got < n:
                return f"除外の理由 {cond}／{reason} の件数が少ない: {got}（下限 {n}）"
    if "judged_by_statement_min" in e:
        n = sum(d["count"] for d in r.get("judged_by_statement", {}).values())
        if n < e["judged_by_statement_min"]:
            return f"会社の文による判定の件数が少ない: {n}"
    for u in e.get("unavailable_include", []):
        if not any(x["term"] == u["term"] and x["level"] == u["level"] for x in r.get("unavailable", [])):
            return f"無い入力の記録が無い: {u}（返り値 {r.get('unavailable')}）"
    if not rows and "rows_satisfy" in e and not e.get("allow_empty"):
        return "行が 0"
    return None


def main() -> int:
    pos = _load("screen.jsonl") + _load("screen_manual.jsonl")
    neg, qs = _load("screen_fail_closed.jsonl"), _load("screen_queries.jsonl")
    try:
        from companies.core.screen import evaluate_company, screen_companies
    except ImportError:
        print(f"FAIL: 横断検索（companies.core.screen）が未実装＝正例 0/{len(pos)}・負例 0/{len(neg)}・横断の問 0/{len(qs)}")
        return 1
    fails = []
    for items, check, fn in ((pos, check_value, evaluate_company), (neg, check_negative, evaluate_company), (qs, check_query, screen_companies)):
        for q in items:
            try:
                err = check(q, fn)
            except Exception as ex:  # 1 問の例外で全体を止めない
                err = f"例外: {ex!r}"[:300]
            if err:
                fails.append((q["id"], err))
    for i, err in fails[:40]:
        print(f"  FAIL {i}: {err}")
    print(f"{'PASS' if not fails else 'FAIL'}: 横断検索 正例 {len(pos)}・負例 {len(neg)}・横断の問 {len(qs)}・失敗 {len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
