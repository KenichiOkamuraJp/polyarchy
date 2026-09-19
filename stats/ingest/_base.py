"""
取込モジュールの共通部品（取得・キャッシュ・数値判定・重複検査・表読みの小物・CLI・書き出し尾部）。

**表の読み方そのもの（どの行・どの列を採るか）は共通化しない**＝原典固有で「1 行／1 列に確定しなければエラー」の要石は各モジュールに残す。

- `http_get(url)` … UA は curl 相当が既定（取得の作法＝stats/README.md。上書きするのは UA 文字列で弾く pdf_hakusho だけ。IMF は "Mozilla/5.0"・独自名を、OECD は "Python-urllib" を 403 にする＝2026-08 観測）。
  一過性エラーは指数バックオフで再試行（世銀は 1 系列 46 回投げるため 400/429/5xx が散発）。404 は即送出。
- `fetch(url, day=…, kind=…)` … 原本を `stats/data/cache/<kind>/<取得日>/<ファイル名>` に保存し、`Last-Modified` を公表日（YYYY-MM-DD）として返す。
  `accessor.cache` はこのパスの DATA_DIR 相対（exact_match の照合対象＝変えない）。
- `is_numeric(raw)` … 値として保存してよい文字列（符号・小数・指数表記。桁区切り・"-"・"…"・"x" は不可）。
- `assert_unique(recs)` … 同一 (period, region) に別の値があればエラー（セル指定が甘い証拠＝黙って上書きしない）。
- `run_cli(types, ingest_series, description)` … `--series/--all/--dry-run`・終了コード 0/2・`合計 N 値／M 系列` の定型。
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import re
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Callable, Iterable, Optional

from polyarchy_common.logsetup import get_logger

from stats.core.paths import CACHE_DIR, DATA_DIR
from stats.core.registry import Registry, Series, default_registry
from stats.core.values import ValueRecord, write_values

log = get_logger("polyarchy.stats.ingest")

USER_AGENT = "curl/8.7.1"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}
_WS = re.compile(r"\s+")  # str パターンの \s は全角空白（U+3000）・NBSP（U+00A0）も含む
_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?$")


class SourceError(RuntimeError):
    """取得元・セル指定の不備（「該当データがない」とは区別する）。各モジュールの例外はこれを継承する。"""


# ---------------------------------------------------------------- 取得・キャッシュ

def http_get(url: str, *, timeout: int = 120, retries: int = 4, headers: Optional[dict] = None) -> tuple[bytes, str]:
    """GET。返り値：(本文, Last-Modified 由来の公表日 'YYYY-MM-DD' か '')。404 は即送出、それ以外は指数バックオフで再試行。"""
    hdrs = DEFAULT_HEADERS if headers is None else headers
    last: Exception = RuntimeError(url)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                lm = r.headers.get("Last-Modified", "")
            return body, _parse_http_date(lm)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                raise
            if attempt < retries - 1:
                wait = 2 ** attempt * 3
                if isinstance(e, urllib.error.HTTPError) and e.code == 429:  # レート制限（OECD SDMX 等）：Retry-After か 30 秒以上で待つ
                    ra = (e.headers or {}).get("Retry-After", "") if hasattr(e, "headers") else ""
                    wait = max(wait, int(ra) if str(ra).isdigit() else 30 * (attempt + 1))
                log.warning("取得失敗（%s）: %s ＝ %s 秒後に再試行 %d/%d", e, url, wait, attempt + 2, retries)
                time.sleep(wait)
    raise last


def _parse_http_date(lm: str) -> str:
    if not lm:
        return ""
    try:
        return email.utils.parsedate_to_datetime(lm).date().isoformat()
    except (TypeError, ValueError):
        return ""


def cache_path(kind: str, day: str, name: str, subdir: str = "") -> Path:
    """原本の置き場 `stats/data/cache/<kind>/<day>[/<subdir>]/<name>`（親ディレクトリを作る）。"""
    p = CACHE_DIR / kind / day / subdir / name if subdir else CACHE_DIR / kind / day / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def cache_rel(p: Path) -> str:
    """accessor.cache に刻む DATA_DIR 相対パス。"""
    return str(p.relative_to(DATA_DIR))


def fetch(url: str, *, day: str, kind: str, timeout: int = 120, retries: int = 4, skip_if_exists: bool = False,
          name: str = "", subdir: str = "", headers: Optional[dict] = None) -> tuple[Path, str]:
    """原本を取得してキャッシュ。返り値：(キャッシュ先, 公表日=Last-Modified の YYYY-MM-DD か '')。
    skip_if_exists=True で同日キャッシュがあれば取得しない（公表日は '' になる）。"""
    out = cache_path(kind, day, name or Path(url).name, subdir)
    if skip_if_exists and out.exists():
        return out, ""
    body, published = http_get(url, timeout=timeout, retries=retries, headers=headers)
    out.write_bytes(body)
    return out, published


# ---------------------------------------------------------------- 値の判定・検査

def is_numeric(raw: str) -> bool:
    """値として保存してよい文字列か（符号・小数・指数表記。桁区切り・"-"・"…"・"x"・"--" は不可）。変換はしない。"""
    return bool(_NUM_RE.match((raw or "").strip()))


def assert_unique(recs: Iterable[ValueRecord], *, exc: type[Exception] = SourceError) -> None:
    """同一 (period, region) に**異なる**値があればエラー（同値の重複は許す）。黙って上書きしない。"""
    seen: dict[tuple[str, str], str] = {}
    for r in recs:
        k = (r.period, r.region)
        if k in seen and seen[k] != r.value:
            raise exc(f"{r.series_id}: {r.period}{'' if r.region == 'JP' else '/' + r.region} に複数の値（{seen[k]} / {r.value}）＝アクセサの指定不足")
        seen[k] = r.value


def finish(s: Series, recs: list[ValueRecord], *, dry_run: bool, exc: type[Exception] = SourceError) -> int:
    """書き出し尾部：0 件はエラー → 重複検査 → 書き出し（dry_run なら書かない）→ 件数。"""
    if not recs:
        raise exc(f"{s.series_id}: 値が0件")
    assert_unique(recs, exc=exc)
    if not dry_run:
        write_values(s.series_id, recs)
    return len(recs)


# ---------------------------------------------------------------- 表読みの小物（行ラベル・表示書式・列記号）

def _norm(label: object) -> str:
    """行ラベル等の正規化：空白（全角・NBSP・改行含む）を除去。表記ゆれ（'５．　国内総生産'）を吸収する。"""
    return _WS.sub("", str(label or ""))


def _decimals(number_format: str) -> Optional[int]:
    """表示書式から小数桁を取る（'#,##0.0' → 1・'#,##0' → 0）。判別できなければ None。"""
    fmt = (number_format or "").split(";")[0]
    if not fmt or fmt == "General":
        return None
    m = re.search(r"\.([0#]+)", fmt)
    if m:
        return len(m.group(1))
    return 0 if re.search(r"[0#]", fmt) else None


def cell_text(value: object, number_format: str) -> Optional[str]:
    """公表どおりの表示文字列（桁区切りは付けない）＝セルの表示書式の小数桁で四捨五入。数値でない・書式から桁を取れなければ None。"""
    if value is None or isinstance(value, str):
        return None
    if not isinstance(value, (int, float)):
        return None
    nd = _decimals(number_format)
    if nd is None:
        return None
    q = Decimal(1).scaleb(-nd)
    d = Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP)
    s = format(d, "f")
    return "0" if s in ("-0", "-0.0") else s


def _pick_row(labels: dict[int, str], row_label: str, occurrence: int = 0) -> int:
    """行ラベル辞書（行番号1始まり→正規化済みラベル）から**1行だけ**確定する。xlsx/xls 共通。
    正規化完全一致を優先、無ければ前方一致。0 行・複数行（occurrence 未指定）はエラー＝黙って選ばない。"""
    want = _norm(row_label)
    if not want:
        raise SourceError("accessor.row_label が未設定")
    exact = [r for r, v in labels.items() if v == want]
    hits = exact if len(exact) == 1 else [r for r, v in labels.items() if v.startswith(want)]
    if occurrence:
        if len(hits) < occurrence:
            raise SourceError(f"行ラベル {row_label!r} の出現 {occurrence} 番目が無い（一致 {len(hits)} 行）")
        return hits[occurrence - 1]
    if len(hits) != 1:
        raise SourceError(f"行ラベル {row_label!r} に一致する行が {len(hits)} 行（1行に確定しない・occurrence を指定）: {hits[:5]}")
    return hits[0]


def _col_letter(idx0: int) -> str:
    """0始まりの列番号 → A1 形式の列記号（accessor.cell 用）。"""
    s, n = "", idx0 + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _col_index(letter: str) -> int:
    """A1 形式の列記号 → 1始まりの列番号（'A'→1・'AA'→27）。"""
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


# ---------------------------------------------------------------- CLI 定型

def today() -> str:
    return dt.date.today().isoformat()


def run_cli(types: tuple[str, ...], ingest_series: Callable[..., int], description: str,
            argv: Optional[list[str]] = None) -> int:
    """`--series <id>`（複数可）／`--all`（status=registered かつ accessor.type ∈ types）／`--dry-run`。
    終了コード：0＝完了・2＝対象なし／未登録。各系列の件数と `合計 N 値／M 系列` を表示。"""
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--series", action="append", default=[], help="series_id（複数可）")
    ap.add_argument("--all", action="store_true", help=f"registered かつ accessor.type ∈ {list(types)} の全系列")
    ap.add_argument("--dry-run", action="store_true", help="取得だけして書かない")
    a = ap.parse_args(argv)
    reg: Registry = default_registry()
    if a.all:
        targets = [s for s in reg.series.values() if s.status == "registered" and s.accessor.get("type") in types]
    else:
        targets = []
        for sid in a.series:
            s = reg.get(sid)
            if s is None:
                print(f"未登録: {sid}", file=sys.stderr)
                return 2
            targets.append(s)
    if not targets:
        print("対象なし（--series か --all）", file=sys.stderr)
        return 2
    total = 0
    failed: list[tuple[str, str]] = []
    for s in targets:
        try:
            n = ingest_series(s, dry_run=a.dry_run)
        except SourceError as e:  # 取得元のエラーは系列ごとに記録して続行（1 セルの「該当データなし」で全体を止めない）
            print(f"{s.series_id}: 失敗 {e}", file=sys.stderr)
            failed.append((s.series_id, str(e)))
            continue
        print(f"{s.series_id}: {n} 値{'（dry-run）' if a.dry_run else ''}")
        total += n
    print(f"合計 {total} 値／{len(targets) - len(failed)} 系列" + (f"（失敗 {len(failed)} 系列）" if failed else ""))
    for sid, msg in failed:
        print(f"  失敗: {sid}: {msg}")
    return 1 if failed else 0
