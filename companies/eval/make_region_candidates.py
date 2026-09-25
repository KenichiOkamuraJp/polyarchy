"""第 1b 便②（地域別）の評価問の素材づくり（実装より先に立てる＝docs/第1b便_計画.md §6.1）。

期待値は**本文の inline XBRL（`XBRL/PublicDoc/*_honbun_*_ixbrl.htm`）の `ix:nonNumeric`** から、本ファイルのパーサで取る
＝取込（インスタンスの .xbrl のテキストブロック・`companies.ingest.edinet`）とは別のファイル・別のパーサ。同じ出所なので誤りの検出は限定的。
合計のセルだけは別経路がある＝表のセルの数値×単位が、同じ書類でタグの付いた売上高（第 1 便の値の置き場）と一致したセルだけを採る。

  python -m companies.eval.make_region_candidates
出力：companies/data/eval/regions.jsonl（正例）・regions_fail_closed.jsonl（負例）。**上書きする**＝人手で直した行があるときは実行しない。

写し方の仕様（取込と同じ仕様を独立に実装する）：
- セルの文字列＝セルの中を行に分け（p・div・br の境目）、各行の空白（半角・タブ・改行・nbsp）の連なりを 1 つの半角空白にして前後の空白
  （全角空白を含む）を落とし、空の行を捨てて "\\n" でつなぐ。結合セルは展開しない（rowspan／colspan は 2 以上のときだけ）。
- 段落＝表の外の p・div の行（同じ規則で整える）。80 字以下は全文。80 字を超える段落は「。」で区切った文のうち金額・比率を含む文だけ全文、
  それ以外は {"type": "omitted", "chars": 字数}（連なる省略は 1 つにまとめ、字数は足す）。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile

from lxml import etree

from companies.core import store
from companies.eval.make_candidates import EVAL, TODAY
from companies.ingest.edinet_api import CACHE

ELEMENTS = {"RevenuesFromExternalCustomersInformationForEachRegionTextBlock": "revenue",
            "PropertyPlantAndEquipmentInformationForEachRegionTextBlock": "property_plant_and_equipment",
            "InformationAboutGeographicalAreasIFRSTextBlock": "geographic_areas_ifrs"}
XHTML = "{http://www.w3.org/1999/xhtml}"
LINE_BREAK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
AMOUNT = re.compile(r"\d{1,3}(,\d{3})+|\d+(\.\d+)?\s*(百万円|千円|億円|円|％|%|百万米ドル|千米ドル|米ドル|ドル)")
SHORT = 80
# 棚卸しで詳しく見た会社（記録 §4）＝型ごとに 1 社以上
NAMED = ["株式会社極洋", "株式会社　サカタのタネ", "任天堂株式会社", "株式会社セブン＆アイ・ホールディングス", "三洋化成工業株式会社",
         "象印マホービン株式会社", "株式会社大気社", "日清オイリオグループ株式会社", "日本国土開発株式会社", "三菱自動車工業株式会社",
         "長瀬産業株式会社", "株式会社ミマキエンジニアリング", "マックス株式会社", "和弘食品株式会社", "株式会社日立製作所",
         "トヨタ自動車株式会社", "ソニーグループ株式会社", "東京海上ホールディングス株式会社", "ソフトバンクグループ株式会社",
         "住友化学株式会社", "参天製薬株式会社", "アンリツ株式会社", "三菱商事株式会社", "横浜ゴム株式会社",
         "キリンホールディングス株式会社", "三井海洋開発株式会社"]
HOME = re.compile(r"^(日本|本邦|国内|日本国内)")
N_TOTAL = 40


def _local(tag) -> str:
    return tag.split("}")[-1] if isinstance(tag, str) else ""


def _clean(line: str) -> str:
    return re.sub(r"[ \t\r\n\xa0]+", " ", line).strip(" \t\r\n\xa0　")


def cell_text(el) -> str:
    lines, cur = [], []

    def walk(e):
        if _local(e.tag) in LINE_BREAK:
            lines.append("".join(cur)); cur.clear()
        if e.text:
            cur.append(e.text)
        for c in e:
            if isinstance(c.tag, str):
                walk(c)
            if c.tail:
                cur.append(c.tail)
        if _local(e.tag) in LINE_BREAK:
            lines.append("".join(cur)); cur.clear()
    if el.text:
        cur.append(el.text)
    for c in el:
        if isinstance(c.tag, str):
            walk(c)
        if c.tail:
            cur.append(c.tail)
    lines.append("".join(cur))
    return "\n".join(x for x in (_clean(l) for l in lines) if x)


def paragraph_items(text: str) -> list[dict]:
    if len(text) <= SHORT:
        return [{"type": "text", "text": text}]
    out: list[dict] = []
    for s in [x + "。" if i < len(parts) - 1 else x for parts in [text.split("。")] for i, x in enumerate(parts)]:
        if not s:
            continue
        if AMOUNT.search(s):
            out.append({"type": "text", "text": s})
        elif out and out[-1]["type"] == "omitted":
            out[-1]["chars"] += len(s)
        else:
            out.append({"type": "omitted", "chars": len(s)})
    return out


def table_rows(tb) -> list[list[dict]]:
    """その表に直接属する行だけ（入れ子の表の行は、入れ子を持つセルの tables に入れる）。"""
    rows = []
    for tr in tb.iter(f"{XHTML}tr"):
        if next(a for a in tr.iterancestors(f"{XHTML}table")) is not tb:
            continue
        row = []
        for td in tr:
            if _local(td.tag) not in ("td", "th"):
                continue
            c = {"text": cell_text(td)}
            for k in ("rowspan", "colspan"):
                n = int(td.get(k) or 1)
                if n >= 2:
                    c[k] = n
            inner = [t for t in td.iter(f"{XHTML}table") if next(t.iterancestors(f"{XHTML}td", f"{XHTML}th"), None) is td]
            if inner:
                c["tables"] = [{"rows": table_rows(t)} for t in inner]
            row.append(c)
        rows.append(row)
    return rows


def content_of(parts: list) -> list[dict]:
    out: list[dict] = []

    def walk(e):
        for c in e:
            if not isinstance(c.tag, str):
                continue
            if _local(c.tag) == "table":
                out.append({"type": "table", "rows": table_rows(c)})
            elif c.find(f".//{XHTML}table") is not None:
                walk(c)
            elif _local(c.tag) in LINE_BREAK:
                for line in cell_text(c).split("\n"):
                    if line:
                        out.extend(paragraph_items(line))
            else:
                walk(c)
    for p in parts:
        walk(p)
    return out


def ix_blocks(doc_id: str) -> list[dict]:
    """本文の inline XBRL から地域別の欄（要素名・context・中身）。continuedAt もたどる。"""
    out = []
    with zipfile.ZipFile(CACHE / "xbrl" / f"{doc_id}.zip") as z:
        for n in sorted(z.namelist()):
            if not (n.startswith("XBRL/PublicDoc/") and "_honbun_" in n and n.endswith("_ixbrl.htm")):
                continue
            raw = z.read(n)
            if not any(k.encode() in raw for k in ELEMENTS):
                continue
            root = etree.fromstring(raw, etree.XMLParser(huge_tree=True, recover=True))
            ix = root.nsmap.get("ix", "http://www.xbrl.org/2008/inlineXBRL")
            conts = {c.get("id"): c for c in root.iter(f"{{{ix}}}continuation")}
            for e in root.iter(f"{{{ix}}}nonNumeric"):
                local = (e.get("name") or "").split(":")[-1]
                if local not in ELEMENTS:
                    continue
                parts, nxt = [e], e.get("continuedAt")
                while nxt in conts:
                    parts.append(conts[nxt]); nxt = conts[nxt].get("continuedAt")
                out.append({"element": e.get("name"), "section": ELEMENTS[local], "context": e.get("contextRef"),
                            "content": content_of(parts)})
    return out


def _q(c: dict, doc: str, period: str, b: dict, kind: str, **extra) -> dict:
    basis = "non_consolidated" if "NonConsolidated" in b["context"] or not c["consolidated"] else "consolidated"
    return {"id": f"{c['edinet_code']}-{b['section'][:12]}-{b['context'][:12]}-{kind}-{extra.get('table', '')}-{extra.get('row', '')}-{extra.get('col', '')}-{doc}",
            "company": {"edinet_code": c["edinet_code"], "name": c["name"]}, "period": period, "basis": basis, "doc_id": doc,
            "kind": kind, "expected": {"element": b["element"], "section": b["section"], "context": b["context"], **extra},
            "checked_by": "本文 inline XBRL（取込とは別のファイル・別のパーサ）・人手の目視は未", "checked_at": TODAY}


def _periods(c: dict, doc: str) -> dict:
    fye = c["documents"][doc]["fiscal_year_end"][:7]
    y, m = int(fye[:4]), fye[5:]
    return {"CurrentYearDuration": fye, "Prior1YearDuration": f"{y - 1}-{m}"}


def cell_questions(c: dict, doc: str) -> list[dict]:
    """型を覆うセルを選ぶ＝本邦の見出し・最後の数値（合計が多い）・結合セル・「うち」・改行つき・段落と表の順・長い段落。"""
    per = _periods(c, doc)
    out = []
    for b in ix_blocks(doc):
        base = b["context"].replace("_NonConsolidatedMember", "")
        if base not in per:
            continue
        tables = [(i, x) for i, x in enumerate(b["content"]) if x["type"] == "table"]
        picks = {}
        for ti, (_, t) in enumerate(tables):
            cells = [(r, k, cell) for r, row in enumerate(t["rows"]) for k, cell in enumerate(row)]
            tests = {"home": lambda x: HOME.match(x["text"]),
                     "last_number": None,
                     "span": lambda x: x.get("rowspan") or x.get("colspan"),
                     "uchi": lambda x: re.search(r"うち|内、", x["text"]),
                     "multiline": lambda x: "\n" in x["text"],
                     "nested": lambda x: x.get("tables")}
            for kind, test in tests.items():
                if kind in picks:
                    continue
                if kind == "last_number":
                    hit = [x for x in cells if re.fullmatch(r"[△▲\-]?[\d,]+", x[2]["text"])]
                    hit = hit[-1:]
                else:
                    hit = [x for x in cells if test(x[2])][:1]
                if hit:
                    r, k, cell = hit[0]
                    picks[kind] = (ti, r, k, cell)
        for kind, (ti, r, k, cell) in picks.items():
            out.append(_q(c, doc, per[base], b, kind, table=ti, row=r, col=k, text=cell["text"],
                          rowspan=cell.get("rowspan", 1), colspan=cell.get("colspan", 1),
                          **({"nested_tables": cell["tables"]} if cell.get("tables") else {}),
                          same_text_cells=sum(1 for row in tables[ti][1]["rows"] for x in row if x["text"] == cell["text"])))
        texts = [x["text"] for x in b["content"] if x["type"] == "text"]
        if tables and b["content"][0]["type"] == "text" and base == "CurrentYearDuration":
            out.append(_q(c, doc, per[base], b, "order", first_text=b["content"][0]["text"],
                          first_table_at=tables[0][0], n_tables=len(tables)))
        amounts = [t for t in texts if len(t) > SHORT and AMOUNT.search(t)]
        if amounts:
            out.append(_q(c, doc, per[base], b, "prose_amount", text=amounts[0]))
        if any(x["type"] == "omitted" for x in b["content"]):
            out.append(_q(c, doc, per[base], b, "omitted_paragraph",
                          omitted_chars=sum(x["chars"] for x in b["content"] if x["type"] == "omitted")))
    return out


UNIT = {"百万円": 10 ** 6, "千円": 10 ** 3}
REVENUE = re.compile(r"売上|収益|営業収入")


def total_questions() -> list[dict]:
    """合計のセル（2 経路）＝セルの数値×単位が、同じ書類・当期のタグの付いた売上高と一致したセルが欄に 1 つだけのとき。"""
    reg = store.registry()
    order = sorted(reg, key=lambda k: hashlib.sha1(k.encode()).hexdigest())
    out, n_ifrs = [], 0
    for code in order:
        if len(out) >= N_TOTAL:
            break
        c = reg[code]
        doc = c["docs"][-1]
        tagged = {}
        for f in store.facts_of(code):
            if f["doc_id"] == doc and f["context"] == "CurrentYearDuration" and f["unit"] == "JPY" and REVENUE.search(f["label"] or "") \
                    and not re.search(r"原価|利益|その他", f["label"] or ""):
                tagged[int(f["value"])] = f
        if not tagged:
            continue
        for b in ix_blocks(doc):
            if b["context"] != "CurrentYearDuration" or b["section"] == "property_plant_and_equipment":
                continue
            blob = json.dumps(b["content"], ensure_ascii=False)
            units = {u for u in UNIT if re.search(rf"単位[：:]?\s*{u}|（{u}）|\({u}\)|金額[：:（(]\s*{u}|\"{u}\"", blob)}
            if len(units) != 1:
                continue
            mul = UNIT[units.pop()]
            hits = []
            tables = [x for x in b["content"] if x["type"] == "table"]
            for ti, t in enumerate(tables):
                for r, row in enumerate(t["rows"]):
                    for k, cell in enumerate(row):
                        if re.fullmatch(r"[\d,]+", cell["text"]) and int(cell["text"].replace(",", "")) * mul in tagged:
                            hits.append((ti, r, k, cell, tagged[int(cell["text"].replace(",", "")) * mul]))
            if len(hits) != 1:
                continue
            if b["section"] == "geographic_areas_ifrs":
                if n_ifrs >= N_TOTAL // 4:
                    continue
                n_ifrs += 1
            elif len(out) - n_ifrs >= N_TOTAL - N_TOTAL // 4:
                continue
            ti, r, k, cell, f = hits[0]
            q = _q(c, doc, _periods(c, doc)["CurrentYearDuration"], b, "total_two_path", table=ti, row=r, col=k, text=cell["text"],
                   rowspan=cell.get("rowspan", 1), colspan=cell.get("colspan", 1))
            q["expected"]["tagged"] = {"element": f["element"], "value": f["value"]}
            q["checked_by"] = "2 経路一致（本文 inline XBRL の表のセル×単位＝同じ書類のタグの付いた売上高）"
            out.append(q)
            break
    return out


def positives() -> list[dict]:
    reg = store.registry()
    by_name = {c["name"]: c for c in reg.values()}
    out = []
    for name in NAMED:
        c = by_name[name]
        qs = cell_questions(c, c["docs"][-1])
        out += qs
        print(f"  {name}: {len(qs)} 問", file=sys.stderr)
    tot = total_questions()
    print(f"  合計のセル（2 経路）: {len(tot)} 問（IFRS {sum(1 for q in tot if q['expected']['section'] == 'geographic_areas_ifrs')}）", file=sys.stderr)
    seen, uniq = set(), []
    for q in out + tot:
        if q["id"] not in seen:
            seen.add(q["id"]); uniq.append(q)
    return uniq


def negatives() -> list[dict]:
    reg = store.registry()
    by_name = {c["name"]: c for c in reg.values()}

    def co(name):
        c = by_name[name]
        return {"edinet_code": c["edinet_code"], "name": c["name"]}, c["fiscal_year_end"][:7]

    out = []
    for name, quote, note in (
            ("カネコ種苗株式会社", "90", "本邦が 90% 超のため省略（最も多い型。「％」は全角の会社がある）"),
            ("株式会社中村屋", "本邦以外", "連結なしの会社（単体の context）・本邦以外に無い"),
            ("コカ・コーラ ボトラーズジャパンホールディングス株式会社", "大部分", "IFRS・数字の無い「大部分」＝型は分類しない")):
        company, per = co(name)
        out.append({"id": f"{company['edinet_code']}-regions-omitted-{per}", "company": company, "period": per, "basis": None,
                    "reason": "omitted", "quote_contains": quote, "note": note + "。会社の文を quotes で返す"})
    for name, note in (("キヤノン株式会社", "米国基準＝注記が XBRL に無い"),
                       ("大豊建設株式会社", "日本基準で「地域ごとの情報」の見出しはあるが地域の要素でタグ付けしていない"),
                       ("中外製薬株式会社", "IFRS で地域の欄が無い")):
        company, per = co(name)
        out.append({"id": f"{company['edinet_code']}-regions-not_tagged-{per}", "company": company, "period": per, "basis": None,
                    "reason": "not_tagged", "note": note})
    company, per = co("株式会社中村屋")
    out += [
        {"id": f"{company['edinet_code']}-regions-consolidated-of-nonconsolidated", "company": company, "period": per, "basis": "consolidated",
         "reason": "no_consolidated_statements", "note": "連結を作成していない会社に連結を指定"},
        {"id": f"{company['edinet_code']}-regions-out_of_range-2019-03", "company": company, "period": "2019-03", "basis": None,
         "reason": "out_of_range", "note": "収録の無い決算期＝近い期の欄は返さない"},
        {"id": f"{company['edinet_code']}-regions-bad_period-FY2025", "company": company, "period": "FY2025", "basis": None,
         "reason": "bad_period", "note": "決算期は YYYY-MM"},
        {"id": "regions-unknown-company", "company": {"edinet_code": None, "name": "存在しない架空商事株式会社"}, "period": "2026-03",
         "basis": None, "reason": "unknown_company", "note": "存在しない会社"},
    ]
    return out


def main() -> int:
    pos, neg = positives(), negatives()
    (EVAL / "regions.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in pos))
    (EVAL / "regions_fail_closed.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in neg))
    print(f"正例 {len(pos)}・負例 {len(neg)} → {EVAL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
