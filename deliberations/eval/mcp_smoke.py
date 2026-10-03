"""
MCP サーバのスモーク：ツールが 2 本・読み取り専用の注記・検索と一覧が返り値の契約どおり。

    python -m deliberations.eval.mcp_smoke
"""
import asyncio
import os
import sys

os.environ.setdefault("DELIB_QUERY_LOG", "off")  # スモークの問を捕捉ログに混ぜない

from deliberations.serving import mcp_server as srv

NEED_KEYS = {"org", "meeting", "session_no", "date", "doc_kind", "material_no", "title", "role", "who",
             "source_url", "text", "notes"}


def main() -> int:
    bad = []
    tools = asyncio.run(srv.mcp.list_tools())
    names = sorted(t.name for t in tools)
    if names != ["list_meeting", "search_deliberations"]:
        bad.append(f"ツール {names}")
    for t in tools:
        if not (t.annotations and t.annotations.readOnlyHint):
            bad.append(f"{t.name} に readOnlyHint が無い")
        if "layer" in (t.inputSchema.get("properties") or {}):
            bad.append(f"{t.name} に layer 引数がある（公開固定に反する）")
    r = srv.search_deliberations("デジタル行財政改革会議の改組と事務局機能の移管", top_k=5)
    if not r["results"]:
        bad.append("検索が空")
    for h in r["results"]:
        if NEED_KEYS - set(h):
            bad.append(f"返り値に欠け {NEED_KEYS - set(h)}")
        if h["role"] in ("構成員（政府外）", "外部（ヒアリング）", "不明") and not h["notes"]:
            bad.append("見解の注記が無い")
    if not srv.search_deliberations("x", roles=["大臣"]).get("error"):
        bad.append("語彙に無い区分を受け付けた")
    m = srv.list_meeting("dgk", 14)
    if not m.get("found") or not m.get("items"):
        bad.append("list_meeting dgk 14 が空")
    if srv.list_meeting("dgk", 999).get("found"):
        bad.append("無い回で found=true")
    r2 = srv.search_deliberations("AI戦略会議で構成員から出た懸念", orgs=["ai_senryaku"], roles=["不明"], top_k=3)
    if any(h["who"] != "不明" for h in r2["results"]):
        bad.append("roles=不明 の絞り込みに名前つきが混じった")
    print(f"[mcp_smoke] ツール {names}・検索 {len(r['results'])} 件・一覧 {len(m.get('items', []))} 件・取り違え {len(bad)}")
    for b in bad:
        print("  ✗", b)
    print("PASS" if not bad else "FAIL")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
