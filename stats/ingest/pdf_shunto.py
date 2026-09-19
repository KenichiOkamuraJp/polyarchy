"""
経団連「春季労使交渉 業種別妥結結果（加重平均・最終集計）」PDF からの取込（大手／中小・総平均の妥結額と賃上げ率）。
設計は docs/データソース選定.md §1（既収 PDF＝recommendations の団体統計）・データ拡充計画.md 第5弾。

- 原典：recommendations 既収 PDF（`recommendations/data/pdfs/keidanren/<doc_id>.pdf`）。対象は catalog.csv の題名が
  「<年>年春季労使交渉・大手企業業種別妥結結果」「…中小企業業種別妥結結果」（回答状況＝中間集計は対象外）。
- 決定論パーサ（pymupdf のテキストトークン列）：
  大手：トークン列 [社,円,％,社,円,％] の**2回目**の直後に現れる数値4つ＝（今年 妥結額, 今年 率, 前年 妥結額, 前年 率）
  中小：「その他非製造業」の後の括弧なし数値のうち [10:14]（自行6＋非製造業平均4 の次）＝総平均の4つ
  規則に合わなければエラー（黙って選ばない）。
- 整合性：各 PDF の「前年」欄は前年 PDF の「今年」欄（最終集計）と一致するはず＝取込時に照合し、不一致はエラー（原典注記で
  前年値が改定されている場合のみ notes で許容）。
- 値は PDF の表示どおり（妥結額は桁区切りを除いた円の整数文字列、率は小数2桁の文字列）。原本は stats/data/cache/pdf/<取得日>/ にコピー。

**2026-08-18 以降：経団連系列は status=guide（値を保持しない）＝この取込は既定では対象なし。**
経団連の著作権規定（商用目的は事前相談）を受け、値の再配布をやめ発見層で読み方だけ案内する（docs/再配布条件.md）。
本モジュールは検証済みパーサとして残す（許諾を得て再取込する場合は seed_registry の status を registered に戻す）。

実行（リポジトリ root）：  python -m stats.ingest.pdf_shunto --all
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.paths import ROOT_DIR
from stats.core.registry import Registry, Series, default_registry
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, cache_path, cache_rel, finish, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.pdf_shunto")
CATALOG = ROOT_DIR / "recommendations" / "data" / "catalog.csv"
PDF_DIR = ROOT_DIR / "recommendations" / "data" / "pdfs" / "keidanren"
_NUM = re.compile(r"^-?\d[\d,]*(\.\d+)?$")  # PDF 表示（桁区切り付き）の数値トークン＝_base.is_numeric とは用途が違う


class ShuntoParseError(SourceError):
    pass


def list_docs(kind: str) -> list[tuple[str, str, str]]:
    """catalog.csv から (年, doc_id, title) を返す。kind=large|sme。最終集計（妥結結果）のみ。"""
    pat = re.compile(r"^(\d{4})年春季労使交渉・(大手|中小)企業業種別妥結結果")
    out = []
    with CATALOG.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            m = pat.match(row.get("title", "") or "")
            if not m:
                continue
            if (kind == "large") != (m.group(2) == "大手"):
                continue
            out.append((m.group(1), row["doc_id"], row["title"]))
    # 同一年に複数あれば最新（doc_id の番号が大きい方＝後の公表）を採る
    best: dict[str, tuple[str, str, str]] = {}
    for y, did, t in sorted(out):
        best[y] = (y, did, t)
    return sorted(best.values())


def tokens(pdf: Path) -> list[str]:
    import fitz
    d = fitz.open(pdf)
    if d.page_count != 1:
        raise ShuntoParseError(f"{pdf.name}: 1ページ想定だが {d.page_count} ページ")
    return [x.strip() for x in d[0].get_text().split("\n") if x.strip()]


def parse_large(t: list[str]) -> list[str]:
    idx = [i for i in range(len(t) - 5) if t[i:i + 6] == ["社", "円", "％", "社", "円", "％"]]
    if len(idx) < 2:
        raise ShuntoParseError("大手：[社,円,％,社,円,％] が2回現れない")
    nums = []
    j = idx[1] + 6
    while len(nums) < 4 and j < len(t):
        if _NUM.match(t[j]):
            nums.append(t[j])
        elif t[j] not in ("総", "平", "均", "総平均"):
            raise ShuntoParseError(f"大手：総平均の位置に想定外のトークン {t[j]!r}")
        j += 1
    if len(nums) != 4:
        raise ShuntoParseError("大手：総平均の数値4つが揃わない")
    return nums


def _find_label(t: list[str], label: str) -> int:
    """ラベルが1トークンでも縦書き分割（'そ','の','他',…）でも見つける。返り値＝ラベル末尾トークンの位置。"""
    if label in t:
        return t.index(label)
    n = len(label)
    for i in range(len(t) - n + 1):
        if "".join(t[i:i + n]) == label and all(len(x) == 1 for x in t[i:i + n]):
            return i + n - 1
    raise ShuntoParseError(f"中小：『{label}』が無い")


def parse_sme(t: list[str]) -> list[str]:
    k = _find_label(t, "その他非製造業")
    plain = []
    for x in t[k + 1:]:
        if x.startswith("(") or x.startswith("（") or x.endswith(")") or x.endswith("）"):
            continue
        if _NUM.match(x):
            plain.append(x)
        elif plain and len(plain) >= 14:
            break
        elif not _NUM.match(x) and plain and len(plain) < 14 and x not in ("社", "円", "％"):
            raise ShuntoParseError(f"中小：総平均の前に想定外のトークン {x!r}（{len(plain)} 個目）")
        if len(plain) >= 14:
            break
    if len(plain) < 14:
        raise ShuntoParseError("中小：数値が14個揃わない")
    return plain[10:14]


def clean_amount(s: str) -> str:
    return s.replace(",", "")


def ingest_kind(kind: str, series_by_measure: dict[str, Series], *, dry_run: bool, day: str) -> dict[str, int]:
    docs = list_docs(kind)
    if not docs:
        raise ShuntoParseError(f"{kind}: catalog に対象 PDF が無い")
    parse = parse_large if kind == "large" else parse_sme
    rows: dict[str, tuple[str, str, str, str, str]] = {}  # year -> (amt, rate, prev_amt, prev_rate, doc_id)
    for y, did, title in docs:
        pdf = PDF_DIR / f"{did}.pdf"
        if not pdf.exists():
            log.warning("%s: PDF が無い（%s）＝この年は値なし", did, pdf)
            continue
        cp = cache_path("pdf", day, pdf.name)
        if not cp.exists():
            shutil.copy2(pdf, cp)
        a, r, pa, pr = parse(tokens(pdf))
        rows[y] = (clean_amount(a), r, clean_amount(pa), pr, did)
    # 整合性：前年欄 == 前年PDFの今年欄
    mism = []
    for y, (a, r, pa, pr, did) in rows.items():
        py = str(int(y) - 1)
        if py in rows and (rows[py][0], rows[py][1]) != (pa, pr):
            mism.append((y, (pa, pr), (rows[py][0], rows[py][1])))
    if mism:
        raise ShuntoParseError(f"{kind}: 前年欄と前年PDFの不一致 {mism}")
    out: dict[str, int] = {}
    for meas, s in series_by_measure.items():
        recs = []
        for y, (a, r, pa, pr, did) in rows.items():
            v = a if meas == "wage_hike_amount" else r
            recs.append(ValueRecord(series_id=s.series_id, period=y, region="JP", value=v, status="", vintage=day, retrieved_at=day,
                                    accessor={"type": "pdf_table", "doc_id": did, "page": 1, "table": "業種別妥結結果（加重平均）", "row_label": "総平均",
                                              "col_label": "妥結額" if meas == "wage_hike_amount" else "アップ率（増減率）", "year_col": y,
                                              "cache": cache_rel(cache_path("pdf", day, f"{did}.pdf"))}))
        out[s.series_id] = finish(s, recs, dry_run=dry_run, exc=ShuntoParseError)
        log.info("%s: 値 %d（%s〜%s）前年欄との整合 OK", s.series_id, len(recs), min(rows), max(rows))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="経団連 春季妥結 PDF 取込")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    reg: Registry = default_registry()
    day = today()
    total = 0
    for kind in ("large", "sme"):
        sm = {s.measure: s for s in reg.series.values() if s.status == "registered" and s.accessor.get("type") == "pdf_table" and s.dims == kind}
        if not sm:
            continue
        for sid, n in ingest_kind(kind, sm, dry_run=a.dry_run, day=day).items():
            print(f"{sid}: {n} 値"); total += n
    print(f"合計 {total} 値")
    return 0


if __name__ == "__main__":
    sys.exit(main())
