"""
収集：会議体ごとの一覧ページから回を見つけ、回のページの資料と一覧の記録を取得して目録を書く。

    python -m deliberations.ingest.collect                 # 一覧を取り直し、新しい回・手元に無いファイルだけ取得
    python -m deliberations.ingest.collect --org dgk       # 会議体を絞る
    python -m deliberations.ingest.collect --offline       # 通信せず、手元の原文から目録を作り直す
    python -m deliberations.ingest.collect --refresh-pages # 既知の回のページも取り直す（資料の差し替えの確認）

目録（data/raw/manifest.jsonl）は 1 行 1 件：議事次第ページ・資料・参考資料・記録・非公開（名前だけ）。
手元にあるファイルは取り直さない（sha256 は手元のファイルから計算）。取得の作法は sources.py。

慎重に取るホスト（sources.SLOW_HOSTS＝経済産業省）は、間隔をあけ・1 回の実行の本数を抑え・WAF のチャレンジが出たら
その実行の間は送らない（解かない）。通信で見なかったもの（本数の上限・チャレンジの後・--offline で手元に無いもの）は、
最後に会議体ごとに一覧で示し data/raw/_collect_report.json に書く（黙って手元の古いもので済ませない）。

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
                                          SLOW_HOSTS, SOURCES, Source)

JST = timezone(timedelta(hours=9))
RECORD_LABEL = re.compile(r"議事録|議事要旨|議事概要")
NONPUBLIC = re.compile(r"[【（(](非公開|非公表)[】）)]|※\s*(非公開|非公表)")  # 「※非公表」＝デジタル庁
FILE_EXT = re.compile(r"(?i)\.(pdf|xlsx?|docx?|pptx?)$")
_last: dict[str, float] = {}       # ホスト → 前の要求の時刻
_sent: dict[str, int] = {}         # ホスト → この実行で送った本数（SLOW_HOSTS の budget と比べる）
_blocked: dict[str, str] = {}      # チャレンジを返したホスト → 理由（この実行の間は送らない）
_fresh: set[str] = set()           # この実行で取り直した手元のパス（一覧の先取りを二重に取らない）
REPORT = RAW_DIR / "_collect_report.json"


class WafChallenge(RuntimeError):
    """取得元が JS チャレンジ（人の確認）を返した＝解かない。"""


def now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def fetch(url: str) -> bytes | None:
    """取得（curl）。失敗は None。1 リクエストごとに REQUEST_INTERVAL 秒（SLOW_HOSTS はそのホストの interval）以上あける。
    WAF のチャレンジ（HTTP 202 か x-amzn-waf-action ヘッダ）は WafChallenge。"""
    host = urlparse(url).hostname or ""
    gap = SLOW_HOSTS.get(host, {}).get("interval", REQUEST_INTERVAL)
    wait = gap - (time.time() - _last.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    args = ["curl", "-s", "-L", "--max-time", "120", "-D", "-", "-w", "\n%{http_code}"]
    if host in BROWSER_UA_HOSTS:
        args += ["-A", BROWSER_UA]
    r = subprocess.run(args + [url], capture_output=True)
    _last[host] = time.time()
    _sent[host] = _sent.get(host, 0) + 1
    out, _, code = r.stdout.rpartition(b"\n")
    head, body = _split_headers(out)
    code = code.decode(errors="replace").strip()
    if code == "202" or re.search(rb"(?im)^x-amzn-waf-action:", head):
        raise WafChallenge(f"WAF のチャレンジ（HTTP {code}）: {url}")
    if code != "200":
        print(f"[collect] 取得失敗 HTTP {code}: {url}", file=sys.stderr)
        return None
    return body


def _split_headers(out: bytes) -> tuple[bytes, bytes]:
    """curl -D - の出力を（最後の応答のヘッダ, 本文）に分ける（-L でリダイレクトをたどると応答のヘッダが続けて並ぶ）。"""
    head = b""
    while out.startswith(b"HTTP/"):
        h, sep, rest = out.partition(b"\r\n\r\n")
        if not sep:
            break
        head, out = h, rest
    return head, out


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _strip_noise(s: str) -> str:
    # HTML のコメントも除く（規制改革推進会議の回のページは差し替え前のリンクをコメントの中に残している）
    return re.sub(r"(?is)<!--.*?-->|<(script|style|head)\b.*?</\1>", "", s)


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
    # 「令和7年（2025年）9月18日」（デジタル庁）の西暦の括弧も読み飛ばす
    m = re.search(r"令和\s*([0-9]+|元)\s*年\s*(?:[（(]\s*\d{4}\s*年\s*[）)])?\s*([0-9]+)\s*月\s*([0-9]+)\s*日", t)
    if not m:  # 西暦だけのページ（経済産業省など）＝令和の日付が無いときだけ（第 1〜3 便の会議体は令和で不変）
        w = re.search(r"(20\d{2})\s*年\s*([0-9]+)\s*月\s*([0-9]+)\s*日", t)
        return f"{w.group(1)}-{int(w.group(2)):02d}-{int(w.group(3)):02d}" if w else None
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


LABEL_NO = re.compile(r"^((?:参考資料|資料)\s*[0-9０-９][0-9０-９\-－‐ー―の・]*|参考資料(?=\s))\s*(.+)$")
# 分割版（1 つの資料を複数の PDF に分けて載せる型＝リンクの文字列は「１（PDF形式…）」だけ）。資料名と番号は最初の
# 部分の前の文字列「資料１－２ GOGEN株式会社 御提出資料 分割版：」にあり、2 つ目以降は「／」の後に続く
# 回のページの中の、資料ではないリンク（「※最終版は こちら に掲載」の「こちら」・英語版）＝内閣官房 日本成長戦略
SKIP_LABEL = re.compile(r"^こちら$|English")
SPLIT_PART = re.compile(r"^([0-9０-９]{1,2})\s*[（(]PDF")


def split_label(prefix: str, label: str) -> tuple[str, str]:
    """資料番号がリンクの文字列の先頭にある型（規制改革推進会議「資料１ー１ 経済産業省提出資料」）を
    (prefix, label) に分ける。前のセルに番号がある型（prefix に番号あり）はそのまま。"""
    if material_no(prefix) or not (m := LABEL_NO.match(label)):
        return prefix, label
    return m.group(1), m.group(2).strip()


def row_text_before(page: str, href: str) -> str:
    """リンクの前の、同じ表の行（<tr>・<li>・<p> の始まりから）の文字列。分割版の資料名が <br> の前の
    行の中にある型（規制改革推進会議 第 3 回）で、名前と番号を読む。"""
    s = _strip_noise(page)
    tail = href.rsplit("/", 1)[-1]
    i = s.find(tail)
    if i < 0:
        return ""
    j = max(s.rfind(t, 0, i) for t in ("<tr", "<li", "<p>", "<p "))
    return _text(s[j if j >= 0 else 0: s.rfind("<a", 0, i)])


def _local(path: str) -> bytes | None:
    p = RAW_DIR / path
    return p.read_bytes() if p.exists() else None


def _hold_reason(host: str) -> str | None:
    """このホストに今は送らない理由（無ければ None）。"""
    if host in _blocked:
        return "チャレンジの後"
    budget = SLOW_HOSTS.get(host, {}).get("budget")
    if budget is not None and _sent.get(host, 0) >= budget:
        return f"1 回の実行の上限 {budget} 本"
    return None


def _get(url: str, path: str, offline: bool, refetch: bool = False,
         unseen: list | None = None) -> tuple[bytes | None, bool]:
    """手元にあれば手元（refetch のときは取り直す）、無ければ取得して保存。(内容, 今回取得したか)。
    unseen＝通信で見なかったもの（送らない理由・手元にあるか）を積む先（会議体ごと）。"""
    have = _local(path)
    if have is not None and (not refetch or path in _fresh):
        return have, False
    reason = "通信なし（--offline）" if offline else _hold_reason(urlparse(url).hostname or "")
    if reason is None:
        try:
            body = fetch(url)
        except WafChallenge as e:
            _blocked[urlparse(url).hostname or ""] = str(e)
            print(f"[collect] ★ {e}＝解かない。このホストへはこの実行の間は送らない", file=sys.stderr)
            reason = "チャレンジ"
    if reason is not None:
        if unseen is not None and (have is None or not offline) and path not in {x["path"] for x in unseen}:
            unseen.append({"path": path, "url": url, "reason": reason, "have_local": have is not None})
        return have, False
    if body is None:
        return have, False
    _fresh.add(path)
    p = RAW_DIR / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return body, True


def _group_no(src: Source, m: re.Match) -> int:
    """href の一致 → 回次（値のある最初の括弧の組）。回の番号が無い会議体の 6 桁（YYMMDD）は 20YYMMDD に読む。"""
    g = next(x for x in m.groups() if x)
    return int("20" + g) if src.session_is_date and len(g) == 6 else int(g)


def _session_no(src: Source, a: dict) -> int | None:
    """一覧のリンク → 回次。回の URL に回次があればそこから（括弧の組 1）、無ければリンクの文字列の「第N回」から。"""
    m = re.search(src.session_href, a["href"])
    if not m:
        return None
    if m.groups():
        return _group_no(src, m)
    k = re.search(r"第\s*(\d+)\s*回", unicodedata.normalize("NFKC", a["label"]))
    return int(k.group(1)) if k else None


def index_pages(src: Source) -> list[tuple[str, str]]:
    """会議体の一覧ページの (URL, 手元のパス)。2 枚目以降は _index2.html…。"""
    return [(u, f"{src.org}/_index{i + 1 if i else ''}.html") for i, u in enumerate((src.index,) + src.more_index)]


def collect_source(src: Source, offline: bool, refresh_pages: bool, old: dict,
                   unseen: list | None = None) -> list[dict]:
    """1 会議体分の目録の行を返す。unseen＝通信で見なかったものを積む先。"""
    rows: list[dict] = []
    links: list[dict] = []
    for iu, ipath in index_pages(src):
        idx_raw, _ = _get(iu, ipath, offline, refetch=not offline, unseen=unseen)
        if idx_raw is None:
            print(f"[collect] {src.org}: 一覧ページが無い（offline で手元にも無い）: {iu}", file=sys.stderr)
            return rows
        links += anchors(idx_raw.decode("utf-8", "replace"), iu, from_h1=False)
    sessions = sorted({n: a["href"] for a in links
                       if (n := _session_no(src, a)) is not None and n >= src.min_session}.items())
    records: dict[int, list[dict]] = {}
    for a in links:
        m = re.search(src.record_href, a["href"])
        if m and src.record_label and not RECORD_LABEL.search(a["label"]):
            a = {**a, "label": src.record_label}  # 「PDF形式：…」だけのリンク＝一覧の列の見出しの語
        if m and RECORD_LABEL.search(a["label"]):
            n = _group_no(src, m)
            if a["href"] not in {x["href"] for x in records.get(n, [])}:
                records.setdefault(n, []).append(a)

    for n, url in sessions:
        d = f"{src.org}/{n:02d}"
        raw, fetched = _get(url, f"{d}/_page.html", offline, refetch=refresh_pages, unseen=unseen)
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
        split_ctx: tuple[str, str] | None = None
        for a in items:
            if a["href"] in done or SKIP_LABEL.search(a["label"]) or (
                    src.skip_material and re.search(src.skip_material, a["prefix"] + a["label"])):
                continue
            done.add(a["href"])
            path = f"{d}/{a['href'].rsplit('/', 1)[-1]}"
            body, got = _get(a["href"], path, offline, unseen=unseen)
            prefix, label = split_label(a["prefix"], a["label"])
            if sp := SPLIT_PART.match(label):
                if "分割版" in prefix:
                    base = prefix.split("分割版")[0].strip() or row_text_before(page, a["href"]).split("分割版")[0].strip()
                    bp, bl = split_label("", base)
                    split_ctx = (bp, bl)
                if split_ctx:
                    prefix, label = split_ctx[0], f"{split_ctx[1]}（分割版 {unicodedata.normalize('NFKC', sp.group(1))}）"
            else:
                split_ctx = None
            kind = kind_candidate(prefix, label)
            rows.append({**meta, "material_name": label, "material_no": material_no(prefix),
                         "list_prefix": prefix, "doc_kind": kind, "source_url": a["href"],
                         "path": path if body is not None else None, "public": True,
                         "fetch_error": None if body is not None else _missing(unseen, path)})
            if got:
                print(f"[collect] 取得 {path}（{len(body):,} バイト）")
        for it in nonpublic_items(page):
            rows.append({**meta, "material_name": it["label"], "material_no": material_no(it["prefix"]),
                         "list_prefix": it["prefix"], "doc_kind": kind_candidate(it["prefix"], it["label"]),
                         "source_url": url, "path": None, "public": False})
    # 同じ回に議事録（逐語）と議事要旨の両方があれば、議事要旨は目録に残すが検索の単位にしない（同じ発言の二重を避ける）
    verbatim = {r["session_no"] for r in rows if r["doc_kind"] == "記録" and "議事録" in r["material_name"]}
    for r in rows:
        if r["doc_kind"] == "記録" and r["session_no"] in verbatim and "議事録" not in r["material_name"]:
            r["superseded"] = True
    for r in rows:
        if r.get("path"):
            b = (RAW_DIR / r["path"]).read_bytes()
            r["bytes"] = len(b)
            r["sha256"] = hashlib.sha256(b).hexdigest()
            prev = old.get(r["path"])
            r["fetched_at"] = prev["fetched_at"] if prev and prev.get("sha256") == r["sha256"] else now()
    return rows


def _missing(unseen: list | None, path: str) -> str:
    hit = next((x for x in unseen or [] if x["path"] == path), None)
    return f"通信で見ていない（{hit['reason']}）" if hit else "取得できない"


def report(unseen: dict[str, list], offline: bool) -> None:
    """通信で見なかったものを会議体ごとに示し、data/raw/_collect_report.json に書く。"""
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({"checked_at": now(), "offline": offline, "sent": _sent, "blocked": _blocked,
                                  "unseen": unseen}, ensure_ascii=False, indent=1), encoding="utf-8")
    if offline:
        print(f"[collect] ★ 通信なし（--offline）＝一覧を見ていない＝新しい回があっても分からない → {REPORT}")
    for org, xs in unseen.items():
        if not xs or offline and all(x["have_local"] for x in xs):
            continue
        print(f"[collect] ★ {org}：通信で見ていないもの {len(xs)} 件（ブラウザで保存して --offline で目録を作れる）")
        for x in xs:
            what = ("一覧（新しい回があっても分からない）" if "/_index" in x["path"] else
                    "回のページ" if x["path"].endswith("_page.html") else "ファイル")
            print(f"    {what}・{x['reason']}{'・手元の古いもので読んだ' if x['have_local'] else ''}: "
                  f"data/raw/{x['path']}  ← {x['url']}")


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
    unseen: dict[str, list] = {s.org: [] for s in srcs}
    if not args.offline:  # 本数の上限のあるホストは一覧を先に取る（新しい回を知るのが先＝回のページ・ファイルは残りの本数で）
        for s in srcs:
            for iu, ipath in index_pages(s):
                if (urlparse(iu).hostname or "") in SLOW_HOSTS:
                    _get(iu, ipath, False, refetch=True, unseen=unseen[s.org])
    for s in srcs:
        got = collect_source(s, args.offline, args.refresh_pages, old, unseen[s.org])
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
    report(unseen, args.offline)


if __name__ == "__main__":
    main()
