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
  期間の既定＝trend_company はその会社の収録の最古〜最新・screen_trend は基準日の 4 か月前までの 13 年（海外売上比率は 11 年＝地域別の欄は最古の書類の前期から）＝全社で同じ年数（年数の違う集約を並べない）。
  期のずれのある式（ROA の前期の総資産 等）は、入力がそろう最古の年から（指定した期間の最初の年の入力がそろわなければ insufficient_history）。
  restated＝出所の書類と同じ期・同じ項目・同じ会計基準の値が違う他の書類（表示単位の粗い方の 1 単位以内の差は丸め＝数えない）。
  stock_split＝1 株当たりの項目で、系列の出所の書類が株式分割の前と後にまたがる（隣り合う書類の重なる期で 1 株当たりの値だけが一定の比率でずれる）。
              出所がすべて分割の後の書類なら除外しない（古い書類の分割の前の値は restated に並ぶだけ）。
  share_count_changed の行は comparable＝{"bottom_line": {"aggregate", "value", "periods"}（同じ期間・basis の当期純利益の同じ集約・
              years_meeting は value なし・計算できなければ reason）, "issued_shares": [{"period", "value", "doc_id"}]（系列の最初と最後の年・
              その年を載せた最新の書類）}。株数をそろえた EPS は作らない（2026-10-01 本人決定）。screen_trend は companies を指定したときだけ
              excluded_companies（会社ごとの理由と comparable）。
  share_count_changed＝1 株当たりの項目で、隣り合う年の 1 株の大きさ（各年の出所の書類の自己資本 ÷ 1 株当たり純資産・当期純利益 ÷ 1 株当たり
              当期純利益＝株式数の目安）が、計算できる目安のすべてで同じ向きに 1.45 倍以上ずれる（株式分割・併合の前と後の値が混ざる＝
              同じ書類の中の段差・restated の出ない段差を含む。大きな増資・合併もここに入る）。
  海外売上比率（段②）の年 t＝t の地域別の表が載る書類のうち最新＝翌年の書類（日本基準は前期の欄・IFRS は表の前期の列）。最新の年はその年の書類。
    本邦と合計のセルの特定は第 1c 便と同じ（合計×単位＝その書類のタグの付いた最上段の収益 t）。表が無く文だけの年は overseas_statement_only で除外。
  段③＝lookup_segments／lookup_regions に期間（period_from・period_to）＝1 社の区分・地域別の年ごとの並び：
    lookup_segments(company, period=None, *, basis=None, doc_id=None, period_from=None, period_to=None, elements=None)
      elements＝期間のとき系列を要素で絞る（11 年で約 99KB になる＝年の並び・区分の組み替えは絞らない）
      期間のとき -> {"found": True, "years": [{"period", "found", "reason"?, "doc_id"?, "segments"?}],
                     "series": [{"member", "label", "kind", "element", "section", "values": [{"period", "value", "doc_id", "restated"?}]}],
                     "regrouped": [{"period", "added": [区分], "removed": [区分]}]（会社が定義した区分の組の年ごとの増減＝旧区分と新区分を対応づけない）}
    lookup_regions(company, period=None, *, basis=None, doc_id=None, period_from=None, period_to=None)
      期間のとき -> {"found": True, "years": [{"period", "found", "reason"?, "doc_id"?, "sections"?}]}（欄は表のまま）
    各年＝その期を当期か前期として載せた最新の書類（1 年ずつ引いたときと同じ）。period と期間の同時指定・期間と doc_id は bad_request。
  集約＝cagr（(終/始)^(1/(年数−1))−1）・change（終−始）・ratio（終/始）・mean・min・max・streak_up／streak_down
        （期間の終わりから数えた連続増加／減少の回数）・years_meeting（year_min／year_max を満たした年数）。
  screen_trend(conditions, *, order_by=None, order="desc", industries=None, manufacturing=None, basis=None,
               period_from=None, period_to=None, limit=20, as_of=None, companies=None, detail=False)
    行の series は細い列の形＝{"periods", "values", "doc_ids", "inputs": {項目: [各年の開示値]}（開示値の条件は省く）, "restated"?:
    [{"period", "term", "doc_id", "value"}]} と行の elements（項目→要素）。companies（EDINET コード・10 社まで）で会社を指定し detail=true で各年の入力の出典一式（trend_company と同じ形）。
    上位 20 社で 50KB 以下（返り値は利用側のモデルの文脈に入る・MCP では字下げなしの JSON 文字列）
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
        elif q["kind"] == "series_overseas":
            i = next((y for y in g["inputs"] if y["term"] == "overseas_sales_ratio[t]"), {})
            if (i.get("home_text"), i.get("total_text")) != (x["home_text"], x["total_text"]):
                return f"{x['period']} の本邦と合計のセルが違う: {i.get('home_text')}／{i.get('total_text')}（期待 {x['home_text']}／{x['total_text']}）"
            if not _close(g["value"], x["value"]):
                return f"{x['period']} の値が違う: {g['value']}（期待 {x['value']}）"
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
    return _check_comparable(q.get("expect_comparable"), r.get("comparable"))


def _check_comparable(want: dict | None, got: dict | None) -> str | None:
    """share_count_changed に添える比べられる事実＝当期純利益の同じ集約と、最初と最後の年の発行済株式総数（株数をそろえた EPS は作らない）。"""
    if want is None:
        return None
    if not got:
        return "比べられる事実（comparable）が無い"
    bl, wb = got.get("bottom_line") or {}, want["bottom_line"]
    if bl.get("aggregate") != wb["aggregate"] or bl.get("periods") != wb["periods"]:
        return f"当期純利益の集約・期間が違う: {bl.get('aggregate')} {bl.get('periods')}（期待 {wb['aggregate']} {wb['periods']}）"
    if wb["value"] is not None and (bl.get("value") is None or not _close(bl["value"], wb["value"], TOL_CAGR)):
        return f"当期純利益の {wb['aggregate']} が違う: {bl.get('value')}（期待 {wb['value']}）"
    have = [(x.get("period"), x.get("value"), x.get("doc_id")) for x in got.get("issued_shares") or []]
    need = [(x["period"], x["value"], x["doc_id"]) for x in want["issued_shares"]]
    if have != need:
        return f"発行済株式総数が違う: {have}（期待 {need}）"
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


def _years(s_) -> list[tuple[str, str]]:
    """行の系列の (期, 値)＝細い列の形（periods・values）と detail の形（各年の辞書の並び）の両方を読む。"""
    if isinstance(s_, dict):
        return list(zip(s_["periods"], s_["values"]))
    return [(y["period"], y["value"]) for y in s_]


def _name(c: dict) -> str:
    return f"{c['aggregate']}:{c.get('metric') or c.get('expr')}"


def check_query(q: dict, st) -> str | None:
    kw = {k: q[k] for k in ("order_by", "order", "industries", "manufacturing", "basis", "period_from", "period_to", "limit", "as_of",
                             "companies", "detail") if k in q}
    r = st(q["conditions"], **kw)
    e = q["expect"]
    if "max_kb" in e:  # 返り値は利用側のモデルの文脈に入る＝大きさに上限（2026-10-01）
        kb = len(json.dumps(r, ensure_ascii=False).encode()) / 1000
        if kb > e["max_kb"]:
            return f"返り値が大きい: {kb:.0f}KB（上限 {e['max_kb']}KB）"
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
                again = _agg([Decimal(v_) for _, v_ in _years(s)], c["aggregate"], c)
                if not _close(v, str(again), TOL_CAGR):
                    return f"{row['company']['name']}: {n} の値 {v} が系列から計算した {again} と合わない"
            ps = [p_ for p_, _ in _years(s)]
            if ps != sorted(ps):
                return f"{row['company']['name']}: 系列が年の順でない"
            if (q.get("period_from") and ps[0] < q["period_from"]) or (q.get("period_to") and ps[-1] > q["period_to"]):
                return f"{row['company']['name']}: 系列が期間の外の年を含む {ps[0]}〜{ps[-1]}"
            if "restated_years" not in row or n not in row["restated_years"]:
                return f"{row['company']['name']}: restated_years が無い"
    if e.get("same_span") and rows:  # 期間の既定＝全社で同じ期間（年数の違う集約を並べない）
        n = _name(q["conditions"][0])
        spans = {(_years(row["series"][n])[0][0][:4], len(_years(row["series"][n]))) for row in rows}
        lens = {k[1] for k in spans}
        if max(lens) - min(lens) > 1:
            return f"期間の既定で系列の年数が会社によって違う: {sorted(lens)}"
    for row in rows:
        for n, s_ in row.get("series", {}).items():
            if e.get("compact"):
                if not isinstance(s_, dict) or not (len(s_["periods"]) == len(s_["values"]) == len(s_["doc_ids"])):
                    return f"{row['company']['name']}: 細い返り値の系列が列の形（periods・values・doc_ids が同じ長さ）でない"
                for t in e.get("inputs_terms", []):
                    col = (s_.get("inputs") or {}).get(t)
                    if not col or len(col) != len(s_["periods"]) or any(v is None for v in col):
                        return f"{row['company']['name']}: 入力 {t} の各年の開示値がそろわない"
                if "elements" not in row:
                    return f"{row['company']['name']}: 細い返り値に入力の要素（elements）が無い"
            if e.get("detail") and not (isinstance(s_, list) and all(isinstance(y.get("inputs"), list) and all("element" in i or "home_text" in i for i in y["inputs"]) for y in s_)):
                return f"{row['company']['name']}: detail=true で各年の入力の出典一式が無い"
    if set(e.get("companies_exclude", [])) & {row["company"]["edinet_code"] for row in r.get("rows", [])}:
        return f"並びに出てはいけない会社が入った: {sorted(set(e['companies_exclude']) & {row['company']['edinet_code'] for row in rows})}"
    for code in e.get("excluded_companies_comparable", []):  # companies を指定した問は除外した会社ごとに理由と比べられる事実
        x = next((x for x in r.get("excluded_companies") or [] if x.get("company", {}).get("edinet_code") == code), None)
        if not x or x.get("reason") != "share_count_changed" or not (x.get("comparable") or {}).get("issued_shares"):
            return f"{code}: excluded_companies に share_count_changed と比べられる事実が無い"
    for reason, words in e.get("excluded_note_includes", {}).items():
        notes = " ".join(d.get(reason, {}).get("note", "") for d in (r.get("excluded") or {}).values())
        if not all(w in notes for w in words):
            return f"除外の理由 {reason} の note に {words} の案内が無い"
    if "companies_only" in e and {row["company"]["edinet_code"] for row in rows} - set(e["companies_only"]):
        return "companies の指定の外の会社が入った"
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
    for w in e.get("ratio_note_includes", []):
        if w not in (r.get("ratio_note") or ""):
            return f"ratio_note に「{w}」が無い"
    for w in e.get("ratio_note_excludes", []):
        if w in (r.get("ratio_note") or ""):
            return f"ratio_note に問と関係の無い「{w}」が付いている"
    if "note_includes" in e:
        for w in e["note_includes"]:
            if w not in json.dumps(r.get("note", ""), ensure_ascii=False):
                return f"返り値の note に「{w}」が無い"
    return None


def _mk(m: str) -> str:
    return m.replace(":", "")  # 置き場の区分（接頭辞:要素）と公式 CSV の context の末尾（つないだ形）をそろえる


def check_lookup(q: dict, fns) -> str | None:
    seg, regn = fns
    if q["kind"] == "lookup_excluded":
        fn = seg if q["tool"] == "segments" else regn
        r = fn(q["company"], q.get("period"), **{k: q[k] for k in ("doc_id", "period_from", "period_to") if k in q})
        if r.get("found") is not False or r.get("reason") != q["reason"]:
            return f"受付の誤り・収録外にならない: {r.get('reason')}（期待 {q['reason']}）"
        return None
    if q["kind"] == "segments_range_elements":
        r = seg(q["company"]["edinet_code"], period_from=q["period_from"], period_to=q["period_to"], elements=[q["element"]])
        if not r.get("found"):
            return f"found=false: {r.get('reason')}"
        if {s_["element"] for s_ in r["series"]} != {q["element"]}:
            return f"要素で絞った系列に他の要素が入る: {sorted({s_['element'] for s_ in r['series']})[:5]}"
        if not set(q["members"]) <= {_mk(s_["member"]) for s_ in r["series"]}:
            return "要素で絞ると区分の系列が欠ける"
        if not r.get("years") or "regrouped" not in r:
            return "要素で絞ると年の並び・区分の組み替えが返らない"
        return None
    fn = seg if q["kind"] == "segments_range" else regn
    r = fn(q["company"]["edinet_code"], period_from=q["period_from"], period_to=q["period_to"])
    if not r.get("found"):
        return f"found=false: {r.get('reason')}"
    e = q["expected"]
    got = r.get("years", [])
    if [(y["period"], y.get("doc_id")) for y in got] != [(y["period"], y["doc_id"]) for y in e["years"]]:
        return f"年と出所の書類が違う: {[(y['period'], y.get('doc_id')) for y in got]}（期待 {[(y['period'], y['doc_id']) for y in e['years']]}）"[:400]
    if q["kind"] == "regions_range":
        for g, x in zip(got, e["years"]):
            secs = [s_ for s_ in g.get("sections", []) if s_["section"] == x["section"]]
            if len(secs) != 1:
                return f"{x['period']} の欄 {x['section']} が 1 つでない: {len(secs)}"
            have = [c["text"].strip() for c in secs[0]["content"] if c.get("type") == "text"] if not secs[0]["has_table"] else None
            cells = [c["text"].strip() for t in secs[0]["content"] if t["type"] == "table" for row in t["rows"] for c in row if c["text"].strip()]
            if cells != x["cells"]:
                return f"{x['period']} の表のセルが違う（{len(cells)} 個・期待 {len(x['cells'])} 個）: {cells[:6]} …（期待 {x['cells'][:6]} …）"
        return None
    series = {(_mk(s_["member"]), s_["element"]): s_["values"] for s_ in r.get("series", [])}
    for m, want in e["series"].items():
        have = series.get((m, q["element"]))
        if have is None:
            return f"区分 {m} の {q['element']} の系列が無い"
        if [(v["period"], v["value"], v["doc_id"]) for v in have] != [(v["period"], v["value"], v["doc_id"]) for v in want]:
            return f"区分 {m} の系列が違う: {[(v['period'], v['value'], v['doc_id']) for v in have]}（期待 {[(v['period'], v['value'], v['doc_id']) for v in want]}）"[:400]
        for v, w in zip(have, want):
            if {(x["doc_id"], x["value"]) for x in v.get("restated", [])} != {(x["doc_id"], x["value"]) for x in w.get("restated", [])}:
                return f"区分 {m} の {w['period']} の restated が違う: {v.get('restated')}（期待 {w.get('restated')}）"
    rg = [{"period": x["period"], "added": sorted(map(_mk, x["added"])), "removed": sorted(map(_mk, x["removed"]))} for x in r.get("regrouped", [])]
    if rg != e["regrouped"]:
        return f"区分の組み替えが違う: {rg}（期待 {e['regrouped']}）"[:400]
    return None


def main() -> int:
    pos, neg, qs = _load("trend.jsonl"), _load("trend_fail_closed.jsonl"), _load("trend_queries.jsonl")
    lk = _load("trend_lookup.jsonl")  # 段③（1 社の区分・地域別の年ごとの並び）
    try:
        from companies.core.trend import screen_trend, trend_company
    except ImportError:
        print(f"FAIL: 時系列の横断検索（companies.core.trend）が未実装＝正例 0/{len(pos)}・負例 0/{len(neg)}・横断の問 0/{len(qs)}")
        return 1
    fails = []
    from companies.core.regions import lookup_regions
    from companies.core.segments import lookup_segments
    for items, check, fn in ((pos, check_series, trend_company), (neg, check_negative, trend_company), (qs, check_query, screen_trend),
                             (lk, check_lookup, (lookup_segments, lookup_regions))):
        for q in items:
            try:
                err = check(q, fn)
            except Exception as ex:  # 1 問の例外で全体を止めない
                err = f"例外: {ex!r}"[:300]
            if err:
                fails.append((q["id"], err))
    for i, err in fails[:40]:
        print(f"  FAIL {i}: {err}")
    print(f"{'PASS' if not fails else 'FAIL'}: 時系列の横断検索 正例 {len(pos)}・負例 {len(neg)}・横断の問 {len(qs)}・区分と地域別の年ごと {len(lk)}・失敗 {len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
