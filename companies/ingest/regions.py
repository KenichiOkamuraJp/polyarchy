"""地域別の欄（第 1b 便②）の取込＝テキストブロックの HTML を、段落と表の順に、セル単位で公表どおりに写す（docs/第1b便_計画.md §6）。

欄＝日本基準 `RevenuesFromExternalCustomersInformationForEachRegionTextBlock`・`PropertyPlantAndEquipmentInformationForEachRegionTextBlock`、
IFRS `InformationAboutGeographicalAreasIFRSTextBlock`。インスタンス（.xbrl）のテキストブロックを読む（評価問は本文の inline XBRL から＝別経路）。

写し方：
- セルの文字列＝セルの中を行に分け（p・div・br 等の境目）、各行の空白（半角・タブ・改行・nbsp）の連なりを 1 つの半角空白にして前後の空白
  （全角空白を含む）を落とし、空の行を捨てて "\\n" でつなぐ。**結合セルは展開しない**（rowspan／colspan は 2 以上のときだけ添える）。
- 段落＝表の外の行（同じ規則）。表のある欄では 80 字以下は全文・80 字を超える段落は「。」で区切った文のうち金額・比率を含む文だけ全文、
  それ以外は {"type": "omitted", "chars": 字数}（原文は丸ごと再配布しない。表の外にしか無い値＝日立の米国・中国は拾う）。
  **表の無い欄は全文**（会社が表を載せない理由の文＝引用。短い）。
- 国内・海外への寄せ・単位の解釈・行と列の意味づけはしない。
"""
from __future__ import annotations

import re

from lxml import html as lhtml

ELEMENTS = {"RevenuesFromExternalCustomersInformationForEachRegionTextBlock": "revenue",
            "PropertyPlantAndEquipmentInformationForEachRegionTextBlock": "property_plant_and_equipment",
            "InformationAboutGeographicalAreasIFRSTextBlock": "geographic_areas_ifrs"}
LINE_BREAK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
AMOUNT = re.compile(r"\d{1,3}(,\d{3})+|\d+(\.\d+)?\s*(百万円|千円|億円|円|％|%|百万米ドル|千米ドル|米ドル|ドル)")
SHORT = 80
NONCON = "_NonConsolidatedMember"


def _tag(e) -> str:
    return e.tag.split("}")[-1].lower() if isinstance(e.tag, str) else ""


def _clean(line: str) -> str:
    return re.sub(r"[ \t\r\n\xa0]+", " ", line).strip(" \t\r\n\xa0　")


def lines_of(el) -> list[str]:
    """要素の中の文字列を行に分ける（行の境目＝p・div・br 等）。"""
    lines, cur = [], []

    def flush():
        lines.append("".join(cur)); cur.clear()

    def walk(e, top=False):
        br = not top and _tag(e) in LINE_BREAK
        if br:
            flush()
        if e.text:
            cur.append(e.text)
        for c in e:
            if isinstance(c.tag, str):
                walk(c)
            if c.tail:
                cur.append(c.tail)
        if br:
            flush()
    walk(el, top=True)
    flush()
    return [x for x in (_clean(l) for l in lines) if x]


def _paragraph(text: str, abridge: bool) -> list[dict]:
    if not abridge or len(text) <= SHORT:
        return [{"type": "text", "text": text}]
    parts = text.split("。")
    out: list[dict] = []
    for i, s in enumerate(parts):
        s = s + "。" if i < len(parts) - 1 else s
        if not s:
            continue
        if AMOUNT.search(s):
            out.append({"type": "text", "text": s})
        elif out and out[-1]["type"] == "omitted":
            out[-1]["chars"] += len(s)
        else:
            out.append({"type": "omitted", "chars": len(s)})
    return out


def _own(table, tag: str):
    """その表に直接属する要素（入れ子の表の中は除く）。"""
    for e in table.iter():
        if _tag(e) == tag and next((a for a in e.iterancestors() if _tag(a) == "table"), None) is table:
            yield e


def _rows(table) -> list[list[dict]]:
    """行の並び。セルの中に表が入れ子になっている会社がある（マックス＝「北米・中南米／うち米国」を 1 セルの中の表で書く）
    ＝そのセルは text（中身を行に分けた文字列）に加えて tables（入れ子の表の行の並び・同じ形）を持つ。"""
    rows = []
    for tr in _own(table, "tr"):
        row = []
        for td in tr:
            if _tag(td) not in ("td", "th"):
                continue
            cell = {"text": "\n".join(lines_of(td))}
            for k in ("rowspan", "colspan"):
                try:
                    n = int(td.get(k) or 1)
                except ValueError:
                    n = 1
                if n >= 2:
                    cell[k] = n
            nested = [t for t in td.iter() if _tag(t) == "table" and next(a for a in t.iterancestors() if _tag(a) in ("td", "th")) is td]
            if nested:
                cell["tables"] = [{"rows": _rows(t)} for t in nested]
            row.append(cell)
        rows.append(row)
    return rows


def content_of(block_html: str) -> tuple[list[dict], int]:
    """欄の中身（段落と表を順に）と、原典の表の数（入れ子を含む `<table` の数＝写しの検算に使う）。"""
    root = lhtml.fragment_fromstring(block_html, create_parent="div")
    has_table = any(_tag(e) == "table" for e in root.iter())
    paras: list[tuple[str, str | list]] = []

    def walk(e):
        for c in e:
            if not isinstance(c.tag, str):
                continue
            t = _tag(c)
            if t == "table":
                paras.append(("table", _rows(c)))
            elif any(_tag(x) == "table" for x in c.iter() if x is not c):
                walk(c)
            elif t in LINE_BREAK:
                for line in lines_of(c):
                    paras.append(("text", line))
            else:
                walk(c)
    walk(root)
    content: list[dict] = []
    for kind, x in paras:
        if kind == "table":
            content.append({"type": "table", "rows": x})
        else:
            content.extend(_paragraph(x, abridge=has_table))
    return content, len(re.findall(r"<table\b", block_html, flags=re.I))


def region_parts(inst: dict, doc_id: str, submitted: str, consolidated: bool) -> tuple[dict, list[dict]]:
    """書類の属性（当期・前期の期末）と、地域別の欄の写し。"""
    ctxs = inst["contexts"]
    periods = {k: ctxs[c][0].split("/")[-1][:7] for k, c in (("current", "CurrentYearDuration"), ("prior", "Prior1YearDuration")) if c in ctxs}
    sections, seen = [], set()
    for f in inst["facts"]:
        sec = ELEMENTS.get(f["name"])
        if not sec or f["nil"] or not f["value"] or (f["name"], f["context"]) in seen:
            continue
        seen.add((f["name"], f["context"]))
        content, n_src = content_of(f["value"])
        end = ctxs[f["context"]][0].split("/")[-1]
        sections.append({"element": f"{f['prefix']}:{f['name']}", "section": sec, "context": f["context"],
                         "basis": "non_consolidated" if NONCON in f["context"] or not consolidated else "consolidated",
                         "period": end[:7], "period_end": end, "source_tables": n_src, "content": content,
                         "doc_id": doc_id, "submitted": submitted})
    return {"submitted": submitted, "periods": periods}, sections
