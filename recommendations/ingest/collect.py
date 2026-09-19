"""
政策文書の収集スクリプト（Phase 4）。

系統ごとにアダプタを持ち、インデックスページを解析して PDF を
data/pdfs/<org>/ に規則名で保存し、data/catalog.csv にメタデータ行を追記する。
カタログ列は Phase4_収集_メタデータ設計.md §2.2 に準拠。

使い方:
    python -m recommendations.ingest.collect keidanren --years 2026 --limit 6      # 経団連スパイク・少数
    python -m recommendations.ingest.collect keidanren --years 2026 2025           # 経団連 本番収集
    python -m recommendations.ingest.collect keidanren_legacy --years 1996 --limit 3  # 旧サイト（1990〜2012-04）スパイク
    python -m recommendations.ingest.collect keidanren_legacy                      # 旧サイト 全量（B19 遡及拡充）
    python -m recommendations.ingest.collect rengo                                 # 連合 teigen 全収集（年指定不要）
    python -m recommendations.ingest.collect nissho                                # 日商 政策提言アーカイブ全収集（年指定不要）

方針:
  - 直PDF（index が *.pdf）→ そのまま取得。
  - HTML詳細（*.html）→ 詳細ページから *_honbun.pdf（本文）を優先取得。
    honbun が無く概要/別紙(*_gaiyo/*_besshi)しか無い、または PDF が無い場合は
    本文がHTMLのみ → ダウンロードせず status='html_only' で記録（欠落を可視化）。
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import date as _date
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from recommendations.core.config import PDF_DIR, DATA_DIR

CATALOG = DATA_DIR / "catalog.csv"
UA = {"User-Agent": "Mozilla/5.0 (personal policy-RAG research; contact: kenichi.okamura.jp@gmail.com)"}
SLEEP_S = 1.5  # サーバ負荷に配慮したリクエスト間隔

CATALOG_COLUMNS = [
    "file_name", "doc_id", "org", "org_type", "title", "date", "date_int",
    "doc_type", "field_tags", "layer", "source_url", "retrieved_at", "status",
    # 分野タグ・文書性格（2026-08-15 付与＝LLM 判定・Sonnet 5 採用。新規収集行は空＝未判定）
    "policy_tags", "doc_nature",
]


def _get(url: str) -> requests.Response:
    r = requests.get(url, timeout=30, headers=UA)
    r.raise_for_status()
    r.encoding = "utf-8"  # 経団連は宣言ISO-8859-1だが実体UTF-8
    time.sleep(SLEEP_S)
    return r


def _doc_type_hint(title: str) -> str:
    """タイトルからの緩い doc_type 推定（要人手レビュー）。

    先頭の「資料」系キーワードは多声/転載・調査文書の検出（B4 帰属事故対策）：
    公開質問状・政党比較＝他者（政党）の声の転載、アンケート結果＝会員調査の集計。
    いずれも団体自身の提言ではないため 提言 より先に判定する。
    """
    for kw, t in [("公開質問状", "資料"), ("質問状", "資料"), ("政策比較", "資料"),
                  ("アンケート結果", "資料"), ("アンケート調査結果", "資料"),
                  ("意識調査", "資料"),
                  ("要望", "要望"), ("意見", "意見"), ("コメント", "意見"),
                  ("談話", "談話"), ("答申", "答申"), ("報告書", "報告書"),
                  ("レポート", "報告書"), ("提言", "提言"),
                  ("状況", "資料"), ("集計", "資料"), ("戦略", "提言")]:
        if kw in title:
            return t
    return "提言"  # 既定（経団連 policy の大半）


HTML_THIN_CHARS = 200  # これ未満は本文抽出失敗とみなす（status=html_thin）


def _main_node(soup: BeautifulSoup):
    """本文コンテナ（main/article/#main/#contents）を返す。無ければ body/全体。"""
    return (soup.find("main") or soup.find("article")
            or soup.find(id="main") or soup.find(id="contents")
            or soup.body or soup)


def extract_article_text(html: str) -> str:
    """記事HTMLから本文テキストを抽出する（判断B の主機構・全系統共通）。

    script/style/nav/header/footer/aside/form を除去し、main/article 等の本文コンテナを
    優先して get_text。空行を畳む。系統横断で使えるよう汎用実装（抽出品質は要点検）。
    """
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        t.decompose()
    lines = [ln.strip() for ln in _main_node(soup).get_text("\n").splitlines()]
    return "\n".join(ln for ln in lines if ln)


# ------------------------------------------------------ 共通骨格：取得→保存→catalog 行
#
# 5 団体アダプタの同一骨格（PDF 解決→HTML 本文抽出→HTML_THIN_CHARS 判定→skip_existing→
# catalog 行組み立て）を 1 本にまとめたもの（所見 2026-08-19 段 4）。各アダプタは
# index 関数＋正規化（entry → fetch_and_record の引数）だけを持つ。
#
# status の意味（catalog.csv の status 列＝従来どおり）:
#   ok         PDF を保存した／skip_existing で既存 PDF を使った
#   html_text  PDF が無く HTML 本文を .txt に保存した／既存 .txt を使った
#   html_thin  HTML 本文が HTML_THIN_CHARS 未満＝抽出失敗とみなし保存しない
#   html_only  PDF も HTML 本文抽出も無い（政府系＝本文 PDF が見つからない）
#   error      PDF 解決・ダウンロード・HTML 抽出のいずれかで例外

ORG_TYPE = {"keidanren": "経済団体", "rengo": "労働団体", "nissho": "経済団体",
            "gov": "政府", "doyukai": "経済団体"}


def catalog_row(file_name: str, org: str, title: str, date: str, doc_type: str,
                field_tags: str, source_url: str, status: str, today: str) -> dict:
    """catalog.csv の 1 行（列は CATALOG_COLUMNS の先頭 13 列。policy_tags/doc_nature は後工程）。"""
    return {
        "file_name": file_name, "doc_id": file_name.rsplit(".", 1)[0],
        "org": org, "org_type": ORG_TYPE[org],
        "title": title, "date": date,
        "date_int": int(date.replace("-", "")) if date else "",
        "doc_type": doc_type, "field_tags": field_tags, "layer": "公開",
        "source_url": source_url, "retrieved_at": today,
        "status": status,
    }


def fetch_and_record(entry: dict, org: str, out_dir: Path, today: str,
                     skip_existing: bool = False) -> dict:
    """1 エントリを取得・保存して catalog 行を返す（ネットワークは PDF DL と html_text 呼び出しのみ）。

    entry（各アダプタが正規化した dict）:
      base        ファイル名 stem（例 keidanren_2026_037）
      title/date  表示タイトル・ISO 日付（空可）
      doc_type    文書種別（省略時は _doc_type_hint(title)）
      field_tags  分野タグ（省略時は空）
      page_url    詳細/一覧ページの URL（PDF 無しのときの source_url・html_only の記録先）
      pdf_url     本文 PDF の URL（None＝PDF 無し）
      html_text   PDF 無しのときの本文。str（取得済）または callable()→(text, title|None)。
                  None なら HTML 本文抽出をしない＝status html_only（政府系）
      thin_chars  html_thin 判定のしきい値（省略時 HTML_THIN_CHARS。旧経団連は短文提言が実在）
      resolve_error  True なら解決段階で例外済＝status error（取得しない）
      error_ext   resolve_error 時の file_name 拡張子（既定 ".pdf"）
      source_url  "pdf_first"（既定：pdf_url or page_url）／"page_first"（page_url or pdf_url）
      log_date    True なら取得ログに [date] を添える
    """
    base, title, date = entry["base"], entry["title"], entry.get("date", "")
    field_tags = entry.get("field_tags", "")
    page_url, pdf_url = entry.get("page_url"), entry.get("pdf_url")
    html_text = entry.get("html_text")
    dlog = f" [{date}]" if entry.get("log_date") else ""
    status = "ok"

    if entry.get("resolve_error"):
        status = "error"
        file_name = base + entry.get("error_ext", ".pdf")
    elif pdf_url:  # PDF 本文あり
        file_name = base + ".pdf"
        dest = out_dir / file_name
        if skip_existing and dest.exists():
            print(f"  = skip既存 {file_name}")
        else:
            try:
                dest.write_bytes(_get(pdf_url).content)
                print(f"  ✓ {file_name}{dlog}  ← {pdf_url}")
            except Exception as ex:
                print(f"  ! DL失敗 {pdf_url}: {ex}", file=sys.stderr)
                status = "error"
    elif html_text is None:  # PDF 無し・HTML 本文抽出もしない（政府系）
        file_name = base + ".pdf"
        status = "html_only"
        print(f"  – html_only(本文PDF無し): {title[:34]} ({page_url})")
    else:  # PDF 無し → HTML 本文をテキスト抽出（判断B の主機構）
        file_name = base + ".txt"
        dest = out_dir / file_name
        if skip_existing and dest.exists():
            print(f"  = skip既存 {file_name}")
            status = "html_text"
        else:
            try:
                if callable(html_text):
                    text, t2 = html_text()
                    if t2:
                        title = t2
                else:
                    text = html_text
                if len(text) < entry.get("thin_chars", HTML_THIN_CHARS):
                    status = "html_thin"
                    print(f"  – html_thin({len(text)}字): {title[:34]} ({page_url})")
                else:
                    dest.write_text(text, encoding="utf-8")
                    status = "html_text"
                    print(f"  ✎ {file_name} ({len(text)}字)  ← {page_url}")
            except Exception as ex:
                print(f"  ! HTML抽出失敗 {page_url}: {ex}", file=sys.stderr)
                status = "error"

    if entry.get("source_url") == "page_first":
        source_url = page_url or pdf_url
    else:
        source_url = pdf_url or page_url
    # doc_type は最終タイトルで推定（連合は HTML の h1 でタイトルが置き換わることがある）
    doc_type = entry.get("doc_type") or _doc_type_hint(title)
    return catalog_row(file_name, org, title, date, doc_type, field_tags, source_url, status, today)


def _out_dir(org: str) -> Path:
    d = PDF_DIR / org
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- 経団連アダプタ

KEIDANREN_BASE = "https://www.keidanren.or.jp"


def keidanren_index(year: int) -> list[dict]:
    """年別インデックスを解析してエントリのリストを返す。"""
    url = f"{KEIDANREN_BASE}/policy/{year}/"
    soup = BeautifulSoup(_get(url).text, "html.parser")
    entries = []
    for li in soup.find_all("li"):
        a = li.find("a", href=True)
        if not a or f"/policy/{year}/" not in a["href"]:
            continue
        num = a["href"].rsplit("/", 1)[-1].split(".")[0]  # "037.pdf"->037, "022.html"->022
        if not num.isdigit():
            continue  # 年アーカイブ/ナビ等の非エントリ（例 /policy/2026/）を除外
        meta = li.find("div", class_="meta")
        t = meta.find("time") if meta else None
        label = meta.find("span", class_="label") if meta else None
        title = " ".join(a.get_text().split())
        entries.append({
            "num": num,  # 例 "037", "022"
            "href": urljoin(KEIDANREN_BASE, a["href"]),
            "is_pdf": a["href"].lower().endswith(".pdf"),
            "date": t["datetime"] if (t and t.has_attr("datetime")) else "",
            "category": label.get_text(strip=True) if label else "",
            "title": title,
        })
    return entries


def keidanren_resolve_pdf(entry: dict) -> str | None:
    """エントリから取得すべき本文PDFのURLを返す。無ければ None（html_only）。"""
    if entry["is_pdf"]:
        return entry["href"]
    # HTML詳細 → 本文PDFを探す（英語版 /en/ は日本語コーパスから除外）
    soup = BeautifulSoup(_get(entry["href"]).text, "html.parser")
    pdfs = [urljoin(entry["href"], a["href"]) for a in soup.find_all("a", href=True)
            if a["href"].lower().endswith(".pdf") and "/en/" not in a["href"].lower()]
    if not pdfs:
        return None  # 日本語PDFが無い（例: B7等の英語のみ文書）= html_only
    honbun = [p for p in pdfs if "_honbun" in p.lower()]
    if honbun:
        return honbun[0]
    # honbun が無く gaiyo/besshi のみ = 本文はHTML → html_only 扱い
    if all(("_gaiyo" in p.lower() or "_besshi" in p.lower()) for p in pdfs):
        return None
    return pdfs[0]  # その他は先頭PDFを本文とみなす


def collect_keidanren(years: list[int] | None, limit: int | None, skip_existing: bool = False) -> list[dict]:
    if not years:
        raise SystemExit("経団連は --years の指定が必須です（例: --years 2026 2025）")
    out_dir = _out_dir("keidanren")
    rows, taken = [], 0
    today = _date.today().isoformat()
    for year in years:
        for e in keidanren_index(year):
            if limit and taken >= limit:
                return rows
            pdf_url, resolve_error = None, False
            try:
                pdf_url = keidanren_resolve_pdf(e)
            except Exception as ex:
                print(f"  ! resolve失敗 {e['href']}: {ex}", file=sys.stderr)
                resolve_error = True
            row = fetch_and_record({
                "base": f"keidanren_{year}_{e['num']}", "title": e["title"], "date": e["date"],
                "field_tags": e["category"], "page_url": e["href"], "pdf_url": pdf_url,
                # PDF 無し → 詳細ページを取り直して本文抽出（従来どおり）
                "html_text": lambda e=e: (extract_article_text(_get(e["href"]).text), None),
                "resolve_error": resolve_error,
            }, "keidanren", out_dir, today, skip_existing)
            if row["status"] == "ok":  # 経団連の limit は従来どおり PDF 取得数で数える
                taken += 1
            rows.append(row)
    return rows


# ---------------------------------------- 経団連 旧サイトアダプタ（B19 遡及拡充）
#
# 旧サイト japanese/policy/ に現存する 1990〜2012-04 の文書を収集する（棚卸し 2026-09-07）。
# 現行 collect_keidanren（policy/YYYY/・2010 年〜）と分けた理由＝索引 3 様式・ISO-2022-JP・
# 複数ページ分割という現行に無い機構が 3 つ入るため。org は "keidanren" のまま（案A＝
# 2002-05-28 統合以前が旧経団連である旨は宣言層で注記し、catalog は分けない）。
#   - 索引様式: 1995〜1999=index{Y}.html（pol 通し番号）／2000〜2001=index{Y}.html
#     （YYYY/NNN）／1990・1991・2002〜2012=総合索引 index.html（YYYY/NNN）。
#   - 文字コード: 全ページ ISO-2022-JP（meta 宣言から検出・現行 _get の UTF-8 決め打ちは不可）。
#   - 日付: 索引のリンク直後「(1995年12月19日)」（全角数字混在・月まで→ -01 補完）。
#     索引に無い場合は本文冒頭から補う。
#   - 本文 4 類型: 単ページ／目次→honbun.html／目次→複数サブページ連結／PDF 直リンク。
#   - 命名: 2000 年以降=keidanren_YYYY_NNN（原番号そのまま）・1995〜1999=keidanren_YYYY_pNNN
#     （pol 番号は年をまたぐ通し番号＝別体系を p で刻印）。
#   - 総合索引は 2010〜2012 も載る（現行収録と同じ YYYY/NNN 番号体系）＝catalog に既収の
#     doc_id はスキップして取りこぼしだけ補完する（gap-fill）。

KEIDANREN_LEGACY_INDEX = f"{KEIDANREN_BASE}/japanese/policy"
KEIDANREN_LEGACY_YEARS = [1990, 1991] + list(range(1995, 2013))
_LEGACY_ZEN2HAN = str.maketrans("０１２３４５６７８９（）", "0123456789()")
_LEGACY_A_RE = re.compile(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_LEGACY_MAX_SUBPAGES = 40  # 分割本文の連結上限（実測最大は pol118 の 19）
_LEGACY_THIN_CHARS = 100   # 旧サイトは正味の短文提言が実在（実測 pol123=192 字）＝既定 200 を緩める


def _legacy_get(url: str) -> str:
    """旧ページ取得（meta charset 検出＝ISO-2022-JP。_get の UTF-8 決め打ちの旧サイト版）。"""
    r = requests.get(url, timeout=30, headers=UA)
    r.raise_for_status()
    m = re.search(rb'charset=["\']?([a-zA-Z0-9_-]+)', r.content[:2048])
    r.encoding = m.group(1).decode("ascii") if m else (r.apparent_encoding or "iso-2022-jp")
    time.sleep(SLEEP_S)
    return r.text


def _legacy_date(text: str) -> str:
    """「(1995年12月19日)」等を ISO へ（全角数字対応・日なし→ -01 補完）。無ければ空。"""
    m = re.search(r"(\d{4})年\s*(\d{1,2})月(?:\s*(\d{1,2})日)?",
                  text.translate(_LEGACY_ZEN2HAN))
    if not m:
        return ""
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3) or 1):02d}"


def _legacy_strip(html: str) -> str:
    """アンカー内 HTML/空白を畳んでテキスト化。"""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def keidanren_legacy_index(years: set[int]) -> list[dict]:
    """旧索引を解析してエントリのリストを返す（(year, num) 先勝ちで名寄せ）。

    リンク直後〜次リンクまでのテキストから日付を拾う（索引は手書き HTML で
    リスト構造が年により揺れるため、DOM でなく文書順の正規表現で解析する）。
    コメントアウト行（<!-- -->）は削除済み文書＝除外。
    """
    jobs = [(f"{KEIDANREN_LEGACY_INDEX}/index{y}.html", y)
            for y in sorted(years) if 1995 <= y <= 2001]
    if years - set(range(1995, 2002)):
        jobs.append((f"{KEIDANREN_LEGACY_INDEX}/index.html", None))  # 総合索引
    entries: dict[tuple, dict] = {}
    for url, year_hint in jobs:
        html = re.sub(r"<!--.*?-->", "", _legacy_get(url), flags=re.S)
        anchors = list(_LEGACY_A_RE.finditer(html))
        for i, m in enumerate(anchors):
            href = m.group(1)
            pm = re.search(r"(?:^|/)pol(\d+)", href)
            ym = re.search(r"(?:^|/)(\d{4})/(\d+)(?:/index\.html|\.html|\.pdf|/[\w.]+\.html)$",
                           href)
            if year_hint and year_hint <= 1999:      # pol 通し番号の年
                if not pm:
                    continue
                year, num = year_hint, f"p{pm.group(1)}"
            elif year_hint:                          # 2000/2001 の年別索引
                if not ym or int(ym.group(1)) != year_hint:
                    continue
                year, num = year_hint, ym.group(2)
            else:                                    # 総合索引
                if not ym or int(ym.group(1)) not in years:
                    continue
                year, num = int(ym.group(1)), ym.group(2)
            key = (year, num)
            if key in entries:
                continue  # 再掲リンク（実測＝同一 href の重複のみ）
            tail_end = anchors[i + 1].start() if i + 1 < len(anchors) else m.end() + 200
            title = _legacy_strip(m.group(2))
            # 日付はリンク直後（tail）を優先＝タイトル中の年月（「2002年9月度…調査」等）に
            # 発行日を乗っ取られない（検収 2026-09-07＝退職金調査 5 件が 1 年ズレた）。
            entries[key] = {
                "year": year, "num": num,
                "href": urljoin(url, href),
                "is_pdf": href.lower().endswith(".pdf"),
                "title": title,
                "date": (_legacy_date(_legacy_strip(html[m.end():tail_end]))
                         or _legacy_date(title)),
            }
    return list(entries.values())


def keidanren_legacy_resolve(page_url: str, num: str) -> tuple[str | None, str | None]:
    """エントリページから (本文PDFのURL, HTML本文テキスト) を返す（どちらか一方）。

    本文 PDF は**ホワイトリスト**＝文書番号名（polNNN.pdf／NNN.pdf・末尾 1 英字可）か
    honbun を含む名前だけ（検収 2026-09-07＝gaiyo/shiryo/sanko/table 等の補助 PDF を
    先頭 PDF 規則が本文と誤認した 218+ 件への対策。ブラックリストでは補助名の揺れ
    〔youyaku/kekka/kobetsu/outline…〕を追い切れない）。経団連ドメイン外の PDF は
    コメント対象の原典リンク＝本文でない（公取委・官邸等の死リンク 8 件が error になった）。
    PDF が無ければ本文テキスト＝ディレクトリ型（polNNN/・YYYY/NNN/）は同一ディレクトリの
    サブページを目次順に連結。本文が薄く補助 PDF しか無いページは補助 PDF で代替する。
    """
    html = _legacy_get(page_url)
    soup = BeautifulSoup(html, "html.parser")
    pdfs = [u for u in (urljoin(page_url, a["href"]) for a in soup.find_all("a", href=True)
                        if a["href"].lower().endswith(".pdf"))
            if u.startswith(KEIDANREN_BASE) and "/en/" not in u.lower()]
    n = num.lstrip("p")
    honbun = [p for p in pdfs
              if "honbun" in p.lower()
              or re.fullmatch(rf"(pol)?0*{n}[a-z]?\.pdf", p.rsplit("/", 1)[-1].lower())]
    if honbun:
        return honbun[0], None
    text = extract_article_text(html)
    if re.search(r"/(pol\d+|\d{4}/\d+)/[^/]*$", page_url):  # ディレクトリ型＝分割本文
        subs: list[str] = []
        for a in soup.find_all("a", href=True):
            h = a["href"].split("#")[0]
            if (not h or h.startswith(("http:", "https:", "/", "../", "mailto:"))
                    or not h.endswith(".html") or h == "index.html"):
                continue
            full = urljoin(page_url, h)
            if full != page_url and full not in subs:
                subs.append(full)
        for sub in subs[:_LEGACY_MAX_SUBPAGES]:
            text += "\n\n" + extract_article_text(_legacy_get(sub))
    if len(text) < _LEGACY_THIN_CHARS and pdfs:
        return pdfs[0], None  # 本文ページが空同然＝補助 PDF でも無いよりよい
    return None, text


def collect_keidanren_legacy(years: list[int] | None, limit: int | None,
                             skip_existing: bool = False) -> list[dict]:
    """経団連 旧サイト（1990〜2012-04）を収集。years 未指定は旧サイト全期間。

    catalog に既収の doc_id（status=ok/html_text）はスキップ＝2010〜2012 は
    取りこぼしの補完だけになり、再走行も増分になる。
    """
    yrs = set(years) if years else set(KEIDANREN_LEGACY_YEARS)
    unknown = yrs - set(KEIDANREN_LEGACY_YEARS)
    if unknown:
        raise SystemExit(f"旧サイトに無い年です: {sorted(unknown)}（対象={KEIDANREN_LEGACY_YEARS}）")
    done = set()
    if CATALOG.exists():
        with CATALOG.open(encoding="utf-8") as f:
            done = {r["doc_id"] for r in csv.DictReader(f)
                    if r["status"] in ("ok", "html_text")}
    out_dir = _out_dir("keidanren")
    rows, taken, skipped = [], 0, 0
    today = _date.today().isoformat()
    for e in sorted(keidanren_legacy_index(yrs), key=lambda x: (x["year"], x["num"])):
        base = f"keidanren_{e['year']}_{e['num']}"
        if base in done:
            skipped += 1
            continue
        if limit and taken >= limit:
            break
        pdf_url, body, resolve_error = None, None, False
        try:
            if e["is_pdf"]:
                pdf_url = e["href"]
            else:
                pdf_url, body = keidanren_legacy_resolve(e["href"], e["num"])
        except Exception as ex:
            print(f"  ! resolve失敗 {e['href']}: {ex}", file=sys.stderr)
            resolve_error = True
        if body and not e["date"]:  # 索引に日付が無い少数例は本文冒頭から補う
            e["date"] = _legacy_date(body[:500])
        row = fetch_and_record({
            "base": base, "title": e["title"], "date": e["date"],
            "page_url": e["href"], "pdf_url": pdf_url, "html_text": body,
            "thin_chars": _LEGACY_THIN_CHARS,
            "resolve_error": resolve_error, "log_date": True,
        }, "keidanren", out_dir, today, skip_existing)
        if row["status"] in ("ok", "html_text"):
            taken += 1
        rows.append(row)
    if skipped:
        print(f"  （catalog 既収のためスキップ: {skipped} 件）")
    return rows


# ------------------------------------------------------------------ 連合アダプタ
#
# 連合(jtuc-rengo)は WordPress のテーマ別サイト。中核は「政策・制度 要求と提言」で、
# 分野別セクション(page-3-2〜30)の本文HTMLが主体・PDFは少数（大半 2020 digest）。
# リコネ結論(§1.7): PDF豊富なのは経団連だけ → 連合は「本文HTML主体＋少数PDF」。
#   - 分野別セクション → extract_article_text で本文を .txt に（status=html_text）。
#     field_tags は一覧の分野ラベル（経済政策/税制改革/…）由来で良質、title はページ h1。
#   - PDF → そのまま取得。日付は URL パス /uploads/YYYY/MM/ 由来（月まで→ -01 補完）。
#   - date/doc_type は弱い（提言本体は版全体で1発行、個別日付なし）→ 要人手レビュー。
#
# ★Phase 11 の知見（2つの日付の落とし穴・catalog は手動訂正済／再収集時は上書き注意）:
#   (1) teigen 節は date="" のまま出る。実体は living「デジタル政策集」で節ごとに更新時点が
#       異なる。全文の最新の権威的確認は 2024-07-19（連合中執確認まで反映済版）＝これを基準に、
#       後日の確定過去事象を明示参照する節のみ その事象月の下限日にスタンプ（ハイブリッド）。
#   (2) digest PDF の URL パス日付は信用不可。連合は /uploads/2020/05/ の**同一URLで2025改訂版に
#       差し替え**ており、パス由来の 2020-05 は実体（本文「第95回中央委員会確認/2025.5.28」）と
#       5年ズレていた。再収集する場合は本文の改訂表記を確認し catalog の date を維持/訂正すること。

RENGO_BASE = "https://www.jtuc-rengo.or.jp"
RENGO_TEIGEN = f"{RENGO_BASE}/activity/seisaku_jitsugen/teigen/"


def _sanitize_stem(name: str) -> str:
    """ファイル名 stem を ASCII 安全化（日本語等は落とす）。空なら 'doc'。"""
    s = re.sub(r"[^A-Za-z0-9_-]+", "", Path(name).stem).strip("_-")
    return s or "doc"


def rengo_teigen_index() -> list[dict]:
    """teigen トップを解析し、分野別セクション(本文HTML)と PDF のエントリを返す。

    - セクションは page-3/page-3-N（Nはセクション番号）。同一Nへの複数リンクは
      文書順の**初出**を採る（初出=正規ナビ＝分野名がクリーン。後続は重複で表記揺れ）。
    - PDF は /uploads/YYYY/MM/ から日付を復元。
    """
    soup = BeautifulSoup(_get(RENGO_TEIGEN).text, "html.parser")
    entries, seen_sec, seen_pdf = [], set(), set()
    for a in soup.find_all("a", href=True):
        href = urljoin(RENGO_TEIGEN, a["href"])
        text = " ".join(a.get_text().split())
        m = re.search(r"/teigen/page-3/page-3-(\d+)/?$", href)
        if m:
            n = int(m.group(1))
            if n in seen_sec or not text:
                continue  # 初出のクリーンな分野ラベルのみ採用
            seen_sec.add(n)
            entries.append({"kind": "html", "num": f"{n:02d}", "href": href,
                            "field": text, "title": text, "date": ""})
        elif href.lower().endswith(".pdf") and href not in seen_pdf:
            seen_pdf.add(href)
            mp = re.search(r"/uploads/(\d{4})/(\d{2})/", href)
            date = f"{mp.group(1)}-{mp.group(2)}-01" if mp else ""
            stem = _sanitize_stem(href.rsplit("/", 1)[-1])
            num = f"{mp.group(1)}{mp.group(2)}_{stem}" if mp else stem
            entries.append({"kind": "pdf", "num": num, "href": href,
                            "field": "", "title": text or Path(href).stem, "date": date})
    return entries


def collect_rengo(years: list[int] | None, limit: int | None,
                  skip_existing: bool = False) -> list[dict]:
    """連合 teigen を収集（years は未使用＝年インデックスを持たないため）。"""
    out_dir = _out_dir("rengo")
    rows, taken = [], 0
    today = _date.today().isoformat()

    def _html(href: str):
        """分野別セクションの本文。h1 があればタイトルに採用する。"""
        soup = BeautifulSoup(_get(href).text, "html.parser")
        h1 = soup.find("h1")
        title = " ".join(h1.get_text().split()) if (h1 and h1.get_text(strip=True)) else None
        return extract_article_text(str(soup)), title

    for e in rengo_teigen_index():
        if limit and taken >= limit:
            break
        is_pdf = e["kind"] == "pdf"
        row = fetch_and_record({
            "base": f"rengo_{'teigen_' if not is_pdf else ''}{e['num']}",
            "title": e["title"], "date": e["date"], "field_tags": e["field"],
            "page_url": e["href"], "pdf_url": e["href"] if is_pdf else None,
            "html_text": (lambda href=e["href"]: _html(href)) if not is_pdf else None,
        }, "rengo", out_dir, today, skip_existing)
        if row["status"] in ("ok", "html_text"):
            taken += 1
        rows.append(row)
    return rows


# ------------------------------------------------------------------ 日商アダプタ
#
# 日商(jcci)の政策提言アーカイブ。一覧は /news/recommendations/index01〜04（news形式・
# `ul.m-news-list` に日付/タイトル/分野タグ）。詳細は /news/recommendations/[indexNN/]YYYY/*.html。
# リコネ結論（初期の「PDF 0」を訂正）: 政策提言の詳細ページは**実際の意見書/要望をPDFで持つ**
# （HTML本文は短い公表要約）→ 日商は経団連型の**PDF主体**。PDFが無いページのみ HTML本文を .txt 化。
#   - URL の時刻部（YYYY/MMDDhhmmss.html）で doc_id を一意化（同日複数提言があるため日付では不足）。
#   - 詳細に複数PDF（本文＋別紙/概要）がある場合は全て取得し _1/_2 で連番（内容を落とさない）。
#   - 日付は一覧の「YYYY年MM月DD日」由来。分野タグはカテゴリページ由来で疎（要人手レビュー）。

JCCI_BASE = "https://www.jcci.or.jp"
JCCI_INDEXES = [f"{JCCI_BASE}/news/recommendations/index{i:02d}/" for i in range(1, 5)]
# 一覧タグのうち「分野」でなく文書種別/汎用ラベルなもの（field_tags から除外）。
_JCCI_GENERIC_TAGS = {"政策提言", "ニュース", "会頭コメント", "日商", "調査・研究"}
# 詳細URL: /news/recommendations/[indexNN/]YYYY/<stem>.html
_JCCI_DETAIL_RE = re.compile(r"/news/recommendations/(?:index\d+/)?(20\d\d)/(\d+)\.html$")


def _quoted_title(text: str) -> str | None:
    """文中の鉤括弧タイトルを返す。外側「」を貪欲に取り（内側『』のネストを保持）、
    無ければ『』。どちらも無ければ None。"""
    m = re.search(r"「(.+)」", text) or re.search(r"『(.+)』", text)
    return m.group(1) if m else None


def nissho_index() -> list[dict]:
    """index01〜04 を横断し、提言詳細ページのエントリを URL で重複排除して返す。

    同一提言が複数のカテゴリページに出るため、初出を基準にマージ（分野タグは汎用でない
    ものを優先採用）。戻り値の各エントリ: url/year/stem/date/title/field。
    """
    byurl: dict[str, dict] = {}
    for idx in JCCI_INDEXES:
        soup = BeautifulSoup(_get(idx).text, "html.parser")
        ul = soup.find("ul", class_="m-news-list")
        if not ul:
            continue
        for li in ul.find_all("li", recursive=False):
            a = li.find("a", href=True)
            if not a:
                continue
            url = urljoin(JCCI_BASE, a["href"])
            m = _JCCI_DETAIL_RE.search(url)
            if not m:
                continue  # 提言アーカイブの詳細ページのみ（news/comment ラッパは除外）
            txt = " ".join(li.get_text().split())
            dm = re.match(r"(20\d\d)年(\d{1,2})月(\d{1,2})日", txt)
            date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
            title = _quoted_title(txt) or txt
            tag_el = li.find(class_="m-news-list__tag")
            tag = tag_el.get_text(strip=True).lstrip("# ").strip() if tag_el else ""

            e = byurl.setdefault(url, {"url": url, "year": m.group(1), "stem": m.group(2),
                                       "date": date, "title": title, "field": ""})
            if not e["date"] and date:
                e["date"] = date
            if tag and tag not in _JCCI_GENERIC_TAGS and not e["field"]:
                e["field"] = tag  # 分野カテゴリを優先採用
    return list(byurl.values())


def nissho_detail(url: str) -> tuple[list[str], str, str]:
    """詳細ページを取得し (本文PDFのURL群, タイトル, HTML本文テキスト) を返す。

    PDF は本文コンテナ内のみ探索（サイト共通ナビの誤検出回避）。タイトルは h1/h2 の
    「」内を優先。本文テキストは PDF が無い場合のフォールバック用。
    """
    soup = BeautifulSoup(_get(url).text, "html.parser")
    node = _main_node(soup)
    pdfs: list[str] = []
    for a in node.find_all("a", href=True):
        if a["href"].lower().endswith(".pdf"):
            pu = urljoin(url, a["href"])
            if pu not in pdfs:
                pdfs.append(pu)
    h = node.find(["h1", "h2"]) or soup.find("title")
    htext = " ".join(h.get_text().split()) if h else ""
    title = _quoted_title(htext) or htext
    body = extract_article_text(str(soup))
    return pdfs, title, body


def collect_nissho(years: list[int] | None, limit: int | None,
                   skip_existing: bool = False) -> list[dict]:
    """日商 政策提言アーカイブを収集（years は未使用＝日付索引ではないため）。"""
    out_dir = _out_dir("nissho")
    rows, taken = [], 0
    today = _date.today().isoformat()
    for e in nissho_index():
        if limit and taken >= limit:
            break
        base = f"nissho_{e['year']}_{e['stem']}"
        common = {"date": e["date"], "field_tags": e["field"], "page_url": e["url"]}
        try:
            pdfs, dtitle, body = nissho_detail(e["url"])
        except Exception as ex:
            print(f"  ! detail失敗 {e['url']}: {ex}", file=sys.stderr)
            rows.append(fetch_and_record({"base": base, "title": e["title"], **common,
                                          "resolve_error": True, "error_ext": ".html"},
                                         "nissho", out_dir, today, skip_existing))
            continue
        title = dtitle or e["title"]

        if pdfs:  # PDF本文あり（複数なら _1/_2 …で全取得）
            got = False
            for i, pu in enumerate(pdfs, 1):
                suffix = f"_{i}" if len(pdfs) > 1 else ""
                row = fetch_and_record({"base": base + suffix, "title": title, **common,
                                        "pdf_url": pu}, "nissho", out_dir, today, skip_existing)
                got = got or row["status"] == "ok"
                rows.append(row)
            if got:
                taken += 1
        else:  # PDF無し → HTML本文をテキスト抽出（detail で取得済）
            row = fetch_and_record({"base": base, "title": title, **common,
                                    "html_text": body}, "nissho", out_dir, today, skip_existing)
            rows.append(row)
            if row["status"] == "html_text":
                taken += 1
    return rows


# -------------------------------------------------------------------- 政府アダプタ
#
# 政府は単一インデックスが無く府省横断で分散（設計§1.2で最難と判断）。範囲は
# **C（諮問会議・規制改革のとりまとめ/答申）主 ＋ A（財政審の建議）従** に確定済。
# 3サブ系統をそれぞれ「成果文書（提言性の高い本文）」に絞って収集する（生議事録は §1.2 で後回し）:
#   - cefp  経済財政諮問会議: 取りまとめ資料ページの**骨太方針**(経済財政運営と改革の基本方針)。
#           2025は直PDF、2018-2024はHTML詳細→ *_basicpolicies_ja.pdf（本文, 概要/PR資料/英語版は除外）。
#   - kisei 規制改革推進会議: 答申等ページの**裸日付PDF** opinion/YYMMDD.pdf（答申/中間答申本文）。
#           point/main/initiatives/_N（概要・別紙・分割版）は除外。日付は6桁stem=20YYMMDD。
#   - zaiseishin 財政制度等審議会: 答申・報告ページの**建議**リンク→詳細ページ先頭PDF(=01.pdf=建議本文)。
#           02+（概要・参考資料）は本文でないので採らない。日付は詳細dir zaiseiaYYYYMMDD 由来。
#   - zeicho 政府税制調査会: 現行サイトは議事録/会議資料のみで答申アーカイブが無い →
#           §1.2「生議事録は後回し・成果文書優先」に従い**今回はスキップ**（後日 議事録路を検討）。
# 全て org=gov / org_type=政府。全PDFはテキスト抽出良好・スキャンPDF無しを収集スパイクで確認済。

CEFP_CABINET = "https://www5.cao.go.jp/keizai-shimon/kaigi/cabinet/cabinet-index.html"
KISEI_REPORT = "https://www8.cao.go.jp/kisei-kaikaku/kisei/publication/p_report.html"
ZAISEISHIN_REPORT = ("https://www.mof.go.jp/about_mof/councils/fiscal_system_council/"
                     "sub-of_fiscal_system/report/index.html")


def _wareki_to_iso(text: str) -> str:
    """文中の和暦日付（令和/平成 N年M月D日）を最初の1件だけ ISO(YYYY-MM-DD)へ。無ければ空。"""
    m = re.search(r"(令和|平成)\s*(元|\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
    if not m:
        return ""
    era, y, mo, d = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
    yy = 1 if y == "元" else int(y)
    year = (2018 + yy) if era == "令和" else (1988 + yy)  # R1=2019, H1=1989
    return f"{year}-{mo:02d}-{d:02d}"


def gov_cefp_index() -> list[dict]:
    """CEFP 取りまとめページから骨太方針(経済財政運営と改革の基本方針)エントリを返す。

    タイトルに西暦年を持つものだけ採用（ナビの honebuto-index は年が無く自然に除外）。
    2025は直PDF、他年は HTML詳細（本文PDFは download 時に resolve）。
    日付: 本文テキストの和暦優先→ href の decisionMMDD→年のみ(-06-01)。
    """
    soup = BeautifulSoup(_get(CEFP_CABINET).text, "html.parser")
    entries, seen = [], set()
    for a in soup.find_all("a", href=True):
        txt = " ".join(a.get_text().split())
        my = re.search(r"経済財政運営と改革の基本方針\s?(\d{4})", txt)
        if not my:
            continue
        year = my.group(1)
        if year in seen:
            continue
        seen.add(year)
        href = urljoin(CEFP_CABINET, a["href"])
        iso = _wareki_to_iso(txt)
        if not iso:
            md = re.search(r"decision(\d{2})(\d{2})", href)
            iso = f"{year}-{md.group(1)}-{md.group(2)}" if md else f"{year}-06-01"
        entries.append({
            "source": "cefp", "num": year, "title": txt, "date": iso,
            "doc_type": "方針",
            "pdf_url": href if href.lower().endswith(".pdf") else None,
            "html_url": None if href.lower().endswith(".pdf") else href,
        })
    return entries


def gov_kisei_index() -> list[dict]:
    """規制改革 答申等ページから裸日付PDF(答申/中間答申本文)を返す。"""
    soup = BeautifulSoup(_get(KISEI_REPORT).text, "html.parser")
    pat = re.compile(r"/opinion/(\d{6})\.pdf$")
    entries, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(KISEI_REPORT, a["href"])
        m = pat.search(href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        stem = m.group(1)  # YYMMDD（西暦下2桁）
        iso = f"20{stem[:2]}-{stem[2:4]}-{stem[4:6]}"
        title = " ".join(a.get_text().split())
        title = re.sub(r"\s*（PDF形式.*$", "", title)  # 「（PDF形式:…KB）」を除去
        entries.append({
            "source": "kisei", "num": stem, "title": title, "date": iso,
            "doc_type": "答申" if "答申" in title else "提言",
            "pdf_url": href, "html_url": None,
        })
    return entries


def gov_zaiseishin_index() -> list[dict]:
    """財政審 答申・報告ページから建議エントリ（詳細dirが西暦8桁=2019年以降）を返す。

    建議本文は詳細ページの先頭PDF(=01.pdf)を download 時に resolve。dir名 zaiseiaYYYYMMDD が
    西暦8桁のものに限定（それ以前は和暦6桁dirで混在・件数はA系目安8で足りるため対象外）。
    """
    soup = BeautifulSoup(_get(ZAISEISHIN_REPORT).text, "html.parser")
    pat = re.compile(r"/report/zaiseia(\d{8})/")
    entries, seen = [], set()
    for a in soup.find_all("a", href=True):
        txt = " ".join(a.get_text().split())
        if "建議" not in txt:
            continue
        href = urljoin(ZAISEISHIN_REPORT, a["href"])
        m = pat.search(href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        ymd = m.group(1)
        entries.append({
            "source": "zaiseishin", "num": ymd, "title": txt,
            "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}", "doc_type": "建議",
            "pdf_url": None, "html_url": href,
        })
    return entries


def _gov_resolve_pdf(source: str, html_url: str) -> str | None:
    """HTML詳細ページから本文PDFのURLを返す（source別ロジック）。無ければ None。"""
    soup = BeautifulSoup(_get(html_url).text, "html.parser")
    if source == "cefp":
        pdfs = [urljoin(html_url, a["href"]) for a in soup.find_all("a", href=True)
                if a["href"].lower().endswith(".pdf")]
        honbun = [p for p in pdfs if p.lower().endswith("basicpolicies_ja.pdf")]
        if honbun:
            return honbun[0]
        # フォールバック: 日本語で概要/英語でない最初のPDF
        for p in pdfs:
            pl = p.lower()
            if "summary" not in pl and "_en" not in pl and "shiryo" not in pl:
                return p
        return pdfs[0] if pdfs else None
    # zaiseishin: 本文コンテナ内の先頭PDF(=01.pdf=建議本文)
    node = _main_node(soup)
    for a in node.find_all("a", href=True):
        if a["href"].lower().endswith(".pdf"):
            return urljoin(html_url, a["href"])
    return None


GOV_SOURCES = {"cefp": gov_cefp_index, "kisei": gov_kisei_index,
               "zaiseishin": gov_zaiseishin_index}


def collect_gov(years: list[int] | None, limit: int | None,
                skip_existing: bool = False, source: str | None = None) -> list[dict]:
    """政府(諮問会議/規制改革/財政審)の成果文書を収集。

    source 指定で単一サブ系統のみ（スパイク/テスト用）。years 指定で発行年フィルタ。
    limit はサブ系統ごとの取得上限（横断合計でなく各系統に適用）。
    """
    out_dir = _out_dir("gov")
    rows = []
    today = _date.today().isoformat()
    yrset = {str(y) for y in years} if years else None
    sources = [source] if source else list(GOV_SOURCES)
    for src in sources:
        print(f"--- gov:{src} ---")
        taken = 0
        for e in GOV_SOURCES[src]():
            if limit and taken >= limit:
                break
            if yrset and e["date"][:4] not in yrset:
                continue
            pdf_url, resolve_error = e["pdf_url"], False
            try:
                if not pdf_url:  # HTML詳細から本文PDFを解決
                    pdf_url = _gov_resolve_pdf(src, e["html_url"])
            except Exception as ex:
                print(f"  ! resolve失敗 {e.get('html_url')}: {ex}", file=sys.stderr)
                resolve_error = True
            row = fetch_and_record({
                "base": f"gov_{src}_{e['num']}", "title": e["title"], "date": e["date"],
                "doc_type": e["doc_type"], "page_url": e["html_url"], "pdf_url": pdf_url,
                "html_text": None,  # 政府系は HTML 本文化しない＝本文 PDF 無しは html_only
                "resolve_error": resolve_error, "source_url": "page_first", "log_date": True,
            }, "gov", out_dir, today, skip_existing)
            if row["status"] == "ok":
                taken += 1
            rows.append(row)
    return rows


# ---------------------------------------------------------------- 同友会アダプタ
#
# 同友会(doyukai)の提言一覧はJS(Vue)動的描画だが、実体は静的JSON
#   /policyproposals/json/article_list.json（全期間・約950件, title/url/date/tags）
# を描画しているだけ → **playwright 不要**（Phase 4 設計§1.6「要playwright」判断を訂正）。
#   - field_tags は tag_list.json の id→分野名を転記（複数タグは「、」連結。
#     フィルタは部分一致(filters.py)なので連結表現で機能する）。
#   - 詳細ページは静的HTML。本文PDFはアンカーテキスト「本文」を優先
#     （uploads/docs/YYYYMMDD**a**.pdf=本文 / b=概要 の命名慣行）。概要/参考のみ・PDF無しは
#     HTML本文を .txt 化（判断B）。一覧 url が直PDF のエントリはそのまま取得。
#   - 公開提言のみ（layer=公開）。

DOYUKAI_BASE = "https://www.doyukai.or.jp"
DOYUKAI_ARTICLES = f"{DOYUKAI_BASE}/policyproposals/json/article_list.json"
DOYUKAI_TAGS = f"{DOYUKAI_BASE}/policyproposals/json/tag_list.json"


def _strip_outer_quotes(title: str) -> str:
    """全体が『』/「」で囲まれたタイトルの外側括弧を除去（JSON title の表記慣行）。"""
    t = title.strip()
    for op, cl in (("『", "』"), ("「", "」")):
        if t.startswith(op) and t.endswith(cl):
            return t[1:-1]
    return t


def doyukai_index() -> list[dict]:
    """article_list.json を解析してエントリのリストを返す（日付降順のまま）。"""
    arts = _get(DOYUKAI_ARTICLES).json()
    tagmap = {t["id"]: t["name"] for t in _get(DOYUKAI_TAGS).json()}
    entries = []
    for a in arts:
        url = urljoin(DOYUKAI_BASE, a["url"])
        if not url.startswith(DOYUKAI_BASE):
            continue  # 外部リンクは対象外
        date = (a.get("date") or "")[:10]
        stem = _sanitize_stem(url.rsplit("/", 1)[-1])
        entries.append({
            "url": url, "is_pdf": url.lower().endswith(".pdf"),
            "year": a.get("year") or date[:4], "stem": stem, "date": date,
            "title": _strip_outer_quotes(" ".join(a["title"].split())),
            "field": "、".join(tagmap[t] for t in a.get("tags", []) if t in tagmap),
        })
    return entries


def doyukai_resolve_pdf(detail_html: str, page_url: str) -> tuple[str | None, str]:
    """詳細HTMLから (本文PDFのURL, HTML本文テキスト) を返す。PDF無しは (None, 本文)。"""
    soup = BeautifulSoup(detail_html, "html.parser")
    node = _main_node(soup)
    cands = [(" ".join(a.get_text().split()), urljoin(page_url, a["href"]))
             for a in node.find_all("a", href=True)
             if a["href"].lower().endswith(".pdf")]
    body = extract_article_text(detail_html)
    if not cands:
        return None, body
    honbun = [h for t, h in cands if "本文" in t]
    if honbun:
        return honbun[0], body
    # 「本文」表記が無い場合: 概要/参考/別紙でない先頭PDF（全て補助資料なら先頭を本文扱い）
    aux = ("概要", "参考", "別紙", "要約", "記者会見")
    main_pdfs = [h for t, h in cands if not any(k in t for k in aux)]
    return (main_pdfs or [h for _, h in cands])[0], body


def collect_doyukai(years: list[int] | None, limit: int | None,
                    skip_existing: bool = False) -> list[dict]:
    """同友会 公開提言を収集。years で発行年フィルタ（未指定は全期間・約950件）。"""
    out_dir = _out_dir("doyukai")
    rows, taken = [], 0
    today = _date.today().isoformat()
    yrset = {str(y) for y in years} if years else None
    for e in doyukai_index():
        if limit and taken >= limit:
            break
        if yrset and e["year"] not in yrset:
            continue
        pdf_url, body, resolve_error = None, "", False
        try:
            if e["is_pdf"]:
                pdf_url = e["url"]
            else:
                pdf_url, body = doyukai_resolve_pdf(_get(e["url"]).text, e["url"])
        except Exception as ex:
            print(f"  ! resolve失敗 {e['url']}: {ex}", file=sys.stderr)
            resolve_error = True
        row = fetch_and_record({
            "base": f"doyukai_{e['year']}_{e['stem']}", "title": e["title"], "date": e["date"],
            "field_tags": e["field"], "page_url": e["url"], "pdf_url": pdf_url,
            "html_text": body,  # 詳細ページ取得時に抽出済（判断B）
            "resolve_error": resolve_error, "log_date": True,
        }, "doyukai", out_dir, today, skip_existing)
        if row["status"] in ("ok", "html_text"):
            taken += 1
        rows.append(row)
    return rows


ADAPTERS = {"keidanren": collect_keidanren, "keidanren_legacy": collect_keidanren_legacy,
            "rengo": collect_rengo, "nissho": collect_nissho, "gov": collect_gov,
            "doyukai": collect_doyukai}


def write_catalog(rows: list[dict]) -> None:
    """カタログに追記（file_name で重複排除、既存を新規で上書き）。
    ★後工程列（policy_tags/doc_nature）は新規行が空なら既存値を温存する（2026-09-03 修正＝
    --skip-existing の再収集で既存行のタグが消え全再判定になる事故の再発防止）。"""
    existing = {}
    if CATALOG.exists():
        with CATALOG.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing[r["file_name"]] = r
    for r in rows:
        old = existing.get(r["file_name"])
        if old:
            for col in ("policy_tags", "doc_nature"):
                if not (r.get(col) or "").strip() and (old.get(col) or "").strip():
                    r[col] = old[col]
        existing[r["file_name"]] = r
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    with CATALOG.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CATALOG_COLUMNS)
        w.writeheader()
        for r in existing.values():
            w.writerow(r)
    print(f"\nカタログ更新: {CATALOG}（全 {len(existing)} 行）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("org", choices=list(ADAPTERS))
    ap.add_argument("--years", type=int, nargs="+", default=None,
                    help="対象年（経団連は必須。連合は年インデックス無しのため未使用）")
    ap.add_argument("--limit", type=int, default=None, help="取得PDF数の上限（スパイク用）")
    ap.add_argument("--skip-existing", action="store_true", help="既存ファイルは再取得しない")
    ap.add_argument("--source", choices=list(GOV_SOURCES), default=None,
                    help="政府のサブ系統を1つに限定（cefp/kisei/zaiseishin, スパイク用）")
    args = ap.parse_args()

    print(f"収集: {args.org}  years={args.years}  limit={args.limit}  "
          f"source={args.source}  skip_existing={args.skip_existing}")
    kwargs = {"source": args.source} if args.org == "gov" else {}
    rows = ADAPTERS[args.org](args.years, args.limit, args.skip_existing, **kwargs)
    write_catalog(rows)
    from collections import Counter
    tally = Counter(r["status"] for r in rows)
    print(f"結果: {dict(tally)}  総エントリ={len(rows)}")


if __name__ == "__main__":
    main()
