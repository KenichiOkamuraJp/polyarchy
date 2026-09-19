"""
stats MCP スモークテスト（実プロトコル越し・クレジット0）。

固定する契約：
1. 公開ツールが [list_datasets, find_statistics, lookup_statistic, lookup_panel, list_sources] であり、どれにも layer 引数が無い
2. 未登録の系列を lookup すると **found=false**（近似で何かを返さない＝fail-closed）
3. 発見層は該当なしで count=0
4. stdout がプロトコル以外で汚れていない（stdio 越しに JSON-RPC が壊れず往復できること自体が証明）
5. 捕捉ログ（STATS_QUERY_LOG）に fail-closed の記録が残る

実行（リポジトリ root）：  python -m stats.eval.mcp_smoke
"""
import asyncio
import json

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from stats.eval._client import payload as _payload, server_params


async def main() -> int:
    params, tmp_log = server_params("polyarchy_stats_smoke_qlog.jsonl")
    tmp_log.unlink(missing_ok=True)
    print("=== stats MCP スモークテスト開始（stdio 越しに stats.serving.mcp_server へ接続）===")
    ok = True
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"[1] 公開ツール: {names}")
            ok &= names == ["find_statistics", "list_datasets", "list_sources", "lookup_panel", "lookup_statistic"]
            for t in tools.tools:
                props = (t.inputSchema or {}).get("properties", {})
                if any("layer" in p.lower() for p in props):
                    print(f"    ✗ {t.name} に layer 引数がある"); ok = False
            print("    layer 引数: なし（機密は要求不可）")

            r = _payload(await session.call_tool("lookup_statistic",
                                                 {"series_id": "nonexistent.series", "period": "2024"}))
            print(f"[2] 未登録系列の参照: found={r.get('found')} reason={r.get('reason')}")
            ok &= r.get("found") is False and r.get("reason") == "unknown_series"

            r = _payload(await session.call_tool("find_statistics", {"query": "存在しない統計XYZ"}))
            print(f"[3] 発見層（該当なし）: count={r.get('count')} layer={r.get('layer')}")
            ok &= r.get("count") == 0

            r = _payload(await session.call_tool("lookup_statistic",
                                                 {"series_id": "cao.sna2020.gdp_nominal.fy", "period": "FY2000..FY2003"}))
            vals = r.get("values") or []
            print(f"[2b] 範囲指定（FY2000..FY2003）: found={r.get('found')} count={r.get('count')} "
                  f"values キー={sorted(vals[0].keys()) if vals else '-'} 出典系列単位={'accessor' not in (r.get('source') or {})}")
            ok &= r.get("found") is True and r.get("count") == 4 and len(vals) == 4
            ok &= all(set(v) >= {"period", "value"} for v in vals) and "value" not in r
            ok &= "accessor" not in (r.get("source") or {})     # 範囲の出典は系列単位（B2 契約）

            r = _payload(await session.call_tool("lookup_statistic",
                                                 {"series_id": "soumu.cpi2020.cpi_all.m", "period": "1970-01..2026-12"}))
            print(f"[2c] 範囲上限: found={r.get('found')} reason={r.get('reason')}（該当 {r.get('available')} > {r.get('limit')}・値は返さない）")
            ok &= r.get("found") is False and r.get("reason") == "range_too_wide" and "values" not in r

            r = _payload(await session.call_tool("lookup_statistic",
                                                 {"series_id": "cao.sna2020.gdp_nominal.fy", "period": "FY2003..FY2000"}))
            print(f"[2d] 逆順の範囲: found={r.get('found')} reason={r.get('reason')}")
            ok &= r.get("found") is False and r.get("reason") == "period_format"

            r = _payload(await session.call_tool("lookup_statistic",
                                                 {"series_id": "cao.sna2020.gdp_nominal.fy", "period": "FY2100..FY2105"}))
            print(f"[2e] 提供範囲外の範囲: found={r.get('found')} reason={r.get('reason')}")
            ok &= r.get("found") is False and r.get("reason") == "out_of_range"

            # lookup_panel（B11）：fail-closed は系列ごと（unknown は errors・他は返す）・上限超過は黙って切らない・誤 slug は errors
            r = _payload(await session.call_tool("lookup_panel", {"period": "FY2020..FY2024", "id_pattern": "mof.hojin.sales.{industry}-{size}.fy",
                                                                  "dims": {"industry": ["allexfin", "no_such_industry"], "size": ["allsize"]},
                                                                  "series_ids": ["mof.hojin.nonexistent.allexfin-allsize.fy"]}))
            errs = {e.get("series_id") or e.get("slug"): e.get("reason") for e in r.get("errors") or []}
            print(f"[3a] lookup_panel: found={r.get('found')} count={r.get('count')} errors={errs} caution={len((r.get('quality_summary') or {}).get('caution') or [])}")
            r2 = _payload(await session.call_tool("lookup_panel", {"period": "FY2024", "id_pattern": "mof.hojin.employees_avg.{industry}-{size}.fy",
                                                                   "dims": {"industry": ["services"]}}))
            print(f"[3d] lookup_panel 展開（services × 全規模）: series={r2.get('series_count')}")
            ok &= r2.get("series_count") == 5 and "mof.hojin.employees_avg.services-cap100m-1b.fy" in (r2.get("values") or {})
            ok &= (r.get("found") is True and r.get("count") == 5 and errs.get("mof.hojin.nonexistent.allexfin-allsize.fy") == "unknown_series"
                   and errs.get("no_such_industry") == "unknown_series" and "mof.hojin.sales.allexfin-allsize.fy" in (r.get("values") or {})
                   and r["quality_summary"]["caution"] == ["mof.hojin.sales.allexfin-allsize.fy"])
            r = _payload(await session.call_tool("lookup_panel", {"period": "FY1960..FY2024", "id_pattern": "mof.hojin.{measure}.{industry}-{size}.fy"}))
            print(f"[3b] lookup_panel 上限超過: found={r.get('found')} reason={r.get('reason')} requested_series={r.get('requested_series')}")
            ok &= r.get("found") is False and r.get("reason") == "range_too_wide" and "values" not in r
            r = _payload(await session.call_tool("lookup_panel", {"period": "FY2024", "series_ids": ["xx.yy.zz.a"]}))
            print(f"[3c] lookup_panel 全滅: found={r.get('found')} reason={r.get('reason')}")
            ok &= r.get("found") is False and r.get("reason") == "no_values_in_panel" and not r.get("values")
            # 第 7 弾 段 A（2026-08-22）：S-3＝展開 0 件でも診断（errors・coverage）を捨てない／A-4＝応答の大きさと verbose=false／S-1＝0 件の診断
            r = _payload(await session.call_tool("lookup_panel", {"period": "FY2024", "id_pattern": "mof.hojin.sales.{industry}-{size}.fy",
                                                                  "dims": {"industry": ["no_such_industry"]}}))
            print(f"[3e] lookup_panel 展開 0 件: reason={r.get('reason')} errors={len(r.get('errors') or [])} coverage={bool(r.get('coverage'))}")
            ok &= r.get("reason") == "no_series" and len(r.get("errors") or []) == 1 and bool(r.get("coverage")) and "coverage" in (r.get("hint") or "")
            # 利用側プロジェクトが実際に投げた形（葉 38 業種 × 規模 4 区分 × FY2009–24＝152 系列・2,432 値）
            leaves = ["mfg_chemical", "mfg_steel", "mfg_nonferrous", "mfg_ceramics", "mfg_petroleum_coal", "mfg_pulp_paper", "mfg_general_machinery",
                      "mfg_production_machinery", "mfg_business_machinery", "mfg_electrical", "mfg_ict_equipment", "mfg_transport_equipment", "mfg_food",
                      "mfg_textile", "mfg_wood", "mfg_printing", "mfg_metal_products", "mfg_other", "construction", "electricity", "gas_heat_water", "ict",
                      "transport_postal", "wholesale", "retail", "realestate", "goods_leasing", "advertising", "other_professional", "accommodation_food",
                      "living_amusement", "other_services", "medical_welfare", "staffing", "education", "pure_holding", "agri_forestry_fishery", "mining"]
            big = {"period": "FY2009..FY2024", "id_pattern": "mof.hojin.value_added.{industry}-{size}.fy",
                   "dims": {"industry": leaves, "size": ["cap1b", "cap100m-1b", "cap10m-100m", "capu10m"]}}
            res = await session.call_tool("lookup_panel", big)
            r = _payload(res)
            n_chars = sum(len(getattr(b, "text", "") or "") for b in res.content)
            print(f"[3f] lookup_panel 大パネル: series={r.get('series_count')} count={r.get('count')} text={n_chars:,} 字 quality_groups={len(r.get('quality_groups') or {})}")
            # 基線（2026-08-22 実測：改修前 155,694 字 → 127,656 字）：出典は表ごとの citation_template・品質は quality_groups に 1 回。上限は回帰の物差し
            ok &= (r.get("found") is True and r.get("series_count") == 152 and r.get("count") == 2432 and n_chars < 140_000
                   and all("citation" not in m["source"] for m in r["series"].values()) and bool(r.get("quality_groups")))
            r = _payload(await session.call_tool("lookup_panel", {**big, "verbose": False}))
            print(f"[3g] lookup_panel verbose=false: series 省略={'series' not in r} units={list((r.get('units') or {}).keys())}")
            ok &= "series" not in r and bool(r.get("units")) and bool(r.get("values")) and bool(r.get("sources"))
            r = _payload(await session.call_tool("find_statistics", {"query": "固定資本減耗 雇用者報酬 営業余剰", "dataset": "sna2020"}))
            print(f"[3h] find_statistics 複数語 0 件診断: total={r.get('total')} matched_tokens={r.get('matched_tokens')}")
            ok &= r.get("total") == 0 and (r.get("matched_tokens") or {}).get("固定資本減耗", 0) > 0 and "語を減らして" in (r.get("hint") or "")
            r = _payload(await session.call_tool("find_statistics", {"query": "混合所得", "dataset": "hojin"}))
            print(f"[3i] find_statistics 絞り込みで 0 件: matched={r.get('matched_tokens')} unfiltered={r.get('matched_tokens_unfiltered')}")
            ok &= (r.get("total") == 0 and (r.get("matched_tokens_unfiltered") or {}).get("混合所得", 0) > 0 and "絞り込みを外す" in (r.get("hint") or ""))
            r = _payload(await session.call_tool("find_statistics", {"query": "付加価値", "scope": "intl"}))
            r2 = _payload(await session.call_tool("find_statistics", {"query": "付加価値", "scope": "jp"}))
            r3 = _payload(await session.call_tool("list_datasets", {"scope": "intl"}))
            print(f"[3k] scope: intl={r.get('total')} jp={r2.get('total')} datasets(intl)={[d['dataset'] for d in r3.get('datasets') or []]}")
            ok &= (all(x["scope"] == "intl" for x in r["series"]) and all(x["scope"] == "jp" for x in r2["series"]) and r["total"] + r2["total"] > 0
                   and bool(r3.get("datasets")) and all(d["scope"] == "intl" for d in r3["datasets"]))
            r = _payload(await session.call_tool("find_statistics", {"query": "営業余剰"}))
            fam = next((f for f in r.get("families") or [] if f.get("dataset") == "sna_activity"), None)
            bad = [d["slug"] for d in (fam or {}).get("dims", {}).get("industry", []) if d["slug"] == d["label"]]
            print(f"[3j] sna_activity の families 表示名: スラグのまま={len(bad)}")
            ok &= fam is not None and not bad

            r = _payload(await session.call_tool("list_sources", {}))
            print(f"[4] list_sources: {len(r.get('sources') or [])} org")
            ok &= isinstance(r.get("sources"), list)
            r = _payload(await session.call_tool("list_datasets", {}))
            print(f"[4b] list_datasets: {r.get('count')} datasets（値を含まない: {all('value' not in d for d in r.get('datasets') or [])}）")
            ok &= isinstance(r.get("datasets"), list) and (r.get("count") == 0 or all("value" not in d for d in r["datasets"]))

    rows = [json.loads(l) for l in tmp_log.read_text(encoding="utf-8").splitlines() if l.strip()] if tmp_log.exists() else []
    print(f"[5] 捕捉ログ: {len(rows)} 行（found=false の記録: {sum(1 for x in rows if x.get('found') is False)}）")
    ok &= len(rows) >= 2 and any(x.get("found") is False for x in rows)
    # 第 11 弾 第 5 便（2026-09-15）：畳まれた検索は result_count=0 でも family_count／collapsed を持つ（週次レポート・triage が 0 件と誤認しない）
    fam_rows = [x for x in rows if x.get("tool") == "find_statistics" and x.get("family_count")]
    print(f"[5b] 捕捉ログ family_count: 畳み記録 {len(fam_rows)} 行（例 collapsed={fam_rows[0].get('collapsed') if fam_rows else None}）")
    ok &= bool(fam_rows) and all(isinstance(x.get("collapsed"), int) and x["collapsed"] > 0 for x in fam_rows)
    print("=" * 60)
    print("総合: PASS ✅（疎通・fail-closed・層公開固定・stdout クリーン・捕捉配線）" if ok else "総合: FAIL ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
