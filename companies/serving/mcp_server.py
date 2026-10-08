"""Polyarchy 企業情報DB（companies）の MCP サーバ。読み取り専用・公開データのみ・層は公開固定（layer 引数なし）。

  python -m companies.serving.mcp_server                       # stdio
  python -m companies.serving.mcp_server --http --port 8767    # Streamable HTTP（配信形）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from polyarchy_common.logsetup import configure_quiet_logging, get_logger, guard_stdout_for_stdio

_REAL_STDOUT = guard_stdout_for_stdio() if "--http" not in sys.argv else None  # stdio では stdout がプロトコル線

import anyio  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from companies.core import lookup, regions, screen, segments, store, trend  # noqa: E402
from companies.core.items import ITEMS  # noqa: E402
from polyarchy_common.capture import append_record  # noqa: E402

configure_quiet_logging()
log = get_logger("polyarchy.companies")
QUERY_LOG = Path(os.getenv("COMPANIES_QUERY_LOG") or Path(__file__).resolve().parent.parent / "data" / "query_log" / "queries.jsonl")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

SERVER_INSTRUCTIONS = (
    "Polyarchy 企業情報DB(companies)。日本の有価証券報告書(EDINET・金融庁)の「主要な経営指標等の推移」と従業員の状況を、"
    "公表どおりの値で厳密参照する読み取り専用サービス(公開データのみ)。2層構造:発見層(find_company=企業の同定/list_items=項目の語彙)と"
    "参照層(lookup_company_facts=値の完全一致参照/lookup_segments=セグメント別の値を表ごと/lookup_regions=地域別〔国・地域ごと〕の売上高・有形固定資産・非流動資産の表を公表どおりに)。値は XBRL に書かれた文字列のまま返す(換算・丸め・補完なし。比率は 0.444 の形・金額は円)。"
    "連結と単体(提出会社)は別の系列で、どちらの値かを必ず返す。該当が無ければ found=false と理由を返し、別の連結/単体の別・隣の項目・近い決算期の値では埋めない"
    "(例=IFRS の会社は「売上高」ではなく「売上収益」(item=revenue)、持株会社・金融・建設などは「営業収益」「経常収益」「完成工事高」や"
    "会社が独自に定義した項目で開示している=net_sales が found=false のとき suggest に、その会社が最上段の収益として開示している項目と"
    "引き直し方が返る。alternatives にはその決算期に開示している全項目の一覧が返る)。"
    "セグメント別(lookup_segments)は会社が定義した区分をその会社のラベルのまま返し、業種横断の区分や利益の物差しに寄せない"
    "(セグメント利益が営業利益か経常利益か事業利益かは会社の定義=要素とラベルのまま)。"
    "地域別(lookup_regions)は表をセル単位で写して返し、国内/海外への寄せ・比率の計算はしない(表の読み方は返り値の read_note)。"
    "各値には出典(書類管理番号・提出日・要素・context・URL・引用1行)が付く。単社の参照層は派生値(利益率・前年比 等)を計算しない=構成する項目を引いて利用側で計算する。"
    "横断検索(list_metrics=使える項目・検証済みの型・未収録の項目/screen_companies=条件で会社を絞り並べる)に限り、定義した式で派生値をサーバが計算し、"
    "行ごとに式と入力の開示値(出典つき)を添えて派生値と明記する。比較できない会社は除外の理由と件数で返し、未収録の入力は未収録と返す(近い項目で埋めない)。"
    "時系列(screen_trend=複数年の条件で会社を絞り並べる〔連続増収・年平均成長率・ずっと一定以上 等〕)は、各年の値をその年を載せた最新の書類から取り、"
    "年ごとの系列(値・出所の書類)と集約(派生値)を返す。後年の書類で組み替えられた年は restated に他の書類の値が並ぶ(除外しない)。"
    "1 社の年ごとの並びは lookup_segments/lookup_regions に period_from/period_to(区分の組み替えは regrouped で事実として示し、旧区分と新区分を対応づけない)。"
    "値そのものは各提出会社の開示に帰属し、本サービスは値を保証しない(原典で確認すること)。"
)
mcp = FastMCP("polyarchy-companies", instructions=SERVER_INSTRUCTIONS)


def _compact_json(r: dict) -> str:
    """字下げなしの JSON 文字列で返す＝FastMCP は辞書を字下げ 2 の JSON にする（返り値は利用側のモデルの文脈に入る＝
    時系列の横断で同じ中身が 25KB→40KB に膨らんだ・2026-10-01）。中身は辞書を返すときと同じ。"""
    return json.dumps(r, ensure_ascii=False, separators=(",", ":"))


def _capture(tool: str, args: dict, r: dict, **extra) -> None:
    append_record(QUERY_LOG, {"tool": tool, "args": args, "found": r.get("found"), "reason": r.get("reason"), **extra})


def _vocab_only(unavailable: list[dict]) -> list[dict]:
    """30 日を超えて残す集計（週次の利用集計）に回すのは、サーバ側の固定の語（項目・未収録の目録のキー）だけ＝利用者が書いた語は載せない
    （docs/第1c便_計画.md §2-6）。polyarchy_common.usage_report はこの欄を件数として数える（サービスのソースは読まない）。"""
    fixed = set(screen.ITEMS) | set(screen.NOT_INGESTED) | set(screen.PSEUDO) | set(screen.DERIVED)
    return [{"term": u["term"], "level": u["level"]} for u in unavailable
            if u.get("level") in ("input_not_ingested", "input_not_disclosed") and u.get("term") in fixed]


@mcp.tool(title="企業を同定する", annotations=READ_ONLY)
def find_company(query: str) -> dict:
    """社名(一部でも可)・証券コード(4桁)・EDINET コード(E+5桁)から、有価証券報告書の提出会社を同定する。

    1 社に定まれば company(EDINET コード・証券コード・社名・会計基準・連結の有無・決算期末)を返す。
    候補が複数なら found=false・reason=ambiguous_company と candidates を返す(推測で 1 社に決めない=候補から EDINET コードで引き直す)。
    該当なしは reason=unknown_company。収録は有価証券報告書の提出会社のみ(非上場でも提出会社なら収録・提出していない会社は無い)。
    """
    r = lookup.find_company(query)
    _capture("find_company", {"query": query}, r)
    return r


@mcp.tool(title="企業の開示値を参照する", annotations=READ_ONLY)
def lookup_company_facts(company: str, period: str, item: str | None = None, element: str | None = None,
                         basis: str | None = None, doc_id: str | None = None,
                         accounting_standard: str | None = None) -> dict:
    """企業×項目×決算期の値を、有価証券報告書に書かれたとおりに返す(完全一致参照)。

    company=EDINET コード・証券コード・社名。period=決算期末の YYYY-MM(例 2025-03。年度表記は不可)。
    item=項目のキー(list_items の語彙。例 net_sales, total_assets, average_annual_salary)。
    element=要素 ID(会社が独自に定義した項目や、同じ決算期に会計基準の違う値が並ぶときに指定。alternatives/competing に出る)。item と element はどちらか一方。
    basis=consolidated(連結)/non_consolidated(単体=提出会社)。省くと、連結を作成している会社は連結・していない会社は単体。
    accounting_standard=Japan GAAP/IFRS/US GAAP。IFRS の会社には日本基準の表を併記する会社や、移行年に 2 つの基準の値が並ぶ会社がある=
    指定が無ければ書類が宣言する会計基準の値を返し、もう一方の基準の値を other_standards に必ず添える(指定すればその基準の値)。
    平均年齢・平均勤続年数は「年」と「月」に分けて開示する会社がある=companion に対の値が付く(40 年と 5 月=40 歳 5 か月)。
    doc_id=書類管理番号。省くと提出日が最新の書類の値(同じ決算期の値は後年の書類に再掲され、遡及修正で変わり得る=
    source.other_documents に他の書類の値が並ぶ。特定の書類の値が要るときに指定)。
    平均年間給与・平均年齢・平均勤続年数・資本金・配当などは単体にだけある=basis=non_consolidated を指定する。
    返り値=value(文字列のまま)・unit・decimals・basis・element・label・period_end・source(書類・提出日・引用)・license。
    無ければ found=false と reason(item_not_disclosed / no_consolidated_statements / out_of_range / bad_period / unknown_item /
    unknown_company / ambiguous_company / ambiguous_item)。item_not_disclosed では alternatives(その会社がその決算期に開示している項目の一覧・値なし)が返る。
    売上高(net_sales)などの最上段の収益が無いときは suggest に、代わりに開示している項目(IFRS の売上収益=revenue・営業収益・経常収益・
    各社が定義した項目)と引き直し方が返る=それは「売上高」とは別の概念なので、返ってきたラベルのまま扱う。
    """
    r = lookup.lookup_company_facts(company, item=item, element=element, period=period, basis=basis, doc_id=doc_id,
                                    accounting_standard=accounting_standard)
    _capture("lookup_company_facts", {"company": company, "period": period, "item": item, "element": element, "basis": basis, "doc_id": doc_id,
                                      "accounting_standard": accounting_standard}, r)
    return r


@mcp.tool(title="セグメント別の値を参照する", annotations=READ_ONLY)
def lookup_segments(company: str, period: str | None = None, basis: str | None = None, doc_id: str | None = None,
                    period_from: str | None = None, period_to: str | None = None, elements: list[str] | None = None) -> str:
    """企業×決算期のセグメント別の値を、有価証券報告書に書かれたとおりに表ごと返す(完全一致参照)。期間を指定すれば年ごとに並べる。

    company=EDINET コード・証券コード・社名。period=決算期末の YYYY-MM(各書類に当期・前期の 2 期だけ載る)。
    period_from/period_to=期間(period の代わり・doc_id とは併用しない)＝各年はその期を載せた最新の書類の値(1 年ずつ引いたときと同じ)。
    期間のとき返り値=years(年ごとの出所の書類・区分の一覧)・series(区分×要素の年ごとの値・restated=同じ期の値が違う他の書類)・
    regrouped(会社が定義した報告セグメントの組が前の年と違う年=増えた区分・無くなった区分。旧区分と新区分は対応づけない=組み替えの前後で
    区分の値をつないで伸び率を出さない)。elements=期間のとき系列を要素 ID で絞る(例 ["jpcrp_cor:RevenuesFromExternalCustomers"]・
    絞らないと 10 年で約 100KB)。
    basis=consolidated/non_consolidated(省くと、連結を作成している会社は連結)。doc_id=書類管理番号(省くと提出日が最新の書類)。
    返り値=segments(区分の一覧=member〔要素 ID〕・label〔会社のラベル。標準の区分で会社のラベルが無ければタクソノミの標準ラベル〕・kind)・facts(区分×要素の値=member・element・label・
    section〔segment_information=セグメント情報の注記/employees=従業員の状況/capex=設備投資/research_and_development=研究開発〕・
    value〔文字列のまま〕・unit・decimals)・source。
    区分は会社の定義のまま(業種横断の区分に寄せない)。kind=company_defined/reportable_total/other_reportable/other/reconciling/
    corporate/unallocated_and_elimination/total/other_standard(意味は返り値の kind_note)。company_defined は事業セグメントとは限らない
    (会社独自の調整額・消去・全社・小計もこの kind)=何の区分かは label で読む。other_reportable(その他)は、報告セグメントの計の外の
    「その他」に使う会社と、計に含まれる報告セグメントの一つに使う会社がある=reportable_total に足してよいかは同じ表の total と
    突き合わせて決める(total が無ければ書類の本文の表で見る)。区分の足し算の関係は返さない=合計を作るときは
    kind と label を見て調整額・全社・合計・小計を二重に数えない。合計を問われたら、会社が表に書いた計・合計を答えにする
    (reportable_total・total があればその値=会社の開示値。区分から足した値は派生値で丸めにより合わないことがある=示すなら派生値と明記して並べる)。
    会社が表に書いた計・合計・連結の値は区分として返らないことがある
    (区分の軸を持たない値として書かれる)=区分から足し直さず、主要な経営指標等の推移にある項目なら lookup_company_facts で、
    無い項目(日本基準・IFRS の営業利益 等)は書類の本文の表で見る。
    利益の物差し(営業利益・経常利益・事業利益・セグメント利益 等)は会社ごとに違う=element と label のまま扱う。
    無ければ found=false と reason(no_segment_figures=セグメント情報の注記に数値が無い〔単一セグメント・記載の省略 等〕→ quote に会社の文 1 行 /
    not_tagged=米国基準の会社で注記が XBRL に無い〔本文の表だけ〕/ no_consolidated_statements / out_of_range / bad_period /
    unknown_company / ambiguous_company)。数値が無いときもタグのある欄(従業員の状況 等)は other_sections に返る(区分の label・kind は segments)。
    """
    r = segments.lookup_segments(company, period, basis=basis, doc_id=doc_id, period_from=period_from, period_to=period_to, elements=elements)
    _capture("lookup_segments", {"company": company, "period": period, "basis": basis, "doc_id": doc_id, "period_from": period_from,
                                 "period_to": period_to, "elements": elements}, r)
    return _compact_json(r)


@mcp.tool(title="地域別の表を参照する", annotations=READ_ONLY)
def lookup_regions(company: str, period: str | None = None, basis: str | None = None, doc_id: str | None = None,
                   period_from: str | None = None, period_to: str | None = None) -> str:
    """企業×決算期の地域別(国・地域ごと)の売上高・有形固定資産(IFRS は売上収益・非流動資産)の欄を、有価証券報告書に書かれたとおりに返す。

    company=EDINET コード・証券コード・社名。period=決算期末の YYYY-MM(各書類に当期・前期の 2 期)。
    period_from/period_to=期間(period の代わり・doc_id とは併用しない)＝years に年ごとの欄(その期の欄が載る最新の書類＝翌年の書類の前期の欄・
    IFRS は翌年の書類の表の前期の列)。見出しの地域は年で変わり得る=寄せない。
    basis=consolidated/non_consolidated(省くと、連結を作成している会社は連結)。doc_id=書類管理番号(省くと提出日が最新の書類)。
    地域別の値は XBRL の数値のタグが無く表だけ=表をセル単位で公表どおりに写して返す(数値に換算しない)。
    返り値=sections(欄ごと=section〔revenue=売上高/property_plant_and_equipment=有形固定資産/geographic_areas_ifrs=IFRS の地域別情報〕・
    context・period・period_in_columns〔IFRS は 1 つの欄の表に前期と当期の列が並ぶ〕・content〔欄の中身を原典の順に=text 段落/table 表/omitted 省いた長い文の字数〕)・read_note・source。
    表は rows(行の並び・セルは HTML の並び)で、結合セルは展開しない=rowspan/colspan のあるセルは複数の行・列を占める 1 つのセル(値は 1 回だけ数える)。
    「うち」・括弧の値は内数=合計に足さない。単位は表の前の段落・表の上段・見出しの括弧・値の末尾のどこかにある。
    何の値の表か(売上/非流動資産)は表の前の段落で読む(section は要素名で決まり中身とずれることがある)。表の外の文に国ごとの値があることがある(text)。
    国内/海外への寄せ・地域の読み替え・比率の計算はしない=海外比率などを示すなら使ったセル(本邦と合計)を添えて派生値と明記する。
    無ければ found=false と reason(omitted=欄はあるが表が無い〔本邦が 90% 超・本邦以外に無い 等〕→ quotes に会社の文 /
    not_tagged=地域の欄が無い〔米国基準・地域の要素でタグ付けしていない書類〕/ no_consolidated_statements / out_of_range / bad_period /
    unknown_company / ambiguous_company)。
    """
    r = regions.lookup_regions(company, period, basis=basis, doc_id=doc_id, period_from=period_from, period_to=period_to)
    _capture("lookup_regions", {"company": company, "period": period, "basis": basis, "doc_id": doc_id, "period_from": period_from,
                                "period_to": period_to}, r)
    return _compact_json(r)


@mcp.tool(title="横断検索の項目と型を見る", annotations=READ_ONLY)
def list_metrics() -> dict:
    """screen_companies・screen_trend の条件・並べ方に使える語の一覧＝集約の語彙(aggregates・screen_trend)・＝項目のキー(開示値)・仮の項目(top_line=最上段の収益/bottom_line=当期純利益)・
    派生項目・検証済みの型(名前つきの指標＝式と注)・未収録の項目(まだ使えない＝使うと input_not_ingested)・業種(EDINET の提出者業種)・
    除外の理由の一覧・式の書き方。screen_companies を使う前に引く。
    """
    return screen.list_metrics()


@mcp.tool(title="条件で会社を絞り込み並べる", annotations=READ_ONLY)
def screen_companies(conditions: list[dict], order_by: str | None = None, order: str = "desc", industries: list[str] | None = None,
                     manufacturing: bool | None = None, basis: str | None = None, period_from: str | None = None,
                     period_to: str | None = None, limit: int = 20) -> dict:
    """条件で会社を絞り込み、1 つの値で並べて返す(横断検索)。使える語・検証済みの型・未収録の項目は list_metrics で引く。

    conditions=1〜5 個の {"metric": 検証済みの型か項目のキー} または {"expr": 式} に、任意で "min"/"max"(比率は 0.1 の形=10%)。条件は AND。
    式=項目のキー・数・+ - * /・括弧・期のずれ x[t]〜x[t-4](同じ書類の 5 期推移)だけ(関数・文字列・比較は使えない)。
    order_by=並べる条件(metric か expr の文字列・省くと最初の条件)・order=desc/asc・limit=1〜100(既定 20)。
    industries=EDINET の提出者業種(東証 33 業種の表記)の配列・manufacturing=true/false(製造業 16 業種)。basis=consolidated/non_consolidated
    (省くと会社ごとに連結を優先)。period_from/period_to=決算期末 YYYY-MM の範囲(省くと各社の最新の決算期・基準日から 18 か月より前は除外)。
    返り値=rows(会社・業種・決算期・書類・values〔条件ごとの値＝派生値か開示値〕・inputs〔入力の開示値と出典。地域別の比率は本邦と合計のセルと分類の基準の文〕)・
    matched(条件を満たす全件数)・excluded(条件ごと・理由ごとの件数と社名の例＝比較できない会社を黙って落とさない)・
    judged_by_statement(表が無く会社の文で判定した件数)・unavailable(使えなかった入力＝未収録・開示なし)・definitions(使った式)。
    決算期は会社ごとに違う(行の period を見る)。業種は登録の業種で事業の実態とずれる会社がある。値の比較・解釈・文章化は利用側で行う。
    無ければ found=false と reason(unknown_item=語彙に無い語〔candidates〕/input_not_ingested=未収録の項目/bad_expression=式として受け付けない/
    bad_request/bad_period)。
    """
    args = {"conditions": conditions, "order_by": order_by, "order": order, "industries": industries, "manufacturing": manufacturing,
            "basis": basis, "period_from": period_from, "period_to": period_to, "limit": limit}
    r = screen.screen_companies(conditions, order_by=order_by, order=order, industries=industries, manufacturing=manufacturing,
                                basis=basis, period_from=period_from, period_to=period_to, limit=limit)
    un = r.get("unavailable") or []
    _capture("screen_companies", args, r, matched=r.get("matched"), unavailable=un, unavailable_vocab=_vocab_only(un))
    return r


@mcp.tool(title="複数年の条件で会社を絞り込み並べる", annotations=READ_ONLY)
def screen_trend(conditions: list[dict], order_by: str | None = None, order: str = "desc", industries: list[str] | None = None,
                 manufacturing: bool | None = None, basis: str | None = None, period_from: str | None = None,
                 period_to: str | None = None, limit: int = 20, companies: list[str] | None = None, detail: bool = False) -> str:
    """複数年の条件で会社を絞り込み、1 つの集約値で並べて返す(時系列の横断検索)。集約の語彙・使える項目と型は list_metrics で引く。

    conditions=1〜5 個の {"metric": 検証済みの型か項目のキー} または {"expr": 式} に "aggregate"(集約=list_metrics の aggregates のどれか)と、
    任意で "min"/"max"(集約値の条件)・"year_min"/"year_max"(各年の値の条件=年数を数える集約で使う)。条件は AND。比率は 0.1 の形=10%。
    条件の名前="<aggregate>:<metric または expr>"(order_by に使う・省くと最初の条件)。order=desc/asc・limit=1〜100(既定 20)。
    period_from/period_to=決算期末 YYYY-MM の範囲(省くと全社で同じ 13 年=基準日の 4 か月前まで・海外売上比率は 11 年)。
    指定した期間の端まで年がそろわない会社は insufficient_history で除外(年数の違う集約を並べない)。
    各年の値=その年の入力がすべて載る書類のうち提出日が最新の書類の値(単社の参照の既定と同じ)。後年の書類で組み替えられた年は除外せず、
    行の series の restated に他の書類の値・restated_years に年が並ぶ=段差は利用側が系列で確かめる。
    系列が途切れる会社(会計基準・決算期・連結の有無・最上段の収益の項目の変更・株式分割をまたぐ 1 株当たりの項目 等)は除外の理由と件数で返す。
    返り値=rows(会社・values〔集約＝派生値〕・series〔年ごとの値・出所の書類・入力の開示値〕・elements〔入力の要素〕・restated_years)・matched・excluded・definitions・note。
    companies=EDINET コードの配列(1〜10 社＝その会社だけ)・detail=true で各年の入力の出典一式(要素・context・単位)と restated の詳細(既定は細い形)。
    無ければ found=false と reason(unknown_aggregate/unknown_item/input_not_ingested/bad_expression/bad_request/bad_period)。
    1 年の条件(最新の決算期で絞る)は screen_companies。
    """
    args = {"conditions": conditions, "order_by": order_by, "order": order, "industries": industries, "manufacturing": manufacturing,
            "basis": basis, "period_from": period_from, "period_to": period_to, "limit": limit, "companies": companies, "detail": detail}
    r = trend.screen_trend(conditions, order_by=order_by, order=order, industries=industries, manufacturing=manufacturing,
                           basis=basis, period_from=period_from, period_to=period_to, limit=limit, companies=companies, detail=detail)
    un = r.get("unavailable") or []
    # 語彙に無い集約は回数だけを恒久集計へ＝固定の語 aggregate で（利用者が書いた語〔median 等〕は 30 日の捕捉ログの args にだけ残る）
    agg = [{"term": "aggregate", "level": "unknown_aggregate"}] if r.get("reason") == "unknown_aggregate" else []
    _capture("screen_trend", args, r, matched=r.get("matched"), unavailable=un, unavailable_vocab=_vocab_only(un) + agg)
    return _compact_json(r)


@mcp.tool(title="項目の語彙を見る", annotations=READ_ONLY)
def list_items() -> dict:
    """lookup_company_facts の item に使えるキーの一覧(キー・日本語の呼び名・対応する標準タクソノミの要素)。

    1 つのキーに束ねているのは同じ概念の会計基準違い(日本基準/IFRS/米国基準)だけ。売上高/売上収益/営業収益/経常収益、経常利益/税引前利益は別のキー。
    """
    return {"items": [{"item": k, "label": label, "elements": [f"jpcrp_cor:{e}" for e in els]} for k, (label, els) in ITEMS.items()],
            "n_companies": len(store.registry()), "source": "EDINET 有価証券報告書(金融庁)", "license": "公共データ利用規約(PDL1.0)"}


def _health() -> dict:
    n = len(store.registry())
    return {"ok": n > 0, "companies": n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--host", default=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_HTTP_PORT", "8767")))
    a = ap.parse_args()
    if a.http:
        from polyarchy_common.mcp_http import serve_streamable_http
        serve_streamable_http(mcp, host=a.host, port=a.port, path=os.getenv("MCP_HTTP_PATH", "/mcp"),
                              tools_desc="find_company / lookup_company_facts / lookup_segments / lookup_regions / list_items / list_metrics / screen_companies / screen_trend", logger_name="polyarchy.companies",
                              health_check=_health)
        return 0
    log.info("companies MCP（stdio）起動＝収録 %d 社", len(store.registry()))
    anyio.run(_run_stdio)
    return 0


async def _run_stdio() -> None:
    """プロトコルは確保しておいた実 stdout へ（sys.stdout は stderr に差し替え済み＝ライブラリの print が伝送路を汚さない）。"""
    from io import TextIOWrapper

    from mcp.server.stdio import stdio_server
    out = anyio.wrap_file(TextIOWrapper(_REAL_STDOUT.buffer, encoding="utf-8"))
    async with stdio_server(stdout=out) as (r, w):
        await mcp._mcp_server.run(r, w, mcp._mcp_server.create_initialization_options())


if __name__ == "__main__":
    sys.exit(main())
