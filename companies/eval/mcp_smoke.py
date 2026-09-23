"""MCP スモーク（stdio）＝疎通・ツール定義（読み取り専用・layer 引数なし）・fail-closed・stdout クリーン。

  python -m companies.eval.mcp_smoke
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run() -> list[str]:
    errs: list[str] = []
    env = {**os.environ, "COMPANIES_QUERY_LOG": os.path.join(tempfile.mkdtemp(), "q.jsonl")}  # スモークで捕捉ログを汚さない
    params = StdioServerParameters(command=sys.executable, args=["-m", "companies.serving.mcp_server"], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:  # stdout が汚れていればここで壊れる
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        if set(tools) != {"find_company", "lookup_company_facts", "lookup_segments", "list_items"}:
            errs.append(f"[1] ツール集合が違う: {sorted(tools)}")
        for t in tools.values():
            if "layer" in (t.inputSchema.get("properties") or {}):
                errs.append(f"[2] {t.name} に layer 引数がある（層は公開固定）")
            if not (t.annotations and t.annotations.readOnlyHint) or not t.title:
                errs.append(f"[3] {t.name} に title／readOnlyHint が無い")
        # 「合計して」と頼まれると区分から足し直した値を先に出した（2026-09-23 staging・ChatGPT）＝会社の計を答えにするよう説明で案内する
        if "合計を問われたら、会社が表に書いた計・合計を答えにする" not in (getattr(tools.get("lookup_segments"), "description", "") or ""):
            errs.append("[11] lookup_segments の説明に、会社の計・合計を答えにする案内が無い")

        async def call(name, **kw):
            res = await s.call_tool(name, kw)
            return json.loads(res.content[0].text)

        items = await call("list_items")
        if not any(i["item"] == "net_sales" for i in items["items"]) or items["n_companies"] < 1:
            errs.append("[4] list_items が語彙／収録社数を返さない")
        r1 = await call("lookup_company_facts", company="E99999", period="2025-03", item="total_assets")
        if r1.get("found") or r1.get("reason") != "unknown_company" or r1.get("value"):
            errs.append(f"[5] 存在しない会社で fail-closed にならない: {r1}")
        r2 = await call("lookup_company_facts", company="E99999", period="FY2024", item="total_assets")
        if r2.get("found") or r2.get("reason") != "bad_period":
            errs.append(f"[6] 年度表記を弾かない: {r2}")
        r3 = await call("lookup_company_facts", company="E99999", period="2025-03", item="ebitda")
        if r3.get("found") or r3.get("reason") != "unknown_item":
            errs.append(f"[7] 語彙に無い項目を弾かない: {r3}")
        r5 = await call("lookup_segments", company="E99999", period="2025-03")
        if r5.get("found") or r5.get("reason") != "unknown_company" or r5.get("facts"):
            errs.append(f"[9] セグメント：存在しない会社で fail-closed にならない: {r5}")
        r6 = await call("lookup_segments", company="E99999", period="FY2024")
        if r6.get("found") or r6.get("reason") != "bad_period":
            errs.append(f"[10] セグメント：年度表記を弾かない: {r6}")
        r4 = await call("find_company", query="")
        if r4.get("found"):
            errs.append("[8] 空の問い合わせで会社を返した")
    return errs


def main() -> int:
    try:
        errs = asyncio.run(asyncio.wait_for(run(), 120))  # ゲートは止まらない（応答が来なければ FAIL）
    except BaseException as e:  # noqa: BLE001  タイムアウトは TaskGroup の例外群で上がる
        errs = [f"[0] 疎通できない／時間切れ: {e!r}"[:300]]
    for e in errs:
        print("  FAIL", e)
    print("PASS: MCP 疎通・ツール定義・fail-closed・stdout クリーン" if not errs else f"FAIL: {len(errs)} 件")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
