"""EDINET の有価証券報告書 → 値の置き場（第 1 便＝主要な経営指標等の推移・従業員の状況）。

  python -m companies.ingest.edinet --cached                          # 取得済みの書類（data/cache/xbrl/*.zip）を取り込む
  python -m companies.ingest.edinet --docids S100XXXX,S100YYYY
  python -m companies.ingest.edinet --from 2025-06-20 --to 2025-06-30 [--n 50]

取り込むもの＝次元が「連結・個別」だけの context（当期〜四期前）にある、
  ①標準要素の `…SummaryOfBusinessResults`（語彙に無いものも持つ＝「代わりに何が開示されているか」の一覧に使う）
  ②各社の拡張要素の `…KeyFinancialData`／会社の名前空間の `…SummaryOfBusinessResults`（会社が定義した項目＝zip 内のラベルつき）
  ③従業員の状況（従業員数・平均年間給与・平均年齢・平均勤続年数）
値は公表どおりの文字列。連結と単体は別の系列（basis）。項目単位で単体へ落とす処理は**しない**（実データ検証 2026-09-20 §2）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import zipfile
from pathlib import Path

from companies.core import store
from companies.core.items import ITEMS
from companies.ingest.edinet_api import CACHE, fetch_zip, is_yuho, list_docs, parse_instance

NONCON = "_NonConsolidatedMember"
PLAIN = {f"{p}{k}" for p in ("CurrentYear", "Prior1Year", "Prior2Year", "Prior3Year", "Prior4Year") for k in ("Duration", "Instant")}
STD_LABEL = {el: label for label, els in ITEMS.values() for el in els}
_EMP = "InformationAboutReportingCompanyInformationAboutEmployees"


def wanted(prefix: str, name: str) -> bool:
    if prefix == "jpcrp_cor":
        return "SummaryOfBusinessResults" in name or name == "NumberOfEmployees" or name.endswith(_EMP)
    # 各社の拡張要素＝経営指標の表に置く名前は 2 通りある（2026-09-21 実測）：…KeyFinancialData（トヨタの営業収益 等）と、
    # 会社の名前空間に置いた …SummaryOfBusinessResults（建設業の完成工事高・IFRS の会社の売上高 等）
    return prefix.startswith("jpcrp030000-asr_") and (name.endswith("KeyFinancialData") or "SummaryOfBusinessResults" in name)


def extension_labels(zip_path: Path) -> dict[str, str]:
    """各社の拡張要素の日本語ラベル（標準ラベル）＝zip 内 `*_lab.xml`。"""
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.startswith("XBRL/PublicDoc/") and n.endswith("_lab.xml")]
        text = z.read(names[0]).decode("utf-8") if names else ""
    out = {}
    for m in re.finditer(r'<link:label\b([^>]*)>([^<]*)</link:label>', text):
        attrs, label = m.groups()
        lab = re.search(r'xlink:label="([^"]+?)_label"', attrs)
        if lab and 'role/label"' in attrs and 'xml:lang="ja"' in attrs:
            prefix, _, local = lab.group(1).rpartition("_")
            out[f"{prefix}:{local}"] = label.strip()
    return out


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
