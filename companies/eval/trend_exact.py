"""第 1e 便（時系列の横断検索）の判定＝系列の出所（案 B）・つなぎ目の restated・集約・系列が途切れる会社の除外・横断の問
（docs/第1e便_計画.md §2・§3）。

  python -m companies.eval.trend_exact
問：
  trend.jsonl（系列と集約の正例）・trend_fail_closed.jsonl（会社ごとの除外）＝make_trend_candidates.py が作る
  trend_queries.jsonl（横断の問・受付の誤り＝人手）
参照する契約（core/trend.py・未実装のあいだは全問 FAIL）：
  trend_company(code, *, metric=None, expr=None, aggregate=None, year_min=None, year_max=None, basis=None,
                period_from=None, period_to=None, as_of=None)
    -> {"ok": True, "basis", "accounting_standard", "top_line_item",
        "series": [{"period", "value": str, "doc_id", "inputs": [{"term", "item"?, "element", "value", "doc_id", …}],
                    "restated"?: [{"term", "doc_id", "value"}]}],   # 年の古い順
        "restated_years": [決算期 …], "aggregate"?: str, "value"?: str}   # aggregate を指定したときだけ value（派生値）
     | {"ok": False, "reason": str, …}（会社ごとの除外）| {"found": False, "reason": str, …}（受付の誤り）
  各年の値＝その年の入力がすべて載る書類のうち提出日が最新の書類の値（案 B＝単社の参照の既定と同じ）。
  期間の既定＝trend_company はその会社の収録の最古〜最新・screen_trend は基準日の 4 か月前までの 13 年（全社で同じ 13 年＝年数の違う集約を並べない）。
  期のずれのある式（ROA の前期の総資産 等）は、入力がそろう最古の年から（指定した期間の最初の年の入力がそろわなければ insufficient_history）。
  restated＝出所の書類と同じ期・同じ項目・同じ会計基準の値が違う他の書類（表示単位の粗い方の 1 単位以内の差は丸め＝数えない）。
  stock_split＝1 株当たりの項目で、系列の出所の書類が株式分割の前と後にまたがる（隣り合う書類の重なる期で 1 株当たりの値だけが一定の比率でずれる）。
              出所がすべて分割の後の書類なら除外しない（古い書類の分割の前の値は restated に並ぶだけ）。
  集約＝cagr（(終/始)^(1/(年数−1))−1）・change（終−始）・ratio（終/始）・mean・min・max・streak_up／streak_down
        （期間の終わりから数えた連続増加／減少の回数）・years_meeting（year_min／year_max を満たした年数）。
  screen_trend(conditions, *, order_by=None, order="desc", industries=None, manufacturing=None, basis=None,
               period_from=None, period_to=None, limit=20, as_of=None)
    conditions＝[{"metric" または "expr", "aggregate", "min"?, "max"?, "year_min"?, "year_max"?}]・条件の名前＝"<aggregate>:<metric または expr>"
    -> {"found": True, "rows": [{"company", "values": {条件の名前: str}, "series": {条件の名前: [...]}, "restated_years": {条件の名前: [...]}}],
        "excluded": {条件の名前: {理由: {"count", "examples"}}}, "unavailable", "matched", "note"} | {"found": False, "reason": str}
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal

from companies.eval.exact_match import EVAL

TOL = Decimal("1e-12")
TOL_CAGR = Decimal("1e-9")  # 分数の冪＝計算の経路で末尾が揺れる


def _load(name: str) -> list[dict]:
    p = EVAL / name
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def _close(a: str, b: str, tol: Decimal = TOL) -> bool:
    x, y = Decimal(a), Decimal(b)
    return abs(x - y) <= tol * max(Decimal(1), abs(y))


def _kw(q: dict) -> dict:
    return {k: q.get(k) for k in ("basis", "period_from", "period_to", "as_of")}


def check_series(q: dict, tc) -> str | None:
    e = q["expected"]
    r = tc(q["company"]["edinet_code"], metric=q["metric"], **_kw(q))
    if not r.get("ok"):
        return f"系列が返らない: {r.get('reason')}"
    if r.get("basis") != q["expected_basis"]:
        return f"basis が違う: {r.get('basis')}（期待 {q['expected_basis']}）"
    if r.get("accounting_standard") != e["accounting_standard"]:
        return f"会計基準が違う: {r.get('accounting_standard')}（期待 {e['accounting_standard']}）"
    if e.get("top_line_item") and r.get("top_line_item") != e["top_line_item"]:
        return f"最上段の収益の項目が違う: {r.get('top_line_item')}（期待 {e['top_line_item']}）"
    got = r["series"]
    if [g["period"] for g in got] != [x["period"] for x in e["series"]]:
        return f"年の並びが違う: {[g['period'] for g in got]}（期待 {[x['period'] for x in e['series']]}）"
    for g, x in zip(got, e["series"]):
        if g["doc_id"] != x["doc_id"]:
            return f"{x['period']} の出所の書類が違う: {g['doc_id']}（期待 {x['doc_id']}＝その年を載せた最新の書類）"
        if q["kind"] == "series":
            if g["value"] != x["value"]:  # 開示値＝文字列のまま
                return f"{x['period']} の値が違う: {g['value']!r}（期待 {x['value']!r}）"
            if g["inputs"][0].get("element") != x["element"]:
                return f"{x['period']} の要素が違う: {g['inputs'][0].get('element')}（期待 {x['element']}）"
            want = {(y["doc_id"], y["value"]) for y in x.get("restated", [])}
            have = {(y["doc_id"], y["value"]) for y in g.get("restated", [])}
            if want != have:
                return f"{x['period']} の restated が違う: {sorted(have)}（期待 {sorted(want)}）"
        else:
            if not _close(g["value"], x["value"]):
                return f"{x['period']} の値が違う: {g['value']}（期待 {x['value']}）"
            ins = {i["term"]: i["value"] for i in g["inputs"]}
            if ins != x["inputs"]:
                return f"{x['period']} の入力が違う: {ins}（期待 {x['inputs']}）"
    if q["kind"] == "series" and r.get("restated_years") != e["restated_years"]:
        return f"restated_years が違う: {r.get('restated_years')}（期待 {e['restated_years']}）"
    for agg, want in e["aggregates"].items():
        extra = {"year_min": e["year_min"]} if agg == "years_meeting" else {}
        a = tc(q["company"]["edinet_code"], metric=q["metric"], aggregate=agg, **extra, **_kw(q))
        if not a.get("ok"):
            return f"集約 {agg} が返らない: {a.get('reason')}"
        if not _close(a["value"], want, TOL_CAGR if agg == "cagr" else TOL):
            return f"集約 {agg} が違う: {a['value']}（期待 {want}）"
    return None


def check_negative(q: dict, tc) -> str | None:
    r = tc(q["company"]["edinet_code"], metric=q["metric"], aggregate=q.get("aggregate"), **_kw(q))
    if r.get("ok"):
        return f"除外されずに返した: {r.get('value') or len(r.get('series', []))}（期待 {q['reason']}）"
    if r.get("reason") != q["reason"]:
        return f"除外の理由が違う: {r.get('reason')}（期待 {q['reason']}）"
    return None


def _agg(values: list[Decimal], agg: str, c: dict) -> Decimal:
    """行の series から集約を計算し直す（返り値の自己整合）。"""
    x, n = values, len(values)
    if agg == "change":
        return x[-1] - x[0]
    if agg == "ratio":
        return x[-1] / x[0]
    if agg == "cagr":
        return (x[-1] / x[0]) ** (Decimal(1) / (n - 1)) - 1
    if agg == "mean":
        return sum(x) / n
    if agg in ("min", "max"):
        return min(x) if agg == "min" else max(x)
    if agg in ("streak_up", "streak_down"):
        k = 0
        for a, b in zip(reversed(x[:-1]), reversed(x[1:])):
            if (b > a) if agg == "streak_up" else (b < a):
                k += 1
            else:
                break
        return Decimal(k)
    lo, hi = c.get("year_min"), c.get("year_max")
    return Decimal(sum(1 for y in x if (lo is None or y >= Decimal(str(lo))) and (hi is None or y <= Decimal(str(hi)))))


def _name(c: dict) -> str:
    return f"{c['aggregate']}:{c.get('metric') or c.get('expr')}"


def check_query(q: dict, st) -> str | None:
    kw = {k: q[k] for k in ("order_by", "order", "industries", "manufacturing", "basis", "period_from", "period_to", "limit", "as_of") if k in q}
    r = st(q["conditions"], **kw)
    e = q["expect"]
    if "reason" in e:
        if r.get("found") is not False or r.get("reason") != e["reason"]:
            return f"受付の誤りにならない: {r.get('reason')}（期待 {e['reason']}）"
        for u in e.get("unavailable_include", []):
            if not any(x["term"] == u["term"] and x["level"] == u["level"] for x in r.get("unavailable", [])):
                return f"無い入力の記録が無い: {u}"
        return None
    if not r.get("found"):
        return f"found=false: {r.get('reason')}"
    rows = r.get("rows", [])
    if not rows and not e.get("allow_empty"):
        return "行が 0"
    for row in rows:
        for c in q["conditions"]:
            n = _name(c)
            v, s = row["values"].get(n), row.get("series", {}).get(n)
            if v is None or not s:
                return f"{row['company']['name']}: 条件 {n} の値か系列が無い"
            if ("min" in c and Decimal(v) < Decimal(str(c["min"]))) or ("max" in c and Decimal(v) > Decimal(str(c["max"]))):
                return f"{row['company']['name']}: 条件 {n} を満たさない値 {v}"
            if e.get("recompute", True):  # 行の系列から集約を計算し直して一致すること
                again = _agg([Decimal(y["value"]) for y in s], c["aggregate"], c)
                if not _close(v, str(again), TOL_CAGR):
                    return f"{row['company']['name']}: {n} の値 {v} が系列から計算した {again} と合わない"
            ps = [y["period"] for y in s]
            if ps != sorted(ps):
                return f"{row['company']['name']}: 系列が年の順でない"
            if (q.get("period_from") and ps[0] < q["period_from"]) or (q.get("period_to") and ps[-1] > q["period_to"]):
                return f"{row['company']['name']}: 系列が期間の外の年を含む {ps[0]}〜{ps[-1]}"
            if "restated_years" not in row or n not in row["restated_years"]:
                return f"{row['company']['name']}: restated_years が無い"
    if e.get("same_span") and rows:  # 期間の既定＝全社で同じ期間（年数の違う集約を並べない）
        n = _name(q["conditions"][0])
        spans = {(row["series"][n][0]["period"][:4], len(row["series"][n])) for row in rows}
        lens = {k[1] for k in spans}
        if max(lens) - min(lens) > 1:
            return f"期間の既定で系列の年数が会社によって違う: {sorted(lens)}"
    if e.get("no_manufacturing") and any(row["company"]["manufacturing"] for row in rows):
        return "非製造業の絞り込みに製造業の会社が入った"
    if "max_rows" in e and len(rows) > e["max_rows"]:
        return f"行が多い: {len(rows)}"
    if e.get("sorted"):
        ob = q.get("order_by") or _name(q["conditions"][0])
        vals = [Decimal(row["values"][ob]) for row in rows]
        if vals != sorted(vals, reverse=q.get("order", "desc") == "desc"):
            return "並び順が違う"
    for reason in e.get("excluded_include", []):
        if not any(reason in d for d in r.get("excluded", {}).values()):
            return f"除外の理由 {reason} が返らない: {list((r.get('excluded') or {}).values())}"[:300]
    names = {row["company"]["name"] for row in r.get("rows", [])}
    for nm in e.get("companies_include", []):
        if nm not in names:
            return f"{nm} が並びに入らない（matched {r.get('matched')}）"
    if "note_includes" in e:
        for w in e["note_includes"]:
            if w not in json.dumps(r.get("note", ""), ensure_ascii=False):
                return f"返り値の note に「{w}」が無い"
    return None


def main() -> int:
    pos, neg, qs = _load("trend.jsonl"), _load("trend_fail_closed.jsonl"), _load("trend_queries.jsonl")
    try:
        from companies.core.trend import screen_trend, trend_company
    except ImportError:
        print(f"FAIL: 時系列の横断検索（companies.core.trend）が未実装＝正例 0/{len(pos)}・負例 0/{len(neg)}・横断の問 0/{len(qs)}")
        return 1
    fails = []
    for items, check, fn in ((pos, check_series, trend_company), (neg, check_negative, trend_company), (qs, check_query, screen_trend)):
        for q in items:
            try:
                err = check(q, fn)
            except Exception as ex:  # 1 問の例外で全体を止めない
                err = f"例外: {ex!r}"[:300]
            if err:
                fails.append((q["id"], err))
    for i, err in fails[:40]:
        print(f"  FAIL {i}: {err}")
    print(f"{'PASS' if not fails else 'FAIL'}: 時系列の横断検索 正例 {len(pos)}・負例 {len(neg)}・横断の問 {len(qs)}・失敗 {len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
