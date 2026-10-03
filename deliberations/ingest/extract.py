"""
文字の抽出（開発計画 §4.3）。PyMuPDF の既定の get_text（ブロック単位）を使い、NFKC で正規化する。

- スライド：ページごとの本文。ブロックの中の 1〜3 文字の行は直前の行に連結する（図の中で「防|災|医|療」と
  割れる文字列を戻す）。ブロックをまたいでは連結しない。文字数が SPARSE_CHARS に届かないページは
  text_quality=sparse（捨てずに残す＝資料が在る事実と出典は返せる）。
- 記録（議事録・議事要旨）：ページ番号だけの行（「3」「-2-」）を除いた (ページ, 行) の列。行の連結は
  records.py が発言の単位で行う。
NFKC は必須（第 1 便の 55 本に「⼈⼯知能」のような康煕部首の字形が混じる＝正規化しないと BM25 が当たらない）。
"""
import re
import unicodedata
from dataclasses import dataclass

import fitz

from deliberations.core.paths import RAW_DIR

SPARSE_CHARS = 100
PAGENO = re.compile(r"^\s*[-－‐―]?\s*\d{1,3}\s*[-－‐―]?\s*$")


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def nchars(s: str) -> int:
    return len(re.sub(r"\s", "", s))


@dataclass
class Page:
    no: int            # 1 始まり
    text: str
    chars: int
    quality: str       # ok / sparse


def _join_short(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if out and nchars(s) <= 3:
            out[-1] += s
        else:
            out.append(s)
    return out


def slide_pages(path: str) -> list[Page]:
    """スライド・文章の資料のページごとの本文（NFKC・短い行の連結済み）。"""
    pages = []
    for i, pg in enumerate(fitz.open(RAW_DIR / path), 1):
        blocks = [b for b in pg.get_text("blocks") if b[6] == 0]  # 文字のブロックだけ
        parts = []
        for b in blocks:
            lines = [ln for ln in nfkc(b[4]).splitlines() if not PAGENO.match(ln)]
            joined = _join_short(lines)
            if joined:
                parts.append("\n".join(joined))
        text = "\n".join(parts)
        c = nchars(text)
        pages.append(Page(i, text, c, "sparse" if c < SPARSE_CHARS else "ok"))
    return pages


def record_lines(path: str) -> list[tuple[int, str]]:
    """記録の (ページ, 行) の列（NFKC・空行とページ番号の行を除く）。"""
    out = []
    for i, pg in enumerate(fitz.open(RAW_DIR / path), 1):
        for ln in nfkc(pg.get_text()).splitlines():
            if ln.strip() and not PAGENO.match(ln):
                out.append((i, ln.rstrip()))
    return out


def page_lines(path: str, page_no: int) -> list[str]:
    """1 ページの行（NFKC・空行を除く）。表紙・ページ下端の提出者表示の読み取り用。"""
    pg = fitz.open(RAW_DIR / path)[page_no - 1]
    return [ln.strip() for ln in nfkc(pg.get_text()).splitlines() if ln.strip()]
