"""原典完全一致ゲート（参照層）。正例＝値が文字列として一致／負例＝fail-closed（値を返さない・禁止値を返さない）。

  python -m companies.eval.exact_match            # 全問 PASS で exit 0
問＝companies/data/eval/exact_match.jsonl・fail_closed.jsonl（作り方は make_candidates.py）。

判定に使う参照層の契約（実装は `companies.core.lookup`・未実装のあいだは全問 FAIL）：
  find_company(query: str) -> {"found": bool, "company": {...}|None, "candidates": [...], "reason": str|None}
  lookup_company_facts(company: str, *, item: str|None, element: str|None, period: str, basis: str|None)
    （doc_id: str|None を付けると、その書類の値。省くと提出日が最新の書類の値）
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
    # 書類を固定して引く＝新しい書類を取り込んでも問が動かない（同じ決算期の値は後年の書類に再掲・遡及修正され得る）
    r = lookup(q["company"]["edinet_code"], item=q["item"], element=q["element"], period=q["period"], basis=q["basis"],
               doc_id=q["source"]["doc_id"] if q.get("pin", True) else None, accounting_standard=q.get("accounting_standard"))
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
    # 各社の拡張要素は会社のラベルつきで返す契約（要素 ID の英語名だけでは利用側が意味を推せない・2026-09-23 目視で欠落を発見）
    if not q["expected_element"].startswith("jpcrp_cor:") and not r.get("label"):
        return "拡張要素のラベル（label）が空"
    if "expected_label" in q and r.get("label") != q["expected_label"]:
        return f"ラベルが不一致: {r.get('label')!r} ≠ {q['expected_label']!r}"
    if "expected_companion" in q and (r.get("companion") or {}).get("value", "（欄なし）") != q["expected_companion"]:
        return f"対の値（年／月）が不一致: {r.get('companion')!r} ≠ {q['expected_companion']!r}"
    if q.get("expect_other_standards") and not r.get("other_standards"):
        return "もう一方の会計基準の値（other_standards）が添えられていない"
    if q.get("expect_other_documents") and not r["source"].get("other_documents"):
        return "他の書類の値（other_documents）が並んでいない"
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
    missing = [k for k in q.get("expect_suggest", []) if k not in {s.get("item") for s in r.get("suggest", [])}]
    if missing:
        return f"引き直し先の案内（suggest）に {missing} が無い"
    # 会社独自の項目への案内（item を持たない＝要素 ID で名指す）。ラベルが空だと案内から落ちる（2026-09-23 のラベル欠落で実測）
    missing_el = [e for e in q.get("expect_suggest_elements", []) if e not in {s.get("element") for s in r.get("suggest", [])}]
    if missing_el:
        return f"引き直し先の案内（suggest）に会社独自の項目 {missing_el} が無い"
    if q.get("expect_alternatives") and not r.get("alternatives"):
        return "代わりに開示されている項目の一覧（alternatives）が無い"
    if q.get("expect_competing") and len(r.get("competing") or []) < 2:
        return "並んでいる要素（competing）が 2 つ以上示されていない"
    if q.get("expect_candidates") and not r.get("candidates"):
        return "候補（candidates）が無い"
    return None


def unlabeled_extensions() -> list[str]:
    """値の置き場の全件で、ラベルが空の拡張要素（会社・要素）。問の外の会社も含めて 0 件であること。"""
    from companies.core import store
    out = set()
    for f in (store.STORE / "facts").glob("*.json"):
        for r in json.loads(f.read_text()):
            if not r["element"].startswith("jpcrp_cor:") and not r.get("label"):
                out.add(f"{f.stem} {r['element']}")
    return sorted(out)


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
    unlabeled = unlabeled_extensions()
    for u in unlabeled[:10]:
        print(f"  FAIL ラベルが空の拡張要素: {u}")
    if unlabeled:
        fails.append(("store", f"ラベルが空の拡張要素 {len(unlabeled)} 件"))
    for i, e in fails[:40]:
        print(f"  FAIL {i}: {e}")
    print(f"{'PASS' if not fails else 'FAIL'}: 正例 {len(pos)}・負例 {len(neg)}・失敗 {len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
