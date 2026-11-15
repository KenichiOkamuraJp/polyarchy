"""
MCP サーバのスモーク：ツールが 2 本・読み取り専用の注記・検索と一覧が返り値の契約どおり。

    python -m deliberations.eval.mcp_smoke
"""
import asyncio
import os
import re
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
        if not (t.annotations and t.annotations.read_only_hint and t.annotations.title
                and t.annotations.destructive_hint is False and t.annotations.open_world_hint is False) or not t.title:
            bad.append(f"{t.name} に title／注釈の title（annotations.title）／注釈の 3 つの明示（readOnlyHint=True・destructiveHint=False・openWorldHint=False）が無い")
        if "layer" in (t.input_schema.get("properties") or {}):
            bad.append(f"{t.name} に layer 引数がある（公開固定に反する）")
    # 会議体は説明文に列挙しない（2026-11-14〜・足すたびに ChatGPT のコネクタを作り直していた）＝説明文にコードが無く、
    # 返り値（該当なしの coverage_note・収録に無いコードを渡したときの vocabulary）に収録の全コードがあること。
    from deliberations.core.search import coverage, coverage_note
    from deliberations.ingest.sources import BY_ORG
    defs = srv.INSTRUCTIONS + srv.SEARCH_DESC + srv.LIST_DESC
    listed = sorted(code for code in BY_ORG if re.search(rf"(?<![a-z_]){code}(?![a-z_])", defs))
    if listed:
        bad.append(f"説明文に会議体のコード {listed}（会議体を足すたびにコネクタの作り直しが要る形に戻る）")
    note, held = coverage_note(), list(coverage())
    vocab = srv.search_deliberations("x", orgs=["no_such_org"]).get("vocabulary", {}).get("orgs", [])
    missing = sorted(code for code in held if code not in vocab or f"（{code}・" not in note)
    if missing or set(vocab) - set(held):
        bad.append(f"収録のある会議体と coverage_note・vocabulary が食い違う（無い {missing}・余分 {sorted(set(vocab) - set(held))}）")
    if empty := sorted(set(BY_ORG) - set(held)):
        print(f"[mcp_smoke] 取得元を定義したが収録の無い会議体（返り値に出さない）: {empty}")
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
