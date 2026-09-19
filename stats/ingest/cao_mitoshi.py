"""
内閣府「政府経済見通し」（閣議決定・年央試算）PDF の「主要経済指標」表からの取込（第 11 弾 第 7 便・2026-09-15）。

- 要件源＝実利用ログ：名目GDP の FY2026 が out_of_range 18 回・「名目GDP 見通し 2026年度」等の projection 検索 約 10 回。
- 取得元＝https://www5.cao.go.jp/keizai1/mitoshi/mitoshi.html（索引）にある
    r{YY}{MM}{DD}mitoshi.pdf … 「令和N年度の経済見通しと経済財政運営の基本的態度」本文（12 月＝閣議了解・1 月＝閣議決定。**最新日付の 1 本**）
    r{YY}{MM}{DD}shisan.pdf  … 「令和N年度の経済見通しに関する年央試算」（7 月・**最新日付の 1 本**）
  ファイル名の r+YYMMDD が令和年月日＝edition（版）。索引に無ければ取れない（過去版の頁 mitoshikako.html は使わない＝最新版のみ保持＝WEO と同じ判断）。
  内閣府サイトの利用規約＝PDL1.0 準拠（◎・数値は著作権の対象外）。
- 決定論パーサ（PyMuPDF の find_tables）：
  閣議決定本文＝「主要経済指標」を含むページの最初の表。列＝[項目, 実績(FY-2) 兆円, 実績見込み(FY-1) 兆円程度, 見通し(FY) 兆円程度,
    対前年度比 FY-2 名目, FY-2 実質, FY-1 名目, FY-1 実質, FY 名目, FY 実質]。年度は見出し「令和N年度」から（令和 N＝2018+N）。
    項目によっては見出し行（単位）の次の無名行に値が入る（完全失業率・経常収支対名目GDP比）＝直前の見出しの項目として読む。
  年央試算＝「主要経済指標」を含むページの最初の表。列＝[項目, 小項目, 実績(FY-1), 政府経済見通し(FY), 今回試算(FY)]。年度は見出し「YYYY年度」から。
  ▲ は負号（「▲ 0.7」→「-0.7」）・万人の桁区切り「6,968」→「6968」＝**表記の正規化のみ**（丸め・換算なし）。
- **収録する列＝見通し（実績見込み・見通し・今回試算）だけ**。「実績」列（FY-2 の確報値等）は SNA 等の観測系列で引く（見通し系列に混ぜない）。
  値は全て kind=projection（系列の kind）。版が変われば値は差し替わる（旧版は保持しない）。
- accessor: {"type": "cao_mitoshi_pdf", "doc": "mitoshi"|"shisan", "row": "国内総生産", "col": "level"|"nominal_growth"|"real_growth"|"value"}
  （col＝閣議決定本文の列群：level＝兆円（実績見込み・見通し）／nominal_growth・real_growth＝対前年度比／value＝1 列しか無い項目〔完全失業率・変化率〕。
    年央試算は常に value＝今回試算）。
- 原本は stats/data/cache/cao/<取得日>/。

実行（リポジトリ root）：  python -m stats.ingest.cao_mitoshi --all
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import SourceError, cache_rel, fetch, finish, http_get, is_numeric, run_cli, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.cao_mitoshi")
BASE = "https://www5.cao.go.jp/keizai1/mitoshi/"
INDEX = BASE + "mitoshi.html"
TYPES = ("cao_mitoshi_pdf",)
_index_cache: dict[str, dict] = {}
_table_cache: dict[tuple[str, str], tuple[Path, dict, str]] = {}


class MitoshiError(SourceError):
    pass


def reiwa_to_year(n: int) -> int:
    return 2018 + n


def discover(day: Optional[str] = None) -> dict:
    """索引ページから最新の本文 PDF と年央試算 PDF を決める：{"mitoshi": {"file","edition"}, "shisan": {...}}（edition＝YYYY-MM-DD）。"""
    if "index" not in _index_cache:
        body, _ = http_get(INDEX)
        html = body.decode("utf-8", errors="replace")
        out: dict = {}
        for kind in ("mitoshi", "shisan"):
            found = sorted(set(re.findall(rf"(r(\d{{2}})(\d{{2}})(\d{{2}}){kind}\.pdf)", html)))
            if not found:
                raise MitoshiError(f"索引に {kind}.pdf が無い（{INDEX}）")
            f, yy, mm, dd = max(found, key=lambda t: (int(t[1]), int(t[2]), int(t[3])))
            out[kind] = {"file": f, "edition": f"{reiwa_to_year(int(yy))}-{mm}-{dd}"}
        _index_cache["index"] = out
    return _index_cache["index"]


def _norm_num(t: str) -> str:
    """「▲ 0.7」→「-0.7」・「6,968」→「6968」・「(0.1)」（寄与度の括弧）→「0.1」。数値でなければそのまま返す。"""
    s = (t or "").replace("\n", " ").strip().replace("▲", "-").replace("△", "-").replace(",", "")
    s = re.sub(r"\s+", "", s)
    if re.fullmatch(r"\(-?\d+(\.\d+)?\)", s):
        s = s[1:-1]
    return s


def _fy_of_reiwa(label: str) -> Optional[str]:
    m = re.search(r"令和\s*([０-９\d]+)\s*年度", label or "")
    if not m:
        return None
    n = int(m.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789")))
    return f"FY{reiwa_to_year(n)}"


def load_table(doc: str, day: str) -> tuple[Path, dict, str]:
    """PDF を取得し「主要経済指標」のページの最初の表を {項目: [セル…]} に読む。返り値＝(キャッシュ先, 表, edition)。"""
    key = (doc, day)
    if key not in _table_cache:
        info = discover(day)[doc]
        cp, _ = fetch(BASE + info["file"], day=day, kind="cao", skip_if_exists=True, name=info["file"])
        import fitz
        d = fitz.open(cp)
        pages = [i for i in range(len(d)) if "主要経済指標" in d[i].get_text()]
        if not pages:
            raise MitoshiError(f"{info['file']}: 「主要経済指標」のページが無い")
        pg = d[pages[0]]
        tabs = pg.find_tables().tables
        if not tabs:
            raise MitoshiError(f"{info['file']}: p{pages[0] + 1} に表が検出できない")
        grid = [[(c or "").strip() for c in row] for row in tabs[0].extract()]  # 改行は残す（縦結合セルの 1 行目が項目名＝年央試算）
        _table_cache[key] = (cp, {"grid": grid, "file": info["file"], "page": pages[0] + 1}, info["edition"])
    return _table_cache[key]


def parse_mitoshi(grid: list[list[str]]) -> tuple[dict[str, list[str]], list[str]]:
    """閣議決定本文の表→({項目: 10 セル}, [FY-2, FY-1, FY])。無名行は直前の見出し項目に帰属させる。"""
    head = grid[0]
    fys = [_fy_of_reiwa(c) for c in head[1:4]]
    if any(f is None for f in fys):
        raise MitoshiError(f"見出しの年度が読めない: {head[:4]}")
    rows: dict[str, list[str]] = {}
    pending = ""
    for r in grid[3:]:
        label = re.sub(r"\s+", "", r[0])
        cells = [_norm_num(c) for c in r[1:]]
        if label:
            if any(is_numeric(c) for c in cells):
                rows[label] = cells
                pending = ""
            else:
                pending = label  # 単位だけの見出し行（完全失業率 等）＝次の無名行が値
        elif pending and any(is_numeric(c) for c in cells):
            rows[pending] = cells
            pending = ""
    return rows, [str(f) for f in fys]


def parse_shisan(grid: list[list[str]]) -> tuple[dict[str, list[str]], list[str]]:
    """年央試算の表→({項目: [実績, 政府経済見通し, 今回試算]}, [FY-1, FY])。"""
    head = grid[0]
    yrs = [re.search(r"(\d{4})年度", c or "") for c in head]
    fys = [f"FY{m.group(1)}" for m in yrs if m]
    if len(fys) != 2:
        raise MitoshiError(f"見出しの年度が 2 つに確定しない: {head}")
    rows: dict[str, list[str]] = {}
    for r in grid[2:]:
        # 1 列目は縦結合セル（「実質国内総生産（ＧＤＰ）」の下に小項目が連なる）＝1 行目だけが項目名。小項目は 2 列目。
        label = re.sub(r"\s+", "", (r[0] or r[1] or "").split("\n")[0])
        cells = [_norm_num(c) for c in r[2:5]]
        if label and any(is_numeric(c) for c in cells):
            rows[label] = cells
    return rows, fys


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    day = day or today()
    a = s.accessor
    if a.get("type") not in TYPES:
        raise ValueError(f"{s.series_id}: accessor.type={a.get('type')} は対象外")
    doc, row, col = str(a["doc"]), str(a["row"]), str(a.get("col", "value"))
    cp, tbl, edition = load_table(doc, day)
    recs: list[ValueRecord] = []
    base = {"type": "cao_mitoshi_pdf", "doc": doc, "row": row, "col": col, "file": tbl["file"], "page": tbl["page"], "edition": edition,
            "cache": cache_rel(cp)}
    if doc == "mitoshi":
        rows, fys = parse_mitoshi(tbl["grid"])
        if row not in rows:
            raise MitoshiError(f"{s.series_id}: 項目「{row}」が表に無い（{sorted(rows)[:12]}…）")
        cells = rows[row]
        # 列群：level＝[FY-2, FY-1, FY]（実績・実績見込み・見通し）／growth＝[FY-2 名目, FY-2 実質, FY-1 名目, FY-1 実質, FY 名目, FY 実質]
        if col == "level":
            picks = [(fys[1], cells[1]), (fys[2], cells[2])]
        elif col == "nominal_growth":
            picks = [(fys[1], cells[5]), (fys[2], cells[7])]
        elif col == "real_growth":
            picks = [(fys[1], cells[6]), (fys[2], cells[8])]
        elif col == "value":  # 1 列群だけの項目（完全失業率・変化率）＝[FY-2, FY-1, FY]
            picks = [(fys[1], cells[1]), (fys[2], cells[2])]
        else:
            raise MitoshiError(f"{s.series_id}: col={col!r} は対象外")
    elif doc == "shisan":
        rows, fys = parse_shisan(tbl["grid"])
        if row not in rows:
            raise MitoshiError(f"{s.series_id}: 項目「{row}」が年央試算の表に無い（{sorted(rows)[:12]}…）")
        picks = [(fys[1], rows[row][2])]  # 今回試算（FY）だけ。実績列と政府経済見通し列は取らない
    else:
        raise MitoshiError(f"{s.series_id}: doc={doc!r} は対象外")
    for fy, v in picks:
        if not v:
            continue  # 空欄（該当なし）＝値を作らない
        if not is_numeric(v):
            raise MitoshiError(f"{s.series_id}: {fy} の値「{v}」が数値でない")
        recs.append(ValueRecord(series_id=s.series_id, period=fy, region="JP", value=v, status="", vintage=edition, retrieved_at=day,
                                published_at=edition, accessor={**base, "period": fy}, kind="projection"))
    if not recs:
        raise MitoshiError(f"{s.series_id}: 値が0件（doc={doc} row={row} col={col}）")
    log.info("%s: %s（%s・p%d）→ 値 %d（%s）", s.series_id, tbl["file"], edition, tbl["page"], len(recs), "・".join(p for p, _ in picks))
    return finish(s, recs, dry_run=dry_run, exc=MitoshiError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(TYPES, ingest_series, "内閣府 政府経済見通し（閣議決定・年央試算）PDF 主要経済指標の取込", argv)


if __name__ == "__main__":
    sys.exit(main())
