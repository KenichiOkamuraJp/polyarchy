"""項目の語彙＝正規化キー → 標準タクソノミの要素（local name）。

規則（companies/CLAUDE.md・docs/開発計画.md §3）：
- キーに載せるのは**標準の要素だけ**。各社の拡張要素はキーに寄せず、要素 ID を指定して「会社が定義した項目」として引く。
- 1 つのキーに束ねるのは**同じ概念の会計基準違い**だけ（J-GAAP／IFRS／US GAAP）。隣の概念は別キー
  （売上高／売上収益／営業収益／経常収益、経常利益／税引前利益、純資産／親会社の所有者に帰属する持分 は束ねない）。
- **要素名ではなく公式ラベルで意味を確かめてから足す**。要素名は当てにならない
  （`EquityToAssetRatioIFRSSummaryOfBusinessResults` の公式ラベルは「１株当たり親会社所有者帰属持分（IFRS）」＝比率ではない）。
- 載せるのは実データで観測した要素だけ（2026-09-20 の 20 社＋2026-09-21 の母集団 2,403 社）。足すときは公式ラベルを EDINET の公式 CSV で確かめ、評価問を立てる。
"""
from __future__ import annotations

S = "SummaryOfBusinessResults"
_EMP = "InformationAboutReportingCompanyInformationAboutEmployees"

# key: (日本語の呼び名, [要素の local name …])
ITEMS: dict[str, tuple[str, list[str]]] = {
    # 収益（最上段）＝概念ごとに別キー
    "net_sales": ("売上高", [f"NetSales{S}", f"RevenuesUSGAAP{S}"]),  # US GAAP の公式ラベルは「売上高（US GAAP）」
    # 売上収益＝公式ラベルどおり売上高とは別のキー（2026-09-22 決定）。IFRS の会社の大半はこちら＝net_sales で引くと suggest で案内される
    "revenue": ("売上収益", [f"RevenueIFRS{S}", "RevenueKeyFinancialData"]),
    "operating_revenue": ("営業収益", [f"OperatingRevenue1{S}"]),
    "operating_receipts": ("営業収入", [f"OperatingRevenue2{S}"]),  # 営業収益とは別の公式ラベル
    "gross_operating_revenue": ("営業総収入", [f"GrossOperatingRevenue{S}"]),
    "ordinary_revenue": ("経常収益", [f"OrdinaryIncome{S}"]),
    "net_premiums_written": ("正味収入保険料", [f"NetPremiumsWritten{S}INS"]),
    # 利益
    "operating_profit": ("営業利益", [f"OperatingIncomeLossUSGAAP{S}"]),  # 日本基準・IFRS の経営指標の表には標準の営業利益が無い
    "ordinary_profit": ("経常利益", [f"OrdinaryIncomeLoss{S}"]),
    "profit_before_tax": ("税引前利益", [f"ProfitLossBeforeTaxIFRS{S}", f"ProfitLossBeforeTaxUSGAAP{S}"]),
    "profit_attributable_to_owners": ("親会社株主に帰属する当期純利益", [
        f"ProfitLossAttributableToOwnersOfParent{S}", f"ProfitLossAttributableToOwnersOfParentIFRS{S}",
        f"NetIncomeLossAttributableToOwnersOfParentUSGAAP{S}"]),
    "net_income": ("当期純利益", [f"NetIncomeLoss{S}"]),
    "profit": ("当期利益", [f"ProfitLossIFRS{S}"]),  # IFRS の当期利益（非支配持分を含む）＝親会社帰属とも日本基準の当期純利益とも別
    "equity_method_income_if_applied": ("持分法を適用した場合の投資利益", [f"EquityInEarningsLossesOfAffiliatesIfEquityMethodIsApplied{S}"]),
    "comprehensive_income": ("包括利益", [f"ComprehensiveIncome{S}", f"ComprehensiveIncomeIFRS{S}", f"ComprehensiveIncomeUSGAAP{S}"]),
    "comprehensive_income_attributable_to_owners": ("親会社の所有者に帰属する包括利益", [
        f"ComprehensiveIncomeAttributableToOwnersOfParentIFRS{S}", f"ComprehensiveIncomeAttributableToOwnersOfParentUSGAAP{S}"]),
    # 財政状態
    "net_assets": ("純資産額", [f"NetAssets{S}", f"EquityIncludingPortionAttributableToNonControllingInterestUSGAAP{S}"]),
    "equity_attributable_to_owners": ("親会社の所有者に帰属する持分", [f"EquityAttributableToOwnersOfParentIFRS{S}",
                                                          f"EquityAttributableToOwnersOfParentUSGAAP{S}"]),  # US GAAP の公式ラベルは「株主資本」
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
    "capital_adequacy_ratio_domestic": ("自己資本比率（国内基準）", [f"CapitalAdequacyRatioDomesticStandard{S}"]),
    # 保険業
    "net_loss_ratio": ("正味損害率", [f"NetLossRatio{S}INS"]),
    "net_operating_expense_ratio": ("正味事業費率", [f"NetOperatingExpenseRatio{S}INS"]),
    "interest_and_dividend_income": ("利息及び配当金収入", [f"InterestAndDividendIncome{S}INS"]),
    "investment_yield_income": ("運用資産利回り（インカム利回り）", [f"InvestmentAssetsYieldIncomeYield{S}INS"]),
    "investment_yield_realized": ("資産運用利回り（実現利回り）", [f"InvestmentYieldRealizedYield{S}INS"]),
}

ELEMENT_TO_KEY: dict[str, str] = {el: k for k, (_, els) in ITEMS.items() for el in els}
assert len(ELEMENT_TO_KEY) == sum(len(els) for _, els in ITEMS.values()), "同じ要素が 2 つのキーに載っている"

BASES = ("consolidated", "non_consolidated")

# 最上段の収益の仲間＝どれか 1 つで引いて無かったとき、その会社が開示している仲間のキーを suggest で案内する（値は返さない）
TOP_LINE = ("net_sales", "revenue", "operating_revenue", "operating_receipts", "gross_operating_revenue", "ordinary_revenue",
            "net_premiums_written")
TOP_LINE_LABEL = ("売上", "収益", "収入", "完成工事高", "保険料")  # 各社の拡張要素のラベルから仲間を拾う語（案内のためだけに使う）

# 「40 歳 5 か月」を年と月の 2 要素で開示する会社がある（2026-09-21 実測＝約 8%）。年だけ返すと端数が黙って落ちる＝対の項目を必ず添える。
COMPANION = {"average_age_years": "average_age_months", "average_age_months": "average_age_years",
             "average_service_years": "average_service_months", "average_service_months": "average_service_years"}
STANDARDS = ("Japan GAAP", "IFRS", "US GAAP")


def standard_of(element: str) -> str:
    """標準要素がどの会計基準の表のものか（要素名の接尾＝タクソノミの命名規約）。"""
    local = element.split(":")[-1]
    return "IFRS" if "IFRS" in local else "US GAAP" if "USGAAP" in local else "Japan GAAP"
