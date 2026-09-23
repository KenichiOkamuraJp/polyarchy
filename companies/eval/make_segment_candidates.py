"""第 1b 便（セグメント別）の評価問の素材づくり（実装より先に立てる＝docs/第1b便_計画.md §4）。

正例の期待値は第 1 便と同じく **EDINET の公式 CSV（API type=5）** から取り、自前のパーサの読みと**2 経路で一致した点だけ**を採る。
区分（メンバー）は context の次元から、会社が定義した区分のラベルは lab.xml からたどり、**本文（`*_honbun_*.htm`）にも同じ文字列で
出てくるときだけ**期待値にする（公式 CSV は区分のラベルを持たない）。

  python -m companies.eval.make_segment_candidates
出力：companies/data/eval/segments.jsonl（正例）・segments_fail_closed.jsonl（負例）。**上書きする**＝人手で直した行があるときは実行しない。
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import re
import sys
import zipfile

from companies.core import store
from companies.eval.make_candidates import EVAL, TODAY, official_csv
from companies.ingest import verify_xbrl as v
from companies.ingest.edinet import extension_labels

AXIS = "OperatingSegmentsAxis"
NONCON = "_NonConsolidatedMember"
PER_COMPANY = 10
# 型を名指しした会社（証券コード）＝日本基準・IFRS・銀行・保険・小売・鉄道・通信・連結なし（単体の注記でセグメントを開示）
POSITIVE = ["2204", "1801", "6501", "7203", "2802", "8306", "8766", "8750", "3382", "9020", "9432", "6758"]

# 値が載っている欄＝要素名で決まる（2026-09-23 実測・422 書類）。ここに無い要素はセグメント情報の注記
SECTION = {"NumberOfEmployees": "employees", "AverageNumberOfTemporaryWorkers": "employees",
           "CapitalExpendituresOverviewOfCapitalExpendituresEtc": "capex",
           "ResearchAndDevelopmentExpensesResearchAndDevelopmentActivities": "research_and_development"}
# 標準の区分の種類（会社が定義した区分は company_defined）
KIND = {"ReportableSegmentsMember": "reportable_total", "ReconcilingItemsMember": "reconciling",
        "CorporateSharedMember": "corporate", "TotalOfReportableSegmentsAndOthersMember": "total",
        "OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember": "other",
        "UnallocatedAmountsAndEliminationMember": "unallocated_and_elimination",
        "OtherReportableSegmentsMember": "other_reportable", "OtherOperatingSegmentsAxisMember": "other"}


def section_of(element: str) -> str:
    return SECTION.get(element.split(":")[1], "segment_information") if element.startswith("jpcrp_cor:") else "segment_information"


def kind_of(member: str) -> str:
    prefix, _, local = member.partition(":")
    return KIND.get(local, "other_standard") if prefix in ("jpcrp_cor", "jppfs_cor", "jpigp_cor") else "company_defined"


def body_text(zp) -> str:
    with zipfile.ZipFile(zp) as z:
        html = "".join(z.read(n).decode("utf-8") for n in z.namelist()
                       if n.startswith("XBRL/PublicDoc/") and "_honbun_" in n and n.endswith(".htm"))
    return re.sub(r"\s|&nbsp;|　", "", html_mod.unescape(re.sub(r"<[^>]+>", "", html)))


def candidates(doc_id: str) -> tuple[dict, list[dict]]:
    zp = v.fetch_zip(doc_id)
    inst = v.parse_instance(zp)
    dei = {f["name"]: f["value"] for f in inst["facts"] if f["prefix"] == "jpdei_cor"}
    mine = {(f"{f['prefix']}:{f['name']}", f["context"]): f for f in inst["facts"] if not f["nil"]}
    labels, body = extension_labels(zp), body_text(zp)
    out = []
    for el, _label, ctx, _rel, _cn, _pt, unit, _u, value in official_csv(doc_id):
        period, dims = inst["contexts"].get(ctx, (None, {}))
        seg = [m for d, m in dims.items() if d.endswith(AXIS)]
        if not seg or set(d.split(":")[-1] for d in dims) - {AXIS, "ConsolidatedOrNonConsolidatedAxis"}:
            continue  # セグメントの軸（と連結・個別の軸）だけの context
        if not ctx.startswith(("CurrentYear", "Prior1Year")) or value in ("", "－") or not unit:  # 数値だけ（文章の欄は第 1b 便の外）
            continue
        f = mine.get((el, ctx))
        if f is None or f["value"] != value:
            if f is not None:
                print(f"  2 経路の不一致＝捨てる: {doc_id} {el} {ctx}: csv={value!r} xbrl={f['value']!r}", file=sys.stderr)
            continue
        member = seg[0]
        q = {"member": member, "member_kind": kind_of(member), "element": el, "section": section_of(el),
             "value": value, "unit": unit, "context": ctx,
             "basis": "non_consolidated" if NONCON in ctx else "consolidated", "period": period.split("/")[-1][:7]}
        lab = labels.get(member)
        if q["member_kind"] == "company_defined" and lab and re.sub(r"\s|　", "", lab) in body:
            q["member_label"] = lab
        out.append(q)
    return dei, out


def pick(cands: list[dict]) -> list[dict]:
    """型を覆うように選ぶ＝区分の種類・欄・当期／前期・連結／単体を 1 つずつ先に取り、残りを決まった順（ハッシュ）で埋める。"""
    order = sorted(cands, key=lambda q: hashlib.sha1(f"{q['element']}{q['context']}".encode()).hexdigest())
    chosen: list[dict] = []
    for key in ("member_kind", "section", "basis"):
        for val in sorted({q[key] for q in order}):
            q = next((q for q in order if q[key] == val and q not in chosen), None)
            if q and len(chosen) < PER_COMPANY:
                chosen.append(q)
    for prior in (False, True):
        q = next((q for q in order if q["context"].startswith("Prior1Year") == prior and q not in chosen), None)
        if q and len(chosen) < PER_COMPANY and not any(c["context"].startswith("Prior1Year") == prior for c in chosen):
            chosen.append(q)
    for q in order:
        if len(chosen) >= PER_COMPANY:
            break
        if q not in chosen:
            chosen.append(q)
    return chosen


def positives() -> list[dict]:
    reg = store.registry()
    by_sec = {c.get("sec_code"): c for c in reg.values()}
    out = []
    for sec in POSITIVE:
        c = by_sec[sec]
        doc = c["docs"][-1]
        dei, cands = candidates(doc)
        for q in pick(cands):
            short = q["member"].split(":")[1][:30]
            out.append({"id": f"{c['edinet_code']}-{q['element'].split(':')[1][:30]}-{short}-{q['basis'][:3]}-{q['period']}-{doc}",
                        "company": {"edinet_code": c["edinet_code"], "name": c["name"]},
                        "period": q["period"], "basis": q["basis"], "doc_id": doc,
                        "expected": {k: q[k] for k in ("member", "member_kind", "element", "section", "value", "unit", "context")
                                     } | ({"member_label": q["member_label"]} if "member_label" in q else {}),
                        "checked_by": "edinet_csv+xbrl（2 経路一致・区分のラベルは lab.xml と本文の一致・人手の目視は未）",
                        "checked_at": TODAY})
        print(f"  {c['name']}: 候補 {len(cands)} → {min(len(cands), PER_COMPANY)}", file=sys.stderr)
    return out


def negatives() -> list[dict]:
    """負例＝値を返さない型（計画 §3 の fail-closed）。理由コードは構造から機械的に言えるものだけ＝単一か省略かは会社の文の引用で示す。"""
    reg = store.registry()
    by_sec = {c.get("sec_code"): c for c in reg.values()}

    def co(sec):
        c = by_sec[sec]
        return {"edinet_code": c["edinet_code"], "name": c["name"]}, c["fiscal_year_end"][:7]

    out = []
    for sec, quote in (("7974", "単一"), ("4502", "単一"), ("2130", "単一"), ("8558", "のみ")):
        company, per = co(sec)
        out.append({"id": f"{company['edinet_code']}-no_segment_figures-{per}", "company": company, "period": per, "basis": None,
                    "reason": "no_segment_figures", "quote_contains": quote,
                    "note": "セグメント情報の数値が無い（単一セグメント・記載の省略）。単一か省略かは分類せず、会社の文を引用して示す"})
    for sec in ("7751", "6301", "8604"):
        company, per = co(sec)
        out.append({"id": f"{company['edinet_code']}-not_tagged-{per}", "company": company, "period": per, "basis": None,
                    "reason": "not_tagged", "expect_other_sections": sec != "8604",
                    "note": "米国基準＝セグメント情報の注記が XBRL に無い（本文の表だけ）。従業員の状況などタグのある欄は other_sections で返す"})
    company, per = co("2204")
    out += [
        {"id": f"{company['edinet_code']}-consolidated-of-nonconsolidated", "company": company, "period": per, "basis": "consolidated",
         "reason": "no_consolidated_statements", "note": "連結を作成していない会社に連結を指定＝単体の値を連結として返さない"},
        {"id": f"{company['edinet_code']}-out_of_range-2019-03", "company": company, "period": "2019-03", "basis": None,
         "reason": "out_of_range", "note": "収録の無い決算期＝近い期の値は返さない"},
        {"id": f"{company['edinet_code']}-bad_period-FY2025", "company": company, "period": "FY2025", "basis": None,
         "reason": "bad_period", "note": "決算期は YYYY-MM"},
        {"id": "unknown-company", "company": {"edinet_code": None, "name": "存在しない架空商事株式会社"}, "period": "2026-03", "basis": None,
         "reason": "unknown_company", "note": "存在しない会社"},
    ]
    company, per = co("6501")
    out.append({"id": f"{company['edinet_code']}-segments-out_of_range-2023-03", "company": company, "period": "2023-03", "basis": None,
                "reason": "out_of_range",
                "note": "セグメントの値は各書類に当期・前期の 2 期だけ（収録の書類は 2025-03 と 2026-03 の 2 本＝2024-03 までは載る）。"
                        "第 1 便の 5 期推移には 2023-03 があっても、セグメントの値は無い＝経営指標の期から推して返さない"})
    return out


def main() -> int:
    pos, neg = positives(), negatives()
    (EVAL / "segments.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in pos))
    (EVAL / "segments_fail_closed.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in neg))
    print(f"正例 {len(pos)}・負例 {len(neg)} → {EVAL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
