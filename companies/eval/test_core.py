"""ネットワーク不要の単体テスト＝語彙と評価問の整合（不退転の下限つき）。

  python -m companies.eval.test_core
"""
from __future__ import annotations

import json
import re
import sys

from companies.core.items import BASES, ELEMENT_TO_KEY, ITEMS
from companies.eval.exact_match import EVAL

MIN_POS, MIN_NEG = 120, 20  # 問は減らさない（足したら上げる）
REASONS = {"item_not_disclosed", "no_consolidated_statements", "out_of_range", "bad_period", "unknown_item",
           "unknown_company", "ambiguous_company"}


def main() -> int:
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", k) for k in ITEMS), "キーは英小文字・数字・_"
    assert all(label and els for label, els in ITEMS.values()), "呼び名と要素は必須"
    # 隣の概念を 1 つのキーに束ねていないこと（実データ検証 2026-09-20 §2 の誤答の型）
    for a, b in (("NetSales", "OperatingRevenue1"), ("NetSales", "OrdinaryIncome"), ("OrdinaryIncomeLoss", "ProfitLossBeforeTaxIFRS"),
                 ("EquityToAssetRatio", "EquityToAssetRatioIFRS")):
        ka, kb = (ELEMENT_TO_KEY[f"{x}SummaryOfBusinessResults"] for x in (a, b))
        assert ka != kb, f"{a} と {b} が同じキー {ka}"
    pos = [json.loads(l) for l in (EVAL / "exact_match.jsonl").read_text().splitlines() if l.strip()]
    neg = [json.loads(l) for l in (EVAL / "fail_closed.jsonl").read_text().splitlines() if l.strip()]
    assert len(pos) >= MIN_POS and len(neg) >= MIN_NEG, f"問が減った: 正例 {len(pos)}・負例 {len(neg)}"
    assert len({q["id"] for q in pos + neg}) == len(pos) + len(neg), "id の重複"
    for q in pos:
        assert (q["item"] is None) != (q["element"] is None), f"{q['id']}: item か element のどちらか一方"
        assert q["item"] is None or q["item"] in ITEMS, f"{q['id']}: 語彙に無い項目"
        assert q["basis"] in BASES and re.fullmatch(r"\d{4}-\d{2}", q["period"]), q["id"]
        assert isinstance(q["expected_value"], str) and q["expected_value"], f"{q['id']}: 期待値は文字列"
        if q["item"]:
            assert ELEMENT_TO_KEY[q["expected_element"].split(":")[1]] == q["item"], f"{q['id']}: 要素とキーの対応"
    for q in neg:
        assert q["expect"] == "not_found" and q["reason"] in REASONS, q["id"]
    assert {q["reason"] for q in neg} == REASONS, "理由の種類ごとに最低 1 問"
    print(f"PASS: 語彙 {len(ITEMS)} キー／{len(ELEMENT_TO_KEY)} 要素・正例 {len(pos)}・負例 {len(neg)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
