"""地域別の欄の参照層（第 1b 便②）。企業×決算期の地域別の欄を、段落と表の順にセル単位で公表どおりに返す＝docs/第1b便_計画.md §6。

寄せないもの＝地域（会社の区分）・国内／海外・単位・何の値の表か・「うち」の足し算の関係。表の形は会社ごとに違う＝利用側が read_note に沿って読む。
fail-closed：欄はあるが表が無い → omitted＋会社の文（型は分類しない）／欄が無い → not_tagged。
取込用の依存（lxml）は import しない＝写しは取込（ingest/regions.py）で済ませ、ここは値の置き場を読むだけ。
"""
from __future__ import annotations

import re

from companies.core import store

SECTION_LABEL = {"revenue": "地域ごとの情報（売上高・日本基準）", "property_plant_and_equipment": "地域ごとの情報（有形固定資産・日本基準）",
                 "geographic_areas_ifrs": "地域別に関する情報（IFRS＝売上収益・非流動資産を 1 つの欄に）"}
# 読み方の案内（2026-09-23 棚卸し＝記録/地域別の表の棚卸し_2026-09-23.md の型から）
READ_NOTE = (
    "content は欄の中身を原典の順に並べたもの（text＝段落・table＝表・omitted＝80 字を超える段落のうち金額・比率を含まない文を省いた箇所〔字数のみ〕）。"
    "表は rows＝行の並び・各行のセルは HTML の並びのまま。結合セルは展開していない＝rowspan／colspan のあるセルは複数の行・列を占める 1 つのセル"
    "（値は 1 回だけ数える。展開して読むと同じ値が 2 回出て二重に数える）。セルの文字列は公表どおり（改行は \\n）。"
    "セルの中に表が入れ子になっている会社がある＝そのセルは text（中身を行に分けたもの）と tables（入れ子の表の行の並び）を持つ。"
    "単位は会社ごとに書く場所が違う＝表の前の段落・表の上段の行・見出しの括弧（日本(百万円)）・値の末尾（2,077,642千円）のいずれか。欄に単位が無く、同じ注記の別の欄（売上高の欄の表の上）にだけ書く会社もある。"
    "期は日本基準では欄ごと（context・period）、IFRS は 1 つの欄の表に前期と当期の列が並ぶ（列の見出しで読む・period_in_columns）。"
    "何の値の表か（売上高・売上収益・非流動資産・有形固定資産）は表の前の段落や表の見出しで読む＝section は要素名で決まり、中身とずれることがある"
    "（売上高の欄に有形固定資産の表を置く会社がある＝欄の中の見出しの文を優先する）。IFRS は 1 つの欄に表が 2〜4 つ（売上収益と非流動資産・前期と当期）、"
    "1 つの表に両方が並ぶこともある。「うち」「内、」「(うち米国)」、括弧の値（419,075(403,098) の括弧の中）は内数＝合計に足さない。"
    "「計」「小計」「海外売上収益」は小計、「消去」「全社」は調整の行・列。地域の区分・国内／海外への寄せ・比率は返さない（寄せない）"
    "＝海外比率などを示すなら、使ったセル（本邦の行・列と合計）を添えて派生値と明記する。表の外の文に地域の値があることがある（text の文）。"
)


def _source(co: dict, doc_id: str, doc: dict) -> dict:
    return {"provider": "EDINET", "doc_type": "有価証券報告書", "doc_id": doc_id, "submitted": doc["submitted"],
            "url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}",
            "citation": f"出典：EDINET 有価証券報告書（{co['name']}・{doc['submitted']} 提出・書類管理番号 {doc_id}）／XBRL のテキストブロックから表を写した"}


UNIT_ONLY = re.compile(r"[（(]?単位[:：][^）)]*[）)]?")


def _is_filler(text: str) -> bool:
    """表のセルのうち、値でも見出しでもないもの＝空・単位だけ・文（「。」を含む＝省略・該当なしの文）。"""
    s = re.sub(r"\s+", "", text)
    return not s or "。" in s or bool(UNIT_ONLY.fullmatch(s))


def _is_value_table(t: dict) -> bool:
    """値の表か。省略・該当なしの文を枠なしの 1 セルの表に入れる会社・文の後に空のセルだけの表を置く会社がある
    （PDF では段落に見える＝2026-09-26 目視で発見・母集団で 6 社）＝セルが空・単位・文だけの表は表として数えない。"""
    return any(not _is_filler(c["text"]) for row in t["rows"] for c in row)


def _has_table(sec: dict) -> bool:
    return any(x["type"] == "table" and _is_value_table(x) for x in sec["content"])


def _content(sec: dict) -> list[dict]:
    """値の表が 1 つも無い欄は、表に入った文を段落として返す（空のセルは落とす・文字列は公表どおり）＝表があるように見せない。
    値の表がある欄はそのまま（単位だけの表も表の番号も原典どおり）。"""
    if _has_table(sec):
        return sec["content"]
    out = []
    for x in sec["content"]:
        if x["type"] == "table":
            out.extend({"type": "text", "text": c["text"]} for row in x["rows"] for c in row if c["text"].strip())
        else:
            out.append(x)
    return out


def _for_period(data: dict, doc_id: str, period: str, basis: str) -> list[dict]:
    """その書類で、その決算期・basis の欄。IFRS の欄は当期の 1 つに 2 期が並ぶ＝前期を問われたら当期の欄を返す（period_in_columns）。"""
    prior = data["docs"][doc_id]["periods"].get("prior")
    out = []
    for s in data["sections"]:
        if s["doc_id"] != doc_id or s["basis"] != basis:
            continue
        if s["period"] == period:
            out.append({**s, "period_in_columns": s["section"] == "geographic_areas_ifrs"})
        elif s["section"] == "geographic_areas_ifrs" and period == prior and s["context"].startswith("CurrentYear"):
            out.append({**s, "period_in_columns": True})
    return out


def _section(s: dict) -> dict:
    return {"element": s["element"], "section": s["section"], "section_label": SECTION_LABEL[s["section"]], "context": s["context"],
            "period": s["period"], "period_end": s["period_end"], "period_in_columns": s["period_in_columns"],
            "has_table": _has_table(s), "content": _content(s)}


def lookup_regions(company: str, period: str, *, basis: str | None = None, doc_id: str | None = None) -> dict:
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
    data = store.regions_of(co["edinet_code"])
    docs = data["docs"]
    if doc_id is not None and doc_id not in docs:
        return _miss("unknown_document", company=co, hint="この会社の収録書類に無い書類管理番号",
                     documents=sorted((d["submitted"], k) for k, d in docs.items()))
    covering = sorted((k for k, d in docs.items() if period in d["periods"].values() and (doc_id is None or k == doc_id)),
                      key=lambda k: (docs[k]["submitted"], k))
    if not covering:
        return _miss("out_of_range", company=co, available_periods=sorted({p for d in docs.values() for p in d["periods"].values()}),
                     hint="地域別の欄は各書類に当期・前期の 2 期だけ。収録の無い決算期＝近い期の欄から推して返さない")
    with_sec = [k for k in covering if _for_period(data, k, period, basis)]
    if not with_sec:
        latest = covering[-1]
        others = sorted({s["basis"] for s in data["sections"] if s["doc_id"] == latest} - {basis})
        return _miss("not_tagged", company=co, basis=basis, period=period, source=_source(co, latest, docs[latest]),
                     hint="この書類に地域別の欄（地域ごとの情報の要素）が無い＝米国基準（注記が XBRL に無い）や、地域の要素でタグ付けしていない書類・関連情報ごと記載を省略している書類（単一セグメント 等）。"
                          "書類の URL で本文を見る" + (f"（{'・'.join(others)} の欄はある＝basis で引き直す）" if others else ""))
    use = with_sec[-1]  # 提出日が最新の書類
    secs = [_section(s) for s in _for_period(data, use, period, basis)]
    base = {"company": co, "basis": basis, "period": period, "sections": secs, "read_note": READ_NOTE,
            "source": {**_source(co, use, docs[use]), "other_documents": [(docs[k]["submitted"], k) for k in with_sec if k != use]},
            "license": {"grade": "○", "terms": "公共データ利用規約（PDL1.0）＝出典の明記と加工の明記"}}
    if not any(s["has_table"] for s in secs):
        quotes = [{"section": s["section"], "context": s["context"], "text": " ".join(x["text"] for x in s["content"] if x["type"] == "text")}
                  for s in secs]
        return _miss("omitted", **base, quotes=quotes,
                     hint="地域別の欄はあるが表が無い（本邦が 90% 超・本邦以外に無い 等＝理由は quotes の会社の文のとおり。型は分類しない）")
    return {"found": True, **base,
            "note": "表は公表どおりの文字列。同じ決算期の欄は翌年の書類にも前期として載り、組み替え・遡及修正で変わり得る"
                    "（other_documents の書類を doc_id で指定すればその書類の欄）"}


def _count_tables(tables: list[dict]) -> int:
    """表の数（セルの中に入れ子になった表を含む）。"""
    return sum(1 + _count_tables([n for row in t["rows"] for c in row for n in c.get("tables", [])]) for t in tables)


def unreadable_sections() -> list[str]:
    """値の置き場の全件で、写しが原典と合わない欄（原典の表の数と写した表の数が違う・文字列が null のセル）。0 件であること。"""
    out = []
    for code in store.region_codes():
        for s in store.regions_of(code)["sections"]:
            tables = [x for x in s["content"] if x["type"] == "table"]
            n = _count_tables(tables)
            if n != s["source_tables"]:
                out.append(f"{code} {s['doc_id']} {s['element']} {s['context']}: 表 {n}（原典 {s['source_tables']}・入れ子を含む）")
            elif any(c.get("text") is None for t in tables for row in t["rows"] for c in row):
                out.append(f"{code} {s['doc_id']} {s['element']} {s['context']}: 文字列が null のセル")
    return out
