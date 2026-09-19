"""
法人企業統計（e-Stat 0003060791）の業種（cat02）・規模（cat03）語彙＝スラグ対応表。定義はここ（コード）が正。
core に置く理由＝発見層のファミリーカード（Registry.search_collapsed）がスラグの表示名を要るため（ingest と serving の両方から参照）。

- 第 6 弾（2026-08-21 調査・データ拡充計画.md §4a）：62 コードは現行分類で 1 業種 1 コード・欠年なしの 1 連続区間。
  旧分類の細分は e-Stat 側で「(H20年度まで)」の別コード（slug 末尾 _h20）＝FY2008 で終わる。
- first_fy は売上高（cat01=045・全規模）で値が始まる年度（実測）。項目によりさらに遅いもの（従業員賞与 FY2007–）は系列側で上書き。
- parent は合計整合チェックの親（Σ子＝親。FY2009 以降で厳密成立・FY2008 以前は内訳が揃わず不成立＝記録のみ）。
- スラグは `_` 区切り（dims の `<業種>-<規模>` を最初の `-` で分割できるよう業種側に `-` を使わない）。
"""
from __future__ import annotations

# (slug, cat02 コード, e-Stat 表示名, 売上高の開始年度, 親コード)
HOJIN_INDUSTRIES: list[tuple[str, str, str, str, str | None]] = [
    ("allexfin", "104", "全産業（除く金融保険業）", "FY1960", None),
    ("mfg", "108", "製造業", "FY1960", "104"),
    ("mfg_food", "109", "食料品製造業", "FY1960", "108"),
    ("mfg_textile", "110", "繊維工業", "FY2009", "108"),
    ("mfg_textile_h20", "163", "繊維工業(H20年度まで)", "FY1960", "108"),
    ("mfg_apparel_h20", "111", "衣服・その他の繊維製品製造業(H20年度まで)", "FY1975", "108"),
    ("mfg_wood", "112", "木材・木製品製造業", "FY1975", "108"),
    ("mfg_pulp_paper", "113", "パルプ・紙・紙加工品製造業", "FY1960", "108"),
    ("mfg_printing", "114", "印刷・同関連業", "FY1975", "108"),
    ("mfg_chemical", "115", "化学工業", "FY1960", "108"),
    ("mfg_petroleum_coal", "116", "石油製品・石炭製品製造業", "FY1975", "108"),
    ("mfg_ceramics", "117", "窯業・土石製品製造業", "FY1960", "108"),
    ("mfg_steel", "118", "鉄鋼業", "FY1960", "108"),
    ("mfg_nonferrous", "119", "非鉄金属製造業", "FY1960", "108"),
    ("mfg_metal_products", "120", "金属製品製造業", "FY1960", "108"),
    ("mfg_general_machinery", "154", "はん用機械器具製造業", "FY2009", "108"),
    ("mfg_production_machinery", "121", "生産用機械器具製造業", "FY1960", "108"),
    ("mfg_business_machinery", "124", "業務用機械器具製造業", "FY1971", "108"),
    ("mfg_electrical", "122", "電気機械器具製造業", "FY1960", "108"),
    ("mfg_ict_equipment", "145", "情報通信機械器具製造業", "FY2004", "108"),
    ("mfg_transport_equipment", "146", "輸送用機械器具製造業(集約)", "FY2004", "108"),
    ("mfg_motor_vehicles", "123", "自動車・同附属品製造業", "FY1960", "146"),
    ("mfg_other_transport", "125", "その他の輸送用機械器具製造業", "FY1960", "146"),
    ("mfg_other", "126", "その他の製造業", "FY1960", "108"),
    ("nonmfg", "144", "非製造業", "FY1960", "104"),
    ("agri_forestry_fishery", "105", "農林水産業(集約)", "FY1960", "144"),
    ("agri_forestry", "101", "農業、林業", "FY2009", "105"),
    ("agri_h20", "162", "農業(H20年度まで)", "FY1961", "105"),
    ("forestry_h20", "102", "林業(H20年度まで)", "FY1963", "105"),
    ("fishery", "103", "漁業", "FY1960", "105"),
    ("mining", "106", "鉱業、採石業、砂利採取業", "FY1960", "144"),
    ("construction", "107", "建設業", "FY1960", "144"),
    ("electricity", "135", "電気業", "FY1960", "144"),
    ("gas_heat_water", "136", "ガス・熱供給・水道業", "FY1960", "144"),
    ("ict", "142", "情報通信業", "FY1971", "144"),
    ("transport_postal", "134", "運輸業、郵便業(集約)", "FY1960", "144"),
    ("land_transport", "131", "陸運業", "FY1960", "134"),
    ("water_transport", "132", "水運業", "FY1960", "134"),
    ("other_transport", "133", "その他の運輸業", "FY1971", "134"),
    ("wholesale_retail", "129", "卸売業・小売業(集約)", "FY1960", "144"),
    ("wholesale", "127", "卸売業", "FY1960", "129"),
    ("retail", "128", "小売業", "FY1960", "129"),
    ("realestate_leasing", "155", "不動産業、物品賃貸業(集約)", "FY2009", "144"),
    ("realestate", "130", "不動産業", "FY1960", "155"),
    ("goods_leasing", "149", "物品賃貸業(集約)", "FY2004", "155"),
    ("leasing", "150", "リース業", "FY2004", "149"),
    ("other_goods_leasing", "151", "その他の物品賃貸業", "FY2004", "149"),
    ("services", "137", "サービス業(集約)", "FY1960", "144"),
    ("accommodation_food", "156", "宿泊業、飲食サービス業(集約)", "FY2009", "137"),
    ("accommodation", "139", "宿泊業", "FY1975", "156"),
    ("food_services", "148", "飲食サービス業", "FY2004", "156"),
    ("living_amusement", "157", "生活関連サービス業、娯楽業(集約)", "FY2009", "137"),
    ("living_services", "140", "生活関連サービス業", "FY1975", "157"),
    ("amusement", "141", "娯楽業", "FY1975", "157"),
    ("professional", "161", "学術研究、専門・技術サービス業(集約)", "FY2009", "137"),
    ("advertising", "138", "広告業", "FY1975", "161"),
    ("pure_holding", "158", "純粋持株会社", "FY2009", "161"),
    ("other_professional", "159", "その他の学術研究、専門・技術サービス業", "FY2009", "161"),
    ("education", "153", "教育、学習支援業", "FY2004", "137"),
    ("medical_welfare", "152", "医療、福祉業", "FY2004", "137"),
    ("staffing", "160", "職業紹介・労働者派遣業", "FY2009", "137"),
    ("other_services", "143", "その他のサービス業", "FY1975", "137"),
]

BY_SLUG = {s: (code, name, fy, parent) for s, code, name, fy, parent in HOJIN_INDUSTRIES}
BY_CODE = {code: (s, name, fy, parent) for s, code, name, fy, parent in HOJIN_INDUSTRIES}
assert len(BY_SLUG) == len(BY_CODE) == 62
INDUSTRY_ORDER = {s: i for i, (s, *_rest) in enumerate(HOJIN_INDUSTRIES)}  # e-Stat の表の順（製造業→内訳→非製造業→内訳）

# 規模（cat03）。区分の細分・統合は年で異なるため最小版は 全規模＋3 区分（1千万円未満 16 は Tier 2 で追加予定）。
HOJIN_SIZE = {"allsize": ("26", "全規模"), "cap1b": ("25", "資本金10億円以上"),
              "cap100m-1b": ("24", "資本金1億円以上10億円未満"), "cap10m-100m": ("19", "資本金1千万円以上1億円未満"),
              "capu10m": ("16", "資本金1千万円未満")}  # capu10m は第 6 弾 Tier 2（2026-08-21）で追加＝FY1975〜。4 区分の和＝全規模
HOJIN_SIZE_NOTE = ("規模区分は資本金階級（e-Stat cat03）。区分の細分・統合は年で異なるため 10億円以上／1億〜10億／1千万〜1億／1千万円未満（FY1975〜）の 4 区分。"
                   "4 区分の和＝全規模（FY1975 以降）。")
SIZE_FIRST = {"capu10m": "FY1975"}  # 規模区分ごとの開始年度（他は FY1960）
SIZE_ORDER = {s: i for i, s in enumerate(HOJIN_SIZE)}


def dim_label(position: str, slug: str) -> str:
    """dims トークンの表示名（industry／size）。語彙に無ければスラグをそのまま返す。"""
    if position == "industry" and slug in BY_SLUG:
        return BY_SLUG[slug][1]
    if position == "size" and slug in HOJIN_SIZE:
        return HOJIN_SIZE[slug][1]
    return slug


def industry_note(slug: str) -> str:
    """系列 notes に刻む業種の注意（分類境界・集約・旧分類）。"""
    code, name, fy, parent = BY_SLUG[slug]
    out = []
    if slug.endswith("_h20"):
        out.append("旧分類（平成20年度＝FY2008 まで）の業種＝FY2009 以降は値なし（後継は現行分類の別系列）。")
    elif fy >= "FY2009":
        out.append("日本標準産業分類 2007 年改定の適用（FY2009）以降の業種＝それ以前は値なし。")
    elif slug not in ("allexfin", "mfg", "nonmfg"):
        out.append("業種別の内訳は FY2009（産業分類改定）を境に定義が変わりうる。合計整合（Σ内訳＝計）は FY2009 以降で成立・以前は不成立。")
    if "(集約)" in name:
        out.append("「(集約)」＝e-Stat が複数業種を合算した集計区分。")
    return " ".join(out)


# ---------------------------------------------------------------- 分析の定石（利用者 Claude がツール応答で読める場所に置く＝文書にしか無いと届かない）
# list_datasets の hojin エントリ（analysis_notes）・families の usage_hint・pure_holding の notes から参照される。値は含まない（式と系列 ID のみ）。
HOJIN_ANALYSIS_NOTES = {
    "value_added": (
        "付加価値＝e-Stat の定義値 mof.hojin.value_added.<業種>-<規模>.fy（cat01=073）を直接引くのが正（合成しない）。"
        "法人企業統計の定義：付加価値＝人件費（役員給与＋役員賞与＋従業員給与＋従業員賞与＋福利厚生費）＋支払利息等＋動産・不動産賃借料＋租税公課＋営業純益。"
        "「営業利益＋減価償却費＋人件費」型の合成は別定義（営業純益≠営業利益・賃借料/租税公課の扱いが違う）＝Excel 等と突き合わせるときは定義を揃える。"
        "役員給与・役員賞与を含むか含まないかで一人当たり付加価値が数％変わる。"
        "定義の典拠＝財政金融統計月報 第846号 調査方法の概要 §8・§9⑴（FY2007 以降の算式。FY2006 以前は賞与 2 項目を別建てしない旧算式＝breaks を参照）。"),
    "value_added_identity": (
        "付加価値の恒等式（誤読が多い＝2026-08-24 利用側の実例）：**営業純益 ＝ 営業利益 − 支払利息等**。"
        "したがって定義式『付加価値＝人件費＋支払利息等＋賃借料＋租税公課＋**営業純益**』は、営業利益で書き直すと"
        "**付加価値＝人件費＋賃借料＋租税公課＋営業利益**（**支払利息等は相殺されて消える**）。"
        "『営業利益＋人件費＋支払利息＋賃借料＋租税公課』と書いてある資料は、営業利益の位置に営業純益を置いているか、支払利息を二重に足している。"
        "労働分配率＝人件費÷付加価値も同じ理由で分母の取り方が変わる（純粋持株会社を含むかでも変わる＝pure_holding の項）。"),
    "per_employee": "一人当たり付加価値＝value_added ÷ employees_avg（期中平均従業員数・人）。同じセル（業種×規模）同士で割る。",
    "pure_holding": (
        "純粋持株会社（mof.hojin.<measure>.pure_holding-<規模>.fy・FY2009〜）は売上が子会社配当・経営管理料で原価がほぼ立たず、従業員が少ない"
        "（一人当たり付加価値 数十百万円）。上位集計（professional ⊂ services ⊂ nonmfg ⊂ allexfin）の比率・一人当たり指標を押し上げる。"
        "実勢を見るときは同じ期・同じ規模の pure_holding を分子・分母の両方から引く："
        "粗利率(持株除く)＝1−(cogs.X−cogs.pure_holding)/(sales.X−sales.pure_holding)、"
        "一人当たりVA(持株除く)＝(value_added.X−value_added.pure_holding)/(employees_avg.X−employees_avg.pure_holding)。"
        "Σ子＝親は FY2009 以降で厳密に成立するので、どの階層 X からも同じ値を引けば整合する（連結消去ではない点に注意）。"
        "財務省自身も「純粋持株会社の経常利益」（全産業の計数から純粋持株会社の計数を除いた結果）を公表している"
        "（https://www.mof.go.jp/pri/reference/ssc/results/data.htm の keitunenenpo.xlsx）＝この控除は財務省の扱いと同じ。"),
    "size_classes": (
        "規模 4 区分（cap1b／cap100m-1b／cap10m-100m／capu10m＝1千万円未満・FY1975〜）の和＝全規模（allsize）。"
        "3 区分だけでは全規模に届かない（1千万円未満が従業員の約 2 割）。業種×規模のセルは標本が小さく年々の振れが大きい＝単年より数年平均で見る。"),
    "accounting_break": (
        "収益認識基準（FY2021 強制・影響幅 FY2018〜FY2022）は売上高と売上原価・販管費を同額動かす＝**売上を分母にする比率**（粗利率・原価率・売上高営業利益率・付加価値率）だけが壊れる。"
        "売上総利益・営業利益・付加価値・一人当たり付加価値の**水準**は不変＝断層を跨いで使える。"),
    "deflator": "実質化は内閣府 SNA のデフレーター（cao.sna2020.deflator.fy・2020 暦年連鎖）を利用側で当てる（stats は換算しない）。",
    "consistency": "Σ業種内訳＝業種計は製造業 FY2004 以降・非製造業/サービス業 FY2009 以降で厳密成立（それ以前は旧分類）。会計恒等式 売上高−売上原価−販管費＝営業利益は全セル成立。",
}
