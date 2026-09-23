"""ネットワーク不要の単体テスト＝語彙と評価問の整合（不退転の下限つき）。

  python -m companies.eval.test_core
"""
from __future__ import annotations

import json
import re
import sys

from companies.core.items import BASES, ELEMENT_TO_KEY, ITEMS
from companies.eval.exact_match import EVAL

MIN_POS, MIN_NEG, MIN_FIND = 371, 36, 20  # 問は減らさない（足したら上げる）
MIN_SEG_POS, MIN_SEG_NEG = 134, 18        # 第 1b 便（セグメント別）
SEG_REASONS = {"no_segment_figures", "not_tagged", "no_consolidated_statements", "out_of_range", "bad_period", "unknown_company"}
SEG_SECTIONS = {"segment_information", "employees", "capex", "research_and_development"}
REASONS = {"item_not_disclosed", "no_consolidated_statements", "out_of_range", "bad_period", "unknown_item",
           "unknown_company", "ambiguous_company", "ambiguous_item"}


def main() -> int:
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", k) for k in ITEMS), "キーは英小文字・数字・_"
    assert all(label and els for label, els in ITEMS.values()), "呼び名と要素は必須"
    # 隣の概念を 1 つのキーに束ねていないこと（実データ検証 2026-09-20 §2 の誤答の型）
    for a, b in (("NetSales", "RevenueIFRS"), ("NetSales", "OperatingRevenue1"), ("NetSales", "OrdinaryIncome"), ("OrdinaryIncomeLoss", "ProfitLossBeforeTaxIFRS"),
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
    assert {q["reason"] for q in neg} >= REASONS - {"ambiguous_item"}, "理由の種類ごとに最低 1 問"  # ambiguous_item は書類の宣言基準で定まらないときだけ
    find = [json.loads(l) for l in (EVAL / "find_quality.jsonl").read_text().splitlines() if l.strip()]
    assert len(find) >= MIN_FIND and len({q["id"] for q in find}) == len(find), f"発見層の問が減った／id 重複: {len(find)}"
    assert all(q["expect"] in ("found", "ambiguous_company", "unknown_company") for q in find)
    seg = [json.loads(l) for l in (EVAL / "segments.jsonl").read_text().splitlines() if l.strip()]
    seg_neg = [json.loads(l) for l in (EVAL / "segments_fail_closed.jsonl").read_text().splitlines() if l.strip()]
    assert len(seg) >= MIN_SEG_POS and len(seg_neg) >= MIN_SEG_NEG, f"セグメントの問が減った: 正例 {len(seg)}・負例 {len(seg_neg)}"
    assert len({q["id"] for q in seg + seg_neg}) == len(seg) + len(seg_neg), "セグメントの問の id の重複"
    for q in seg:
        e = q["expected"]
        assert q["basis"] in BASES and re.fullmatch(r"\d{4}-\d{2}", q["period"]) and q["doc_id"], q["id"]
        assert isinstance(e["value"], str) and e["value"] and e["section"] in SEG_SECTIONS, q["id"]
        assert e["member_kind"] != "company_defined" or e.get("member_label"), f"{q['id']}: 会社が定義した区分はラベルを固定する"
    assert {q["reason"] for q in seg_neg} == SEG_REASONS, "セグメントの理由の種類ごとに最低 1 問"
    print(f"PASS: 語彙 {len(ITEMS)} キー／{len(ELEMENT_TO_KEY)} 要素・正例 {len(pos)}・負例 {len(neg)}・発見層 {len(find)}"
          f"・セグメント 正例 {len(seg)}／負例 {len(seg_neg)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
