"""第 1d 便（遡り）の評価問の素材づくり（実装より先に立てる＝docs/第1d便_計画.md §3）。

期待値の取り方は make_candidates と同じ＝公式 CSV（type=5）と自前のパーサの 2 経路が一致した点だけ。
既存の exact_match.jsonl／fail_closed.jsonl には人手で足した問がある＝あちらは再生成せず、この便の問は別のファイルに置く。

  python -m companies.eval.make_backfill_candidates
出力：companies/data/eval/backfill.jsonl（正例）・backfill_fail_closed.jsonl（負例）＝A・C（遡りの取込が前提）と、
      period_basis.jsonl・period_basis_fail_closed.jsonl＝B（いまの置き場でも成り立つ＝遡りと別に配布できる）。**上書きする**。

問の型：
  A. 古い書類にしか載らない期＝最古の書類の四期前（書類を指定しない参照でもその書類の値）＋最古の書類の当期（書類を指定して）
  B. 連結の有無は決算期ごと（2026-09-28 実測）＝会社の最新の書類の宣言（DEI）ではなく、その期に連結の値があるか
     - 今は連結なしと宣言・5 期推移の古い期に連結の値がある（日本製麻 等）＝連結を指定すれば連結の値・指定が無ければ連結
     - 今は連結ありと宣言・古い期に連結の値が無い（六甲バター 等）＝連結を指定すれば no_consolidated_statements・指定が無ければ単体
  C. 取得範囲より前の期＝out_of_range（最古の書類の四期前の 1 年前）
  D. 遡及修正＝書類を指定しない参照は最新の書類の値（古い値は other_documents）・古い書類を指定すれば古い値（両方を公式 CSV で）
  E. 横断検索の過去の期＝その期を当期とする書類で計算（入力は単社の参照層で書類を固定して引き、式は本ファイルで計算）
★D・E は値の置き場（全量の取込）から選ぶ＝COMPANIES_STORE を全量の置き場にして回す
"""
from __future__ import annotations

import json
import sys

from companies.eval.make_candidates import EVAL, NONCON, ELEMENT_TO_KEY, TODAY, load, period_of, plain, positive

# A＝様式の違う古い書類（2016-10〜2017-06 提出＝API の取得範囲の最古の年）
OLDEST = {
    "S100AKEX": "トヨタ（米国基準）", "S100AF55": "鹿島（建設・日本基準）", "S100APSA": "三菱ＵＦＪ（銀行）",
    "S100AJZW": "参天製薬（IFRS・インスタンスが 2 個）", "S100ALK2": "東京海上（保険）", "S100AKPV": "野村（米国基準・証券）",
    "S100AI0Y": "日立（IFRS）", "S100AFIH": "ソニー（米国基準）", "S100AO1V": "青森放送（連結なし）", "S100ALNL": "第一生命（保険）",
    "S1009Z4L": "アサヒ（IFRS・12 月決算）", "S1008WMW": "大盛工業（7 月決算・当時は連結なし）",
}
# B＝連結の有無が期で変わる会社の書類
CONSOLIDATION = {
    "S100YFIL": "日本製麻（2026 年は連結なし・2022〜2025 年 3 月期は連結）", "S100YE3K": "ハビックス（同じ型）",
    "S100XSYQ": "六甲バター（2026 年は連結・2021〜2024 年 12 月期は連結なし）", "S100WRZI": "工藤建設（同じ型・6 月決算）",
    "S100XTWS": "カンロ（同じ型）",
}
PER_DOC = 4
TA = "jpcrp_cor:TotalAssetsSummaryOfBusinessResults"


def _instant(n: int) -> str:
    return ("CurrentYear" if n == 0 else f"Prior{n}Year") + "Instant"


def oldest_only(c: dict, note: str) -> list[dict]:
    out = []
    own = "" if c["consolidated"] else NONCON
    cands = sorted((el, ctx) for (el, ctx) in c["rows"] if el.startswith("jpcrp_cor:") and el.split(":")[1] in ELEMENT_TO_KEY
                   and plain(ctx) and ctx.startswith("Prior4Year") and ctx.endswith(NONCON) == bool(own)
                   and not ELEMENT_TO_KEY[el.split(":")[1]].startswith("average_"))
    used = set()
    for el, ctx in cands:
        key = ELEMENT_TO_KEY[el.split(":")[1]]
        if key in used:
            continue
        q = positive(c, el, ctx)
        if not q or q.get("accounting_standard") or "expected_companion" in q:
            continue
        used.add(key)
        q.update(id=q["id"] + "-oldest", pin=False,
                 note=f"{note}＝取得範囲の最古の書類の四期前＝この書類にしか載らない期（書類を指定しなくてもこの書類の値）")
        out.append(q)
        if len(out) >= PER_DOC:
            break
    cur = (TA, _instant(0) + own)
    if cur in c["rows"] and (q := positive(c, *cur)):
        q.update(id=q["id"] + "-pinned", note=f"{note}＝古い書類を指定して当期の値（後年の書類にも再掲される期）")
        out.append(q)
    return out


def before_coverage(c: dict) -> dict:
    p = min(period_of(c, x) for x in c["contexts"] if plain(x))  # 連結なしの会社は連結の context が無い＝最古の期を context から
    before = f"{int(p[:4]) - 1}{p[4:]}"
    return {"id": f"{c['edinet_code']}-before-coverage-{before}", "company": {"edinet_code": c["edinet_code"], "name": c["name"]},
            "item": "total_assets", "basis": None, "period": before, "expect": "not_found", "reason": "out_of_range",
            "note": "取得範囲の最古の書類の四期前より 1 年前＝収録外（近い期の値を返さない）"}


def consolidation(c: dict, note: str) -> tuple[list[dict], list[dict]]:
    """その書類の 5 期推移で、連結の値がある期・無い期を宣言（DEI）と突き合わせて問にする。"""
    pos, neg = [], []
    who = {"edinet_code": c["edinet_code"], "name": c["name"]}
    for n in range(4, 0, -1):
        con, non = (TA, _instant(n)), (TA, _instant(n) + NONCON)
        has_con = c["rows"].get(con, {}).get("value") not in (None, "", "－")
        has_non = c["rows"].get(non, {}).get("value") not in (None, "", "－")
        if not c["consolidated"] and has_con:  # 今は連結なし・この期は連結
            q = positive(c, *con)
            if q:
                q.update(id=q["id"] + "-con-period", pin=False,
                         note=f"{note}＝書類は連結なしと宣言するが、この期は連結の値がある＝連結を指定すれば連結の値（no_consolidated_statements にしない）")
                d = {**q, "id": q["id"] + "-default", "basis": None, "expected_basis": "consolidated",
                     "note": "basis の指定が無いとき＝その期に連結の値があれば連結（会社の最新の宣言で単体にしない）"}
                pos += [q, d]
            break
        if c["consolidated"] and has_non and not has_con:  # 今は連結・この期は連結なし
            per = period_of(c, non[1])  # 連結の context はその期に無いことがある＝期は単体の context から
            neg.append({"id": f"{c['edinet_code']}-total_assets-con-noncon-period-{per}", "company": who,
                        "item": "total_assets", "basis": "consolidated", "period": per, "expect": "not_found",
                        "reason": "no_consolidated_statements",
                        "note": f"{note}＝書類は連結ありと宣言するが、この期は連結の値が無い（連結を作成していなかった期）＝単体の値を連結として返さない"})
            q = positive(c, *non)
            if q:
                q.update(id=q["id"] + "-noncon-period-default", pin=False, basis=None, expected_basis="non_consolidated",
                         note="basis の指定が無いとき＝その期に連結の値が無ければ単体（連結を作成していなかった期）")
                pos.append(q)
            break
    return pos, neg


# D. 遡及修正（株式分割による 1 株当たりの値の組み替え 等）＝同じ期の値が古い書類と新しい書類で違う
N_RESTATED = 6


def restated() -> list[dict]:
    """値の置き場（全量）から、同じ期・同じ要素の値が書類で違う点を選び、両方の書類の公式 CSV で確かめて問にする
    ＝書類を指定しない参照は提出日が最新の書類の値（other_documents に古い値）・古い書類を指定すれば古い値。"""
    import hashlib
    from companies.core import store
    from companies.core.lookup import ELEMENT_KEY
    reg = store.registry()
    out = []
    for code in sorted(reg, key=lambda k: hashlib.sha1(f"restated-{k}".encode()).hexdigest()):
        if len(out) >= N_RESTATED * 2:
            break
        by = {}
        for f in store.facts_of(code):
            if f["element"] in ELEMENT_KEY and not f["dims"] and f["basis"] == ("consolidated" if reg[code]["consolidated"] else "non_consolidated"):
                by.setdefault((f["element"], f["period"]), {})[f["doc_id"]] = (f["submitted"], f["value"], f["context"])
        hit = next(((k, v) for k, v in sorted(by.items()) if len(set(x[1] for x in v.values())) > 1), None)
        if not hit:
            continue
        (el, period), docs = hit
        old_doc = min(docs, key=lambda d: (docs[d][0], d))
        new_doc = max(docs, key=lambda d: (docs[d][0], d))
        if docs[old_doc][1] == docs[new_doc][1]:
            continue
        qs = []
        for doc, pin in ((old_doc, True), (new_doc, False)):
            c = load(doc)
            q = positive(c, el, docs[doc][2])
            if not q or q.get("accounting_standard") or "expected_companion" in q:
                qs = []
                break
            q.update(id=q["id"] + ("-restated-old" if pin else "-restated-latest"), pin=pin)
            if not pin:
                q.update(expect_other_documents=True, note=f"遡及修正＝古い書類（{old_doc}）の値 {docs[old_doc][1]} と違う。書類を指定しない参照は提出日が最新の書類の値")
            else:
                q.update(note=f"遡及修正＝古い書類を指定すれば古い値（最新の書類 {new_doc} では {docs[new_doc][1]}）")
            qs.append(q)
        out += qs
    return out


# E. 横断検索の過去の期（period_from／period_to）＝その期を当期とする書類で計算する
SCREEN_OLD = ("S100AF55", "S100AJZW", "S100APSA", "S100AO1V")  # 鹿島・参天（IFRS）・三菱ＵＦＪ（銀行）・青森放送（連結なし）


def screen_old() -> tuple[list[dict], list[dict]]:
    from decimal import Decimal
    from companies.core import store
    from companies.eval.make_screen_candidates import _get, _periods, _top
    pos, neg = [], []
    for doc in SCREEN_OLD:
        code = next(k for k, c in store.registry().items() if doc in c["documents"])
        c = store.registry()[code]
        std = c["documents"][doc]["accounting_standard"]
        basis = "consolidated" if any(f["doc_id"] == doc and f["basis"] == "consolidated" for f in store.facts_of(code)) else "non_consolidated"
        ps = _periods(code, doc, basis)
        t, t1 = ps[0], ps[1]
        bottom = "profit_attributable_to_owners" if basis == "consolidated" else "net_income"
        bot, top = _get(code, bottom, t, doc, basis, std), _top(code, t, doc, basis, std)
        ta, ta1 = _get(code, "total_assets", t, doc, basis, std), _get(code, "total_assets", t1, doc, basis, std)
        base = {"company": {"edinet_code": code, "name": c["name"]}, "as_of": "2026-09-27", "basis": basis, "doc_id": doc, "period": t,
                "period_from": t, "period_to": t,
                "checked_by": "単社の参照層 lookup_company_facts（書類を固定）で入力を引き、式は本ファイルで計算＝式エンジンとは別の経路",
                "checked_at": TODAY, "note": "第 1d 便＝過去の期を指定した横断検索は、その期を当期とする書類（その年の有報）で計算する"}
        if bot and top and Decimal(top["value"]) > 0:
            pos.append({"id": f"{code}-net_margin-{t}-window", "kind": "value", "metric": "net_margin", **base,
                        "expected": {"inputs": {"bottom_line[t]": bot, "top_line[t]": top}, "value": str(Decimal(bot["value"]) / Decimal(top["value"]))}})
        if bot and ta and ta1:
            pos.append({"id": f"{code}-roa-{t}-window", "kind": "value", "metric": "roa", **base,
                        "expected": {"inputs": {"bottom_line[t]": bot, "total_assets[t]": ta, "total_assets[t-1]": ta1},
                                     "value": str(Decimal(bot["value"]) / ((Decimal(ta["value"]) + Decimal(ta1["value"])) / 2))}})
        before = f"{int(t[:4]) - 2}-01"
        neg.append({"id": f"{code}-window-before-coverage", "kind": "excluded", "metric": "roa", "company": {"edinet_code": code, "name": c["name"]},
                    "as_of": "2026-09-27", "period_from": before, "period_to": f"{int(t[:4]) - 2}-12", "reason": "no_period_in_window",
                    "note": "取得範囲の最古の書類より前の年を当期とする書類は無い＝近い年の書類で計算しない"})
    return pos, neg


def main() -> int:
    pos, neg = [], []
    for d, note in OLDEST.items():
        c = load(d)
        pos += oldest_only(c, note)
        neg.append(before_coverage(c))
    bpos, bneg = [], []
    for d, note in CONSOLIDATION.items():
        p, n = consolidation(load(d), note)
        if not p and not n:
            print(f"  問にならない書類: {d} {note}", file=sys.stderr)
        bpos += p
        bneg += n
    pos += restated()
    spos, sneg = screen_old()
    out = {"backfill.jsonl": pos, "backfill_fail_closed.jsonl": neg, "period_basis.jsonl": bpos, "period_basis_fail_closed.jsonl": bneg,
           "screen_backfill.jsonl": spos, "screen_backfill_fail_closed.jsonl": sneg}
    ids = [q["id"] for qs in out.values() for q in qs]
    assert len(set(ids)) == len(ids), "id の重複"
    for name, qs in out.items():
        for q in qs:
            q.setdefault("checked_at", TODAY)
        (EVAL / name).write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in qs))
    print(" ・".join(f"{k} {len(v)}" for k, v in out.items()) + f" → {EVAL}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
