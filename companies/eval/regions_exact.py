"""第 1b 便②（地域別）の原典完全一致ゲート。正例＝欄の中身（セル・段落・順序）が本文と一致／負例＝fail-closed。

  python -m companies.eval.regions_exact          # 全問 PASS で exit 0
問＝companies/data/eval/regions.jsonl・regions_fail_closed.jsonl（作り方は make_region_candidates.py・契約は docs/第1b便_計画.md §6）。

判定に使う参照層の契約（実装は `companies.core.regions`・未実装のあいだは全問 FAIL）：
  lookup_regions(company: str, period: str, *, basis: str|None = None, doc_id: str|None = None)
    -> {"found": True, "basis", "period",
        "sections": [{"element", "section", "context", "period", "period_in_columns": bool, "has_table": bool,
                      "content": [{"type": "text", "text"} | {"type": "omitted", "chars"} |
                                  {"type": "table", "rows": [[{"text", "rowspan"?, "colspan"?}]]}]}],
        "read_note": str, "source": {"doc_id", "submitted", "url", "citation", "other_documents"}, "license": {...}}
    -> {"found": False, "reason": str, "quotes": [{"section", "context", "text"}], ...}
       reason＝omitted（欄はあるが表が無い＝会社の文を quotes で返し、型は分類しない）／not_tagged（地域の欄が無い）／
               no_consolidated_statements／out_of_range／bad_period／unknown_company／ambiguous_company
  unreadable_sections() -> list[str]   # 値の置き場の全件で、原典の欄の表の数と写した表の数が一致しない欄・文字列が null のセル（0 件であること）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"
NOTE_MUST = ("rowspan", "うち", "単位", "中身とずれる", "寄せない")  # read_note が案内すること（契約 §6）


def _load(name: str) -> list[dict]:
    return [json.loads(l) for l in (EVAL / name).read_text().splitlines() if l.strip()]


def check_positive(q: dict, lookup) -> str | None:
    e = q["expected"]
    r = lookup(q["company"]["edinet_code"], q["period"], basis=q["basis"], doc_id=q["doc_id"])  # 書類を固定して引く
    if not r.get("found"):
        return f"found=false（reason={r.get('reason')}）"
    if r.get("basis") != q["basis"]:
        return f"連結／単体が不一致: {r.get('basis')!r}"
    if (r.get("source") or {}).get("doc_id") != q["doc_id"]:
        return f"出所の書類が不一致: {(r.get('source') or {}).get('doc_id')!r}"
    note = r.get("read_note") or ""
    missing = [w for w in NOTE_MUST if w not in note]
    if missing:
        return f"read_note に {missing} の案内が無い"
    secs = [s for s in r.get("sections", []) if s.get("element") == e["element"] and s.get("context") == e["context"]]
    if len(secs) != 1:
        return f"欄が {len(secs)} 件（1 件のはず）: {e['element']} {e['context']}"
    s = secs[0]
    if s.get("section") != e["section"]:
        return f"section が不一致: {s.get('section')!r} ≠ {e['section']!r}"
    content = s.get("content") or []
    tables = [x for x in content if x.get("type") == "table"]
    kind = q["kind"]
    if kind == "order":
        if not content or content[0].get("type") != "text" or content[0].get("text") != e["first_text"]:
            return f"最初の段落が不一致: {content[:1]!r} ≠ {e['first_text']!r}"
        first = next((i for i, x in enumerate(content) if x.get("type") == "table"), None)
        if first != e["first_table_at"] or len(tables) != e["n_tables"]:
            return f"段落と表の順が不一致: 最初の表 {first}（期待 {e['first_table_at']}）・表 {len(tables)}（期待 {e['n_tables']}）"
        return None
    if kind == "prose_amount":
        if not any(x.get("type") == "text" and x.get("text") == e["text"] for x in content):
            return f"表の外の金額の文が無い: {e['text'][:40]!r}"
        return None
    if kind == "omitted_paragraph":
        got = sum(x.get("chars", 0) for x in content if x.get("type") == "omitted")
        if got != e["omitted_chars"]:
            return f"置き換えた段落の字数が不一致: {got} ≠ {e['omitted_chars']}"
        return None
    # セル
    if e["table"] >= len(tables):
        return f"表 {e['table']} が無い（表 {len(tables)}）"
    rows = tables[e["table"]].get("rows") or []
    if e["row"] >= len(rows) or e["col"] >= len(rows[e["row"]]):
        return f"セル ({e['row']},{e['col']}) が無い"
    cell = rows[e["row"]][e["col"]]
    if cell.get("text") != e["text"]:
        return f"セルの文字列が不一致: {cell.get('text')!r} ≠ {e['text']!r}"
    if cell.get("rowspan", 1) != e["rowspan"] or cell.get("colspan", 1) != e["colspan"]:
        return f"結合が不一致: rowspan={cell.get('rowspan', 1)}・colspan={cell.get('colspan', 1)}（期待 {e['rowspan']}・{e['colspan']}）"
    if "nested_tables" in e and cell.get("tables") != e["nested_tables"]:  # セルの中の入れ子の表（マックス）
        return f"入れ子の表が不一致: {json.dumps(cell.get('tables'), ensure_ascii=False)[:120]}"
    if "same_text_cells" in e:  # 結合セルを展開していない＝同じ文字列のセルの数が原典と同じ
        n = sum(1 for row in rows for x in row if x.get("text") == e["text"])
        if n != e["same_text_cells"]:
            return f"同じ文字列のセルが {n} 個（原典 {e['same_text_cells']}）＝結合セルを展開していないか"
    return None


def check_negative(q: dict, lookup) -> str | None:
    who = q["company"]["edinet_code"] or q["company"]["name"]
    r = lookup(who, q["period"], basis=q.get("basis"))
    if r.get("found"):
        return f"欄を返した（期待＝not_found・{q['reason']}）"
    if any(x.get("type") == "table" for s in r.get("sections") or [] for x in s.get("content") or []):
        return "found=false なのに表がある"
    if r.get("reason") != q["reason"]:
        return f"理由が不一致: {r.get('reason')!r} ≠ {q['reason']!r}"
    if "quote_contains" in q and not any(q["quote_contains"] in (x.get("text") or "") for x in r.get("quotes") or []):
        return f"会社の文（quotes）に {q['quote_contains']!r} が無い: {r.get('quotes')!r}"[:300]
    return None


def main() -> int:
    pos, neg = _load("regions.jsonl"), _load("regions_fail_closed.jsonl")
    try:
        from companies.core.regions import lookup_regions as lookup, unreadable_sections
    except ImportError:
        print(f"FAIL: 参照層（companies.core.regions）が未実装＝正例 0/{len(pos)}・負例 0/{len(neg)}")
        return 1
    fails = []
    for qs, check in ((pos, check_positive), (neg, check_negative)):
        for q in qs:
            try:
                err = check(q, lookup)
            except Exception as ex:  # 1 問の例外で全体を止めない
                err = f"例外: {ex!r}"
            if err:
                fails.append((q["id"], err))
    bad = unreadable_sections()
    for u in bad[:10]:
        print(f"  FAIL 写しが原典と合わない欄: {u}")
    if bad:
        fails.append(("store", f"写しが原典と合わない欄 {len(bad)} 件"))
    for i, err in fails[:40]:
        print(f"  FAIL {i}: {err}")
    print(f"{'PASS' if not fails else 'FAIL'}: 地域別 正例 {len(pos)}・負例 {len(neg)}・失敗 {len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
