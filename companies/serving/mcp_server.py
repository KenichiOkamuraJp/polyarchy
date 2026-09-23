"""Polyarchy 企業情報DB（companies）の MCP サーバ。読み取り専用・公開データのみ・層は公開固定（layer 引数なし）。

  python -m companies.serving.mcp_server                       # stdio
  python -m companies.serving.mcp_server --http --port 8767    # Streamable HTTP（配信形）
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from polyarchy_common.logsetup import configure_quiet_logging, get_logger, guard_stdout_for_stdio

_REAL_STDOUT = guard_stdout_for_stdio() if "--http" not in sys.argv else None  # stdio では stdout がプロトコル線

import anyio  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from companies.core import lookup, segments, store  # noqa: E402
from companies.core.items import ITEMS  # noqa: E402
from polyarchy_common.capture import append_record  # noqa: E402

configure_quiet_logging()
log = get_logger("polyarchy.companies")
QUERY_LOG = Path(os.getenv("COMPANIES_QUERY_LOG") or Path(__file__).resolve().parent.parent / "data" / "query_log" / "queries.jsonl")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

SERVER_INSTRUCTIONS = (
    "Polyarchy 企業情報DB(companies)。日本の有価証券報告書(EDINET・金融庁)の「主要な経営指標等の推移」と従業員の状況を、"
    "公表どおりの値で厳密参照する読み取り専用サービス(公開データのみ)。2層構造:発見層(find_company=企業の同定/list_items=項目の語彙)と"
    "参照層(lookup_company_facts=値の完全一致参照/lookup_segments=セグメント別の値を表ごと)。値は XBRL に書かれた文字列のまま返す(換算・丸め・補完なし。比率は 0.444 の形・金額は円)。"
    "連結と単体(提出会社)は別の系列で、どちらの値かを必ず返す。該当が無ければ found=false と理由を返し、別の連結/単体の別・隣の項目・近い決算期の値では埋めない"
    "(例=IFRS の会社は「売上高」ではなく「売上収益」(item=revenue)、持株会社・金融・建設などは「営業収益」「経常収益」「完成工事高」や"
    "会社が独自に定義した項目で開示している=net_sales が found=false のとき suggest に、その会社が最上段の収益として開示している項目と"
    "引き直し方が返る。alternatives にはその決算期に開示している全項目の一覧が返る)。"
    "セグメント別(lookup_segments)は会社が定義した区分をその会社のラベルのまま返し、業種横断の区分や利益の物差しに寄せない"
    "(セグメント利益が営業利益か経常利益か事業利益かは会社の定義=要素とラベルのまま)。"
    "各値には出典(書類管理番号・提出日・要素・context・URL・引用1行)が付く。派生値(利益率・前年比 等)は計算しない。"
    "値そのものは各提出会社の開示に帰属し、本サービスは値を保証しない(原典で確認すること)。"
)
mcp = FastMCP("polyarchy-companies", instructions=SERVER_INSTRUCTIONS)


def _capture(tool: str, args: dict, r: dict) -> None:
    append_record(QUERY_LOG, {"tool": tool, "args": args, "found": r.get("found"), "reason": r.get("reason")})


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
def lookup_segments(company: str, period: str, basis: str | None = None, doc_id: str | None = None) -> dict:
    """企業×決算期のセグメント別の値を、有価証券報告書に書かれたとおりに表ごと返す(完全一致参照)。

    company=EDINET コード・証券コード・社名。period=決算期末の YYYY-MM(各書類に当期・前期の 2 期だけ載る)。
    basis=consolidated/non_consolidated(省くと、連結を作成している会社は連結)。doc_id=書類管理番号(省くと提出日が最新の書類)。
    返り値=segments(区分の一覧=member〔要素 ID〕・label〔会社のラベル。標準の区分で会社のラベルが無ければタクソノミの標準ラベル〕・kind)・facts(区分×要素の値=member・element・label・
    section〔segment_information=セグメント情報の注記/employees=従業員の状況/capex=設備投資/research_and_development=研究開発〕・
    value〔文字列のまま〕・unit・decimals)・source。
    区分は会社の定義のまま(業種横断の区分に寄せない)。kind=company_defined/reportable_total/other_reportable/other/reconciling/
    corporate/unallocated_and_elimination/total/other_standard(意味は返り値の kind_note)。company_defined は事業セグメントとは限らない
    (会社独自の調整額・消去・全社・小計もこの kind)=何の区分かは label で読む。区分の足し算の関係は返さない=合計を作るときは
    kind と label を見て調整額・全社・合計・小計を二重に数えない。
    利益の物差し(営業利益・経常利益・事業利益・セグメント利益 等)は会社ごとに違う=element と label のまま扱う。
    無ければ found=false と reason(no_segment_figures=セグメント情報の注記に数値が無い〔単一セグメント・記載の省略 等〕→ quote に会社の文 1 行 /
    not_tagged=米国基準の会社で注記が XBRL に無い〔本文の表だけ〕/ no_consolidated_statements / out_of_range / bad_period /
    unknown_company / ambiguous_company)。数値が無いときもタグのある欄(従業員の状況 等)は other_sections に返る。
    """
    r = segments.lookup_segments(company, period, basis=basis, doc_id=doc_id)
    _capture("lookup_segments", {"company": company, "period": period, "basis": basis, "doc_id": doc_id}, r)
    return r


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
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767)
    a = ap.parse_args()
    if a.http:
        from polyarchy_common.mcp_http import serve_streamable_http
        serve_streamable_http(mcp, host=a.host, port=a.port, path=os.getenv("MCP_HTTP_PATH", "/mcp"),
                              tools_desc="find_company / lookup_company_facts / lookup_segments / list_items", logger_name="polyarchy.companies",
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
