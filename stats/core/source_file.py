"""原典の確認先：値ごとの取得記録（ValueRecord.accessor）から、その値を取った公表ファイル（原票）の URL を作る。

要件源＝導入団体の利用者の要望（2026-11-09）：値を資料に使う前に原典で確かめたい＝原票へ 1 クリックで行きたい。
出典の `url`（Series.source_url）は目次・案内のページが多く（SNA の年次推計は版の目次）、利用者は目次からファイルを探し直していた。

- 返すのは**取込が実際に取りに行った URL**。取込の URL の作り方の写しを持ち（サーバは取込を import しない＝取込の依存を読まない）、
  写しの出どころは COPIED_FROM_INGEST に宣言する＝test_core が取込の定数と突き合わせる。
- e-Stat の API で取る表（法人企業統計ほか）は決まったファイルの URL が無い＝**表の画面**（そこからダウンロードできる）を返す。
- PDF は値の載った頁（`#page=N`）まで。
- 出さない型は UNLINKED_TYPES に理由つき（API で取る国際系列・公表のたびにファイルの番号が替わる型）＝出典の `url` を使う。
- 推測しない：必要な欄が無い・ひな型が埋まっていない・http(s) でない・知らない型は None（壊れたリンクを返さない）。
- 新しく取りに行かない＝鮮度は値と同じ（取得元がファイルを動かせば、次の再取込までは取込の時点の URL のまま）。
"""
from __future__ import annotations

from typing import Callable, Optional

ESRI = "https://www.esri.cao.go.jp/jp/sna/data/data_list/"
ESTAT_TABLE = "https://www.e-stat.go.jp/dbview?sid={sid}"
MAIKIN = "https://www.e-stat.go.jp/stat-search/file-download?statInfId={sid}&fileKind=1"
BOJ_FLAT = "https://www.stat-search.boj.or.jp/info/"
BOJ_MTSHTML = "https://www.stat-search.boj.or.jp/ssi/mtshtml/"
MOF_JGB_ALL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
MOF_JGB_CUR = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
MOF_ZAISEI = "https://www.mof.go.jp/policy/budget/reference/statistics/"
MOF_ZAIMU = "https://www.mof.go.jp/policy/budget/report/public_finance_fact_sheet/"
SOUMU_TOKITSU = "https://www.soumu.go.jp/main_content/"
SOUMU_HAKUSHO = "https://www.soumu.go.jp/menu_seisaku/hakusyo/chihou/"
CAO_INFRA = "https://www5.cao.go.jp/keizai2/ioj/result/data/"
CAO_MITOSHI = "https://www5.cao.go.jp/keizai1/mitoshi/"

# 写しの出どころ（取込のモジュール, 属性）→ ここの値。test_core が 1 件ずつ取込の定数と突き合わせる
COPIED_FROM_INGEST: dict[tuple[str, str], str] = {
    ("esri", "BASE"): ESRI,
    ("maikin", "BASE"): MAIKIN,
    ("boj_flat", "BASE"): BOJ_FLAT,
    ("boj_mtshtml", "BASE"): BOJ_MTSHTML,
    ("mof", "JGB_ALL"): MOF_JGB_ALL,
    ("mof", "JGB_CUR"): MOF_JGB_CUR,
    ("mof_zaisei", "BASE"): MOF_ZAISEI,
    ("mof_zaimu", "BASE"): MOF_ZAIMU,
    ("soumu_tokitsu", "BASE"): SOUMU_TOKITSU,
    ("soumu_hakusho", "BASE"): SOUMU_HAKUSHO,
    ("cao_infra", "BASE"): CAO_INFRA,
    ("cao_mitoshi", "BASE"): CAO_MITOSHI,
}

# 原票のリンクを出さない型（理由つき）。登録済みの型は、ここか下の _RULES のどちらか一方に載る（test_core が検査）
UNLINKED_TYPES: dict[str, str] = {
    "oecd_sdmx": "API（OECD SDMX）で取る＝利用者が開いて読める原票のファイルが無い",
    "imf_dm": "API（IMF DataMapper）で取る",
    "wb_api": "API（世界銀行）で取る",
    "bls_api": "API（BLS）で取る",
    "eurostat_api": "API（Eurostat）で取る",
    "estat_catalog_xlsx": "公表のたびに e-Stat のファイルの番号（statInfId）が替わり、古い番号は開けなくなる（2026-11-09 に確認）",
    "pdf_table": "値を持たない（status=guide の系列だけ）",
}


def _rel(base: str, key: str) -> Callable[[dict], Optional[str]]:
    """base＋accessor[key]（相対パス）。ひな型（{Q} 等）が埋まっていなければ出さない。"""
    def build(a: dict) -> Optional[str]:
        rel = str(a.get(key) or "")
        return base + rel if rel and "{" not in rel else None
    return build


def _own_url(a: dict) -> Optional[str]:
    """取込が accessor に URL をそのまま記録している型。"""
    u = str(a.get("url") or "")
    return u if u.startswith(("https://", "http://")) else None


def _with_page(build: Callable[[dict], Optional[str]]) -> Callable[[dict], Optional[str]]:
    def wrapped(a: dict) -> Optional[str]:
        u = build(a)
        return f"{u}#page={a['page']}" if u and a.get("page") else u
    return wrapped


def _id(template: str, key: str) -> Callable[[dict], Optional[str]]:
    def build(a: dict) -> Optional[str]:
        v = str(a.get(key) or "")
        return template.format(sid=v) if v else None
    return build


def _soumu_hakusho(a: dict) -> Optional[str]:
    # 年版 "r03(2021)"＋"s-128-4.csv" → r03data/2021data/csv/s-128-4.csv（取込の fetch_csv と同じ並び）
    ed, name = str(a.get("edition") or ""), str(a.get("file") or "")
    head, sep, tail = ed.partition("(")
    if not (name and sep and head and tail.endswith(")") and tail[:-1].isdigit()):
        return None
    return f"{SOUMU_HAKUSHO}{head}data/{tail[:-1]}data/csv/{name}"


def _mof_jgb(a: dict) -> Optional[str]:
    # jgbcm.csv は当月分（月が替わると行が jgbcm_all.csv へ移る）＝取込の時点で値が載っていたファイルを返す
    return {"jgbcm_all.csv": MOF_JGB_ALL, "jgbcm.csv": MOF_JGB_CUR}.get(str(a.get("file") or ""))


_RULES: dict[str, Callable[[dict], Optional[str]]] = {
    "esri_xlsx": _rel(ESRI, "file"),
    "esri_xls": _rel(ESRI, "file"),
    "esri_xlsx_yearsheets": _rel(ESRI, "file"),
    "esri_qe_csv": _rel(ESRI, "file"),
    "estat": _id(ESTAT_TABLE, "statsDataId"),
    "maikin_csv": _id(MAIKIN, "statInfId"),
    "boj_flat": _rel(BOJ_FLAT, "zip"),
    "boj_mtshtml": _rel(BOJ_MTSHTML, "page"),
    "boj_file": _own_url,
    "ipss_xlsx": _own_url,
    "cao_gap_xlsx": _own_url,
    "pdf_hakusho_sme": _with_page(_own_url),
    "cao_mitoshi_pdf": _with_page(_rel(CAO_MITOSHI, "file")),
    "mof_csv": _mof_jgb,
    "mof_zaisei": _rel(MOF_ZAISEI, "file"),
    "mof_zaimu_xlsx": _rel(MOF_ZAIMU, "file"),
    "soumu_tokitsu_xlsx": _rel(SOUMU_TOKITSU, "file"),
    "soumu_hakusho": _soumu_hakusho,
    "cao_infra_xlsx": _rel(CAO_INFRA, "file"),
}
LINKED_TYPES = frozenset(_RULES)


def source_file_url(accessor: Optional[dict]) -> Optional[str]:
    """値の取得記録 → 原票の URL（作れなければ None）。"""
    if not accessor:
        return None
    rule = _RULES.get(str(accessor.get("type") or ""))
    return rule(accessor) if rule else None


def file_fields(by_url: dict[str, list[str]]) -> dict:
    """{原票の URL: [期, …]}（期の順に入れた dict）→ 出典の欄。1 本なら file_url、期によって違えば file_urls＝[{url, periods}]、無ければ空。"""
    if not by_url:
        return {}
    if len(by_url) == 1:
        return {"file_url": next(iter(by_url))}
    return {"file_urls": [{"url": u, "periods": ps} for u, ps in by_url.items()]}


FILE_FIELDS = ("file_url", "file_urls")
