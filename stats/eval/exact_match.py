"""
原典完全一致ゲート（docs/参照粒度設計.md §9）。実プロトコル（stdio）越しに lookup_statistic を叩く。

- 正例 `stats/data/eval/exact_match.jsonl`：value が expected_value と**文字列として一致**し、
  source.accessor が source_locator の各キーと一致（＝原典の同じセルに戻れる）。
- 負例 `stats/data/eval/fail_closed.jsonl`：found=false かつ reason が期待どおり（近似で何かを返さない）。
- 1件でも外れたら FAIL（終了コード 1）。原典の改定で外れた場合は expected を人手で再確認して更新する（自動更新しない）。

実行（リポジトリ root）：  python -m stats.eval.exact_match
"""
import asyncio
import json

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from stats.core.paths import EVAL_DIR
from stats.eval._client import payload as _payload, server_params


def _load(name: str) -> list[dict]:
    p = EVAL_DIR / name
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]


async def main() -> int:
    pos, neg = _load("exact_match.jsonl"), _load("fail_closed.jsonl")
    if not pos and not neg:
        print("評価セットが空（stats/data/eval/）"); return 1
    params, _tmp_log = server_params("polyarchy_stats_exact_qlog.jsonl")
    print(f"=== 原典完全一致ゲート: 正例 {len(pos)}・負例 {len(neg)} ===")
    fails: list[str] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for c in pos:
                r = _payload(await session.call_tool("lookup_statistic", {"series_id": c["series_id"], "period": c["period"],
                                                                          "region": c.get("region", "JP")}))
                ok = r.get("found") is True and str(r.get("value")) == str(c["expected_value"])
                acc = (r.get("source") or {}).get("accessor") or {}
                loc = c.get("source_locator") or {}
                ok_loc = all(str(acc.get(k)) == str(v) for k, v in loc.items())
                if c.get("expected_kind"):
                    ok_loc = ok_loc and r.get("kind") == c["expected_kind"]
                mark = "✓" if ok and ok_loc else "✗"
                print(f"  {mark} {c['id']}: {c['series_id']} {c['period']} 期待={c['expected_value']} 実際={r.get('value')} found={r.get('found')} locator={'一致' if ok_loc else '不一致'}")
                if not (ok and ok_loc):
                    fails.append(c["id"])
            for c in neg:
                r = _payload(await session.call_tool("lookup_statistic", {"series_id": c["series_id"], "period": c["period"],
                                                                          "region": c.get("region", "JP")}))
                ok = r.get("found") is False and (not c.get("reason") or r.get("reason") == c["reason"])
                print(f"  {'✓' if ok else '✗'} {c['id']}: found={r.get('found')} reason={r.get('reason')}（期待 {c.get('reason')}）")
                if not ok:
                    fails.append(c["id"])
                if "value" in r:
                    print(f"  ✗ {c['id']}: found=false なのに value を含む"); fails.append(c["id"] + ":value")
    print("=" * 60)
    if fails:
        print(f"総合: FAIL ✗ {len(fails)} 件: {fails}")
        return 1
    print(f"総合: PASS ✅（正例 {len(pos)} 完全一致・負例 {len(neg)} fail-closed）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
