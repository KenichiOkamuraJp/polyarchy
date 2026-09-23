"""発見層（企業の同定）と参照層（値の完全一致参照）。DB は事実を返すことに徹する。

fail-closed：
- 企業が同定できない・候補が複数 → 値を返さない（候補が複数なら候補を返す＝推測で 1 社に決めない）。
- その会社・その連結／単体の別・その決算期に項目が無い → found=false。**隣の項目・別の basis・近い期で埋めない**。
  代わりに、その会社が開示している項目の一覧（値なし）を返す＝選ぶのは利用側。
"""
from __future__ import annotations

import re
import unicodedata

from companies.core import store
from companies.core.items import BASES, COMPANION, ITEMS, STANDARDS, TOP_LINE, TOP_LINE_LABEL, standard_of

ELEMENT_KEY = {f"jpcrp_cor:{el}": k for k, (_, els) in ITEMS.items() for el in els}  # ラベルは読むときに語彙から引く（語彙を足しても取り込み直さない）
PERIOD = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
MAX_CANDIDATES = 20


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+|株式会社|\(株\)|㈱", "", s).lower()


def _brief(c: dict) -> dict:
    out = {k: c.get(k) for k in ("edinet_code", "sec_code", "name", "accounting_standard", "consolidated", "fiscal_year_end")}
    former = [n for n in c.get("names", []) if n != c.get("name")]
    return {**out, "former_names": former} if former else out


def _names(c: dict) -> list[str]:
    return [_norm(n) for n in [*c.get("names", []), c.get("name"), c.get("name_en")] if n]


def find_company(query: str) -> dict:
    reg, q = store.registry(), (query or "").strip()
    if not q:
        return {"found": False, "company": None, "candidates": [], "reason": "unknown_company"}
    if re.fullmatch(r"[Ee]\d{5}", q):
        hits = [c for k, c in reg.items() if k == q.upper()]
    elif re.fullmatch(r"\d{4}0?", q):
        hits = [c for c in reg.values() if c.get("sec_code") == q[:4]]
    else:
        n = _norm(q)
        exact = [c for c in reg.values() if n in _names(c)]
        hits = exact or [c for c in reg.values() if n and any(n in x for x in _names(c))]
    if len(hits) == 1:
        return {"found": True, "company": _brief(hits[0]), "candidates": [], "reason": None}
    if not hits:
        return {"found": False, "company": None, "candidates": [], "reason": "unknown_company",
                "hint": f"収録は有価証券報告書の提出会社 {len(reg)} 社。EDINET コード・証券コード・社名で指定する"}
    return {"found": False, "company": None, "reason": "ambiguous_company", "n_candidates": len(hits),
            "candidates": [_brief(c) for c in sorted(hits, key=lambda c: c["edinet_code"])[:MAX_CANDIDATES]]}


def _miss(reason: str, **extra) -> dict:
    return {"found": False, "reason": reason, **extra}


def _disclosed(facts, basis: str, period: str) -> list[dict]:
    """その会社がその連結／単体の別・その決算期に開示している項目（値は含めない）。"""
    el_key = {f"jpcrp_cor:{el}": k for k, (_, els) in ITEMS.items() for el in els}
    seen = {}
    for f in facts:
        if f["basis"] == basis and f["period"] == period and not f["dims"]:
            k = el_key.get(f["element"])
            seen[f["element"]] = {"item": k, "element": f["element"], "label": ITEMS[k][0] if k else f["label"], "basis": basis}
    return sorted(seen.values(), key=lambda d: (d["item"] is None, d["item"] or d["element"]))


def lookup_company_facts(company: str, *, item: str | None = None, element: str | None = None,
                         period: str, basis: str | None = None, doc_id: str | None = None,
                         accounting_standard: str | None = None) -> dict:
    """doc_id を省くと提出日が最新の書類の値。同じ決算期の値は後年の書類に再掲され、遡及修正で変わり得る＝書類を指定すればその書類の値。"""
    if (item is None) == (element is None):
        return _miss("bad_request", hint="item（語彙のキー）か element（要素 ID＝会社が定義した項目）のどちらか一方を指定する")
    if item is not None and item not in ITEMS:
        return _miss("unknown_item", hint="語彙に無い項目。派生値は計算しない＝構成する項目を引いて利用側で計算する",
                     items={k: label for k, (label, _) in ITEMS.items()})
    if basis is not None and basis not in BASES:
        return _miss("bad_request", hint=f"basis は {BASES} のいずれか（省くと、連結を作成している会社は連結・していない会社は単体）")
    if accounting_standard is not None and accounting_standard not in STANDARDS:
        return _miss("bad_request", hint=f"accounting_standard は {STANDARDS} のいずれか")
    if not PERIOD.fullmatch(period or ""):
        return _miss("bad_period", hint="period は決算期末の YYYY-MM（例＝2025-03）。年度表記は読み替えない")
    found = find_company(company)
    if not found["found"]:
        return _miss(found["reason"], candidates=found["candidates"], **({"hint": found["hint"]} if "hint" in found else {}))
    co = found["company"]
    if basis == "consolidated" and not co["consolidated"]:
        return _miss("no_consolidated_statements", company=co, hint="連結財務諸表を作成していない会社。basis=non_consolidated で引く")
    basis = basis or ("consolidated" if co["consolidated"] else "non_consolidated")
    facts = store.facts_of(co["edinet_code"])
    if doc_id is not None:
        facts = tuple(f for f in facts if f["doc_id"] == doc_id)
        if not facts:
            return _miss("unknown_document", company=co, hint="この会社の収録書類に無い書類管理番号",
                         documents=sorted({(f["submitted"], f["doc_id"]) for f in store.facts_of(co["edinet_code"])}))
    periods = sorted({f["period"] for f in facts})
    if period not in periods:
        return _miss("out_of_range", company=co, available_periods=periods, hint="収録の無い決算期。近い期の値は返さない")
    wanted = {f"jpcrp_cor:{el}" for el in ITEMS[item][1]} if item else {element}
    hits = [f for f in facts if f["element"] in wanted and f["basis"] == basis and f["period"] == period and not f["dims"]]
    if accounting_standard is not None and item:
        hits = [f for f in hits if standard_of(f["element"]) == accounting_standard]
    if not hits:
        other = "non_consolidated" if basis == "consolidated" else "consolidated"
        alts, guide = _disclosed(facts, basis, period), {}
        if item in TOP_LINE:  # 最上段の収益＝この会社が代わりに開示している仲間の項目を名指しで案内する（値は返さない）
            sug = [a for a in alts if a["item"] in TOP_LINE] + [
                a for a in alts if a["item"] is None and a["label"] and any(w in a["label"] for w in TOP_LINE_LABEL)
                and not any(x in a["label"] for x in ("利益", "損失", "率", "１株", "1株"))]
            if sug:
                why = ("IFRS の会社は「売上高」ではなく「売上収益」で開示することが多い。" if co["accounting_standard"] == "IFRS" else
                       "持株会社・金融・建設・小売などは「売上高」ではなく営業収益・経常収益・完成工事高・各社が定義した項目で開示する。")
                guide = {"suggest": [{**a, "how": (f"item={a['item']}" if a["item"] else f"element={a['element']}") + " で引き直す"} for a in sug],
                         "suggest_note": why + "この会社が最上段の収益として開示しているのは suggest の項目（別の概念＝「売上高」として扱わない）"}
        if element is not None:  # 接頭辞（名前空間）違い＝要素 ID の完全一致ではないので値は返さない。名前が一致する要素が 1 つだけなら案内する
            local = element.rpartition(":")[2]
            same = [a for a in alts if a["element"].rpartition(":")[2] == local and a["element"] != element]
            if len(same) == 1:
                guide = {"suggest": [{**same[0], "how": f"element={same[0]['element']} で引き直す"}],
                         "suggest_note": "element は接頭辞（名前空間）つきの要素 ID の完全一致で引く。接頭辞を除いた名前が一致する要素がこの 1 つだけある"}
        return _miss("item_not_disclosed", company=co, basis=basis, period=period, **guide,
                     disclosed_in_other_basis=any(f["element"] in wanted and f["basis"] == other and f["period"] == period for f in facts),
                     alternatives=alts,
                     hint="この会社はこの項目をこの連結／単体の別では開示していない。別の basis や隣の項目の値では埋めない＝"
                          "alternatives（開示されている項目の一覧）から選んで引き直す")
    others = []
    if len({f["element"] for f in hits}) > 1 and item:
        # 同じ決算期に会計基準の違う値が並ぶ（移行年・日本基準の表の併記）。既定は**その書類が宣言する会計基準**の値を返し、
        # もう一方の基準の値を other_standards に必ず添える（2026-09-21 決定＝利便のため既定を置くが、もう一方を隠さない）。
        newest = max(hits, key=lambda f: (f["submitted"], f["doc_id"]))["doc_id"]
        declared = (store.registry()[co["edinet_code"]].get("documents", {}).get(newest) or {}).get("accounting_standard")
        mine = [f for f in hits if f["doc_id"] == newest and standard_of(f["element"]) == declared]
        if len({f["element"] for f in mine}) == 1:
            seen = {}
            for g in sorted(hits, key=lambda f: (f["submitted"], f["doc_id"])):
                if g["element"] != mine[0]["element"]:
                    seen[g["element"]] = {"accounting_standard": standard_of(g["element"]), "element": g["element"], "value": g["value"],
                                          "unit": g["unit"], "doc_id": g["doc_id"]}
            others = list(seen.values())
            hits = [f for f in hits if f["element"] == mine[0]["element"]]
    if len({f["element"] for f in hits}) > 1:
        # 同じ決算期に会計基準の違う値が並ぶ。IFRS へ移行した会社の移行年（US GAAP と IFRS）のほか、IFRS の表と日本基準の表を
        # 併記する会社がある（2026-09-21 実測＝約 2%）。どちらも事実＝片方を黙って選ばない。両方を会計基準つきで示し、
        # accounting_standard（または element）の指定で引き直させる。
        latest = {}
        for f in sorted(hits, key=lambda f: (f["submitted"], f["doc_id"])):
            latest[f["element"]] = {"accounting_standard": standard_of(f["element"]), "element": f["element"], "label": f["label"],
                                    "value": f["value"], "unit": f["unit"], "doc_id": f["doc_id"]}
        return _miss("ambiguous_item", company=co, basis=basis, period=period, competing=list(latest.values()),
                     hint="この決算期には、同じ項目の値が会計基準ごとに複数開示されている。どちらも開示された値＝"
                          "accounting_standard（Japan GAAP／IFRS／US GAAP）か element を指定して引き直す")
    f = max(hits, key=lambda f: (f["submitted"], f["doc_id"]))  # 提出日が最新の書類の値
    extra = {}
    if item in COMPANION:  # 年と月の 2 要素で開示する会社＝対の値を必ず添える（無ければ無いと書く）
        els = {f"jpcrp_cor:{el}" for el in ITEMS[COMPANION[item]][1]}
        pair = [g for g in facts if g["element"] in els and g["basis"] == basis and g["period"] == period and g["doc_id"] == f["doc_id"]]
        extra["companion"] = {"item": COMPANION[item], "label": ITEMS[COMPANION[item]][0], "value": pair[0]["value"] if pair else None,
                              "note": "年と月に分けて開示する会社がある＝両方で 1 つの値（例＝40 年と 5 月＝40 歳 5 か月）。value が null なら月の開示は無い"}
    if others:
        extra["other_standards"] = others
        extra["note"] = ("この決算期には会計基準ごとに複数の値が開示されている。返したのは書類が宣言する会計基準の値＝"
                         "other_standards にもう一方の値がある（accounting_standard を指定すればその基準の値を返す）")
    label = ITEMS[ELEMENT_KEY[f["element"]]][0] if f["element"] in ELEMENT_KEY else f["label"]
    return {"found": True, **extra, "accounting_standard": standard_of(f["element"]) if f["element"].startswith("jpcrp_cor:") else co["accounting_standard"], "company": co, "item": item, "item_label": ITEMS[item][0] if item else None,
            "element": f["element"], "label": label, "basis": f["basis"], "period": f["period"], "period_end": f["period_end"],
            "value": f["value"], "unit": f["unit"], "decimals": f["decimals"],
            "source": {"provider": "EDINET", "doc_type": "有価証券報告書", "doc_id": f["doc_id"], "submitted": f["submitted"],
                       "context": f["context"],
                       "other_documents": sorted({(g["submitted"], g["doc_id"], g["value"]) for g in hits if g["doc_id"] != f["doc_id"]}),
                       "url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{f['doc_id']}",
                       "citation": f"出典：EDINET 有価証券報告書（{co['name']}・{f['submitted']} 提出・書類管理番号 {f['doc_id']}）／XBRL から抽出"},
            "license": {"grade": "○", "terms": "公共データ利用規約（PDL1.0）＝出典の明記と加工の明記"}}
