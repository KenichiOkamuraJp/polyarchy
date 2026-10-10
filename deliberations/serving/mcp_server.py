"""
審議会議事録DB の MCP サーバ（読み取り専用・公開データのみ・層は公開固定）。

    python -m deliberations.serving.mcp_server                      # stdio（ローカル）
    python -m deliberations.serving.mcp_server --http --port 8768   # Streamable HTTP（配信形）

ツールは 2 本（ツール数の増殖はルーティングを劣化させる＝長期開発計画 §3）：
- search_deliberations … 会議の過程文書（配布資料・議事録・議事要旨）のハイブリッド検索
- list_meeting         … 1 つの回の資料と記録の一覧（決定論の参照）
"""
import argparse
import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from polyarchy_common.logsetup import configure_quiet_logging, get_logger, guard_stdout_for_stdio

INSTRUCTIONS = (
    "Polyarchy 審議会議事録DB(deliberations)。政府の会議体の過程文書＝回ごとの配布資料・議事録・議事要旨を"
    "横断検索する読み取り専用サービス(公開データのみ)。収録＝デジタル行財政改革会議(本会議)・人工知能戦略本部・"
    "人工知能戦略専門調査会・AI戦略会議・規制改革推進会議 デジタル・AIワーキング・グループ(企業・団体・府省の"
    "ヒアリング)・先進的AI利活用アドバイザリーボード(デジタル庁)・日本成長戦略会議・日本成長戦略本部。会議の決定文書(取りまとめ・基本計画など)は政策主張DB"
    "(search_policy_docs)にあり、ここには入れない＝決まった内容は政策主張DB、決まるまでの議論・資料はこちら。"
    "返す断片には毎回、会議体・回次・開催日・資料番号・提出者または発言者とその区分(政務/事務局/府省・会議体/"
    "構成員(政府外)/外部(ヒアリング)/不明)・出典URLが付く。構成員(政府外)・外部・不明の断片は提出者・発言者の"
    "見解であって会議・政府の決定ではない(返り値の notes に明記)。発言者・提出者は記録と資料に書かれたとおりで、"
    "書かれていなければ不明と返す(推定しない)。匿名の要約(発言者の名前が無い議事要旨)の発言は誰の発言か特定"
    "できない。文章の生成や要約はしない(利用側で行う)。該当が無ければ results は空。"
)

SEARCH_DESC = (
    "審議会・会議体の配布資料と議事録・議事要旨を検索する(関連度順の抜粋＋帰属)。"
    "例：「第14回デジタル行財政改革会議で担当大臣の資料は会議の改組について何と書いているか」"
    "「AI戦略会議で構成員からどんな懸念が出たか」。絞り込み＝orgs(会議体: dgk=デジタル行財政改革会議・"
    "ai_hq=人工知能戦略本部・ai_senmon=人工知能戦略専門調査会・ai_senryaku=AI戦略会議・"
    "kisei_ai_wg=規制改革推進会議 デジタル・AIワーキング・グループ・da_ai_board=先進的AI利活用アドバイザリーボード・"
    "seicho=日本成長戦略会議・seicho_honbu=日本成長戦略本部)・"
    "since/until"
    "(YYYYMMDD)・doc_kinds(資料/参考資料/議事録/議事要旨)・roles(政務/事務局/府省・会議体/構成員(政府外)/"
    "外部(ヒアリング)/不明)。top_k は既定 5・上限 20。ある論点が最初に出た回や前後の回を見るときは、"
    "ヒットの会議体と回次で list_meeting を呼ぶ。"
)
LIST_DESC = (
    "1 つの回(会議体のコードと回次)の資料と記録の一覧を返す(検索ではなく決定論の参照)。各資料の資料番号・名前・"
    "提出者と区分・出典URL、記録(議事録・議事要旨)の型、非公開で名前だけ載っている資料を含む。回が無ければ found=false。"
)

log = get_logger("polyarchy.deliberations.mcp")
mcp = FastMCP("polyarchy-deliberations", instructions=INSTRUCTIONS)
_svc = None


def svc():
    global _svc
    if _svc is None:
        from deliberations.core.search import DeliberationsSearch
        _svc = DeliberationsSearch()
    return _svc


def capture(tool: str, args: dict, n: int) -> None:
    """捕捉ログ（評価の燃料・30 日で削除）：ツール名・引数・件数・時刻（＋認証済みなら user_hash）。会話本文は取らない。"""
    import os as _os
    from pathlib import Path

    from deliberations.core.paths import QUERY_LOG
    from polyarchy_common.capture import append_record
    path = Path(_os.getenv("DELIB_QUERY_LOG", str(QUERY_LOG)))
    if _os.getenv("DELIB_QUERY_LOG", "") != "off":
        append_record(path, {"tool": tool, **{k: v for k, v in args.items() if v not in (None, [], "")},
                             "hits": n}, logger_name="polyarchy.deliberations.capture")


def _tuple(x) -> tuple:
    if not x:
        return ()
    return tuple(x) if isinstance(x, (list, tuple)) else (x,)


@mcp.tool(name="search_deliberations", title="審議会議事録DB：資料・議事録の検索", description=SEARCH_DESC,
          annotations=ToolAnnotations(title="審議会議事録DB：資料・議事録の検索", readOnlyHint=True, openWorldHint=False))
def search_deliberations(query: str, orgs: list[str] | None = None, since: int | None = None,
                         until: int | None = None, doc_kinds: list[str] | None = None,
                         roles: list[str] | None = None, top_k: int = 5) -> dict:
    from deliberations.core import config
    from deliberations.core.search import DOC_KINDS, ROLES, DelibFilter
    from deliberations.ingest.sources import BY_ORG
    bad = ([o for o in _tuple(orgs) if o not in BY_ORG] + [k for k in _tuple(doc_kinds) if k not in DOC_KINDS]
           + [r for r in _tuple(roles) if r not in ROLES])
    if bad:
        return {"query": query, "results": [], "error": f"語彙に無い値: {bad}",
                "vocabulary": {"orgs": list(BY_ORG), "doc_kinds": list(DOC_KINDS), "roles": list(ROLES)}}
    k = max(1, min(int(top_k or config.TOP_K), config.TOP_K_MAX))
    flt = DelibFilter(orgs=_tuple(orgs), date_from=since, date_to=until, doc_kinds=_tuple(doc_kinds), roles=_tuple(roles))
    hits = svc().search(query, flt, top_k=k)
    capture("search_deliberations", {"query": query, "orgs": orgs, "since": since, "until": until,
                                     "doc_kinds": doc_kinds, "roles": roles, "top_k": k}, len(hits))
    out = {"query": query, "results": [h.to_dict() for h in hits]}
    if not hits:
        from deliberations.core.search import coverage_note
        out["coverage_note"] = coverage_note()
    return out


@mcp.tool(name="list_meeting", title="審議会議事録DB：回の資料と記録の一覧", description=LIST_DESC,
          annotations=ToolAnnotations(title="審議会議事録DB：回の資料と記録の一覧", readOnlyHint=True, openWorldHint=False))
def list_meeting(org: str, session_no: int) -> dict:
    from deliberations.core.search import list_meeting as _list
    from deliberations.ingest.sources import BY_ORG
    r = _list(org, int(session_no))
    capture("list_meeting", {"org": org, "session_no": session_no}, len(r["items"]) if r else 0)
    if r is None:
        return {"found": False, "org": org, "session_no": session_no,
                "reason": f"その会議体・回次は収録していない（会議体のコードは {'/'.join(BY_ORG)}）"}
    return {"found": True, **r}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--host", default=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_HTTP_PORT", "8768")))
    args = ap.parse_args()
    configure_quiet_logging()
    if args.http:
        from polyarchy_common.mcp_http import serve_streamable_http
        svc()  # 起動時に読み込む（最初の問を待たせない）
        serve_streamable_http(mcp, host=args.host, port=args.port, path=os.getenv("MCP_HTTP_PATH", "/mcp"),
                              tools_desc="tools=search_deliberations,list_meeting",
                              logger_name="polyarchy.deliberations.mcp",
                              health_check=lambda: {"ok": svc().count > 0, "points": svc().count})
        return
    real = guard_stdout_for_stdio()
    from io import TextIOWrapper

    import anyio
    from mcp.server.stdio import stdio_server

    async def run():
        async with stdio_server(stdout=anyio.wrap_file(TextIOWrapper(real.buffer, encoding="utf-8"))) as (r, w):
            await mcp._mcp_server.run(r, w, mcp._mcp_server.create_initialization_options())
    anyio.run(run)


if __name__ == "__main__":
    sys.exit(main())
