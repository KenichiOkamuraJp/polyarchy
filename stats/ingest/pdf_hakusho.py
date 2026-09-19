"""
中小企業白書 付属統計資料（PDF）からの取込＝開業率・廃業率（雇用保険事業年報ベース・年度）。第 7 弾 補遺（2026-08-22・要件源＝利用側プロジェクト §5.5）。

- 原典：中小企業庁「中小企業白書 付属統計資料」PDF（例 2025 年版 `pamflet/hakusyo/2025/PDF/chusho/09Hakusyo_fuzokutoukei_web.pdf`）の
  「有雇用事業所数による開廃業率の推移」（第 12 表・資料＝厚生労働省「雇用保険事業年報」より中小企業庁作成）。
  定義＝開業率＝当該年度の保険関係新規成立事業所数／前年度末の適用事業所数×100、廃業率＝同 消滅事業所数／同×100（％）。
  雇用保険事業年報そのものは e-Stat の API に無い（2026-08-22 実測）＝白書の表が唯一の機械可読に近い時系列。
- 決定論パーサ（pymupdf のテキストトークン列）：
  1. 表題 `table_title` を含む**1 ページ**を確定（0 か 2 ページ以上ならエラー＝黙って選ばない。頁・表番号は年版で動くので表題で探す）。
  2. 数字は **PUA フォント**（U+3EDC〜U+3EE5＝0〜9・U+3EDA＝小数点）で埋め込まれている＝写像して通常の数字にする。
  3. `年度` の後ろのトークンを読む：2 桁の年（小数点なし）が k 個 → 開業率 k 個（小数点あり）→ 廃業率 k 個、を繰り返す
     （2 ブロック目以降に「開業率」「廃業率」のラベルは無い）。年は 2 桁＝50 以上は 19xx・未満は 20xx。
     ブロックの個数が合わなければエラー。
- 値は PDF の表示どおり（小数 1 桁の文字列）。原本は stats/data/cache/pdf/<取得日>/ に保存。
- 取得は**ブラウザ UA が要る**（curl UA は 403）。再配布条件＝経産省サイト利用規約（PDL1.0 準拠・数値は著作権の対象外）＝中小企業庁サイトは
  meti.go.jp 配下＝同規約が適用されると判断（docs/再配布条件.md に記録・要確認）。

実行（リポジトリ root）：
    python -m stats.ingest.pdf_hakusho --all
    python -m stats.ingest.pdf_hakusho --series meti.hakusho_sme.entry_rate_ei.fy --dry-run
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, cache_rel, fetch, finish, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.pdf_hakusho")

TYPES = ("pdf_hakusho_sme",)
# 白書 PDF は curl 相当の UA も "Mozilla/5.0" だけの UA も 403 で弾く（2026-09-19 再実測）＝一般的なブラウザ UA で取得
# （値の取得元は認証なしの公開 PDF。UA を上書きするのは本モジュールだけ＝取得の作法は stats/README.md）
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                   "Accept": "application/pdf,*/*", "Accept-Language": "ja"}
# 数字の PUA 写像（2025 年版で実測：U+3EDC..U+3EE5＝0..9・U+3EDA＝'.'）。版で変わり得るので写像後に数値として検証する
PUA_DIGITS = {chr(0x3EDC + i): str(i) for i in range(10)}
PUA_DIGITS[chr(0x3EDA)] = "."


class HakushoParseError(SourceError):
    pass


def depua(tok: str) -> str:
    return "".join(PUA_DIGITS.get(ch, ch) for ch in tok)


def _is_year2(t: str) -> bool:
    return len(t) == 2 and t.isdigit()


def _is_rate(t: str) -> bool:
    return t.count(".") == 1 and t.replace(".", "").isdigit()


def _obtain_pdf(url: str, *, day: str, name: str) -> tuple[Path, str]:
    """原本 PDF を取得。chusho.meti.go.jp は AWS WAF の JS チャレンジ（HTTP 202・x-amzn-waf-action: challenge・本文空）を返すことがある
    ＝**突破しない**。その場合は既にキャッシュにある同名の原本（過去の取得・または人がブラウザで保存して置いたもの）を使い、無ければエラー。"""
    from stats.core.paths import CACHE_DIR

    def _is_pdf(p: Path) -> bool:
        return p.exists() and p.stat().st_size > 0 and p.read_bytes()[:5] == b"%PDF-"

    cands = sorted((c for c in (CACHE_DIR / "pdf").glob(f"*/{name}") if _is_pdf(c)), key=lambda p: p.parent.name)
    if cands and cands[-1].parent.name == day:
        return cands[-1], ""  # 同日の原本が既にある（人が置いたものを含む）＝取得しない（上書きしない）
    tmp, published = fetch(url, day=day, kind="pdf", name=name + ".download", headers=BROWSER_HEADERS)
    if _is_pdf(tmp):
        final = tmp.with_name(name)
        tmp.rename(final)
        return final, published
    tmp.unlink(missing_ok=True)
    if not cands:
        raise HakushoParseError(f"{url}: 取得元が bot 検知（WAF チャレンジ）で PDF を返さない。ブラウザで保存して "
                                f"{CACHE_DIR / 'pdf' / day / name} に置いてから再実行してください（自動で突破はしない）")
    log.warning("%s: WAF チャレンジで取得不可＝キャッシュ原本 %s を使用", Path(url).name, cache_rel(cands[-1]))
    return cands[-1], ""


def find_table_page(pdf: Path, table_title: str) -> int:
    """表題を含むページを 1 つに確定（0 か複数ならエラー）。返り値は 0 始まりの頁番号。"""
    import fitz
    d = fitz.open(pdf)
    # 目次にも表題が載るので、表の本体（『年度』の見出しと『開業率』の行ラベルを伴う頁）だけを数える
    hits = [i for i in range(d.page_count) if (lambda t: table_title in t and "年度" in t and "開業率" in t)(d[i].get_text())]
    if len(hits) != 1:
        raise HakushoParseError(f"{pdf.name}: 表題 {table_title!r} を含むページが {len(hits)} 頁（1 頁に確定しない）: {hits}")
    return hits[0]


def parse_entry_exit(tokens: list[str]) -> list[tuple[str, str, str]]:
    """`年度` 以降のトークン列 → [(FY, 開業率, 廃業率)]。ブロック＝年 k 個 → 開業率 k 個 → 廃業率 k 個 の繰り返し。"""
    try:
        start = tokens.index("年度") + 1
    except ValueError as e:
        raise HakushoParseError("トークン列に『年度』が無い") from e
    seq = [depua(t) for t in tokens[start:]]
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(seq):
        years: list[str] = []
        while i < len(seq) and _is_year2(seq[i]):
            years.append(seq[i]); i += 1
        if not years:
            break  # 表の末尾（『12 表』等）
        while i < len(seq) and seq[i] in ("開業率", "廃業率"):
            i += 1
        rates: list[str] = []
        while i < len(seq) and len(rates) < 2 * len(years):
            if seq[i] in ("開業率", "廃業率"):
                i += 1; continue
            if not _is_rate(seq[i]):
                raise HakushoParseError(f"率の位置に非数値 {seq[i]!r}（年 {years}）")
            rates.append(seq[i]); i += 1
        if len(rates) != 2 * len(years):
            raise HakushoParseError(f"年 {len(years)} 個に対し率が {len(rates)} 個（2 倍でない）")
        k = len(years)
        for y, e, x in zip(years, rates[:k], rates[k:]):
            yy = int(y)
            out.append((f"FY{1900 + yy if yy >= 50 else 2000 + yy}", e, x))
    if not out:
        raise HakushoParseError("開廃業率の行が 1 つも取れない")
    fys = [r[0] for r in out]
    if len(set(fys)) != len(fys) or fys != sorted(fys):
        raise HakushoParseError(f"年度が重複または非単調: {fys[:5]}…")
    return out


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    acc = s.accessor
    if acc.get("type") not in TYPES:
        raise ValueError(f"{s.series_id}: accessor.type={acc.get('type')} は pdf_hakusho の対象外")
    url, title, col = acc["url"], acc["table_title"], acc["column"]
    if col not in ("開業率", "廃業率"):
        raise ValueError(f"{s.series_id}: accessor.column は 開業率|廃業率")
    cp, published = _obtain_pdf(url, day=day, name=f"hakusho_{acc.get('edition', '')}_{Path(url).name}")
    import fitz
    pno = find_table_page(cp, title)
    toks = [x.strip() for x in fitz.open(cp)[pno].get_text().split("\n") if x.strip()]
    rows = parse_entry_exit(toks)
    recs = [ValueRecord(series_id=s.series_id, period=fy, region="JP", value=(e if col == "開業率" else x), status="",
                        vintage=str(acc.get("edition", "")) or published or day, retrieved_at=day, published_at=published,
                        accessor={"type": "pdf_hakusho_sme", "url": url, "page": pno + 1, "table_title": title, "column": col,
                                  "row": fy, "edition": acc.get("edition", ""), "cache": cache_rel(cp)})
            for fy, e, x in rows]
    log.info("%s: %s p.%d 『%s』%s → 値 %d（%s〜%s）", s.series_id, cp.name, pno + 1, title, col, len(recs), rows[0][0], rows[-1][0])
    return finish(s, recs, dry_run=dry_run, exc=HakushoParseError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(TYPES, ingest_series, "中小企業白書 付属統計資料 PDF の取込（開廃業率）", argv)


if __name__ == "__main__":
    sys.exit(main())
