"""
EDINET XBRL 正規化検証（企業情報アプリの着手前検証・使い捨て寄り）。

目的：有価証券報告書（様式 030000）の XBRL から、業種をまたいで同じ意味で取れる「主要な経営指標等の推移」
（jpcrp_cor:*SummaryOfBusinessResults）と従業員情報、ならびに定性開示のテキストブロック（事業等のリスク・MD&A・
サステナビリティ）を、N 社ぶん取り出して**被覆率**（どの社で何が取れ、何が取れないか）を数える。

前提：EDINET API v2（Subscription-Key 必須）。キーは環境変数 EDINET_API_KEY（companies/.env も読む・git 外）。
利用規約：PDL1.0（商用可・出典明記・加工明記）／API 利用はスクレイピング禁止の例外。短時間大量アクセスは禁止＝
1 リクエストごとに待つ。

使い方：
  python -m companies.ingest.verify_xbrl --from 2025-06-20 --to 2025-06-30 --n 10
  python -m companies.ingest.verify_xbrl --docids S100XXXX,S100YYYY
出力：companies/data/verify/<実行日時>/ に 社別 JSON・coverage.md（被覆表）・summary_elements.csv（全社の Summary 要素一覧）。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from lxml import etree

from companies.ingest.edinet_api import (API, CACHE, DATA, HERE, WAIT_SEC, XBRLDI, XBRLI, _get, _key,  # noqa: F401
                                         fetch_zip, is_yuho, list_docs, parse_instance)


# 業種横断で同じ意味になることを期待する要素（local name）。取れない社は「別名の Summary 要素」を一覧から拾って判断する。
TARGET_NUMERIC = {
    "売上高": ["NetSalesSummaryOfBusinessResults", "RevenueSummaryOfBusinessResults",
               "OperatingRevenue1SummaryOfBusinessResults", "RevenueIFRSSummaryOfBusinessResults",
               "OrdinaryIncomeBNKSummaryOfBusinessResults", "OperatingRevenueSECSummaryOfBusinessResults"],
    "経常利益": ["OrdinaryIncomeLossSummaryOfBusinessResults", "ProfitLossBeforeTaxIFRSSummaryOfBusinessResults"],
    "親会社帰属当期純利益": ["ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
                   "ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
                   "ProfitLossIFRSSummaryOfBusinessResults"],
    "純資産": ["NetAssetsSummaryOfBusinessResults", "EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
             "EquityIFRSSummaryOfBusinessResults"],
    "総資産": ["TotalAssetsSummaryOfBusinessResults", "TotalAssetsIFRSSummaryOfBusinessResults"],
    "自己資本比率": ["EquityToAssetRatioSummaryOfBusinessResults", "RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults"],
    "ROE": ["RateOfReturnOnEquitySummaryOfBusinessResults", "RateOfReturnOnEquityIFRSSummaryOfBusinessResults"],
    "従業員数（連結）": ["NumberOfEmployees"],
    "従業員数（提出会社）": ["NumberOfEmployees"],  # NonConsolidatedMember で別扱い
    "平均年間給与": ["AverageAnnualSalaryInformationAboutReportingCompanyInformationAboutEmployees"],
    "平均年齢": ["AverageAgeYearsInformationAboutReportingCompanyInformationAboutEmployees"],
    "平均勤続年数": ["AverageLengthOfServiceYearsInformationAboutReportingCompanyInformationAboutEmployees"],
}
TARGET_TEXT = {
    "事業の内容": ["DescriptionOfBusinessTextBlock"],
    "経営方針・経営環境・対処すべき課題": ["BusinessPolicyBusinessEnvironmentIssuesToAddressEtcTextBlock"],
    "サステナビリティ": ["DisclosureOfSustainabilityRelatedFinancialInformationTextBlock",
                  "SustainabilityRelatedInformationTextBlock"],
    "事業等のリスク": ["BusinessRisksTextBlock"],
    "MD&A": ["ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock"],
    "大株主の状況": ["MajorShareholdersTextBlock"],
    "役員の状況": ["InformationAboutOfficersTextBlock"],
}
CTX_CUR = "CurrentYearDuration"
CTX_CUR_I = "CurrentYearInstant"
CTX_FILING = "FilingDateInstant"  # 第一部の定性開示（事業等のリスク等）は提出日時点の context
# 容量の見立て（開発計画 §5）の対象＝検索に載せる候補の 5 ブロック（大株主・役員は表＝検索の対象外）
SEARCH_BLOCKS = ("事業の内容", "経営方針・経営環境・対処すべき課題", "サステナビリティ", "事業等のリスク", "MD&A")
N_FILERS = 3900  # 有報提出会社の概数（外挿の母数）


def text_chars(html_text: str) -> int:
    """TextBlock（HTML）の正味の字数＝タグを除き、実体参照を戻し、空白を畳んだ長さ。"""
    t = html.unescape(re.sub(r"<[^>]+>", " ", html_text))
    return len(re.sub(r"\s+", "", t))


def pick(facts: list[dict], names: list[str], ctx_ids: tuple[str, ...]) -> dict | None:
    """context 群を優先順に、各 context 群の中で別名を優先順に探す（連結を全別名で先に・単体は後）。"""
    for c in ctx_ids:
        for n in names:
            for f in facts:
                if f["name"] == n and f["context"] == c and not f["nil"] and f["value"] != "":
                    return f
    return None


def analyze(doc: dict, inst: dict) -> dict:
    facts = inst["facts"]
    dei = {f["name"]: f["value"] for f in facts if f["prefix"] == "jpdei_cor"}
    out = {
        "docID": doc["docID"], "filerName": doc.get("filerName") or dei.get("FilerNameInJapaneseDEI"),
        "edinetCode": doc.get("edinetCode") or dei.get("EDINETCodeDEI"),
        "secCode": doc.get("secCode"), "periodEnd": doc.get("periodEnd"), "submit": doc.get("submitDateTime"),
        "accounting_standard": dei.get("AccountingStandardsDEI"),
        "consolidated": dei.get("WhetherConsolidatedFinancialStatementsArePreparedDEI"),
        "numeric": {}, "text": {},
    }
    for label, names in TARGET_NUMERIC.items():
        if label == "従業員数（提出会社）":
            f = pick(facts, names, (f"{CTX_CUR_I}_NonConsolidatedMember",))
        elif label.startswith("平均"):
            f = pick(facts, names, (f"{CTX_CUR_I}_NonConsolidatedMember", CTX_CUR_I, f"{CTX_CUR}_NonConsolidatedMember"))
        else:
            f = pick(facts, names, (CTX_CUR, CTX_CUR_I,  # 連結なし（単体のみ）の社は単体へ
                                    f"{CTX_CUR}_NonConsolidatedMember", f"{CTX_CUR_I}_NonConsolidatedMember"))
        out["numeric"][label] = None if f is None else {"element": f["name"], "value": f["value"],
                                                         "unit": f["unit"], "decimals": f["decimals"], "context": f["context"]}
    for label, names in TARGET_TEXT.items():
        f = pick(facts, names, (CTX_FILING,))
        out["text"][label] = None if f is None else {"element": f["name"], "chars": text_chars(f["value"])}
    # 発見用：この社が持つ Summary 要素と TextBlock の全名
    # 連結を作成していない社は単体の context にしか値が無い（2026-09-20 実測＝見落とすと「当期純利益」が候補に出ない）
    cur = (CTX_CUR, CTX_CUR_I) if out["consolidated"] == "true" else (
        CTX_CUR, CTX_CUR_I, f"{CTX_CUR}_NonConsolidatedMember", f"{CTX_CUR_I}_NonConsolidatedMember")
    out["all_summary_elements"] = sorted({f["name"] for f in facts if "SummaryOfBusinessResults" in f["name"]
                                          and f["context"] in cur and not f["nil"]})
    out["all_textblocks"] = sorted({f["name"] for f in facts if f["name"].endswith("TextBlock")
                                    and f["prefix"] == "jpcrp_cor" and f["context"] == CTX_FILING and not f["nil"]})
    return out


def capacity_lines(ok: list[dict], chars_per_chunk: int) -> list[str]:
    """定性開示の字数の実測と、全社への外挿（開発計画 §5 の判断材料）。外挿は標本が小さいので桁の見立てに限る。"""
    if not ok:
        return []
    lines = ["", "## 容量の見立て（定性開示・字数は正味＝タグと空白を除く）", "",
             "| ブロック | 社数 | 平均 | 中央値 | 最大 |", "|---|---|---|---|---|"]
    for label in TARGET_TEXT:
        cs = [r["text"][label]["chars"] for r in ok if r["text"].get(label)]
        if cs:
            lines.append(f"| {label} | {len(cs)} | {statistics.mean(cs):,.0f} | {statistics.median(cs):,.0f} | {max(cs):,} |")
    per = [sum(r["text"][b]["chars"] for b in SEARCH_BLOCKS if r["text"].get(b)) for r in ok]
    mean, med = statistics.mean(per), statistics.median(per)
    zips = [r["zip_bytes"] for r in ok if r.get("zip_bytes")]
    lines += ["", f"- 検索候補 5 ブロックの合計（1 社）：平均 {mean:,.0f} 字・中央値 {med:,.0f} 字・最大 {max(per):,} 字",
              f"- 外挿（{N_FILERS:,} 社・1 期分）：約 {mean * N_FILERS / 1e6:,.0f} 百万字"
              f" ≒ {mean * N_FILERS / chars_per_chunk / 1e4:,.0f} 万チャンク（1 チャンク {chars_per_chunk} 字と仮定）",
              f"- 標本 {len(ok)} 社＝大企業に偏ると過大になる（中央値での外挿：約 {med * N_FILERS / chars_per_chunk / 1e4:,.0f} 万チャンク）"]
    if zips:
        lines.append(f"- 原本 zip：平均 {statistics.mean(zips) / 1e6:.1f}MB／社 → 全社 約 {statistics.mean(zips) * N_FILERS / 1e9:.0f}GB（S3 に置く分）")
    return lines


# ---------------------------------------------------------------- 実行

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="d_from", help="提出日 範囲 始")
    ap.add_argument("--to", dest="d_to", help="提出日 範囲 終")
    ap.add_argument("--n", type=int, default=10, help="採る社数（範囲内の先頭から）")
    ap.add_argument("--docids", help="docID をカンマ区切りで直接指定")
    ap.add_argument("--chars-per-chunk", type=int, default=500, help="容量の外挿に使う 1 チャンクの字数（仮定）")
    a = ap.parse_args()

    docs: list[dict] = []
    if a.docids:
        docs = [{"docID": d.strip()} for d in a.docids.split(",") if d.strip()]
    else:
        if not (a.d_from and a.d_to):
            ap.error("--from/--to か --docids を指定")
        day, end = dt.date.fromisoformat(a.d_from), dt.date.fromisoformat(a.d_to)
        while day <= end and len(docs) < a.n:
            for d in list_docs(day):
                if is_yuho(d):
                    docs.append(d)
                    if len(docs) >= a.n:
                        break
            day += dt.timedelta(days=1)
    print(f"対象 {len(docs)} 社", file=sys.stderr)

    outdir = DATA / "verify" / dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")  # 実行ごとに分ける（同日の再実行で上書きしない）
    outdir.mkdir(parents=True, exist_ok=True)
    results = []
    for d in docs:
        zp = fetch_zip(d["docID"])
        try:
            r = analyze(d, parse_instance(zp))
        except Exception as e:  # 1 社の失敗で全体を止めない（何が失敗したかは残す）
            r = {"docID": d["docID"], "filerName": d.get("filerName"), "error": repr(e)}
        r["zip_bytes"] = zp.stat().st_size
        results.append(r)
        (outdir / f"{d['docID']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
        print(f"  {d['docID']} {r.get('filerName')} {'ERROR ' + r['error'] if 'error' in r else ''}", file=sys.stderr)

    ok = [r for r in results if "error" not in r]
    lines = [f"# XBRL 正規化検証 被覆表（{dt.date.today()}・{len(ok)}/{len(results)} 社解析）", "",
             "| 社 | 会計基準 | 連結 | " + " | ".join(TARGET_NUMERIC) + " | " + " | ".join(TARGET_TEXT) + " |",
             "|---|---|---|" + "---|" * (len(TARGET_NUMERIC) + len(TARGET_TEXT))]
    for r in ok:
        cells = [r["filerName"] or r["docID"], r["accounting_standard"] or "", r["consolidated"] or ""]
        cells += [("◎ " + v["value"][:12]) if v else "×" for v in r["numeric"].values()]
        cells += [f"◎ {v['chars']:,}字" if v else "×" for v in r["text"].values()]
        lines.append("| " + " | ".join(cells) + " |")
    cov = Counter()
    for r in ok:
        cov.update(k for k, v in {**r["numeric"], **r["text"]}.items() if v)
    lines += ["", "## 被覆率", ""] + [f"- {k}: {cov[k]}/{len(ok)}" for k in list(TARGET_NUMERIC) + list(TARGET_TEXT)]
    lines += ["", "## 未採用の Summary 要素（要素名 → 社数）＝正規化表に足す候補", ""]
    extra = Counter()
    known = {n for ns in TARGET_NUMERIC.values() for n in ns}
    for r in ok:
        extra.update(n for n in r["all_summary_elements"] if n not in known)
    lines += [f"- {n}: {c}" for n, c in extra.most_common()]
    lines += capacity_lines(ok, a.chars_per_chunk)
    (outdir / "coverage.md").write_text("\n".join(lines))
    with (outdir / "summary_elements.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["docID", "filerName", "element"])
        for r in ok:
            for n in r["all_summary_elements"]:
                w.writerow([r["docID"], r["filerName"], n])
    print(f"出力: {outdir}", file=sys.stderr)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
