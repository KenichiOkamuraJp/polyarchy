"""発見層（企業の同定）の到達率。全問 PASS が基準。別名・表記ゆれは**問を立ててから**直す。

  python -m companies.eval.find_quality
問＝companies/data/eval/find_quality.jsonl（人手で足す。expect＝found／ambiguous_company／unknown_company）。
"""
from __future__ import annotations

import json
import sys

from companies.core.lookup import find_company
from companies.eval.exact_match import EVAL


def check(q: dict) -> str | None:
    r = find_company(q["query"])
    if q["expect"] == "found":
        if not r["found"] or r["company"]["edinet_code"] != q["edinet_code"]:
            return f"{q['edinet_code']} に定まらない: found={r['found']} reason={r.get('reason')} {(r.get('company') or {}).get('edinet_code')}"
        return None
    if r["found"]:
        return f"1 社に決めた: {r['company']['edinet_code']}（期待＝{q['expect']}）"
    if r.get("reason") != q["expect"]:
        return f"理由が不一致: {r.get('reason')!r}"
    if q["expect"] == "ambiguous_company":
        if not r.get("candidates") or not r.get("n_candidates"):
            return "候補・件数が無い"
        if q.get("must_include") and q["must_include"] not in {c["edinet_code"] for c in r["candidates"]}:
            return f"候補に {q['must_include']} が無い"
    return None


def main() -> int:
    qs = [json.loads(l) for l in (EVAL / "find_quality.jsonl").read_text().splitlines() if l.strip()]
    fails = [(q["id"], q["query"], e) for q in qs if (e := check(q))]
    for i, query, e in fails:
        print(f"  FAIL {i} {query!r}: {e}")
    print(f"{'PASS' if not fails else 'FAIL'}: 発見層 {len(qs) - len(fails)}/{len(qs)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
