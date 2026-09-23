"""セグメント別の値の参照層（第 1b 便）。企業×決算期のセグメント表を**表ごと**返す＝docs/第1b便_計画.md §3。

寄せないもの＝区分（会社の定義）・利益の物差し（要素のまま）・区分の足し算の関係。区分の種類（kind）は返す＝利用側が合計を
作るときに、調整額・全社・合計・会社が定義した小計を二重に数えないため。
fail-closed：数値が無いときは値を返さない。単一セグメントか記載の省略かは分類せず、注記から会社の文を 1 行引用する（quote）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from companies.core import store

AXIS = "OperatingSegmentsAxis"
STANDARD_PREFIXES = ("jpcrp_cor", "jppfs_cor", "jpigp_cor")
# 値が載っている欄＝要素名で決まる（2026-09-23 実測・422 書類）。ここに無い要素はセグメント情報の注記
SECTION = {"NumberOfEmployees": "employees", "AverageNumberOfTemporaryWorkers": "employees",
           "CapitalExpendituresOverviewOfCapitalExpendituresEtc": "capex",
           "ResearchAndDevelopmentExpensesResearchAndDevelopmentActivities": "research_and_development"}
SECTION_LABEL = {"segment_information": "セグメント情報（注記）", "employees": "従業員の状況", "capex": "設備投資等の概要",
                 "research_and_development": "研究開発活動"}
# 標準の区分の種類（会社が定義した区分は company_defined）
KIND = {"ReportableSegmentsMember": "reportable_total", "ReconcilingItemsMember": "reconciling",
        "CorporateSharedMember": "corporate", "TotalOfReportableSegmentsAndOthersMember": "total",
        "OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember": "other",
        "UnallocatedAmountsAndEliminationMember": "unallocated_and_elimination",
        "OtherReportableSegmentsMember": "other_reportable", "OtherOperatingSegmentsAxisMember": "other"}
KIND_NOTE = ("kind＝company_defined（会社が定義した区分。小計の区分もあり得る）／reportable_total（報告セグメント計）／other（その他）／"
             "reconciling（調整額）／corporate（全社）／unallocated_and_elimination（消去又は全社）／total（合計）。"
             "区分の足し算の関係は返さない＝合計を作るときは kind を見て二重に数えない")
LABELS = Path(__file__).resolve().parent / "segment_labels.json"


def section_of(element: str) -> str:
    prefix, _, local = element.partition(":")
    return SECTION.get(local, "segment_information") if prefix == "jpcrp_cor" else "segment_information"


def kind_of(member: str) -> str:
    prefix, _, local = member.partition(":")
    return KIND.get(local, "other_standard") if prefix in STANDARD_PREFIXES else "company_defined"


@lru_cache(maxsize=1)
def standard_labels() -> dict[str, str]:
    """標準要素の公式ラベル（公式 CSV の「項目名」から作った表＝ops/build_segment_labels.py）。会社が定義した要素は lab.xml のラベル。"""
    return json.loads(LABELS.read_text()) if LABELS.exists() else {}


def _source(co: dict, doc_id: str, doc: dict) -> dict:
    return {"provider": "EDINET", "doc_type": "有価証券報告書", "doc_id": doc_id, "submitted": doc["submitted"],
            "url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}",
            "citation": f"出典：EDINET 有価証券報告書（{co['name']}・{doc['submitted']} 提出・書類管理番号 {doc_id}）／XBRL から抽出"}


def _fact(f: dict) -> dict:
    label = f["element_label"] or standard_labels().get(f["element"])
    return {"member": f["member"], "element": f["element"], "label": label, "section": f["section"], "value": f["value"],
            "unit": f["unit"], "decimals": f["decimals"], "context": f["context"]}


def lookup_segments(company: str, period: str, *, basis: str | None = None, doc_id: str | None = None) -> dict:
    from companies.core.lookup import BASES, PERIOD, _miss, find_company  # 発見層と理由コードは第 1 便と共通
    if basis is not None and basis not in BASES:
        return _miss("bad_request", hint=f"basis は {BASES} のいずれか（省くと、連結を作成している会社は連結・していない会社は単体）")
    if not PERIOD.fullmatch(period or ""):
        return _miss("bad_period", hint="period は決算期末の YYYY-MM（例＝2025-03）。年度表記は読み替えない")
    found = find_company(company)
    if not found["found"]:
        return _miss(found["reason"], candidates=found["candidates"], **({"hint": found["hint"]} if "hint" in found else {}))
    co = found["company"]
    if basis == "consolidated" and not co["consolidated"]:
        return _miss("no_consolidated_statements", company=co, hint="連結財務諸表を作成していない会社。basis=non_consolidated で引く")
    basis = basis or ("consolidated" if co["consolidated"] else "non_consolidated")
    data = store.segments_of(co["edinet_code"])
    docs = data["docs"]
    if doc_id is not None and doc_id not in docs:
        return _miss("unknown_document", company=co, hint="この会社の収録書類に無い書類管理番号",
                     documents=sorted((d["submitted"], k) for k, d in docs.items()))
    # その決算期を当期か前期として載せている書類（セグメントの値は各書類に 2 期だけ）
    covering = sorted((k for k, d in docs.items() if period in d["periods"].values() and (doc_id is None or k == doc_id)),
                      key=lambda k: (docs[k]["submitted"], k))
    if not covering:
        periods = sorted({p for d in docs.values() for p in d["periods"].values()})
        return _miss("out_of_range", company=co, available_periods=periods,
                     hint="セグメントの値は各書類に当期・前期の 2 期だけ。収録の無い決算期＝近い期や経営指標の期から推して返さない")
    rows = [f for f in data["facts"] if f["period"] == period and f["basis"] == basis and f["doc_id"] in covering]
    # セグメント情報の注記は、連結を作成している会社では連結にしか無い＝その会社の単体（提出会社）のセグメント別の値は
    # 従業員の状況の欄にだけ出る（構造）。その場合は注記の有無を問わず、載っている値を返す
    parent_only = basis == "non_consolidated" and co["consolidated"]
    with_seg = [k for k in covering if any(f["doc_id"] == k and (parent_only or f["section"] == "segment_information") for f in rows)]
    if not with_seg:
        latest = covering[-1]
        doc = docs[latest]
        other = [_fact(f) for f in rows if f["doc_id"] == latest]
        note = doc["notes"].get(basis)
        base = {"company": co, "basis": basis, "period": period, "source": _source(co, latest, doc),
                "other_sections": other, "section_labels": SECTION_LABEL}
        # 注記に数値のタグが無い（本文の表だけ）か、数値そのものが無い（単一セグメント・記載の省略）か。分類はしない＝根拠は構造と会社の文だけ：
        #  - 米国基準で注記が XBRL に無い → not_tagged
        #  - 注記に会社の文（単一・省略 等）がある → no_segment_figures（その文を quote で示す）
        #  - 会社の文が無く、注記に「報告セグメントごとの…」の表の見出しがあるか、他の欄で 2 つ以上の区分をタグ付けしている → not_tagged
        #    （2026-09-23 母集団で発見＝日本基準でも注記の表だけタグの無い書類がある。三菱製鋼・中日本鋳工・ハニーズ）
        segs = {f["member"] for f in rows if f["doc_id"] == latest and kind_of(f["member"]) in ("company_defined", "other", "other_reportable")}
        quote = (note or {}).get("quote")
        if (doc.get("accounting_standard") == "US GAAP" and not note) or (
                note and not quote and (note.get("segment_tables") or len(segs) >= 2)):
            return _miss("not_tagged", **base,
                         hint="この書類のセグメント情報の注記の表に数値のタグが無い（本文の表だけ）＝値を返せない。書類の URL で本文を見る。"
                              "タグのある欄（従業員の状況・設備投資・研究開発）は other_sections")
        return _miss("no_segment_figures", **base, quote=quote,
                     hint="この書類のセグメント情報の注記に数値が無い（単一セグメント・記載の省略 等）。理由は quote の会社の文のとおり＝分類しない"
                          "（quote が空なら、注記の本文を書類の URL で見る）")
    use = with_seg[-1]  # 提出日が最新の書類
    hits = [f for f in rows if f["doc_id"] == use]
    seen = {}
    for f in hits:
        seen.setdefault(f["member"], {"member": f["member"], "label": f["member_label"], "kind": kind_of(f["member"])})
    return {"found": True, "company": co, "basis": basis, "period": period,
            "segments": sorted(seen.values(), key=lambda s: (s["kind"] != "company_defined", s["member"])),
            "facts": [_fact(f) for f in hits],
            "kind_note": KIND_NOTE, "section_labels": SECTION_LABEL,
            **({"basis_note": "連結を作成している会社の単体（提出会社）＝セグメント情報の注記は連結にだけあり、提出会社のセグメント別の値は従業員の状況の欄にだけ載る"}
               if parent_only else {}),
            "source": {**_source(co, use, docs[use]),
                       "other_documents": [(docs[k]["submitted"], k) for k in with_seg if k != use]},
            "note": "値は公表どおりの文字列。同じ決算期の値は翌年の書類にも前期として載り、組み替え・遡及修正で変わり得る"
                    "（other_documents の書類を doc_id で指定すればその書類の値）",
            "license": {"grade": "○", "terms": "公共データ利用規約（PDL1.0）＝出典の明記と加工の明記"}}


def unlabeled_members() -> list[str]:
    """値の置き場の全件で、会社が定義した区分のうちラベルが空のもの（0 件であること）。"""
    out = set()
    for code in store.segment_codes():
        for f in store.segments_of(code)["facts"]:
            if kind_of(f["member"]) == "company_defined" and not f["member_label"]:
                out.add(f"{code} {f['member']}")
    return sorted(out)
