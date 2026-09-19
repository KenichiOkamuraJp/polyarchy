"""
Phase 10：MCP スモークテスト（実プロトコル越しの疎通確認）。

`python -m recommendations.serving.mcp_server` を stdio サブプロセスとして起動し、MCP クライアントとして
接続してツールを実際に叩く。これが通ることは同時に以下を証明する：
  (a) search_policy_docs が MCP から叩けてランク済チャンク＋メタを返す。
  (d) stdout がクリーン＝ライブラリの print/進捗バーが JSON-RPC を壊していない
      （壊れていればクライアントの JSON パースが失敗する）。
  (b') tool schema に layer 引数が存在しない（機密を要求する術がない）。

実行：
    python -m recommendations.eval.mcp_smoke
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parents[2]  # リポジトリ root（python -m recommendations.… の cwd）


def _result_payload(result) -> dict:
    """CallToolResult から dict ペイロードを取り出す（structuredContent 優先）。"""
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_text": text}
    return {}


async def main() -> int:
    # 捕捉ログを一時ファイルへ隔離（本番 real fuel を汚さない・§31.8）。MCP stdio は既定で
    # 最小環境しか子へ渡さないため、POLYARCHY_QUERY_LOG を env で明示転送する（未転送だと
    # サーバ子プロセスが既定の data/query_log/queries.jsonl＝本番ログに書いてしまう）。
    tmp_log = Path(tempfile.gettempdir()) / "polyarchy_mcp_smoke_qlog.jsonl"
    tmp_log.unlink(missing_ok=True)
    child_env = {**os.environ, "POLYARCHY_QUERY_LOG": str(tmp_log)}
    params = StdioServerParameters(
        command=sys.executable,           # polyarchy env の python
        args=["-m", "recommendations.serving.mcp_server"],
        cwd=str(HERE),
        env=child_env,
    )
    print("=== MCP スモークテスト開始（stdio 越しに recommendations.serving.mcp_server へ接続）===")
    ok = True
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1) ツール一覧 & layer 引数の非存在を確認 -------------------------
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"[1] 公開ツール: {names}")
            assert "search_policy_docs" in names, "search_policy_docs が公開されていない"
            spd = next(t for t in tools.tools if t.name == "search_policy_docs")
            props = (spd.inputSchema or {}).get("properties", {})
            print(f"    search_policy_docs 引数: {list(props)}")
            has_layer = any("layer" in p.lower() or "機密" in p for p in props)
            print(f"    layer 引数の有無: {'あり(!!)' if has_layer else 'なし（機密は要求不可）'}")
            ok = ok and not has_layer

            # 2) 通常検索（一発）---------------------------------------------
            q = "最低賃金の全国加重平均1500円への引き上げについて各団体の立場は？"
            r = await session.call_tool("search_policy_docs", {"query": q, "top_k": 5})
            payload = _result_payload(r)
            res = payload.get("results", [])
            layers = sorted({c["layer"] for c in res})
            print(f"[2] 一発検索: {payload.get('count')} 件, 層={layers}, "
                  f"適用={payload.get('applied_filter')}")
            for c in res[:3]:
                print(f"      #{c['rank']} {c['file_name']} [{c['org']}/{c['date']}] "
                      f"score={c['score']} p.{c['page']}")
            ok = ok and len(res) > 0 and layers == ["公開"]
            # 収録範囲の明示（設計＝recommendations/docs/コーパス収録範囲の明示.md）：
            # coverage_note は全レスポンス必須・freshness は全 5 団体分（orgs 未指定）。
            fresh = payload.get("freshness") or {}
            print(f"    coverage_note: {'あり' if payload.get('coverage_note') else 'なし(!!)'}, "
                  f"freshness 団体={sorted(fresh)}")
            ok = ok and bool(payload.get("coverage_note"))
            ok = ok and sorted(fresh) == ["doyukai", "gov", "keidanren", "nissho", "rengo"]
            ok = ok and all(v.get("last_ingested") and v.get("latest_doc_date")
                            for v in fresh.values())

            # 3) 団体フィルタ（多段検索の構成要素）---------------------------
            r2 = await session.call_tool(
                "search_policy_docs", {"query": "最低賃金 1500円", "orgs": ["nissho"], "top_k": 3})
            p2 = _result_payload(r2)
            orgs2 = sorted({c["org"] for c in p2.get("results", [])})
            print(f"[3] 団体=nissho 検索: {p2.get('count')} 件, 団体={orgs2}, "
                  f"freshness={sorted(p2.get('freshness') or {})}")
            ok = ok and orgs2 == ["nissho"]
            # orgs 指定時は freshness も該当団体のみ返る。
            ok = ok and sorted(p2.get("freshness") or {}) == ["nissho"]

            # 4) 日付フィルタ ------------------------------------------------
            r3 = await session.call_tool(
                "search_policy_docs", {"query": "カーボンニュートラル GX", "since": 20250101, "top_k": 3})
            p3 = _result_payload(r3)
            dates3 = [c["date"] for c in p3.get("results", [])]
            print(f"[4] since=20250101 検索: 日付={dates3}")
            ok = ok and all(d >= "2025" for d in dates3 if d)

            # 5) list_orgs ---------------------------------------------------
            r4 = await session.call_tool("list_orgs", {})
            p4 = _result_payload(r4)
            codes = [o["code"] for o in p4.get("orgs", [])]
            print(f"[5] list_orgs: {codes} coverage_note="
                  f"{'あり' if p4.get('coverage_note') else 'なし(!!)'}")
            ok = ok and "keidanren" in codes and "rengo" in codes
            # 発見層にも coverage_note＋団体別鮮度（last_ingested）を載せる。
            ok = ok and bool(p4.get("coverage_note"))
            ok = ok and all(o.get("last_ingested") for o in p4.get("orgs", []))

            # 6) 低ヒット時の coverage_warning（未来日付フィルタで空振りを作る）------
            r5 = await session.call_tool(
                "search_policy_docs", {"query": "最低賃金", "since": 20990101, "top_k": 3})
            p5 = _result_payload(r5)
            print(f"[6] 空振り検索(since=20990101): {p5.get('count')} 件, "
                  f"coverage_warning={'あり' if p5.get('coverage_warning') else 'なし(!!)'}")
            ok = ok and p5.get("count") == 0 and bool(p5.get("coverage_warning"))

            # 7) top_k 緩和（バッチ2 段2）：上限 20 まで返り、超過要求は applied_filter に明示 ---
            r6 = await session.call_tool(
                "search_policy_docs", {"query": "賃上げ", "top_k": 25})
            p6 = _result_payload(r6)
            print(f"[7] top_k=25 要求: {p6.get('count')} 件返却, 適用={p6.get('applied_filter')}")
            ok = ok and p6.get("count", 0) > 5 and p6.get("count", 99) <= 20
            ok = ok and "top_k=25→20に制限" in (p6.get("applied_filter") or "")

            # 8) sweep_policy_docs（バッチ2 段2・多段のツール昇格）：schema に layer なし -----
            assert "sweep_policy_docs" in names, "sweep_policy_docs が公開されていない"
            swp = next(t for t in tools.tools if t.name == "sweep_policy_docs")
            sprops = (swp.inputSchema or {}).get("properties", {})
            has_layer_s = any("layer" in p.lower() or "機密" in p for p in sprops)
            print(f"[8] sweep_policy_docs 引数: {list(sprops)} "
                  f"layer 引数={'あり(!!)' if has_layer_s else 'なし（機密は要求不可）'}")
            ok = ok and not has_layer_s

            # 9) sweep 網羅（coverage）：団体別 fan-out で複数団体が返る --------------
            r7 = await session.call_tool(
                "sweep_policy_docs",
                {"query": "各団体は賃上げについてどのような立場か", "mode": "coverage"})
            p7 = _result_payload(r7)
            breadth = len({c["org"] for c in p7.get("results", [])})
            layers7 = sorted({c["layer"] for c in p7.get("results", [])})
            cov_rows = p7.get("org_coverage", [])
            print(f"[9] sweep(coverage): {p7.get('count')} 件, 団体幅={breadth}, 層={layers7}, "
                  f"org_coverage={len(cov_rows)}団体, 適用={p7.get('applied_filter')}")
            ok = ok and p7.get("strategy") == "coverage" and breadth >= 2
            ok = ok and layers7 == ["公開"] and len(cov_rows) == 5
            ok = ok and bool(p7.get("coverage_note"))

            # 10) sweep 集約（aggregation）：複数団体言及の最上級は abstain＋根拠 ------
            r8 = await session.call_tool(
                "sweep_policy_docs",
                {"query": "最低賃金の引き上げを最初に提言したのはどの団体か", "mode": "aggregation"})
            p8 = _result_payload(r8)
            print(f"[10] sweep(aggregation): decision={p8.get('decision')} "
                  f"rationale={'あり' if p8.get('rationale') else 'なし(!!)'} "
                  f"evidence={len(p8.get('org_coverage', []))}団体")
            ok = ok and p8.get("strategy") == "aggregation"
            ok = ok and p8.get("decision") in ("resolve", "abstain")
            ok = ok and bool(p8.get("rationale")) and len(p8.get("org_coverage", [])) == 5

            # 11) sweep auto：網羅マーカー（"それぞれ"）で coverage に倒れる ----------
            r9 = await session.call_tool(
                "sweep_policy_docs",
                {"query": "エネルギー政策についてそれぞれの団体の主張は", "mode": "auto"})
            p9 = _result_payload(r9)
            print(f"[11] sweep(auto): 戦略={p9.get('strategy')} {p9.get('count')} 件")
            ok = ok and p9.get("strategy") == "coverage" and p9.get("count", 0) > 0

            # 12) field 絞り（B22）：発行元タグの無い gov でも 21 分類で絞れる＋照合先の明示 -------
            r10 = await session.call_tool(
                "search_policy_docs", {"query": "需給ギャップ", "orgs": ["gov"], "field": "財政", "top_k": 3})
            p10 = _result_payload(r10)
            tags10 = [c.get("policy_tags") for c in p10.get("results", [])]
            print(f"[12] gov×field=財政: {p10.get('count')} 件, 適用={p10.get('applied_filter')}, "
                  f"policy_tags={tags10[:1]}")
            ok = ok and p10.get("count", 0) > 0 and "税制・財政" in (p10.get("applied_filter") or "")
            ok = ok and all(any("財政" in t for t in (ts or [])) for ts in tags10)
            r11 = await session.call_tool(
                "search_policy_docs", {"query": "需給ギャップ", "field": "存在しない分野", "top_k": 1})
            p11 = _result_payload(r11)
            print(f"[12b] 語彙外 field: hint={'あり' if p11.get('hint') else 'なし'}")
            ok = ok and bool(p11.get("hint"))
            r12 = await session.call_tool("list_orgs", {})
            p12 = _result_payload(r12)
            ok = ok and len(p12.get("fields", [])) == 21

    print("=" * 60)
    print(f"総合: {'PASS ✅' if ok else 'FAIL ❌'}  "
          f"（MCP 疎通・公開ツール・層公開固定・stdout クリーン・収録範囲明示・"
          f"top_k緩和・sweep 多段・field 21 分類を確認）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
