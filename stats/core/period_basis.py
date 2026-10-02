"""
期の定義（period basis）の宣言＝その系列の値が「いつの値か」（第 12 弾 第 2 便・2026-10-02）。

要件源＝利用側の財政 PL・BS 分析：暦年末（SNA の BS）・年度末（資金循環・公会計）・期中（フロー）が混ざると取り違える。
同じ期の表記（"2024"・"FY2024"）でも、期末残高・期中の合計／平均・特定日の値は別物＝lookup_panel は混在を警告し、
lookup_statistic は系列ごとに期の定義を返す。

宣言は **dataset 単位**（系列ごとに持たない＝登録簿 series.jsonl を太らせない・B23）。同じ dataset に残高とフローが
混在するもの（hojin・fof・sna2020 等）だけ measure の規則で分ける。
★ **原典の表題・注記で確かめられないものは宣言しない**（未宣言＝判定に使わず period_basis_undeclared に名前を出す）。
推測で「揃っている」と言わない＝fail-closed と同じ規律。例：労働力調査の失業率（月末 1 週間の状態）・人口の年央推計・企業数。
新しい dataset を足したら、ここで宣言するか UNDECLARED_DATASETS に理由つきで載せる（test_core が漏れを検知する）。
"""
from __future__ import annotations

from typing import Optional

# 期の種類：flow＝期中（期間の合計・平均・率）／end＝期末残高／oct1＝各年 10 月 1 日現在（人口）／fiscal_close＝年度内に終わる決算期の期末
_LABEL = {
    ("flow", "a"): "暦年（期中）", ("flow", "fy"): "年度（期中）", ("flow", "q"): "四半期（期中）", ("flow", "fq"): "年度の四半期（期中）",
    ("flow", "m"): "月（期中）", ("flow", "h"): "半期（期中）", ("flow", "d"): "日次（基準日の値）",
    ("end", "a"): "暦年末", ("end", "fy"): "年度末", ("end", "q"): "四半期末", ("end", "fq"): "四半期末（年度の四半期）", ("end", "m"): "月末",
    ("oct1", "a"): "各年10月1日現在",
    ("fiscal_close", "fy"): "年度（4月〜翌3月）に終わる決算期の期末",
}

# 法人企業統計の貸借対照表の項目（当期末）＝決算期末の残高。ほかの項目（損益・人件費・設備投資・期中平均従業員数・比率）は期中
_HOJIN_BS = {
    "cash_deposits", "retained_earnings", "total_assets", "current_assets", "inventories", "fixed_assets", "tangible_fixed_assets",
    "intangible_fixed_assets", "land", "investments_other", "investment_securities", "stocks_fixed", "liabilities", "current_liabilities",
    "short_term_borrowings", "fixed_liabilities", "long_term_borrowings", "bonds", "net_assets", "capital_stock", "shareholders_equity",
}

# 国の財務書類の貸借対照表の項目（seed_registry.ZAIMU_ROWS のシート 0）
_ZAIMU_BS = {
    "cash_deposits", "securities", "loans", "pension_investment_deposits", "tangible_fixed_assets", "state_property_ex_public", "land",
    "public_property", "public_property_land", "public_property_facilities", "investments", "assets_total", "financing_bills",
    "government_bonds", "iaa_bonds", "borrowings", "postal_savings", "insurance_reserves", "public_pension_deposits",
    "retirement_allowances", "liabilities_total", "net_assets",
}

# dataset → 期の種類（全 measure 共通）
_BY_DATASET = {
    # 期末残高（表題・原表で暦年末／年度末と明記）
    "sna_sector_bs": "end", "sna_gg_bs": "end", "sna_fcs": "end", "iip": "end",
    # 各年 10 月 1 日現在（人口推計・将来推計人口の表題）
    "jinko": "oct1", "pop2023": "oct1",
    # 期中（国民経済計算のフロー・財政・物価・賃金・国際収支 等）
    "sna_sector": "flow", "sna_gg": "flow", "sna_activity": "flow", "qe2020": "flow", "sna1990": "flow", "sna2000": "flow",
    "cao_gap": "flow", "gap": "flow", "mitoshi": "flow", "mitoshi_mid": "flow",
    "zaisei": "flow", "chihozaisei": "flow", "chihokeikaku": "flow", "shaho": "flow", "hakusho_sme": "flow",
    "cpi2020": "flow", "cpi2025": "flow", "cgpi": "flow", "fx": "flow", "bop": "flow", "jgb": "flow",
    "shokugyo": "flow", "roudou_emp": "flow",
    "pdb": "flow", "stan": "flow", "earnings": "flow",
}

# 同じ dataset に残高とフローが混在するもの＝measure で決める（返り値 None＝未宣言）
def _by_measure(dataset: str, measure: str, freq: str) -> Optional[str]:
    if dataset == "hojin":
        if measure in _HOJIN_BS:
            return "end" if freq == "fq" else "fiscal_close"  # 四半期別調査の当期末＝四半期末
        return None if measure == "population_count" else "flow"  # 母集団法人数は時点の定義を原典で確かめていない
    if dataset == "fof":
        if measure.startswith("stock.") or freq == "q":  # 四半期の 5 系列は家計の金融資産（ストック・四半期末）
            return "end"
        return "flow" if measure.startswith("flow.") else None
    if dataset == "sna2020":
        return "end" if measure.startswith("stock.") else "flow"  # stock.* はストック編（暦年末）
    if dataset == "maikin":
        if measure == "employees":
            return "end"  # 常用労働者数（本月末）
        return None if measure == "parttime_workers" else "flow"
    if dataset == "rates":
        return {"call_on_avg": "flow", "basic_loan_rate": "end"}.get(measure)  # 月平均／月末（表題）
    if dataset == "weo":
        return None if measure in ("gov_debt_gdp", "population") else "flow"  # 債務残高の時点・人口の推計時点は国で違い得る
    if dataset == "wdi":
        return None if measure == "population" else "flow"  # 年央推計
    if dataset == "sdbs":
        return None if measure == "enterprises" else "flow"
    if dataset == "zaimu_shorui":
        # 国の財務書類（第 12 弾 第 3 便）：貸借対照表＝年度末（3 月 31 日）・増減計算書の期末＝年度末・期首（前年度末）は期の表記と時点がずれる＝宣言しない
        if measure == "nw_opening":
            return None
        return "end" if measure in _ZAIMU_BS or measure == "nw_closing" else "flow"
    if dataset in ("us", "eu"):
        return None if measure == "unemployment_rate" else "flow"
    return None


# 宣言しない dataset（理由）。ここに無く、_BY_DATASET にも _by_measure にも無い dataset は test_core が FAIL にする
UNDECLARED_DATASETS = {
    "roudou": "労働力調査は月末 1 週間の就業状態＝『月（期中）』とも『月末』とも言い切れない",
    "fm": "IMF Fiscal Monitor の債務残高の時点（暦年末／会計年度末）は国で違い得る",
}
_MEASURE_RULE_DATASETS = ("hojin", "fof", "sna2020", "maikin", "rates", "weo", "wdi", "sdbs", "us", "eu", "zaimu_shorui")


def basis_of(dataset: str, measure: str, freq: str) -> Optional[str]:
    """期の種類（flow／end／oct1／fiscal_close）。未宣言は None。"""
    if dataset in _BY_DATASET:
        return _BY_DATASET[dataset]
    if dataset in _MEASURE_RULE_DATASETS:
        return _by_measure(dataset, measure, freq)
    return None


def label_of(dataset: str, measure: str, freq: str) -> Optional[str]:
    """期の定義の表示名（例『暦年末』『年度（期中）』）。未宣言・周期と組み合わない宣言は None。"""
    b = basis_of(dataset, measure, freq)
    return _LABEL.get((b, freq)) if b else None


def is_covered(dataset: str) -> bool:
    """宣言の対象として扱われている dataset か（宣言済み・measure 規則・理由つき未宣言のいずれか）。"""
    return dataset in _BY_DATASET or dataset in _MEASURE_RULE_DATASETS or dataset in UNDECLARED_DATASETS
