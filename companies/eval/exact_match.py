"""原典完全一致ゲート（参照層）。正例＝値が文字列として一致／負例＝fail-closed（値を返さない・禁止値を返さない）。

  python -m companies.eval.exact_match            # 全問 PASS で exit 0
問＝companies/data/eval/exact_match.jsonl・fail_closed.jsonl（作り方は make_candidates.py）。

判定に使う参照層の契約（実装は `companies.core.lookup`・未実装のあいだは全問 FAIL）：
  find_company(query: str) -> {"found": bool, "company": {...}|None, "candidates": [...], "reason": str|None}
  lookup_company_facts(company: str, *, item: str|None, element: str|None, period: str, basis: str|None)
    -> {"found": bool, "value": str, "basis": str, "element": str, "source": {"doc_id": ...},
        "reason": str|None, "alternatives": [...], "candidates": [...]}
  company は EDINET コードか社名。basis を省いたときは、連結を作成している会社は連結・作成していない会社は単体（どちらかを必ず返す）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"


def _load(name: str) -> list[dict]:
    return [json.loads(l) for l in (EVAL / name).read_text().splitlines() if l.strip()]


def check_positive(q: dict, lookup) -> str | None:
    r = lookup(q["company"]["edinet_code"], item=q["item"], element=q["element"], period=q["period"], basis=q["basis"])
    if not r.get("found"):
        return f"found=false（reason={r.get('reason')}）"
    if r.get("value") != q["expected_value"]:
        return f"値が不一致: {r.get('value')!r} ≠ {q['expected_value']!r}"
    if r.get("basis") != q["basis"]:
        return f"連結／単体が不一致: {r.get('basis')!r}"
    if r.get("element") != q["expected_element"]:
        return f"要素が不一致: {r.get('element')!r}"
    if (r.get("source") or {}).get("doc_id") != q["source"]["doc_id"]:
        return f"出所の書類が不一致: {(r.get('source') or {}).get('doc_id')!r}"
    return None


def check_negative(q: dict, lookup) -> str | None:
    who = q["company"]["edinet_code"] or q["company"]["name"]
    r = lookup(who, item=q["item"], element=None, period=q["period"], basis=q.get("basis"))
    if r.get("found"):
        return f"値を返した: {r.get('value')!r}（期待＝not_found・{q['reason']}）"
    if r.get("value") not in (None, ""):
        return "found=false なのに value がある"
    if r.get("reason") != q["reason"]:
        return f"理由が不一致: {r.get('reason')!r} ≠ {q['reason']!r}"
    leaked = [x for x in q.get("must_not_value", []) if x in json.dumps(r, ensure_ascii=False)]
    if leaked:
        return f"禁止値が応答に含まれる: {leaked}"
    if q.get("expect_alternatives") and not r.get("alternatives"):
        return "代わりに開示されている項目の一覧（alternatives）が無い"
    if q.get("expect_candidates") and not r.get("candidates"):
        return "候補（candidates）が無い"
    return None


def main() -> int:
    pos, neg = _load("exact_match.jsonl"), _load("fail_closed.jsonl")
    try:
        from companies.core.lookup import lookup_company_facts as lookup
    except ImportError:
        print(f"FAIL: 参照層（companies.core.lookup）が未実装＝正例 0/{len(pos)}・負例 0/{len(neg)}")
        return 1
    fails = []
    for qs, check in ((pos, check_positive), (neg, check_negative)):
        for q in qs:
            try:
                err = check(q, lookup)
            except Exception as e:  # 1 問の例外で全体を止めない
                err = f"例外: {e!r}"
            if err:
                fails.append((q["id"], err))
    for i, e in fails[:40]:
        print(f"  FAIL {i}: {e}")
    print(f"{'PASS' if not fails else 'FAIL'}: 正例 {len(pos)}・負例 {len(neg)}・失敗 {len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
