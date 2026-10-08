"""業種（第 1c 便の絞り込み）の判定＝EDINET コードリストの提出者業種を登録簿の全社に写したか。

  python -m companies.eval.industries_exact
問＝companies/data/eval/industries.jsonl（コードリストの値をそのまま期待値にした点検）。
全件の判定＝登録簿の全社に業種がある・業種は東証 33 業種の語彙（コードリストの表記）の中・製造業の定義は 16 業種。
例外は提出義務が無くなった会社（コードリストの提出者業種が「提出義務者以外」＝業種なし・第 1d 便）だけ。
"""
from __future__ import annotations

import json
import sys

from companies.eval.exact_match import EVAL


def main() -> int:
    qs = [json.loads(l) for name in ("industries.jsonl", "industries_backfill.jsonl") if (EVAL / name).exists()
          for l in (EVAL / name).read_text().splitlines() if l.strip()]
    try:
        from companies.core.industries import INDUSTRIES, MANUFACTURING, industry_of, unmatched_companies
    except ImportError:
        print(f"FAIL: 業種（companies.core.industries）が未実装＝0/{len(qs)}")
        return 1
    fails = []
    if len(INDUSTRIES) != 33 or not MANUFACTURING <= set(INDUSTRIES) or len(MANUFACTURING) != 16:
        fails.append(("vocab", f"業種の語彙 {len(INDUSTRIES)}・製造業 {len(MANUFACTURING)}（33・16 のはず）"))
    for q in qs:
        r = industry_of(q["company"]["edinet_code"])
        got = {k: (r or {}).get(k) for k in q["expected"]}
        if got != q["expected"]:
            fails.append((q["id"], f"{got} ≠ {q['expected']}"))
    bad = unmatched_companies()
    if bad:
        fails.append(("store", f"業種が無い・語彙の外の会社 {len(bad)} 社（例 {bad[:5]}）"))
    if not all([qs]):  # 問が 0 件で PASS にしない（作り直しで空になったファイルを見逃さない・2026-10-09）
        fails.append(("eval", "読み込んだ問が 0 件のファイルがある（評価ファイルが空・読めない）"))
    for i, err in fails[:40]:
        print(f"  FAIL {i}: {err}")
    print(f"{'PASS' if not fails else 'FAIL'}: 業種 {len(qs)} 問・失敗 {len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
