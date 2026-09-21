"""項目の語彙＝正規化キー → 標準タクソノミの要素（local name）。

規則（companies/CLAUDE.md・docs/開発計画.md §3）：
- キーに載せるのは**標準の要素だけ**。各社の拡張要素はキーに寄せず、要素 ID を指定して「会社が定義した項目」として引く。
- 1 つのキーに束ねるのは**同じ概念の会計基準違い**だけ（J-GAAP／IFRS／US GAAP）。隣の概念は別キー
  （売上高／営業収益／経常収益、経常利益／税引前利益、純資産／親会社の所有者に帰属する持分 は束ねない）。
- **要素名ではなく公式ラベルで意味を確かめてから足す**。要素名は当てにならない
  （`EquityToAssetRatioIFRSSummaryOfBusinessResults` の公式ラベルは「１株当たり親会社所有者帰属持分（IFRS）」＝比率ではない）。
- 載せるのは実データで観測した要素だけ（2026-09-20・20 社）。足すときは評価問を先に立てる。
"""
from __future__ import annotations

S = "SummaryOfBusinessResults"
_EMP = "InformationAboutReportingCompanyInformationAboutEmployees"

# key: (日本語の呼び名, [要素の local name …])
ITEMS: dict[str, tuple[str, list[str]]] = {
    # 収益（最上段）＝概念ごとに別キー
    "net_sales": ("売上高", [f"NetSales{S}", f"RevenueIFRS{S}", f"RevenuesUSGAAP{S}"]),
    "operating_revenue": ("営業収益", [f"OperatingRevenue1{S}"]),
    "ordinary_revenue": ("経常収益", [f"OrdinaryIncome{S}"]),
    "net_premiums_written": ("正味収入保険料", [f"NetPremiumsWritten{S}INS"]),
    # 利益
    "ordinary_profit": ("経常利益", [f"OrdinaryIncomeLoss{S}"]),
    "profit_before_tax": ("税引前利益", [f"ProfitLossBeforeTaxIFRS{S}", f"ProfitLossBeforeTaxUSGAAP{S}"]),
    "profit_attributable_to_owners": ("親会社株主に帰属する当期純利益", [
        f"ProfitLossAttributableToOwnersOfParent{S}", f"ProfitLossAttributableToOwnersOfParentIFRS{S}",
        f"NetIncomeLossAttributableToOwnersOfParentUSGAAP{S}"]),
    "net_income": ("当期純利益", [f"NetIncomeLoss{S}"]),
    "comprehensive_income": ("包括利益", [f"ComprehensiveIncome{S}"]),
    "comprehensive_income_attributable_to_owners": ("親会社の所有者に帰属する包括利益", [
        f"ComprehensiveIncomeAttributableToOwnersOfParentIFRS{S}", f"ComprehensiveIncomeAttributableToOwnersOfParentUSGAAP{S}"]),
    # 財政状態
    "net_assets": ("純資産額", [f"NetAssets{S}", f"EquityIncludingPortionAttributableToNonControllingInterestUSGAAP{S}"]),
    "equity_attributable_to_owners": ("親会社の所有者に帰属する持分", [f"EquityAttributableToOwnersOfParentIFRS{S}"]),
    "total_assets": ("総資産額", [f"TotalAssets{S}", f"TotalAssetsIFRS{S}", f"TotalAssetsUSGAAP{S}"]),
    "equity_ratio": ("自己資本比率", [f"EquityToAssetRatio{S}", f"RatioOfOwnersEquityToGrossAssetsIFRS{S}", f"EquityToAssetRatioUSGAAP{S}"]),
    "roe": ("自己資本利益率", [f"RateOfReturnOnEquity{S}", f"RateOfReturnOnEquityIFRS{S}", f"RateOfReturnOnEquityUSGAAP{S}"]),
    "capital_stock": ("資本金", [f"CapitalStock{S}"]),
    "issued_shares": ("発行済株式総数（普通株式）", [f"TotalNumberOfIssuedShares{S}"]),
    # 1 株当たり・株価
    "net_assets_per_share": ("１株当たり純資産額", [f"NetAssetsPerShare{S}"]),
    "equity_per_share_attributable_to_owners": ("１株当たり親会社所有者帰属持分", [
        f"EquityToAssetRatioIFRS{S}",  # ★要素名は比率に見えるが公式ラベルは 1 株当たり持分
        f"EquityAttributableToOwnersOfParentPerShareUSGAAP{S}"]),
    "eps_basic": ("１株当たり当期純利益", [f"BasicEarningsLossPerShare{S}", f"BasicEarningsLossPerShareIFRS{S}", f"BasicEarningsLossPerShareUSGAAP{S}"]),
    "eps_diluted": ("潜在株式調整後１株当たり当期純利益", [f"DilutedEarningsPerShare{S}", f"DilutedEarningsLossPerShareIFRS{S}", f"DilutedEarningsLossPerShareUSGAAP{S}"]),
    "per": ("株価収益率", [f"PriceEarningsRatio{S}", f"PriceEarningsRatioIFRS{S}", f"PriceEarningsRatioUSGAAP{S}"]),
    "dividend_per_share": ("１株当たり配当額", [f"DividendPaidPerShare{S}"]),
    "interim_dividend_per_share": ("１株当たり中間配当額", [f"InterimDividendPaidPerShare{S}"]),
    "payout_ratio": ("配当性向", [f"PayoutRatio{S}"]),
    # キャッシュ・フロー
    "cf_operating": ("営業活動によるキャッシュ・フロー", [f"NetCashProvidedByUsedInOperatingActivities{S}", f"CashFlowsFromUsedInOperatingActivitiesIFRS{S}", f"CashFlowsFromUsedInOperatingActivitiesUSGAAP{S}"]),
    "cf_investing": ("投資活動によるキャッシュ・フロー", [f"NetCashProvidedByUsedInInvestingActivities{S}", f"CashFlowsFromUsedInInvestingActivitiesIFRS{S}", f"CashFlowsFromUsedInInvestingActivitiesUSGAAP{S}"]),
    "cf_financing": ("財務活動によるキャッシュ・フロー", [f"NetCashProvidedByUsedInFinancingActivities{S}", f"CashFlowsFromUsedInFinancingActivitiesIFRS{S}", f"CashFlowsFromUsedInFinancingActivitiesUSGAAP{S}"]),
    "cash_and_equivalents": ("現金及び現金同等物の残高", [f"CashAndCashEquivalents{S}", f"CashAndCashEquivalentsIFRS{S}", f"CashAndCashEquivalentsUSGAAP{S}"]),
    # 従業員
    "employees": ("従業員数", ["NumberOfEmployees"]),
    "average_annual_salary": ("平均年間給与", [f"AverageAnnualSalary{_EMP}"]),
    "average_age_years": ("平均年齢（年）", [f"AverageAgeYears{_EMP}"]),
    "average_age_months": ("平均年齢（月）", [f"AverageAgeMonths{_EMP}"]),
    "average_service_years": ("平均勤続年数（年）", [f"AverageLengthOfServiceYears{_EMP}"]),
    "average_service_months": ("平均勤続年数（月）", [f"AverageLengthOfServiceMonths{_EMP}"]),
    # 銀行（提出会社）
    "deposits": ("預金残高", [f"Deposits{S}"]),
    "loans": ("貸出金残高", [f"LoansAndBillsDiscounted{S}"]),
    "securities": ("有価証券残高", [f"Securities{S}"]),
}

ELEMENT_TO_KEY: dict[str, str] = {el: k for k, (_, els) in ITEMS.items() for el in els}
assert len(ELEMENT_TO_KEY) == sum(len(els) for _, els in ITEMS.values()), "同じ要素が 2 つのキーに載っている"

BASES = ("consolidated", "non_consolidated")

# 「40 歳 5 か月」を年と月の 2 要素で開示する会社がある（2026-09-21 実測＝約 8%）。年だけ返すと端数が黙って落ちる＝対の項目を必ず添える。
COMPANION = {"average_age_years": "average_age_months", "average_age_months": "average_age_years",
             "average_service_years": "average_service_months", "average_service_months": "average_service_years"}
STANDARDS = ("Japan GAAP", "IFRS", "US GAAP")


def standard_of(element: str) -> str:
    """標準要素がどの会計基準の表のものか（要素名の接尾＝タクソノミの命名規約）。"""
    local = element.split(":")[-1]
    return "IFRS" if "IFRS" in local else "US GAAP" if "USGAAP" in local else "Japan GAAP"
