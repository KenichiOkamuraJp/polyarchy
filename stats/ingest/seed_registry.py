"""
S 系列（docs/指標棚卸し.md §5・データソース選定.md §2）をレジストリ `stats/data/registry/series.jsonl` に書き出す。

- 定義はここ（コード）が正で、jsonl は生成物（両方 git 追跡）。変更はここを直して再生成。
- e-Stat で表・分類コードまで確定した系列は status="registered"（取込 `stats.ingest.estat` の対象）。
  取得元は決まったが表・コードの確定が取込時になる系列は status="planned"（発見層には出る・lookup は found=false）。
- 派生（仮想）エントリは components を持ち値を持たない。

実行（リポジトリ root）：  python -m stats.ingest.seed_registry
"""
from __future__ import annotations

import json
from pathlib import Path

from polyarchy_common.taxonomy import TAGS

from stats.core.licenses import license_for
from stats.core.paths import REGISTRY_PATH
from stats.core.registry import Registry, Series
from stats.core.hojin_vocab import HOJIN_INDUSTRIES, BY_SLUG as HOJIN_IND_INFO, HOJIN_SIZE, HOJIN_SIZE_NOTE, SIZE_FIRST, industry_note

# 分野タグは polyarchy_common.taxonomy.TAGS（英字キー→表示語・21 分類）を**キーで参照**（文字列の複製を作らない＝taxonomy の規約）。
# 語彙に無い名前で引くと起動時に KeyError＝改名が黙って通らない。
TAG_MACRO = TAGS["macro"]
TAG_FISCAL = TAGS["tax_fiscal"]
TAG_FIN = TAGS["finance"]
TAG_IND = TAGS["industry"]
TAG_LABOR = TAGS["labor"]
TAG_SOC = TAGS["social_sec"]
TAG_POP = TAGS["children_pop"]
TAG_CORP = TAGS["governance"]
TAG_TRADE = TAGS["trade"]
TAG_SME = TAGS["sme"]
TAG_REGION = TAGS["regional"]

ESTAT_URL = "https://www.e-stat.go.jp/dbview?sid={sid}"
# 国民経済計算 年次推計（確報）の版＝この年の公表分。基準改定・年次推計の更新でここを上げて再取込する。
SNA_EDITION = "2024"
ESRI_TOP = f"https://www.esri.cao.go.jp/jp/sna/data/data_list/kakuhou/files/{SNA_EDITION}/{SNA_EDITION}_kaku_top.html"


def _s(**kw) -> Series:
    kw.setdefault("lang", "ja")
    kw.setdefault("region_codes", ("JP",))
    return Series(**kw)


# 法人企業統計 時系列（e-Stat）：業種 cat02／規模 cat03 の語彙（dims と表示名・区分定義）
# 業種は第 6 弾（2026-08-21）で 62 コードへ拡張＝語彙は hojin_industries.HOJIN_INDUSTRIES（コードが正）。
HOJIN_IND = {slug: (code, name) for slug, code, name, _fy, _parent in HOJIN_INDUSTRIES}
# 規模語彙 HOJIN_SIZE／HOJIN_SIZE_NOTE は core/hojin_vocab.py（業種と同じ場所）。


# ---------------------------------------------------------------- 第6弾 段3：法人企業統計 年度次の断層表（breaks）
# 典拠は原典注記・制度文書。データからの推定は入れない（検定は見落とし検出にだけ使い、結果は文書に記録）。
MOF846 = ("財務省財務総合政策研究所「財政金融統計月報」第846号（令和3年度 法人企業統計年報特集・2022-10）調査方法の概要 §9 本調査結果利用上の注意"
          "（https://www.mof.go.jp/pri/publication/zaikin_geppo/hyou/g846/846_b.pdf）")
_BRK_ACCOUNTING_FY2021 = {
    "period": "FY2021", "kind": "accounting", "treatment": "exclude_ratio",
    "affects_from": "FY2018", "affects_to": "FY2022",  # 早期適用可（FY2018）〜強制適用の翌年度（3 月決算以外の法人が入り切る）
    "label": "収益認識に関する会計基準（企業会計基準第29号）の強制適用＝総額表示→純額表示（代理人取引・消化仕入等）",
    "note": "売上高と売上原価・販管費の対応関係が変わるため、粗利率・原価率等の**比率は FY2020 以前と比較できない**（水準補正では直せない）。"
            "早期適用は FY2018 から可・強制は 2021 年 4 月 1 日以後開始の事業年度＝影響は FY2020〜FY2022 に分散。卸売・小売・広告等の代理人性の高い業種で大きい。",
    "affected": "売上高を分母にする比率（粗利率・原価率・売上高営業利益率・付加価値率）と売上高・売上原価・販管費の水準の前後比較",
    "invariant": "売上総利益・営業利益・付加価値・一人当たり付加価値・人件費の水準（売上と原価を同額動かすため差額は不変＝断層を跨いで使える）",
    "source": "企業会計基準委員会 企業会計基準第29号「収益認識に関する会計基準」（2018-03-30 公表・2020-03-31 改正）。"
              "財務省側＝" + MOF846 + " ⑻「平成10年以降企業会計基準等の変更が行われているが，当調査は…財務諸表上の計数について調査している統計であるため，"
              "こうした会計処理方法の変更に伴う影響があることに留意する必要がある」（収益認識基準を名指しした個別注記は無い＝2026-08-22 確認）",
}
_BRK_POPULATION_FY2012 = {
    "period": "FY2012", "kind": "population", "treatment": "level_shift",
    "label": "FY2012 の一時的な段差（全産業・全規模の有形固定資産 458.8→428.1→455.2 兆円＝翌年度に復帰）",
    "note": "標本法人の入替え・母集団推計に起因する一年限りの段差とみられる。ストック系列の FY2012 は前後と接続しない（水準比較は FY2011→FY2013 で行う）。",
    "source": "e-Stat 0003060791 の収録値で確認（2026-08-21）。財務省の個別注記は**無い**ことを確認（2026-08-22）＝財政金融統計月報 第738号（平成24年度年報特集）"
              "調査方法の概要 §3 標本法人の選定方法に年次の標本名簿更新（平成24年3月末現在の法人名簿）の記載があるのみ",
}
_BRK_COVERAGE_BONUS = {
    "period": "FY2007", "kind": "coverage", "treatment": "note_only",
    "label": "従業員賞与の表章開始（平成18年度＝FY2006 以前は従業員給与に含めて調査・平成19年度＝FY2007 以降は単独項目）",
    "note": "人件費を構成 5 項目で合成するときは FY2007 以降で揃う。FY2006 以前の従業員給与は賞与込み＝給与だけの前後比較は不可。",
    "source": MOF846 + " §8 付加価値率の注「従業員賞与は，平成18年度調査以前では従業員給与に含めて調査を行っていたが，平成19年度調査以降は従業員給与に含めず単独項目として調査」",
}
_BRK_CLASSIFICATION_FY2009 = {
    "period": "FY2009", "kind": "classification", "treatment": "separate_series",
    "label": "日本標準産業分類 2007 年改定の適用（業種細分の再編＝旧分類は (H20年度まで) の別系列）",
    "note": "業種計（製造業・非製造業・サービス業 等）は継続系列だが、内訳の構成が FY2009 で変わる。Σ内訳＝計は製造業 FY2004 以降・非製造業/サービス業 FY2009 以降で成立。",
    "source": "財務省 報道発表 2009-06-25「法人企業統計調査の標本抽出方法等を変更します」§4「平成20年4月1日から改定『日本標準産業分類』…が施行されたことから，本調査の業種分類についてもこれに準拠して，"
              "平成21年4～6月期調査から改定」（https://www.mof.go.jp/pri/reference/ssc/summary/hyohon.htm・別添5 業種分類の見直し／新旧業種分類の接続）。同時に標本抽出方法も変更"
              "（1〜5億円層の抽出改良・5〜10億円層を全数・ローテーション・サンプリング導入）。e-Stat cat02 の (H20年度まで) コードの終了＝FY2008 と整合",
}
_BRK_HOLDING = {
    "period": "FY2009", "kind": "coverage", "treatment": "note_only",
    "label": "純粋持株会社の区分新設（FY2009〜）＝持株会社化の進展で売上 1.7 兆（FY2009）→17.5 兆（FY2024）・粗利率 83〜87%",
    "note": "持株会社の売上は子会社からの受取配当・経営管理料で原価がほぼ立たない＝上位集計（学術専門(集約)・サービス業(集約)・非製造業・全産業）の粗利率を押し上げる。"
            "実勢を見るときは同じ期の mof.hojin.<measure>.pure_holding-allsize.fy を控除する（連結消去ではない点に注意）。",
    "source": "e-Stat 0003060791 cat02=158 の収録開始年度と収録値（2026-08-21 確認）",
}
_BRK_CLASSIFICATION_FY2004 = {
    "period": "FY2004", "kind": "classification", "treatment": "separate_series",
    "label": "製造業内訳の再編（日本標準産業分類 2002 年改定の適用＝情報通信機械器具・輸送用機械器具(集約)・物品賃貸業 等の新設、電気機械からの分離）",
    "note": "製造業の内訳は FY2004 を境に構成が変わる（例：電気機械器具の従業員数が FY2004 に約 4 割減＝情報通信機械へ分離）。Σ製造業内訳＝製造業計は FY2004 以降で成立。",
    "source": MOF846 + " ⑽「平成16年度調査より業種分類の変更（見直し等）が行われた」。e-Stat cat02 の新区分の開始年度（FY2004）と収録値の段差で確認（2026-08-21）",
}
_BRK_ACCOUNTING_OFFICER_BONUS = {
    "period": "FY2007", "kind": "accounting", "treatment": "level_shift", "affects_from": "FY2006", "affects_to": "FY2007",
    "label": "役員賞与の調査区分の変更（平成18年度＝FY2006 以前は利益処分項目・平成19年度＝FY2007 以降は費用項目）＝FY2006 は移行年で極端に小さい",
    "note": "FY2006 の役員賞与は前後と接続しない（全産業で前年の 1/30 以下＝会社法施行 2006-05 で利益処分としての役員賞与が消え、費用項目としての調査は FY2007 から）。"
            "役員報酬の時系列は FY2007 以降か FY2005 以前で分けて見る。内部留保率の算式も FY2007 で変更。",
    "source": MOF846 + " §8 注「役員賞与は，平成18年度調査以前では利益処分項目として調査を行っていたが，平成19年度調査以降は費用項目として調査」"
              "・企業会計基準第4号「役員賞与に関する会計基準」（2005-11-29）・会社法（2006-05-01 施行）",
}
_BRK_CLASSIFICATION_FY1975 = {
    "period": "FY1975", "kind": "classification", "treatment": "separate_series",
    "label": "業種分類の変更（昭和50年度＝FY1975 調査から）",
    "note": "FY1975 を境に業種内訳の構成が変わる（木材・印刷・石油石炭・宿泊・生活関連・娯楽・広告 等はこの年度から表章）。業種計は継続。",
    "source": MOF846 + " ⑹「昭和50年度調査から業種分類の変更が行われた」。e-Stat cat02 の開始年度 FY1975 の区分と整合",
}
_BRK_VA_DEFINITION_FY2007 = {
    "period": "FY2007", "kind": "coverage", "treatment": "level_shift",
    "label": "付加価値の定義変更（平成19年度＝FY2007 以降は役員賞与・従業員賞与を人件費に含めて算出）",
    "note": "FY2006 以前：付加価値＝営業純益＋役員給与＋従業員給与（賞与込み）＋福利厚生費＋支払利息等＋賃借料＋租税公課。FY2007 以降：役員賞与・従業員賞与を明示的に加算。"
            "従業員賞与は FY2006 以前は給与に含まれていたため実質的な差は役員賞与分（利益処分→費用）。長期の付加価値率・労働分配率は FY2007 で段差がありうる。",
    "source": MOF846 + " §8 付加価値率［平成18年度調査以前］［平成19年度調査以降］の算式",
}
PL_RATIO_MEASURES = {"sales", "cogs", "sga", "operating_profit", "ordinary_profit", "net_income", "value_added",
                     "operating_surplus", "nonoperating_income", "nonoperating_expenses"}
ACCOUNTING_AFFECTED = {"sales", "cogs", "sga"}
VA_MEASURES = {"value_added", "operating_surplus", "interest_paid", "rent_paid", "taxes_dues", "depreciation",
               "employee_wages", "employee_bonus", "officer_compensation", "officer_bonus", "welfare_costs", "personnel_costs", "labour_share"}
STOCK_MEASURES = {"tangible_fixed_assets", "intangible_fixed_assets", "land", "fixed_assets", "total_assets", "investments_other", "stocks_fixed"}
HOLDING_PARENTS = {"pure_holding", "professional", "services", "nonmfg", "allexfin"}


def hojin_breaks(measure: str, ind: str, first_period: str) -> tuple[dict, ...]:
    """系列に付く断層（測度×業種で機械適用）。順序＝期の昇順。"""
    out: list[dict] = []
    if ind not in ("allexfin", "mfg", "nonmfg") and first_period < "FY2009" and not ind.endswith("_h20"):
        out.append(_BRK_CLASSIFICATION_FY2009)
    if ind.startswith("mfg_") and first_period < "FY2004" and not ind.endswith("_h20"):
        out.append(_BRK_CLASSIFICATION_FY2004)
    if ind not in ("allexfin", "mfg", "nonmfg") and first_period < "FY1975":
        out.append(_BRK_CLASSIFICATION_FY1975)
    if measure in ("value_added", "labour_share"):
        out.append(_BRK_VA_DEFINITION_FY2007)
    if measure == "officer_bonus":
        out.append(_BRK_ACCOUNTING_OFFICER_BONUS)
    if measure == "employee_bonus":
        out.append(_BRK_COVERAGE_BONUS)
    if measure in (PL_RATIO_MEASURES | VA_MEASURES | {"employees_avg"}) and ind in HOLDING_PARENTS:
        out.append(_BRK_HOLDING)
    if measure in STOCK_MEASURES:
        out.append(_BRK_POPULATION_FY2012)
    if measure in ACCOUNTING_AFFECTED:  # 水準が動くのは売上高・売上原価・販管費のみ（差額＝利益・付加価値は不変＝caution を付けない）
        out.append(_BRK_ACCOUNTING_FY2021)
    last = "FY2008" if ind.endswith("_h20") else "FY9999"   # 旧分類の系列は FY2008 で終わる
    out = [b for b in out if first_period <= b["period"] <= last]  # 系列の収録期間内の断層だけ
    return tuple(sorted(out, key=lambda b: b["period"]))


def hojin_usable_from(ind: str, first_period: str) -> str:
    """分類改定由来の「連続利用できる最初の年度」。製造業内訳＝FY2004・非製造業/サービス業内訳＝FY2009（合計整合の実測）・業種計と全産業＝first_period。"""
    from stats.core.hojin_vocab import BY_SLUG
    if ind in ("allexfin", "mfg", "nonmfg") or ind.endswith("_h20"):
        return first_period
    parent = BY_SLUG[ind][3]
    floor = "FY2004" if parent == "108" or ind.startswith("mfg_") else "FY2009"
    return max(first_period, floor)


def hojin_fy(measure: str, code: str, title: str, unit: str = "百万円", tags=(TAG_CORP, TAG_IND), notes: str = "",
             ind: str = "allexfin", size: str = "allsize", first_period: str = "FY1960") -> Series:
    """法人企業統計 時系列 年度次（0003060791）の1セル列＝1系列。dims=<業種>-<規模>。"""
    sid = "0003060791"
    icode, iname = HOJIN_IND[ind]
    scode, sname = HOJIN_SIZE[size]
    dims = f"{ind}-{size}"
    extra = "" if (ind == "allexfin" and size == "allsize") else " " + HOJIN_SIZE_NOTE
    return _s(
        series_id=f"mof.hojin.{measure}.{dims}.fy", title=f"法人企業統計 {title}（{iname}・{sname}・年度）",
        org="mof", org_name="財務省", source_url=ESTAT_URL.format(sid=sid), sector="企業", unit=unit,
        granularity="年度", freq="fy", dataset="hojin", measure=measure, dims=dims,
        stat_name="法人企業統計調査 時系列データ", table_id=sid, table_title="金融業、保険業以外の業種（原数値）年度次",
        policy_tags=tuple(tags) + ((TAG_SME,) if size == "cap10m-100m" else ()), period_converter="estat_hojin_fy", first_period=first_period,
        accessor={"type": "estat", "statsDataId": sid, "cdCat01": code, "cdCat02": icode, "cdCat03": scode},
        # {item}（title）に業種・規模が入るので重ねない（A-4・2026-08-22：パネルで表ごとのテンプレート 1 本に括るため系列固有の語を持たない）
        citation_template="財務省「法人企業統計調査 時系列データ」金融業、保険業以外の業種（原数値）年度次（e-Stat 0003060791・{item}／{period}）取得 {retrieved_at}",
        notes=(notes or "原数値。金額は百万円（換算しない）。") + extra,
        breaks=hojin_breaks(measure, ind, first_period), usable_from=hojin_usable_from(ind, first_period))


def hojin_fq(measure: str, code: str, title: str, unit: str = "百万円", tags=(TAG_CORP, TAG_IND), notes: str = "",
             ind: str = "allexfin", size: str = "allsize") -> Series:
    """法人企業統計 時系列 四半期（0003060191）。年度四半期表記 FY2024Q1＝4-6月。項目コードは年度表と異なる。"""
    sid = "0003060191"
    icode, iname = HOJIN_IND[ind]
    scode, sname = HOJIN_SIZE[size]
    dims = f"{ind}-{size}"
    return _s(
        series_id=f"mof.hojin.{measure}.{dims}.fq", title=f"法人企業統計 {title}（{iname}・{sname}・四半期・原数値）",
        org="mof", org_name="財務省", source_url=ESTAT_URL.format(sid=sid), sector="企業", unit=unit,
        granularity="四半期（年度）", freq="fq", dataset="hojin", measure=measure, dims=dims,
        stat_name="法人企業統計調査 時系列データ", table_id=sid, table_title="金融業、保険業以外の業種（原数値）四半期",
        policy_tags=tuple(tags), period_converter="estat_hojin_fq", first_period="FY1954Q1",
        accessor={"type": "estat", "statsDataId": sid, "cdCat01": code, "cdCat02": icode, "cdCat03": scode},
        citation_template="財務省「法人企業統計調査 時系列データ」金融業、保険業以外の業種（原数値）四半期（e-Stat 0003060191・{item}／{period}）取得 {retrieved_at}",
        notes=(notes or "原数値（季節調整値 0003066618 は別系列＝最小版外）。四半期は年度四半期表記（FY2024Q1＝2024年4-6月）。金額は百万円。")
              + "四半期調査は資本金1千万円以上が対象（年次調査と母集団が異なる）。",
        breaks=(({**_BRK_ACCOUNTING_FY2021, "period": "FY2021Q1", "affects_from": "FY2018Q1", "affects_to": "FY2022Q4"},)
                if measure in ACCOUNTING_AFFECTED else ()))


# ---------------------------------------------------------------- 第5弾(b)：法人企業統計 規模別の付加価値・営業外・人件費構成
# 付加価値の恒等式（法人企業統計の定義）：
#   付加価値 ＝ 人件費 ＋ 支払利息等 ＋ 動産・不動産賃借料 ＋ 租税公課 ＋ 営業純益
#   人件費   ＝ 役員給与 ＋ 役員賞与 ＋ 従業員給与 ＋ 従業員賞与 ＋ 福利厚生費
# 年次表に「人件費」の単独項目は無い（cat01 に存在しない）ため、人件費・労働分配率は**派生エントリ**として
# 構成系列を返す（値は計算しない＝参照粒度設計 §6）。構成5項目のうち賞与は表章開始が遅く、
# 全期間で人件費を組むなら「付加価値 − （支払利息等＋賃借料＋租税公課＋営業純益）」の側を使う。
HOJIN_VA = [  # (measure, cat01, 表示名, 単位, タグ)
    ("value_added", "073", "付加価値", (TAG_CORP, TAG_IND)),
    ("operating_surplus", "074", "営業純益", (TAG_CORP, TAG_IND)),
    ("rent_paid", "069", "動産・不動産賃借料", (TAG_CORP, TAG_IND)),
    ("taxes_dues", "070", "租税公課", (TAG_CORP, TAG_FISCAL)),
    ("interest_paid", "068", "支払利息等", (TAG_CORP, TAG_FIN)),
    ("officer_compensation", "065", "役員給与", (TAG_LABOR, TAG_CORP)),
    ("officer_bonus", "057", "役員賞与", (TAG_LABOR, TAG_CORP)),
    ("employee_bonus", "235", "従業員賞与", (TAG_LABOR, TAG_CORP)),
    ("welfare_costs", "067", "福利厚生費", (TAG_LABOR, TAG_CORP)),
    ("nonoperating_income", "049", "営業外収益", (TAG_CORP, TAG_FIN)),
    ("nonoperating_expenses", "050", "営業外費用", (TAG_CORP, TAG_FIN)),
    ("investment_securities", "151", "投資有価証券", (TAG_CORP, TAG_FIN)),
]
PERSONNEL_PARTS = ("officer_compensation", "officer_bonus", "employee_wages", "employee_bonus", "welfare_costs")


def hojin_size_expand(existing: set[str]) -> list[Series]:
    """HOJIN_VA の各項目を4区分（全規模＋資本金3区分）へ広げる。既に登録済みの series_id は作らない。"""
    out: list[Series] = []
    for size in ("allsize", "cap1b", "cap100m-1b", "cap10m-100m"):
        for measure, code, ttl, tags in HOJIN_VA:
            sid = f"mof.hojin.{measure}.allexfin-{size}.fy"
            if sid in existing:
                continue
            out.append(hojin_fy(measure, code, ttl, tags=tags, size=size))
    return out


# ---------------------------------------------------------------- 第6弾：業種別パネル Tier 1（全業種 62 × 全規模 × 11 項目）
# 要件＝粗利率・付加価値率のシフトシェア分解。売上原価・販管費は従来未登録。
# 人件費は構成 5 項目を個別登録（合成はモデル側＝DB は実測値のみ）。従業員賞与のみ表章が FY2007 から。
HOJIN_TIER1 = [  # (measure, cat01, 表示名, 単位, タグ, first_period の下限)
    ("sales", "045", "売上高", "百万円", (TAG_CORP, TAG_IND), "FY1960"),
    ("cogs", "046", "売上原価", "百万円", (TAG_CORP, TAG_IND), "FY1960"),
    ("sga", "047", "販売費及び一般管理費", "百万円", (TAG_CORP, TAG_IND), "FY1960"),
    ("operating_profit", "048", "営業利益", "百万円", (TAG_CORP, TAG_IND), "FY1960"),
    ("employee_wages", "066", "従業員給与", "百万円", (TAG_LABOR, TAG_CORP), "FY1960"),
    ("employee_bonus", "235", "従業員賞与", "百万円", (TAG_LABOR, TAG_CORP), "FY2007"),
    ("officer_compensation", "065", "役員給与", "百万円", (TAG_LABOR, TAG_CORP), "FY1960"),
    ("officer_bonus", "057", "役員賞与", "百万円", (TAG_LABOR, TAG_CORP), "FY1960"),
    ("welfare_costs", "067", "福利厚生費", "百万円", (TAG_LABOR, TAG_CORP), "FY1960"),
    ("depreciation", "062", "減価償却費", "百万円", (TAG_CORP, TAG_IND), "FY1960"),
    ("employees_avg", "072", "期中平均従業員数", "人", (TAG_LABOR, TAG_CORP), "FY1960"),
]


# e-Stat に存在しないセル（getStatsData が「該当データなし」）＝登録しない（unknown_series が正しい応答）。取込で判明したものを列挙。
HOJIN_EMPTY_CELLS = {("employee_bonus", "forestry_h20", "cap1b")}


# Tier 3（2026-08-21・前倒し）：付加価値の定義値と構成（合成せず e-Stat 定義で引けるように）＋資本・経常項目を全セルへ
HOJIN_TIER3 = [
    ("value_added", "073", "付加価値", "百万円", (TAG_CORP, TAG_IND), "FY1960"),
    ("operating_surplus", "074", "営業純益", "百万円", (TAG_CORP, TAG_IND), "FY1960"),
    ("interest_paid", "068", "支払利息等", "百万円", (TAG_CORP, TAG_FIN), "FY1960"),
    ("rent_paid", "069", "動産・不動産賃借料", "百万円", (TAG_CORP, TAG_IND), "FY1960"),
    ("taxes_dues", "070", "租税公課", "百万円", (TAG_CORP, TAG_FISCAL), "FY1960"),
    ("ordinary_profit", "051", "経常利益", "百万円", (TAG_CORP, TAG_IND), "FY1960"),
    ("tangible_fixed_assets", "148", "有形固定資産", "百万円", (TAG_CORP, TAG_FIN), "FY1960"),
    # 第 11 弾 第 2 便（2026-09-15・実利用ログ unknown_series）：無形固定資産（当期末・cat01=149）を全セルへ。内訳（ソフトウェア 016／ソフトウェアを除く 015）は未収録
    ("intangible_fixed_assets", "149", "無形固定資産", "百万円", (TAG_CORP, TAG_FIN), "FY1960"),
    ("capex_ex_software", "086", "設備投資（ソフトウェアを除く）", "百万円", (TAG_CORP, TAG_IND), "FY1961"),
    # 第 7 弾 B-1/B-2（2026-08-22・要件源＝利用側プロジェクト D-3）：資本装備率の分解に土地（控除）と BS の骨格を全セルへ
    ("land", "012", "土地（固定資産）", "百万円", (TAG_CORP, TAG_FIN), "FY1960"),
    ("fixed_assets", "147", "固定資産", "百万円", (TAG_CORP, TAG_FIN), "FY1960"),
    ("total_assets", "022", "資産合計", "百万円", (TAG_CORP, TAG_FIN), "FY1960"),
    ("net_assets", "157", "純資産", "百万円", (TAG_CORP, TAG_FIN), "FY1960"),
]


# 改善 3（2026-08-22）：セルの薄さを測る材料＝母集団法人数（cat01=001・社）。推計値＝集計値÷集計法人数×母集団法人数（財務省 推計値算出の方法）。
HOJIN_SAMPLE = [("population_count", "001", "母集団法人数", "社", (TAG_CORP, TAG_IND), "FY1960")]


def hojin_industry_panel(existing: set[str]) -> list[Series]:
    """Tier 1（全業種 62 × 全規模）＋ Tier 2（全業種 62 × 規模 4 区分＝10億以上／1億〜10億／1千万〜1億／1千万未満）× HOJIN_TIER1 の 11 項目。
    Tier 2 は 2026-08-21 に全業種で投入（要件＝サービス業への雇用移動が「中小の低生産性サービス業」への移動かを規模で割る）。
    既登録 series_id は作らない。first_period＝業種の開始年度・規模の開始年度（1千万円未満は FY1975）・項目の下限の最も遅いもの。"""
    out: list[Series] = []
    cells = [(slug, size) for slug, *_ in HOJIN_INDUSTRIES for size in HOJIN_SIZE]
    for ind, size in cells:
        _code, _name, ind_fy, _parent = HOJIN_IND_INFO[ind]
        ind_fy = max(ind_fy, SIZE_FIRST.get(size, "FY1960"))
        for measure, code, ttl, unit, tags, floor in HOJIN_TIER1 + HOJIN_TIER3 + HOJIN_SAMPLE:
            sid = f"mof.hojin.{measure}.{ind}-{size}.fy"
            if sid in existing or (measure, ind, size) in HOJIN_EMPTY_CELLS:
                continue
            note = "原数値。金額は百万円（換算しない）。" if unit == "百万円" else "原数値。"
            if measure == "population_count":
                note = ("母集団法人数（当期末・社）＝このセルの推計の母数（推計値＝集計値÷集計法人数×母集団法人数）。標本法人数そのものは時系列表に無い。"
                        "母集団が小さいセル（目安 100 社未満）は標本誤差が大きい＝quality.sample の根拠。")
            if measure == "employee_bonus":
                note += "従業員賞与の表章は FY2007 から（以前は従業員給与に含むとみられる）＝人件費を合成する側は FY2007 境界に注意。"
            if size == "cap1b" and measure in ACCOUNTING_AFFECTED:
                note += "収益認識基準（FY2021 強制）は資本金 10 億円以上で早期適用（FY2018〜）が多い＝規模間の比率比較では影響幅 FY2018〜FY2022 に注意。"
            if size != "allsize" and ind not in ("allexfin", "mfg", "nonmfg"):
                note += "業種×規模のセルは標本が小さく年々の振れが大きい（標本誤差）＝単年の比率より数年平均で見る。"
            if ind == "pure_holding":
                note += "上位集計（学術専門(集約)・サービス業(集約)・非製造業・全産業）から控除して実勢を見る：同じ期・同じ規模のこの系列を分子・分母の両方から引く（list_datasets の hojin analysis_notes.pure_holding）。"
            note = (note + " " + industry_note(ind)).strip()
            out.append(hojin_fy(measure, code, ttl, unit=unit, tags=tags, notes=note, ind=ind, size=size,
                                first_period=max(ind_fy, floor)))
    return out


def hojin_derived() -> list[Series]:
    """人件費・労働分配率＝派生（値を持たない）。構成系列を返して利用者に計算させる（捏造防止）。"""
    out: list[Series] = []
    for size in ("allsize", "cap1b", "cap100m-1b", "cap10m-100m"):
        sname = HOJIN_SIZE[size][1]
        dims = f"allexfin-{size}"
        base = dict(org="mof", org_name="財務省", source_url=ESTAT_URL.format(sid="0003060791"), sector="企業",
                    unit="", granularity="年度", freq="fy", dataset="hojin", dims=dims,
                    stat_name="法人企業統計調査 時系列データ", table_id="0003060791",
                    table_title="金融業、保険業以外の業種（原数値）年度次", first_period="FY1960", status="registered")
        parts = tuple(f"mof.hojin.{m}.{dims}.fy" for m in PERSONNEL_PARTS)
        out.append(_s(series_id=f"mof.hojin.personnel_costs.{dims}.fy", measure="personnel_costs",
                      title=f"法人企業統計 人件費（全産業（除く金融保険業）・{sname}・年度）＝派生・構成系列を参照",
                      policy_tags=(TAG_LABOR, TAG_CORP), components=parts,
                      notes="年次表に人件費の単独項目は無い。人件費＝役員給与＋役員賞与＋従業員給与＋従業員賞与＋福利厚生費"
                            "（構成系列を返す。派生値は計算しない）。賞与2項目は表章開始が遅く全期間では揃わないため、"
                            "全期間で必要なら 付加価値 −（支払利息等＋動産・不動産賃借料＋租税公課＋営業純益）で取る。" + HOJIN_SIZE_NOTE,
                      **base))
        out.append(_s(series_id=f"mof.hojin.labour_share.{dims}.fy", measure="labour_share",
                      title=f"法人企業統計 労働分配率（人件費／付加価値）（全産業（除く金融保険業）・{sname}・年度）＝派生・構成系列を参照",
                      policy_tags=(TAG_LABOR, TAG_CORP),
                      components=parts + (f"mof.hojin.value_added.{dims}.fy", f"mof.hojin.interest_paid.{dims}.fy",
                                          f"mof.hojin.rent_paid.{dims}.fy", f"mof.hojin.taxes_dues.{dims}.fy",
                                          f"mof.hojin.operating_surplus.{dims}.fy"),
                      notes="労働分配率＝人件費／付加価値。定義が複数あり（分母を営業純益ベースにする流儀もある）派生値は計算しない。"
                            "付加価値＝人件費＋支払利息等＋動産・不動産賃借料＋租税公課＋営業純益（法人企業統計の定義）。" + HOJIN_SIZE_NOTE,
                      **base))
    return out


def cpi(measure: str, item: str, title: str, freq: str, tab: str = "1", unit: str = "", notes: str = "",
        first_period: str = "", base: str = "2020") -> Series:
    """CPI の 1 系列（基準年ごとに別 dataset＝接続しない。品目コード（cat01）は 2020/2025 基準で共通）。"""
    sid = {"2020": "0003427113", "2025": "0004052037"}[base]
    other = ("2025年基準（e-Stat 0004052037・2026-08 公開・現行基準）は別系列（基準改定＝接続しない）。" if base == "2020"
             else "2020年基準（soumu.cpi2020.*）は別系列（基準改定＝接続しない）。総務省接続の遡及系列＝1970年から収録。" + CPI_REGION_NOTE)
    gran = {"m": "月次", "a": "暦年", "fy": "年度"}[freq]
    # 第 11 弾 第 3 便（2026-09-15）：2025 年基準は地域次元を持つ（region＝JP＋都道府県庁所在市等 52 市の JIS 5 桁）。
    # 実利用ログ＝「東京都区部 消費者物価指数」0 件・region=13 の lookup 失敗。cdArea を外して全地域を 1 表で取り、region_level=city で市コードを地域に写す。
    regional = base == "2025"
    acc = {"type": "estat", "statsDataId": sid, "cdTab": tab, "cdCat01": item}
    if not regional:
        acc["cdArea"] = "00000"
    return _s(
        series_id=f"soumu.cpi{base}.{measure}.{freq}", title=f"消費者物価指数（{base}年基準）{title}（{'全国・都市別' if regional else '全国'}・{gran}）",
        org="soumu", org_name="総務省", source_url=ESTAT_URL.format(sid=sid), sector="物価・金融",
        unit=unit or f"指数（{base}年=100）",
        granularity=gran, freq=freq, dataset=f"cpi{base}", measure=measure, basis=f"{base}年基準",
        region_level="city" if regional else "", region_codes=(("JP",) + tuple(c for c, _n in CPI_CITY_AREAS)) if regional else ("JP",),
        stat_name=f"{base}年基準消費者物価指数", table_id=sid, table_title=f"消費者物価指数（{base}年基準）",
        policy_tags=(TAG_MACRO, TAG_SOC), period_converter="estat_cpi_time",
        first_period=first_period or ("1970-01" if freq == "m" else "1970"),
        accessor=acc,
        citation_template=f"総務省「{base}年基準消費者物価指数」{'全国・都道府県庁所在市等' if regional else '全国'}（e-Stat {sid}・" + "{item}／{period}）取得 {retrieved_at}",
        superseded_by=(f"soumu.cpi2025.{measure}.{freq}" if base == "2020" else ""), notes=(notes + other).strip())


CPI_REGION_NOTE = ("地域＝JP（全国）＋都道府県庁所在市・政令指定都市 52 市（region＝JIS 市区町村コード 5 桁。例 13100 東京都区部・27100 大阪市・01100 札幌市。"
                   "一覧は list_datasets の cpi2025 analysis_notes.regions）。都道府県コード（13 等）や地方・都市階級の集計区分では引けない（都道府県別の指数は本表に無い）。"
                   "東京都区部は中旬速報のため全国より 1 か月早い期まで入る。相模原市 2015-01〜・川崎市/那覇市 1975-01〜・浜松市/堺市 2010-01〜。")


def roudou_m(measure: str, code: str, title: str) -> Series:
    sid = "0003005798"
    return _s(
        series_id=f"soumu.roudou.{measure}.total.m", title=f"労働力調査 {title}（男女計・全国・月次・原数値）",
        org="soumu", org_name="総務省", source_url=ESTAT_URL.format(sid=sid), sector="家計・労働", unit="万人",
        granularity="月次", freq="m", dataset="roudou", measure=measure, dims="total",
        stat_name="労働力調査 基本集計 全都道府県 全国 月次", table_id=sid, table_title="就業状態別15歳以上人口（2000年1月～）",
        policy_tags=(TAG_LABOR,), period_converter="estat_cpi_time", first_period="2000-01",
        accessor={"type": "estat", "statsDataId": sid, "cdCat01": "000", "cdCat02": code, "cdCat03": "0", "cdArea": "00000"},
        citation_template="総務省「労働力調査」基本集計 全国 月次 就業状態別15歳以上人口（e-Stat 0003005798・{item}／男女計／{period}）取得 {retrieved_at}",
        notes="原数値（季節調整値は別系列）。2011年3〜8月は補完推計値（公表注記）。")


def roudou_rate(freq: str) -> Series:
    """労働力調査 完全失業率（男女計・15歳以上・全国）。表章項目=率(02) × 就業状態=完全失業者(08) の交点。

    長期時系列は「年齢階級別労働力人口比率，就業率及び完全失業率」表にあり、月次 1968年1月〜／
    暦年 1953年〜／年度 1953年度〜。人数（万人）の表 0003005798（2000年〜）とは別表・別系列。"""
    sid, first, since = {
        "m": ("0002060004", "1968-01", "1968年1月～"),
        "a": ("0002060049", "1953", "1953年～"),
        "fy": ("0002060064", "FY1953", "1953年度～"),
    }[freq]
    gran = {"m": "月次", "a": "暦年", "fy": "年度"}[freq]
    tbl = f"年齢階級別労働力人口比率，就業率及び完全失業率（{since}）"
    return _s(
        series_id=f"soumu.roudou.unemployment_rate.total.{freq}",
        title=f"労働力調査 完全失業率（男女計・15歳以上・全国・{gran}・原数値）",
        org="soumu", org_name="総務省", source_url=ESTAT_URL.format(sid=sid), sector="家計・労働", unit="％",
        granularity=gran, freq=freq, dataset="roudou", measure="unemployment_rate", dims="total",
        stat_name="労働力調査 基本集計 全国", table_id=sid, table_title=tbl,
        policy_tags=(TAG_LABOR, TAG_MACRO), period_converter="estat_cpi_time", first_period=first,
        accessor={"type": "estat", "statsDataId": sid, "cdTab": "02", "cdCat01": "000", "cdCat02": "0",
                  "cdCat03": "08", "cdCat04": "00", "cdArea": "00000"},
        citation_template=f"総務省「労働力調査」基本集計 全国 {gran} {tbl}"
                          f"（e-Stat {sid}・完全失業率／男女計・15歳以上／" + "{period}）取得 {retrieved_at}",
        notes="公表値の完全失業率（％）＝完全失業者÷労働力人口の派生計算ではない。原数値（季節調整値は別系列）。"
              + ("2011年3〜8月は補完推計値（公表注記）。" if freq == "m" else ""))


def sna_gdp_fy(measure: str, table: str, title: str, unit: str, row_label: str, kindlabel: str,
               basis_extra: str = "", notes: str = "") -> Series:
    """国民経済計算 年次推計 主要系列表(1) 国内総生産（支出側）年度表（ffm1n／ffm1rn／ffm1dn）の1行＝1系列。"""
    return _s(
        series_id=f"cao.sna2020.{measure}.fy",
        title=f"国民経済計算（2020年基準・2008SNA）年次推計 {title}（年度）",
        org="cao", org_name="内閣府", source_url=ESRI_TOP, sector="マクロ", unit=unit,
        granularity="年度", freq="fy", dataset="sna2020", measure=measure,
        basis=f"2020年基準・2008SNA{basis_extra}", edition=f"{SNA_EDITION}年度年次推計（確報）",
        stat_name="国民経済計算年次推計", table_id=f"{SNA_EDITION}{table}",
        table_title=f"主要系列表(1) 国内総生産（支出側） {kindlabel} 年度",
        policy_tags=(TAG_MACRO,), period_converter="esri_year_fy", first_period="FY1994",
        accessor={"type": "esri_xlsx", "edition": SNA_EDITION,
                  "file": "kakuhou/files/{Y}/tables/{Y}" + f"{table}_jp.xlsx",
                  "sheet": "実数", "row_label": row_label, "header_row": 7, "first_col": 2},
        citation_template="内閣府「国民経済計算年次推計」主要系列表(1) 国内総生産（支出側）"
                          f" {kindlabel} 年度（{SNA_EDITION}年度確報・2020年基準・2008SNA・"
                          "{item}／{period}）取得 {retrieved_at}",
        notes=notes)


def planned(series_id: str, title: str, org: str, org_name: str, sector: str, unit: str, freq: str, granularity: str,
            dataset: str, measure: str, tags, accessor: dict, stat_name: str, url: str, dims: str = "",
            basis: str = "", region_level: str = "", region_codes=("JP",), kind: str = "observation",
            projection_by: str = "", edition: str = "", scenario: str = "", notes: str = "", table_id: str = "",
            table_title: str = "") -> Series:
    return _s(series_id=series_id, title=title, org=org, org_name=org_name, source_url=url, sector=sector, unit=unit,
              granularity=granularity, freq=freq, dataset=dataset, measure=measure, dims=dims, basis=basis,
              region_level=region_level, region_codes=tuple(region_codes), kind=kind, projection_by=projection_by,
              edition=edition, scenario=scenario, stat_name=stat_name, table_id=table_id, table_title=table_title,
              policy_tags=tuple(tags), accessor=accessor, status="planned", notes=notes or "取込未実装（取得元・アクセサ型は確定）。lookup は found=false。")


# 国別系列の地域集合（ISO3）。**取得元に実在するコードだけ**を並べる（2026-08-18 に各 API を実測して確定）。
# 集計値（OECD・EA20・EU27_2020）は ISO3 ではないので入れない（region=国コードの不変条件を崩さないため）。
OECD_MEMBERS = ("AUS", "AUT", "BEL", "CAN", "CHE", "CHL", "COL", "CRI", "CZE", "DEU", "DNK", "ESP", "EST", "FIN",
                "FRA", "GBR", "GRC", "HUN", "IRL", "ISL", "ISR", "ITA", "JPN", "KOR", "LTU", "LUX", "LVA", "MEX",
                "NLD", "NOR", "NZL", "POL", "PRT", "SVK", "SVN", "SWE", "TUR", "USA")          # OECD 加盟38か国
NON_OECD_MAJOR = ("ARG", "BRA", "CHN", "IDN", "IND", "RUS", "SAU", "ZAF")                      # 主要非加盟8か国
CTY_WORLD = tuple(sorted(OECD_MEMBERS + NON_OECD_MAJOR))                 # IMF WEO・世銀 WDI（ともに全世界を収録）
CTY_OECD_PDB = tuple(sorted(OECD_MEMBERS + ("BGR", "HRV", "ROU")))       # OECD 生産性DB（非加盟のEU3か国を収録・中国はなし）
CTY_OECD_WAGE = OECD_MEMBERS                                             # OECD 平均賃金（加盟国のみ）
G9 = ("JPN", "USA", "DEU", "FRA", "GBR", "ITA", "CAN", "CHN", "KOR")     # G7＋中韓（既定値の後方互換）
INTL_NOTE = ("国際比較用（定義・集計方法は各機関のもの）。日本の国内分析は総務省・内閣府等の国内統計系列を使い、"
             "本系列と混ぜない（同じ指標名でも一致しないことがある）。")


def _intl(series_id, title, org, org_name, sector, unit, dataset, measure, tags, accessor, stat_name, url,
          notes="", first_period="", region_codes=CTY_WORLD, kind="observation", edition=""):
    return _s(series_id=series_id, title=title, org=org, org_name=org_name, source_url=url, sector=sector, unit=unit,
              granularity="暦年", freq="a", dataset=dataset, measure=measure, region_level="cty", region_codes=tuple(region_codes),
              kind=kind, edition=edition, stat_name=stat_name, policy_tags=tuple(tags), accessor=accessor,
              first_period=first_period, notes=(notes + " " if notes else "") + INTL_NOTE, status="registered")


# ---------------------------------------------------------------- 第 7 弾 B-3（2026-08-22）：OECD SDBS 構造的企業統計（規模階級別・ISIC Rev.4）
# 要件源＝利用側プロジェクト D-2/D-6（企業数の国際参照点。「社／兆円GDP」に他国の水準を与える）。
# 原典実測（2026-08-22・sdmx.oecd.org）：日本を含む（原典＝経済センサス・2011/2016/2021）。規模区分は**従業者数**＝法人企業統計の資本金区分とは別軸。
# 活動の集計は国により出し方が違う：_T（全活動）は 9 か国のみ、BTNXK（企業部門 B–N・金融保険を除く）は 43 か国（日米独仏英伊を含む）。
# 付加価値は欧州＝VAFC（要素費用）・日米豪等＝VALU（基本価格）で**定義が違う＝別 measure**（混ぜない・換算しない）。
SDBS_DF = "OECD.SDD.TPS,DSD_SDBSBSC_ISIC4@DF_SDBS_ISIC4,1.0"
SDBS_MEASURE = [  # (measure, MEASURE コード, 表示名, 単位, タグ, 注記)
    ("enterprises", "ENTR", "企業数", "社", (TAG_IND, TAG_SME), ""),
    ("persons_employed", "EMPN", "従業者数（persons employed）", "人", (TAG_LABOR, TAG_IND), ""),
    ("value_added_factor_cost", "VAFC", "付加価値（要素費用表示）", "自国通貨（百万）", (TAG_IND,),
     "主に欧州（Eurostat SBS 由来）。日本・米国等は基本価格表示の value_added_basic_prices＝定義が違うので混ぜない。"),
    ("value_added_basic_prices", "VALU", "付加価値（基本価格表示）", "自国通貨（百万）", (TAG_IND,),
     "日本（経済センサス）・米・豪・英等。欧州は要素費用表示の value_added_factor_cost＝定義が違うので混ぜない。"),
]
CTY_SDBS = tuple(sorted(set(OECD_MEMBERS) | {"ALB", "ARM", "BIH", "BGR", "BRA", "CYP", "HRV", "MKD", "MLT", "ROU", "SRB"}))  # 原典に実在する ISO3（2026-08-22 実測）
SDBS_NOTE = ("OECD Structural business statistics（ISIC Rev.4・従業者規模階級別）。各国の事業所・企業統計を OECD が収集（日本＝経済センサス 2011/2016/2021・"
             "米国＝Census SUSB・欧州＝Eurostat SBS）。**規模区分は従業者数**＝法人企業統計（資本金区分）と直接つながらない。"
             "日本の企業数は法人企業統計の母集団法人数（mof.hojin.population_count）とも定義が違う（個人事業所を含む・事業所ベースの集計）。"
             "国により収録年・活動の集計範囲が違う（値が無い年は found=false）。")


# 取得元に存在しない組合せ（SDMX が 404 NoResultsFound）＝登録しない（unknown_series が正しい応答）。取込で判明したものを列挙（2026-08-22 実測）。
# 要素費用表示の付加価値（VAFC＝主に欧州）は「全活動（_T）」を出す国が無い（_T を出す 9 か国はいずれも基本価格表示 VALU）。
# ただし total-emp10_19 だけは値がある（9 値）ので一律には外さない。
SDBS_EMPTY_CELLS = {("value_added_factor_cost", "total", sz) for sz in ("allsize", "emp1_9", "emp20_49", "emp50_249", "emp250plus")}


# ---------------------------------------------------------------- 段 D-(1)（2026-08-23）：OECD STAN 業種別（付加価値・雇用者報酬・雇用）
# 要件源＝利用側プロジェクト D-4。原典実測（2026-08-23）：STAN 2025 edition・日米独仏英伊韓で 36 活動 × 1994〜2024/25 が揃う。日本の値＝JSNA 付表 2 と一致。
STAN_DF = "OECD.STI.PIE,DSD_STAN@DF_STAN_2025,1.0"
STAN_MEASURE = [  # (measure, MEASURE, PRICE_BASE, UNIT_MEASURE, 表示名, 単位, タグ)
    ("value_added", "B1G", "V", "XDC", "付加価値（総・名目）", "自国通貨（百万）", (TAG_IND, TAG_MACRO)),
    ("compensation_employees", "D1", "V", "XDC", "雇用者報酬（名目）", "自国通貨（百万）", (TAG_LABOR, TAG_IND)),
    ("employment", "EMP", "_Z", "PS", "就業者数", "千人", (TAG_LABOR, TAG_IND)),
    ("employees", "SAL", "_Z", "PS", "雇用者数", "千人", (TAG_LABOR, TAG_IND)),
]
STAN_NOTE = ("OECD STAN（Structural Analysis Database・ISIC Rev.4・2025 年版）。各国の国民経済計算（日本＝JSNA 付表 2）に OECD が SUT・SBS 等で推計を足して埋めた表＝"
             "status が『E=推計値』のセルは OECD 推計（accessor.EST_METHOD に推計法）。自国通貨（百万）・雇用は千人（persons）。"
             "分類の対応（ISIC ↔ JSNA）と分析の定石は list_datasets の stan の analysis_notes。")


def oecd_stan_series() -> list[Series]:
    acts = "+".join(code for _s, code, _n, _m in STAN_ACTIVITY)
    out: list[Series] = []
    for meas, mcode, price, unit_code, mttl, unit, tags in STAN_MEASURE:
        key = f"A..{acts}.{mcode}.{price}.{unit_code}"  # measure ごとに 1 本＝36 活動の系列で共有（ingest は URL をメモ化）
        for slug, acode, aname, mapping in STAN_ACTIVITY:
            out.append(_s(
                series_id=f"oecd.stan.{meas}.{slug}.a.cty",
                title=f"OECD STAN {mttl}（{aname}・国別・暦年）", org="oecd", org_name="OECD",
                source_url="https://data-explorer.oecd.org/", sector="対外・国際", unit=unit, granularity="暦年", freq="a",
                dataset="stan", measure=meas, dims=slug, region_level="cty", region_codes=OECD_MEMBERS,  # STAN は加盟 38 か国（中国・インド等は無い＝実測）
                stat_name="STAN Database for Structural Analysis, 2025 edition", table_id="DF_STAN_2025",
                table_title="STAN industrial analysis (ISIC Rev.4)", policy_tags=tuple(tags), edition="2025 edition",
                accessor={"type": "oecd_sdmx", "dataflow": STAN_DF, "startPeriod": "1970", "key": key,
                          "filter": {"MEASURE": mcode, "ACTIVITY": acode, "PRICE_BASE": price, "UNIT_MEASURE": unit_code}},
                first_period="1970", status="registered",
                notes=f"ISIC {acode}。JSNA 付表 2 との対応＝{mapping}。" + STAN_NOTE + " " + INTL_NOTE))
    return out


def oecd_sdbs_series() -> list[Series]:
    out: list[Series] = []
    for meas, mcode, mttl, unit, tags, mnote in SDBS_MEASURE:
        for aslug, acode, attl in SDBS_ACTIVITY:
            for sslug, scode, sttl in SDBS_SIZE:
                if (meas, aslug, sslug) in SDBS_EMPTY_CELLS:
                    continue
                out.append(_s(
                    series_id=f"oecd.sdbs.{meas}.{aslug}-{sslug}.a.cty",
                    title=f"OECD 構造的企業統計 {mttl}（{attl}・{sttl}・国別・暦年）", org="oecd", org_name="OECD",
                    source_url="https://data-explorer.oecd.org/", sector="対外・国際", unit=unit, granularity="暦年", freq="a",
                    dataset="sdbs", measure=meas, dims=f"{aslug}-{sslug}", region_level="cty", region_codes=CTY_SDBS,
                    stat_name="Structural business statistics by size class and economic activity (ISIC Rev. 4)", table_id="DF_SDBS_ISIC4",
                    table_title="SDBS Business statistics by size class and economic activity", policy_tags=tuple(tags),
                    accessor={"type": "oecd_sdmx", "dataflow": SDBS_DF, "startPeriod": "2000",
                              "key": f"A..{mcode}.{acode}.{scode}.",
                              "filter": {"MEASURE": mcode, "ACTIVITY": acode, "SIZE_CLASS": scode}},
                    first_period="2005", status="registered",
                    notes=(mnote + " " if mnote else "") + SDBS_NOTE + " " + INTL_NOTE))
    return out


def intl_series() -> list[Series]:
    out: list[Series] = []
    # IMF WEO（DataMapper）。予測年は値単位で kind=projection（版年以降）。
    weo_url = "https://www.imf.org/external/datamapper/{ind}@WEO"
    for meas, ind, ttl, unit, tags in [
        ("gdp_real_growth", "NGDP_RPCH", "実質GDP成長率", "％（前年比）", (TAG_MACRO, TAG_TRADE)),
        ("gdp_nominal_usd", "NGDPD", "名目GDP（米ドル）", "10億米ドル", (TAG_MACRO, TAG_TRADE)),
        ("gdp_per_capita_usd", "NGDPDPC", "一人当たり名目GDP（米ドル）", "米ドル", (TAG_MACRO, TAG_TRADE)),
        ("gdp_per_capita_ppp", "PPPPC", "一人当たりGDP（購買力平価・国際ドル）", "国際ドル（PPP）", (TAG_MACRO, TAG_TRADE)),
        ("gdp_ppp", "PPPGDP", "GDP（購買力平価・国際ドル）", "10億国際ドル（PPP）", (TAG_MACRO, TAG_TRADE)),
        ("inflation", "PCPIPCH", "インフレ率（消費者物価・年平均）", "％（前年比）", (TAG_MACRO,)),
        ("unemployment_rate", "LUR", "失業率", "％", (TAG_LABOR, TAG_MACRO)),
        ("current_account_gdp", "BCA_NGDPD", "経常収支 対GDP比", "％", (TAG_TRADE, TAG_MACRO)),
        ("gov_debt_gdp", "GGXWDG_NGDP", "一般政府 債務残高 対GDP比", "％", (TAG_FISCAL, TAG_MACRO)),
        ("gov_balance_gdp", "GGXCNL_NGDP", "一般政府 財政収支（純貸出/純借入）対GDP比", "％", (TAG_FISCAL, TAG_MACRO)),
        ("population", "LP", "人口", "百万人", (TAG_POP,)),
    ]:
        out.append(_intl(f"imf.weo.{meas}.a.cty", f"IMF World Economic Outlook {ttl}（国別・暦年・最新版）", "imf", "IMF",
                         "対外・国際", unit, "weo", meas, tags, {"type": "imf_dm", "indicator": ind},
                         "World Economic Outlook（DataMapper）", weo_url.format(ind=ind), first_period="1980",
                         notes="最新版のみ保持（版名は vintage/edition）。版年以降の年は予測値（kind=projection）。版年直前の年も IMF スタッフ推計が混じり得る。"))
    # OECD
    pdb = "OECD.SDD.TPS,DSD_PDB@DF_PDB,"
    out.append(_intl("oecd.pdb.labour_productivity_level.a.cty", "OECD 時間当たり労働生産性（GDP/総労働時間・米ドル PPP・当年価格）", "oecd", "OECD",
                     "対外・国際", "米ドル/時間（PPP・当年価格）", "pdb", "labour_productivity_level", (TAG_IND, TAG_LABOR),
                     {"type": "oecd_sdmx", "dataflow": pdb, "startPeriod": "1970",
                      "filter": {"MEASURE": "GDPHRS", "UNIT_MEASURE": "USD_PPP_H", "PRICE_BASE": "V", "TRANSFORMATION": "N", "ACTIVITY": "_T"}},
                     "Productivity database", "https://data-explorer.oecd.org/", first_period="1970",
                     notes="OBS_VALUE の全桁を保持（Data Explorer の表示は丸め）。加盟38か国＋非加盟のEU3か国（中国は収録なし）。",
                     region_codes=CTY_OECD_PDB))
    out.append(_intl("oecd.pdb.labour_productivity_growth.a.cty", "OECD 時間当たり労働生産性 伸び率（実質・自国通貨・前年比）", "oecd", "OECD",
                     "対外・国際", "％（前年比）", "pdb", "labour_productivity_growth", (TAG_IND, TAG_LABOR),
                     {"type": "oecd_sdmx", "dataflow": pdb, "startPeriod": "1970",
                      "filter": {"MEASURE": "GDPHRS", "UNIT_MEASURE": "XDC_H", "PRICE_BASE": "LR", "TRANSFORMATION": "GY", "ACTIVITY": "_T"}},
                     "Productivity database", "https://data-explorer.oecd.org/", first_period="1971",
                     notes="OBS_VALUE の全桁を保持（Data Explorer の表示は丸め）。加盟38か国＋非加盟のEU3か国（中国は収録なし）。",
                     region_codes=CTY_OECD_PDB))
    # 成長会計（2026-08-23・要件源＝「国別の成長の分解（労働・資本・生産性）」）：同じ dataflow（取得は URL メモ化で 1 本）。
    # **実績成長（GVA 成長率）の寄与分解**であって潜在成長率の分解ではない（日本の潜在は boj.gap.potential_growth_*）。MFP は残差＝OECD の推計。
    GA_NOTE = ("OECD 生産性 DB の成長会計（全活動）。**実績の GVA 成長（連鎖実質・前年比）の寄与度分解**＝労働投入（時間）＋資本サービス＋MFP ≒ GVA 成長率。"
               "潜在成長率の分解ではない（日本の潜在成長率の寄与分解は boj.gap.potential_growth_{employment,hours,capital,tfp}.h＝日銀・半期）。"
               "MFP（多要素生産性）は残差で OECD の推計値＝実測ではない。収録は成長会計を OECD が計算している 21 か国（G7 を含む）。")
    for meas, code, ttl, unit, filt, note in [
        ("gva_growth_contrib_hours", "HRSTO_PCCONG", "GVA 成長への労働投入（労働時間）の寄与", "％ポイント",
         {"MEASURE": "HRSTO_PCCONG", "ACTIVITY": "_T", "TRANSFORMATION": "GY", "ASSET_CODE": "_Z"}, ""),
        ("gva_growth_contrib_capital", "KSER_PCCONG", "GVA 成長への資本サービスの寄与（全資産）", "％ポイント",
         {"MEASURE": "KSER_PCCONG", "ACTIVITY": "_T", "TRANSFORMATION": "GY", "ASSET_CODE": "_T"}, "ICT／非 ICT の内訳は gva_growth_contrib_capital_ict／_nonict。"),
        ("gva_growth_contrib_capital_ict", "KSER_PCCONG", "GVA 成長への資本サービスの寄与（ICT 資産）", "％ポイント",
         {"MEASURE": "KSER_PCCONG", "ACTIVITY": "_T", "TRANSFORMATION": "GY", "ASSET_CODE": "ICT"}, ""),
        ("gva_growth_contrib_capital_nonict", "KSER_PCCONG", "GVA 成長への資本サービスの寄与（非 ICT 資産）", "％ポイント",
         {"MEASURE": "KSER_PCCONG", "ACTIVITY": "_T", "TRANSFORMATION": "GY", "ASSET_CODE": "NICT"}, ""),
        ("gva_growth_contrib_mfp", "MFP_PCCONG", "GVA 成長への多要素生産性（MFP）の寄与（時間ベース）", "％ポイント",
         {"MEASURE": "MFP_PCCONG", "ACTIVITY": "_T", "TRANSFORMATION": "GY", "ASSET_CODE": "_T"}, ""),
        ("mfp_growth", "MFPH", "多要素生産性（MFP・時間ベース）伸び率", "％（前年比）",
         {"MEASURE": "MFPH", "ACTIVITY": "_T", "TRANSFORMATION": "GY", "ASSET_CODE": "_T"}, ""),
        ("lp_growth_contrib_capital_deepening", "KSERHRS_PCCONLP", "労働生産性（時間当たり）成長への資本深化の寄与", "％ポイント",
         {"MEASURE": "KSERHRS_PCCONLP", "ACTIVITY": "_T", "TRANSFORMATION": "GY", "ASSET_CODE": "_T"}, "労働生産性成長 ≒ 資本深化の寄与＋MFP の寄与（lp_growth_contrib_mfp）。"),
        ("lp_growth_contrib_mfp", "MFP_PCCONLP", "労働生産性（時間当たり）成長への MFP の寄与", "％ポイント",
         {"MEASURE": "MFP_PCCONLP", "ACTIVITY": "_T", "TRANSFORMATION": "GY", "ASSET_CODE": "_T"}, ""),
    ]:
        out.append(_intl(f"oecd.pdb.{meas}.a.cty", f"OECD 成長会計 {ttl}（国別・暦年）", "oecd", "OECD", "対外・国際", unit, "pdb", meas,
                         (TAG_MACRO, TAG_IND), {"type": "oecd_sdmx", "dataflow": pdb, "startPeriod": "1970", "filter": filt},
                         "Productivity database", "https://data-explorer.oecd.org/", first_period="1986",
                         notes=(note + " " if note else "") + GA_NOTE, region_codes=CTY_OECD_PDB))
    # 一人当たり／時間当たりの橋渡し（2026-08-23・利用側＝必要条件は「一人当たり」、OECD の生産性は「時間当たり」＝労働時間の趨勢を経由しないと過小評価）
    PW_NOTE = ("一人当たり（就業者 1 人当たり）の伸び ＝ 時間当たりの伸び ＋ 就業者 1 人当たり労働時間の伸び（恒等式）。"
               "OECD の労働時間は**就業者**（自営業を含む）1 人当たり＝JSNA 付表 3 の**雇用者** 1 人当たり（cao.sna_activity.hours_worked_per_employee）とは母数が違う（日本 2023：OECD 1,610.7 時間／JSNA 雇用者 1,661.5 時間）。")
    for meas, ttl, unit, filt in [
        ("hours_per_worker", "就業者 1 人当たり年間労働時間", "時間／人／年", {"MEASURE": "HRSAV", "UNIT_MEASURE": "H_PS", "TRANSFORMATION": "N", "ACTIVITY": "_T"}),
        ("hours_per_worker_growth", "就業者 1 人当たり年間労働時間 伸び率", "％（前年比）", {"MEASURE": "HRSAV", "UNIT_MEASURE": "H_PS", "TRANSFORMATION": "GY", "ACTIVITY": "_T"}),
        ("gdp_per_worker_growth", "就業者 1 人当たり GDP 伸び率（実質・自国通貨・前年比）", "％（前年比）", {"MEASURE": "GDPEMP", "UNIT_MEASURE": "XDC_PS", "PRICE_BASE": "LR", "TRANSFORMATION": "GY", "ACTIVITY": "_T"}),
        ("gdp_per_worker_level_ppp", "就業者 1 人当たり GDP（米ドル PPP・当年価格）", "米ドル／人（PPP・当年価格）", {"MEASURE": "GDPEMP", "UNIT_MEASURE": "USD_PPP_PS", "PRICE_BASE": "V", "TRANSFORMATION": "N", "ACTIVITY": "_T"}),
    ]:
        out.append(_intl(f"oecd.pdb.{meas}.a.cty", f"OECD {ttl}（国別・暦年）", "oecd", "OECD", "対外・国際", unit, "pdb", meas, (TAG_LABOR, TAG_MACRO),
                         {"type": "oecd_sdmx", "dataflow": pdb, "startPeriod": "1970", "filter": filt}, "Productivity database", "https://data-explorer.oecd.org/",
                         first_period="1970", notes=PW_NOTE + " OBS_VALUE の全桁を保持。", region_codes=CTY_OECD_PDB))
    wage = "OECD.ELS.SAE,DSD_EARNINGS@AV_AN_WAGE,"
    out.append(_intl("oecd.earnings.avg_wage_usd_ppp_const.a.cty", "OECD 平均年間賃金（米ドル PPP・実質＝不変価格）", "oecd", "OECD",
                     "対外・国際", "米ドル（PPP・不変価格）", "earnings", "avg_wage_usd_ppp_const", (TAG_LABOR,),
                     {"type": "oecd_sdmx", "dataflow": wage, "startPeriod": "1990",
                      "filter": {"MEASURE": "WG", "UNIT_MEASURE": "USD_PPP", "PRICE_BASE": "Q", "AGGREGATION_OPERATION": "MEAN"}},
                     "Average annual wages", "https://data-explorer.oecd.org/", first_period="1990",
                     notes="基準年（BASE_PER）は OECD の版に依存。加盟国のみ（中国は収録なし）。", region_codes=CTY_OECD_WAGE))
    out.append(_intl("oecd.earnings.avg_wage_lcu_current.a.cty", "OECD 平均年間賃金（自国通貨・名目）", "oecd", "OECD",
                     "対外・国際", "自国通貨（名目）", "earnings", "avg_wage_lcu_current", (TAG_LABOR,),
                     {"type": "oecd_sdmx", "dataflow": wage, "startPeriod": "1990",
                      "filter": {"MEASURE": "WG", "PRICE_BASE": "V", "AGGREGATION_OPERATION": "MEAN"}},
                     "Average annual wages", "https://data-explorer.oecd.org/", first_period="1990",
                     notes="単位は国ごとの自国通貨（JPY/USD/EUR…）。加盟国のみ（中国は収録なし）。", region_codes=CTY_OECD_WAGE))
    out.extend(oecd_sdbs_series())
    out.extend(oecd_stan_series())
    # 世銀 WDI
    for meas, ind, ttl, unit, tags in [
        ("population", "SP.POP.TOTL", "人口（総数）", "人", (TAG_POP,)),
        ("fertility_rate", "SP.DYN.TFRT.IN", "合計特殊出生率", "人", (TAG_POP,)),
        ("market_cap_gdp", "CM.MKT.LCAP.GD.ZS", "上場国内企業 株式時価総額 対GDP比", "％", (TAG_FIN,)),
        ("gdp_usd", "NY.GDP.MKTP.CD", "GDP（現行米ドル）", "米ドル", (TAG_MACRO, TAG_TRADE)),
    ]:
        out.append(_intl(f"wb.wdi.{meas}.a.cty", f"世界銀行 WDI {ttl}（国別・暦年）", "wb", "世界銀行", "対外・国際", unit, "wdi", meas, tags,
                         {"type": "wb_api", "indicator": ind}, "World Development Indicators",
                         f"https://data.worldbank.org/indicator/{ind}", first_period="1960"))
    # 世銀 WDI：GDP 換算行列＝3換算（PPP／米ドル／自国通貨）×2価格（名目=当年価格／実質=不変価格）×総額・一人当たり。
    # 命名は「素＝名目（current）・_real＝実質（constant）」。gdp_usd（NY.GDP.MKTP.CD）が名目USD総額の枡（既存 ID を維持）。
    # 基準年は世銀 API の指標名から確定（2026-08-18 実測）：米ドル不変=2015年・PPP不変=2021年・自国通貨不変は国ごと（世銀は基準年を明示しない）。
    GDP_MATRIX = [
        ("gdp_usd_real", "NY.GDP.MKTP.KD", "GDP（実質・2015年不変価格 米ドル）", "米ドル（2015年不変価格）"),
        ("gdp_ppp", "NY.GDP.MKTP.PP.CD", "GDP（購買力平価・当年価格 国際ドル）", "国際ドル（PPP・当年価格）"),
        ("gdp_ppp_real", "NY.GDP.MKTP.PP.KD", "GDP（購買力平価・実質＝2021年不変価格 国際ドル）", "国際ドル（PPP・2021年不変価格）"),
        ("gdp_lcu", "NY.GDP.MKTP.CN", "GDP（名目・自国通貨）", "自国通貨（名目）"),
        ("gdp_lcu_real", "NY.GDP.MKTP.KN", "GDP（実質・自国通貨 不変価格）", "自国通貨（不変価格）"),
        ("gdp_pc_usd", "NY.GDP.PCAP.CD", "一人当たりGDP（現行米ドル）", "米ドル"),
        ("gdp_pc_usd_real", "NY.GDP.PCAP.KD", "一人当たりGDP（実質・2015年不変価格 米ドル）", "米ドル（2015年不変価格）"),
        ("gdp_pc_ppp", "NY.GDP.PCAP.PP.CD", "一人当たりGDP（購買力平価・当年価格 国際ドル）", "国際ドル（PPP・当年価格）"),
        ("gdp_pc_ppp_real", "NY.GDP.PCAP.PP.KD", "一人当たりGDP（購買力平価・実質＝2021年不変価格 国際ドル）", "国際ドル（PPP・2021年不変価格）"),
        ("gdp_pc_lcu", "NY.GDP.PCAP.CN", "一人当たりGDP（名目・自国通貨）", "自国通貨（名目）"),
        ("gdp_pc_lcu_real", "NY.GDP.PCAP.KN", "一人当たりGDP（実質・自国通貨 不変価格）", "自国通貨（不変価格）"),
    ]
    matrix_note = ("GDP 換算行列（3換算×2価格×総額/一人当たり）の1枡。**換算と価格を混ぜて比較しない**："
                   "水準の国際比較は PPP、時系列の伸びは実質（不変価格）、両方要るなら gdp_ppp_real／gdp_pc_ppp_real。"
                   "名目米ドルは為替変動を、当年価格 PPP はインフレをそのまま拾う。"
                   "自国通貨建ては国をまたいで比較できない（単位が国ごとに異なる＝JPY/USD/EUR…）。"
                   "不変価格の基準年は世銀の版に依存（米ドル=2015年・PPP=2021年・自国通貨は国ごとで API は明示しない）。")
    for meas, ind, ttl, unit in GDP_MATRIX:
        out.append(_intl(f"wb.wdi.{meas}.a.cty", f"世界銀行 WDI {ttl}（国別・暦年）", "wb", "世界銀行", "対外・国際", unit, "wdi", meas,
                         (TAG_MACRO, TAG_TRADE), {"type": "wb_api", "indicator": ind}, "World Development Indicators",
                         f"https://data.worldbank.org/indicator/{ind}", first_period="1960", notes=matrix_note))
    return out


# ---------------------------------------------------------------- 第 7 弾 段 C（2026-08-22）：SNA 経済活動別（付表 2・付表 3）
# 要件源＝利用側プロジェクト D-1（制度部門別の付加価値）。**JSNA は制度部門別の付加価値を公表していない**（所得の発生勘定 i11 は一国経済のみ・
# 部門別 i2〜i6 は第 1 次所得の配分勘定から＝原典で確認 2026-08-22）。代わりに付表 2 が経済活動 17 分類 × 9 項目＋（参考）制度部門別 3 区分
# （市場生産者・一般政府・対家計民間非営利団体）を持つ＝κ（法人付加価値／GDP）の分解にはこちらが直接効く。暦年のみ（年度へ換算しない）。
SNA_S3_TOTAL = [("total", "合計", "合計", None)]  # 付表 3 は '合計' が 2 行（経済活動計＋制度部門計・同値）＝occurrence=1
SNA_S2N_ITEMS = [  # (measure, 列見出し（header_rows 5+6 を連結・空白除去）, 表示名, タグ)
    ("output", "産出額（生産者価格表示）", "産出額（生産者価格表示）", (TAG_MACRO, TAG_IND)),
    ("intermediate_input", "中間投入", "中間投入", (TAG_MACRO, TAG_IND)),
    ("gdp", "国内総生産（生産者価格表示）", "国内総生産（生産者価格表示）", (TAG_MACRO, TAG_IND)),
    ("cfc", "固定資本減耗", "固定資本減耗", (TAG_MACRO, TAG_IND)),
    ("ndp", "国内純生産（生産者価格表示）", "国内純生産（生産者価格表示）", (TAG_MACRO, TAG_IND)),
    ("taxes_less_subsidies", "生産・輸入品に課される税（控除）補助金", "生産・輸入品に課される税（控除）補助金", (TAG_MACRO, TAG_FISCAL)),
    ("domestic_factor_income", "国内要素所得", "国内要素所得", (TAG_MACRO, TAG_IND)),
    ("compensation_employees", "雇用者報酬", "雇用者報酬", (TAG_MACRO, TAG_LABOR)),
    ("operating_surplus_mixed_income", "営業余剰・混合所得", "営業余剰・混合所得", (TAG_MACRO, TAG_IND)),
]
from stats.core.dataset_notes import SNA_ACTIVITY_NOTES  # noqa: E402  分析の定石は core（目録にも載せる）
from stats.core.dim_vocab import CPI_CITY_AREAS, SDBS_ACTIVITY, SDBS_SIZE, SNA_ACTIVITY, SNA_ACTIVITY_S2_EXTRA, STAN_ACTIVITY  # noqa: E402  dims の語彙は core（families の表示名）


def _sna_activity(measure: str, slug: str, rlabel: str, aname: str, *, table: str, title: str, unit: str, tags, freq: str,
                  accessor: dict, conv: str, kind_label: str, notes: str, first_period: str) -> Series:
    gran = {"fy": "年度", "a": "暦年"}[freq]
    return _s(series_id=f"cao.sna_activity.{measure}.{slug}.{freq}",
              title=f"国民経済計算（2020年基準・2008SNA）年次推計 経済活動別 {title}（{aname}・{gran}）",
              org="cao", org_name="内閣府", source_url=ESRI_TOP, sector="マクロ", unit=unit, granularity=gran, freq=freq,
              dataset="sna_activity", measure=measure, dims=slug, basis="2020年基準・2008SNA", edition=f"{SNA_EDITION}年度年次推計（確報）",
              stat_name="国民経済計算年次推計", table_id=f"{SNA_EDITION}{table}", table_title=kind_label,
              policy_tags=tuple(tags), period_converter=conv, first_period=first_period,
              accessor={"edition": SNA_EDITION, "file": "kakuhou/files/{Y}/tables/{Y}" + f"{table}_jp.xlsx", "row_label": rlabel, **accessor},
              status="registered",
              citation_template=f"内閣府「国民経済計算年次推計」{kind_label}（{SNA_EDITION}年度確報・2020年基準・2008SNA・" + "{item}／{period}）取得 {retrieved_at}",
              notes=notes)


def sna_activity_series() -> list[Series]:
    out: list[Series] = []
    note_s2 = ("付表 2「経済活動別の国内総生産・要素所得」。10 億円。暦年のみ。経済活動＝産業分類であって制度部門ではない。"
               "制度部門別の付加価値（非金融法人・金融・政府・家計）は JSNA 非公表＝（参考）市場生産者／一般政府／対家計民間非営利団体の 3 区分まで。"
               "分析の定石は list_datasets の sna_activity の analysis_notes。")
    # 付表 2 名目（年シート×項目列）
    for meas, col, ttl, tags in SNA_S2N_ITEMS:
        for slug, rlabel, aname, _parent in SNA_ACTIVITY + SNA_ACTIVITY_S2_EXTRA:
            if meas in ("output", "intermediate_input") and slug in ("import_taxes", "vat_on_capital_formation", "total"):
                continue  # 原典で空欄（産出額・中間投入は税・合計行に定義されない）＝登録しない（unknown_series が正しい応答）
            out.append(_sna_activity(meas, slug, rlabel, aname, table="s2n", title=f"{ttl}（名目）", unit="10億円", tags=tags, freq="a",
                                     accessor={"type": "esri_xlsx_yearsheets", "year_cell": "B4", "header_rows": [5, 6], "col_header": col},
                                     conv="esri_year_paren", kind_label="付表(2) 経済活動別の国内総生産・要素所得 名目", notes=note_s2, first_period="1994"))
    # 付表 2 実質（1 シート・年ブロック×3 項目の二段見出し）。**原典は数量指数（2020暦年=100・連鎖方式）であって実質額ではない**（2026-08-22 原本確認）
    for meas, sub, ttl in [("output_real_index", "産出額", "産出額 数量指数（実質・連鎖方式）"), ("intermediate_input_real_index", "中間投入", "中間投入 数量指数（実質・連鎖方式）"),
                           ("gdp_real_index", "国内総生産", "国内総生産 数量指数（実質・連鎖方式）")]:
        for slug, rlabel, aname, _parent in SNA_ACTIVITY:
            out.append(_sna_activity(meas, slug, rlabel, aname, table="s2rn", title=ttl, unit="2020暦年=100", tags=(TAG_MACRO, TAG_IND), freq="a",
                                     accessor={"type": "esri_xlsx", "sheet": "実質(連鎖方式)", "header_row": 5, "sub_header_row": 6, "sub_header": sub, "first_col": 2},
                                     conv="esri_year_paren", kind_label="付表(2) 経済活動別の国内総生産・要素所得 実質",
                                     notes=note_s2 + " 実質は連鎖方式の数量指数（2020 暦年=100）＝金額ではない。実質額が要るなら名目÷デフレーター×100 を利用側で計算する（stats は換算しない）。", first_period="1994"))
    # 付表 2 デフレーター（3 シート・行×年列）
    for meas, sheet, ttl in [("output_deflator", "産出デフレーター(連鎖方式)", "産出デフレーター"), ("intermediate_input_deflator", "中間投入デフレーター(連鎖方式)", "中間投入デフレーター"),
                             ("gdp_deflator", "国内総生産デフレーター(連鎖方式)", "国内総生産デフレーター")]:
        for slug, rlabel, aname, _parent in SNA_ACTIVITY:
            out.append(_sna_activity(meas, slug, rlabel, aname, table="s2dn", title=f"{ttl}（連鎖方式）", unit="2020暦年=100", tags=(TAG_MACRO,), freq="a",
                                     accessor={"type": "esri_xlsx", "sheet": sheet, "header_row": 7, "first_col": 2},
                                     conv="esri_year_a", kind_label="付表(2) 経済活動別の国内総生産・要素所得 デフレーター", notes=note_s2, first_period="1994"))
    # 付表 3 就業者・雇用者・労働時間（年度・暦年）
    note_s3 = "付表 3「経済活動別の就業者数・雇用者数、労働時間数」。就業者・雇用者は万人（年平均）・労働時間は雇用者 1 人当たり年間時間。"
    for meas, sheet_suffix, ttl, unit, tags in [("employed_persons", "（１）就業者", "就業者数", "万人", (TAG_LABOR, TAG_IND)),
                                               ("employees", "（２）雇用者", "雇用者数", "万人", (TAG_LABOR, TAG_IND)),
                                               ("hours_worked_per_employee", "（３）労働時間数", "労働時間数（雇用者 1 人当たり・年間）", "時間", (TAG_LABOR, TAG_IND))]:
        for freq, sheet_prefix in (("fy", "年度"), ("a", "暦年")):
            for slug, rlabel, aname, _parent in SNA_ACTIVITY[:-1] + SNA_S3_TOTAL:
                out.append(_sna_activity(meas, slug, rlabel, aname, table="s3", title=ttl, unit=unit, tags=tags, freq=freq,
                                         accessor={"type": "esri_xlsx", "sheet": sheet_prefix + sheet_suffix, "header_row": 7, "first_col": 2,
                                                   **({"occurrence": 1} if slug == "total" else {})},
                                         conv="esri_year_fy" if freq == "fy" else "esri_year_a", kind_label="付表(3) 経済活動別の就業者数・雇用者数、労働時間数",
                                         notes=note_s3, first_period="FY1994" if freq == "fy" else "1994"))
    return out


# ---------------------------------------------------------------- 第 7 弾 補遺（2026-08-22）：開業率・廃業率（中小企業白書 付属統計資料 PDF・雇用保険事業年報ベース）
# 要件源＝利用側プロジェクト §5.5 新要望。雇用保険事業年報は e-Stat API に無い＝白書の第 12 表「有雇用事業所数による開廃業率の推移」が時系列の唯一の機械可読元。
HAKUSHO_EDITION = "2025"
HAKUSHO_PDF = f"https://www.chusho.meti.go.jp/pamflet/hakusyo/{HAKUSHO_EDITION}/PDF/chusho/09Hakusyo_fuzokutoukei_web.pdf"
HAKUSHO_NOTE = ("資料＝厚生労働省「雇用保険事業年報」より中小企業庁作成（中小企業白書 付属統計資料）。"
                "開業率＝当該年度の保険関係新規成立事業所数／前年度末の適用事業所数×100、廃業率＝同 消滅事業所数／同×100。"
                "**事業所**（雇用保険の適用事業所）単位＝企業単位の開廃業ではない・雇用者のいない事業者は含まない（白書注記）。"
                "事業所・企業統計／経済センサスによる開廃業率（会社／個人事業者別・調査間隔ごと）は同 PDF の別表＝未収録。"
                "値は白書の表示（小数 1 桁・％）。白書は毎年更新＝版（edition）で表番号・頁が動くので表題で確定する。")


def hakusho_sme_series() -> list[Series]:
    out: list[Series] = []
    for meas, col, ttl in [("entry_rate_ei", "開業率", "開業率（雇用保険事業年報ベース・事業所）"),
                           ("exit_rate_ei", "廃業率", "廃業率（雇用保険事業年報ベース・事業所）")]:
        out.append(_s(series_id=f"meti.hakusho_sme.{meas}.fy", title=f"中小企業白書 付属統計資料 {ttl}（年度）",
                      org="meti", org_name="中小企業庁", source_url="https://www.chusho.meti.go.jp/pamflet/hakusyo/index.html",
                      sector="企業", unit="％", granularity="年度", freq="fy", dataset="hakusho_sme", measure=meas,
                      stat_name="中小企業白書 付属統計資料", table_id=f"hakusho{HAKUSHO_EDITION}_t12", table_title="有雇用事業所数による開廃業率の推移",
                      edition=f"{HAKUSHO_EDITION}年版", policy_tags=(TAG_SME, TAG_IND, TAG_LABOR), first_period="FY1981", status="registered",
                      accessor={"type": "pdf_hakusho_sme", "url": HAKUSHO_PDF, "edition": HAKUSHO_EDITION,
                                "table_title": "有雇用事業所数による開廃業率の推移", "column": col},
                      citation_template=f"中小企業庁「{HAKUSHO_EDITION}年版中小企業白書 付属統計資料」有雇用事業所数による開廃業率の推移（資料：厚生労働省「雇用保険事業年報」・" + "{item}／{period}）取得 {retrieved_at}",
                      notes=HAKUSHO_NOTE))
    return out


# ---------------------------------------------------------------- 第 8 弾 第 1 便（2026-08-24）：JSNA 制度部門別勘定の残り（F-2）＋制度部門別 期末貸借対照表（F-3）
# 要件源＝利用側プロジェクト §5.9（資金循環ブロック）。既存 esri_xlsx（行ラベル×年列）で取れる。
# 表は**原本のシートを機械で走査して生成し、行ラベル・occurrence を確定させたもの**（2026-08-24・年版が変われば再生成）。
# 部門で勘定の構成が違う（例 npish に経常税の行が無い・gg は税が受取側）＝**無い組合せは登録しない**（unknown_series が正しい応答）。
SECTOR_FILES = {"nfc": "非金融法人企業", "fin": "金融機関", "gg": "一般政府", "hh": "家計（個人企業を含む）", "npish": "対家計民間非営利団体"}
SECTOR_MEASURE_TITLE = {
    "property_income_paid": "財産所得（支払）", "property_income_received": "財産所得（受取）",
    "primary_income_balance_gross": "第１次所得バランス（総）", "current_taxes_paid": "所得・富等に課される経常税（支払）",
    "current_taxes_received": "所得・富等に課される経常税（受取）", "social_contributions_received": "純社会負担（受取）",
    "social_contributions_paid": "純社会負担（支払）", "social_benefits_paid": "現物社会移転以外の社会給付（支払）",
    "social_benefits_received": "現物社会移転以外の社会給付（受取）", "disposable_income_gross": "可処分所得（総）",
    "final_consumption": "最終消費支出", "saving_gross": "貯蓄（総）", "saving_net": "貯蓄（純）",
    "gross_fixed_capital_formation": "総固定資本形成", "inventory_change": "在庫変動", "land_purchase_net": "土地の購入（純）",
    "capital_transfers_received": "資本移転等（受取）", "capital_transfers_paid": "資本移転等（支払・控除）",
    "fin_currency_deposits_assets": "金融取引 現金・預金（資産の変動）", "fin_loans_assets": "金融取引 貸出（資産の変動）",
    "fin_debt_securities_assets": "金融取引 債務証券（資産の変動）", "fin_equity_assets": "金融取引 持分・投資信託受益証券（資産の変動）",
    "fin_insurance_assets": "金融取引 保険・年金・定型保証（資産の変動）",
}
SECTOR_ACCOUNT_NOTE = ("Ⅱ. 制度部門別所得支出勘定／Ⅲ. 制度部門別資本勘定・金融勘定（年度）。フロー。10 億円。"
                       "勘定の構成は部門で違う（税は家計・法人が支払側／一般政府が受取側、対家計民間非営利団体には無い項目がある）＝"
                       "**無い組合せは登録していない**（unknown_series が正しい応答）。純貸出／純借入は cao.sna2020.net_lending.*（付表 18）、"
                       "営業余剰・固定資本減耗は cao.sna2020.operating_surplus_*／cfc_sector.* を参照。")

SECTOR_ACCOUNT_ROWS = {
    "nfc": [
        ("property_income_paid", "i2", "年度（１）", "1.1財産所得（支払）", 0),
        ("property_income_received", "i2", "年度（１）", "1.4財産所得（受取）", 0),
        ("primary_income_balance_gross", "i2", "年度（１）", "（再掲）第１次所得バランス（総）", 0),
        ("current_taxes_paid", "i2", "年度（２）", "2.1所得・富等に課される経常税（支払）", 0),
        ("disposable_income_gross", "i2", "年度（２）", "（再掲）可処分所得（総）", 0),
        ("saving_gross", "i2", "年度（３）", "（再掲）貯蓄（総）", 0),
        ("gross_fixed_capital_formation", "c1", "年度（１）資本", "1.1総固定資本形成", 0),
        ("inventory_change", "c1", "年度（１）資本", "1.3在庫変動", 0),
        ("land_purchase_net", "c1", "年度（１）資本", "1.4土地の購入（純）", 0),
        ("capital_transfers_received", "c1", "年度（１）資本", "1.7資本移転等（受取）", 0),
        ("capital_transfers_paid", "c1", "年度（１）資本", "1.8（控除）資本移転等（支払）", 0),
        ("saving_net", "c1", "年度（１）資本", "1.6貯蓄（純）", 0),
        ("fin_currency_deposits_assets", "c1", "年度（２）金融", "2.2現金・預金", 0),
        ("fin_loans_assets", "c1", "年度（２）金融", "2.3貸出", 0),
        ("fin_debt_securities_assets", "c1", "年度（２）金融", "2.4債務証券", 0),
        ("fin_equity_assets", "c1", "年度（２）金融", "2.5持分・投資信託受益証券", 0),
        ("fin_insurance_assets", "c1", "年度（２）金融", "2.6保険・年金・定型保証", 0),
    ],
    "fin": [
        ("property_income_paid", "i3", "年度（１）", "1.1財産所得（支払）", 0),
        ("property_income_received", "i3", "年度（１）", "1.4財産所得（受取）", 0),
        ("primary_income_balance_gross", "i3", "年度（１）", "（再掲）第１次所得バランス（総）", 0),
        ("current_taxes_paid", "i3", "年度（２）", "2.1所得・富等に課される経常税（支払）", 0),
        ("social_contributions_received", "i3", "年度（２）", "2.6純社会負担（受取）", 0),
        ("social_benefits_paid", "i3", "年度（２）", "2.2現物社会移転以外の社会給付（支払）", 0),
        ("disposable_income_gross", "i3", "年度（２）", "（再掲）可処分所得（総）", 0),
        ("saving_gross", "i3", "年度（３）", "（再掲）貯蓄（総）", 0),
        ("gross_fixed_capital_formation", "c2", "年度（１）資本", "1.1総固定資本形成", 0),
        ("land_purchase_net", "c2", "年度（１）資本", "1.3土地の購入（純）", 0),
        ("saving_net", "c2", "年度（１）資本", "1.5貯蓄（純）", 0),
        ("capital_transfers_received", "c2", "年度（１）資本", "1.6資本移転（受取）", 0),
        ("capital_transfers_paid", "c2", "年度（１）資本", "1.7（控除）資本移転（支払）", 0),
        ("fin_currency_deposits_assets", "c2", "年度（２）金融", "2.2現金・預金", 0),
        ("fin_loans_assets", "c2", "年度（２）金融", "2.3貸出", 0),
        ("fin_debt_securities_assets", "c2", "年度（２）金融", "2.4債務証券", 0),
        ("fin_equity_assets", "c2", "年度（２）金融", "2.5持分・投資信託受益証券", 0),
        ("fin_insurance_assets", "c2", "年度（２）金融", "2.6保険・年金・定型保証", 0),
    ],
    "gg": [
        ("property_income_paid", "i4", "年度（１）", "1.1財産所得（支払）", 0),
        ("property_income_received", "i4", "年度（１）", "1.5財産所得（受取）", 0),
        ("primary_income_balance_gross", "i4", "年度（１）", "（再掲）第１次所得バランス（総）", 0),
        ("current_taxes_received", "i4", "年度（２）", "2.5所得・富等に課される経常税（受取）", 0),
        ("social_contributions_received", "i4", "年度（２）", "2.6純社会負担（受取）", 0),
        ("social_benefits_paid", "i4", "年度（２）", "2.1現物社会移転以外の社会給付（支払）", 0),
        ("disposable_income_gross", "i4", "年度（２）", "（再掲）可処分所得（総）", 0),
        ("final_consumption", "i4", "年度（４）a", "4.1最終消費支出", 0),
        ("saving_gross", "i4", "年度（４）a", "（再掲）貯蓄（総）", 0),
        ("gross_fixed_capital_formation", "c3", "年度（１）資本", "1.1総固定資本形成", 0),
        ("inventory_change", "c3", "年度（１）資本", "1.3在庫変動", 0),
        ("land_purchase_net", "c3", "年度（１）資本", "1.4土地の購入（純）", 0),
        ("capital_transfers_received", "c3", "年度（１）資本", "1.7資本移転（受取）", 0),
        ("capital_transfers_paid", "c3", "年度（１）資本", "1.8（控除）資本移転（支払）", 0),
        ("saving_net", "c3", "年度（１）資本", "1.6貯蓄（純）", 0),
        ("fin_currency_deposits_assets", "c3", "年度（２）金融", "2.2現金・預金", 0),
        ("fin_loans_assets", "c3", "年度（２）金融", "2.3貸出", 0),
        ("fin_debt_securities_assets", "c3", "年度（２）金融", "2.4債務証券", 0),
        ("fin_equity_assets", "c3", "年度（２）金融", "2.5持分・投資信託受益証券", 0),
        ("fin_insurance_assets", "c3", "年度（２）金融", "2.6保険・年金・定型保証", 0),
    ],
    "hh": [
        ("property_income_paid", "i5", "年度（１）", "1.1財産所得（支払）", 0),
        ("property_income_received", "i5", "年度（１）", "1.5財産所得（受取）", 0),
        ("primary_income_balance_gross", "i5", "年度（１）", "（再掲）第１次所得バランス（総）", 0),
        ("current_taxes_paid", "i5", "年度（２）", "2.1所得・富等に課される経常税（支払）", 0),
        ("social_contributions_paid", "i5", "年度（２）", "2.2純社会負担（支払）", 0),
        ("social_benefits_received", "i5", "年度（２）", "2.6現物社会移転以外の社会給付（受取）", 0),
        ("disposable_income_gross", "i5", "年度（２）", "（再掲）可処分所得（総）", 0),
        ("final_consumption", "i5", "年度（４）a", "4.1最終消費支出（個別消費支出）", 0),
        ("saving_gross", "i5", "年度（４）a", "（再掲）貯蓄（総）", 0),
        ("gross_fixed_capital_formation", "c4", "年度（１）資本", "1.1総固定資本形成", 0),
        ("inventory_change", "c4", "年度（１）資本", "1.3在庫変動", 0),
        ("land_purchase_net", "c4", "年度（１）資本", "1.4土地の購入（純）", 0),
        ("capital_transfers_received", "c4", "年度（１）資本", "1.7資本移転（受取）", 0),
        ("capital_transfers_paid", "c4", "年度（１）資本", "1.8（控除）資本移転（支払）", 0),
        ("saving_net", "c4", "年度（１）資本", "1.6貯蓄（純）", 0),
        ("fin_currency_deposits_assets", "c4", "年度（２）金融", "2.2現金・預金", 0),
        ("fin_loans_assets", "c4", "年度（２）金融", "2.3貸出", 0),
        ("fin_debt_securities_assets", "c4", "年度（２）金融", "2.4債務証券", 0),
        ("fin_equity_assets", "c4", "年度（２）金融", "2.5持分・投資信託受益証券", 0),
        ("fin_insurance_assets", "c4", "年度（２）金融", "2.6保険・年金・定型保証", 0),
    ],
    "npish": [
        ("property_income_paid", "i6", "年度（１）", "1.1財産所得（支払）", 0),
        ("property_income_received", "i6", "年度（１）", "1.3財産所得（受取）", 0),
        ("primary_income_balance_gross", "i6", "年度（１）", "（再掲）第１次所得バランス（総）", 0),
        ("social_benefits_paid", "i6", "年度（２）", "2.1現物社会移転以外の社会給付（支払）", 0),
        ("disposable_income_gross", "i6", "年度（２）", "（再掲）可処分所得（総）", 0),
        ("final_consumption", "i6", "年度（４）a", "4.1最終消費支出（個別消費支出）(3.1)", 0),
        ("saving_gross", "i6", "年度（４）a", "（再掲）貯蓄（総）", 0),
        ("gross_fixed_capital_formation", "c5", "年度（１）資本", "1.1総固定資本形成", 0),
        ("land_purchase_net", "c5", "年度（１）資本", "1.3土地の購入（純）", 0),
        ("saving_net", "c5", "年度（１）資本", "1.5貯蓄（純）", 0),
        ("capital_transfers_received", "c5", "年度（１）資本", "1.6資本移転（受取）", 0),
        ("capital_transfers_paid", "c5", "年度（１）資本", "1.7（控除）資本移転（支払）", 0),
        ("fin_currency_deposits_assets", "c5", "年度（２）金融", "2.2現金・預金", 0),
        ("fin_loans_assets", "c5", "年度（２）金融", "2.3貸出", 0),
        ("fin_debt_securities_assets", "c5", "年度（２）金融", "2.4債務証券", 0),
        ("fin_equity_assets", "c5", "年度（２）金融", "2.5持分・投資信託受益証券", 0),
        ("fin_insurance_assets", "c5", "年度（２）金融", "2.6保険・年金・定型保証", 0),
    ],
}

# 期末貸借対照表勘定（ストック編 Ⅱ. 制度部門別勘定・暦年末）。si11 非金融法人／si21 金融機関／si3 一般政府／si4 家計／si5 NPISH。
# (表, 見出し行)＝si11/si21 は部門名の副題が 1 行多く header_row=9・si3/si4/si5 は 8（2026-08-24 原本で確認）
BS_FILES = {"nfc": ("si11", 9), "fin": ("si21", 9), "gg": ("si3", 8), "hh": ("si4", 8), "npish": ("si5", 8)}
BS_MEASURE_TITLE = {
    "nonfinancial_assets": "非金融資産", "produced_assets": "生産資産", "fixed_assets": "固定資産", "inventories": "在庫",
    "nonproduced_assets": "非生産資産（自然資源）", "land": "土地", "financial_assets": "金融資産",
    "currency_deposits": "現金・預金（資産）", "loans_assets": "貸出（資産）", "debt_securities_assets": "債務証券（資産）",
    "equity_assets": "持分・投資信託受益証券（資産）", "insurance_assets": "保険・年金・定型保証（資産）",
    "assets_total": "期末資産（合計）", "liabilities": "負債", "borrowing": "借入（負債）",
    "debt_securities_liabilities": "債務証券（負債）", "equity_liabilities": "持分・投資信託受益証券（負債）",
    "insurance_liabilities": "保険・年金・定型保証（負債）", "net_worth": "正味資産",
}
BS_NOTE = ("ストック編 Ⅱ. 制度部門別勘定 期末貸借対照表勘定（**暦年末**・年度ではない）。10 億円。"
           "資産側と負債側で同じ行ラベルが出るため occurrence で区別している（accessor に記録）。"
           "一国計は cao.sna2020.stock.*（付表 ss2 系）。非金融資産の内訳（資産別）は別表＝固定資本ストックマトリックス（未収録・F-4）。")

BS_SECTOR_ROWS = {
    "nfc": [
        ("nonfinancial_assets", "１．非金融資産", 0),
        ("produced_assets", "（１）生産資産", 0),
        ("fixed_assets", "ａ．固定資産", 0),
        ("inventories", "ｂ．在庫", 0),
        ("nonproduced_assets", "（２）非生産資産（自然資源）", 0),
        ("land", "ａ．土地", 0),
        ("financial_assets", "２．金融資産", 0),
        ("currency_deposits", "（２）現金・預金", 1),
        ("loans_assets", "（３）貸出", 0),
        ("debt_securities_assets", "（４）債務証券", 1),
        ("equity_assets", "（５）持分・投資信託受益証券", 1),
        ("insurance_assets", "（６）保険・年金・定型保証", 1),
        ("assets_total", "期末資産", 0),
        ("borrowing", "（３）借入", 0),
        ("debt_securities_liabilities", "（４）債務証券", 2),
        ("equity_liabilities", "（５）持分・投資信託受益証券", 2),
        ("insurance_liabilities", "（６）保険・年金・定型保証", 2),
        ("liabilities", "３．負債", 0),
        ("net_worth", "４．正味資産", 0),
    ],
    "fin": [
        ("nonfinancial_assets", "１．非金融資産", 0),
        ("produced_assets", "（１）生産資産", 0),
        ("fixed_assets", "ａ．固定資産", 0),
        ("inventories", "ｂ．在庫", 0),
        ("nonproduced_assets", "（２）非生産資産（自然資源）", 0),
        ("land", "ａ．土地", 0),
        ("financial_assets", "２．金融資産", 0),
        ("currency_deposits", "（２）現金・預金", 1),
        ("loans_assets", "（３）貸出", 0),
        ("debt_securities_assets", "（４）債務証券", 1),
        ("equity_assets", "（５）持分・投資信託受益証券", 1),
        ("insurance_assets", "（６）保険・年金・定型保証", 1),
        ("assets_total", "期末資産", 0),
        ("borrowing", "（３）借入", 0),
        ("debt_securities_liabilities", "（４）債務証券", 2),
        ("equity_liabilities", "（５）持分・投資信託受益証券", 2),
        ("insurance_liabilities", "（６）保険・年金・定型保証", 2),
        ("liabilities", "３．負債", 0),
        ("net_worth", "４．正味資産", 0),
    ],
    "gg": [
        ("nonfinancial_assets", "１．非金融資産", 0),
        ("produced_assets", "（１）生産資産", 0),
        ("fixed_assets", "ａ．固定資産", 0),
        ("inventories", "ｂ．在庫", 0),
        ("nonproduced_assets", "（２）非生産資産（自然資源）", 0),
        ("land", "ａ．土地", 0),
        ("financial_assets", "２．金融資産", 0),
        ("currency_deposits", "（２）現金・預金", 1),
        ("loans_assets", "（３）貸出", 0),
        ("debt_securities_assets", "（４）債務証券", 1),
        ("equity_assets", "（５）持分・投資信託受益証券", 1),
        ("insurance_assets", "（６）保険・年金・定型保証", 1),
        ("assets_total", "期末資産", 0),
        ("borrowing", "（３）借入", 0),
        ("debt_securities_liabilities", "（４）債務証券", 2),
        ("equity_liabilities", "（５）持分・投資信託受益証券", 2),
        ("insurance_liabilities", "（６）保険・年金・定型保証", 2),
        ("liabilities", "３．負債", 0),
        ("net_worth", "４．正味資産", 0),
    ],
    "hh": [
        ("nonfinancial_assets", "１．非金融資産", 0),
        ("produced_assets", "（１）生産資産", 0),
        ("fixed_assets", "ａ．固定資産", 0),
        ("inventories", "ｂ．在庫", 0),
        ("nonproduced_assets", "（２）非生産資産（自然資源）", 0),
        ("land", "ａ．土地", 0),
        ("financial_assets", "２．金融資産", 0),
        ("currency_deposits", "（２）現金・預金", 1),
        ("loans_assets", "（３）貸出", 0),
        ("debt_securities_assets", "（４）債務証券", 1),
        ("equity_assets", "（５）持分・投資信託受益証券", 1),
        ("insurance_assets", "（６）保険・年金・定型保証", 1),
        ("assets_total", "期末資産", 0),
        ("borrowing", "（３）借入", 0),
        ("debt_securities_liabilities", "（４）債務証券", 2),
        ("equity_liabilities", "（５）持分・投資信託受益証券", 2),
        ("insurance_liabilities", "（６）保険・年金・定型保証", 2),
        ("liabilities", "３．負債", 0),
        ("net_worth", "４．正味資産", 0),
    ],
    "npish": [
        ("nonfinancial_assets", "１．非金融資産", 0),
        ("produced_assets", "（１）生産資産", 0),
        ("fixed_assets", "ａ．固定資産", 0),
        ("inventories", "ｂ．在庫", 0),
        ("nonproduced_assets", "（２）非生産資産（自然資源）", 0),
        ("land", "ａ．土地", 0),
        ("financial_assets", "２．金融資産", 0),
        ("currency_deposits", "（２）現金・預金", 1),
        ("loans_assets", "（３）貸出", 0),
        ("debt_securities_assets", "（４）債務証券", 1),
        ("equity_assets", "（５）持分・投資信託受益証券", 1),
        ("insurance_assets", "（６）保険・年金・定型保証", 1),
        ("assets_total", "期末資産", 0),
        ("borrowing", "（３）借入", 0),
        ("debt_securities_liabilities", "（４）債務証券", 2),
        ("equity_liabilities", "（５）持分・投資信託受益証券", 2),
        ("insurance_liabilities", "（６）保険・年金・定型保証", 2),
        ("liabilities", "３．負債", 0),
        ("net_worth", "４．正味資産", 0),
    ],
}


def sna_sector_series() -> list[Series]:
    """F-2 制度部門別勘定の残り（年度・フロー）＋ F-3 制度部門別 期末貸借対照表（暦年末・ストック）。"""
    out: list[Series] = []
    for slug, rows in SECTOR_ACCOUNT_ROWS.items():
        sname = SECTOR_FILES[slug]
        for meas, tbl, sheet, rlabel, occ in rows:
            ttl = SECTOR_MEASURE_TITLE[meas]
            acc = {"type": "esri_xlsx", "edition": SNA_EDITION, "file": "kakuhou/files/{Y}/tables/{Y}" + f"{tbl}_jp.xlsx",
                   "sheet": sheet, "row_label": rlabel, "header_row": 7, "first_col": 2}
            if occ:
                acc["occurrence"] = occ
            kl = f"制度部門別勘定 {sname}（{sheet}）"
            out.append(_s(series_id=f"cao.sna_sector.{meas}.{slug}.fy",
                          title=f"国民経済計算（2020年基準・2008SNA）年次推計 制度部門別 {ttl}（{sname}・年度）",
                          org="cao", org_name="内閣府", source_url=ESRI_TOP, sector="マクロ", unit="10億円", granularity="年度", freq="fy",
                          dataset="sna_sector", measure=meas, dims=slug, basis="2020年基準・2008SNA", edition=f"{SNA_EDITION}年度年次推計（確報）",
                          stat_name="国民経済計算年次推計", table_id=f"{SNA_EDITION}{tbl}", table_title=kl,
                          policy_tags=(TAG_MACRO, TAG_FIN), period_converter="esri_year_fy", first_period="FY1994",
                          accessor=acc, status="registered",
                          citation_template=f"内閣府「国民経済計算年次推計」{kl}（{SNA_EDITION}年度確報・2020年基準・2008SNA・" + "{item}／{period}）取得 {retrieved_at}",
                          notes=SECTOR_ACCOUNT_NOTE))
    for slug, rows in BS_SECTOR_ROWS.items():
        sname, (tbl, hrow) = SECTOR_FILES[slug], BS_FILES[slug]
        for meas, rlabel, occ in rows:
            ttl = BS_MEASURE_TITLE[meas]
            acc = {"type": "esri_xlsx", "edition": SNA_EDITION, "file": "kakuhou/files/{Y}/tables/{Y}" + f"{tbl}_jp.xlsx",
                   "sheet": "期末貸借対照表", "row_label": rlabel, "header_row": hrow, "first_col": 2}
            if occ:
                acc["occurrence"] = occ
            kl = f"ストック編 制度部門別 期末貸借対照表勘定 {sname}"
            out.append(_s(series_id=f"cao.sna_sector_bs.{meas}.{slug}.a",
                          title=f"国民経済計算（2020年基準・2008SNA）年次推計 制度部門別 期末貸借対照表 {ttl}（{sname}・暦年末）",
                          org="cao", org_name="内閣府", source_url=ESRI_TOP, sector="マクロ", unit="10億円", granularity="暦年", freq="a",
                          dataset="sna_sector_bs", measure=meas, dims=slug, basis="2020年基準・2008SNA", edition=f"{SNA_EDITION}年度年次推計（確報）",
                          stat_name="国民経済計算年次推計", table_id=f"{SNA_EDITION}{tbl}", table_title=kl,
                          policy_tags=(TAG_MACRO, TAG_FIN), period_converter="esri_year_a", first_period="1994",
                          accessor=acc, status="registered",
                          citation_template=f"内閣府「国民経済計算年次推計」{kl}（{SNA_EDITION}年度確報・2020年基準・2008SNA・" + "{item}／{period}）取得 {retrieved_at}",
                          notes=BS_NOTE))
    return out


# ---------------------------------------------------------------- 第 8 弾 第 2 便（2026-08-24）：日銀 資金循環統計 部門別（F-1）
# 要件源＝利用側プロジェクト §5.9 F-1（最優先）。取得元＝**fof2_jp.zip（系列名称入り）**の `ff_dl_fof_fiscal-year_jp.csv`（**年度** 1979〜）。
# 系列名が「項目／部門／ストック・フロー」に構造化されているので、下表は**原本を機械で走査して生成**した（2026-08-24・版が変われば再生成）。
# 粒度＝**第 1 階層の大分類のみ**（系列名に '－' を含む細目は採らない）＝利用側の要望どおり「部門 × 大分類」。
# 既存の boj.fof.hh_*.q（家計・**四半期**ストック 5 系列・名称なしの ff_value.csv 経由）はそのまま残す＝freq が違う別系列。
FOF_ZIP, FOF_FILE = "fof2_jp.zip", "ff_dl_fof_fiscal-year_jp.csv"
FOF_SECTOR = {"nfc": "非金融法人企業", "fin": "金融機関", "gg": "一般政府", "hh": "家計", "npish": "対家計民間非営利団体", "row": "海外"}
FOF_KIND = {"stock": "ストック（年度末残高）", "flow": "フロー（年度中の取引）"}
FOF_NOTE = ("日本銀行「資金循環統計（２００８ＳＮＡベース）」の年度計数（fof2_jp.zip の系列名称入りファイル）。単位は億円。"
            "**ストック＝年度末残高／フロー＝年度中の取引**（調整＝価格変動等は別表で未収録）。"
            "部門は JSNA の制度部門と近いが**同一ではない**（資金循環は金融取引の分類・JSNA は生産と所得の分類）＝"
            "資金循環側の『負債・資金過不足』（liabilities_net_lending）に**概念が対応するのは JSNA 付表 18 の２．（金融勘定側）＝"
            "cao.sna2020.net_lending_financial.<部門>.fy**（資本勘定側の net_lending.* ではない）。ただし JSNA 側で部門分類等の調整が入るため**値は一致しない**"
            "（FY2024 非金融法人：JSNA 23,927.6 十億円 vs 資金循環 250,321 億円＝25,032.1 十億円）。単位も違う（億円 vs 10 億円）。"
            "**細目（系列名に '－' が付く内訳）は未収録**＝大分類のみ。四半期は boj.fof.hh_*.q（家計のストック 5 系列）だけが別系列として残っている。"
            "★ **measure が `_memo` で終わるものは再掲項目＝合計に足さない**（現在は海外部門の liabilities_foreign_reserves_memo＝外貨準備のみ。"
            "外貨準備は複数の金融商品にまたがるため商品別に既に計上済みで、外貨準備という観点から再掲されている＝"
            "2024 年末ストックで 外貨準備 189.8 兆 > その他対外債権債務 74.5 兆＝どれか 1 項目の内訳ではない）。")

FOF_SERIES = {
    ('fin', 'flow'): [
        ("assets_accounts_receivable_payable", "FOF_FFYF100A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYF100A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYF100A300", "資産・債務証券"),
        ("assets_deposits_with_banks", "FOF_FFYF100A500", "資産・預け金"),
        ("assets_direct_investment_abroad", "FOF_FFYF100A530", "資産・対外直接投資"),
        ("assets_equity_investment_trusts", "FOF_FFYF100A334", "資産・株式等・投資信託受益証券"),
        ("assets_financial_derivatives", "FOF_FFYF100A340", "資産・金融派生商品・雇用者ストックオプション"),
        ("assets_fiscal_loan_deposits", "FOF_FFYF100A190", "資産・財政融資資金預託金"),
        ("assets_insurance_pension", "FOF_FFYF100A400", "資産・保険・年金・定型保証"),
        ("assets_loans", "FOF_FFYF100A200", "資産・貸出"),
        ("assets_other", "FOF_FFYF100A600", "資産・その他"),
        ("assets_other_external_claims", "FOF_FFYF100A550", "資産・その他対外債権債務"),
        ("assets_portfolio_investment_abroad", "FOF_FFYF100A540", "資産・対外証券投資"),
        ("assets_total", "FOF_FFYF100A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYF100A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYF100L520", "負債・未収・未払金"),
        ("liabilities_currency_deposits", "FOF_FFYF100L100", "負債・現金・預金"),
        ("liabilities_debt_securities", "FOF_FFYF100L300", "負債・債務証券"),
        ("liabilities_deposits_with_banks", "FOF_FFYF100L500", "負債・預け金"),
        ("liabilities_equity_investment_trusts", "FOF_FFYF100L334", "負債・株式等・投資信託受益証券"),
        ("liabilities_financial_derivatives", "FOF_FFYF100L340", "負債・金融派生商品・雇用者ストックオプション"),
        ("liabilities_fiscal_loan_deposits", "FOF_FFYF100L190", "負債・財政融資資金預託金"),
        ("liabilities_insurance_pension", "FOF_FFYF100L400", "負債・保険・年金・定型保証"),
        ("liabilities_loans", "FOF_FFYF100L200", "負債・貸出"),
        ("liabilities_net_lending", "FOF_FFYF100L700", "負債・資金過不足"),
        ("liabilities_other", "FOF_FFYF100L600", "負債・その他"),
        ("liabilities_other_external_claims", "FOF_FFYF100L550", "負債・その他対外債権債務"),
        ("liabilities_total", "FOF_FFYF100L900", "負債・合計"),
        ("liabilities_trade_credit", "FOF_FFYF100L510", "負債・企業間・貿易信用"),
    ],
    ('fin', 'stock'): [
        ("assets_accounts_receivable_payable", "FOF_FFYS100A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYS100A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYS100A300", "資産・債務証券"),
        ("assets_deposits_with_banks", "FOF_FFYS100A500", "資産・預け金"),
        ("assets_direct_investment_abroad", "FOF_FFYS100A530", "資産・対外直接投資"),
        ("assets_equity_investment_trusts", "FOF_FFYS100A334", "資産・株式等・投資信託受益証券"),
        ("assets_financial_derivatives", "FOF_FFYS100A340", "資産・金融派生商品・雇用者ストックオプション"),
        ("assets_fiscal_loan_deposits", "FOF_FFYS100A190", "資産・財政融資資金預託金"),
        ("assets_insurance_pension", "FOF_FFYS100A400", "資産・保険・年金・定型保証"),
        ("assets_loans", "FOF_FFYS100A200", "資産・貸出"),
        ("assets_other", "FOF_FFYS100A600", "資産・その他"),
        ("assets_other_external_claims", "FOF_FFYS100A550", "資産・その他対外債権債務"),
        ("assets_portfolio_investment_abroad", "FOF_FFYS100A540", "資産・対外証券投資"),
        ("assets_total", "FOF_FFYS100A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYS100A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYS100L520", "負債・未収・未払金"),
        ("liabilities_currency_deposits", "FOF_FFYS100L100", "負債・現金・預金"),
        ("liabilities_debt_securities", "FOF_FFYS100L300", "負債・債務証券"),
        ("liabilities_deposits_with_banks", "FOF_FFYS100L500", "負債・預け金"),
        ("liabilities_equity_investment_trusts", "FOF_FFYS100L334", "負債・株式等・投資信託受益証券"),
        ("liabilities_financial_derivatives", "FOF_FFYS100L340", "負債・金融派生商品・雇用者ストックオプション"),
        ("liabilities_fiscal_loan_deposits", "FOF_FFYS100L190", "負債・財政融資資金預託金"),
        ("liabilities_insurance_pension", "FOF_FFYS100L400", "負債・保険・年金・定型保証"),
        ("liabilities_loans", "FOF_FFYS100L200", "負債・貸出"),
        ("liabilities_net_financial_assets", "FOF_FFYS100L700", "負債・金融資産･負債差額"),
        ("liabilities_other", "FOF_FFYS100L600", "負債・その他"),
        ("liabilities_other_external_claims", "FOF_FFYS100L550", "負債・その他対外債権債務"),
        ("liabilities_total", "FOF_FFYS100L900", "負債・合計"),
        ("liabilities_trade_credit", "FOF_FFYS100L510", "負債・企業間・貿易信用"),
    ],
    ('gg', 'flow'): [
        ("assets_accounts_receivable_payable", "FOF_FFYF420A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYF420A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYF420A300", "資産・債務証券"),
        ("assets_deposits_with_banks", "FOF_FFYF420A500", "資産・預け金"),
        ("assets_equity_investment_trusts", "FOF_FFYF420A334", "資産・株式等・投資信託受益証券"),
        ("assets_fiscal_loan_deposits", "FOF_FFYF420A190", "資産・財政融資資金預託金"),
        ("assets_loans", "FOF_FFYF420A200", "資産・貸出"),
        ("assets_other", "FOF_FFYF420A600", "資産・その他"),
        ("assets_other_external_claims", "FOF_FFYF420A550", "資産・その他対外債権債務"),
        ("assets_portfolio_investment_abroad", "FOF_FFYF420A540", "資産・対外証券投資"),
        ("assets_total", "FOF_FFYF420A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYF420A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYF420L520", "負債・未収・未払金"),
        ("liabilities_debt_securities", "FOF_FFYF420L300", "負債・債務証券"),
        ("liabilities_deposits_with_banks", "FOF_FFYF420L500", "負債・預け金"),
        ("liabilities_equity_investment_trusts", "FOF_FFYF420L334", "負債・株式等・投資信託受益証券"),
        ("liabilities_loans", "FOF_FFYF420L200", "負債・貸出"),
        ("liabilities_net_lending", "FOF_FFYF420L700", "負債・資金過不足"),
        ("liabilities_other", "FOF_FFYF420L600", "負債・その他"),
        ("liabilities_other_external_claims", "FOF_FFYF420L550", "負債・その他対外債権債務"),
        ("liabilities_total", "FOF_FFYF420L900", "負債・合計"),
        ("liabilities_trade_credit", "FOF_FFYF420L510", "負債・企業間・貿易信用"),
    ],
    ('gg', 'stock'): [
        ("assets_accounts_receivable_payable", "FOF_FFYS420A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYS420A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYS420A300", "資産・債務証券"),
        ("assets_deposits_with_banks", "FOF_FFYS420A500", "資産・預け金"),
        ("assets_equity_investment_trusts", "FOF_FFYS420A334", "資産・株式等・投資信託受益証券"),
        ("assets_financial_derivatives", "FOF_FFYS420A340", "資産・金融派生商品・雇用者ストックオプション"),
        ("assets_fiscal_loan_deposits", "FOF_FFYS420A190", "資産・財政融資資金預託金"),
        ("assets_loans", "FOF_FFYS420A200", "資産・貸出"),
        ("assets_other", "FOF_FFYS420A600", "資産・その他"),
        ("assets_other_external_claims", "FOF_FFYS420A550", "資産・その他対外債権債務"),
        ("assets_portfolio_investment_abroad", "FOF_FFYS420A540", "資産・対外証券投資"),
        ("assets_total", "FOF_FFYS420A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYS420A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYS420L520", "負債・未収・未払金"),
        ("liabilities_debt_securities", "FOF_FFYS420L300", "負債・債務証券"),
        ("liabilities_deposits_with_banks", "FOF_FFYS420L500", "負債・預け金"),
        ("liabilities_equity_investment_trusts", "FOF_FFYS420L334", "負債・株式等・投資信託受益証券"),
        ("liabilities_financial_derivatives", "FOF_FFYS420L340", "負債・金融派生商品・雇用者ストックオプション"),
        ("liabilities_loans", "FOF_FFYS420L200", "負債・貸出"),
        ("liabilities_net_financial_assets", "FOF_FFYS420L700", "負債・金融資産･負債差額"),
        ("liabilities_other", "FOF_FFYS420L600", "負債・その他"),
        ("liabilities_other_external_claims", "FOF_FFYS420L550", "負債・その他対外債権債務"),
        ("liabilities_total", "FOF_FFYS420L900", "負債・合計"),
        ("liabilities_trade_credit", "FOF_FFYS420L510", "負債・企業間・貿易信用"),
    ],
    ('hh', 'flow'): [
        ("assets_accounts_receivable_payable", "FOF_FFYF430A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYF430A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYF430A300", "資産・債務証券"),
        ("assets_deposits_with_banks", "FOF_FFYF430A500", "資産・預け金"),
        ("assets_equity_investment_trusts", "FOF_FFYF430A334", "資産・株式等・投資信託受益証券"),
        ("assets_financial_derivatives", "FOF_FFYF430A340", "資産・金融派生商品・雇用者ストックオプション"),
        ("assets_insurance_pension", "FOF_FFYF430A400", "資産・保険・年金・定型保証"),
        ("assets_loans", "FOF_FFYF430A200", "資産・貸出"),
        ("assets_other", "FOF_FFYF430A600", "資産・その他"),
        ("assets_portfolio_investment_abroad", "FOF_FFYF430A540", "資産・対外証券投資"),
        ("assets_total", "FOF_FFYF430A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYF430A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYF430L520", "負債・未収・未払金"),
        ("liabilities_financial_derivatives", "FOF_FFYF430L340", "負債・金融派生商品・雇用者ストックオプション"),
        ("liabilities_loans", "FOF_FFYF430L200", "負債・貸出"),
        ("liabilities_net_lending", "FOF_FFYF430L700", "負債・資金過不足"),
        ("liabilities_other", "FOF_FFYF430L600", "負債・その他"),
        ("liabilities_total", "FOF_FFYF430L900", "負債・合計"),
        ("liabilities_trade_credit", "FOF_FFYF430L510", "負債・企業間・貿易信用"),
    ],
    ('hh', 'stock'): [
        ("assets_accounts_receivable_payable", "FOF_FFYS430A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYS430A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYS430A300", "資産・債務証券"),
        ("assets_deposits_with_banks", "FOF_FFYS430A500", "資産・預け金"),
        ("assets_equity_investment_trusts", "FOF_FFYS430A334", "資産・株式等・投資信託受益証券"),
        ("assets_financial_derivatives", "FOF_FFYS430A340", "資産・金融派生商品・雇用者ストックオプション"),
        ("assets_insurance_pension", "FOF_FFYS430A400", "資産・保険・年金・定型保証"),
        ("assets_loans", "FOF_FFYS430A200", "資産・貸出"),
        ("assets_other", "FOF_FFYS430A600", "資産・その他"),
        ("assets_portfolio_investment_abroad", "FOF_FFYS430A540", "資産・対外証券投資"),
        ("assets_total", "FOF_FFYS430A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYS430A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYS430L520", "負債・未収・未払金"),
        ("liabilities_financial_derivatives", "FOF_FFYS430L340", "負債・金融派生商品・雇用者ストックオプション"),
        ("liabilities_loans", "FOF_FFYS430L200", "負債・貸出"),
        ("liabilities_net_financial_assets", "FOF_FFYS430L700", "負債・金融資産･負債差額"),
        ("liabilities_other", "FOF_FFYS430L600", "負債・その他"),
        ("liabilities_total", "FOF_FFYS430L900", "負債・合計"),
        ("liabilities_trade_credit", "FOF_FFYS430L510", "負債・企業間・貿易信用"),
    ],
    ('nfc', 'flow'): [
        ("assets_accounts_receivable_payable", "FOF_FFYF410A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYF410A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYF410A300", "資産・債務証券"),
        ("assets_deposits_with_banks", "FOF_FFYF410A500", "資産・預け金"),
        ("assets_direct_investment_abroad", "FOF_FFYF410A530", "資産・対外直接投資"),
        ("assets_equity_investment_trusts", "FOF_FFYF410A334", "資産・株式等・投資信託受益証券"),
        ("assets_financial_derivatives", "FOF_FFYF410A340", "資産・金融派生商品・雇用者ストックオプション"),
        ("assets_fiscal_loan_deposits", "FOF_FFYF410A190", "資産・財政融資資金預託金"),
        ("assets_insurance_pension", "FOF_FFYF410A400", "資産・保険・年金・定型保証"),
        ("assets_loans", "FOF_FFYF410A200", "資産・貸出"),
        ("assets_other", "FOF_FFYF410A600", "資産・その他"),
        ("assets_other_external_claims", "FOF_FFYF410A550", "資産・その他対外債権債務"),
        ("assets_portfolio_investment_abroad", "FOF_FFYF410A540", "資産・対外証券投資"),
        ("assets_total", "FOF_FFYF410A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYF410A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYF410L520", "負債・未収・未払金"),
        ("liabilities_debt_securities", "FOF_FFYF410L300", "負債・債務証券"),
        ("liabilities_deposits_with_banks", "FOF_FFYF410L500", "負債・預け金"),
        ("liabilities_equity_investment_trusts", "FOF_FFYF410L334", "負債・株式等・投資信託受益証券"),
        ("liabilities_financial_derivatives", "FOF_FFYF410L340", "負債・金融派生商品・雇用者ストックオプション"),
        ("liabilities_insurance_pension", "FOF_FFYF410L400", "負債・保険・年金・定型保証"),
        ("liabilities_loans", "FOF_FFYF410L200", "負債・貸出"),
        ("liabilities_net_lending", "FOF_FFYF410L700", "負債・資金過不足"),
        ("liabilities_other", "FOF_FFYF410L600", "負債・その他"),
        ("liabilities_other_external_claims", "FOF_FFYF410L550", "負債・その他対外債権債務"),
        ("liabilities_total", "FOF_FFYF410L900", "負債・合計"),
        ("liabilities_trade_credit", "FOF_FFYF410L510", "負債・企業間・貿易信用"),
    ],
    ('nfc', 'stock'): [
        ("assets_accounts_receivable_payable", "FOF_FFYS410A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYS410A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYS410A300", "資産・債務証券"),
        ("assets_deposits_with_banks", "FOF_FFYS410A500", "資産・預け金"),
        ("assets_direct_investment_abroad", "FOF_FFYS410A530", "資産・対外直接投資"),
        ("assets_equity_investment_trusts", "FOF_FFYS410A334", "資産・株式等・投資信託受益証券"),
        ("assets_financial_derivatives", "FOF_FFYS410A340", "資産・金融派生商品・雇用者ストックオプション"),
        ("assets_fiscal_loan_deposits", "FOF_FFYS410A190", "資産・財政融資資金預託金"),
        ("assets_insurance_pension", "FOF_FFYS410A400", "資産・保険・年金・定型保証"),
        ("assets_loans", "FOF_FFYS410A200", "資産・貸出"),
        ("assets_other", "FOF_FFYS410A600", "資産・その他"),
        ("assets_other_external_claims", "FOF_FFYS410A550", "資産・その他対外債権債務"),
        ("assets_portfolio_investment_abroad", "FOF_FFYS410A540", "資産・対外証券投資"),
        ("assets_total", "FOF_FFYS410A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYS410A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYS410L520", "負債・未収・未払金"),
        ("liabilities_debt_securities", "FOF_FFYS410L300", "負債・債務証券"),
        ("liabilities_deposits_with_banks", "FOF_FFYS410L500", "負債・預け金"),
        ("liabilities_equity_investment_trusts", "FOF_FFYS410L334", "負債・株式等・投資信託受益証券"),
        ("liabilities_financial_derivatives", "FOF_FFYS410L340", "負債・金融派生商品・雇用者ストックオプション"),
        ("liabilities_insurance_pension", "FOF_FFYS410L400", "負債・保険・年金・定型保証"),
        ("liabilities_loans", "FOF_FFYS410L200", "負債・貸出"),
        ("liabilities_net_financial_assets", "FOF_FFYS410L700", "負債・金融資産･負債差額"),
        ("liabilities_other", "FOF_FFYS410L600", "負債・その他"),
        ("liabilities_other_external_claims", "FOF_FFYS410L550", "負債・その他対外債権債務"),
        ("liabilities_total", "FOF_FFYS410L900", "負債・合計"),
        ("liabilities_trade_credit", "FOF_FFYS410L510", "負債・企業間・貿易信用"),
    ],
    ('npish', 'flow'): [
        ("assets_accounts_receivable_payable", "FOF_FFYF440A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYF440A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYF440A300", "資産・債務証券"),
        ("assets_equity_investment_trusts", "FOF_FFYF440A334", "資産・株式等・投資信託受益証券"),
        ("assets_loans", "FOF_FFYF440A200", "資産・貸出"),
        ("assets_other", "FOF_FFYF440A600", "資産・その他"),
        ("assets_total", "FOF_FFYF440A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYF440A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYF440L520", "負債・未収・未払金"),
        ("liabilities_deposits_with_banks", "FOF_FFYF440L500", "負債・預け金"),
        ("liabilities_equity_investment_trusts", "FOF_FFYF440L334", "負債・株式等・投資信託受益証券"),
        ("liabilities_loans", "FOF_FFYF440L200", "負債・貸出"),
        ("liabilities_net_lending", "FOF_FFYF440L700", "負債・資金過不足"),
        ("liabilities_other", "FOF_FFYF440L600", "負債・その他"),
        ("liabilities_total", "FOF_FFYF440L900", "負債・合計"),
    ],
    ('npish', 'stock'): [
        ("assets_accounts_receivable_payable", "FOF_FFYS440A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYS440A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYS440A300", "資産・債務証券"),
        ("assets_equity_investment_trusts", "FOF_FFYS440A334", "資産・株式等・投資信託受益証券"),
        ("assets_loans", "FOF_FFYS440A200", "資産・貸出"),
        ("assets_other", "FOF_FFYS440A600", "資産・その他"),
        ("assets_total", "FOF_FFYS440A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYS440A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYS440L520", "負債・未収・未払金"),
        ("liabilities_deposits_with_banks", "FOF_FFYS440L500", "負債・預け金"),
        ("liabilities_equity_investment_trusts", "FOF_FFYS440L334", "負債・株式等・投資信託受益証券"),
        ("liabilities_loans", "FOF_FFYS440L200", "負債・貸出"),
        ("liabilities_net_financial_assets", "FOF_FFYS440L700", "負債・金融資産･負債差額"),
        ("liabilities_other", "FOF_FFYS440L600", "負債・その他"),
        ("liabilities_total", "FOF_FFYS440L900", "負債・合計"),
    ],
    ('row', 'flow'): [
        ("assets_accounts_receivable_payable", "FOF_FFYF500A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYF500A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYF500A300", "資産・債務証券"),
        ("assets_deposits_with_banks", "FOF_FFYF500A500", "資産・預け金"),
        ("assets_equity_investment_trusts", "FOF_FFYF500A334", "資産・株式等・投資信託受益証券"),
        ("assets_financial_derivatives", "FOF_FFYF500A340", "資産・金融派生商品・雇用者ストックオプション"),
        ("assets_insurance_pension", "FOF_FFYF500A400", "資産・保険・年金・定型保証"),
        ("assets_loans", "FOF_FFYF500A200", "資産・貸出"),
        ("assets_other", "FOF_FFYF500A600", "資産・その他"),
        ("assets_other_external_claims", "FOF_FFYF500A550", "資産・その他対外債権債務"),
        ("assets_total", "FOF_FFYF500A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYF500A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYF500L520", "負債・未収・未払金"),
        ("liabilities_currency_deposits", "FOF_FFYF500L100", "負債・現金・預金"),
        ("liabilities_deposits_with_banks", "FOF_FFYF500L500", "負債・預け金"),
        ("liabilities_direct_investment_abroad", "FOF_FFYF500L530", "負債・対外直接投資"),
        ("liabilities_financial_derivatives", "FOF_FFYF500L340", "負債・金融派生商品・雇用者ストックオプション"),
        ("liabilities_foreign_reserves_memo", "FOF_FFYF500L551", "負債・外貨準備（再掲）"),
        ("liabilities_loans", "FOF_FFYF500L200", "負債・貸出"),
        ("liabilities_net_lending", "FOF_FFYF500L700", "負債・資金過不足"),
        ("liabilities_other", "FOF_FFYF500L600", "負債・その他"),
        ("liabilities_other_external_claims", "FOF_FFYF500L550", "負債・その他対外債権債務"),
        ("liabilities_portfolio_investment_abroad", "FOF_FFYF500L540", "負債・対外証券投資"),
        ("liabilities_total", "FOF_FFYF500L900", "負債・合計"),
        ("liabilities_trade_credit", "FOF_FFYF500L510", "負債・企業間・貿易信用"),
    ],
    ('row', 'stock'): [
        ("assets_accounts_receivable_payable", "FOF_FFYS500A520", "資産・未収・未払金"),
        ("assets_currency_deposits", "FOF_FFYS500A100", "資産・現金・預金"),
        ("assets_debt_securities", "FOF_FFYS500A300", "資産・債務証券"),
        ("assets_deposits_with_banks", "FOF_FFYS500A500", "資産・預け金"),
        ("assets_equity_investment_trusts", "FOF_FFYS500A334", "資産・株式等・投資信託受益証券"),
        ("assets_financial_derivatives", "FOF_FFYS500A340", "資産・金融派生商品・雇用者ストックオプション"),
        ("assets_insurance_pension", "FOF_FFYS500A400", "資産・保険・年金・定型保証"),
        ("assets_loans", "FOF_FFYS500A200", "資産・貸出"),
        ("assets_other", "FOF_FFYS500A600", "資産・その他"),
        ("assets_other_external_claims", "FOF_FFYS500A550", "資産・その他対外債権債務"),
        ("assets_total", "FOF_FFYS500A900", "資産・合計"),
        ("assets_trade_credit", "FOF_FFYS500A510", "資産・企業間・貿易信用"),
        ("liabilities_accounts_receivable_payable", "FOF_FFYS500L520", "負債・未収・未払金"),
        ("liabilities_currency_deposits", "FOF_FFYS500L100", "負債・現金・預金"),
        ("liabilities_deposits_with_banks", "FOF_FFYS500L500", "負債・預け金"),
        ("liabilities_direct_investment_abroad", "FOF_FFYS500L530", "負債・対外直接投資"),
        ("liabilities_financial_derivatives", "FOF_FFYS500L340", "負債・金融派生商品・雇用者ストックオプション"),
        ("liabilities_foreign_reserves_memo", "FOF_FFYS500L551", "負債・外貨準備（再掲）"),
        ("liabilities_loans", "FOF_FFYS500L200", "負債・貸出"),
        ("liabilities_net_financial_assets", "FOF_FFYS500L700", "負債・金融資産･負債差額"),
        ("liabilities_other", "FOF_FFYS500L600", "負債・その他"),
        ("liabilities_other_external_claims", "FOF_FFYS500L550", "負債・その他対外債権債務"),
        ("liabilities_portfolio_investment_abroad", "FOF_FFYS500L540", "負債・対外証券投資"),
        ("liabilities_total", "FOF_FFYS500L900", "負債・合計"),
        ("liabilities_trade_credit", "FOF_FFYS500L510", "負債・企業間・貿易信用"),
    ],
}


def boj_fof_sector_series() -> list[Series]:
    out: list[Series] = []
    for (sec, kind), rows in FOF_SERIES.items():
        sname, kname = FOF_SECTOR[sec], FOF_KIND[kind]
        for meas, code, item in rows:
            out.append(_s(
                series_id=f"boj.fof.{kind}.{meas}.{sec}.fy",
                title=f"資金循環統計 {item}（{sname}・{kname}・年度・億円）",
                org="boj", org_name="日本銀行", source_url="https://www.stat-search.boj.or.jp/",
                sector="物価・金融", unit="億円", granularity="年度", freq="fy",
                dataset="fof", measure=f"{kind}.{meas}", dims=sec,
                stat_name="資金循環統計（２００８ＳＮＡベース）", table_id=FOF_FILE, table_title="資金循環（年度・系列名称入り）",
                policy_tags=(TAG_FIN, TAG_MACRO), first_period="FY1979", status="registered",
                accessor={"type": "boj_flat", "zip": FOF_ZIP, "file": FOF_FILE, "layout": "wide",
                          "code": code, "period_kind": "fy_yyyy", "first_col": 3},
                citation_template="日本銀行「資金循環統計（２００８ＳＮＡベース）」（" + f"{FOF_FILE}・{code}・" + "{item}／{period}）取得 {retrieved_at}",
                notes=FOF_NOTE))
    return out


# ---------------------------------------------------------------- 第 8 弾 第 2 便（2026-08-24）：F-4 固定資本ストックマトリックス／F-6 労調 雇用形態別／F-7 社会保障費用統計
# F-4 付表(22) 固定資本ストックマトリックス（名目）。年ごとに 1 シート・行＝資産分類・列＝一国計／制度部門別／経済活動別の**3 段見出し（結合セル）**。
# 列キーは見出し 6・7・8 行を結合セル解決のうえ連結（accessor `esri_xlsx_yearsheets`・2026-08-24 に 31 シートで一意を確認）。
FCS_ASSETS = [("housing", "１．住宅"), ("other_buildings_structures", "２．その他の建物・構築物"),
              ("nonresidential_buildings", "（１）住宅以外の建物"), ("structures", "（２）構築物"),
              ("machinery_equipment", "３．機械・設備"), ("transport_equipment", "（１）輸送用機械"),
              ("ict_equipment", "（２）情報通信機器"), ("other_machinery", "（３）その他の機械・設備"),
              ("defence_equipment", "４．防衛装備品"), ("cultivated_biological", "５．育成生物資源"),
              ("intellectual_property", "６．知的財産生産物"), ("research_development", "（１）研究・開発"),
              ("software", "（３）コンピュータソフトウェア"), ("total", "固定資産合計")]
FCS_COLUMNS = [("total", "一国計一国計", "一国計"), ("nfc", "制度部門別非金融法人企業", "非金融法人企業"),
               ("nfc_private", "非金融法人企業民間", "非金融法人企業（民間）"), ("nfc_public", "非金融法人企業公的", "非金融法人企業（公的）"),
               ("fin", "金融機関", "金融機関"), ("gg", "一般政府", "一般政府"),
               ("hh", "家計（個人企業を含む）", "家計（個人企業を含む）"), ("npish", "対家計民間非営利団体対家計民間非営利団体", "対家計民間非営利団体")]
FCS_NOTE = ("付表(22) 固定資本ストックマトリックス（名目）。**暦年末**の残高・10 億円。資産分類 × 制度部門（経済活動別の列は未収録）。"
            "利用側の『民間企業設備』は nfc_private の固定資産合計に対応する。期末貸借対照表（cao.sna_sector_bs.fixed_assets.*）とは"
            "同じ固定資産でも表が違う（こちらは資産別の内訳を持つ）。")


def sna_fixed_capital_stock_series() -> list[Series]:
    out: list[Series] = []
    for aslug, rlabel in FCS_ASSETS:
        for cslug, ckey, cname in FCS_COLUMNS:
            out.append(_s(series_id=f"cao.sna_fcs.{aslug}.{cslug}.a",
                          title=f"国民経済計算（2020年基準・2008SNA）年次推計 固定資本ストック {rlabel.split('．')[-1]}（{cname}・暦年末）",
                          org="cao", org_name="内閣府", source_url=ESRI_TOP, sector="マクロ", unit="10億円", granularity="暦年", freq="a",
                          dataset="sna_fcs", measure=aslug, dims=cslug, basis="2020年基準・2008SNA", edition=f"{SNA_EDITION}年度年次推計（確報）",
                          stat_name="国民経済計算年次推計", table_id=f"{SNA_EDITION}ss4n", table_title="付表(22) 固定資本ストックマトリックス（名目）",
                          policy_tags=(TAG_MACRO, TAG_IND), period_converter="esri_year_paren", first_period="1994",
                          accessor={"type": "esri_xlsx_yearsheets", "edition": SNA_EDITION,
                                    "file": "kakuhou/files/{Y}/tables/{Y}ss4n_jp.xlsx", "year_cell": "A5",
                                    "header_rows": [6, 7, 8], "col_header": ckey, "row_label": rlabel},
                          status="registered",
                          citation_template=f"内閣府「国民経済計算年次推計」付表(22) 固定資本ストックマトリックス（名目）（{SNA_EDITION}年度確報・2020年基準・2008SNA・" + "{item}／{period}）取得 {retrieved_at}",
                          notes=FCS_NOTE))
    return out


# F-6 労働力調査 詳細集計（0003006361）：雇用形態 × 年齢階級。四半期 2002Q1〜。正規/非正規の別が本命。
ROUDOU_EMP_TYPE = [("employees", "00", "雇用者"), ("employees_ex_officers", "02", "役員を除く雇用者"),
                   ("regular", "03", "正規の職員・従業員"), ("nonregular", "10", "非正規の職員・従業員"),
                   ("part_time", "04", "パート・アルバイト"), ("dispatched", "07", "労働者派遣事業所の派遣社員"),
                   ("contract", "08", "契約社員・嘱託")]
ROUDOU_EMP_NOTE = ("労働力調査 詳細集計「年齢階級，教育，雇用形態別雇用者数」（四半期・万人）。年齢階級＝総数・教育＝総数・男女計。"
                   "**役員を除く雇用者 ＝ 正規 ＋ 非正規**（正規/非正規は役員を除く雇用者の内訳）。パート・アルバイト／派遣／契約は非正規の内訳。"
                   "基本集計の就業者・雇用者（soumu.roudou.*・月次）とは集計が違う＝混ぜない。")


def roudou_emp_type_series() -> list[Series]:
    sid = "0003006361"
    return [_s(series_id=f"soumu.roudou_emp.{meas}.total.q",
               title=f"労働力調査 詳細集計 {ttl}（年齢計・男女計・四半期）", org="soumu", org_name="総務省",
               source_url=ESTAT_URL.format(sid=sid), sector="家計・労働", unit="万人", granularity="四半期", freq="q",
               dataset="roudou_emp", measure=meas, dims="total",
               stat_name="労働力調査 詳細集計", table_id=sid, table_title="年齢階級，教育，雇用形態別雇用者数(2002年1-3月期～)",
               policy_tags=(TAG_LABOR,), period_converter="estat_quarter", first_period="2002Q1", status="registered",
               accessor={"type": "estat", "statsDataId": sid, "cdCat01": "00", "cdCat02": "02", "cdCat03": code, "cdCat04": "00", "cdCat05": "0"},
               citation_template=f"総務省「労働力調査」詳細集計 年齢階級，教育，雇用形態別雇用者数（e-Stat {sid}・" + "{item}／{period}）取得 {retrieved_at}",
               notes=ROUDOU_EMP_NOTE)
            for meas, code, ttl in ROUDOU_EMP_TYPE]


# F-7 社会保障費用統計（0004029986）：社会保障給付費の部門別推移 1964〜2023年度。
SHAHO_SECTORS = [("total", "11", "合計"), ("medical", "12", "医療"), ("pension", "14", "年金"),
                 ("welfare_other", "15", "福祉その他"), ("long_term_care", "16", "福祉その他_介護対策（再掲）")]
SHAHO_NOTE = ("社会保障費用統計（ILO 基準）時系列表 社会保障給付費の部門別推移。年度・億円。"
              "介護対策は「福祉その他」の**再掲**＝合計に足し込まない。給付費であって社会支出（OECD 基準）とは範囲が違う。")


def shaho_series() -> list[Series]:
    sid = "0004029986"
    return [_s(series_id=f"ipss.shaho.benefits.{slug}.fy",
               title=f"社会保障費用統計 社会保障給付費 {ttl}（年度）", org="ipss", org_name="国立社会保障・人口問題研究所",
               source_url=ESTAT_URL.format(sid=sid), sector="財政", unit="億円", granularity="年度", freq="fy",
               dataset="shaho", measure="benefits", dims=slug,
               stat_name="社会保障費用統計", table_id=sid, table_title="時系列表 社会保障給付費の部門別推移 1964〜2023年度",
               policy_tags=(TAG_SOC, TAG_FISCAL), period_converter="estat_cpi_time", first_period="FY1964", status="registered",
               accessor={"type": "estat", "statsDataId": sid, "cdTab": "901", "cdCat01": code},
               citation_template=f"国立社会保障・人口問題研究所「社会保障費用統計」時系列表 社会保障給付費の部門別推移（e-Stat {sid}・" + "{item}／{period}）取得 {retrieved_at}",
               notes=SHAHO_NOTE)
            for slug, code, ttl in SHAHO_SECTORS]


# ---------------------------------------------------------------- 第4弾：SNA 他表・QE・日銀 gap
def _sna_row(measure, table, title, unit, row_label, freq, sheet="実数", occurrence=0, tags=(TAG_MACRO,), notes="", first_period=None, kind_label=""):
    """国民経済計算 年次推計 の xlsx 1行＝1系列。table 例 ffm1n(年度・名目) fcm1n(暦年・名目) ffm2(年度・分配) s18(付表18)。"""
    gran = {"fy": "年度", "a": "暦年"}[freq]
    conv = "esri_year_fy" if freq == "fy" else "esri_year_a"
    acc = {"type": "esri_xlsx", "edition": SNA_EDITION, "file": "kakuhou/files/{Y}/tables/{Y}" + f"{table}_jp.xlsx",
           "sheet": sheet, "row_label": row_label, "header_row": 7, "first_col": 2}
    if occurrence:
        acc["occurrence"] = occurrence
    return _s(series_id=f"cao.sna2020.{measure}.{freq}", title=f"国民経済計算（2020年基準・2008SNA）年次推計 {title}（{gran}）",
              org="cao", org_name="内閣府", source_url=ESRI_TOP, sector="マクロ", unit=unit, granularity=gran, freq=freq,
              dataset="sna2020", measure=measure, basis="2020年基準・2008SNA", edition=f"{SNA_EDITION}年度年次推計（確報）",
              stat_name="国民経済計算年次推計", table_id=f"{SNA_EDITION}{table}", table_title=kind_label or table,
              policy_tags=tuple(tags), period_converter=conv, first_period=first_period or ("FY1994" if freq == "fy" else "1994"),
              accessor=acc, status="registered",
              citation_template=f"内閣府「国民経済計算年次推計」{kind_label or table}（{SNA_EDITION}年度確報・2020年基準・2008SNA・" + "{item}／{period}）取得 {retrieved_at}",
              notes=notes)


def sna_series() -> list[Series]:
    out: list[Series] = []
    # 支出側（主要系列表(1)）名目 ffm1n(年度)/fcm1n(暦年)、実質 ffm1rn/fcm1rn、デフレーター fcm1dn
    EXP = [
        ("private_consumption", "１．民間最終消費支出", "民間最終消費支出", 0),
        ("hh_consumption", "（１）家計最終消費支出", "家計最終消費支出", 0),
        ("hh_consumption_ex_imputed_rent", "家計最終消費支出（除く持ち家の帰属家賃）", "家計最終消費支出（除く持ち家の帰属家賃）", 0),
        ("gov_consumption", "２．政府最終消費支出", "政府最終消費支出", 0),
        ("gross_capital_formation", "３．総資本形成", "総資本形成", 0),
        ("gross_fixed_capital_formation", "（１）総固定資本形成", "総固定資本形成", 0),
        ("priv_fixed_capital_formation", "ａ．民間", "民間 総固定資本形成", 0),
        ("priv_housing", "（ａ）住宅", "民間住宅", 1),
        ("priv_capex", "（ｂ）企業設備", "民間企業設備", 1),
        ("public_fixed_capital_formation", "ｂ．公的", "公的 総固定資本形成", 1),
        ("public_capex", "（ｂ）企業設備", "公的企業設備", 2),
        ("gov_fixed_capital_formation", "（ｃ）一般政府", "一般政府 総固定資本形成", 0),
        ("inventory_change", "（２）在庫変動", "在庫変動", 0),
        ("net_exports", "４．財貨・サービスの純輸出", "財貨・サービスの純輸出", 0),
        ("exports", "（１）財貨・サービスの輸出", "財貨・サービスの輸出", 0),
        ("imports", "（２）（控除）財貨・サービスの輸入", "財貨・サービスの輸入", 0),
        ("net_income_from_abroad", "（参考）海外からの所得の純受取", "海外からの所得の純受取", 0),
        ("gni", "国民総所得", "国民総所得", 0),
        ("domestic_demand", "（参考）国内需要", "国内需要", 0),
        ("private_demand", "民間需要", "民間需要", 0),
        ("public_demand", "公的需要", "公的需要", 0),
    ]
    for freq, tbl_n, tbl_r in (("fy", "ffm1n", "ffm1rn"), ("a", "fcm1n", "fcm1rn")):
        for meas, lab, ttl, occ in EXP:
            out.append(_sna_row(meas, tbl_n, f"{ttl}（名目）", "10億円", lab, freq, occurrence=occ,
                                kind_label="主要系列表(1) 国内総生産（支出側） 名目"))
        # 実質（連鎖方式）は主要項目のみ（実質表に無い行はエラーになるので確認済みの行だけ）
        for meas, lab, ttl, occ in EXP:
            if meas in ("private_consumption", "hh_consumption", "gov_consumption", "gross_fixed_capital_formation", "priv_housing",
                        "priv_capex", "public_fixed_capital_formation", "exports", "imports", "domestic_demand", "private_demand", "public_demand"):
                out.append(_sna_row(f"{meas}_real", tbl_r, f"{ttl}（実質・連鎖方式・2020暦年連鎖価格）", "10億円", lab, freq, occurrence=occ,
                                    kind_label="主要系列表(1) 国内総生産（支出側） 実質：連鎖方式",
                                    notes="連鎖方式＝内訳の加法性なし（合計と内訳の和は一致しない）。"))
    # 暦年の GDP 3 系列（年度は既存 sna_gdp_fy）
    out.append(_sna_row("gdp_nominal", "fcm1n", "名目国内総生産（支出側）", "10億円", "５．　国内総生産（支出側）(1+2+3+4)", "a",
                        kind_label="主要系列表(1) 国内総生産（支出側） 名目"))
    out.append(_sna_row("gdp_real", "fcm1rn", "実質国内総生産（支出側・連鎖方式・2020暦年連鎖価格）", "10億円", "５．　国内総生産（支出側）", "a",
                        kind_label="主要系列表(1) 国内総生産（支出側） 実質：連鎖方式"))
    out.append(_sna_row("deflator", "fcm1dn", "GDPデフレーター（支出側・連鎖方式）", "指数（2020暦年=100）", "５．　国内総生産（支出側）", "a",
                        kind_label="主要系列表(1) 国内総生産（支出側） デフレーター"))
    # 分配側（主要系列表(2) 国民所得・国民可処分所得の分配）ffm2(年度)/fcm2(暦年)
    DIST = [
        ("compensation_employees", "１．雇用者報酬", "雇用者報酬", (TAG_LABOR, TAG_MACRO)),
        ("wages_salaries", "（１）賃金・俸給", "賃金・俸給", (TAG_LABOR, TAG_MACRO)),
        ("employer_social_contributions", "（２）雇主の社会負担", "雇主の社会負担", (TAG_LABOR, TAG_SOC)),
        ("property_income_nonenterprise", "２．財産所得（非企業部門）", "財産所得（非企業部門）", (TAG_MACRO, TAG_FIN)),
        ("corporate_income", "３．企業所得（企業部門の第１次所得バランス）", "企業所得（企業部門の第１次所得バランス）", (TAG_MACRO, TAG_CORP)),
        ("national_income", "４．国民所得（要素費用表示）(1+2+3)", "国民所得（要素費用表示）", (TAG_MACRO,)),
        ("taxes_less_subsidies_products", "５．生産・輸入品に課される税（控除）補助金", "生産・輸入品に課される税（控除）補助金", (TAG_MACRO, TAG_FISCAL)),
        ("national_income_market", "６．国民所得（市場価格表示）(4+5)", "国民所得（市場価格表示）", (TAG_MACRO,)),
        ("national_disposable_income", "８．国民可処分所得(6+7)", "国民可処分所得", (TAG_MACRO,)),
    ]
    for freq, tbl in (("fy", "ffm2"), ("a", "fcm2")):
        for meas, lab, ttl, tags in DIST:
            out.append(_sna_row(meas, tbl, ttl, "10億円", lab, freq, tags=tags, kind_label="主要系列表(2) 国民所得・国民可処分所得の分配"))
    # 付表(18) 制度部門別の純貸出(+)/純借入(-)＝資本勘定側（１．）を採る（２．資金過不足は金融勘定側＝別）
    for freq, sheet in (("fy", "年度・実数"), ("a", "暦年・実数")):
        for sec, lab, ttl in [("total", "１．純貸出(+)／純借入(-)", "国内合計"), ("nfc", "（１）非金融法人企業", "非金融法人企業"),
                              ("fin", "（２）金融機関", "金融機関"), ("gg", "（３）一般政府", "一般政府"),
                              ("hh", "（４）家計（個人企業を含む）", "家計（個人企業を含む）"), ("npish", "（５）対家計民間非営利団体", "対家計民間非営利団体"),
                              ("row", "（６）海外部門", "海外部門")]:
            out.append(_sna_row(f"net_lending.{sec}", "s18", f"制度部門別 純貸出(+)/純借入(-) {ttl}", "10億円", lab, freq, sheet=sheet,
                                occurrence=(1 if sec != "total" else 0), tags=(TAG_MACRO, TAG_FISCAL),
                                kind_label=f"付表(18) 制度部門別の純貸出(+)／純借入(-)（{sheet}）",
                                notes="資本勘定側（１．純貸出/純借入）。金融勘定側の資金過不足は net_lending_financial.*（２．）。IS バランスの構成系列。"))
            # ２．純貸出／純借入（資金過不足）＝**金融勘定側**（2026-08-24・第 8 弾 第 2 便）。同じ行ラベルの 2 回目＝occurrence=2。
            # 日銀 資金循環（boj.fof.flow.liabilities_net_lending.*）と対応する概念はこちら（ただし JSNA 側で部門分類等の調整が入るため一致はしない）。
            out.append(_sna_row(f"net_lending_financial.{sec}", "s18", f"制度部門別 純貸出(+)/純借入(-)（資金過不足・金融勘定側） {ttl}", "10億円", lab, freq, sheet=sheet,
                                occurrence=(2 if sec != "total" else 0), tags=(TAG_MACRO, TAG_FIN),
                                kind_label=f"付表(18) 制度部門別の純貸出(+)／純借入(-)（{sheet}）",
                                notes="**金融勘定側**（２．純貸出/純借入（資金過不足））。資本勘定側は net_lending.*（１．）＝統計上の不突合の分だけ両者は一致しない。"
                                      "日銀 資金循環統計（boj.fof.flow.liabilities_net_lending.<部門>.fy・億円）と概念は対応するが、"
                                      "JSNA 側で部門分類等の調整が入るため値は一致しない（FY2024 非金融法人：JSNA 23,927.6 十億円 vs 資金循環 250,321 億円＝25,032.1 十億円）。"))
    # 第 7 弾 補遺（2026-08-22・要件源＝利用側プロジェクト §5.5 (c)）：制度部門別の営業余剰（総/純）・固定資本減耗＝Ⅱ. 制度部門別所得支出勘定
    # i2 非金融法人・i3 金融機関・i4 一般政府・i5 家計（営業余剰(持ち家)・混合所得の別建て）・i6 NPISH。第 1 次所得の配分勘定（シート（１））。
    # 「（控除）固定資本減耗」は各表に 2 行（第 1 次所得バランスの再掲と営業余剰の再掲）＝同値・occurrence=1。
    # 用途＝法人企業統計の営業利益と利潤側で挟む（労働側＝人件費÷雇用者報酬の独立検証）。**一致はしない**（FISIM 調整・帰属家賃・
    # 法人企業統計は金融保険業除外・SNA は持株会社の控除なし）＝ずれの構造が安定しているかを見る。
    NOTE_I = ("Ⅱ. 制度部門別所得支出勘定 第 1 次所得の配分勘定。法人企業統計（mof.hojin）の営業利益・営業純益と**一致しない**："
              "SNA は FISIM 調整後・帰属家賃込み・全法人（金融保険業を含む）・個人企業は家計部門。ずれの水準ではなく構造の安定性を見る。"
              "制度部門別の GDP（生産側）そのものは JSNA 非公表（所得の発生勘定は一国経済のみ）＝経済活動別は cao.sna_activity。")
    SECT = [("nfc", "i2", "非金融法人企業", True, False), ("fin", "i3", "金融機関", True, False), ("gg", "i4", "一般政府", False, False),
            ("hh", "i5", "家計（個人企業を含む）", False, True), ("npish", "i6", "対家計民間非営利団体", False, False)]
    for freq, sheet in (("fy", "年度（１）"), ("a", "暦年（１）")):
        for sec, tbl, ttl, has_os, is_hh in SECT:
            kl = f"制度部門別所得支出勘定 {ttl}（{sheet}）"
            out.append(_sna_row(f"cfc_sector.{sec}", tbl, f"制度部門別 固定資本減耗 {ttl}", "10億円", "（控除）固定資本減耗", freq, sheet=sheet,
                                occurrence=1, tags=(TAG_MACRO, TAG_CORP), kind_label=kl, notes=NOTE_I))
            if has_os:
                out.append(_sna_row(f"operating_surplus_gross.{sec}", tbl, f"制度部門別 営業余剰（総） {ttl}", "10億円", "（再掲）営業余剰（総）", freq, sheet=sheet,
                                    tags=(TAG_MACRO, TAG_CORP), kind_label=kl, notes=NOTE_I + " 総＝固定資本減耗を控除する前（法人企業統計の 営業利益＋減価償却費 に近い概念）。"))
                out.append(_sna_row(f"operating_surplus_net.{sec}", tbl, f"制度部門別 営業余剰（純） {ttl}", "10億円", "1.3営業余剰（純）", freq, sheet=sheet,
                                    tags=(TAG_MACRO, TAG_CORP), kind_label=kl, notes=NOTE_I + " 純＝総−固定資本減耗。"))
            if is_hh:
                for meas, lab, t2 in [("operating_surplus_mixed_income_gross.hh", "（再掲）営業余剰・混合所得（総）", "営業余剰・混合所得（総）"),
                                      ("operating_surplus_mixed_income_net.hh", "1.3営業余剰・混合所得（純）", "営業余剰・混合所得（純）"),
                                      ("operating_surplus_owner_occupied_gross.hh", "（１）営業余剰（持ち家）（総）", "営業余剰（持ち家）（総）"),
                                      ("operating_surplus_owner_occupied_net.hh", "（１）営業余剰（持ち家）（純）", "営業余剰（持ち家）（純）"),
                                      ("mixed_income_gross.hh", "（２）混合所得（総）", "混合所得（総）"),
                                      ("mixed_income_net.hh", "（２）混合所得（純）", "混合所得（純）")]:
                    out.append(_sna_row(meas, tbl, f"制度部門別 {t2} {ttl}", "10億円", lab, freq, sheet=sheet, tags=(TAG_MACRO, TAG_CORP), kind_label=kl,
                                        notes=NOTE_I + " 家計部門の営業余剰は持ち家の帰属家賃のみ。個人企業の事業所得は混合所得（労働報酬と資本所得が分離できない）。"))
    return out


# ---------------------------------------------------------------- 第5弾：旧基準の制度部門別（現行と**接続しない**別系列）
# 基準改定（68SNA→93SNA→2008SNA）で定義も水準も変わるため、旧基準は dataset を分けた独立系列として持つ。
# 系列をまたいで繋ぐ（接続する）ことはしない＝利用者が基準を意識せずに長期系列を作れてしまう状態を作らない。
LEGACY_NOTE = ("旧基準の系列。基準改定で定義・推計方法・水準が変わるため、現行（2020年基準・2008SNA）や"
               "他基準の系列と**接続しない**（同一の指標として繋がない）。海外部門は海外からみた符号＝日本の黒字はマイナス。")


def _sna_legacy(dataset: str, basis: str, edition: str, top: str, file: str, table_id: str, kind_label: str,
                measure: str, title: str, row_label: str, freq: str, sheet: str, first_period: str,
                occurrence: int = 0, unit: str = "10億円", tags=(TAG_MACRO, TAG_FISCAL),
                superseded_by: str = "", notes: str = "") -> Series:
    """旧基準（.xls）の年次推計 1行＝1系列。レイアウトは現行 xlsx と同じ（行ラベル×見出し行の西暦）。"""
    gran = {"fy": "年度", "a": "暦年"}[freq]
    acc = {"type": "esri_xls", "file": file, "sheet": sheet, "row_label": row_label, "header_row": 7, "first_col": 2}
    if occurrence:
        acc["occurrence"] = occurrence
    return _s(series_id=f"cao.{dataset}.{measure}.{freq}", title=f"国民経済計算（{basis}）{title}（{gran}）",
              org="cao", org_name="内閣府", source_url=top, sector="マクロ", unit=unit, granularity=gran, freq=freq,
              dataset=dataset, measure=measure, basis=basis, edition=edition,
              stat_name="国民経済計算確報（旧基準）", table_id=table_id, table_title=f"{kind_label}（{gran}・実数）",
              policy_tags=tuple(tags), period_converter="esri_year_fy" if freq == "fy" else "esri_year_a",
              first_period=first_period, accessor=acc, status="registered", superseded_by=superseded_by,
              citation_template=f"内閣府「国民経済計算確報（{basis}）」{kind_label}（{edition}・{{item}}／{{period}}）取得 {{retrieved_at}}",
              notes=(notes + " " if notes else "") + LEGACY_NOTE)


def sna_legacy_series() -> list[Series]:
    out: list[Series] = []
    # (1) 2000年基準・93SNA（2009年度確報）付表19 制度部門別の純貸出(+)/純借入(-)：1980〜2009
    S19 = [("total", "１．純貸出(+)／純借入(-)", "国内合計", 0), ("nfc", "（１）非金融法人企業", "非金融法人企業", 1),
           ("fin", "（２）金融機関", "金融機関", 1), ("gg", "（３）一般政府", "一般政府", 1),
           ("hh", "（４）家計（個人企業を含む）", "家計（個人企業を含む）", 1), ("npish", "（５）対家計民間非営利団体", "対家計民間非営利団体", 1),
           ("row", "（６）海外部門", "海外部門", 1), ("discrepancy", "（７）統計上の不突合", "統計上の不突合", 1)]
    for freq, sheet, fp in (("fy", "年度・実数", "FY1980"), ("a", "暦年・実数", "1980")):
        for sec, lab, ttl, occ in S19:
            out.append(_sna_legacy(
                "sna2000", "2000年基準・93SNA", "2009年度国民経済計算確報（1980年〜2009年）",
                "https://www.esri.cao.go.jp/jp/sna/data/data_list/kakuhou/files/h21/h21_kaku_top.html",
                "kakuhou/files/h21/tables/21s19_jp.xls", "21s19", "付表19 制度部門別の純貸出(+)／純借入(-)",
                f"net_lending.{sec}", f"2009年度確報 制度部門別 純貸出(+)/純借入(-) {ttl}", lab, freq, sheet, fp,
                occurrence=occ, superseded_by=f"cao.sna2020.net_lending.{sec}.{freq}" if sec != "discrepancy" else "",
                notes="資本勘定側（１．）を採る（２．資金過不足＝金融勘定側は別）。"))
    # (2) 1990年基準・68SNA（1998年度確報）付表3 制度部門別の貯蓄投資バランス：1970〜1998
    R3 = [("nfc", "非金融法人企業"), ("fin", "金融機関"), ("gg", "一般政府"),
          ("npish", "対家計民間非営利団体"), ("hh", "家計"), ("row", "海外部門"), ("discrepancy", "統計上の不突合")]
    for freq, sheet, fp in (("fy", "年度・実数", "FY1970"), ("a", "暦年・実数", "1970")):
        for sec, lab in R3:
            out.append(_sna_legacy(
                "sna1990", "1990年基準・68SNA", "1998年度国民経済計算確報（付表は1970年〜1998年）",
                "https://www.esri.cao.go.jp/jp/sna/data/data_list/kakuhou/files/h10/12annual_report_j.html",
                "kakuhou/files/h10/tables/70r3.xls", "70r3", "付表3 制度部門別の貯蓄投資バランス",
                f"saving_investment_balance.{sec}", f"1998年度確報 制度部門別 貯蓄投資差額 {lab}", lab, freq, sheet, fp,
                occurrence=1, superseded_by=f"cao.sna2000.net_lending.{sec}.{freq}" if sec != "discrepancy" else "",
                notes="68SNA の呼称は「貯蓄投資差額」（資本勘定側）。２．資金過不足（金融勘定側）は別。家計は個人企業を含む。"))
    # (3) 1990年基準・68SNA 統合勘定「資本調達勘定」：1955〜1998（部門別ではないが IS バランスの恒等式そのもの）
    A3 = [("gfcf", "3. 1　国内総固定資本形成 (1.9)", "国内総固定資本形成"),
          ("inventory_change", "3. 2　在庫品増加 (1.10)", "在庫品増加"),
          ("net_claims_abroad", "3. 3　海外に対する債権の純増 (4.2)", "海外に対する債権の純増"),
          ("saving", "3. 4　貯蓄 (2.3)", "貯蓄"),
          ("consumption_of_fixed_capital", "3. 5　固定資本減耗 (1.3)", "固定資本減耗"),
          ("capital_transfers_abroad", "3. 6　海外からの資本移転等（純）(6.2)", "海外からの資本移転等（純）"),
          ("discrepancy", "3. 7　統計上の不突合 (1.6)", "統計上の不突合")]
    for freq, sheet, fp in (("fy", "年度（１）実物", "FY1955"), ("a", "暦年（１）実物", "1955")):
        for meas, lab, ttl in A3:
            out.append(_sna_legacy(
                "sna1990", "1990年基準・68SNA", "1998年度国民経済計算確報（1955年〜1998年）",
                "https://www.esri.cao.go.jp/jp/sna/data/data_list/kakuhou/files/h10/12annual_report_j.html",
                "kakuhou/files/h10/tables/55a3.xls", "55a3", "統合勘定3 資本調達勘定（実物取引）",
                f"capital_account.{meas}", f"1998年度確報 統合勘定 資本調達勘定 {ttl}", lab, freq, sheet, fp,
                notes="一国計（制度部門別ではない）。海外に対する債権の純増＝国内部門合計の貯蓄投資差額。"))
    return out


# ---------------------------------------------------------------- 第5弾(c)：固定資本減耗（フロー編 付表16）と資本ストック（ストック編 付表2）
def sna_capital_series() -> list[Series]:
    """グロス投資と資本ストックの間を埋める2枚：付表(16) 民間・公的別の固定資本減耗／ストック編(2) 民間・公的別の資産・負債残高。

    総固定資本形成（フロー）から固定資本減耗を引いたものが純投資で、資本ストックの増分に対応する。
    グロス投資が過去最高でも純投資が薄ければストックは伸びない——その確認に要る2系列。
    """
    out: list[Series] = []
    CFC = [("total", "　　合　　計", "合計", 0), ("private", "１．民間", "民間", 0),
           ("private_corp", "（１）法人企業", "民間 法人企業", 0),
           ("private_corp_capex", "ｂ．企業設備", "民間 法人企業 企業設備", 1),
           ("private_hh_capex", "ｂ．企業設備", "民間 家計（個人企業）・対家計民間非営利団体 企業設備", 2),
           ("public", "２．公的", "公的", 0), ("gov", "（２）一般政府", "公的 一般政府", 0)]
    for freq, sheet in (("fy", "年度"), ("a", "暦年")):
        for meas, lab, ttl, occ in CFC:
            out.append(_sna_row(f"cfc.{meas}", "s16", f"民間・公的別の固定資本減耗 {ttl}", "10億円", lab, freq,
                                sheet=sheet, occurrence=occ, tags=(TAG_MACRO, TAG_CORP),
                                kind_label=f"付表(16) 民間・公的別の固定資本減耗（{sheet}）",
                                notes="固定資本減耗＝既存資本ストックの毎期の目減り。総固定資本形成−固定資本減耗＝純投資。"))
    # ストック編：1シートに全年、見出しは2段（上＝暦年末・結合／下＝民間部門・公的部門・合計）
    STOCK = [("nonfinancial_assets", "１．非金融資産", "非金融資産"),
             ("produced_assets", "（１）生産資産", "生産資産"),
             ("fixed_assets", "ａ．固定資産", "固定資産（資本ストック）"),
             ("inventories", "ｂ．在庫", "在庫"),
             ("nonproduced_assets", "（２）非生産資産（自然資源）", "非生産資産（自然資源）"),
             ("land", "ａ．土地", "土地")]
    SECT = [("private", "民間部門"), ("public", "公的部門"), ("total", "合計")]
    for meas, lab, ttl in STOCK:
        for skey, sname in SECT:
            acc = {"type": "esri_xlsx", "edition": SNA_EDITION,
                   "file": "kakuhou/files/{Y}/tables/{Y}ss2_jp.xlsx", "sheet": "資産・負債残高",
                   "row_label": lab, "header_row": 5, "sub_header_row": 6, "sub_header": sname, "first_col": 2}
            out.append(_s(series_id=f"cao.sna2020.stock.{meas}.{skey}.a",
                          title=f"国民経済計算（2020年基準・2008SNA）ストック編 {ttl} {sname}（暦年末）",
                          org="cao", org_name="内閣府", source_url=ESRI_TOP, sector="マクロ", unit="10億円",
                          granularity="暦年（年末残高）", freq="a", dataset="sna2020", measure=f"stock.{meas}",
                          dims=skey, basis="2020年基準・2008SNA", edition=f"{SNA_EDITION}年度年次推計（ストック編）",
                          stat_name="国民経済計算年次推計", table_id=f"{SNA_EDITION}ss2",
                          table_title="ストック編(2) 民間・公的別の資産・負債残高",
                          policy_tags=(TAG_MACRO, TAG_CORP), period_converter="esri_year_paren", first_period="1994",
                          accessor=acc, status="registered",
                          citation_template=f"内閣府「国民経済計算年次推計」ストック編(2) 民間・公的別の資産・負債残高"
                                            f"（{SNA_EDITION}年度・2020年基準・2008SNA・{{item}}／{sname}／{{period}}）取得 {{retrieved_at}}",
                          notes="期末残高（ストック）。フローの総固定資本形成・固定資本減耗とは別勘定で、"
                                "残高の変動には資本取引のほかに調整勘定（価格変動等）が含まれる。"))
    return out


def qe_series() -> list[Series]:
    """四半期別GDP速報（QE）CSV。実額（名目 gaku-mk／実質 gaku-jk）＋デフレーター（def-qk 水準・rdef-qk 前期比・rdef-qg 前年同期比）＋
    実質寄与度（kiyo-jk）。edition＝公表回（例 2621＝2026年4-6月期 1次速報。ディレクトリは 1次＝qe{YY}{Q}・2次＝qe{YY}{Q}_2）。
    第 9 弾 段 2（2026-08-29）：デフレーター・寄与度は**内閣府公表値のまま**収録（自前計算しない＝近似禁止の原則）。
    edition は data/registry/qe_edition.json が正（段 3・stats.ops.qe_update が唯一の更新経路）。"""
    import json as _json
    from stats.core.paths import REGISTRY_DIR as _RD
    _ed = _json.loads((_RD / "qe_edition.json").read_text(encoding="utf-8"))
    QE_EDITION, QE_DIR, QE_YEAR, QE_LABEL = _ed["edition"], _ed["dir"], str(_ed["year"]), _ed["label"]
    QE_URL = "https://www.esri.cao.go.jp/jp/sna/data/data_list/sokuhou/files/files_sokuhou.html"
    EXP_ITEMS = [("gdp", "国内総生産(支出側)", "", "国内総生産（支出側）"),
                 ("private_consumption", "民間最終消費支出", "", "民間最終消費支出"),
                 ("hh_consumption", "民間最終消費支出", "家計最終消費支出", "家計最終消費支出"),
                 ("priv_housing", "民間住宅", "", "民間住宅"), ("priv_capex", "民間企業設備", "", "民間企業設備"),
                 ("gov_consumption", "政府最終消費支出", "", "政府最終消費支出"),
                 ("public_investment", "公的固定資本形成", "", "公的固定資本形成"),
                 ("exports", "財貨・サービス", "輸出", "財貨・サービスの輸出"), ("imports", "財貨・サービス", "輸入", "財貨・サービスの輸入")]
    out: list[Series] = []

    def _qe(sid: str, *, title: str, unit: str, measure: str, dims: str, sa: bool, f: str, kl: str,
            hdr: str, sub: str, first_period: str, notes: str) -> Series:
        return _s(series_id=sid, title=title, org="cao", org_name="内閣府", source_url=QE_URL,
                  sector="マクロ", unit=unit, granularity="四半期", freq="q", dataset="qe2020",
                  measure=measure, dims=dims, basis="2020年基準・2008SNA", seasonal_adjustment=sa,
                  edition=f"{QE_LABEL}（{QE_EDITION}）", stat_name="四半期別GDP速報", table_id=f"{f}{QE_EDITION}",
                  table_title=kl, policy_tags=(TAG_MACRO,), period_converter="esri_qe_quarter", first_period=first_period,
                  accessor={"type": "esri_qe_csv", "edition": QE_EDITION, "file": f"sokuhou/files/{QE_YEAR}/{QE_DIR}/tables/{f}{{Q}}.csv",
                            "col_header": hdr, "sub_header": sub, "header_row": 3, "sub_header_row": 4, "first_data_row": 8},
                  citation_template=f"内閣府「四半期別GDP速報」{kl}（{QE_LABEL} {f}{QE_EDITION}.csv・" + "{item}／{period}）取得 {retrieved_at}",
                  notes=notes, status="registered")

    for kind, f, kl in (("nominal", "gaku-mk", "名目季節調整系列"), ("real", "gaku-jk", "実質季節調整系列（連鎖方式・2020暦年連鎖価格）")):
        for meas, hdr, sub, ttl in EXP_ITEMS:
            out.append(_qe(f"cao.qe2020.{meas}_{kind}.sa.q",
                           title=f"四半期別GDP速報（QE） {ttl}（{'名目' if kind == 'nominal' else '実質・連鎖方式'}・季節調整系列・年率換算 10億円）",
                           unit="10億円（年率換算・季節調整済）", measure=f"{meas}_{kind}", dims="sa", sa=True, f=f, kl=kl,
                           hdr=hdr, sub=sub, first_period="1994Q1",
                           notes="季節調整済・年率換算。公表回ごとに過去値が改定される（vintage）。暦年四半期表記（2024Q1＝1-3月）。"))
    # デフレーター 3 表（第 9 弾 段 2）。需要項目 9＋国内需要。民間在庫変動・公的在庫変動・純輸出のデフレーターは原典が非公表（***）＝登録しない。
    DEF_ITEMS = EXP_ITEMS + [("domestic_demand", "国内需要", "", "国内需要")]
    for sfx, f, kl, unit, dims_, sa, fp, extra in (
            ("deflator", "def-qk", "四半期デフレーター季節調整系列", "指数（2020暦年=100・季節調整済）", "sa", True, "1994Q1", ""),
            ("deflator_qoq", "rdef-qk", "四半期デフレーター季節調整系列（前期比）", "％（前期比・季節調整済）", "sa", True, "1994Q2", ""),
            ("deflator_yoy", "rdef-qg", "四半期デフレーター原系列（前年同期比）", "％（前年同期比・原系列）", "orig", False, "1995Q1",
             "原系列の前年比（前年同期比・季節調整はかけない）。")):
        for meas, hdr, sub, ttl in DEF_ITEMS:
            out.append(_qe(f"cao.qe2020.{meas}_{sfx}.{dims_}.q",
                           title=f"四半期別GDP速報（QE） {ttl} デフレーター（{kl.replace('四半期デフレーター', '')}・2020暦年=100）",
                           unit=unit, measure=f"{meas}_{sfx}", dims=dims_, sa=sa, f=f, kl=kl, hdr=hdr, sub=sub, first_period=fp,
                           notes=(extra + "GDPデフレーター等の公表値そのまま（連鎖方式）。公表回ごとに過去値が改定される（vintage）。"
                                  "名目シェアは gaku-mk（*_nominal）から計算できる。実質GDP（数量）は *_real を参照。")))
    # 実質寄与度（kiyo-jk＝実質季節調整系列の前期比への寄与度・％pt）。GDP 列は実質GDP前期比（％）＝寄与度の合計欄。
    KIYO_ITEMS = [(m, h, s, t) for (m, h, s, t) in EXP_ITEMS if m != "gdp"] + [
        ("private_inventories", "民間在庫変動", "", "民間在庫変動"),
        ("public_inventories", "公的在庫変動", "", "公的在庫変動"),
        ("net_exports", "財貨・サービス", "純輸出", "財貨・サービスの純輸出"),
        ("domestic_demand", "国内需要", "", "国内需要")]
    kiyo_note = ("内閣府公表の寄与度（実質・季節調整済・前期比・％pt）＝自前の名目ウエイト計算は不要。"
                 "丸めのため寄与度の合計と実質GDP前期比（gdp_real_qoq）は末桁が一致しないことがある。公表回ごとに過去値が改定される（vintage）。")
    out.append(_qe("cao.qe2020.gdp_real_qoq.sa.q",
                   title="四半期別GDP速報（QE） 実質GDP前期比（季節調整系列・％）",
                   unit="％（前期比・季節調整済）", measure="gdp_real_qoq", dims="sa", sa=True,
                   f="kiyo-jk", kl="実質季節調整系列(寄与度)", hdr="国内総生産(支出側)", sub="", first_period="1994Q2",
                   notes="寄与度表（kiyo-jk）の国内総生産列＝実質GDP前期比の公表値。年率換算は *_qoq_ann（nritu 表）。"))
    # 年率換算 3 表（第 9 弾 補遺・2026-08-29 利用側要望）：実質年率 nritu-jk・名目年率 nritu-mk・年率寄与度 nkiyo-jk。
    # 在庫変動・純輸出の伸び率（nritu）は原典が非公表（***）＝登録しない。寄与度（nkiyo）にはある。
    for kind, f, kl in (("real", "nritu-jk", "実質季節調整系列(年率)"), ("nominal", "nritu-mk", "名目季節調整系列(年率)")):
        for meas, hdr, sub, ttl in EXP_ITEMS:
            out.append(_qe(f"cao.qe2020.{meas}_{kind}_qoq_ann.sa.q",
                           title=f"四半期別GDP速報（QE） {ttl}（{'名目' if kind == 'nominal' else '実質・連鎖方式'}・前期比年率・％）",
                           unit="％（前期比年率・季節調整済）", measure=f"{meas}_{kind}_qoq_ann", dims="sa", sa=True, f=f, kl=kl,
                           hdr=hdr, sub=sub, first_period="1994Q2",
                           notes=("前期比の年率換算（公表値のまま）。前期比は " + ("gdp_real_qoq（kiyo-jk）等" if kind == "real" else "デフレーター経由の自前計算不要＝この表が公表値")
                                  + "。公表回ごとに過去値が改定される（vintage）。")))
    kiyo_ann_note = ("内閣府公表の年率換算寄与度（実質・季節調整済・前期比年率・％pt）。丸めのため合計と実質GDP前期比年率"
                     "（gdp_real_qoq_ann）は末桁が一致しないことがある。公表回ごとに過去値が改定される（vintage）。")
    for meas, hdr, sub, ttl in KIYO_ITEMS:
        out.append(_qe(f"cao.qe2020.{meas}_real_contrib_ann.sa.q",
                       title=f"四半期別GDP速報（QE） {ttl} 実質GDP前期比年率への寄与度（季節調整系列・％pt）",
                       unit="％pt（実質・前期比年率・季節調整済）", measure=f"{meas}_real_contrib_ann", dims="sa", sa=True,
                       f="nkiyo-jk", kl="実質季節調整系列(年率寄与度)", hdr=hdr, sub=sub, first_period="1994Q2", notes=kiyo_ann_note))
    for meas, hdr, sub, ttl in KIYO_ITEMS:
        out.append(_qe(f"cao.qe2020.{meas}_real_contrib.sa.q",
                       title=f"四半期別GDP速報（QE） {ttl} 実質GDP前期比への寄与度（季節調整系列・％pt）",
                       unit="％pt（実質・前期比・季節調整済）", measure=f"{meas}_real_contrib", dims="sa", sa=True,
                       f="kiyo-jk", kl="実質季節調整系列(寄与度)", hdr=hdr, sub=sub, first_period="1994Q2", notes=kiyo_note))
    return out


def cao_gap_series() -> list[Series]:
    """内閣府「ＧＤＰギャップ、潜在成長率」（第 9 弾 段 4・2026-08-29）。日銀（boj.gap）とは推計主体・手法・頻度が異なる別系列（混ぜない）。
    原典 URL は QE 公表回に連動して毎回変わる＝accessor に固定せず、取込時に索引ページから発見（ingest/cao_gap.py）。"""
    idx = "https://www5.cao.go.jp/keizai3/getsurei/getsurei-index.html"  # source_url は安定ページ（実 URL は値の accessor に刻む）
    out: list[Series] = []
    ITEMS = [("gdp_gap", "ＧＤＰギャップ", "GDPギャップ", "％"),
             ("potential_growth", "潜在成長率", "潜在成長率", ""),
             ("potential_growth_tfp", "全要素生産性", "潜在成長率 寄与度 全要素生産性（TFP）", "pt"),
             ("potential_growth_capital", "資本投入量", "潜在成長率 寄与度 資本投入量", "pt"),
             ("potential_growth_hours", "労働時間", "潜在成長率 寄与度 労働時間", "pt"),
             ("potential_growth_employment", "就業者数", "潜在成長率 寄与度 就業者数", "pt")]
    for sheet, freq, gr, unit_growth in (("四半期", "q", "四半期", "％{pt}（前期比年率{寄与度}・季節調整済）"),
                                         ("暦年", "a", "暦年", "％{pt}（前年比{寄与度}）"),
                                         ("年度", "fy", "年度", "％{pt}（前年比{寄与度}）")):
        for meas, hdr, ttl, kind_pt in ITEMS:
            unit = "％" if meas == "gdp_gap" else unit_growth.replace("{pt}", "pt" if kind_pt else "").replace("{寄与度}", "寄与度" if kind_pt else "")
            fp = {"q": "1994Q1", "a": "1994", "fy": "FY1994"}[freq] if meas == "gdp_gap" else {"q": "1994Q2", "a": "1995", "fy": "FY1995"}[freq]
            out.append(_s(series_id=f"cao.cao_gap.{meas}.{freq}",
                          title=f"ＧＤＰギャップ・潜在成長率（内閣府） {ttl}（{gr}・{unit}）",
                          org="cao", org_name="内閣府", source_url=idx, sector="マクロ", unit=unit,
                          granularity=gr, freq=freq, dataset="cao_gap", measure=meas,
                          seasonal_adjustment=(freq == "q"),
                          stat_name="ＧＤＰギャップ、潜在成長率（月例経済報告 その他の資料）",
                          table_id="{QE公表回}gap.xlsx", table_title=sheet, policy_tags=(TAG_MACRO,),
                          period_converter="", first_period=fp,
                          accessor={"type": "cao_gap_xlsx", "sheet": sheet, "col_header": hdr},
                          citation_template="内閣府「ＧＤＰギャップ、潜在成長率」（月例経済報告 その他の資料・{item}／{period}）取得 {retrieved_at}",
                          notes=("内閣府の推計値（QE 2次速報後に年 4 回更新・過去値も改定）。GDPギャップ＝（実際のGDP−潜在GDP）／潜在GDP。"
                                 "需給ギャップ（日銀の呼称・boj.gap）とは推計主体・手法・頻度が異なる＝並べるときは推計主体を明示し混ぜない。"
                                 "原典の「-」（1994 の前年比・前期比）は値なし。※印（-0.0 の意味の 0.0）は status に刻む。"),
                          status="registered"))
    return out


def boj_gap_series() -> list[Series]:
    url = "https://www.boj.or.jp/research/research_data/gap/gap.xlsx"
    idx = "https://www.boj.or.jp/research/research_data/gap/index.htm"
    out: list[Series] = []
    for meas, hdr, ttl in [("output_gap", "需給ギャップ", "需給ギャップ"), ("capital_input_gap", "資本投入ギャップ", "資本投入ギャップ"),
                           ("labor_input_gap", "労働投入ギャップ", "労働投入ギャップ")]:
        out.append(_s(series_id=f"boj.gap.{meas}.q", title=f"需給ギャップと潜在成長率 {ttl}（四半期・％）", org="boj", org_name="日本銀行", source_url=idx,
                      sector="マクロ", unit="％", granularity="四半期", freq="q", dataset="gap", measure=meas, stat_name="需給ギャップと潜在成長率",
                      table_id="gap.xlsx data1", table_title="需給ギャップと短観加重平均ＤＩ", policy_tags=(TAG_MACRO,), period_converter="boj_gap_q",
                      first_period="1983Q1", accessor={"type": "boj_file", "url": url, "sheet": "data1", "col_header": hdr, "header_row": 2, "first_data_row": 6},
                      citation_template="日本銀行「需給ギャップと潜在成長率」gap.xlsx data1（{item}／{period}）取得 {retrieved_at}",
                      notes="日銀推計値。GDPギャップ（内閣府の呼称）と同種の概念だが内閣府推計とは定義が異なる（混ぜない）。値は表示書式（小数2桁）で文字列化。", status="registered"))
    for meas, hdr, ttl in [("potential_growth", "潜在成長率", "潜在成長率（前年比）"), ("potential_growth_tfp", "ＴＦＰ", "潜在成長率 寄与度 TFP"),
                           ("potential_growth_capital", "資本ストック", "潜在成長率 寄与度 資本ストック"), ("potential_growth_hours", "労働時間", "潜在成長率 寄与度 労働時間"),
                           ("potential_growth_employment", "就業者数", "潜在成長率 寄与度 就業者数")]:
        out.append(_s(series_id=f"boj.gap.{meas}.h", title=f"需給ギャップと潜在成長率 {ttl}（年度半期・％）", org="boj", org_name="日本銀行", source_url=idx,
                      sector="マクロ", unit="％（前年比・寄与度）", granularity="半期（年度）", freq="h", dataset="gap", measure=meas, stat_name="需給ギャップと潜在成長率",
                      table_id="gap.xlsx data2", table_title="潜在成長率", policy_tags=(TAG_MACRO, TAG_IND), period_converter="boj_gap_h",
                      first_period="FY1983H1", accessor={"type": "boj_file", "url": url, "sheet": "data2", "col_header": hdr, "header_row": 2, "first_data_row": 6},
                      citation_template="日本銀行「需給ギャップと潜在成長率」gap.xlsx data2（{item}／{period}）取得 {retrieved_at}",
                      notes="日銀推計値（前年比・寄与度）。年度半期表記（FY2024H1＝2024年4-9月）。値は表示書式（小数2桁）で文字列化。", status="registered"))
    return out


PREFS = ("JP",) + tuple(f"{i:02d}" for i in range(1, 48))


# ---- 第 11 弾 第 1 便（2026-09-15）毎月勤労統計調査 全国調査 長期時系列表「実数・指数累積データ」（e-Stat ファイル提供 CSV・statInfId 固定・毎月更新）
# 要件源＝実利用ログ（「実質賃金」検索 10 回・planned で値なし＝一度も lookup されず）。経路は第 10 弾 第 2 便で特定（当時 503）→ 2026-09-15 に 200 を確認。
# dims＝<就業形態>-<規模>。就業形態＝total（就業形態計）／general（一般労働者）／part（パートタイム労働者）、規模＝5plus（5 人以上）／30plus（30 人以上）。
# 産業は調査産業計（TL）のみ。値は CSV の文字列そのまま。指数は 2020 年＝100。実質賃金指数は厚労省の公表値（CPI 持家の帰属家賃を除く総合で除したもの）＝自前で割らない。
MAIKIN_ACTUAL_ID, MAIKIN_INDEX_ID = "000032189776", "000032189777"
MAIKIN_EMP = [("total", "0", "就業形態計"), ("general", "1", "一般労働者"), ("part", "2", "パートタイム労働者")]
MAIKIN_SIZE = [("5plus", "T", "事業所規模5人以上", "1990"), ("30plus", "0", "事業所規模30人以上", "1970")]
MAIKIN_INDEX = [  # (measure, 列見出し, 表示名)＝種別「指数」（2020 年＝100）。伸び率（前年比）は measure + "_yoy"（種別「伸び率」・%）
    ("wage_total_nominal", "現金給与総額", "現金給与総額 名目賃金指数"),
    ("wage_total_real", "実質賃金指数（現金給与総額）", "現金給与総額 実質賃金指数"),
    ("wage_scheduled_nominal", "きまって支給する給与", "きまって支給する給与 名目賃金指数"),
    ("wage_scheduled_real", "実質賃金指数（きまって支給する給与）", "きまって支給する給与 実質賃金指数"),
    ("hours_total_index", "総実労働時間", "総実労働時間指数"),
    ("employment_index", "常用雇用", "常用雇用指数"),
]
MAIKIN_RATE = [  # 種別「指数」の行にある率そのもの（%）。伸び率行はポイント差＝収録しない
    ("parttime_ratio", "パートタイム労働者比率", "パートタイム労働者比率", True),   # True＝就業形態計のみ
    ("entry_rate", "入職率", "入職率", False),
    ("exit_rate", "離職率", "離職率", False),
]
MAIKIN_ACTUAL = [  # (measure, 列見出し, 表示名, 単位, 就業形態計のみ?)＝種別「実数」（月平均）
    ("wage_total", "現金給与総額", "現金給与総額", "円", False),
    ("wage_scheduled", "きまって支給する給与", "きまって支給する給与", "円", False),
    ("wage_contractual", "所定内給与", "所定内給与", "円", False),
    ("wage_overtime", "所定外給与", "所定外給与", "円", False),
    ("wage_special", "特別給与", "特別に支払われた給与", "円", False),
    ("hours_total", "総実労働時間", "総実労働時間", "時間", False),
    ("hours_scheduled", "所定内労働時間", "所定内労働時間", "時間", False),
    ("hours_overtime", "所定外労働時間", "所定外労働時間", "時間", False),
    ("days_worked", "出勤日数", "出勤日数", "日", False),
    ("employees", "本月末労働者数", "常用労働者数（本月末）", "人", False),
    ("parttime_workers", "パートタイム労働者数", "パートタイム労働者数", "人", True),
]
# 収録開始年（CSV 実測 2026-09-15）：就業形態計＝規模の開始年（5 人以上 1990・30 人以上 1970）、一般／パート＝1993。例外は下表。伸び率は +1 年。
_MAIKIN_FIRST_OVERRIDE = {  # 値＝開始年（年・月とも 1 月始まり）か (暦年の開始年, 月次の開始期)
    ("employees", "total", "30plus"): ("1977", "1984-08"), ("entry_rate", "total", "30plus"): "1984", ("exit_rate", "total", "30plus"): "1984",
    ("parttime_ratio", "total", "30plus"): "1990", ("parttime_workers", "total", "30plus"): "1990",
    ("wage_contractual", "total", "30plus"): ("1979", "1979-04"), ("wage_overtime", "total", "30plus"): "1986",
    ("employment_index", "general", "5plus"): "1990", ("employment_index", "general", "30plus"): "1990",
    ("employment_index", "part", "5plus"): "1990", ("employment_index", "part", "30plus"): "1990"}
MAIKIN_NOTE = ("毎月勤労統計調査 全国調査 長期時系列表「実数・指数累積データ」（e-Stat ファイル提供 CSV・調査産業計）。"
               "規模 5 人以上は 1990 年〜・30 人以上は 1970 年〜・就業形態別（一般・パート）は 1993 年〜。"
               "実質指数＝名目指数を CPI（持家の帰属家賃を除く総合）で除したもの＝厚労省の公表値をそのまま収録（自前で割らない）。指数は 2020 年＝100。"
               "2012 年以降は再集計値・2011 年以前は従来の公表値（厚労省の累積データの扱いのまま）。速報値は確報で改定される（再取込で最新版に置き換わる＝旧版は保持しない）。")
_BRK_MAIKIN_2018 = {"kind": "population", "treatment": "note_only",
                    "label": "標本の入替方式変更（部分入替）とベンチマーク更新（2018 年 1 月）＝水準に段差",
                    "note": "2018 年の前年比は水準の段差を含む（厚労省は参考として共通事業所ベースの伸び率を別途公表＝未収録）。2018 年前後を跨ぐ水準比較は注意。",
                    "source": "厚生労働省 毎月勤労統計調査「2018 年 1 月分以降の集計方法の変更」・長期時系列表の注記"}
_BRK_MAIKIN_2012 = {"kind": "population", "treatment": "note_only",
                    "label": "再集計値への置換（2012 年以降）＝東京都の 500 人以上規模事業所の抽出調査分を復元推計",
                    "note": "2012〜2017 年は再集計値、2011 年以前は従来の公表値（2004〜2011 年は再集計不能）。",
                    "source": "厚生労働省「毎月勤労統計調査における再集計値等について」（2019 年 1 月）"}


def maikin_series() -> list[Series]:
    out: list[Series] = []
    url_a, url_i = (f"https://www.e-stat.go.jp/stat-search/file-download?statInfId={MAIKIN_ACTUAL_ID}&fileKind=1",
                    f"https://www.e-stat.go.jp/stat-search/file-download?statInfId={MAIKIN_INDEX_ID}&fileKind=1")

    def first(meas: str, emp: str, size: str, start: str, yoy: bool) -> tuple[str, str]:
        """(暦年の開始 period, 月次の開始 period)。伸び率は開始の翌年（1 月始まり）。"""
        ov = _MAIKIN_FIRST_OVERRIDE.get((meas, emp, size)) or (start if emp == "total" else "1993")
        ya, m0 = (ov if isinstance(ov, tuple) else (ov, f"{ov}-01"))
        return (str(int(ya) + 1), f"{int(ya) + 1}-01") if yoy else (ya, m0)

    def add(meas: str, col: str, ttl: str, unit: str, kind: str, sid: str, url: str, table_title: str, total_only: bool,
            yoy: bool = False, basis: str = ""):
        for emp, ecode, elabel in MAIKIN_EMP:
            if total_only and emp != "total":
                continue
            for size, scode, slabel, start in MAIKIN_SIZE:
                pa, pm = first(meas.removesuffix("_yoy"), emp, size, start, yoy)
                for freq, gran, fsuffix, p0 in (("m", "月次", "前年同月比" if yoy else "月次", pm), ("a", "暦年", "前年比" if yoy else "年平均", pa)):
                    brk = tuple({**b, "period": (f"{yy}-01" if freq == "m" else yy)} for b, yy in ((_BRK_MAIKIN_2012, "2012"), (_BRK_MAIKIN_2018, "2018")))
                    out.append(_s(series_id=f"mhlw.maikin.{meas}.{emp}-{size}.{freq}",
                                  title=f"毎月勤労統計 {ttl}（{elabel}・{slabel}・調査産業計・{fsuffix}）",
                                  org="mhlw", org_name="厚生労働省", source_url=url, sector="家計・労働", unit=unit, granularity=gran, freq=freq,
                                  dataset="maikin", measure=meas, dims=f"{emp}-{size}", basis=basis, first_period=p0,
                                  stat_name="毎月勤労統計調査 全国調査 長期時系列表", table_id=sid, table_title=table_title,
                                  policy_tags=(TAG_LABOR,), status="registered", breaks=brk,
                                  accessor={"type": "maikin_csv", "statInfId": sid, "kind": kind, "col": col, "industry": "TL", "size": scode, "emp": ecode},
                                  citation_template=f"厚生労働省「毎月勤労統計調査」全国調査 長期時系列表 {table_title}（e-Stat ファイル {sid}・" + "{item}／{period}）取得 {retrieved_at}",
                                  notes=MAIKIN_NOTE))

    for meas, col, ttl in MAIKIN_INDEX:
        add(meas, col, ttl, "指数（2020年=100）", "指数", MAIKIN_INDEX_ID, url_i, "実数・指数累積データ 指数・伸び率", False, basis="2020年=100")
        add(meas + "_yoy", col, ttl.replace("指数", "").rstrip() + " 伸び率", "％", "伸び率", MAIKIN_INDEX_ID, url_i, "実数・指数累積データ 指数・伸び率", False, yoy=True)
    for meas, col, ttl, total_only in MAIKIN_RATE:
        add(meas, col, ttl, "％", "指数", MAIKIN_INDEX_ID, url_i, "実数・指数累積データ 指数・伸び率", total_only)
    for meas, col, ttl, unit, total_only in MAIKIN_ACTUAL:
        add(meas, col, ttl, unit, "実数", MAIKIN_ACTUAL_ID, url_a, "実数・指数累積データ 実数", total_only)
    return out


# ---- 第 11 弾 第 6 便（2026-09-15）一般職業紹介状況（職業安定業務統計）長期時系列表 第 1 表（e-Stat ファイル提供 xlsx・statInfId は毎月変わる＝カタログ API で特定）
# 要件源＝実利用ログ：「有効求人倍率」検索 22 回・planned への lookup no_values 8 回。
SHOKUGYO_COLS = [  # (measure, 列見出し, 表示名, 単位, 季節調整値あり?)
    ("new_job_openings_ratio", "新規求人倍率", "新規求人倍率", "倍", True),
    ("job_openings_ratio", "有効求人倍率", "有効求人倍率", "倍", True),
    ("placement_rate", "就職率（対新規）", "就職率（対新規求職申込件数）", "％", True),
    ("fill_rate", "充足率（対新規）", "充足率（対新規求人数）", "％", True),
    ("new_openings", "新規求人数", "新規求人数", "人", False),
    ("new_applicants", "新規求職申込件数", "新規求職申込件数", "件", False),
    ("active_openings", "有効求人数", "有効求人数", "人", False),
    ("active_applicants", "有効求職者数", "有効求職者数", "人", False),
    ("placements", "就職件数", "就職件数", "件", False),
]
SHOKUGYO_FIRST = {"a": "1963", "fy": "FY1963", "q": "2002Q1", "m": "2002-01"}          # 原数値（xlsx 実測 2026-09-15：年・年度は 1963〜、四半期・月は 2002〜）
SHOKUGYO_FIRST_SA = {"q": "2002Q1", "m": "2002-01"}                                     # 季節調整値（倍率・率のみ）
SHOKUGYO_NOTE = ("一般職業紹介状況（職業安定業務統計）長期時系列表 第 1 表（パートタイムを含む常用）。公共職業安定所（ハローワーク）を経由した求人・求職の集計＝"
                 "労働市場全体ではない（求人媒体・縁故等は含まない）。倍率＝求人数÷求職者数。年・年度・四半期は原数値（季節調整値は倍率・率の月次・四半期のみ）。"
                 "取得元は e-Stat のファイル提供（毎月更新・ファイル ID が毎月変わるためカタログ API で最新版を特定）。値は xlsx のセル値どおり。")
SHOKUGYO_URL = "https://www.e-stat.go.jp/stat-search/files?page=1&toukei=00450222&tstat=000001020327"


def shokugyo_series() -> list[Series]:
    out: list[Series] = []
    for meas, col, ttl, unit, has_sa in SHOKUGYO_COLS:
        for sa in ((False, True) if has_sa else (False,)):
            freqs = ("m", "q") if sa else ("m", "q", "a", "fy")
            for freq in freqs:
                gran = {"m": "月次", "q": "四半期", "a": "暦年", "fy": "年度"}[freq]
                dims = "sa" if sa else ""
                out.append(_s(series_id=f"mhlw.shokugyo.{meas}." + (f"sa.{freq}" if sa else freq),
                              # 表題に「パート」を入れない（発見層の教訓＝44 系列が「パート」に当たり労調のパート系列を top20 から押し出した）＝対象の注記は notes
                              title=f"一般職業紹介状況 {ttl}（常用計・{gran}・{'季節調整値' if sa else '原数値'}）",
                              org="mhlw", org_name="厚生労働省", source_url=SHOKUGYO_URL, sector="家計・労働", unit=unit, granularity=gran, freq=freq,
                              dataset="shokugyo", measure=meas, dims=dims, seasonal_adjustment=sa,
                              first_period=(SHOKUGYO_FIRST_SA if sa else SHOKUGYO_FIRST)[freq],
                              stat_name="一般職業紹介状況（職業安定業務統計）", table_id="長期時系列表 第1表", table_title="労働市場関係指標（パートタイムを含む常用）",
                              policy_tags=(TAG_LABOR,), status="registered",
                              accessor={"type": "estat_catalog_xlsx", "catalog_word": "一般職業紹介状況 長期時系列表", "title_prefix": "一般職業紹介状況_～",
                                        "table_no": 1, "sheet": "第１表", "col": col, "adjusted": sa},
                              citation_template="厚生労働省「一般職業紹介状況（職業安定業務統計）」長期時系列表 第1表 労働市場関係指標（e-Stat ファイル提供・{item}／{period}）取得 {retrieved_at}",
                              notes=SHOKUGYO_NOTE))
    return out


# ---- 第 11 弾 第 7 便（2026-09-15）内閣府「政府経済見通し」（閣議決定 1 月／年央試算 7 月）PDF 主要経済指標＝kind=projection
# 要件源＝実利用ログ：cao.sna2020.gdp_nominal.fy の FY2026 が out_of_range 18 回・「名目GDP 見通し 2026年度」等 projection 検索 約 10 回。
MITOSHI_EDITION, MITOSHI_FY = "2026-01-23", 2026          # 令和 8 年度の経済見通しと経済財政運営の基本的態度（閣議決定）
SHISAN_EDITION, SHISAN_FY = "2026-07-30", 2026            # 令和 8 年度の経済見通しに関する年央試算
MITOSHI_ROWS = [  # (measure, 表の項目, 列群, 表示名, 単位)
    ("gdp_nominal", "国内総生産", "level", "名目GDP（額）", "兆円"),
    ("gdp_nominal_growth", "国内総生産", "nominal_growth", "名目GDP成長率", "％"),
    ("gdp_real_growth", "国内総生産", "real_growth", "実質GDP成長率", "％"),
    ("private_consumption_real_growth", "民間最終消費支出", "real_growth", "民間最終消費支出 実質成長率", "％"),
    ("priv_capex_real_growth", "民間企業設備", "real_growth", "民間企業設備 実質成長率", "％"),
    ("gni_nominal", "国民総所得", "level", "名目GNI（額）", "兆円"),
    ("compensation_employees", "雇用者報酬", "level", "雇用者報酬（額）", "兆円"),
    ("compensation_employees_growth", "雇用者報酬", "nominal_growth", "雇用者報酬 伸び率", "％"),
    ("employed", "就業者数", "level", "就業者数", "万人"),
    ("employees", "雇用者数", "level", "雇用者数", "万人"),
    ("unemployment_rate", "完全失業率", "value", "完全失業率", "％"),
    ("iip_change", "鉱工業生産指数・変化率", "value", "鉱工業生産指数 変化率", "％"),
    ("cgpi_change", "国内企業物価指数・変化率", "value", "国内企業物価指数 変化率", "％"),
    ("cpi_change", "消費者物価指数・変化率", "value", "消費者物価指数（総合）変化率", "％"),
    ("gdp_deflator_change", "GDPデフレーター・変化率", "value", "GDPデフレーター 変化率", "％"),
    ("current_account", "経常収支", "level", "経常収支（額）", "兆円"),
    ("current_account_gdp_ratio", "経常収支対名目GDP比", "value", "経常収支対名目GDP比", "％"),
]
SHISAN_ROWS = [  # (measure, 表の項目, 表示名)＝年央試算は成長率・率のみ
    ("gdp_real_growth", "実質国内総生産（ＧＤＰ）", "実質GDP成長率"), ("gdp_nominal_growth", "名目国内総生産（ＧＤＰ）", "名目GDP成長率"),
    ("private_consumption_real_growth", "民間最終消費支出", "民間最終消費支出 実質成長率"), ("priv_capex_real_growth", "民間企業設備", "民間企業設備 実質成長率"),
    ("gni_real_growth", "実質国民総所得（ＧＮＩ）", "実質GNI成長率"), ("unemployment_rate", "完全失業率", "完全失業率"),
    ("employees_growth", "雇用者数", "雇用者数 伸び率"), ("iip_change", "鉱工業生産", "鉱工業生産 変化率"),
    ("cgpi_change", "国内企業物価", "国内企業物価 変化率"), ("cpi_change", "消費者物価（総合）", "消費者物価（総合）変化率"),
    ("gdp_deflator_change", "ＧＤＰデフレーター", "GDPデフレーター 変化率"),
]
MITOSHI_NOTE = ("内閣府「経済見通しと経済財政運営の基本的態度」（12 月閣議了解→1 月閣議決定）別添「主要経済指標」の政府見通し。"
                "収録は実績見込み（前年度）と見通し（当年度）の 2 期だけ＝実績列は SNA 等の観測系列で引く（見通しと混ぜない）。"
                "kind=projection・最新版のみ保持（版＝edition・各値の accessor.edition）。表記の正規化＝▲ は負号・万人の桁区切りは除く（丸め・換算なし）。"
                "原典注記＝「上表の諸計数はある程度幅を持って考えられるべきもの」（％程度・兆円程度）。")
SHISAN_NOTE = ("内閣府「経済見通しに関する年央試算」（7 月）主要経済指標の今回試算（当年度）。1 月の政府経済見通しとの比較は cao.mitoshi.* を並べる。"
               "成長率・率のみ（額は年央試算に無い）。kind=projection・最新版のみ保持（版＝edition）。▲ は負号に正規化。")


def mitoshi_series() -> list[Series]:
    out: list[Series] = []
    base = dict(org="cao", org_name="内閣府", source_url="https://www5.cao.go.jp/keizai1/mitoshi/mitoshi.html", sector="マクロ", granularity="年度", freq="fy",
                kind="projection", projection_by="内閣府", policy_tags=(TAG_MACRO,), status="registered")
    for meas, row, col, ttl, unit in MITOSHI_ROWS:
        out.append(_s(series_id=f"cao.mitoshi.{meas}.fy", title=f"政府経済見通し {ttl}（閣議決定・年度・実績見込み／見通し）", unit=unit, dataset="mitoshi",
                      measure=meas, edition=f"{MITOSHI_EDITION} 閣議決定（令和{MITOSHI_FY - 2018}年度の経済見通しと経済財政運営の基本的態度）",
                      first_period=f"FY{MITOSHI_FY - 1}", stat_name="政府経済見通し（経済見通しと経済財政運営の基本的態度）", table_title="主要経済指標（別添）",
                      accessor={"type": "cao_mitoshi_pdf", "doc": "mitoshi", "row": row, "col": col},
                      citation_template="内閣府「経済見通しと経済財政運営の基本的態度」（閣議決定）別添 主要経済指標（{item}／{period}・政府見通し）取得 {retrieved_at}",
                      notes=MITOSHI_NOTE, **base))
    for meas, row, ttl in SHISAN_ROWS:
        out.append(_s(series_id=f"cao.mitoshi_mid.{meas}.fy", title=f"政府経済見通し 年央試算 {ttl}（今回試算・年度）", unit="％", dataset="mitoshi_mid",
                      measure=meas, edition=f"{SHISAN_EDITION} 年央試算（令和{SHISAN_FY - 2018}年度）", first_period=f"FY{SHISAN_FY}",
                      stat_name="政府経済見通し（経済見通しに関する年央試算）", table_title="主要経済指標",
                      accessor={"type": "cao_mitoshi_pdf", "doc": "shisan", "row": row, "col": "value"},
                      citation_template="内閣府「経済見通しに関する年央試算」主要経済指標（{item}／{period}・今回試算）取得 {retrieved_at}",
                      notes=SHISAN_NOTE, **base))
    return out


def jinko_series() -> list[Series]:
    """総務省 人口推計（各年10月1日現在・全国＋都道府県）。国勢調査基準ごとに表が分かれるため、
    公表主体自身が接続済みの「補間補正人口」（1990–2015／2015–2020）＋令和2年基準 統計表（2021–）を parts で束ねる。
    値は各表の公表値そのまま（こちらで接続・補正はしない）。年齢3区分は 2016 年以降のみ（1990–2015 の補間補正表は総数のみ）。"""
    url = "https://www.e-stat.go.jp/stat-search/database?statdisp_id=0003448225"
    P1990 = {"statsDataId": "0004029363", "cdCat01": "000", "cdCat02": "001", "time_from_name": True}          # 総人口・男女計 1990–2015
    P2016 = {"statsDataId": "0004021110", "cdCat01": "000", "cdCat02": "001", "time_from_name": True}          # 補間補正 2016–2020（cat03=年齢3区分）
    P2021 = {"statsDataId": "0003448225", "cdCat01": "000", "cdCat03": "001", "time_from_name": True}          # 令和2年基準 2021–（cat02=年齢3区分）
    out: list[Series] = []
    def mk(meas, ttl, parts, first, notes_extra=""):
        return _s(series_id=f"soumu.jinko.{meas}.a.pref", title=f"人口推計 {ttl}（総人口・男女計・各年10月1日現在・全国＋都道府県）",
                  org="soumu", org_name="総務省", source_url=url, sector="人口", unit="千人", granularity="暦年", freq="a",
                  dataset="jinko", measure=meas, region_level="pref", region_codes=PREFS, stat_name="人口推計",
                  table_id="0004029363/0004021110/0003448225", table_title="国勢調査結果による補間補正人口（1990–2015・2015–2020）／令和2年国勢調査基準 統計表",
                  policy_tags=(TAG_POP,), first_period=first,
                  accessor={"type": "estat", "parts": parts},
                  citation_template="総務省「人口推計」各年10月1日現在（e-Stat・{item}／{period}）取得 {retrieved_at}",
                  notes="国勢調査基準ごとの表を束ねる（1990–2015・2016–2020 は国勢調査結果による補間補正人口、2021– は令和2年国勢調査基準）。"
                        "値は各表の公表値そのまま。地域は JP＋都道府県 JIS 2桁。" + notes_extra, status="registered")
    out.append(mk("pop_total", "総人口", [P1990, {**P2016, "cdCat03": "000"}, {**P2021, "cdCat02": "000"}], "1990"))
    for meas, code, ttl in [("pop_0_14", "008", "15歳未満人口"), ("pop_15_64", "002", "15〜64歳人口"), ("pop_65plus", "003", "65歳以上人口"),
                            ("pop_65_74", "006", "65〜74歳人口"), ("pop_75plus", "007", "75歳以上人口")]:
        out.append(mk(meas, ttl, [{**P2016, "cdCat03": code}, {**P2021, "cdCat02": code}], "2016", " 年齢3区分は 2016 年以降（それ以前の補間補正表は総数のみ）＝1990〜2015 は out_of_range。"))
    return out


def ipss_series() -> list[Series]:
    """社人研「日本の将来推計人口（令和5年推計）」総数・年齢3区分（各年10月1日・千人）＋年齢構造係数。
    出生中位（死亡中位）＝表1-1／出生低位（死亡中位）＝表3-1。値は表示書式（千人は整数・割合は小数1桁）で文字列化。2020年は基準人口（国勢調査）。"""
    base = "https://www.ipss.go.jp/pp-zenkoku/j/zenkoku2023/db_zenkoku2023/s_tables/"
    idx = "https://www.ipss.go.jp/pp-zenkoku/j/zenkoku2023/pp_zenkoku2023.asp"
    out: list[Series] = []
    for scen, fname, tno, slabel in (("medium-medium", "1-1.xlsx", "表1-1", "出生中位（死亡中位）"),
                                     ("low-medium", "3-1.xlsx", "表3-1", "出生低位（死亡中位）")):
        for meas, hdr, occ, ttl, unit in [("pop_total", "総数", 1, "総人口", "千人"), ("pop_0_14", "0～14歳", 1, "0〜14歳人口", "千人"),
                                          ("pop_15_64", "15～64歳", 1, "15〜64歳人口", "千人"), ("pop_65plus", "65歳以上", 1, "65歳以上人口", "千人"),
                                          ("share_0_14", "0～14歳", 2, "年少人口割合", "％"), ("share_15_64", "15～64歳", 2, "生産年齢人口割合", "％"),
                                          ("share_65plus", "65歳以上", 2, "高齢化率（65歳以上人口割合）", "％")]:
            out.append(_s(series_id=f"ipss.pop2023.{meas}.{scen}.a", title=f"日本の将来推計人口（令和5年推計）{ttl}（{slabel}・各年10月1日）",
                          org="ipss", org_name="国立社会保障・人口問題研究所", source_url=idx, sector="人口", unit=unit, granularity="暦年", freq="a",
                          dataset="pop2023", measure=meas, dims=scen, kind="projection", projection_by="国立社会保障・人口問題研究所",
                          edition="令和5年推計（2023年）", scenario=slabel, stat_name="日本の将来推計人口", table_id=tno,
                          table_title=f"{tno} 総数，年齢3区分別総人口および年齢構造係数：{slabel}推計", policy_tags=(TAG_POP, TAG_SOC),
                          period_converter="esri_year_a", first_period="2020", last_period="2070",
                          accessor={"type": "ipss_xlsx", "url": base + fname, "sheet": tno, "col_header": hdr, "col_occurrence": occ,
                                    "header_row": 4, "period_col": 2, "first_data_row": 5},
                          citation_template=f"国立社会保障・人口問題研究所「日本の将来推計人口（令和5年推計）」{tno}（{slabel}・" + "{item}／{period}）取得 {retrieved_at}",
                          notes=f"推計値（kind=projection・{slabel}）。2020年は国勢調査基準人口。千人は表示どおり整数、割合は小数1桁。中位・低位は別系列（dims）。",
                          status="registered"))
    return out


def jgb_series() -> list[Series]:
    """財務省 国債金利情報（基準日・年限別・％）。jgbcm_all.csv（1974-09-24〜）＋jgbcm.csv（当年度）。'-' は値なし。"""
    out: list[Series] = []
    for col in ["1年", "2年", "3年", "4年", "5年", "6年", "7年", "8年", "9年", "10年", "15年", "20年", "25年", "30年", "40年"]:
        m = col.replace("年", "y")
        out.append(_s(series_id=f"mof.jgb.yield.{m}.d", title=f"国債金利情報 {col}（基準日・％）", org="mof", org_name="財務省",
                      source_url="https://www.mof.go.jp/jgbs/reference/interest_rate/index.htm", sector="物価・金融", unit="％", granularity="日次", freq="d",
                      dataset="jgb", measure="yield", dims=m, stat_name="国債金利情報", table_id="jgbcm_all.csv / jgbcm.csv", table_title="国債金利情報（年限別・基準日）",
                      policy_tags=(TAG_FIN, TAG_FISCAL), first_period="1974-09-24",
                      accessor={"type": "mof_csv", "dataset": "jgbcm", "col": col},
                      citation_template="財務省「国債金利情報」（{item}／基準日 {period}）取得 {retrieved_at}",
                      notes="日次（営業日）。基準日の値そのもの＝月末値・年度末値は当該日の値を引く（平均は計算しない）。長期年限は発行開始前は '-'（値なし）。", status="registered"))
    return out


def boj_flat_series() -> list[Series]:
    """日銀 時系列統計データ検索サイトのフラットファイル：国際収支（月次・億円）・対外資産負債残高（暦年末・10億円）・資金循環 家計（四半期末・億円）。"""
    site = "https://www.stat-search.boj.or.jp/"
    out: list[Series] = []
    bp_note = ("値は日銀フラットファイル（bp_m_en.zip）の未丸めの数値文字列。財務省・日銀の公表表は億円整数に丸めているため見た目は異なる（数値としては同一の系列）。"
               "IMF 国際収支マニュアル第6版（BPM6）ベース・1996年1月〜。")
    for meas, code, ttl, tags in [("current_account", "BPBP6JYNCB", "経常収支", (TAG_TRADE, TAG_MACRO)), ("goods_services_balance", "BPBP6JYNTS", "貿易・サービス収支", (TAG_TRADE,)),
                                  ("trade_balance", "BPBP6JYNTB", "貿易収支", (TAG_TRADE,)), ("goods_exports", "BPBP6JYNEX", "輸出（財）", (TAG_TRADE,)),
                                  ("goods_imports", "BPBP6JYNIM", "輸入（財）", (TAG_TRADE,)), ("services_balance", "BPBP6JYNSN", "サービス収支", (TAG_TRADE,)),
                                  ("travel_related_balance", "BPBP6JYNTRN", "旅行・旅客輸送関連サービス収支", (TAG_TRADE,)),
                                  ("primary_income", "BPBP6JYNPIN", "第一次所得収支", (TAG_TRADE, TAG_FIN))]:
        out.append(_s(series_id=f"boj.bop.{meas}.m", title=f"国際収支統計 {ttl}（月次・億円・原数値）", org="boj", org_name="日本銀行", source_url=site,
                      sector="対外・国際", unit="億円", granularity="月次", freq="m", dataset="bop", measure=meas, stat_name="国際収支統計（BPM6）",
                      table_id=code, table_title=f"Balance of Payments (BPM6) {ttl}", policy_tags=tags, first_period="1996-01",
                      accessor={"type": "boj_flat", "zip": "bp_m_en.zip", "file": "bp_m_en.csv", "layout": "wide", "code": code, "period_kind": "yyyymm"},
                      citation_template="日本銀行 時系列統計データ「国際収支統計（BPM6）」系列コード " + code + "（{item}／{period}）取得 {retrieved_at}",
                      notes=bp_note, status="registered"))
    for meas, code, ttl in [("assets", "BPBP6KA", "対外資産残高"), ("liabilities", "BPBP6KL", "対外負債残高"), ("net_assets", "BPBP6KN", "対外純資産残高")]:
        out.append(_s(series_id=f"boj.iip.{meas}.a", title=f"本邦対外資産負債残高 {ttl}（暦年末・10億円）", org="boj", org_name="日本銀行", source_url=site,
                      sector="対外・国際", unit="10億円", granularity="暦年", freq="a", dataset="iip", measure=meas, stat_name="本邦対外資産負債残高（BPM6・暦年末）",
                      table_id=code, table_title=f"International Investment Position {ttl}", policy_tags=(TAG_TRADE, TAG_FIN), first_period="1996",
                      accessor={"type": "boj_flat", "zip": "iip_cy_en.zip", "file": "iip_cy_en.csv", "layout": "wide", "code": code, "period_kind": "yyyy"},
                      citation_template="日本銀行 時系列統計データ「本邦対外資産負債残高」系列コード " + code + "（{item}／{period}末）取得 {retrieved_at}",
                      notes="値はフラットファイルの未丸めの数値文字列（財務省公表表は兆円・億円に丸め）。暦年末残高。", status="registered"))
    for meas, code, ttl in [("hh_financial_assets_total", "FOF_FFAS430A900", "家計 金融資産合計"), ("hh_currency_deposits", "FOF_FFAS430A100", "家計 現金・預金"),
                            ("hh_debt_securities", "FOF_FFAS430A300", "家計 債務証券"), ("hh_equity_investment_trusts", "FOF_FFAS430A334", "家計 株式等・投資信託受益証券"),
                            ("hh_insurance_pension", "FOF_FFAS430A400", "家計 保険・年金・定型保証")]:
        out.append(_s(series_id=f"boj.fof.{meas}.q", title=f"資金循環統計 {ttl}（ストック・四半期末・億円）", org="boj", org_name="日本銀行", source_url=site,
                      sector="物価・金融", unit="億円", granularity="四半期", freq="q", dataset="fof", measure=meas, stat_name="資金循環統計（ストック）",
                      table_id=code, table_title=f"資金循環 {ttl}／ストック", policy_tags=(TAG_FIN, TAG_SOC), first_period="1997Q4",
                      accessor={"type": "boj_flat", "zip": "fof.zip", "file": "ff_value.csv", "layout": "long", "code": code, "period_kind": "yyyyqq"},
                      citation_template="日本銀行 時系列統計データ「資金循環統計」系列コード " + code + "（{item}／{period}末）取得 {retrieved_at}",
                      notes="四半期末残高（暦年四半期表記 2025Q4＝2025年12月末）。速報→確報で改定（vintage）。", status="registered"))
    return out


def zaisei_series() -> list[Series]:
    """財務省「財政統計」定型 Excel。第1表（円・FY1947〜）／第3・4表（百万円・FY1982〜）／第19表(2)（千円・FY1985〜）／第20表（千円・FY1967〜）。"""
    top = "https://www.mof.go.jp/policy/budget/reference/statistics/data.htm"
    out: list[Series] = []
    def z(sid, title, unit, measure, dims, acc, table_id, table_title, first, notes, tags=(TAG_FISCAL,)):
        return _s(series_id=sid, title=title, org="mof", org_name="財務省", source_url=top, sector="財政", unit=unit, granularity="年度", freq="fy",
                  dataset="zaisei", measure=measure, dims=dims, stat_name="財政統計", table_id=table_id, table_title=table_title,
                  policy_tags=tuple(tags), first_period=first, accessor={"type": "mof_zaisei", **acc},
                  citation_template=f"財務省「財政統計」{table_title}（" + "{item}／{period}）取得 {retrieved_at}", notes=notes, status="registered")
    S1 = ["1.-3昭和22～63年度", "1.-4平成", "1.-5令和"]
    for meas, col, ttl in [("revenue_budget", "C", "歳入 予算額"), ("revenue_settled", "D", "歳入 決算額"), ("expenditure_budget", "E", "歳出 予算額"), ("expenditure_settled", "F", "歳出 決算額")]:
        d = "budget" if meas.endswith("budget") else "settled"
        out.append(z(f"mof.zaisei.{meas.split('_')[0]}_total.{d}.fy", f"財政統計 第1表 一般会計 {ttl}（年度・円）", "円", f"{meas.split('_')[0]}_total", d,
                     {"layout": "year_blocks", "file": "01.xlsx", "sheets": S1, "value_col": col}, "01.xlsx", "第1表 明治初年度以降一般会計歳入歳出予算決算", "FY1947",
                     "各年度の「計」行（当初＋補正の合計）。単位は円（換算しない）。予算額＝補正後予算、決算額＝決算。"))
    for kind, f, ttl_k in (("budget", "03.xlsx", "予算"), ("settled", "04.xlsx", "決算")):
        for meas, col, ttl in [("tax_stamp_revenue", "G", "租税及印紙収入（計）"), ("bond_issuance", "N", "公債金"), ("revenue_total_major", "P", "歳入合計")]:
            out.append(z(f"mof.zaisei.{meas}.{kind}.fy", f"財政統計 第{'3' if kind == 'budget' else '4'}表 一般会計歳入主要科目別 {ttl}（{ttl_k}・年度・百万円）", "百万円", meas, kind,
                         {"layout": "year_rows", "file": f, "value_col": col, "first_row": 7}, f, f"第{'3' if kind == 'budget' else '4'}表 昭和57年度以降一般会計歳入主要科目別{ttl_k}", "FY1982",
                         "単位は百万円（換算しない）。予算は当初予算。", tags=(TAG_FISCAL, TAG_MACRO)))
    SEC = [("social_security", "社会保障関係費", (TAG_FISCAL, TAG_SOC)), ("education_science", "文教及び科学振興費", (TAG_FISCAL,)),
           ("debt_service", "国債費", (TAG_FISCAL, TAG_FIN)), ("defense", "防衛関係費", (TAG_FISCAL,)),
           ("public_works", "公共事業関係費", (TAG_FISCAL,)), ("expenditure_total_major", "合計", (TAG_FISCAL, TAG_MACRO))]
    for meas, sec, tags in SEC:
        for hdr, d, ttl in (("当初予算", "initial", "当初予算"), ("補正予算", "supplementary", "補正予算"), ("計", "revised", "当初＋補正 計")):
            out.append(z(f"mof.zaisei.{meas}.{d}.fy", f"財政統計 第19表(2) 主要経費別 一般会計歳出 {sec} {ttl}（年度・千円）", "千円", meas, d,
                         {"layout": "year_sheets", "file": "19b.xlsx", "section": sec, "value_header": hdr}, "19b.xlsx",
                         "第19表(2) 主(重)要経費別分類による一般会計歳出当初予算及び補正予算（昭和60年度〜）", "FY1985",
                         "単位は千円（換算しない）。主要経費の分類は年度により変わる（原典の区分どおり）。昭和59年度以前は第19表(1)＝未取込。", tags))
        out.append(z(f"mof.zaisei.{meas}.settled.fy", f"財政統計 第20表 主要経費別 一般会計歳出 {sec} 決算額（年度・千円）", "千円", meas, "settled",
                     {"layout": "year_sheets", "file": "20.xlsx", "section": sec, "value_header": "決算額"}, "20.xlsx",
                     "第20表 昭和42年度以降主要経費別分類による一般会計歳出予算現額及び決算額", "FY1967",
                     "単位は千円（換算しない）。主要経費の分類は年度により変わる（原典の区分どおり）。", tags))
    return out


def shunto_series() -> list[Series]:
    """経団連 春季労使交渉 業種別妥結結果（加重平均・最終集計）総平均：妥結額（円）・アップ率（％）× 大手／中小。
    **status=guide（値を持たない）**：経団連の著作権規定は「商用目的の使用は予めご相談」＝値の再配布は行わず、発見層に置いて原典の読み方だけ案内する
    （2026-08-18 判断・docs/再配布条件.md）。値は利用側が経団連サイトの PDF を読んで取得する。"""
    out: list[Series] = []
    for d, ttl, extra in (("large", "大手企業", ()), ("sme", "中小企業", (TAG_SME,))):
        for meas, mt, unit, col in (("wage_hike_rate", "賃上げ率（アップ率・増減率）", "％", "アップ率（増減率）"), ("wage_hike_amount", "妥結額（加重平均）", "円", "妥結額")):
            guide = {
                "document": f"経団連「{{年}}年春季労使交渉・{ttl}業種別妥結結果（加重平均）〔最終集計〕」（毎年 大手は 6〜8 月・中小は 8〜9 月頃に PDF で公表）",
                "where": "経団連サイト 政策提言・調査報告 →「労働政策・労使関係」→ 春季労使交渉（https://www.keidanren.or.jp/policy/index09.html）。各年の PDF は https://www.keidanren.or.jp/policy/<年>/<番号>.pdf",
                "table": f"{ttl}業種別妥結結果（加重平均）の表（業種別＋総平均）",
                "row": "総平均",
                "column": f"{col}（当年）。同表の「前年」欄は前年の最終集計値＝前年 PDF の当年欄と一致するので照合に使える",
                "value_form": ("小数 2 桁の％（例 5.39）" if meas == "wage_hike_rate" else "円（例 19,195）"),
                "caveats": ("妥結額は定期昇給（賃金体系維持分）等を含む。中間集計（回答状況）は最終集計と別の文書＝混同しない。"
                            + ("中小は原則従業員500人未満（地方経済団体の協力による調査）。" if d == "sme" else "大手は原則従業員500人以上の主要業種。")),
                "alternatives": "厚生労働省「民間主要企業春季賃上げ要求・妥結状況」（PDL1.0・大手＝資本金10億円以上かつ従業員1,000人以上の労組あり企業）＝集計対象が違うので値は一致しない",
            }
            out.append(_s(series_id=f"keidanren.shunto.{meas}.{d}.a", title=f"春季労使交渉 {ttl} 業種別妥結結果（加重平均・最終集計） 総平均 {mt}",
                          org="keidanren", org_name="経団連", source_url="https://www.keidanren.or.jp/policy/index09.html", sector="家計・労働", unit=unit,
                          granularity="暦年", freq="a", dataset="shunto", measure=meas, dims=d, stat_name="春季労使交渉 業種別妥結結果（最終集計）",
                          table_id="keidanren:policy/<年>/<番号>.pdf", table_title=f"{ttl}業種別妥結結果（加重平均）総平均", policy_tags=(TAG_LABOR,) + extra,
                          first_period="2010", accessor={"type": "pdf_table", "kind": d, "row_label": "総平均", "guide": guide},
                          citation_template=f"経団連「春季労使交渉・{ttl}業種別妥結結果（加重平均・最終集計）」総平均（" + "{item}／{period}）",
                          notes="値は stats に保持しない（経団連の著作権規定＝商用は事前相談のため、発見層で読み方だけ案内）。利用側が原典 PDF を読む。"
                                "妥結額は定期昇給（賃金体系維持分）等を含む。中間集計（回答状況）は別。"
                                + ("中小は原則従業員500人未満（地方経済団体の協力による調査）。" if d == "sme" else "大手は原則従業員500人以上の主要業種。"),
                          status="guide"))
    return out


def chiho_zaisei_series() -> list[Series]:
    """総務省「地方財政状況調査」都道府県分（普通会計決算・千円）。e-Stat DB は FY1990–FY2017（2021-03 登録）。全国＋47都道府県。
    FY2018 以降は総務省 Excel（地方財政状況調査関係資料）＝次段。"""
    url = "https://www.e-stat.go.jp/stat-search/database?statdisp_id=0003173301"
    out: list[Series] = []
    def z(meas, ttl, sid, cd, table_title, tags=(TAG_FISCAL, TAG_REGION)):
        return _s(series_id=f"soumu.chihozaisei.{meas}.fy.pref", title=f"地方財政状況調査 都道府県分 {ttl}（普通会計決算・年度・千円・全国＋都道府県）",
                  org="soumu", org_name="総務省", source_url=url, sector="財政", unit="千円", granularity="年度", freq="fy", dataset="chihozaisei",
                  measure=meas, region_level="pref", region_codes=PREFS, stat_name="地方財政状況調査（都道府県分）", table_id=sid, table_title=table_title,
                  policy_tags=tuple(tags), first_period="FY1990", last_period="FY2017",
                  accessor={"type": "estat", "parts": [{"statsDataId": sid, **cd, "time_from_name": True}]},
                  citation_template="総務省「地方財政状況調査」都道府県分 " + table_title + "（e-Stat " + sid + "・{item}／{period}）取得 {retrieved_at}",
                  notes="e-Stat DB は FY1990〜FY2017（2021-03 登録・以後未更新）。FY2018 以降は総務省 Excel＝次段で追加予定。地域は JP（全国＝都道府県計）＋都道府県 JIS 2桁。単位 千円（換算しない）。"
                        "歳出の合計は表により集計範囲が異なる（決算収支の歳出総額(B) と 性質別経費表の歳出合計(A) は一致しない）＝表名どおりの系列を使い分ける。", status="registered")
    for meas, code, ttl in [("revenue_total", "1000", "歳入合計"), ("local_tax", "1010", "地方税"), ("local_allocation_tax", "1300", "地方交付税"),
                            ("national_treasury_disbursements", "1540", "国庫支出金"), ("local_bonds", "2440", "地方債")]:
        out.append(z(meas, ttl, "0003173301", {"cdTab": "105900", "cdCat01": code}, "歳入の状況 その１ 歳入内訳"))
    for meas, code, ttl in [("expenditure_total_by_nature", "100", "歳出合計（性質別経費表・当年度決算額(A)）"), ("personnel_expenses", "120", "人件費"), ("assistance_expenses", "170", "扶助費"),
                            ("debt_service", "210", "公債費"), ("investment_expenses", "330", "投資的経費"), ("ordinary_construction", "350", "普通建設事業費")]:
        out.append(z(meas, ttl, "0003173109", {"cdTab": "100100", "cdCat01": "100", "cdCat03": code}, "歳出の状況 その１ 性質別経費の状況（当年度決算額）"))
    for meas, code, ttl in [("revenue_total_settlement", "100", "歳入総額(A)"), ("expenditure_total", "110", "歳出総額(B)"),
                            ("real_balance", "140", "実質収支"), ("single_year_balance", "150", "単年度収支")]:
        out.append(z(meas, ttl, "0003173068", {"cdTab": "100100", "cdCat01": code}, "決算収支の状況"))
    return out


def chiho_keikaku_series() -> list[Series]:
    """総務省 地方財政計画（当初・通常収支分・億円）＝地方の「予算」。地方財政白書 資料編「第N表 地方財政計画」CSV を版をまたいで重ねる。"""
    url = "https://www.soumu.go.jp/menu_seisaku/hakusyo/chihou/r08data/2026data/r08czs02-00.html"
    out: list[Series] = []
    def k(meas, ttl, part, label, tags=(TAG_FISCAL, TAG_REGION)):
        return _s(series_id=f"soumu.chihokeikaku.{meas}.fy", title=f"地方財政計画（当初・通常収支分） {ttl}（年度・億円・地方団体計）", org="soumu", org_name="総務省",
                  source_url=url, sector="財政", unit="億円", granularity="年度", freq="fy", dataset="chihokeikaku", measure=meas, stat_name="地方財政計画（地方団体の歳入歳出総額の見込額）",
                  table_id="地方財政白書 資料編 第N表 地方財政計画（版で番号が変わる）", table_title=f"地方財政計画 その{part} {'歳入' if part == 1 else '歳出'}（通常収支分）",
                  policy_tags=tuple(tags), first_period="FY2019",
                  accessor={"type": "soumu_hakusho", "part": part, "row_label": label},
                  citation_template="総務省「地方財政計画」（地方財政白書 資料編 地方財政計画 その" + str(part) + "・{item}／{period}）取得 {retrieved_at}",
                  notes="計画額（当初・通常収支分・億円）。白書の各年版（直近3年度）を新しい版優先で重ねる。国の予算（財政統計）に対応する地方側の計画値＝決算（地方財政状況調査）とは別。", status="registered")
    for meas, ttl, label in [("revenue_total", "歳入合計", "歳入合計"), ("local_tax", "地方税", "地方税"), ("local_transfer_tax", "地方譲与税", "地方譲与税"),
                             ("local_special_grants", "地方特例交付金等", "地方特例交付金等"), ("local_allocation_tax", "地方交付税", "地方交付税"),
                             ("national_treasury_disbursements", "国庫支出金", "国庫支出金"), ("local_bonds", "地方債", "地方債"),
                             ("fees_charges", "使用料及び手数料", "使用料及び手数料"), ("misc_revenue", "雑収入", "雑収入")]:
        out.append(k(meas, ttl, 1, label))
    for meas, ttl, label in [("expenditure_total", "歳出合計", "歳出合計"), ("salary_related", "給与関係経費", "給与関係経費"), ("general_admin", "一般行政経費", "一般行政経費"),
                             ("debt_service", "公債費", "公債費"), ("maintenance", "維持補修費", "維持補修費"), ("investment", "投資的経費", "投資的経費"),
                             ("public_enterprise_transfer", "公営企業繰出金", "公営企業繰出金")]:
        out.append(k(meas, ttl, 4, label))
    return out


def build() -> list[Series]:
    S: list[Series] = []
    # ---- 法人企業統計（e-Stat 確定・取込対象） ----
    PL = [
        ("sales", "045", "売上高", {}), ("operating_profit", "048", "営業利益", {}), ("ordinary_profit", "051", "経常利益", {}),
        ("net_income", "056", "当期純利益", {}), ("value_added", "073", "付加価値", {}),
        ("capex_ex_software", "086", "設備投資（ソフトウェアを除く）", {"notes": "資金需給欄。ソフトウェアを含む設備投資は表の年により無い＝別途。"}),
        ("depreciation", "062", "減価償却費", {}), ("cash_deposits", "002", "現金・預金（当期末流動資産）", {}),
        ("retained_earnings", "226", "利益剰余金（当期末）", {}),
        ("employee_wages", "066", "従業員給与", {"tags": (TAG_LABOR, TAG_CORP)}), ("employee_bonus", "235", "従業員賞与", {"tags": (TAG_LABOR, TAG_CORP)}),
        ("welfare_costs", "067", "福利厚生費", {"tags": (TAG_LABOR, TAG_CORP)}),
        # 第2弾(b) PL・資金需給の残り
        ("nonoperating_income", "049", "営業外収益", {}), ("nonoperating_expenses", "050", "営業外費用", {}),
        ("interest_paid", "068", "支払利息等", {"tags": (TAG_CORP, TAG_FIN)}), ("dividends", "060", "配当金計", {"tags": (TAG_CORP, TAG_FIN)}),
        ("officer_compensation", "065", "役員給与", {"tags": (TAG_LABOR, TAG_CORP)}),
        ("employees_avg", "072", "期中平均従業員数", {"unit": "人", "tags": (TAG_LABOR, TAG_CORP)}),
    ]
    # 第2弾(a) BS 一式（当期末）
    BS = [
        ("total_assets", "022", "資産合計"), ("current_assets", "144", "流動資産"), ("inventories", "146", "棚卸資産"),
        ("fixed_assets", "147", "固定資産"), ("tangible_fixed_assets", "148", "有形固定資産"), ("land", "012", "土地（固定資産）"),
        ("investments_other", "150", "投資その他の資産"), ("investment_securities", "151", "投資有価証券"), ("stocks_fixed", "017", "株式（固定資産）"),
        ("liabilities", "224", "負債"), ("current_liabilities", "152", "流動負債"), ("short_term_borrowings", "153", "短期借入金"),
        ("fixed_liabilities", "154", "固定負債"), ("long_term_borrowings", "155", "長期借入金"), ("bonds", "030", "社債"),
        ("net_assets", "157", "純資産"), ("capital_stock", "036", "資本金"), ("shareholders_equity", "237", "株主資本"),
    ]
    for meas, code, ttl, kw in PL:
        S.append(hojin_fy(meas, code, ttl, **kw))
    for meas, code, ttl in BS:
        S.append(hojin_fy(meas, code, ttl, tags=(TAG_CORP, TAG_FIN),
                          first_period="FY2007" if meas == "shareholders_equity" else "FY1960",
                          notes="貸借対照表（当期末）。原数値・百万円。" + ("株主資本は2006年度以前は表にない（会社法施行前）＝out_of_range。" if meas == "shareholders_equity" else "")))
    # 第2弾(c) 規模別 3区分 × 主要10項目、(d) 業種別（製造/非製造）× 主要8項目
    KEY10 = [("sales", "045", "売上高"), ("operating_profit", "048", "営業利益"), ("ordinary_profit", "051", "経常利益"),
             ("capex_ex_software", "086", "設備投資（ソフトウェアを除く）"), ("cash_deposits", "002", "現金・預金（当期末流動資産）"),
             ("retained_earnings", "226", "利益剰余金（当期末）"), ("employee_wages", "066", "従業員給与"),
             ("employees_avg", "072", "期中平均従業員数"), ("total_assets", "022", "資産合計"), ("net_assets", "157", "純資産")]
    for size in ("cap1b", "cap100m-1b", "cap10m-100m"):
        for meas, code, ttl in KEY10:
            S.append(hojin_fy(meas, code, ttl, unit="人" if meas == "employees_avg" else "百万円", size=size))
    for ind in ("mfg", "nonmfg"):
        for meas, code, ttl in KEY10[:8]:
            S.append(hojin_fy(meas, code, ttl, unit="人" if meas == "employees_avg" else "百万円", ind=ind))
    # 第2弾(e) 四半期表 主要6項目（全産業除く金融保険・全規模）
    for meas, code, ttl, unit in [("sales", "078", "売上高", "百万円"), ("operating_profit", "081", "営業利益", "百万円"),
                                  ("ordinary_profit", "086", "経常利益", "百万円"), ("capex_total", "040", "設備投資（新設固定資産合計）", "百万円"),
                                  ("cash_deposits", "002", "現金・預金（当期末流動資産）", "百万円"), ("personnel_costs", "093", "人件費計", "百万円")]:
        S.append(hojin_fq(meas, code, ttl, unit=unit))
    # ---- CPI 2020年基準（e-Stat 確定・取込対象） ----
    for meas, item, ttl in [("cpi_all", "0001", "総合"), ("cpi_ex_fresh", "0161", "生鮮食品を除く総合"),
                            ("cpi_ex_fresh_energy", "0178", "生鮮食品及びエネルギーを除く総合"),
                            ("cpi_ex_imputed_rent", "0163", "持家の帰属家賃を除く総合")]:
        S.append(cpi(meas, item, ttl, "m"))
        S.append(cpi(f"{meas}_yoy", item, f"{ttl} 前年同月比", "m", tab="3", unit="％"))
        S.append(cpi(meas, item, ttl, "a"))
        S.append(cpi(meas, item, ttl, "fy"))
        # 前年比（暦年）・前年度比（年度）＝表章項目 tab=2「前月比・前年比・前年度比」の年・年度の行。
        # 同 tab の月次は前月比なので月次系列は作らない（前年同月比は tab=3）。
        yoy_note = ("表章項目＝前月比・前年比・前年度比（e-Stat 0003427113 の cdTab=2）のうち"
                    "{gran}の行＝{label}。前年同月比（月次）は cdTab=3 の別系列。"
                    "2025年基準（e-Stat 0004052037・2026-08 公開）は別系列（基準改定＝接続しない）。")
        S.append(cpi(f"{meas}_yoy", item, f"{ttl} 前年比", "a", tab="2", unit="％", first_period="1971",
                     notes=yoy_note.format(gran="暦年", label="前年比")))
        S.append(cpi(f"{meas}_yoy", item, f"{ttl} 前年度比", "fy", tab="2", unit="％", first_period="FY1971",
                     notes=yoy_note.format(gran="年度", label="前年度比")))
        # ---- CPI 2025年基準（第 10 弾 第 1 便・2026-08-29。現行基準＝2026年7月分から。品目コードは 2020 基準と共通・
        #      総務省接続の遡及系列で 1970 年から。2020 基準は superseded_by で降格＝発見層は現行基準が先）----
        S.append(cpi(meas, item, ttl, "m", base="2025"))
        S.append(cpi(f"{meas}_yoy", item, f"{ttl} 前年同月比", "m", tab="3", unit="％", base="2025"))
        S.append(cpi(meas, item, ttl, "a", base="2025"))
        S.append(cpi(meas, item, ttl, "fy", base="2025"))
        S.append(cpi(f"{meas}_yoy", item, f"{ttl} 前年比", "a", tab="2", unit="％", first_period="1971", base="2025"))
        S.append(cpi(f"{meas}_yoy", item, f"{ttl} 前年度比", "fy", tab="2", unit="％", first_period="FY1971", base="2025"))
    # ---- 労働力調査（e-Stat 確定・取込対象） ----
    S += [roudou_m("labour_force", "01", "労働力人口"), roudou_m("employed", "02", "就業者"),
          roudou_m("unemployed", "08", "完全失業者")]
    # 完全失業率（％）＝人数表とは別表の長期時系列（月次 1968年1月〜／暦年・年度 1953年〜）
    S += [roudou_rate("m"), roudou_rate("a"), roudou_rate("fy")]

    # ---- 国民経済計算 年次推計（ESRI 一次 xlsx・主要系列表(1) 国内総生産（支出側）年度＝取込対象） ----
    S += [sna_gdp_fy("gdp_nominal", "ffm1n", "名目国内総生産（支出側）", "10億円", "５．　国内総生産（支出側）(1+2+3+4)",
                     "名目", notes="単位10億円（換算しない）。1994年度以降＝2020年基準・2008SNA。1993年度以前は基準の異なる遡及系列（別系列・接続しない）。"),
          sna_gdp_fy("gdp_real", "ffm1rn", "実質国内総生産（支出側・連鎖方式）", "10億円", "５．　国内総生産（支出側）", "実質：連鎖方式",
                     basis_extra="（2020暦年連鎖価格）",
                     notes="連鎖方式・2020暦年連鎖価格。連鎖のため内訳の合計は総額に一致しない（加算しない）。1993年度以前は接続しない。"),
          sna_gdp_fy("deflator", "ffm1dn", "GDPデフレーター（支出側・連鎖方式）", "指数（2020暦年=100）", "５．　国内総生産（支出側）",
                     "デフレーター：連鎖方式", notes="2020暦年=100。名目÷実質×100 とは丸めの分だけ一致しないことがある（派生計算はしない）。")]

    # ---- 第4弾 マクロの深化：SNA 他表（ESRI xlsx）・QE CSV・日銀 gap.xlsx ＝取込対象 ----
    esri = ESRI_TOP
    S += sna_series()
    # 旧基準（68SNA/93SNA）＝現行と接続しない別系列（dataset=sna1990 / sna2000）
    S += sna_legacy_series()
    # 固定資本減耗（付表16）と資本ストック（ストック編2）＝グロス投資と純投資の橋渡し
    S += sna_capital_series()
    S.append(Series(series_id="cao.sna2020.labour_share.fy", title="労働分配率（SNA：雇用者報酬／国民所得）＝派生・構成系列を参照", org="cao", org_name="内閣府",
                    source_url=esri, sector="マクロ", unit="", granularity="年度", freq="fy", dataset="sna2020", measure="labour_share",
                    policy_tags=(TAG_LABOR, TAG_MACRO), components=("cao.sna2020.compensation_employees.fy", "cao.sna2020.national_income.fy"),
                    notes="派生値は計算しない。定義が複数あるため構成系列を返す（参照粒度設計 §6）。", status="registered"))
    S += qe_series()
    S += boj_gap_series()
    S += cao_gap_series()
    # ---- 第5弾(b) 法人企業統計：付加価値の内訳・営業外・投資有価証券を4区分へ拡張＋派生2種 ----
    S += hojin_size_expand({x.series_id for x in S})
    S += hojin_derived()
    # ---- 第6弾 業種別パネル Tier 1 ----
    S += hojin_industry_panel({x.series_id for x in S})
    # ---- 以下 planned（取得元確定・取込は次段） ----
    bojts = "https://www.stat-search.boj.or.jp/"
    S.append(planned("boj.tankan.bsi.large-mfg.q", "短観 業況判断DI 大企業・製造業（最近）", "boj", "日本銀行", "企業", "％ポイント", "q", "四半期", "tankan", "bsi",
                     (TAG_MACRO, TAG_CORP), {"type": "boj_ts", "code": "TBD"}, "全国企業短期経済観測調査", bojts, dims="large-mfg"))
    # ---- 第 10 弾 第 1 便（2026-08-29）＝為替・コールレート・基準貸付利率（主要時系列 mtshtml）＋企業物価（フラットファイル）----
    mts = "https://www.stat-search.boj.or.jp/ssi/mtshtml/"
    S.append(_s(series_id="boj.fx.usdjpy_avg.m", title="東京市場 ドル・円 スポット 17時時点 月中平均（円/ドル）",
                org="boj", org_name="日本銀行", source_url=mts + "fm08_m_1.html", sector="物価・金融", unit="円/ドル",
                granularity="月次", freq="m", dataset="fx", measure="usdjpy_avg",
                stat_name="外国為替相場状況（東京インターバンク相場）", table_id="fm08_m_1", table_title="為替相場（月次）",
                policy_tags=(TAG_FIN, TAG_TRADE), first_period="1980-01",
                accessor={"type": "boj_mtshtml", "page": "fm08_m_1.html", "code": "FM08'FXERM07"},
                citation_template="日本銀行 主要時系列統計データ表 為替相場（fm08・東京市場 ドル・円 スポット 17時時点/月中平均／{period}）取得 {retrieved_at}",
                notes="為替レート（円相場・ドル円）。原典の収録開始は 1973/01 だが主要時系列表の表示範囲＝1980/01〜を収録。"
                      "日次は fxdaily（PDF）＝未収録。", status="registered"))
    for meas, page, code, ttl, first, note in (
            ("call_on_avg", "fm02_m_1.html", "FM02'STRACLUCON", "無担保コールレート O/N 月平均", "1985-07",
             "政策金利（誘導目標）の実勢＝無担保コールレート（オーバーナイト物）の月平均。誘導目標値そのものは日銀公表文で確認。"),
            ("basic_loan_rate", "ir01_m_1.html", "IR01'MADR1M", "基準割引率および基準貸付利率（月末）", "1980-01",
             "旧公定歩合。補完貸付の適用金利＝政策金利（無担保コール誘導目標）とは別の系列（混ぜない）。")):
        S.append(_s(series_id=f"boj.rates.{meas}.m", title=f"{ttl}（月次・年％）",
                    org="boj", org_name="日本銀行", source_url=mts + page, sector="物価・金融", unit="年％",
                    granularity="月次", freq="m", dataset="rates", measure=meas,
                    stat_name="コールレート・基準貸付利率（主要時系列統計データ表）", table_id=page.replace(".html", ""),
                    table_title=ttl, policy_tags=(TAG_FIN, TAG_MACRO), first_period=first,
                    accessor={"type": "boj_mtshtml", "page": page, "code": code},
                    citation_template=f"日本銀行 主要時系列統計データ表（{page.replace('.html', '')}・{ttl}／" + "{period}）取得 {retrieved_at}",
                    notes=note, status="registered"))
    # ---- 第 10 弾 第 2 便（2026-08-29）＝米 BLS（CPI・失業率）・Eurostat（HICP・失業率）＝月次の海外指標 ----
    for meas, code, ttl, unit, tags, note in (
            ("cpi_u", "CUUR0000SA0", "米国 消費者物価指数（CPI-U・全品目・原数値）", "指数（1982-84年=100）", (TAG_MACRO,),
             "原数値（NSA）。前年比は収録しない（BLS API v1 は指数のみ＝計算しない）。"),
            ("cpi_u_sa", "CUSR0000SA0", "米国 消費者物価指数（CPI-U・全品目・季節調整済）", "指数（1982-84年=100）", (TAG_MACRO,),
             "季節調整済（SA）。"),
            ("unemployment_rate", "LNS14000000", "米国 失業率（季節調整済）", "％", (TAG_LABOR, TAG_MACRO),
             "季節調整済。IMF WEO の年平均失業率（imf.weo.unemployment_rate）とは別系列。")):
        S.append(_s(series_id=f"bls.us.{meas}.m.cty", title=f"{ttl}（月次）",
                    org="bls", org_name="米国労働統計局（BLS）", source_url="https://www.bls.gov/data/",
                    sector="対外・国際", unit=unit, granularity="月次", freq="m", dataset="us",
                    measure=meas, region_level="cty", region_codes=("USA",),
                    stat_name="BLS Public Data API", table_id=code, table_title="BLS timeseries",
                    policy_tags=tags, first_period="1970-01",
                    accessor={"type": "bls_api", "series": code, "start": 1970},
                    citation_template=f"米国労働統計局（BLS）Public Data API（{code}・{ttl}／" + "{period}）取得 {retrieved_at}",
                    notes=note + " 1970年から収録（原典はさらに遡る＝API 枠の節約で 1970 起点）。region=USA。", status="registered"))
    EURO4 = ("DE", "FR", "IT", "ES")
    for meas, ds_, params, ttl, unit, tags, note in (
            ("hicp_yoy", "prc_hicp_manr", {"coicop": "CP00", "unit": "RCH_A"}, "HICP 総合 前年同月比", "％（前年同月比）", (TAG_MACRO,),
             "★実測（2026-08-29）＝Eurostat の提供が 2025-12 で止まっている（原典側の提供状況＝埋めない）。"),
            ("unemployment_rate", "une_rt_m", {"s_adj": "SA", "age": "TOTAL", "sex": "T", "unit": "PC_ACT"},
             "失業率（季節調整済・15〜74歳）", "％（労働力人口比）", (TAG_LABOR, TAG_MACRO),
             "季節調整済。2026-06/07 まで現行（実測）。")):
        S.append(_s(series_id=f"eurostat.eu.{meas}.m.cty", title=f"Eurostat {ttl}（独仏伊西・月次）",
                    org="eurostat", org_name="欧州連合統計局（Eurostat）",
                    source_url=f"https://ec.europa.eu/eurostat/databrowser/product/view/{ds_}",
                    sector="対外・国際", unit=unit, granularity="月次", freq="m", dataset="eu",
                    measure=meas, region_level="cty", region_codes=("DEU", "FRA", "ITA", "ESP"),
                    stat_name=f"Eurostat {ds_}", table_id=ds_, table_title=ttl,
                    policy_tags=tags, first_period="1997-01",
                    accessor={"type": "eurostat_api", "dataset": ds_, "params": params, "geo": list(EURO4)},
                    citation_template=f"Eurostat（{ds_}・{ttl}・国別／" + "{period}）取得 {retrieved_at}",
                    notes=note + " 国別（ISO3）のみ収録＝EA20/EU27 等の集計値は ISO3 でないため収録しない（WEO と同じ判断）。", status="registered"))
    for meas, code, ttl in (("domestic", "PRCG20_2200000000", "国内企業物価指数 総平均"),
                            ("export_yen", "PRCG20_2400000000", "輸出物価指数（円ベース）総平均"),
                            ("import_yen", "PRCG20_2600000000", "輸入物価指数（円ベース）総平均")):
        S.append(_s(series_id=f"boj.cgpi.{meas}.m", title=f"企業物価指数（2020年基準） {ttl}（月次）",
                    org="boj", org_name="日本銀行", source_url="https://www.stat-search.boj.or.jp/info/dload.html",
                    sector="物価・金融", unit="指数（2020年=100）", granularity="月次", freq="m", dataset="cgpi",
                    measure=meas, basis="2020年基準", stat_name="企業物価指数", table_id="cgpi_m_jp",
                    table_title="企業物価指数（フラットファイル・月次）", policy_tags=(TAG_MACRO, TAG_FIN), first_period="2020-01",
                    accessor={"type": "boj_flat", "zip": "cgpi_m_jp.zip", "file": "cgpi_m_jp.csv", "layout": "wide",
                              "code": code, "period_kind": "yyyymm", "first_col": 3},
                    citation_template=f"日本銀行「企業物価指数」2020年基準（フラットファイル cgpi_m_jp・{ttl}／" + "{period}）取得 {retrieved_at}",
                    notes="指数のみ（前年比はフラットファイルに無い＝計算しない。前年比の公表値は日銀の公表資料で確認）。"
                          "2020年基準＝2020/01〜。それ以前の基準の長期系列は未収録（基準改定＝接続しない）。", status="registered"))
    S += jgb_series()
    S += boj_flat_series()
    S += zaisei_series()
    S += jinko_series()
    S += ipss_series()
    S += maikin_series()         # 第 11 弾 第 1 便（2026-09-15）＝毎勤 長期時系列 実数・指数累積データ（planned→registered）
    S += shokugyo_series()       # 第 11 弾 第 6 便（2026-09-15）＝一般職業紹介状況 長期時系列表（e-Stat カタログ→xlsx・planned→registered）
    S += mitoshi_series()        # 第 11 弾 第 7 便（2026-09-15）＝政府経済見通し（閣議決定・年央試算）PDF 主要経済指標（kind=projection）
    S += shunto_series()
    S += chiho_zaisei_series()
    S += chiho_keikaku_series()
    # ---- 第3弾 国際比較（IMF DataMapper／OECD SDMX／世銀）＝取込対象。region=ISO3・OECD加盟38＋主要非加盟8（OECD 系列は収録国のみ） ----
    S += intl_series()
    S += sna_activity_series()   # 第 7 弾 段 C（SNA 経済活動別・付表 2/3）
    S += hakusho_sme_series()    # 第 7 弾 補遺（開廃業率・中小企業白書 PDF）
    S += sna_sector_series()     # 第 8 弾 第 1 便（制度部門別勘定の残り・制度部門別 BS）
    S += boj_fof_sector_series() # 第 8 弾 第 2 便（資金循環 部門別・年度）
    S += sna_fixed_capital_stock_series() + roudou_emp_type_series() + shaho_series()  # 第 8 弾 第 2 便（F-4／F-6／F-7）
    return S


# 発見層の別名（(series_id 群, 追記する語) の列）。検索語として当てたい俗称・話し言葉を notes の文として書く。
# 追加の規律：find_quality に問を立て（todo）、ここに別名を足して pass に昇格させる（テストドリブン）。
DISCOVERY_ALIASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("cao.qe2020.gdp_real_qoq.sa.q",), "実質GDP成長率（前期比）。"),
    (("cao.qe2020.gdp_real_qoq_ann.sa.q", "cao.qe2020.gdp_nominal_qoq_ann.sa.q"), "GDP成長率（前期比年率）。"),
    (("cao.qe2020.private_consumption_real.sa.q", "cao.qe2020.private_consumption_nominal.sa.q"), "個人消費。"),
    (("cao.qe2020.priv_capex_real.sa.q", "cao.qe2020.priv_capex_nominal.sa.q"), "設備投資（民間企業設備）。"),
    (("cao.qe2020.public_investment_real.sa.q", "cao.qe2020.public_investment_nominal.sa.q"), "公共投資（公的固定資本形成）。"),
    (("soumu.cpi2020.cpi_ex_fresh.m", "soumu.cpi2020.cpi_ex_fresh_yoy.m"), "コアCPI（生鮮食品を除く総合）。"),
    (("soumu.cpi2020.cpi_ex_fresh_energy.m", "soumu.cpi2020.cpi_ex_fresh_energy_yoy.m"), "コアコアCPI（生鮮食品及びエネルギーを除く総合）。"),
    (("soumu.cpi2020.cpi_all_yoy.m", "soumu.cpi2020.cpi_all_yoy.a", "soumu.cpi2020.cpi_all_yoy.fy"),
     "物価上昇率・インフレ率（前年比。月次は前年同月比）。"),
    (("soumu.roudou.employed.total.m",), "就業者数。"),
    (("soumu.roudou_emp.nonregular.total.q",), "非正規雇用（非正規の職員・従業員数）。"),
    (("soumu.roudou_emp.regular.total.q",), "正規雇用（正規の職員・従業員数）。"),
    (("mhlw.maikin.wage_total_real.total-5plus.m",), "実質賃金（現金給与総額の実質指数）。"),
    (("mhlw.maikin.wage_total_nominal.total-5plus.m",), "名目賃金（現金給与総額の名目指数）。"),
    (("mhlw.maikin.wage_total_real_yoy.total-5plus.m", "mhlw.maikin.wage_total_real_yoy.total-5plus.a"),
     "実質賃金の伸び率（報道の「実質賃金 前年比」＝就業形態計・5人以上）。"),
    (("mhlw.maikin.wage_total_nominal_yoy.total-5plus.m", "mhlw.maikin.wage_total_nominal_yoy.total-5plus.a"), "名目賃金の伸び率（現金給与総額 前年比）。"),
    (("mhlw.maikin.wage_total.total-5plus.m", "mhlw.maikin.wage_total.total-5plus.a"), "平均給与・月給（現金給与総額の月平均額・毎月勤労統計）。"),
    (("mhlw.maikin.hours_overtime.total-5plus.m",), "残業時間（所定外労働時間）。"),
    (("mhlw.shokugyo.job_openings_ratio.sa.m",), "求人倍率（ハローワーク・報道の有効求人倍率＝季節調整値）。"),
    (("cao.mitoshi.gdp_nominal.fy", "cao.mitoshi.gdp_real_growth.fy"), "政府経済見通し（閣議決定）＝翌年度の名目GDP・成長率の予測・見込み。"),
    (("keidanren.shunto.wage_hike_rate.large.a", "keidanren.shunto.wage_hike_rate.sme.a"), "春闘（春季労使交渉）の賃上げ率。"),
    (("mof.hojin.retained_earnings.allexfin-allsize.fy",), "内部留保（利益剰余金）。"),
    (("mof.hojin.net_assets.allexfin-allsize.fy",), "自己資本（純資産）。"),
    (("mof.zaisei.tax_stamp_revenue.budget.fy", "mof.zaisei.tax_stamp_revenue.settled.fy"), "税収（租税及印紙収入）。"),
    (("mof.zaisei.defense.initial.fy", "mof.zaisei.defense.settled.fy"), "防衛費（防衛関係費）。"),
    (("mof.jgb.yield.10y.d",), "長期金利（10年物国債の利回り）。"),
    (("boj.fx.usdjpy_avg.m",), "為替レート（円相場・ドル円）。"),
    (("soumu.jinko.pop_15_64.a.pref", "ipss.pop2023.pop_15_64.medium-medium.a"), "生産年齢人口（15〜64歳）。"),
    (("imf.weo.gov_debt_gdp.a.cty",), "政府債務残高（対GDP比）の国際比較。"),
    (("oecd.earnings.avg_wage_usd_ppp_const.a.cty", "oecd.earnings.avg_wage_lcu_current.a.cty"), "平均賃金の国際比較。"),
    (("oecd.stan.value_added.total_activities.a.cty",), "業種別（経済活動別）付加価値の国際比較。"),
    # ── バッチ 2（2026-08-29・find_quality 200 問化の todo 20）──
    (("mof.hojin.sales.allexfin-cap10m-100m.fy", "mof.hojin.labour_share.allexfin-cap10m-100m.fy"), "中小企業（資本金1千万〜1億円）。"),
    (("mof.hojin.sales.allexfin-cap1b.fy",), "大企業（資本金10億円以上）。"),
    (("mof.hojin.officer_compensation.allexfin-allsize.fy",), "役員報酬（役員給与）。"),
    (("mof.hojin.cash_deposits.allexfin-allsize.fy",), "手元資金（現金・預金）。"),
    (("imf.weo.gdp_nominal_usd.a.cty",), "ドル建て（米ドル表示）の名目GDP。"),
    (("cao.sna_activity.gdp.health_social.a",), "医療福祉（保健衛生・社会事業）。"),
    (("cao.sna_activity.hours_worked_per_employee.total.fy", "cao.sna_activity.hours_worked_per_employee.total.a"),
     "産業別（経済活動別）の労働時間の総括値。"),
    (("boj.fof.stock.assets_total.hh.fy",), "家計の金融資産（残高合計）。"),
    (("boj.fof.stock.liabilities_debt_securities.gg.fy",), "国債残高（一般政府の債務証券・資金循環ベース）。"),
    (("boj.fof.stock.liabilities_loans.nfc.fy",), "企業の借入金残高。"),
    (("cao.sna_sector_bs.financial_assets.hh.a",), "家計の金融資産残高（期末）。"),
    (("mof.zaisei.public_works.initial.fy", "mof.zaisei.public_works.settled.fy"), "公共事業費（公共事業関係費）。"),
    (("soumu.jinko.pop_75plus.a.pref",), "後期高齢者（75歳以上）。"),
    (("ipss.pop2023.pop_65plus.medium-medium.a",), "高齢者人口（65歳以上）の将来推計。"),
    (("imf.weo.gdp_real_growth.a.cty",), "経済成長率の国際比較。"),
    (("oecd.pdb.lp_growth_contrib_capital_deepening.a.cty",), "資本装備率（資本深化）の寄与。"),
    (("oecd.sdbs.value_added_basic_prices.bizecon_exfin-allsize.a.cty",), "従業者規模別の付加価値（規模区分は emp スラグ）。"),
    (("oecd.stan.employment.total_activities.a.cty",), "業種別（経済活動別）雇用の国際比較。"),
    (("eurostat.eu.unemployment_rate.m.cty",), "欧州（独仏伊西）の失業率。"),
    (("eurostat.eu.hicp_yoy.m.cty",), "欧州（独仏伊西）のインフレ率・消費者物価。"),
    # ── バッチ 3（2026-08-29・第 10 弾 第 1 便＝CPI 2025 年基準へも現行基準として同じ別名を張る）──
    (("soumu.cpi2025.cpi_ex_fresh.m", "soumu.cpi2025.cpi_ex_fresh_yoy.m"), "コアCPI（生鮮食品を除く総合）。"),
    (("soumu.cpi2025.cpi_ex_fresh_energy.m", "soumu.cpi2025.cpi_ex_fresh_energy_yoy.m"), "コアコアCPI（生鮮食品及びエネルギーを除く総合）。"),
    (("soumu.cpi2025.cpi_all_yoy.m", "soumu.cpi2025.cpi_all_yoy.a", "soumu.cpi2025.cpi_all_yoy.fy"),
     "物価上昇率・インフレ率（前年比。月次は前年同月比）。"),
)


def main(path: Path = REGISTRY_PATH) -> None:
    import dataclasses
    series = [dataclasses.replace(x, license=license_for(x)) if not x.license else x for x in build()]
    # 発見層の別名（俗称・話し言葉・官庁呼称差）＝ notes に語を足すだけ（値・定義・表題は不変。find の strength1 で当たる）。
    # 出自＝find_quality の todo 24 問（2026-08-29 初回実測 76.5%）。series 単位で刻む（dataset 単位だと hojin 全細分に付いて flood する）。
    alias_map = {sid: alias for sids, alias in DISCOVERY_ALIASES for sid in sids}
    used: set = set()
    out_series = []
    for x in series:
        a = alias_map.get(x.series_id)
        if a:
            used.add(x.series_id)
            x = dataclasses.replace(x, notes=(x.notes + ("" if not x.notes or x.notes.endswith("。") else "。") + a))
        out_series.append(x)
    unused = set(alias_map) - used
    if unused:
        raise SystemExit(f"DISCOVERY_ALIASES に存在しない series_id: {sorted(unused)}")
    series = out_series
    reg = Registry()
    for s in series:
        reg.register(s)  # 検証（違反があれば例外）
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("# 生成物: python -m stats.ingest.seed_registry（定義は stats/ingest/seed_registry.py が正）\n")
        for s in series:
            d = s.payload()
            for k in ("corpus", "layer", "date_int", "scope"):  # scope は region_level から導出（保存しない）
                d.pop(k, None)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    n_reg = sum(1 for s in series if s.status == "registered")
    n_pl = sum(1 for s in series if s.status == "planned")
    n_gd = sum(1 for s in series if s.status == "guide")
    print(f"レジストリ書き出し: {path} 系列 {len(series)}（取込対象 {n_reg}・planned {n_pl}・guide {n_gd}）")


if __name__ == "__main__":
    main()
