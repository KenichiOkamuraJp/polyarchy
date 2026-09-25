"""EDINET の有価証券報告書 → 値の置き場（第 1 便＝主要な経営指標等の推移・従業員の状況）。

  python -m companies.ingest.edinet --cached                          # 取得済みの書類（data/cache/xbrl/*.zip）を取り込む
  python -m companies.ingest.edinet --docids S100XXXX,S100YYYY
  python -m companies.ingest.edinet --from 2025-06-20 --to 2025-06-30 [--n 50]

取り込むもの＝次元が「連結・個別」だけの context（当期〜四期前）にある、
  ①標準要素の `…SummaryOfBusinessResults`（語彙に無いものも持つ＝「代わりに何が開示されているか」の一覧に使う）
  ②各社の拡張要素の `…KeyFinancialData`／会社の名前空間の `…SummaryOfBusinessResults`（会社が定義した項目＝zip 内のラベルつき）
  ③従業員の状況（従業員数・平均年間給与・平均年齢・平均勤続年数）
  ④セグメント別の数値（第 1b 便）＝次元が「セグメントの軸」（と連結・個別の軸）だけの context（当期・前期）＝別の置き場へ。
    書類ごとにセグメント情報の注記の有無と、数値が無いときに示す会社の文 1 行（単一セグメント・記載の省略）も控える
  ⑤地域別の欄（第 1b 便②）＝テキストブロックの HTML を段落と表の順にセル単位で写す＝別の置き場へ（ingest/regions.py）
値は公表どおりの文字列。連結と単体は別の系列（basis）。項目単位で単体へ落とす処理は**しない**（実データ検証 2026-09-20 §2）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import zipfile
from html import unescape
from pathlib import Path

from companies.core import store
from companies.core.items import ITEMS
from companies.core.segments import AXIS, section_of
from companies.ingest.edinet_api import CACHE, fetch_zip, is_yuho, list_docs, parse_instance
from companies.ingest.regions import region_parts

NONCON = "_NonConsolidatedMember"
PLAIN = {f"{p}{k}" for p in ("CurrentYear", "Prior1Year", "Prior2Year", "Prior3Year", "Prior4Year") for k in ("Duration", "Instant")}
STD_LABEL = {el: label for label, els in ITEMS.values() for el in els}
_EMP = "InformationAboutReportingCompanyInformationAboutEmployees"


def wanted(prefix: str, name: str) -> bool:
    if prefix == "jpcrp_cor":
        # 標準要素にも …KeyFinancialData がある（2026-09-22 実測＝「収益」RevenueKeyFinancialData を最上段に置く会社）
        return ("SummaryOfBusinessResults" in name or name.endswith("KeyFinancialData") or name == "NumberOfEmployees"
                or name.endswith(_EMP))
    # 各社の拡張要素＝経営指標の表に置く名前は 2 通りある（2026-09-21 実測）：…KeyFinancialData（トヨタの営業収益 等）と、
    # 会社の名前空間に置いた …SummaryOfBusinessResults（建設業の完成工事高・IFRS の会社の売上高 等）
    return prefix.startswith("jpcrp030000-asr_") and (name.endswith("KeyFinancialData") or "SummaryOfBusinessResults" in name)


def _attr(attrs: str, name: str) -> str | None:
    m = re.search(rf'\b{name}="([^"]*)"', attrs)
    return m.group(1) if m else None


def extension_labels(zip_path: Path) -> dict[str, str]:
    """各社の拡張要素の日本語ラベル（標準ラベル）＝zip 内 `*_lab.xml`。

    ラベルリンクを loc（要素の id）→ labelArc → label（role=label・ja）の順にたどる。xlink:label の名前は書類ごとに任意
    （`<接頭辞>_<要素>_label` の会社と `label_<要素>` の会社がある＝2026-09-23 に名前の決め打ちで 243 社のラベルが空だった）。
    """
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.startswith("XBRL/PublicDoc/") and n.endswith("_lab.xml")]
        text = z.read(names[0]).decode("utf-8") if names else ""
    locs, arcs, labels = {}, {}, {}
    for m in re.finditer(r"<link:loc\b([^>]*)/?>", text):
        href, name = _attr(m.group(1), "xlink:href"), _attr(m.group(1), "xlink:label")
        if href and name and "#" in href:
            prefix, _, local = href.split("#", 1)[1].rpartition("_")
            locs[name] = f"{prefix}:{local}"
    for m in re.finditer(r"<link:labelArc\b([^>]*)/?>", text):
        src, dst = _attr(m.group(1), "xlink:from"), _attr(m.group(1), "xlink:to")
        if src and dst:
            arcs.setdefault(src, []).append(dst)
    for m in re.finditer(r"<link:label\b([^>]*)>([^<]*)</link:label>", text):
        attrs, label = m.groups()
        if _attr(attrs, "xml:lang") == "ja" and (_attr(attrs, "xlink:role") or "").endswith("/role/label"):
            labels[_attr(attrs, "xlink:label")] = label.strip()
    out = {}
    for name, el in locs.items():
        found = next((labels[d] for d in arcs.get(name, []) if d in labels), None)
        if found:
            out[el] = found
    return out


def _plain_text(html: str) -> str:
    return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", unescape(html))))


def note_quote(text: str) -> tuple[str | None, bool]:
    """セグメント情報の注記から、会社の文（単一セグメント・記載の省略）を 1 つと、報告セグメントごとの表の見出しの有無。分類はしない。

    見るのは【関連情報】より前（セグメント情報の節）だけ＝関連情報の「記載を省略」（地域・顧客）を拾わない。「。」で切ると表のセルが
    連なった断片が 1 文になる（2026-09-23 実測＝三菱製鋼・ハニーズ）＝単位の表記を含む断片・長すぎる断片は採らない。
    """
    seg = text.split("【関連情報】")[0]
    sentences = [s + "。" for s in seg.split("。") if s]
    hit = next((s for s in sentences if re.search(r"単一|省略|のみであ|重要性が乏し", s) and len(s) <= 200 and "単位" not in s), None)
    return hit, bool(re.search(r"報告セグメントごとの(売上高|売上収益|営業収益|収益|利益|資産)", seg))


def segment_parts(inst: dict, labels: dict[str, str], doc_id: str, submitted: str, consolidated: bool) -> tuple[dict, list[dict]]:
    """セグメント別の値と、書類の属性（当期・前期の期末・注記の有無と引用）。

    連結・個別の軸が無い値は、連結を作成している会社なら連結・作成していない会社なら提出会社（単体）の値
    （2026-09-23 実測＝中村屋の設備投資は軸なし）。同じ区分×要素×context の値が本文の 2 か所に載る（トヨタの設備投資）＝1 件にまとめる。
    """
    ctxs = inst["contexts"]
    periods = {k: ctxs[c][0].split("/")[-1][:7] for k, c in (("current", "CurrentYearDuration"), ("prior", "Prior1YearDuration")) if c in ctxs}
    facts, seen = [], set()
    for f in inst["facts"]:
        per, dims = ctxs.get(f["context"], (None, {}))
        axes = {d.split(":")[-1] for d in dims}
        if f["nil"] or f["value"] == "" or not f["unit"] or AXIS not in axes or axes - {AXIS, "ConsolidatedOrNonConsolidatedAxis"}:
            continue
        if not f["context"].startswith(("CurrentYear", "Prior1Year")):
            continue
        member = next(m for d, m in dims.items() if d.split(":")[-1] == AXIS)
        el = f"{f['prefix']}:{f['name']}"
        if (member, el, f["context"], f["value"]) in seen:
            continue
        seen.add((member, el, f["context"], f["value"]))
        end = per.split("/")[-1]
        facts.append({"member": member, "member_label": labels.get(member), "element": el,
                      "element_label": None if el.split(":")[0] in ("jpcrp_cor", "jppfs_cor", "jpigp_cor") else labels.get(el),
                      "section": section_of(el),
                      "basis": "non_consolidated" if NONCON in f["context"] or not consolidated else "consolidated",
                      "period": end[:7], "period_end": end, "value": f["value"], "unit": f["unit"], "decimals": f["decimals"],
                      "context": f["context"], "doc_id": doc_id, "submitted": submitted})
    notes = {}
    for f in inst["facts"]:
        if f["name"].startswith("NotesSegmentInformation") and f["name"].endswith("TextBlock") and f["context"].startswith("CurrentYear") and f["value"]:
            basis = "non_consolidated" if (NONCON in f["context"] or "FinancialStatementsTextBlock" in f["name"] and "Consolidated" not in f["name"]) else "consolidated"
            quote, tables = note_quote(_plain_text(f["value"]))
            notes[basis] = {"present": True, "quote": quote, "segment_tables": tables}
    return {"submitted": submitted, "periods": periods, "notes": notes}, facts


def submitted_of(zip_path: Path) -> str:
    """提出日＝インスタンスのファイル名の末尾（…_<期末>_<連番>_<提出日>.xbrl）。"""
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.startswith("XBRL/PublicDoc/") and n.endswith(".xbrl"))
    m = re.search(r"_(\d{4}-\d{2}-\d{2})\.xbrl$", name)
    if not m:
        raise RuntimeError(f"{zip_path.name}: 提出日をファイル名から取れない: {name}")
    return m.group(1)


def ingest(doc_id: str) -> tuple[dict, int]:
    zp = fetch_zip(doc_id)
    inst = parse_instance(zp)
    dei = {f["name"]: f["value"] for f in inst["facts"] if f["prefix"] == "jpdei_cor"}
    code = dei.get("EDINETCodeDEI")
    if not code:
        raise RuntimeError(f"{doc_id}: EDINET コードが DEI に無い")
    labels, submitted = extension_labels(zp), submitted_of(zp)
    facts = []
    for f in inst["facts"]:
        ctx = f["context"]
        if f["nil"] or f["value"] == "" or ctx.replace(NONCON, "") not in PLAIN or not wanted(f["prefix"], f["name"]):
            continue
        end = inst["contexts"][ctx][0].split("/")[-1]
        el = f"{f['prefix']}:{f['name']}"
        facts.append({"element": el, "label": STD_LABEL.get(f["name"]) or labels.get(el),
                      "basis": "non_consolidated" if ctx.endswith(NONCON) else "consolidated",
                      "period": end[:7], "period_end": end, "value": f["value"], "unit": f["unit"], "decimals": f["decimals"],
                      "context": ctx, "dims": {}, "doc_id": doc_id, "submitted": submitted})
    sec = (dei.get("SecurityCodeDEI") or "").strip()
    meta = {"edinet_code": code, "name": dei.get("FilerNameInJapaneseDEI"), "name_en": dei.get("FilerNameInEnglishDEI"),
            "sec_code": sec[:4] if sec and sec != "－" else None,
            "consolidated": dei.get("WhetherConsolidatedFinancialStatementsArePreparedDEI") == "true",
            "accounting_standard": dei.get("AccountingStandardsDEI"),
            "fiscal_year_end": dei.get("CurrentFiscalYearEndDateDEI"), "submitted": submitted}
    store.write_company(meta, facts)
    doc, seg = segment_parts(inst, labels, doc_id, submitted, meta["consolidated"])
    store.write_segments(code, doc_id, {**doc, "accounting_standard": meta["accounting_standard"]}, seg)
    rdoc, regions = region_parts(inst, doc_id, submitted, meta["consolidated"])
    store.write_regions(code, doc_id, {**rdoc, "accounting_standard": meta["accounting_standard"]}, regions)
    return meta, len(facts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cached", action="store_true", help="取得済みの書類を取り込む（API を叩かない）")
    ap.add_argument("--docids")
    ap.add_argument("--from", dest="d_from")
    ap.add_argument("--to", dest="d_to")
    ap.add_argument("--n", type=int, default=0, help="上限（0＝範囲内の全部）")
    a = ap.parse_args()
    if a.cached:
        docs = sorted(p.stem for p in (CACHE / "xbrl").glob("*.zip"))
    elif a.docids:
        docs = [d.strip() for d in a.docids.split(",") if d.strip()]
    elif a.d_from and a.d_to:
        docs, day = [], dt.date.fromisoformat(a.d_from)
        while day <= dt.date.fromisoformat(a.d_to) and not (a.n and len(docs) >= a.n):
            docs += [d["docID"] for d in list_docs(day) if is_yuho(d)]
            day += dt.timedelta(days=1)
        docs = docs[:a.n] if a.n else docs
    else:
        ap.error("--cached か --docids か --from/--to を指定")
    ng = 0
    for d in docs:
        try:
            meta, n = ingest(d)
            print(f"  {d} {meta['name']}: {n} 値", file=sys.stderr)
        except Exception as e:  # 1 社の失敗で全体を止めない（何が失敗したかは残す）
            ng += 1
            print(f"  {d} ERROR {e!r}", file=sys.stderr)
    print(f"取込 {len(docs) - ng}/{len(docs)} 書類 → {store.STORE}", file=sys.stderr)
    return 0 if docs and not ng else 2


if __name__ == "__main__":
    sys.exit(main())
