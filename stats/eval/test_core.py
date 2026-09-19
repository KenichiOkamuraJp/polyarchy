"""stats.core の単体テスト（ネットワーク不要）：期間表記の厳密解釈・時間コード変換・レジストリ検証。
実行（リポジトリ root）：  python -m stats.eval.test_core
"""
from stats.core.periods import convert, matches_freq, parse
from stats.core.registry import Registry, Series, default_registry


def main() -> int:
    ok = True
    def chk(cond, msg):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + msg)
        ok &= bool(cond)
    print("=== stats.core 単体テスト ===")
    chk(parse("2024").freq == "a" and parse("FY2024").freq == "fy", "暦年/年度の解釈")
    chk(parse("2024Q1").freq == "q" and parse("FY2024Q1").freq == "fq" and parse("FY2024H2").freq == "h", "四半期/年度四半期/半期")
    chk(parse("2024-03").freq == "m" and parse("2024-13") is None and parse("2024-03-31E").freq == "d", "月/日（不正月は None）")
    chk(parse("2024年") is None and parse("FY24") is None and parse(" 2024 ").freq == "a", "表記外は None・前後空白のみ許容")
    chk(matches_freq("2024", "irr") and not matches_freq("2024", "fy") and not matches_freq("FY2024", "a"), "freq 厳密一致")
    chk(convert("estat_hojin_fy", "19600") == "FY1960" and convert("estat_hojin_fy", "1960") is None, "法企 年度コード")
    chk(convert("estat_hojin_fq", "19542") == "FY1954Q1" and convert("estat_hojin_fq", "20251") == "FY2024Q4", "法企 四半期コード（1-3月は前年度Q4）")
    chk(convert("estat_cpi_time", "2026000707") == "2026-07" and convert("estat_cpi_time", "2025100000") == "FY2025"
        and convert("estat_cpi_time", "2025000000") == "2025" and convert("estat_cpi_time", "2025000708") is None, "CPI/労調 時間コード")
    reg = default_registry()
    chk(len(reg.series) >= 500, f"既定レジストリ読込 {len(reg.series)} 系列（2026-08 時点 528・大量欠落を検知するため下限 500）")
    chk(all(not s.violations() for s in reg.series.values()), "全系列が契約検証を通る")
    bad = Series(series_id="Mof.hojin.x.fy", title="t", org="mof", source_url="https://x", accessor={"type": "estat"}, freq="fy")
    chk(any("命名規則" in e for e in bad.violations()), "命名規則違反を検出")
    bad2 = Series(series_id="ipss.pop.x.a", title="t", org="ipss", source_url="https://x", accessor={"type": "ipss_xlsx"}, freq="a", kind="projection")
    chk(any("projection" in e for e in bad2.violations()), "projection の必須欄欠落を検出")
    # 発見層ゴールデン（stats-v1 基線・2026-08-21）：拡充で系列が増えても基線の代表系列が limit 内から押し出されないこと
    import json
    from stats.core.paths import DATA_DIR
    gold = [json.loads(l) for l in (DATA_DIR / "eval" / "find_golden.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    # ファミリー畳み（段 2）：既定は細分を畳み全体系列を残す／industry・size 指定で展開
    rows, fams, total = reg.search_collapsed("売上高")
    chk(total >= 60 and len(fams) == 1 and fams[0]["id_pattern"] == "mof.hojin.sales.{industry}-{size}.fy"
        and all(s.dims in ("allexfin-allsize", "") for s in rows), "『売上高』＝細分を 1 ファミリーに畳み series は全体系列のみ")
    chk(fams[0]["series_count"] + len(rows) == total and any("allsize" in c["sizes"] and "capu10m" in c["sizes"] for c in fams[0]["combinations"]),
        "ファミリーの series_count＋series 行＝total・combinations に全規模＋規模 4 区分（Tier 2）")
    rows2, fams2, _ = reg.search_collapsed("売上高", industry="mfg_food")
    chk(len(rows2) == 5 and not fams2 and {reg.split_dims(s.dims)[1] for s in rows2} == {"allsize", "cap1b", "cap100m-1b", "cap10m-100m", "capu10m"},
        "industry 指定＝畳まず行で返す（全規模＋4 区分）")
    rows3, fams3, total3 = reg.search_collapsed("売上高", size="cap1b", limit=100)
    chk(total3 >= 62 and len(rows3) == total3 and not fams3 and all(reg.split_dims(s.dims)[1] == "cap1b" for s in rows3),
        "size 指定＝畳まず行で返す（Tier 2 後は業種 62 × cap1b）")
    _, fams4, _ = reg.search_collapsed("労働分配率")
    chk(not fams4, "細分が閾値（3）以下なら畳まない（労働分配率＝派生・規模 3 区分）")
    # 第 7 弾 段 A（2026-08-22・要件源＝利用側プロジェクト ドッグフーディング）：偽の「ない」を返さない
    # S-1 複数語クエリ＝空白区切りの AND（以前はクエリ丸ごとの部分一致で常に 0 件）
    chk(reg.search("固定資本減耗 民間", dataset="sna2020")[1] > 0 and reg.search("法人企業統計 売上高 製造業")[1] > 0,
        "S-1 複数語クエリが AND で当たる（固定資本減耗 民間／法人企業統計 売上高 製造業）")
    th = reg.token_hits("固定資本減耗 雇用者報酬 純付加価値", dataset="sna2020")
    chk(th["固定資本減耗"] > 0 and th["雇用者報酬"] > 0 and th["純付加価値"] == 0, "S-1 0 件診断＝語ごとの単独該当数（純付加価値は本当に未収録＝0）")
    chk(reg.search("介護報酬 診療報酬")[1] == 0 and not any(reg.token_hits("介護報酬 診療報酬").values()), "S-1 本当に無い語は 0 件のまま（推測で返さない）")
    # S-4 融合複合語＝英数字と和字の境界で分割（第 9 弾・2026-08-29：「実質GDP」scope=jp が 0 件で利用者が ESRI へ出た実ログ）
    chk(reg.tokenize("実質GDP") == ["実質", "GDP"] and reg.tokenize("GDPギャップ") == ["GDP", "ギャップ"]
        and reg.tokenize("e-Stat") == ["e-Stat"] and reg.tokenize("実質GDP 四半期") == ["実質", "GDP", "四半期"],
        "S-4 tokenize＝融合複合語を境界で分割・ASCII 内の記号（e-Stat）は分割しない")
    chk(reg.search("実質GDP", scope="jp")[1] > 0 and any(s.series_id == "cao.qe2020.gdp_real.sa.q" for s in reg.search("実質GDP", scope="jp")[0]),
        "S-4 「実質GDP」scope=jp が QE 実質系列に当たる（2026-08-28 実ログの 0 件を再現させない）")
    chk(any(s.series_id == "boj.gap.output_gap.q" for s in reg.search("GDPギャップ")[0]),
        "S-4 「GDPギャップ」（内閣府の呼称）が日銀 需給ギャップに当たる")
    # 第 9 弾 発見層の畳み軸の一般化（2026-08-29）：variant 軸（qe2020）と freq 軸（cao_gap）
    vrows, vfams, vtotal = reg.search_collapsed("実質GDP", limit=20)
    chk(any(s.series_id == "cao.qe2020.gdp_real.sa.q" for s in vrows)
        and not any(s.dataset == "qe2020" and s.measure == "private_consumption_real" for s in vrows)
        and any(f.get("axis") == "variant" and f.get("variant") == "real" for f in vfams),
        "variant 畳み＝GDP は行に残り・需要項目は variant カードへ（実質GDP）")
    vf = next(f for f in vfams if f.get("variant") == "real")
    chk(vf["id_pattern"] == "cao.qe2020.{item}_real.sa.q" and any(it["slug"] == "private_consumption" for it in vf["items"])
        and vf["series_count"] == len(vf["items"]),
        "variant カード＝id_pattern・items 語彙・series_count が整合")
    ds_seen = {s.dataset for s in vrows}
    chk("sna2020" in ds_seen or "weo" in ds_seen or "wdi" in ds_seen,
        "variant 畳みで qe2020 の独占が解け、年次 SNA・国際比較が top20 に戻る（劣化対策の目的）")
    frows, ffams, _ = reg.search_collapsed("GDPギャップ", limit=20)
    chk(any(s.series_id == "cao.cao_gap.gdp_gap.q" for s in frows)
        and not any(s.series_id in ("cao.cao_gap.gdp_gap.a", "cao.cao_gap.gdp_gap.fy") for s in frows),
        "freq 畳み＝最も細かい四半期を行に残す（利用側が見落とした .q が構造的に前へ）")
    ff = next(f for f in ffams if f.get("axis") == "freq" and f.get("measure") == "gdp_gap")
    chk(ff["kept"] == "cao.cao_gap.gdp_gap.q" and {x["freq"] for x in ff["freqs"]} == {"q", "a", "fy"}
        and ff["id_pattern"] == "cao.cao_gap.gdp_gap.{freq}",
        "freq カード＝kept・freqs（全周期の series_id）・id_pattern")
    erows, efams, _ = reg.search_collapsed("GDPギャップ", limit=50, freq="a")
    chk(any(s.series_id == "cao.cao_gap.gdp_gap.a" for s in erows) and not any(f.get("axis") == "freq" for f in efams),
        "freq 指定＝freq 軸は畳まず行で返す（escape hatch）")
    chk(any(s.series_id == "cao.qe2020.gdp_real_qoq.sa.q" for s in reg.search_collapsed("実質GDP前期比", limit=20)[0]),
        "variant 単独系列（gdp_real_qoq）は畳まれず行のまま")
    # S-5 英字の大小無視（2026-08-29 利用側指摘：年次 SNA の表題は「実質国内総生産」＝ローマ字 GDP は id の gdp にしか無い）
    jp_rows = reg.search_collapsed("実質GDP", scope="jp", limit=20)[0]
    chk(any(s.series_id == "cao.sna2020.gdp_real.fy" for s in jp_rows) and any(s.series_id == "cao.sna2020.gdp_real.a" for s in jp_rows),
        "S-5 「実質GDP」scope=jp に年次 SNA 実質GDP本体（fy・a）が行で出る（casefold）")
    chk(reg.search("gdpデフレーター")[1] > 0 and reg.search("GDPデフレーター")[1] == reg.search("gdpデフレーター")[1],
        "S-5 英字の大小で該当数が変わらない")
    chk(ff["series_count"] == len(ff["freqs"]) - 1 and any(x.get("kept_in_series") for x in ff["freqs"]),
        "freq カード＝series_count は畳んだ数（kept は kept_in_series で明示）")
    # S-2 目録の measures／units は全件（以前は [:40]／[:6] で value_added 等 6 件・sna2020 26 件が黙って消えていた）
    cat = {d["dataset"]: d for d in reg.catalog()}
    miss = {ds: sorted({x.measure for x in reg.series.values() if x.dataset == ds and x.measure} - set(cat[ds]["measures"])) for ds in cat}
    chk(not any(miss.values()), f"S-2 目録の measures ⊇ レジストリの measure 集合（全 dataset）{ {k: v for k, v in miss.items() if v} }")
    chk("value_added" in cat["hojin"]["measures"] and "tangible_fixed_assets" in cat["hojin"]["measures"] and "stock.land" in cat["sna2020"]["measures"],
        "S-2 value_added・tangible_fixed_assets・stock.land が目録に出る")
    chk(all(set(cat[ds]["units"]) == {x.unit for x in reg.series.values() if x.dataset == ds and x.unit} for ds in cat), "S-2 目録の units も全件")
    # S-3 unknown_series の診断＝measure はあるが dims の組合せが無いとき、収録済みの組合せを返す
    cov = reg.diagnose_unknown("mof.hojin.sales.nosuch_industry-cap1b.fy")
    chk(cov and cov["measure"] == "sales" and cov["requested_dims"] == "nosuch_industry-cap1b" and any("allexfin" in c["industries"] for c in cov["combinations"]),
        "S-3 measure 収録済み＋dims 不一致＝coverage.combinations を返す")
    chk(reg.diagnose_unknown("mof.hojin.nosuch_measure.allexfin-allsize.fy") is None and reg.diagnose_unknown("xx") is None, "S-3 measure 自体が無ければ None")
    # §5.4 利用者が「残してほしい」と明記した注記（付加価値は定義値を直接引く・合成しない）
    from stats.core.hojin_vocab import HOJIN_ANALYSIS_NOTES
    chk("定義値" in HOJIN_ANALYSIS_NOTES["value_added"] and "合成しない" in HOJIN_ANALYSIS_NOTES["value_added"]
        and "analysis_notes" in cat["hojin"] and cat["hojin"]["analysis_notes"]["value_added"] == HOJIN_ANALYSIS_NOTES["value_added"],
        "analysis_notes.value_added＝『定義値を直接引く・合成しない』が目録に載る（利用側プロジェクト §5.4）")
    # 段 B：land／fixed_assets／total_assets／net_assets が業種×規模の全セルにある（D-3）
    for meas in ("land", "fixed_assets", "total_assets", "net_assets"):
        n = sum(1 for x in reg.series.values() if x.dataset == "hojin" and x.measure == meas)
        chk(n >= 306 and reg.get(f"mof.hojin.{meas}.mfg_food-cap1b.fy") is not None, f"段 B {meas}＝業種×規模 {n} 系列（mfg_food-cap1b あり）")
    # 第 8 弾 第 1 便（2026-08-24）：制度部門別勘定の恒等式が閉じる（値ストアがあるときだけ）＋注記
    from stats.core.values import ValueStore as _VS
    _vs = _VS()

    def _v(sid, p):
        r = _vs.lookup(sid, p, "JP")
        return float(r.value) if r else None
    if _vs.has_data("cao.sna_sector.saving_net.nfc.fy"):
        bad = []
        for slug in ("nfc", "fin", "gg", "hh", "npish"):
            for y in ("FY2024", "FY2015", "FY2000"):
                parts = [_v(f"cao.sna_sector.{m}.{slug}.fy", y) for m in ("saving_net", "capital_transfers_received", "capital_transfers_paid", "gross_fixed_capital_formation")]
                cfc, nl = _v(f"cao.sna2020.cfc_sector.{slug}.fy", y), _v(f"cao.sna2020.net_lending.{slug}.fy", y)
                inv = _v(f"cao.sna_sector.inventory_change.{slug}.fy", y) or 0
                land = _v(f"cao.sna_sector.land_purchase_net.{slug}.fy", y) or 0
                if None in parts or cfc is None or nl is None:
                    bad.append(f"{slug}/{y}:欠"); continue
                sv, ctr, ctp, gfcf = parts
                if abs(sv + ctr - ctp - (gfcf - cfc + inv + land) - nl) > 0.6:
                    bad.append(f"{slug}/{y}")
        chk(not bad, f"F-2 資本勘定の恒等式（純貸出＝貯蓄純＋資本移転受−払−(総固定資本形成−固定資本減耗＋在庫変動＋土地購入純)）が 5 部門×3 年で付表 18 と一致{bad[:4]}")
        bad = [f"{slug}/{y}" for slug in ("nfc", "fin", "gg", "hh", "npish") for y in ("2023", "2010")
               if abs((_v(f"cao.sna_sector_bs.assets_total.{slug}.a", y) or 0) - (_v(f"cao.sna_sector_bs.nonfinancial_assets.{slug}.a", y) or 0) - (_v(f"cao.sna_sector_bs.financial_assets.{slug}.a", y) or 0)) > 0.6
               or abs((_v(f"cao.sna_sector_bs.assets_total.{slug}.a", y) or 0) - (_v(f"cao.sna_sector_bs.liabilities.{slug}.a", y) or 0) - (_v(f"cao.sna_sector_bs.net_worth.{slug}.a", y) or 0)) > 0.6]
        chk(not bad, f"F-3 BS の恒等式（非金融＋金融＝期末資産＝負債＋正味資産）が 5 部門×2 年で成立{bad[:4]}")
    # 第 8 弾 第 2 便（2026-08-24）：資金循環／固定資本ストック／雇用形態／社会保障給付費の恒等式
    if _vs.has_data("boj.fof.stock.assets_total.hh.fy"):
        bad = []
        for sec in ("nfc", "fin", "gg", "hh", "npish", "row"):
            for kind in ("stock", "flow"):
                for y in ("FY2024", "FY2010"):
                    tot = _v(f"boj.fof.{kind}.assets_total.{sec}.fy", y)
                    if tot is None:
                        continue
                    parts = [_v(f"boj.fof.{kind}.assets_{m}.{sec}.fy", y) for m in
                             ("currency_deposits", "loans", "debt_securities", "equity_investment_trusts", "insurance_pension",
                              "financial_derivatives", "deposits_with_banks", "trade_credit", "accounts_receivable_payable",
                              "portfolio_investment_abroad", "direct_investment_abroad", "other_external_claims",
                              "fiscal_loan_deposits", "foreign_reserves", "other")]
                    if abs(tot - sum(x for x in parts if x is not None)) > 1:
                        bad.append(f"{sec}/{kind}/{y}")
        chk(not bad, f"F-1 資金循環：assets_total ＝ 資産側の各項目の和（6 部門×ストック/フロー×2 年）{bad[:4]}")
        # 2026-08-24 利用側から「assets_total と liabilities_total が同値＝欠陥」と報告があったが、**原典の定義**（balancing を負債側に計上）。
        # 欠陥でないことと、balancing を除いた負債の和が取れることを、両方ゲートで固定して再報告を止める。
        LI = ("loans", "debt_securities", "equity_investment_trusts", "financial_derivatives", "insurance_pension", "deposits_with_banks",
              "trade_credit", "accounts_receivable_payable", "other_external_claims", "other", "currency_deposits", "fiscal_loan_deposits",
              "portfolio_investment_abroad", "direct_investment_abroad")  # `_memo`（再掲＝外貨準備）は足さない
        bad = []
        for sec in ("nfc", "fin", "gg", "hh", "npish", "row"):
            for kind, bal in (("flow", "net_lending"), ("stock", "net_financial_assets")):
                for y in ("FY2024", "FY2010", "FY1990"):
                    tot = _v(f"boj.fof.{kind}.liabilities_total.{sec}.fy", y)
                    if tot is None:
                        continue
                    if abs(tot - sum(_v(f"boj.fof.{kind}.liabilities_{m}.{sec}.fy", y) or 0 for m in LI) - (_v(f"boj.fof.{kind}.liabilities_{bal}.{sec}.fy", y) or 0)) > 1:
                        bad.append(f"{sec}/{kind}/{y}")
        chk(not bad, f"F-1 資金循環：liabilities_total ＝ Σ負債項目 ＋ balancing（資金過不足／金融資産負債差額）{bad[:4]}")
        bad = [f"{sec}/{kind}" for sec in ("nfc", "fin", "gg", "hh", "npish", "row") for kind in ("stock", "flow")
               if abs((_v(f"boj.fof.{kind}.assets_total.{sec}.fy", "FY2024") or 0) - (_v(f"boj.fof.{kind}.liabilities_total.{sec}.fy", "FY2024") or 0)) > 0.5]
        chk(not bad, f"F-1 資金循環：assets_total ＝ liabilities_total は**原典の定義**（balancing を負債側に計上）＝欠陥ではない{bad[:4]}")
        # 再掲項目は `_memo` 接尾辞で機械判別できる（利用側が「合計に足さない」を規則で扱えるように・2026-08-24）
        memo = {s2.measure for s2 in reg.series.values() if s2.dataset == "fof" and s2.measure.endswith("_memo")}
        chk(memo == {"stock.liabilities_foreign_reserves_memo", "flow.liabilities_foreign_reserves_memo"},
            f"F-1 資金循環：再掲項目は `_memo` 接尾辞のみ（外貨準備）＝合計から除く対象が機械で分かる {sorted(memo)}")
        chk(all(m.split(".", 1)[1][len("liabilities_"):].removesuffix("_memo") not in LI for m in memo),
            "F-1 資金循環：`_memo` の項目が合計の構成要素（LI）に含まれていない")
        nlf = [_v(f"cao.sna2020.net_lending_financial.{s2}.fy", "FY2024") for s2 in ("nfc", "fin", "gg", "hh", "npish", "row")]
        chk(all(x is not None for x in nlf) and abs(sum(nlf)) < 0.6, "付表 18 の２．（金融勘定側）＝6 部門の和が 0（資金循環の恒等式）")
    if _vs.has_data("cao.sna_fcs.total.total.a"):
        bad = [y for y in ("2024", "2010", "1994")
               if abs((_v("cao.sna_fcs.total.total.a", y) or 0) - sum(_v(f"cao.sna_fcs.total.{s2}.a", y) or 0 for s2 in ("nfc", "fin", "gg", "hh", "npish"))) > 0.6
               or abs((_v("cao.sna_fcs.total.nfc.a", y) or 0) - (_v("cao.sna_fcs.total.nfc_private.a", y) or 0) - (_v("cao.sna_fcs.total.nfc_public.a", y) or 0)) > 0.6]
        chk(not bad, f"F-4 固定資本ストック：一国計＝Σ制度部門・非金融法人＝民間＋公的{bad}")
        chk(abs((_v("cao.sna_fcs.total.total.a", "2024") or 0) - (_v("cao.sna2020.stock.fixed_assets.total.a", "2024") or 0)) < 0.6,
            "F-4 固定資本ストックの一国計＝ストック編 付表（cao.sna2020.stock.fixed_assets.total）＝別表と一致")
    if _vs.has_data("soumu.roudou_emp.regular.total.q"):
        bad = [q for q in ("2024Q4", "2015Q1", "2002Q1")
               if abs((_v("soumu.roudou_emp.employees_ex_officers.total.q", q) or 0) - (_v("soumu.roudou_emp.regular.total.q", q) or 0) - (_v("soumu.roudou_emp.nonregular.total.q", q) or 0)) > 1.5]
        chk(not bad, f"F-6 労調：役員を除く雇用者＝正規＋非正規（万人の丸めで ±1）{bad}")
    if _vs.has_data("ipss.shaho.benefits.total.fy"):
        bad = [y for y in ("FY2023", "FY2010", "FY1990")
               if abs((_v("ipss.shaho.benefits.total.fy", y) or 0) - sum(_v(f"ipss.shaho.benefits.{m}.fy", y) or 0 for m in ("medical", "pension", "welfare_other"))) > 1.5]
        chk(not bad, f"F-7 社会保障給付費：合計＝医療＋年金＋福祉その他（介護は再掲＝足さない）{bad}")
    chk("営業純益" in HOJIN_ANALYSIS_NOTES["value_added_identity"] and "相殺されて消える" in HOJIN_ANALYSIS_NOTES["value_added_identity"],
        "analysis_notes.value_added_identity＝営業純益＝営業利益−支払利息等（支払利息の二重計上を防ぐ・2026-08-24 利用側の実例）")
    from stats.core.dataset_notes import GAP_ANALYSIS_NOTES
    chk("employment_assumption" in GAP_ANALYSIS_NOTES and "vs_oecd" in GAP_ANALYSIS_NOTES,
        "analysis_notes（boj.gap）＝潜在成長率の就業者前提・OECD 成長会計と並べるときの注意")
    # scope（2026-08-23）：jp／intl の区別＝region_level で決まる・検索は jp が先・scope で絞れる
    chk(reg.get("oecd.stan.value_added.mfg.a.cty").scope == "intl" and reg.get("mof.hojin.sales.allexfin-allsize.fy").scope == "jp"
        and reg.get("soumu.chihozaisei.revenue_total.pref.fy").scope == "jp" if reg.get("soumu.chihozaisei.revenue_total.pref.fy") else True, "scope＝cty なら intl・それ以外（全国・都道府県）は jp")
    rows_i, _t = reg.search("付加価値", scope="intl"); rows_j, _t2 = reg.search("付加価値", scope="jp")
    chk(rows_i and all(x.scope == "intl" for x in rows_i) and rows_j and all(x.scope == "jp" for x in rows_j), "scope フィルタ（intl／jp）")
    # 段 D-(1)（2026-08-23）：STAN の業種分類対応＝identity と宣言した slug は日本の値が JSNA 付表 2 と一致する（値ストアがあるときだけ）
    from stats.core.dim_vocab import STAN_ACTIVITY, dim_label
    from stats.core.values import ValueStore
    vs = ValueStore()
    ident = [slug for slug, _c, _n, m in STAN_ACTIVITY if m == "identity" and vs.has_data(f"oecd.stan.value_added.{slug}.a.cty")]
    bad = []
    for slug in ident:
        a = vs.lookup(f"oecd.stan.value_added.{slug}.a.cty", "2024", "JPN"); b = vs.lookup(f"cao.sna_activity.gdp.{slug}.a", "2024", "JP")
        if a and b and abs(float(a.value) - float(b.value) * 1000) > 0.6:
            bad.append(slug)
    chk(not ident or not bad, f"段 D-(1) STAN identity 対応（{len(ident)} slug）＝日本の 2024 値が JSNA 付表 2 と一致{bad or ''}")
    chk(dim_label("industry", "mfg_electronic_optical", "stan") == "電子部品・コンピュータ・情報通信機器" and dim_label("industry", "mfg_food", "sna_activity") == "食料品",
        "dim_label は dataset ごとの語彙（stan／sna_activity）")
    # 段 3 品質メタ：断層表の機械適用（典拠＝原典注記・制度文書）
    def kinds(sid):
        return [(b["period"], b["kind"]) for b in reg.get(sid).breaks]
    chk(("FY2021", "accounting") in kinds("mof.hojin.sales.wholesale-allsize.fy") and ("FY2021", "accounting") in kinds("mof.hojin.cogs.mfg_food-allsize.fy")
        and ("FY2021", "accounting") in kinds("mof.hojin.sga.allexfin-cap1b.fy"), "FY2021 accounting（収益認識）が売上高・売上原価・販管費に付く")
    chk(not any(k == "accounting" for _, k in kinds("mof.hojin.employees_avg.allexfin-allsize.fy"))
        and not any(k == "accounting" for _, k in kinds("mof.hojin.operating_profit.allexfin-allsize.fy")),
        "従業員数・営業利益（水準は不変）には accounting 断層が付かない")
    chk(("FY2012", "population") in kinds("mof.hojin.tangible_fixed_assets.allexfin-allsize.fy"), "FY2012 population が有形固定資産に付く")
    chk(("FY2009", "classification") in kinds("mof.hojin.sales.mfg_food-allsize.fy") and ("FY2009", "classification") not in kinds("mof.hojin.sales.mfg_textile_h20-allsize.fy")
        and not any(k == "classification" for _, k in kinds("mof.hojin.sales.mfg_general_machinery-allsize.fy")),
        "FY2009 classification は FY2009 を跨ぐ業種細分のみ（H20 系列・FY2009 開始系列には付かない）")
    chk(reg.get("mof.hojin.sales.mfg_food-allsize.fy").usable_from == "FY2004" and reg.get("mof.hojin.sales.wholesale-allsize.fy").usable_from == "FY2009"
        and reg.get("mof.hojin.sales.allexfin-allsize.fy").usable_from == "FY1960", "usable_from＝製造業内訳 FY2004・非製造業内訳 FY2009・全産業 first_period")
    chk(("FY2004", "classification") in kinds("mof.hojin.employees_avg.mfg_electrical-allsize.fy") and ("FY2004", "classification") not in kinds("mof.hojin.sales.wholesale-allsize.fy"),
        "FY2004 classification は製造業内訳のみ")
    chk(("FY2007", "accounting") in kinds("mof.hojin.officer_bonus.allexfin-allsize.fy"), "役員賞与に FY2007 accounting（利益処分→費用項目・財務省 846 号 §8 注）")
    chk(("FY1975", "classification") in kinds("mof.hojin.sales.mfg_food-allsize.fy") and ("FY1975", "classification") not in kinds("mof.hojin.sales.mfg_wood-allsize.fy"),
        "FY1975 classification は FY1975 を跨ぐ業種細分のみ（財務省 846 号 §9⑹）")
    chk(("FY2007", "coverage") in kinds("mof.hojin.value_added.wholesale-cap1b.fy"), "付加価値に FY2007 定義変更（846 号 §8 算式）")
    chk(all("未確認" not in b.get("source", "") for s in reg.series.values() for b in s.breaks), "断層の典拠に『未確認』が残っていない")
    # 改善 3：quality.json（生成物）＝標本の薄さは上位集計に対する相対散らばりで判定（景気変動を標本誤差と取り違えない）
    from stats.ops.series_quality import QUALITY_PATH
    qj = json.loads(QUALITY_PATH.read_text(encoding="utf-8")) if QUALITY_PATH.exists() else {}
    chk(len(qj) >= 6000, f"quality.json 読込 {len(qj)} 系列")
    chk("volatile" in qj.get("mof.hojin.operating_profit.ict-cap10m-100m.fy", {}).get("flags", [])
        and "volatile" not in qj.get("mof.hojin.operating_profit.mfg-allsize.fy", {}).get("flags", []),
        "volatile＝情報通信×中小の営業利益には立ち・製造業全規模には立たない（相対散らばり）")
    chk("small_cell" in qj.get("mof.hojin.sales.fishery-cap1b.fy", {}).get("flags", []) and qj["mof.hojin.sales.fishery-cap1b.fy"]["population_latest"] < 100,
        "small_cell＝母集団法人数<100 社（漁業×10億以上）")
    b21 = next(b for b in reg.get("mof.hojin.sales.wholesale-allsize.fy").breaks if b["period"] == "FY2021")
    chk(b21.get("affects_from") == "FY2018" and b21.get("affects_to") == "FY2022", "収益認識の断層に影響幅 FY2018〜FY2022")
    chk(all(s.first_period <= b["period"] for s in reg.series.values() for b in s.breaks), "断層は系列の収録開始以降のみ")
    lost = []
    # 判定は**行ベースのみ**＝must_keep が limit 内の series 行に残る＋（打切り語）非 hojin 行数が基線以上。
    # total は判定に使わない（golden の count/truncated_at_baseline は記録用）。理由＝発見層の修正は
    # 「照合を弱める方向のみ」（S-1 AND・S-4 複合語分割・S-5 casefold）＝total の単調増加が正であり、
    # total を固定すると正しい改善を FAIL にしてしまう（2026-08-29 利用側の問いに対する明文化）。
    for g in gold:
        top = reg.search_collapsed(g["query"], limit=g["limit"])[0]  # 実際の応答形（畳んだ後の series 行）で検証
        got = {s.series_id for s in top}
        lost += [f'{g["query"]}:{sid}' for sid in g["must_keep"] if sid not in got]
        if g.get("min_non_hojin") is not None:  # 基線で打切り済みの語＝hojin 以外の件数が基線を下回らない
            n = sum(1 for s in top if s.dataset != "hojin")
            if n < g["min_non_hojin"]:
                lost.append(f'{g["query"]}:非hojin {n}<{g["min_non_hojin"]}')
    chk(gold and not lost, f"発見層ゴールデン {len(gold)} 語：must_keep が limit 内に残る・打切り語は非 hojin 件数が基線以上" + (f"（欠落 {lost[:5]}）" if lost else ""))
    # 発見層 品質 100 問（第 9 弾 発展・2026-08-29）＝「この語で引いたら到達できるか」。TDD の回路：
    # 新規は status=todo で追加（FAIL 可）→ 直したら pass に昇格 → **pass の脱落はここで FAIL**（不退転）。
    # 詳細レポートは python -m stats.eval.find_quality
    from stats.eval.find_quality import check as fq_check, load_entries as fq_load
    fq = fq_load()
    fq_results = {e["id"]: fq_check(reg, e)[0] for e in fq}
    fq_regressed = [e["id"] for e in fq if e.get("status") == "pass" and not fq_results[e["id"]]]
    n_fq_ok = sum(fq_results.values())
    chk(len(fq) >= 200 and not fq_regressed,
        f"発見層 品質 {len(fq)} 問：到達 {n_fq_ok}（{100.0 * n_fq_ok / len(fq):.1f}%）・pass 不退転"
        + (f"（脱落 {fq_regressed[:5]}）" if fq_regressed else f"・todo {len(fq) - n_fq_ok} は改善対象"))
    # dataset 引数の語彙（第 11 弾 第 5 便・2026-09-15）＝series_id の 2 番目の要素の集合。実利用ログの `mof.hojin`・`法人企業統計` は語彙外＝入口で hint
    _ds = reg.dataset_ids()
    chk({"hojin", "cpi2025", "maikin", "sna2020"} <= _ds and "mof.hojin" not in _ds and "法人企業統計" not in _ds,
        f"dataset 語彙 {len(_ds)} 件（hojin/cpi2025/maikin/sna2020 を含み・org 付きや和名は含まない）")
    # 捕捉ログ→品質問題の候補抽出（stats.ops.quality_candidates・2026-08-29）：純関数 candidates_from の規則
    from stats.ops.quality_candidates import candidates_from
    _evs = [
        {"tool": "find_statistics", "query": "茶柱指数", "total": 0, "result_count": 0, "ts": "t1"},
        {"tool": "lookup_statistic", "series_id": "soumu.cpi2020.cpi_all.m", "found": True, "ts": "t2"},
        {"tool": "find_statistics", "query": "存在しないテストクエリ", "total": 0, "result_count": 0, "ts": "t3"},
        {"tool": "find_statistics", "query": "売上高", "total": 300, "result_count": 20, "ts": "t4"},
        {"tool": "find_statistics", "query": "経常利益 製造業 非製造業", "total": 10, "result_count": 0, "family_count": 1, "collapsed": 10, "ts": "t5"},
    ]
    _cands = candidates_from(_evs)
    chk([c["query"] for c in _cands] == ["茶柱指数"]
        and _cands[0]["targets"] == ["soumu.cpi2020.cpi_all.m"] and _cands[0]["status"] == "todo"
        and _cands[0]["source"] == "usage-log",
        "quality_candidates＝0 件は草稿・直後 lookup を target 添付・テスト語は除外・既存の問（売上高）は重複排除")
    chk(all(c["query"] != "経常利益 製造業 非製造業" for c in _cands),
        "quality_candidates＝カードに畳まれた検索（result_count 0・collapsed 10・total 10）は打切りでも 0 件でもない＝草稿にしない")
    r2 = Registry(); r2.register(reg.get("mof.hojin.sales.allexfin-allsize.fy"))
    try:
        r2.register(reg.get("mof.hojin.sales.allexfin-allsize.fy")); chk(False, "重複登録の拒否")
    except ValueError:
        chk(True, "重複登録の拒否")
    from stats.ingest._base import SourceError, _col_index, _col_letter, assert_unique, is_numeric
    from stats.core.values import ValueRecord
    chk(all(is_numeric(x) for x in ("0", "12", "-3.5", "+7", "0.25", ".5", "1e3", "-2.5E-2", " 42 ")), "ingest 数値判定：数値（負・小数・指数）を通す")
    chk(not any(is_numeric(x) for x in ("-", "…", "...", "--", "x", "X", "", "1,234", "12-3", "3.", "1.2.3", "***", "－")),
        "ingest 数値判定：非数値記号（-・…・x・--）を値にしない（e-Stat の '$' も同じ判定）")
    def _vr(period, region, value):
        return ValueRecord(series_id="t.x.y.a", period=period, region=region, value=value, status="", vintage="", retrieved_at="", accessor={})
    assert_unique([_vr("2024", "JP", "1"), _vr("2024", "JP", "1"), _vr("2024", "13", "2")])
    try:
        assert_unique([_vr("2024", "JP", "1"), _vr("2024", "JP", "2")]); chk(False, "ingest 重複検査")
    except SourceError:
        chk(True, "ingest 重複検査：同一 (period, region) の別値はエラー・同値／別地域は許す")
    chk(_col_index("A") == 1 and _col_index("Z") == 26 and _col_index("AA") == 27 and _col_letter(0) == "A" and _col_letter(26) == "AA"
        and all(_col_index(_col_letter(i)) == i + 1 for i in range(0, 800)), "ingest 列記号⇄列番号の往復")
    # ---- ingest 純関数（ネットワーク不要・原典の読み方の要石） ----
    from stats.core.periods import from_wareki_date, from_wareki_fy
    chk(from_wareki_date("S49.9.24") == "1974-09-24" and from_wareki_date("H10.4.1") == "1998-04-01" and from_wareki_date("R8.7.31") == "2026-07-31"
        and from_wareki_date("令和8年7月31日") == "2026-07-31" and from_wareki_date("Ｒ元.5.1") == "2019-05-01", "和暦日付 'S49.9.24'→ISO（漢字/全角/元年も）")
    chk(from_wareki_date("R8.2.30") is None and from_wareki_date("2024-01-01") is None and from_wareki_date("") is None and from_wareki_date("X8.1.1") is None,
        "和暦日付：不正日・表記外・語彙外元号は None")
    chk(from_wareki_fy("令和8年度") == "FY2026" and from_wareki_fy("平成元年度") == "FY1989" and from_wareki_fy("令和 8 年度") == "FY2026"
        and from_wareki_fy("Ｓ60年度") == "FY1985", "和暦年度→FY（年度必須）")
    chk(from_wareki_fy("R6") is None and from_wareki_fy("令和8年") is None and from_wareki_fy("2026年度") is None, "和暦年度：「年度」無し・「年」・西暦は None")
    chk(from_wareki_fy("Ｓ60", require_suffix=False) == "FY1985" and from_wareki_fy("Ｈ元", require_suffix=False) == "FY1989"
        and from_wareki_fy("昭和42", require_suffix=False) == "FY1967" and from_wareki_fy("令和6年度", require_suffix=False) == "FY2024"
        and from_wareki_fy("令和8年", require_suffix=False) is None, "和暦年度（require_suffix=False）：シート名 'Ｓ60'・'Ｈ元' も受ける")
    from stats.ingest.boj_flat import period_of
    chk(period_of("yyyymm", "202407") == "2024-07" and period_of("yyyy", "2024") == "2024" and period_of("yyyyqq", "202403") == "2024Q3",
        "boj_flat 期間コード yyyymm/yyyy/yyyyqq")
    chk(period_of("yyyymm", "202413") is None and period_of("yyyyqq", "202405") is None and period_of("yyyy", "20240") is None,
        "boj_flat 期間コード：範囲外は None")
    from stats.ingest._base import _decimals, cell_text, _pick_row
    chk(_decimals("#,##0.0") == 1 and _decimals("#,##0") == 0 and _decimals("0.00_ ") == 2 and _decimals("General") is None and _decimals("") is None,
        "ingest 表示書式→小数桁（General/空は None）")
    chk(cell_text(534706.19999999995, "#,##0.0") == "534706.2" and cell_text(1.005, "0.00") == "1.01" and cell_text(2.5, "0") == "3"
        and cell_text(-0.04, "0.0") == "0" and cell_text("12", "0") is None and cell_text(1.5, "General") is None,
        "ingest セル値→公表どおりの表示文字列（四捨五入・-0 は 0・文字列/General は None）")
    labels = {1: "国内総生産", 2: "民間最終消費支出", 3: "国内総生産（支出側）", 4: "民間最終消費支出"}
    chk(_pick_row(labels, "国内 総生産") == 1 and _pick_row(labels, "民間最終消費支出", 2) == 4, "ingest 行ラベル：正規化完全一致を優先・occurrence 指定")
    try:
        _pick_row(labels, "民間最終消費支出"); chk(False, "ingest 行ラベル：複数行はエラー")
    except SourceError:
        chk(True, "ingest 行ラベル：複数行はエラー（黙って選ばない）")
    try:
        _pick_row(labels, "雇用者報酬"); chk(False, "ingest 行ラベル：0行はエラー")
    except SourceError:
        chk(True, "ingest 行ラベル：0行はエラー")
    from stats.ingest.mof_zaisei import sheet_to_fy, cell_str
    chk(sheet_to_fy("Ｓ60") == "FY1985" and sheet_to_fy("Ｈ元") == "FY1989" and sheet_to_fy("R6") == "FY2024" and sheet_to_fy("昭和42") == "FY1967"
        and sheet_to_fy("令和6") == "FY2024" and sheet_to_fy("明治元") == "FY1868", "mof_zaisei シート名→年度（漢字/半角/全角/元年）")
    chk(sheet_to_fy("参考") is None and sheet_to_fy("R") is None and sheet_to_fy("令和123") is None, "mof_zaisei シート名：表記外は None")
    chk(cell_str("1,234") == "1234" and cell_str(1234) == "1234" and cell_str(12.0) == "12" and cell_str(12.5) == "12.5"
        and cell_str("－") is None and cell_str(True) is None and cell_str(None) is None, "mof_zaisei セル→文字列（桁区切り除去・整数化・非数値は None）")
    from stats.ingest.soumu_hakusho import wareki_fy, _val
    chk(wareki_fy("令和8年度") == "FY2026" and wareki_fy("平成26年度") == "FY2014" and wareki_fy("令和元年度") == "FY2019" and wareki_fy("令和 8 年度") == "FY2026",
        "soumu_hakusho 和暦年度→FY")
    chk(wareki_fy("令和8年") is None and wareki_fy("2026年度") is None and wareki_fy("") is None, "soumu_hakusho 和暦年度：表記外は None")
    chk(_val("1,234") == "1234" and _val("△ 63") == "-63" and _val("−") is None and _val("") is None and _val("12.5") == "12.5", "soumu_hakusho 値（桁区切り・△→負号・'−' は値なし）")
    from stats.ingest.pdf_hakusho import HakushoParseError, depua, parse_entry_exit
    pua = lambda t: "".join(chr(0x3EDC + int(c)) if c.isdigit() else (chr(0x3EDA) if c == "." else c) for c in t)  # noqa: E731
    toks = ["x", "年度"] + [pua(y) for y in ("81", "82")] + ["開業率", pua("7.2"), pua("6.4"), "廃業率", pua("3.7"), pua("5.8")] \
           + [pua(y) for y in ("99", "00", "01")] + [pua(v) for v in ("4.4", "4.9", "4.4", "4.0", "4.0", "4.4")] + ["12 表", "表題"]
    chk(parse_entry_exit(toks) == [("FY1981", "7.2", "3.7"), ("FY1982", "6.4", "5.8"), ("FY1999", "4.4", "4.0"), ("FY2000", "4.9", "4.0"), ("FY2001", "4.4", "4.4")],
        "pdf_hakusho PUA 写像＋ブロック（年 k → 開業率 k → 廃業率 k・2 ブロック目以降はラベル無し・2 桁年は 50 境界で 19xx/20xx）")
    chk(depua(pua("12.5")) == "12.5", "pdf_hakusho PUA 写像（U+3EDC..＝0..9・U+3EDA＝.）")
    try:
        parse_entry_exit(["年度", pua("81"), pua("82"), pua("7.2")]); chk(False, "pdf_hakusho 率の個数不足はエラー")
    except HakushoParseError:
        chk(True, "pdf_hakusho 率の個数が年の 2 倍でなければエラー（黙って選ばない）")
    from stats.ingest.pdf_shunto import parse_large, parse_sme, ShuntoParseError
    t_large = ["x", "社", "円", "％", "社", "円", "％", "y", "社", "円", "％", "社", "円", "％", "総平均", "19,752", "5.37", "19,195", "5.39", "z"]
    chk(parse_large(t_large) == ["19,752", "5.37", "19,195", "5.39"], "pdf_shunto 大手：2回目の [社,円,％]×2 の直後の数値4つ")
    try:
        parse_large(["社", "円", "％", "社", "円", "％", "1", "2", "3", "4"]); chk(False, "pdf_shunto 大手：1回しか無ければエラー")
    except ShuntoParseError:
        chk(True, "pdf_shunto 大手：[社,円,％]×2 が1回しか無ければエラー")
    nums = [str(i) for i in range(1, 15)]
    t_sme = ["…", "そ", "の", "他", "非", "製", "造", "業"] + ["(1)", "(2)"] + nums + ["end"]
    chk(parse_sme(t_sme) == ["11", "12", "13", "14"], "pdf_shunto 中小：『その他非製造業』（縦書き分割）後の括弧なし数値 [10:14]")
    try:
        parse_sme(["その他非製造業"] + nums[:5]); chk(False, "pdf_shunto 中小：14 個揃わなければエラー")
    except ShuntoParseError:
        chk(True, "pdf_shunto 中小：数値が 14 個揃わなければエラー")
    # 第 9 弾 段 3：QE 公表回の発見（年次索引の決定論パース）と評価照合の独立 CSV パーサ
    from stats.ops.qe_update import csv_value, edition_of_dir, label_of_dir, parse_toukei
    chk(edition_of_dir("qe262") == "2621" and edition_of_dir("qe262_2") == "2622" and edition_of_dir("qe261_2") == "2612",
        "qe_update 公表回コード（1次=qe{YY}{Q}・2次=qe{YY}{Q}_2）")
    chk(label_of_dir("qe262") == "2026年4-6月期 1次速報" and label_of_dir("qe264") == "2026年10-12月期 1次速報",
        "qe_update 公表回の名称（年はデータの年＝20YY）")
    try:
        edition_of_dir("qe265"); chk(False, "qe_update 表記外ディレクトリはエラー")
    except Exception:
        chk(True, "qe_update 表記外ディレクトリ（四半期 5）はエラー＝黙って選ばない")
    toukei = ('<a href="/jp/sna/data/data_list/sokuhou/files/2026/qe262/gdemenuja.html">x</a>'
              '<a href="/jp/sna/data/data_list/sokuhou/files/2026/qe261_2/gdemenuja.html">y</a>'
              '<a href="/jp/sna/data/data_list/sokuhou/files/2026/qe262/gdemenuja.html">重複</a>')
    rel = parse_toukei(toukei)
    chk([r["edition"] for r in rel] == ["2612", "2621"] and rel[-1]["dir"] == "qe262" and rel[-1]["year"] == "2026",
        "qe_update parse_toukei＝重複除去・edition 昇順・最新が末尾")
    qe_csv = ("t\ne\n"
              ',国内総生産(支出側),財貨・サービス,,\n,,純輸出,輸出,輸入\n_\n_\n_\n'
              '1994/ 1- 3.,"521,943.6 ",1.0,2.0,3.0\n 4- 6.,"523,000.0 ",,4.0,\n1995/ 1- 3.,9.9,,,\n')
    chk(csv_value(qe_csv, "1994Q1", "国内総生産(支出側)") == "521943.6"
        and csv_value(qe_csv, "1994Q2", "国内総生産(支出側)") == "523000.0"
        and csv_value(qe_csv, "1994Q2", "財貨・サービス", "輸出") == "4.0"
        and csv_value(qe_csv, "1994Q2", "財貨・サービス", "輸入") is None
        and csv_value(qe_csv, "1995Q1", "国内総生産(支出側)") == "9.9",
        "qe_update csv_value＝独立パーサ（引用符・桁区切り・年キャリー・空欄は None）")
    print("総合: PASS ✅" if ok else "総合: FAIL ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
