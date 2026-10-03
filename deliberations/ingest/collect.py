"""
収集：会議体ごとの一覧ページから回を見つけ、回のページの資料と一覧の記録を取得して目録を書く。

    python -m deliberations.ingest.collect                 # 一覧を取り直し、新しい回・手元に無いファイルだけ取得
    python -m deliberations.ingest.collect --org dgk       # 会議体を絞る
    python -m deliberations.ingest.collect --offline       # 通信せず、手元の原文から目録を作り直す
    python -m deliberations.ingest.collect --refresh-pages # 既知の回のページも取り直す（資料の差し替えの確認）

目録（data/raw/manifest.jsonl）は 1 行 1 件：議事次第ページ・資料・参考資料・記録・非公開（名前だけ）。
手元にあるファイルは取り直さない（sha256 は手元のファイルから計算）。取得の作法は sources.py。

一覧と回のページの HTML の癖（M0 で実測）：
- 記録（議事録・議事要旨・議事概要）は回のページに無く、一覧ページにだけある（4 会議体とも）。
- デジタル行財政改革会議の一覧は記録のリンクの </a> が閉じていない＝ブロックの解析に頼らず、
  <a> の開始タグの直後の文字列をラベルとして読む。
- href に改行が混じることがある＝空白を除いてから解決する。
- 非公開の資料はリンクが無く、名前に【非公開】【非公表】（非公開）が付く＝public:false で目録に入れる。
"""
import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from deliberations.core.paths import MANIFEST, RAW_DIR
from deliberations.ingest.sources import (BROWSER_UA, BROWSER_UA_HOSTS, BY_ORG, REQUEST_INTERVAL,
                                          SOURCES, Source)

JST = timezone(timedelta(hours=9))
RECORD_LABEL = re.compile(r"議事録|議事要旨|議事概要")
NONPUBLIC = re.compile(r"[【（(](非公開|非公表)[】）)]")
FILE_EXT = re.compile(r"(?i)\.(pdf|xlsx?|docx?|pptx?)$")
_last = [0.0]


def now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def fetch(url: str) -> bytes | None:
    """取得（curl）。失敗は None。1 リクエストごとに REQUEST_INTERVAL 秒以上あける。"""
    wait = REQUEST_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    args = ["curl", "-s", "-L", "--max-time", "120", "-w", "\n%{http_code}"]
    if urlparse(url).hostname in BROWSER_UA_HOSTS:
        args += ["-A", BROWSER_UA]
    r = subprocess.run(args + [url], capture_output=True)
    _last[0] = time.time()
    body, _, code = r.stdout.rpartition(b"\n")
    if code.strip() != b"200":
        print(f"[collect] 取得失敗 HTTP {code.decode(errors='replace').strip()}: {url}", file=sys.stderr)
        return None
    return body


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _strip_noise(s: str) -> str:
    return re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", s)


def anchors(page: str, base: str, from_h1: bool = True) -> list[dict]:
    """本文の <a href> を {href, label, prefix} で返す。label は開始タグの直後の文字列（</a> が閉じていなくても読める）、
    prefix は同じブロック（li/p/tr/dd＝表は行単位で、前のセルの資料番号も読む）の中でリンクの前にある文字列（資料番号の手がかり）。
    from_h1＝本文を最初の <h1> から読む（回のページのナビゲーションを除く）。一覧ページは表が <h1> より
    前にあることがある（デジタル行財政改革会議）ので False で呼ぶ＝回と記録の href の型で絞る。"""
    s = _strip_noise(page)
    m = re.search(r"(?i)<h1", s) if from_h1 else None
    if m:
        s = s[m.start():]
    out = []
    blocks = re.split(r"(?i)</li>|</p>|</tr>|</dd>|<br\s*/?>|<li[^>]*>|<p[^>]*>|<tr[^>]*>|<h\d[^>]*>", s)
    for b in blocks:
        last_end = 0
        for am in re.finditer(r"(?is)<a\s[^>]*?href\s*=\s*\"([^\"]*)\"[^>]*>", b):
            href = re.sub(r"\s+", "", am.group(1))
            if not href or href.startswith(("#", "mailto")):
                continue
            rest = b[am.end():]
            close = re.search(r"(?i)</a>|<a\s", rest)
            label = _text(rest[: close.start()] if close else rest)
            prefix = _text(re.sub(r"(?is)<a\s.*?(</a>|$)", "", b[last_end: am.start()]))
            last_end = am.end() + (close.end() if close and close.group(0).lower() == "</a>" else 0)
            out.append({"href": urljoin(base, href), "label": label, "prefix": prefix})
    return out


def nonpublic_items(page: str) -> list[dict]:
    """リンクの無い非公開の資料（名前に【非公開】等）を {label, prefix} で返す。"""
    out = []
    for li in re.findall(r"(?is)<li[^>]*>(.*?)</li>", _strip_noise(page)):
        if re.search(r"(?i)<a\s", li):
            continue
        t = _text(li)
        if NONPUBLIC.search(t):
            m = re.match(r"((?:参考資料|資料)\s*[0-9０-９\-－‐ー―の・]*)\s*(.*)", t)
            out.append({"prefix": m.group(1).strip() if m else "", "label": (m.group(2) if m else t).strip()})
    return out


def page_date(page: str) -> str | None:
    t = unicodedata.normalize("NFKC", _text(_strip_noise(page)))
    m = re.search(r"令和\s*([0-9]+|元)\s*年\s*([0-9]+)\s*月\s*([0-9]+)\s*日", t)
    if not m:
        return None
    y = 1 if m.group(1) == "元" else int(m.group(1))
    return f"{2018 + y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def page_title(page: str) -> str:
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", _strip_noise(page))
    return _text(m.group(1)) if m else ""


def kind_candidate(prefix: str, label: str) -> str:
    if RECORD_LABEL.search(label + prefix):
        return "記録"
    if "議事次第" in label + prefix:
        return "議事次第"
    if prefix.startswith("参考"):
        return "参考資料"
    return "資料"


def material_no(prefix: str) -> str:
    m = re.match(r"((?:参考資料|資料)\s*[0-9０-９\-－‐ー―の・]*)", prefix or "")
    return m.group(1).strip() if m else ""


def _local(path: str) -> bytes | None:
    p = RAW_DIR / path
    return p.read_bytes() if p.exists() else None


def _get(url: str, path: str, offline: bool, refetch: bool = False) -> tuple[bytes | None, bool]:
    """手元にあれば手元（refetch のときは取り直す）、無ければ取得して保存。(内容, 今回取得したか)。"""
    have = _local(path)
    if have is not None and not refetch:
        return have, False
    if offline:
        return have, False
    body = fetch(url)
    if body is None:
        return have, False
    p = RAW_DIR / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return body, True


def collect_source(src: Source, offline: bool, refresh_pages: bool, old: dict) -> list[dict]:
    """1 会議体分の目録の行を返す。"""
    rows: list[dict] = []
    idx_raw, _ = _get(src.index, f"{src.org}/_index.html", offline, refetch=not offline)
    if idx_raw is None:
        print(f"[collect] {src.org}: 一覧ページが無い（offline で手元にも無い）", file=sys.stderr)
        return rows
    idx = idx_raw.decode("utf-8", "replace")
    links = anchors(idx, src.index, from_h1=False)
    sessions = sorted({int(m.group(1)): a["href"] for a in links
                       if (m := re.search(src.session_href, a["href"]))}.items())
    records: dict[int, list[dict]] = {}
    for a in links:
        m = re.search(src.record_href, a["href"])
        if m and RECORD_LABEL.search(a["label"]):
            records.setdefault(int(m.group(1)), []).append(a)

    for n, url in sessions:
        d = f"{src.org}/{n:02d}"
        raw, fetched = _get(url, f"{d}/_page.html", offline, refetch=refresh_pages)
        if raw is None:
            continue
        page = raw.decode("utf-8", "replace")
        meta = {"org": src.org, "session_no": n, "date": page_date(page),
                "mochimawari": "持ち回り" in _text(_strip_noise(page)), "title": page_title(page)}
        rows.append({**meta, "material_name": "（議事次第ページ）", "material_no": "", "doc_kind": "議事次第ページ",
                     "source_url": url, "path": f"{d}/_page.html", "public": True})
        items = [a for a in anchors(page, url) if FILE_EXT.search(a["href"])]
        seen = {a["href"] for a in items}
        items += [a for a in records.get(n, []) if a["href"] not in seen]
        done = set()
        for a in items:
            if a["href"] in done:
                continue
            done.add(a["href"])
            path = f"{d}/{a['href'].rsplit('/', 1)[-1]}"
            body, got = _get(a["href"], path, offline)
            kind = kind_candidate(a["prefix"], a["label"])
            rows.append({**meta, "material_name": a["label"], "material_no": material_no(a["prefix"]),
                         "list_prefix": a["prefix"], "doc_kind": kind, "source_url": a["href"],
                         "path": path if body is not None else None, "public": True,
                         "fetch_error": None if body is not None else "取得できない"})
            if got:
                print(f"[collect] 取得 {path}（{len(body):,} バイト）")
        for it in nonpublic_items(page):
            rows.append({**meta, "material_name": it["label"], "material_no": material_no(it["prefix"]),
                         "list_prefix": it["prefix"], "doc_kind": kind_candidate(it["prefix"], it["label"]),
                         "source_url": url, "path": None, "public": False})
    for r in rows:
        if r.get("path"):
            b = (RAW_DIR / r["path"]).read_bytes()
            r["bytes"] = len(b)
            r["sha256"] = hashlib.sha256(b).hexdigest()
            prev = old.get(r["path"])
            r["fetched_at"] = prev["fetched_at"] if prev and prev.get("sha256") == r["sha256"] else now()
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="審議会議事録DB の収集（一覧→回→資料・記録）")
    ap.add_argument("--org", action="append", help="会議体のコード（複数可・省略で全部）")
    ap.add_argument("--offline", action="store_true", help="通信せず手元の原文から目録を作り直す")
    ap.add_argument("--refresh-pages", action="store_true", help="既知の回のページも取り直す")
    args = ap.parse_args()
    lines = ([json.loads(l) for l in MANIFEST.open(encoding="utf-8")] if MANIFEST.exists() else [])
    old = {r["path"]: r for r in lines if r.get("path")}
    srcs = [BY_ORG[o] for o in args.org] if args.org else list(SOURCES)
    targets = {s.org for s in srcs}
    keep = [r for r in lines if r["org"] not in targets]  # 対象外の会議体の行はそのまま残す
    rows = list(keep)
    for s in srcs:
        got = collect_source(s, args.offline, args.refresh_pages, old)
        print(f"[collect] {s.org}: 回 {len({r['session_no'] for r in got})}・行 {len(got)}"
              f"（ファイル {sum(1 for r in got if r.get('path') and r['doc_kind'] != '議事次第ページ')}・"
              f"非公開 {sum(1 for r in got if not r['public'])}・取得できない {sum(1 for r in got if r.get('fetch_error'))}）")
        rows += got
    rows.sort(key=lambda r: (r["org"], r["session_no"], r["doc_kind"] != "議事次第ページ", r.get("path") or "~"))
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[collect] 目録 {len(rows)} 行 → {MANIFEST}")


if __name__ == "__main__":
    main()
