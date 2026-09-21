"""評価問の素材づくり（実装より先に立てる＝docs/開発計画.md §4）。

正例の期待値は **EDINET の公式 CSV（API type=5＝金融庁による XBRL→CSV 変換）** から取る＝取込（XBRL インスタンスを自前で読む経路）とは
独立の経路。同じ点を自前のパーサでも読み、**2 経路が一致した点だけ**を正例にする（不一致は標準エラーに出して捨てる）。
期間（決算期末）と連結の有無は XBRL インスタンスの context／DEI から取る（値ではなく構造）。

  python -m companies.eval.make_candidates            # 既存の問の書類を対象（--docids=S100XXXX,… で足す）
出力：companies/data/eval/exact_match.jsonl（正例）・fail_closed.jsonl（負例）。**上書きする**＝人手で直した行があるときは実行しない。
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import sys
import zipfile

from companies.core.items import COMPANION, ELEMENT_TO_KEY, ITEMS, standard_of
from companies.ingest import verify_xbrl as v

EVAL = v.DATA / "eval"
PICK_CTX = ("CurrentYear", "Prior2Year", "Prior4Year")  # 最新・中間・5 期推移の端
PER_COMPANY = 4
NONCON = "_NonConsolidatedMember"
TODAY = dt.date.today().isoformat()


def official_csv(doc_id: str) -> list[list[str]]:
    p = v.CACHE / "csv" / f"{doc_id}.zip"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(v._get(f"{v.API}/documents/{doc_id}", {"type": 5}, binary=True))
    z = zipfile.ZipFile(p)
    name = next(n for n in z.namelist() if "/jpcrp030000-asr" in n)
    return list(csv.reader(io.StringIO(z.read(name).decode("utf-16")), delimiter="\t"))[1:]


def load(doc_id: str) -> dict:
    inst = v.parse_instance(v.fetch_zip(doc_id))
    dei = {f["name"]: f["value"] for f in inst["facts"] if f["prefix"] == "jpdei_cor"}
    mine = {(f"{f['prefix']}:{f['name']}", f["context"]): f["value"] for f in inst["facts"] if not f["nil"]}
    rows = {}
    for el, label, ctx, _rel, _cn, _pt, unit_id, _unit, value in official_csv(doc_id):
        rows[(el, ctx)] = {"label": label, "unit": unit_id, "value": value}
    return {"doc_id": doc_id, "edinet_code": dei.get("EDINETCodeDEI"), "name": dei.get("FilerNameInJapaneseDEI"),
            "consolidated": dei.get("WhetherConsolidatedFinancialStatementsArePreparedDEI") == "true",
            "standard": dei.get("AccountingStandardsDEI"), "contexts": inst["contexts"], "mine": mine, "rows": rows}


def period_of(c: dict, ctx: str) -> str | None:
    per = c["contexts"].get(ctx, (None,))[0]
    return per.split("/")[-1][:7] if per else None


def plain(ctx: str) -> bool:
    """次元なし（連結）か、連結・個別の次元だけ（単体）の context か＝セグメント等の内訳を除く。"""
    return ctx.replace(NONCON, "") in {f"{p}{k}" for p in ("CurrentYear", "Prior1Year", "Prior2Year", "Prior3Year", "Prior4Year")
                                       for k in ("Duration", "Instant")}


def positive(c: dict, el: str, ctx: str, *, by_element: bool = False) -> dict | None:
    row = c["rows"][(el, ctx)]
    if row["value"] in ("", "－") or c["mine"].get((el, ctx)) != row["value"]:
        if row["value"] not in ("", "－"):
            print(f"  2 経路の不一致＝捨てる: {c['name']} {el} {ctx}: csv={row['value']!r} xbrl={c['mine'].get((el, ctx))!r}", file=sys.stderr)
        return None
    key = ELEMENT_TO_KEY.get(el.split(":")[1])
    std = None
    if key and not by_element and len(rivals(c, el, ctx)) > 1:
        std = standard_of(el)  # 同じ決算期に会計基準の違う値が並ぶ（移行年・日本基準の表の併記）＝会計基準を指定して引く問にする
    basis = "non_consolidated" if ctx.endswith(NONCON) else "consolidated"
    q = {"id": f"{c['edinet_code']}-{(el.split(':')[1] if by_element else key)[:40]}{'-' + std.replace(' ', '') if std else ''}"
               f"-{basis[:3]}-{period_of(c, ctx)}-{c['doc_id']}",
         "company": {"edinet_code": c["edinet_code"], "name": c["name"]},
         "item": None if by_element else key, "element": el if by_element else None,
         "basis": basis, "period": period_of(c, ctx),
         "expected_value": row["value"], "unit": row["unit"], "expected_element": el,
         "source": {"doc_id": c["doc_id"], "context": ctx},
         "checked_by": "edinet_csv+xbrl（2 経路一致・人手の目視は未）", "checked_at": TODAY}
    if std:
        q["accounting_standard"] = std
    if key in COMPANION and not by_element:  # 年と月に分けて開示する会社＝対の値が必ず添えられること（無ければ null）
        pair = [c["rows"][(f"jpcrp_cor:{e}", ctx)]["value"] for e in ITEMS[COMPANION[key]][1] if (f"jpcrp_cor:{e}", ctx) in c["rows"]]
        q["expected_companion"] = next((x for x in pair if x not in ("", "－")), None)
    return q


def rivals(c: dict, el: str, ctx: str) -> list[str]:
    """同じ context に値を持つ、同じキーの標準要素。"""
    key = ELEMENT_TO_KEY.get(el.split(":")[1])
    return sorted(e for (e, x) in c["rows"] if x == ctx and e.startswith("jpcrp_cor:") and ELEMENT_TO_KEY.get(e.split(":")[1]) == key
                  and c["rows"][(e, x)]["value"] not in ("", "－"))


def positives(c: dict) -> list[dict]:
    own = NONCON if not c["consolidated"] else ""
    cand, other = [], []
    for (el, ctx) in c["rows"]:
        if not el.startswith("jpcrp_cor:") or el.split(":")[1] not in ELEMENT_TO_KEY or not plain(ctx):
            continue
        if not ctx.startswith(PICK_CTX):
            continue
        key = ELEMENT_TO_KEY[el.split(":")[1]]
        if key.startswith("average_") or key == "employees":
            continue  # 従業員の項目は下で別に 1 点
        (cand if ctx.endswith(NONCON) == bool(own) else other).append((el, ctx))
    order = lambda t: hashlib.sha1(f"{c['doc_id']}{t}".encode()).hexdigest()
    out, used = [], set()
    for el, ctx in sorted(cand, key=order):  # 会社の主たる系列（連結があれば連結）から、項目が重ならないよう 4 点
        key = ELEMENT_TO_KEY[el.split(":")[1]]
        if key in used:
            continue
        q = positive(c, el, ctx)
        if q:
            out.append(q); used.add(key)
        if len(out) >= PER_COMPANY:
            break
    if c["consolidated"]:  # 連結の会社でも単体の系列を別に引けること（連結と単体は別の系列）
        for el, ctx in sorted((t for t in other if t[1].startswith("CurrentYear")), key=order)[:1]:
            q = positive(c, el, ctx)
            if q:
                out.append(q)
    sal = ("jpcrp_cor:AverageAnnualSalaryInformationAboutReportingCompanyInformationAboutEmployees", f"CurrentYearInstant{NONCON}")
    if sal in c["rows"]:
        q = positive(c, *sal)
        if q:
            out.append(q)
    emp = f"CurrentYearInstant{NONCON}"
    for e in ("AverageAgeYears", "AverageAgeMonths", "AverageLengthOfServiceMonths"):
        t = (f"jpcrp_cor:{e}InformationAboutReportingCompanyInformationAboutEmployees", emp)
        if t in c["rows"]:
            q = positive(c, *t)
            if q:
                out.append(q)
    # 会社が定義した項目（拡張要素）＝要素 ID を指定して引く
    ext = [(el, ctx) for (el, ctx) in c["rows"] if el.startswith("jpcrp030000-asr_")
           and (el.endswith("KeyFinancialData") or "SummaryOfBusinessResults" in el)
           and ctx in ("CurrentYearDuration", "Prior4YearDuration")]
    for el, ctx in sorted(ext, key=order)[:2]:
        q = positive(c, el, ctx, by_element=True)
        if q:
            out.append(q)
    return out


def latest_default(cs: list[dict]) -> list[dict]:
    """書類を指定しない参照＝提出日が最新の書類の値（遡及修正があれば新しい値）＋他の書類の値が並ぶこと。"""
    out, by = [], {}
    for c in cs:
        by.setdefault(c["edinet_code"], []).append(c)
    for code, group in by.items():
        if len(group) < 2:
            continue
        old, new = sorted(group, key=lambda c: c["doc_id"])[0], sorted(group, key=lambda c: c["doc_id"])[-1]
        own = NONCON if not new["consolidated"] else ""
        n = 0
        for (el, ctx) in sorted(new["rows"]):
            if n >= 2 or not el.startswith("jpcrp_cor:") or el.split(":")[1] not in ELEMENT_TO_KEY or not ctx.startswith("Prior1Year"):
                continue
            if ctx.endswith(NONCON) != bool(own) or not plain(ctx) or len(rivals(new, el, ctx)) > 1:
                continue
            cur = ctx.replace("Prior1Year", "CurrentYear")
            if (el, cur) not in old["rows"] or period_of(old, cur) != period_of(new, ctx):
                continue
            q = positive(new, el, ctx)
            if not q or ELEMENT_TO_KEY[el.split(":")[1]] in COMPANION:
                continue
            restated = old["rows"][(el, cur)]["value"] != q["expected_value"]
            if n == 0 or restated:
                q.update(id=q["id"] + "-latest", pin=False, expect_other_documents=True,
                         note=("遡及修正あり＝" if restated else "") + "書類を指定しない参照は提出日が最新の書類の値。古い書類の値は other_documents に並ぶ")
                out.append(q); n += 1
    return out


def negatives(cs: list[dict]) -> list[dict]:
    """fail-closed の問＝2026-09-20 の実データ検証で実際に出た誤答の型を固定する（docs/記録/実データ検証_2026-09-20.md §2）。"""
    out = []
    cur = "CurrentYearDuration"
    for c in cs:
        who = {"edinet_code": c["edinet_code"], "name": c["name"]}
        per = period_of(c, cur)
        con_sales = any((f"jpcrp_cor:{e}", cur) in c["rows"] and c["rows"][(f"jpcrp_cor:{e}", cur)]["value"] not in ("", "－")
                        for e in ("NetSalesSummaryOfBusinessResults", "RevenueIFRSSummaryOfBusinessResults", "RevenuesUSGAAPSummaryOfBusinessResults"))
        if c["consolidated"] and not con_sales:
            # 連結に「売上高」が無い会社＝単体の売上高・営業収益で埋めてはならない
            forbid = [c["rows"][(f"jpcrp_cor:{e}", cur + NONCON)]["value"]
                      for e in ("NetSalesSummaryOfBusinessResults", "OperatingRevenue1SummaryOfBusinessResults")
                      if (f"jpcrp_cor:{e}", cur + NONCON) in c["rows"]]
            out.append({"id": f"{c['edinet_code']}-net_sales-con-none", "company": who, "item": "net_sales", "basis": "consolidated",
                        "period": per, "expect": "not_found", "reason": "item_not_disclosed",
                        "must_not_value": [x for x in forbid if x not in ("", "－")],
                        "expect_alternatives": True,
                        "note": "連結の経営指標に標準の『売上高』が無い（営業収益・経常収益・保険料・各社の拡張要素で開示）。単体の値や隣の項目で埋めない＝代わりに開示されている収益項目の一覧を返す"})
        if not c["consolidated"]:
            out.append({"id": f"{c['edinet_code']}-net_sales-con-nocons", "company": who, "item": "net_sales", "basis": "consolidated",
                        "period": per, "expect": "not_found", "reason": "no_consolidated_statements",
                        "note": "連結財務諸表を作成していない会社に連結を指定＝単体の値を連結として返さない"})
            sal = ("jpcrp_cor:AverageAnnualSalaryInformationAboutReportingCompanyInformationAboutEmployees", f"CurrentYearInstant{NONCON}")
            if sal not in c["rows"] or c["rows"][sal]["value"] in ("", "－"):
                out.append({"id": f"{c['edinet_code']}-salary-none", "company": who, "item": "average_annual_salary",
                            "basis": "non_consolidated", "period": per, "expect": "not_found", "reason": "item_not_disclosed",
                            "note": "平均年間給与の開示が無い提出会社"})
    for c in cs:  # 会計基準の移行年＝同じキーに 2 つの値が並ぶ決算期は、片方を黙って選ばない
        amb = sorted({(ELEMENT_TO_KEY[e.split(":")[1]], x) for (e, x) in c["rows"] if e.startswith("jpcrp_cor:")
                      and e.split(":")[1] in ELEMENT_TO_KEY and plain(x) and len(rivals(c, e, x)) > 1})
        for key, ctx in amb[:1]:
            out.append({"id": f"{c['edinet_code']}-{key}-two-standards", "company": {"edinet_code": c["edinet_code"], "name": c["name"]},
                        "item": key, "basis": "non_consolidated" if ctx.endswith(NONCON) else "consolidated", "period": period_of(c, ctx),
                        "expect": "not_found", "reason": "ambiguous_item", "expect_competing": True,
                        "note": "同じ決算期に会計基準の違う値が並ぶ（IFRS への移行年）＝両方を示し、要素 ID の指定で引き直させる"})
    first = cs[0]
    who = {"edinet_code": first["edinet_code"], "name": first["name"]}
    out += [
        {"id": "period-before-coverage", "company": who, "item": "total_assets", "basis": None, "period": "1990-03",
         "expect": "not_found", "reason": "out_of_range", "note": "収録範囲より前の決算期＝近い期の値を返さない"},
        {"id": "period-wrong-notation", "company": who, "item": "total_assets", "basis": None, "period": "FY2024",
         "expect": "not_found", "reason": "bad_period", "note": "期間キーは決算期末 YYYY-MM＝年度表記を推測で読み替えない"},
        {"id": "period-wrong-month", "company": who, "item": "total_assets", "basis": None,
         "period": (period_of(first, "CurrentYearInstant") or "2025-03")[:5] + ("09" if not (period_of(first, "CurrentYearInstant") or "").endswith("09") else "03"),
         "expect": "not_found", "reason": "out_of_range", "note": "決算月が違う＝同じ年の別の月の値として返さない"},
        {"id": "item-unknown", "company": who, "item": "ebitda", "basis": None, "period": period_of(first, "CurrentYearInstant"),
         "expect": "not_found", "reason": "unknown_item", "note": "語彙に無い項目（派生値）＝計算して返さない・語彙の一覧を示す"},
        {"id": "company-unknown-code", "company": {"edinet_code": "E99999", "name": None}, "item": "total_assets", "basis": None,
         "period": "2025-03", "expect": "not_found", "reason": "unknown_company", "note": "存在しない EDINET コード"},
        {"id": "company-unknown-name", "company": {"edinet_code": None, "name": "存在しない商事株式会社"}, "item": "total_assets",
         "basis": None, "period": "2025-03", "expect": "not_found", "reason": "unknown_company", "note": "該当する会社が無い"},
        {"id": "company-ambiguous-name", "company": {"edinet_code": None, "name": "銀行"}, "item": "total_assets", "basis": None,
         "period": "2025-03", "expect": "not_found", "reason": "ambiguous_company", "expect_candidates": True,
         "note": "候補が複数＝推測で 1 社に決めず、候補を返して値は返さない"},
    ]
    return out


def main() -> int:
    # 対象の書類は既存の問から取る（取込でキャッシュが増えても問の母集団が動かない）。増やすときは --docids で足す
    have = EVAL / "exact_match.jsonl"
    docs = sorted({json.loads(l)["source"]["doc_id"] for l in have.read_text().splitlines() if l.strip()} if have.exists() else set())
    docs = sorted(set(docs) | {d.strip() for a in sys.argv[1:] if a.startswith("--docids=") for d in a.split("=", 1)[1].split(",")})
    if not docs:
        sys.exit("対象の書類が無い＝--docids=S100XXXX,… を指定")
    cs = [load(d) for d in docs]
    pos = [q for c in cs for q in positives(c)] + latest_default(cs)
    neg = list({q["id"]: q for q in negatives(cs)}.values())  # 2 書類ある会社は同じ負例が 2 回できる＝id で 1 つに
    assert len({q["id"] for q in pos}) == len(pos) and len({q["id"] for q in neg}) == len(neg), "id の重複"
    EVAL.mkdir(parents=True, exist_ok=True)
    for name, qs in (("exact_match.jsonl", pos), ("fail_closed.jsonl", neg)):
        (EVAL / name).write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in qs))
    print(f"{len(cs)} 社 → 正例 {len(pos)}・負例 {len(neg)} → {EVAL}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
