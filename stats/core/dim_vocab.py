"""
dims（業種・活動・規模）スラグの語彙＝表示名。hojin は stats.core.hojin_vocab、それ以外はここ。
core に置く理由＝発見層のファミリーカード（Registry._family_card）が dataset ごとにスラグの表示名を要るため
（2026-08-22・利用側指摘＝sna_activity の families で 36 中 14 件がスラグのまま＝スラグを選ぶのはまさにそのカード）。
定義はここが正（seed_registry はここを import して系列を組む）。
"""
from __future__ import annotations

# ---------------------------------------------------------------- SNA 経済活動別（付表 2・付表 3）：(slug, 行ラベル, 表示名, 親 slug)
SNA_ACTIVITY = [  # (slug, 行ラベル, 表示名, 親 slug)＝付表 2・付表 3 共通の経済活動分類（2008SNA・JSIC 準拠の SNA 分類）
    ("agri_forestry_fishery", "１．農林水産業", "農林水産業", "total_activities"),
    ("mining", "２．鉱業", "鉱業", "total_activities"),
    ("mfg", "３．製造業", "製造業", "total_activities"),
    ("mfg_food", "（１）食料品", "食料品", "mfg"),
    ("mfg_textile", "（２）繊維製品", "繊維製品", "mfg"),
    ("mfg_pulp_paper", "（３）パルプ・紙・紙加工品", "パルプ・紙・紙加工品", "mfg"),
    ("mfg_chemical", "（４）化学", "化学", "mfg"),
    ("mfg_petroleum_coal", "（５）石油・石炭製品", "石油・石炭製品", "mfg"),
    ("mfg_ceramics", "（６）窯業・土石製品", "窯業・土石製品", "mfg"),
    ("mfg_primary_metal", "（７）一次金属", "一次金属", "mfg"),
    ("mfg_metal_products", "（８）金属製品", "金属製品", "mfg"),
    ("mfg_machinery", "（９）はん用・生産用・業務用機械", "はん用・生産用・業務用機械", "mfg"),
    ("mfg_electronic_parts", "（１０）電子部品・デバイス", "電子部品・デバイス", "mfg"),
    ("mfg_electrical", "（１１）電気機械", "電気機械", "mfg"),
    ("mfg_ict_equipment", "（１２）情報・通信機器", "情報・通信機器", "mfg"),
    ("mfg_transport_equipment", "（１３）輸送用機械", "輸送用機械", "mfg"),
    ("mfg_other", "（１４）その他の製造業", "その他の製造業", "mfg"),
    ("electricity_gas_water_waste", "４．電気・ガス・水道・廃棄物処理業", "電気・ガス・水道・廃棄物処理業", "total_activities"),
    ("construction", "５．建設業", "建設業", "total_activities"),
    ("wholesale_retail", "６．卸売・小売業", "卸売・小売業", "total_activities"),
    ("transport_postal", "７．運輸・郵便業", "運輸・郵便業", "total_activities"),
    ("accommodation_food", "８．宿泊・飲食サービス業", "宿泊・飲食サービス業", "total_activities"),
    ("ict", "９．情報通信業", "情報通信業", "total_activities"),
    ("finance_insurance", "１０．金融・保険業", "金融・保険業", "total_activities"),
    ("realestate", "１１．不動産業", "不動産業", "total_activities"),
    ("professional_business_support", "１２．専門・科学技術、業務支援サービス業", "専門・科学技術、業務支援サービス業", "total_activities"),
    ("public_admin", "１３．公務", "公務", "total_activities"),
    ("education", "１４．教育", "教育", "total_activities"),
    ("health_social", "１５．保健衛生・社会事業", "保健衛生・社会事業", "total_activities"),
    ("other_services", "１６．その他のサービス", "その他のサービス", "total_activities"),
    ("total_activities", "経済活動計", "経済活動計", None),
]
SNA_ACTIVITY_S2_EXTRA = [  # 付表 2 だけにある行（総計と（参考）制度部門別）
    ("import_taxes", "輸入品に課される税・関税", "輸入品に課される税・関税", "total"),
    ("vat_on_capital_formation", "（控除）総資本形成に係る消費税", "（控除）総資本形成に係る消費税", "total"),
    ("total", "合計", "合計（＝国内総生産）", None),
    ("market_producers", "市場生産者", "（参考）制度部門別：市場生産者", "total_activities"),
    ("general_government", "一般政府", "（参考）制度部門別：一般政府", "total_activities"),
    ("npish", "対家計民間非営利団体", "（参考）制度部門別：対家計民間非営利団体", "total_activities"),
]

# ---------------------------------------------------------------- OECD SDBS：(slug, コード, 表示名)
SDBS_ACTIVITY = [  # (slug, ACTIVITY コード, 表示名)
    ("bizecon_exfin", "BTNXK", "企業部門（B–N・金融保険業を除く）"),
    ("total", "_T", "全活動"),
    ("mfg", "C", "製造業"),
]
SDBS_SIZE = [  # (slug, SIZE_CLASS コード, 表示名)＝従業者数階級
    ("allsize", "_T", "全規模"), ("emp1_9", "S1T9", "従業者 1〜9 人"), ("emp10_19", "S10T19", "従業者 10〜19 人"),
    ("emp20_49", "S20T49", "従業者 20〜49 人"), ("emp50_249", "S50T249", "従業者 50〜249 人"), ("emp250plus", "S_GE250", "従業者 250 人以上"),
]

# ---------------------------------------------------------------- OECD STAN（ISIC Rev.4）：(slug, ACTIVITY コード, 表示名, JSNA 付表 2 との対応)
# 対応の「identity」＝日本の STAN 値が JSNA 付表 2 の値と全年で一致（2026-08-23 実測・OECD の日本の原典は JSNA）。slug を sna_activity と共有する。
# 「sum」＝JSNA の 1 分類が STAN の複数セクションの和と一致。「estimate」＝STAN の日本値が OECD 推計（OBS_STATUS=E・SUT/SBS 法）で JSNA と一致しない。
STAN_ACTIVITY = [
    ("total_activities", "_T", "全活動", "identity"),
    ("agri_forestry_fishery", "A", "農林水産業", "identity"),
    ("mining", "B", "鉱業", "identity"),
    ("mfg", "C", "製造業", "identity"),
    ("mfg_food", "C10T12", "食料品・飲料・たばこ", "identity"),
    ("mfg_textile_apparel_leather", "C13T15", "繊維・衣服・皮革", "estimate（JSNA 繊維製品と不一致）"),
    ("mfg_wood_paper_printing", "C16T18", "木材・紙・印刷", "estimate（JSNA はパルプ・紙とその他に分かれる）"),
    ("mfg_petroleum_coal", "C19", "石油・石炭製品", "identity"),
    ("mfg_chemical", "C20_21", "化学・医薬品", "identity"),
    ("mfg_rubber_plastic_nonmetal", "C22_23", "ゴム・プラスチック・窯業土石", "estimate"),
    ("mfg_ceramics", "C23", "窯業・土石製品", "identity"),
    ("mfg_primary_metal", "C24", "一次金属", "identity"),
    ("mfg_metal_products", "C25", "金属製品", "identity"),
    ("mfg_electronic_optical", "C26", "電子部品・コンピュータ・情報通信機器", "sum（JSNA 電子部品・デバイス＋情報・通信機器）"),
    ("mfg_electrical", "C27", "電気機械", "identity"),
    ("mfg_machinery", "C28", "はん用・生産用・業務用機械", "identity"),
    ("mfg_transport_equipment", "C29_30", "輸送用機械", "identity"),
    ("mfg_furniture_other_repair", "C31T33", "家具・その他・修理", "estimate"),
    ("electricity_gas", "D", "電気・ガス・熱供給", "sum の片割れ（JSNA 電気・ガス・水道・廃棄物処理＝D＋E）"),
    ("water_waste", "E", "水道・廃棄物処理", "sum の片割れ（JSNA 電気・ガス・水道・廃棄物処理＝D＋E）"),
    ("construction", "F", "建設業", "identity"),
    ("wholesale_retail", "G", "卸売・小売業", "identity"),
    ("transport_postal", "H", "運輸・郵便業", "identity"),
    ("accommodation_food", "I", "宿泊・飲食サービス業", "identity"),
    ("ict", "J", "情報通信業", "identity"),
    ("finance_insurance", "K", "金融・保険業", "identity"),
    ("realestate", "L", "不動産業", "identity"),
    ("professional", "M", "専門・科学技術サービス", "sum の片割れ（JSNA 専門・科学技術、業務支援＝M＋N）"),
    ("admin_support", "N", "業務支援サービス", "sum の片割れ（JSNA 専門・科学技術、業務支援＝M＋N）"),
    ("public_admin", "O", "公務", "identity"),
    ("education", "P", "教育", "identity"),
    ("health_social", "Q", "保健衛生・社会事業", "identity"),
    ("arts_entertainment", "R", "芸術・娯楽", "内訳（JSNA その他のサービス＝R＋S＋T＋U）"),
    ("other_service_activities", "S", "その他のサービス活動", "内訳（同上）"),
    ("households", "T", "家計の雇用活動", "内訳（同上・日本は 0）"),
    ("other_services", "RTU", "その他のサービス（R〜U）", "identity"),
]
_STAN_LABEL = {slug: name for slug, _c, name, _m in STAN_ACTIVITY}
_STAN_ORDER = {slug: i for i, (slug, *_r) in enumerate(STAN_ACTIVITY)}

# 制度部門（sna_sector／sna_sector_bs の dims）
SNA_SECTOR = [("nfc", "非金融法人企業"), ("fin", "金融機関"), ("gg", "一般政府"), ("hh", "家計（個人企業を含む）"), ("npish", "対家計民間非営利団体"),
              ("row", "海外"), ("total", "一国計"), ("nfc_private", "非金融法人企業（民間）"), ("nfc_public", "非金融法人企業（公的）")]
_SECTOR_LABEL = dict(SNA_SECTOR)
_SECTOR_ORDER = {s: i for i, (s, _n) in enumerate(SNA_SECTOR)}

_SNA_LABEL = {slug: name for slug, _lab, name, _p in SNA_ACTIVITY + SNA_ACTIVITY_S2_EXTRA}
_SNA_ORDER = {slug: i for i, (slug, *_r) in enumerate(SNA_ACTIVITY + SNA_ACTIVITY_S2_EXTRA)}
_SDBS_IND = {slug: name for slug, _c, name in SDBS_ACTIVITY}
_SDBS_SIZE = {slug: name for slug, _c, name in SDBS_SIZE}
_SDBS_ORDER = {slug: i for i, (slug, *_r) in enumerate(SDBS_ACTIVITY)}
_SDBS_SIZE_ORDER = {slug: i for i, (slug, *_r) in enumerate(SDBS_SIZE)}


# ---------------------------------------------------------------- QE（qe2020）：measure＝{item}_{variant} の直積（第 9 弾 発見層の variant 畳み）
QE_ITEMS = [  # (slug, 表示名)＝原典の列順。measure の先頭が item・残りが variant（分解は長い slug 優先）
    ("gdp", "国内総生産（支出側）"), ("private_consumption", "民間最終消費支出"), ("hh_consumption", "家計最終消費支出"),
    ("priv_housing", "民間住宅"), ("priv_capex", "民間企業設備"), ("private_inventories", "民間在庫変動"),
    ("gov_consumption", "政府最終消費支出"), ("public_investment", "公的固定資本形成"), ("public_inventories", "公的在庫変動"),
    ("net_exports", "財貨・サービスの純輸出"), ("exports", "財貨・サービスの輸出"), ("imports", "財貨・サービスの輸入"),
    ("domestic_demand", "国内需要"),
]
QE_VARIANTS = {  # variant slug → 表示名（ファミリーカードの label）
    "nominal": "名目実額（季節調整系列・年率換算 10億円）",
    "real": "実質実額（連鎖方式・季節調整系列・年率換算 10億円）",
    "deflator": "デフレーター水準（季節調整系列・2020暦年=100）",
    "deflator_qoq": "デフレーター前期比（季節調整系列・％）",
    "deflator_yoy": "デフレーター前年同期比（原系列・％）",
    "real_contrib": "実質GDP前期比への寄与度（季節調整系列・％pt）",
    "real_contrib_ann": "実質GDP前期比年率への寄与度（季節調整系列・％pt）",
    "real_qoq": "実質GDP前期比（季節調整系列・％）",
    "real_qoq_ann": "実質 前期比年率（季節調整系列・％）",
    "nominal_qoq_ann": "名目 前期比年率（季節調整系列・％）",
}
_QE_ITEM_LABEL = dict(QE_ITEMS)
_QE_ITEM_ORDER = {slug: i for i, (slug, _n) in enumerate(QE_ITEMS)}
_QE_ITEMS_BY_LEN = sorted(_QE_ITEM_LABEL, key=len, reverse=True)


def split_measure_variant(measure: str) -> tuple[str, str] | tuple[None, None]:
    """qe2020 の measure を (item, variant) に分解（長い item slug 優先の前方一致）。分解できなければ (None, None)。"""
    for it in _QE_ITEMS_BY_LEN:
        if measure.startswith(it + "_"):
            return it, measure[len(it) + 1:]
    return None, None


def qe_item_label(slug: str) -> str:
    return _QE_ITEM_LABEL.get(slug, slug)


def qe_item_order(slug: str) -> int:
    return _QE_ITEM_ORDER.get(slug, 10**6)


def qe_variant_label(variant: str) -> str:
    return QE_VARIANTS.get(variant, variant)


# 消費者物価指数 2025 年基準（e-Stat 0004052037）の地域＝全国＋都道府県庁所在市・政令指定都市 52 市（JIS 市区町村コード 5 桁＝region）。
# 都市階級（大都市・中都市…）・地方（北海道地方…）の集計区分（コード 000xx）は JIS コードでないため収録しない（第 11 弾 第 3 便・2026-09-15）。
CPI_CITY_AREAS: tuple[tuple[str, str], ...] = (
    ("01100", "札幌市"), ("02201", "青森市"), ("03201", "盛岡市"), ("04100", "仙台市"), ("05201", "秋田市"), ("06201", "山形市"),
    ("07201", "福島市"), ("08201", "水戸市"), ("09201", "宇都宮市"), ("10201", "前橋市"), ("11100", "さいたま市"), ("12100", "千葉市"),
    ("13100", "東京都区部"), ("14100", "横浜市"), ("14130", "川崎市"), ("14150", "相模原市"), ("15100", "新潟市"), ("16201", "富山市"),
    ("17201", "金沢市"), ("18201", "福井市"), ("19201", "甲府市"), ("20201", "長野市"), ("21201", "岐阜市"), ("22100", "静岡市"),
    ("22130", "浜松市"), ("23100", "名古屋市"), ("24201", "津市"), ("25201", "大津市"), ("26100", "京都市"), ("27100", "大阪市"),
    ("27140", "堺市"), ("28100", "神戸市"), ("29201", "奈良市"), ("30201", "和歌山市"), ("31201", "鳥取市"), ("32201", "松江市"),
    ("33100", "岡山市"), ("34100", "広島市"), ("35203", "山口市"), ("36201", "徳島市"), ("37201", "高松市"), ("38201", "松山市"),
    ("39201", "高知市"), ("40100", "北九州市"), ("40130", "福岡市"), ("41201", "佐賀市"), ("42201", "長崎市"), ("43100", "熊本市"),
    ("44201", "大分市"), ("45201", "宮崎市"), ("46201", "鹿児島市"), ("47201", "那覇市"),
)
CPI_CITY_LABEL = dict(CPI_CITY_AREAS)

# 毎月勤労統計（maikin・第 11 弾）：dims＝<就業形態>-<規模>。industry の位置に就業形態を置く（発見層の facet 名は industry/size のまま）。
_MAIKIN_EMP = {"total": "就業形態計", "general": "一般労働者", "part": "パートタイム労働者"}
_MAIKIN_SIZE = {"5plus": "事業所規模5人以上", "30plus": "事業所規模30人以上"}
_MAIKIN_EMP_ORDER = {s: i for i, s in enumerate(("total", "general", "part"))}
_MAIKIN_SIZE_ORDER = {s: i for i, s in enumerate(("5plus", "30plus"))}


def dim_label(position: str, slug: str, dataset: str = "") -> str:
    """dims トークンの表示名（position＝industry／size）。dataset で語彙を切り替える（スラグは dataset 間で衝突し得る＝mfg_food 等）。
    語彙に無ければスラグをそのまま返す。"""
    from stats.core.hojin_vocab import dim_label as _hojin
    if dataset == "sna_activity":
        return _SNA_LABEL.get(slug, slug)
    if dataset == "sdbs":
        return (_SDBS_IND if position == "industry" else _SDBS_SIZE).get(slug, slug)
    if dataset == "stan":
        return _STAN_LABEL.get(slug, slug)
    if dataset in ("sna_sector", "sna_sector_bs", "fof", "sna_fcs"):
        return _SECTOR_LABEL.get(slug, slug)
    if dataset == "maikin":
        return (_MAIKIN_EMP if position == "industry" else _MAIKIN_SIZE).get(slug, slug)
    return _hojin(position, slug)


def dim_order(position: str, dataset: str = "") -> dict[str, int]:
    """ファミリーカードの並び（原典の表の順）。"""
    from stats.core.hojin_vocab import INDUSTRY_ORDER, SIZE_ORDER
    if dataset == "sna_activity":
        return _SNA_ORDER if position == "industry" else {}
    if dataset == "sdbs":
        return _SDBS_ORDER if position == "industry" else _SDBS_SIZE_ORDER
    if dataset == "stan":
        return _STAN_ORDER if position == "industry" else {}
    if dataset in ("sna_sector", "sna_sector_bs", "fof", "sna_fcs"):
        return _SECTOR_ORDER if position == "industry" else {}
    if dataset == "maikin":
        return _MAIKIN_EMP_ORDER if position == "industry" else _MAIKIN_SIZE_ORDER
    return INDUSTRY_ORDER if position == "industry" else SIZE_ORDER
