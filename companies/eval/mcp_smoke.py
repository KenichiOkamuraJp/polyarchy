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


INSTRUCTIONS: list[str] = [""]
TOOL_NAMES: list[str] = []  # 出力の 1 行目に本数と名前を出す（配布後に手元で本数を確かめられるように）


async def run() -> list[str]:
    errs: list[str] = []
    env = {**os.environ, "COMPANIES_QUERY_LOG": os.path.join(tempfile.mkdtemp(), "q.jsonl")}  # スモークで捕捉ログを汚さない
    params = StdioServerParameters(command=sys.executable, args=["-m", "companies.serving.mcp_server"], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:  # stdout が汚れていればここで壊れる
        init = await s.initialize()
        INSTRUCTIONS[:] = [init.instructions or ""]
        tools = {t.name: t for t in (await s.list_tools()).tools}
        TOOL_NAMES[:] = sorted(tools)
        if set(tools) != {"find_company", "lookup_company_facts", "lookup_segments", "lookup_regions", "list_items",
                          "list_metrics", "screen_companies", "screen_trend"}:
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
        r7 = await call("lookup_regions", company="E99999", period="2025-03")
        if r7.get("found") or r7.get("reason") != "unknown_company" or r7.get("sections"):
            errs.append(f"[12] 地域別：存在しない会社で fail-closed にならない: {r7}")
        r8 = await call("lookup_regions", company="E99999", period="FY2024")
        if r8.get("found") or r8.get("reason") != "bad_period":
            errs.append(f"[13] 地域別：年度表記を弾かない: {r8}")
        # 結合セルを展開して読むと二重に数える＝説明で案内する（契約 §6）
        if "結合セルは展開しない" not in (getattr(tools.get("lookup_regions"), "description", "") or ""):
            errs.append("[14] lookup_regions の説明に、結合セルを展開しない旨が無い")
        # 第 1c 便：ツールの説明に指標の一覧を書かない（型を足してもツールの定義が変わらない＝ChatGPT のコネクタを作り直さずに済む）
        from companies.core.screen import PRESETS
        desc = getattr(tools.get("screen_companies"), "description", "") or ""
        if "list_metrics" not in desc or any(k in desc for k in PRESETS):
            errs.append("[15] screen_companies の説明が list_metrics を案内しない／型の名前を並べている")
        m = await call("list_metrics")
        if not m.get("presets") or "gross_profit" not in (m.get("not_ingested") or {}) or len(m.get("industries") or []) != 33:
            errs.append("[16] list_metrics が型・未収録の目録・業種を返さない")
        r9 = await call("screen_companies", conditions=[{"expr": "gross_profit / top_line"}])
        if r9.get("found") or r9.get("reason") != "input_not_ingested":
            errs.append(f"[17] 未収録の項目の式を弾かない: {r9.get('reason')}")
        r10 = await call("screen_companies", conditions=[{"expr": "secret_word_xyz / top_line"}])
        if r10.get("found") or r10.get("reason") != "unknown_item":
            errs.append(f"[18] 語彙に無い語を弾かない: {r10.get('reason')}")
        r11 = await call("screen_companies", conditions=[{"metric": "roa", "min": 0.3}], limit=3)
        if not r11.get("found") or len(r11.get("rows") or []) > 3 or "excluded" not in r11:
            errs.append(f"[19] 横断検索が行と除外を返さない: {str(r11)[:200]}")
        recs = [json.loads(l) for l in open(env["COMPANIES_QUERY_LOG"]) if l.strip()]
        scr = [x for x in recs if x.get("tool") == "screen_companies"]
        vocab = [u for x in scr for u in x.get("unavailable_vocab") or []]
        if not any(u["term"] == "gross_profit" and u["level"] == "input_not_ingested" for u in vocab):
            errs.append("[20] 未収録の項目が捕捉ログ（unavailable_vocab）に残らない")
        if any(u["term"] == "secret_word_xyz" for u in vocab):
            errs.append("[21] 語彙に無い語（利用者が書いた語）が恒久集計向けの unavailable_vocab に入った")
        if not any(u.get("term") == "secret_word_xyz" for x in scr for u in x.get("unavailable") or []):
            errs.append("[22] 語彙に無い語が捕捉ログ（30 日）に残らない＝triage で拾えない")
        if "横断" not in INSTRUCTIONS[0]:
            errs.append("[23] サーバの説明に横断検索（派生値を計算する唯一の入口）が無い")
        # 第 1e 便（時系列）：集約の語彙はツールの説明に書かず list_metrics に（足しても定義が変わらない）
        from companies.core.trend import AGGREGATES
        tdesc = getattr(tools.get("screen_trend"), "description", "") or ""
        if "list_metrics" not in tdesc or any(f"{k}=" in tdesc or f"「{k}」" in tdesc for k in AGGREGATES if k not in ("min", "max", "mean")):
            errs.append("[24] screen_trend の説明が list_metrics を案内しない／集約の名前を並べている")
        if set(AGGREGATES) - set((m.get("aggregates") or {})):
            errs.append("[25] list_metrics が集約の語彙を返さない")
        for name in ("lookup_segments", "lookup_regions"):
            props = (tools[name].inputSchema.get("properties") or {}) if name in tools else {}
            if not {"period_from", "period_to"} <= set(props):
                errs.append(f"[26] {name} に期間（period_from／period_to）の引数が無い")
        if "elements" not in ((tools["lookup_segments"].inputSchema.get("properties") or {}) if "lookup_segments" in tools else {}):
            errs.append("[27] lookup_segments に要素で絞る引数（elements）が無い（11 年で約 99KB）")
        t1 = await call("screen_trend", conditions=[{"metric": "top_line", "aggregate": "median"}])
        if t1.get("found") or t1.get("reason") != "unknown_aggregate":
            errs.append(f"[28] 語彙に無い集約を弾かない: {t1.get('reason')}")
        t2 = await call("screen_trend", conditions=[{"metric": "top_line", "aggregate": "streak_up", "min": 12}], limit=3)
        if not t2.get("found") or "excluded" not in t2 or len(t2.get("rows") or []) > 3:
            errs.append(f"[29] screen_trend が行と除外を返さない: {str(t2)[:200]}")
        res = await s.call_tool("screen_trend", {"conditions": [{"metric": "top_line", "aggregate": "cagr"}], "limit": 20})
        if len(res.content[0].text.encode()) > 50_000:  # 伝送路の上の文字列で測る（字下げも数える）
            errs.append(f"[34] screen_trend の返り値が大きい（上位 20 社で {len(res.content[0].text.encode()) // 1000}KB・上限 50KB）")
        if not {"companies", "detail"} <= set((tools["screen_trend"].inputSchema.get("properties") or {}) if "screen_trend" in tools else {}):
            errs.append("[35] screen_trend に companies／detail の引数が無い（細い返り値の詳細を引き直す入口）")
        t3 = await call("screen_trend", conditions=[{"metric": "gross_profit", "aggregate": "cagr"}])
        if t3.get("found") or t3.get("reason") != "input_not_ingested":
            errs.append(f"[30] screen_trend：未収録の項目を弾かない: {t3.get('reason')}")
        recs = [json.loads(l) for l in open(env["COMPANIES_QUERY_LOG"]) if l.strip()]
        if not any(u["term"] == "gross_profit" for x in recs if x.get("tool") == "screen_trend" for u in x.get("unavailable_vocab") or []):
            errs.append("[31] screen_trend の未収録の項目が捕捉ログ（unavailable_vocab）に残らない")
        if "screen_trend" not in json.dumps(r11, ensure_ascii=False):
            errs.append("[32] screen_companies の返り値に、複数年の条件は screen_trend の案内が無い")
        if "時系列" not in INSTRUCTIONS[0] or "screen_trend" not in INSTRUCTIONS[0]:
            errs.append("[33] サーバの説明に時系列（screen_trend）が無い")
        r4 = await call("find_company", query="")
        if r4.get("found"):
            errs.append("[8] 空の問い合わせで会社を返した")
    return errs


def main() -> int:
    try:
        errs = asyncio.run(asyncio.wait_for(run(), 120))  # ゲートは止まらない（応答が来なければ FAIL）
    except BaseException as e:  # noqa: BLE001  タイムアウトは TaskGroup の例外群で上がる
        errs = [f"[0] 疎通できない／時間切れ: {e!r}"[:300]]
    print(f"ツール {len(TOOL_NAMES)} 本: {', '.join(TOOL_NAMES)}")
    for e in errs:
        print("  FAIL", e)
    print("PASS: MCP 疎通・ツール定義・fail-closed・stdout クリーン" if not errs else f"FAIL: {len(errs)} 件")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
