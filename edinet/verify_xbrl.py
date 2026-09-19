"""
EDINET XBRL 正規化検証（企業情報アプリの着手前検証・使い捨て寄り）。

目的：有価証券報告書（様式 030000）の XBRL から、業種をまたいで同じ意味で取れる「主要な経営指標等の推移」
（jpcrp_cor:*SummaryOfBusinessResults）と従業員情報、ならびに定性開示のテキストブロック（事業等のリスク・MD&A・
サステナビリティ）を、N 社ぶん取り出して**被覆率**（どの社で何が取れ、何が取れないか）を数える。

前提：EDINET API v2（Subscription-Key 必須）。キーは環境変数 EDINET_API_KEY（edinet/.env も読む・git 外）。
利用規約：PDL1.0（商用可・出典明記・加工明記）／API 利用はスクレイピング禁止の例外。短時間大量アクセスは禁止＝
1 リクエストごとに待つ。

使い方：
  python -m edinet.verify_xbrl --from 2025-06-20 --to 2025-06-30 --n 10
  python -m edinet.verify_xbrl --docids S100XXXX,S100YYYY
出力：edinet/data/verify/<実行日>/ に 社別 JSON・coverage.md（被覆表）・summary_elements.csv（全社の Summary 要素一覧）。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from lxml import etree

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = DATA / "cache"
API = "https://api.edinet-fsa.go.jp/api/v2"
WAIT_SEC = 1.0  # 規約「短時間における大量のアクセス」回避

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"

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


# ---------------------------------------------------------------- API

def _key() -> str:
    k = os.getenv("EDINET_API_KEY")
    if not k:
        envf = HERE / ".env"
        if envf.exists():
            for line in envf.read_text().splitlines():
                if line.startswith("EDINET_API_KEY="):
                    k = line.split("=", 1)[1].strip().strip('"')
    if not k:
        sys.exit("EDINET_API_KEY が未設定（環境変数か edinet/.env）。キーは EDINET API 利用申請で発行（無料・本人登録）。")
    return k


def _get(url: str, params: dict, *, binary: bool = False) -> bytes:
    params = {**params, "Subscription-Key": _key()}
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "curl/8.7.1", "Accept": "*/*"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = r.read()
            time.sleep(WAIT_SEC)
            return body
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise
            time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 ** attempt)
    raise RuntimeError(f"取得失敗: {full.replace(_key(), '***')}")


def list_docs(day: dt.date) -> list[dict]:
    p = CACHE / "list" / f"{day}.json"
    if p.exists():
        return json.loads(p.read_text())["results"]
    body = _get(f"{API}/documents.json", {"date": day.isoformat(), "type": 2})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return json.loads(body)["results"]


def fetch_zip(doc_id: str) -> Path:
    p = CACHE / "xbrl" / f"{doc_id}.zip"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_get(f"{API}/documents/{doc_id}", {"type": 1}, binary=True))
    return p


def is_yuho(d: dict) -> bool:
    # 企業内容等の開示に関する内閣府令（010）・有価証券報告書（030000）・XBRL あり・訂正は除く
    return d.get("ordinanceCode") == "010" and d.get("formCode") == "030000" and d.get("xbrlFlag") == "1"


# ---------------------------------------------------------------- XBRL

def parse_instance(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.startswith("XBRL/PublicDoc/") and n.endswith(".xbrl")]
        if len(names) != 1:
            raise RuntimeError(f"{zip_path.name}: PublicDoc の .xbrl が {len(names)} 個（1 個のはず）")
        root = etree.fromstring(z.read(names[0]))
    # contexts: id -> (period, dims)
    ctx = {}
    for c in root.iter(f"{{{XBRLI}}}context"):
        dims = {}
        for m in c.iter(f"{{{XBRLDI}}}explicitMember"):
            dims[m.get("dimension")] = m.text
        per = c.find(f"{{{XBRLI}}}period")
        inst = per.findtext(f"{{{XBRLI}}}instant")
        period = inst if inst else f"{per.findtext(f'{{{XBRLI}}}startDate')}/{per.findtext(f'{{{XBRLI}}}endDate')}"
        ctx[c.get("id")] = (period, dims)
    facts = []
    for el in root.iter():
        cref = el.get("contextRef")
        if cref is None or not isinstance(el.tag, str):
            continue
        ns, local = el.tag[1:].split("}")
        prefix = root.nsmap and next((k for k, v in root.nsmap.items() if v == ns), ns)
        text = (el.text or "") if len(el) == 0 else etree.tostring(el, method="text", encoding="unicode")
        facts.append({
            "prefix": prefix, "name": local, "context": cref, "unit": el.get("unitRef"),
            "decimals": el.get("decimals"), "nil": el.get(f"{{{XBRLI}}}nil") == "true",
            "value": text.strip(),
        })
    return {"contexts": ctx, "facts": facts}


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
        "docID": doc["docID"], "filerName": doc.get("filerName"), "edinetCode": doc.get("edinetCode"),
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
        out["text"][label] = None if f is None else {"element": f["name"], "chars": len(re.sub(r"<[^>]+>", "", f["value"]))}
    # 発見用：この社が持つ Summary 要素と TextBlock の全名
    out["all_summary_elements"] = sorted({f["name"] for f in facts if "SummaryOfBusinessResults" in f["name"]
                                          and f["context"] in (CTX_CUR, CTX_CUR_I) and not f["nil"]})
    out["all_textblocks"] = sorted({f["name"] for f in facts if f["name"].endswith("TextBlock")
                                    and f["prefix"] == "jpcrp_cor" and f["context"] == CTX_FILING and not f["nil"]})
    return out


# ---------------------------------------------------------------- 実行

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="d_from", help="提出日 範囲 始")
    ap.add_argument("--to", dest="d_to", help="提出日 範囲 終")
    ap.add_argument("--n", type=int, default=10, help="採る社数（範囲内の先頭から）")
    ap.add_argument("--docids", help="docID をカンマ区切りで直接指定")
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

    outdir = DATA / "verify" / dt.date.today().isoformat()
    outdir.mkdir(parents=True, exist_ok=True)
    results = []
    for d in docs:
        zp = fetch_zip(d["docID"])
        try:
            r = analyze(d, parse_instance(zp))
        except Exception as e:  # 1 社の失敗で全体を止めない（何が失敗したかは残す）
            r = {"docID": d["docID"], "filerName": d.get("filerName"), "error": repr(e)}
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
