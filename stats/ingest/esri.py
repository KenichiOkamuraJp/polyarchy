"""
内閣府 ESRI 国民経済計算（年次推計）xlsx からの取込（原本キャッシュ→セル指定で値抽出→値ストア）。
設計は docs/データソース選定.md §1・§2.1（M1–M3）・参照粒度設計.md §6（accessor＝原典の同じセルに戻れること）。

- 取得元は ESRI 一次 xlsx（e-Stat の同表は副）。URL は `https://www.esri.cao.go.jp/jp/sna/data/data_list/<file>`。
  旧基準（1990年基準・68SNA／2000年基準・93SNA）の年次推計は .xls（BIFF）＝accessor.type=esri_xls（xlrd で読む）。
  旧基準は現行基準と**接続しない別系列**として持つ（dataset=sna1990 / sna2000。基準改定で定義も水準も変わる）。
- 原本は `stats/data/cache/esri/<取得日>/<ファイル名>` にそのまま保存（完全一致テストの根拠）。
- **値は「公表どおりの表示文字列」**：xlsx のセルは丸め前の二進小数（例 534706.19999999995）なので、
  セルの表示書式（#,##0.0 等）の小数桁で四捨五入した文字列を値とする。桁を書式から取れないセルは採らない。
- 行はラベル（第1列）の**正規化一致**で1行に確定する。0行・複数行なら黙って選ばずエラー（捏造防止）。
- 列は見出し行の西暦（1994 …）を period_converter で正規表記へ決定論変換。
- 基準改定（2015→2020年基準）で過去値が書き換わる＝vintage（原本の Last-Modified）と edition を必ず刻む。

実行（リポジトリ root）：
    python -m stats.ingest.esri --series cao.sna2020.gdp_nominal.fy
    python -m stats.ingest.esri --all            # status=registered かつ accessor.type=esri_xlsx の全系列
    python -m stats.ingest.esri --all --dry-run  # 取得だけして書かない
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.periods import convert, matches_freq
from stats.core.registry import Series
from stats.core.values import ValueRecord
from stats.ingest._base import (SourceError, _col_letter, _norm, _pick_row, cache_rel, cell_text, fetch, finish, is_numeric,
                                run_cli, today)

configure_quiet_logging()
log = get_logger("polyarchy.stats.ingest.esri")

BASE = "https://www.esri.cao.go.jp/jp/sna/data/data_list/"
TYPES = ("esri_xlsx", "esri_xls", "esri_qe_csv", "esri_xlsx_yearsheets")


class EsriSourceError(SourceError):
    """取得元・セル指定の不備（「該当データがない」とは区別する）。"""


def resolve_file(accessor: dict) -> str:
    """accessor.file の {Y} を accessor.edition（年次推計の年＝2024 等）で埋める。"""
    f = str(accessor.get("file", ""))
    if not f:
        raise EsriSourceError("accessor.file が未設定")
    y = str(accessor.get("edition", ""))
    if "{Y}" in f:
        if not y:
            raise EsriSourceError(f"accessor.file に {{Y}} があるが edition が未設定: {f}")
        f = f.replace("{Y}", y)
    return f


def fetch_file(rel_path: str, day: str, *, timeout: int = 120) -> tuple[Path, str]:
    """原本 xlsx/xls/CSV を取得してキャッシュ。返り値：(キャッシュ先, 公表日=Last-Modified の YYYY-MM-DD)。UA は他モジュールと同じ curl 相当。"""
    return fetch(BASE + rel_path, day=day, kind="esri", timeout=timeout)


def find_row(ws, row_label: str, occurrence: int = 0) -> int:
    """openpyxl のシートから行を1行に確定する（第1列のラベルで正規化一致）。"""
    return _pick_row({r: _norm(ws.cell(r, 1).value) for r in range(1, ws.max_row + 1)}, row_label, occurrence)


def _qe_value(text: str) -> Optional[str]:
    """QE CSV のセル '521,929.2 ' → '521929.2'。空・非数値は None。"""
    t = (text or "").replace(",", "").strip()
    return t if is_numeric(t) else None


def ingest_qe_csv(s: Series, *, dry_run: bool, day: str) -> int:
    """四半期別GDP速報（QE）の CSV（横持ち・行＝四半期・列＝項目・年は先頭行のみ）。
    accessor: file（{Q}=公表回 例 2612・{Q1}=qe261_2 のようなディレクトリ）, col_header（3行目の項目見出し）, sub_header（4行目・任意）"""
    import csv
    import io
    acc = s.accessor
    q = str(acc.get("edition", ""))
    rel = str(acc["file"]).replace("{Q}", q).replace("{QDIR}", str(acc.get("qdir", "")))
    cp, published = fetch_file(rel, day)
    text = cp.read_bytes().decode("shift_jis", errors="strict")
    rows = list(csv.reader(io.StringIO(text)))
    hdr_row = int(acc.get("header_row", 3)) - 1
    sub_row = int(acc.get("sub_header_row", 4)) - 1
    want = _norm(acc["col_header"]); want_sub = _norm(acc.get("sub_header", ""))
    if want_sub:  # 下位項目：見出し行は空欄で 4 行目に項目名が入る（例 家計最終消費支出・輸出・輸入）
        cols = [i for i, h in enumerate(rows[sub_row]) if _norm(h) == want_sub]
    else:
        cols = [i for i, h in enumerate(rows[hdr_row]) if _norm(h) == want]
    if len(cols) != 1:
        raise EsriSourceError(f"{s.series_id}: 列見出し {acc['col_header']!r}/{acc.get('sub_header','')!r} が {len(cols)} 列（1列に確定しない）")
    col = cols[0]
    recs: list[ValueRecord] = []
    year = ""
    for r in rows[int(acc.get("first_data_row", 8)) - 1:]:
        if not r or not r[0].strip():
            continue
        lab = r[0].strip()
        m = re.match(r"^(\d{4})/(.*)$", lab)
        if m:
            year, rest = m.group(1), m.group(2)
        else:
            rest = lab
        if not year:
            continue
        period = convert("esri_qe_quarter", f"{year}/{rest}")
        if period is None:
            continue  # 年度・暦年の合計行など
        val = _qe_value(r[col] if col < len(r) else "")
        if val is None:
            continue
        recs.append(ValueRecord(series_id=s.series_id, period=period, region="JP", value=val, status="",
                                vintage=published or day, retrieved_at=day, published_at=published,
                                accessor={"type": "esri_qe_csv", "file": rel, "col_header": acc["col_header"],
                                          "sub_header": acc.get("sub_header", ""), "row_label": lab, "edition": q,
                                          "cache": cache_rel(cp)}))
    if not recs:
        raise EsriSourceError(f"{s.series_id}: 値が0件")
    log.info("%s: %s 列%d → 値 %d vintage=%s", s.series_id, Path(rel).name, col, len(recs), published)
    return finish(s, recs, dry_run=dry_run, exc=EsriSourceError)


def ingest_xls(s: Series, *, dry_run: bool, day: str) -> int:
    """旧基準（68SNA・93SNA）の年次推計は .xls（BIFF）。レイアウトは xlsx と同じ＝行ラベル×見出し行の西暦。

    値の採り方は xlsx と同一：セルの表示書式の小数桁で四捨五入した「公表どおりの表示文字列」。
    xls の書式は xlrd の XF→FORMAT から取る（formatting_info=True が必須）。
    """
    import xlrd  # 旧基準 .xls の取込時のみ必要

    acc = s.accessor
    if not s.period_converter:
        raise ValueError(f"{s.series_id}: period_converter が未設定")
    rel = resolve_file(acc)
    cp, published = fetch_file(rel, day)
    wb = xlrd.open_workbook(cp, formatting_info=True)
    sheet = acc.get("sheet") or wb.sheet_names()[0]
    if sheet not in wb.sheet_names():
        raise EsriSourceError(f"{s.series_id}: シート {sheet!r} が無い（{wb.sheet_names()}）")
    sh = wb.sheet_by_name(sheet)
    row = _pick_row({r + 1: _norm(sh.cell_value(r, 0)) for r in range(sh.nrows)},
                    acc.get("row_label", ""), int(acc.get("occurrence", 0) or 0))
    hrow = int(acc.get("header_row", 0))
    if not hrow:
        raise EsriSourceError(f"{s.series_id}: accessor.header_row が未設定")

    def fmt_of(r0: int, c0: int) -> str:
        return wb.format_map[wb.xf_list[sh.cell_xf_index(r0, c0)].format_key].format_str

    recs: list[ValueRecord] = []
    bad_head: list[str] = []
    skipped = 0
    for c0 in range(int(acc.get("first_col", 2)) - 1, sh.ncols):
        head = sh.cell_value(hrow - 1, c0)
        if head is None or str(head).strip() == "":
            continue
        code = str(int(head)) if isinstance(head, (int, float)) else str(head).strip()
        period = convert(s.period_converter, code)
        if period is None:
            bad_head.append(code)
            continue
        if s.freq and not matches_freq(period, s.freq):
            bad_head.append(code)
            continue
        v = sh.cell_value(row - 1, c0)
        text = cell_text(v, fmt_of(row - 1, c0)) if sh.cell_type(row - 1, c0) == xlrd.XL_CELL_NUMBER else None
        if text is None:
            skipped += 1  # 空欄・非数値＝値ではない（found=false 側）
            continue
        recs.append(ValueRecord(
            series_id=s.series_id, period=period, region="JP", value=text, status="",
            vintage=published or day, retrieved_at=day, published_at=published,
            accessor={"type": "esri_xls", "file": rel, "sheet": sheet, "row_label": acc.get("row_label", ""),
                      **({"occurrence": acc["occurrence"]} if acc.get("occurrence") else {}),
                      "cell": f"{_col_letter(c0)}{row}", "col_header": code, "edition": acc.get("edition", ""),
                      "cache": cache_rel(cp)}))
    if bad_head:
        uniq = sorted(set(bad_head))
        raise EsriSourceError(f"{s.series_id}: 変換できない列見出し {len(uniq)} 種（例 {uniq[:5]}）。period_converter={s.period_converter}")
    log.info("%s: %s!%s 行%d → 値 %d（空欄 %d）vintage=%s", s.series_id, Path(rel).name, sheet, row, len(recs), skipped, published)
    if not recs:
        raise EsriSourceError(f"{s.series_id}: 値が0件（行 {row}・見出し行 {hrow}）＝セル指定を疑う")
    return finish(s, recs, dry_run=dry_run, exc=EsriSourceError)


def ingest_yearsheets(s: Series, *, dry_run: bool, day: str) -> int:
    """付表 2「経済活動別の国内総生産・要素所得」（名目 s2n）の形＝**年ごとに 1 シート**・行＝経済活動・列＝項目（第 7 弾 段 C・2026-08-22）。
    accessor: file, year_cell（例 "B4"＝'令和6暦年 (2024)'・period_converter で期に変換）, header_rows（例 [5, 6]＝見出しを連結して正規化一致）,
    col_header（例 '国内総生産（生産者価格表示）'）, row_label（＋occurrence）。列・行とも 1 つに確定しなければエラー（黙って選ばない）。
    **結合セルは解決する**（多段見出しの上位ラベルは結合セルで 1 回しか値を持たない＝固定資本ストックマトリックス ss4n の
    「制度部門別／非金融法人企業／民間」型。前方補完は隣の列へ誤って伝播するので使わない）。"""
    import openpyxl
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

    acc = s.accessor
    if not s.period_converter:
        raise ValueError(f"{s.series_id}: period_converter が未設定")
    rel = resolve_file(acc)
    cp, published = fetch_file(rel, day)
    wb = openpyxl.load_workbook(cp, data_only=True)
    ycol_letter, yrow = coordinate_from_string(str(acc.get("year_cell", "B4")))
    ycol = column_index_from_string(ycol_letter)
    hrows = [int(x) for x in acc.get("header_rows", [5, 6])]
    want = _norm(acc["col_header"])
    recs: list[ValueRecord] = []
    skipped = 0

    def merged_map(ws) -> dict:
        """結合セル (row, col) → 左上の値。多段見出しの上位ラベルを列ごとに解決する。"""
        m = {}
        for rng in ws.merged_cells.ranges:
            v = ws.cell(rng.min_row, rng.min_col).value
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    m[(r, c)] = v
        return m

    for sheet in wb.sheetnames:
        ws = wb[sheet]
        mm = merged_map(ws)
        ytext = str(ws.cell(yrow, ycol).value or "")
        period = convert(s.period_converter, ytext)
        if period is None:
            raise EsriSourceError(f"{s.series_id}: シート {sheet!r} の year_cell {ytext!r} を期に変換できない（period_converter={s.period_converter}）")
        if s.freq and not matches_freq(period, s.freq):
            raise EsriSourceError(f"{s.series_id}: シート {sheet!r} の期 {period} が freq={s.freq} と合わない")
        def hkey(c: int) -> str:
            return _norm("".join(str(mm.get((r, c), ws.cell(r, c).value) or "") for r in hrows))
        cols = [c for c in range(2, ws.max_column + 1) if hkey(c) == want]
        if len(cols) != 1:
            raise EsriSourceError(f"{s.series_id}: シート {sheet!r} で列見出し {acc['col_header']!r} が {len(cols)} 列（1 列に確定しない）")
        col = cols[0]
        row = find_row(ws, acc.get("row_label", ""), int(acc.get("occurrence", 0) or 0))
        c = ws.cell(row, col)
        text = cell_text(c.value, c.number_format)
        if text is None:
            skipped += 1
            continue
        recs.append(ValueRecord(
            series_id=s.series_id, period=period, region="JP", value=text, status="",
            vintage=published or day, retrieved_at=day, published_at=published,
            accessor={"type": "esri_xlsx_yearsheets", "file": rel, "sheet": sheet, "row_label": acc.get("row_label", ""),
                      **({"occurrence": acc["occurrence"]} if acc.get("occurrence") else {}),
                      "col_header": acc["col_header"], "cell": f"{c.column_letter}{row}", "year_cell": ytext,
                      "edition": acc.get("edition", ""), "cache": cache_rel(cp)}))
    log.info("%s: %s 年シート %d → 値 %d（空欄 %d）vintage=%s", s.series_id, Path(rel).name, len(wb.sheetnames), len(recs), skipped, published)
    if not recs:
        raise EsriSourceError(f"{s.series_id}: 値が0件＝セル指定を疑う")
    return finish(s, recs, dry_run=dry_run, exc=EsriSourceError)


def ingest_series(s: Series, *, dry_run: bool = False, day: Optional[str] = None) -> int:
    import openpyxl  # 取込時のみ必要（サービング側には持ち込まない）

    day = day or today()
    acc = s.accessor
    if acc.get("type") == "esri_xlsx_yearsheets":
        return ingest_yearsheets(s, dry_run=dry_run, day=day)
    if acc.get("type") == "esri_qe_csv":
        return ingest_qe_csv(s, dry_run=dry_run, day=day)
    if acc.get("type") == "esri_xls":
        return ingest_xls(s, dry_run=dry_run, day=day)
    if acc.get("type") != "esri_xlsx":
        raise ValueError(f"{s.series_id}: accessor.type={acc.get('type')} は esri_xlsx 取込の対象外")
    if not s.period_converter:
        raise ValueError(f"{s.series_id}: period_converter が未設定")
    rel = resolve_file(acc)
    cp, published = fetch_file(rel, day)
    wb = openpyxl.load_workbook(cp, data_only=True)
    sheet = acc.get("sheet") or wb.sheetnames[0]
    if sheet not in wb.sheetnames:
        raise EsriSourceError(f"{s.series_id}: シート {sheet!r} が無い（{wb.sheetnames}）")
    ws = wb[sheet]
    row = find_row(ws, acc.get("row_label", ""), int(acc.get("occurrence", 0) or 0))
    hrow = int(acc.get("header_row", 0))
    if not hrow:
        raise EsriSourceError(f"{s.series_id}: accessor.header_row が未設定")

    # 二段見出し（ストック編 ss2 等）：見出し行は年（結合セルのため前方補完）、その下の行が部門。
    sub_row = int(acc.get("sub_header_row", 0) or 0)
    want_sub = _norm(acc.get("sub_header", ""))
    if sub_row and not want_sub:
        raise EsriSourceError(f"{s.series_id}: sub_header_row があるが sub_header が未設定")

    recs: list[ValueRecord] = []
    bad_head: list[str] = []
    skipped = 0
    carried = ""  # 結合セルの年見出しを右へ引き継ぐ（二段見出しのときのみ）
    for col in range(int(acc.get("first_col", 2)), ws.max_column + 1):
        head = ws.cell(hrow, col).value
        if head is not None and str(head).strip() != "":
            carried = str(int(head)) if isinstance(head, (int, float)) else str(head).strip()
        elif not sub_row:
            continue
        if sub_row:
            if _norm(ws.cell(sub_row, col).value) != want_sub:
                continue
            if not carried:
                continue
            code = carried
        else:
            code = str(int(head)) if isinstance(head, (int, float)) else str(head).strip()
        period = convert(s.period_converter, code)
        if period is None:
            bad_head.append(code)
            continue
        if s.freq and not matches_freq(period, s.freq):
            bad_head.append(code)
            continue
        c = ws.cell(row, col)
        text = cell_text(c.value, c.number_format)
        if text is None:
            skipped += 1  # 空欄・非数値＝値ではない（found=false 側）
            continue
        recs.append(ValueRecord(
            series_id=s.series_id, period=period, region="JP", value=text, status="",
            vintage=published or day, retrieved_at=day, published_at=published,
            accessor={"type": "esri_xlsx", "file": rel, "sheet": sheet, "row_label": acc.get("row_label", ""),
                      **({"occurrence": acc["occurrence"]} if acc.get("occurrence") else {}),
                      **({"sub_header": acc["sub_header"]} if sub_row else {}),
                      "cell": f"{c.column_letter}{row}", "col_header": code, "edition": acc.get("edition", ""),
                      "cache": cache_rel(cp)}))
    if bad_head:
        uniq = sorted(set(bad_head))
        raise EsriSourceError(f"{s.series_id}: 変換できない列見出し {len(uniq)} 種（例 {uniq[:5]}）。period_converter={s.period_converter}")
    log.info("%s: %s!%s 行%d → 値 %d（空欄 %d）vintage=%s", s.series_id, Path(rel).name, sheet, row, len(recs), skipped, published)
    if not recs:
        raise EsriSourceError(f"{s.series_id}: 値が0件（行 {row}・見出し行 {hrow}）＝セル指定を疑う")
    return finish(s, recs, dry_run=dry_run, exc=EsriSourceError)


def main(argv: Optional[list[str]] = None) -> int:
    return run_cli(TYPES, ingest_series, "ESRI 国民経済計算 xlsx/xls/QE CSV 取込（原本キャッシュ→値ストア）", argv)


if __name__ == "__main__":
    sys.exit(main())
