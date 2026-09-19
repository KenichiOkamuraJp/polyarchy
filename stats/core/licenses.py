"""
再配布・商用利用条件の表（docs/再配布条件.md・原文確認 2026-08-18）。core の関心事＝値を返すときに license として付く。

- grade：◎＝出典明示で商用可／○＝可だが条件あり／△＝商用は要相談（Series.license の契約は core.registry）。
- 系列への割当は **取得経路** で決める（org より accessor.type が優先：総務省・財務省でも e-Stat 経由なら e-Stat 規約）。
"""
from __future__ import annotations

from stats.core.registry import Series

PDL = {"grade": "◎", "terms": "公共データ利用規約（第1.0版・PDL1.0）", "url": "https://www.digital.go.jp/resources/open_data/public_data_license_v1.0",
       "note": "商用可・CC BY 4.0 互換。出典明示。数値データは著作権の対象外。編集・加工した場合はその旨を記載"}
LICENSES = {
    "estat": {"grade": "◎", "terms": "e-Stat 利用規約＋API 利用規約", "url": "https://www.e-stat.go.jp/terms-of-use",
              "note": "商用可・CC BY 4.0 互換。出典「政府統計の総合窓口(e-Stat)」。API 利用サービス公開時はクレジット文（サービスの内容は国によって保証されたものではありません）を表示。appId は譲渡禁止"},
    "cao": {**PDL, "url": "https://www.cao.go.jp/notice/rule.html"},
    "mof": {**PDL, "url": "https://www.mof.go.jp/about_mof/notice/index.html"},
    "soumu": {**PDL, "url": "https://www.soumu.go.jp/menu_kyotsuu/policy/tyosaku.html"},
    "boj": {"grade": "△", "terms": "日本銀行ウェブサイト 著作権・免責事項", "url": "https://www.boj.or.jp/about/copyright.htm",
            "note": "転載・複製は出所明記で可。商用目的の転載・複製は事前に情報サービス局へ相談。無断改変不可"},
    "ipss": {"grade": "◎", "terms": "社人研 サイトのご利用にあたって（著作権・リンク許可）＝公共データ利用規約（第1.0版・PDL1.0）準拠", "url": "https://www.ipss.go.jp/site-ad/",
             "note": "「権利表記の記載がない限り『公共データ利用規約（第1.0版）』（PDL1.0）に準拠した利用条件の下で、利用することができます」＝CC BY 4.0 互換・商用可。出典例「出典：『日本の将来推計人口（令和5年推計）』（国立社会保障・人口問題研究所）（URL）」。編集・加工した場合はその旨を別記"},
    "imf": {"grade": "○", "terms": "IMF Copyright and Usage — The Use of IMF Data（Effective 2024-10-11）", "url": "https://www.imf.org/en/about/copyright-and-terms#data",
            "note": "統計データ（WEO 含む）は特則：download, extract, copy, create derivative works, publish, distribute, use 可。条件＝出典表記「Source: International Monetary Fund, Database Name, <link>」・改変禁止（加工時は明示）・下流利用者への条件周知・単体販売時は無償入手可能である旨を購入者に通知。商用再利用は copyright@imf.org へ許諾申請。自動化ツールによる一括ダウンロード・LLM 学習利用は明示許可が必要"},
    "oecd": {"grade": "◎", "terms": "OECD Terms and Conditions §3 Data — Permitted Use（Last updated 2024-07-01）", "url": "https://www.oecd.org/en/about/terms-conditions.html",
             "note": "「you can extract from, download, copy, adapt, print, distribute, share and embed Data for any purpose, even for commercial use」。条件＝出典表記（データ付属の引用、無ければ「OECD (year), (dataset name), (data source) DOI or URL (accessed on (date))」）・派生物にも同じ表記義務を継承。第三者所有データは別＝系列ごとに source タブで確認。API はレート制限・停止の裁量あり"},
    "wb": {"grade": "◎", "terms": "World Bank Data Catalog Public Licenses（CC BY 4.0）", "url": "https://datacatalog.worldbank.org/public-licenses",
           "note": "データセットは CC BY 4.0＝商用・再配布可・出典明示。worldbank.org 本体の非商用規約とは別"},
    "bls": {"grade": "◎", "terms": "BLS Website Copyright Information（米連邦政府の著作物＝パブリックドメイン）",
            "url": "https://www.bls.gov/opub/copyright-information.htm",
            "note": "パブリックドメイン＝商用・再配布可。出典は BLS。API 利用規約（https://www.bls.gov/developers/termsOfService.htm）＝取得日を明示し、次の文を明示する：BLS.gov cannot vouch for the data or analyses derived from these data after the data have been retrieved from BLS.gov. 改変した内容を BLS 出典として示さない・BLS ロゴは使用不可（API v1 はキー不要・1 日 25 リクエスト）"},
    "eurostat": {"grade": "◎", "terms": "Eurostat Copyright notice and free re-use of data",
                 "url": "https://ec.europa.eu/eurostat/help/copyright-notice",
                 "note": "統計データは出典明示で商用・非商用とも再利用可（許諾手続き不要）。★商用可は EU 加盟国・EFTA 加盟国・EU 加盟候補国のデータに限る（米国・日本・中国など域外国のデータと、Eurostat 以外が出所と明示されたデータは商用不可）＝本 DB の収録は EU 加盟国のみ。加工した場合はその旨と Eurostat が責任を負わない旨を明示。引用＝Source: データセットの DOI（加工版は datacode のリンク）とアクセス日"},
    "meti": {**PDL, "url": "https://www.meti.go.jp/main/rules.html",
             "note": "経済産業省 ウェブサイト利用規約＝PDL1.0 準拠・商用可・出典明示。中小企業庁（chusho.meti.go.jp）は同ドメイン配下＝同規約の適用と判断（2026-08-22・庁の規約ページは bot 拒否で原文未確認＝要確認）。白書の表の数値は著作権の対象外・原資料は厚生労働省「雇用保険事業年報」"},
    "keidanren": {"grade": "△", "terms": "経団連 著作権について", "url": "https://www.keidanren.or.jp/about.html",
                  "note": "著作権法の範囲内で引用・転載。商用目的は事前相談。→ stats は値を保持しない（status=guide＝発見層で原典の読み方だけ案内・値は利用側が PDF を読む）"},
}


def license_for(s: Series) -> dict:
    """系列の license（取得経路で決める：e-Stat 経由＝e-Stat 規約／経団連 PDF＝経団連／それ以外＝調査主体）。未知の org は {}。"""
    t = s.accessor.get("type", "")
    if t in ("estat", "estat_file", "maikin_csv", "estat_catalog_xlsx"):  # ファイル提供（file-download・カタログ経由）も e-Stat 規約
        return LICENSES["estat"]
    if t == "pdf_table":
        return LICENSES["keidanren"]
    return LICENSES.get(s.org, {})
