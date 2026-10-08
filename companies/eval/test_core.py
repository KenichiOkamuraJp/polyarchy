"""ネットワーク不要の単体テスト＝語彙と評価問の整合（不退転の下限つき）。

  python -m companies.eval.test_core
"""
from __future__ import annotations

import json
import re
import sys

from companies.core.items import BASES, ELEMENT_TO_KEY, ITEMS
from companies.eval.exact_match import EVAL

MIN_POS, MIN_NEG, MIN_FIND = 372, 36, 20  # 問は減らさない（足したら上げる）
MIN_SEG_POS, MIN_SEG_NEG = 134, 18        # 第 1b 便（セグメント別）
SEG_REASONS = {"no_segment_figures", "not_tagged", "no_consolidated_statements", "out_of_range", "bad_period", "unknown_company"}
SEG_SECTIONS = {"segment_information", "employees", "capex", "research_and_development"}
MIN_REG_POS, MIN_REG_NEG = 291, 16        # 第 1b 便②（地域別）
MIN_IND = 13                              # 第 1c 便（業種）
MIN_SCR_POS, MIN_SCR_NEG, MIN_SCR_Q = 273, 25, 8  # 第 1c 便（横断検索）
MIN_TREND_POS, MIN_TREND_NEG, MIN_TREND_Q, MIN_TREND_LK = 23, 18, 29, 16  # 第 1e 便（時系列）＝負例・1 社の年ごとは人手の *_manual を含む
REG_REASONS = {"omitted", "not_tagged", "no_consolidated_statements", "out_of_range", "bad_period", "unknown_company"}
REG_SECTIONS = {"revenue", "property_plant_and_equipment", "geographic_areas_ifrs"}
REG_KINDS = {"home", "last_number", "span", "uchi", "multiline", "order", "prose_amount", "omitted_paragraph", "total_two_path", "nested"}
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
    # 第 1d 便の問（連結の有無は決算期ごと・遡り）は別のファイル＝同じ整合の検査にかける
    pos = [json.loads(l) for f in ("exact_match.jsonl", "period_basis.jsonl", "backfill.jsonl") for l in (EVAL / f).read_text().splitlines() if l.strip()]
    neg = [json.loads(l) for f in ("fail_closed.jsonl", "period_basis_fail_closed.jsonl", "backfill_fail_closed.jsonl")
           for l in (EVAL / f).read_text().splitlines() if l.strip()]
    assert len(pos) >= MIN_POS and len(neg) >= MIN_NEG, f"問が減った: 正例 {len(pos)}・負例 {len(neg)}"
    assert len({q["id"] for q in pos + neg}) == len(pos) + len(neg), "id の重複"
    for q in pos:
        assert (q["item"] is None) != (q["element"] is None), f"{q['id']}: item か element のどちらか一方"
        assert q["item"] is None or q["item"] in ITEMS, f"{q['id']}: 語彙に無い項目"
        # basis を指定しない問（第 1d 便）は expected_basis で既定を固定する
        assert (q["basis"] in BASES or (q["basis"] is None and q.get("expected_basis") in BASES)) and re.fullmatch(r"\d{4}-\d{2}", q["period"]), q["id"]
        assert isinstance(q["expected_value"], str) and q["expected_value"], f"{q['id']}: 期待値は文字列"
        if q["item"]:
            assert ELEMENT_TO_KEY[q["expected_element"].split(":")[1]] == q["item"], f"{q['id']}: 要素とキーの対応"
    for q in neg:
        assert q["expect"] == "not_found" and q["reason"] in REASONS, q["id"]
    assert {q["reason"] for q in neg} >= REASONS - {"ambiguous_item"}, "理由の種類ごとに最低 1 問"  # ambiguous_item は書類の宣言基準で定まらないときだけ
    find = [json.loads(l) for f in ("find_quality.jsonl", "find_quality_backfill.jsonl") for l in (EVAL / f).read_text().splitlines() if l.strip()]
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
    reg = [json.loads(l) for l in (EVAL / "regions.jsonl").read_text().splitlines() if l.strip()]
    reg_neg = [json.loads(l) for l in (EVAL / "regions_fail_closed.jsonl").read_text().splitlines() if l.strip()]
    assert len(reg) >= MIN_REG_POS and len(reg_neg) >= MIN_REG_NEG, f"地域別の問が減った: 正例 {len(reg)}・負例 {len(reg_neg)}"
    assert len({q["id"] for q in reg + reg_neg}) == len(reg) + len(reg_neg), "地域別の問の id の重複"
    for q in reg:
        assert q["basis"] in BASES and re.fullmatch(r"\d{4}-\d{2}", q["period"]) and q["doc_id"], q["id"]
        assert q["kind"] in REG_KINDS and q["expected"]["section"] in REG_SECTIONS, q["id"]
    assert {q["kind"] for q in reg} == REG_KINDS, "地域別の問の型ごとに最低 1 問"
    assert {q["reason"] for q in reg_neg} == REG_REASONS, "地域別の理由の種類ごとに最低 1 問"
    ind = [json.loads(l) for f in ("industries.jsonl", "industries_backfill.jsonl") for l in (EVAL / f).read_text().splitlines() if l.strip()]
    assert len(ind) >= MIN_IND and len({q["id"] for q in ind}) == len(ind), f"業種の問が減った／id 重複: {len(ind)}"
    assert any(q["expected"]["manufacturing"] for q in ind) and any(not q["expected"]["manufacturing"] for q in ind), "製造業と非製造業の両方"
    assert any(q["expected"]["listing"] == "非上場" for q in ind), "非上場の会社を 1 問以上"
    scr = [json.loads(l) for f in ("screen.jsonl", "screen_manual.jsonl", "screen_backfill.jsonl") for l in (EVAL / f).read_text().splitlines() if l.strip()]
    scr_neg = [json.loads(l) for f in ("screen_fail_closed.jsonl", "screen_backfill_fail_closed.jsonl") for l in (EVAL / f).read_text().splitlines() if l.strip()]
    scr_q = [json.loads(l) for l in (EVAL / "screen_queries.jsonl").read_text().splitlines() if l.strip()]
    assert len(scr) >= MIN_SCR_POS and len(scr_neg) >= MIN_SCR_NEG and len(scr_q) >= MIN_SCR_Q, f"横断検索の問が減った: {len(scr)}／{len(scr_neg)}／{len(scr_q)}"
    assert len({q["id"] for q in scr + scr_neg + scr_q}) == len(scr) + len(scr_neg) + len(scr_q), "横断検索の問の id の重複"
    from companies.core.screen import PRESETS
    assert {q["metric"] for q in scr} >= set(PRESETS) | {"overseas_sales_ratio"}, "検証済みの型ごとに正例 1 問以上"
    assert {q["reason"] for q in scr_neg} >= {"input_not_disclosed", "input_not_ingested", "unknown_item", "bad_expression", "standard_changed",
                                            "irregular_period", "nonpositive_denominator", "stale_period", "not_disclosed_loss_year"}, "規則と無い入力の 3 段ごとに負例 1 問以上"
    # screen_manual（人手・作り直せない）の値の問は期で固定する＝各社の次の有報を取り込んでも書類が動かない（2026-10-09）
    man = [json.loads(l) for l in (EVAL / "screen_manual.jsonl").read_text().splitlines() if l.strip()]
    assert all(q.get("period_from") == q.get("period_to") == q["period"] for q in man), "screen_manual の問は period_from=period_to=period で固定する"
    # 第 1e 便（時系列）＝下限と、除外の理由ごとに負例 1 問以上（2026-10-09 まで下限が無く、0 件でも PASS した）
    def _lines(*names):
        return [json.loads(l) for f in names if (EVAL / f).exists() for l in (EVAL / f).read_text().splitlines() if l.strip()]
    trd, trd_neg = _lines("trend.jsonl"), _lines("trend_fail_closed.jsonl", "trend_manual_fail_closed.jsonl")
    trd_q, trd_lk = _lines("trend_queries.jsonl"), _lines("trend_lookup.jsonl", "trend_lookup_manual.jsonl")
    assert (len(trd) >= MIN_TREND_POS and len(trd_neg) >= MIN_TREND_NEG and len(trd_q) >= MIN_TREND_Q and len(trd_lk) >= MIN_TREND_LK), \
        f"時系列の問が減った: {len(trd)}／{len(trd_neg)}／{len(trd_q)}／{len(trd_lk)}"
    assert len({q["id"] for q in trd + trd_neg + trd_q + trd_lk}) == len(trd) + len(trd_neg) + len(trd_q) + len(trd_lk), "時系列の問の id の重複"
    from companies.core.trend import TREND_REASONS
    missing = set(TREND_REASONS) - {q["reason"] for q in trd_neg}
    assert not missing, f"時系列の除外の理由ごとに負例 1 問以上（欠け {sorted(missing)}）"
    print(f"PASS: 語彙 {len(ITEMS)} キー／{len(ELEMENT_TO_KEY)} 要素・正例 {len(pos)}・負例 {len(neg)}・発見層 {len(find)}"
          f"・セグメント 正例 {len(seg)}／負例 {len(seg_neg)}・地域別 正例 {len(reg)}／負例 {len(reg_neg)}・業種 {len(ind)}"
          f"・横断検索 正例 {len(scr)}／負例 {len(scr_neg)}／横断の問 {len(scr_q)}"
          f"・時系列 正例 {len(trd)}／負例 {len(trd_neg)}／横断の問 {len(trd_q)}／1 社の年ごと {len(trd_lk)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
