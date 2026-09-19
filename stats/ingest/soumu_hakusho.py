"""
総務省「地方財政白書」資料編 CSV からの取込＝**地方財政計画（当初・億円）**の歳入・歳出（通常収支分）。
設計は docs/データ拡充計画.md（第5弾 追加：地方の予算＝地方財政計画）。

- 各年版の資料編（例 https://www.soumu.go.jp/menu_seisaku/hakusyo/chihou/r08data/2026data/r08czs00-00.html）から
  「第N表 地方財政計画」を題名で見つけ（表番号は版で変わる：r08=120・r06=119）、その1（歳入・通常収支分）＝s-N-1.csv・その4（歳出・通常収支分）＝s-N-4.csv を取る。
- 各表は直近3年度の計画額（億円）。版を新しい順に重ね、同一年度は**新しい版を優先**（前年度分は国会修正等で新版が改定値を載せることがある＝新版を正とし旧値は警告ログのみ）。
- 行ラベルの完全一致（正規化）。値は CSV の文字列（桁区切り除去・'△ 63'→'-63'）。'−' は値なし。
- 原本 CSV は stats/data/cache/soumu/<取得日>/<版>/。

実行（リポジトリ root）：  python -m stats.ingest.soumu_hakusho --all
"""
from __future__ import annotations

import csv
import html as htmllib
import io
import re
import sys
import urllib.error
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.periods import from_wareki_fy
from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, _norm, cache_rel, fetch, finish, http_get, is_numeric, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.soumu_hakusho")
BASE = "https://www.soumu.go.jp/menu_seisaku/hakusyo/chihou/"
# 版：（版接頭辞, データ年）新しい順。無い版はスキップ。
EDITIONS = [("r08", 2026), ("r07", 2025), ("r06", 2024), ("r05", 2023), ("r04", 2022), ("r03", 2021), ("r02", 2020), ("h31", 2019), ("h30", 2018), ("h29", 2017)]


class HakushoError(SourceError):
    pass


def wareki_fy(label: str) -> Optional[str]:
    """見出しセル '令和8年度'／'平成26年度'／'令和元年度' → 'FY2026'…（「年度」必須＝年度見出し行の検出にも使う）。"""
    return from_wareki_fy(label)


def find_table_no(prefix: str, year: int) -> Optional[int]:
    """資料編 index から「第N表 地方財政計画」の N を得る。"""
    try:
        raw, _ = http_get(f"{BASE}{prefix}data/{year}data/{prefix}czs00-00.html", timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # その版は無い
        raise
    h = raw.decode("shift_jis", errors="ignore")
    for u, t in re.findall(r'href="([^"]+)"[^>]*>(.*?)</a>', h, re.S):
        t = _norm(re.sub(r"<[^>]+>", "", htmllib.unescape(t)))
        m = re.match(r"^第(\d+)表地方財政計画$", t)
        if m:
            return int(m.group(1))
    return None


def fetch_csv(prefix: str, year: int, name: str, day: str) -> Optional[Path]:
    url = f"{BASE}{prefix}data/{year}data/csv/{name}.csv"
    try:
        p, _ = fetch(url, day=day, kind="soumu", subdir=f"{prefix}_{year}", timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return p


def _val(text: str) -> Optional[str]:
    """CSV のセル → 値文字列（桁区切り除去・'△ 63'→'-63'）。'−'・空は値なし（None）。"""
    t = (text or "").strip().replace(",", "").replace("△", "-").replace(" ", "")
    return t if is_numeric(t) else None


def parse_table(path: Path, row_label: str) -> dict[str, tuple[str, str]]:
    """返り値 {FY: (値, 行ラベル原文)}。見出し行（'令和8年度' 等）の列位置で年度を決める。"""
    rows = list(csv.reader(io.StringIO(path.read_bytes().decode("shift_jis", errors="strict"))))
    hdr_i = None
    for i, r in enumerate(rows[:8]):
        if any(wareki_fy(c) for c in r):
            hdr_i = i
            break
    if hdr_i is None:
        raise HakushoError(f"{path.name}: 年度見出し行が無い")
    # 年度見出しは 計画額／構成比／増減率 の各ブロックで繰り返す → 最初の出現（計画額ブロック）だけを採る
    cols: dict[int, str] = {}
    seen_fy: set[str] = set()
    for j, c in enumerate(rows[hdr_i]):
        fy = wareki_fy(c)
        if fy and fy not in seen_fy:
            cols[j] = fy
            seen_fy.add(fy)
    want = _norm(row_label)
    hits = [r for r in rows[hdr_i + 1:] if r and _norm(r[0]) == want]
    if len(hits) != 1:
        raise HakushoError(f"{path.name}: 行 {row_label!r} が {len(hits)} 行（1行に確定しない）")
    r = hits[0]
    out = {}
    for j, fy in cols.items():
        if j < len(r):
            v = _val(r[j])
            if v is not None:
                out[fy] = (v, r[0])
    return out


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    a = s.accessor
    if a.get("type") != "soumu_hakusho":
        raise ValueError(f"{s.series_id}: accessor.type={a.get('type')} は soumu_hakusho の対象外")
    part = int(a["part"])  # 1=歳入（通常収支分） 4=歳出（通常収支分）
    merged: dict[str, tuple[str, str, str, Path]] = {}  # FY -> (value, edition, csvname, path)
    for prefix, year in EDITIONS:
        n = find_table_no(prefix, year)
        if n is None:
            continue
        p = fetch_csv(prefix, year, f"s-{n}-{part}", day)
        if p is None:
            continue
        try:
            got = parse_table(p, a["row_label"])
        except HakushoError as e:
            log.warning("%s: %s（この版は飛ばす）", s.series_id, e)
            continue
        for fy, (v, lab) in got.items():
            if fy in merged:
                if merged[fy][0] != v:
                    # 前年度分は国会修正等を反映して新しい版で改定されることがある＝新しい版を正とし、旧版の値は記録だけ残す
                    log.warning("%s: %s は版で値が異なる（採用 %s=%s／旧 %s=%s）", s.series_id, fy, merged[fy][1], merged[fy][0], prefix, v)
                continue  # 新しい版を優先
            merged[fy] = (v, f"{prefix}({year})", f"s-{n}-{part}.csv", p)
    if not merged:
        raise HakushoError(f"{s.series_id}: 値が0件")
    recs = [ValueRecord(series_id=s.series_id, period=fy, region="JP", value=v, status="", vintage=ed, retrieved_at=day,
                        accessor={"type": "soumu_hakusho", "edition": ed, "file": name, "row_label": a["row_label"], "col_fy": fy,
                                  "cache": cache_rel(p)})
            for fy, (v, ed, name, p) in merged.items()]
    log.info("%s: 値 %d（%s〜%s・版 %d）", s.series_id, len(recs), min(merged), max(merged), len({x[1] for x in merged.values()}))
    return finish(s, recs, dry_run=dry_run, exc=HakushoError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(("soumu_hakusho",), ingest_series, "地方財政白書 資料編 CSV（地方財政計画）取込", argv)


if __name__ == "__main__":
    sys.exit(main())
