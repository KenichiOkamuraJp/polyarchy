"""
同時実行のゲート（2026-10-14・B45 段 3）：ツールを同時に動かしても、1 本ずつ動かしたときと結果が同じ。

待受（`polyarchy_common.mcp_http`）は、本サービスのツールを N 本まで同時に別のスレッドで動かす（N＝unit の
MCP_TOOL_CONCURRENCY）。検索の部品が呼び出しをまたいで状態を共有していると、同時に動いた呼び出しが互いの条件
（団体・期間・分野の絞り込み）や分かち書きの途中の状態を取り違え、黙って別の結果を返す。本ゲートは本番と同じ
ツールの関数（`build_server` が登録したもの）を、評価問から作った呼び出しの列で 2 通りに流して突き合わせる：
  ① 1 本ずつ（順に）② 同時に CONCURRENCY 本（順を混ぜて・スレッドで）
絞り込みの評価問（eval_filter.json＝団体・期間・分野の全部の型）・アンカーの評価問（4 問に 1 問）・団体横断
（網羅と集約の評価問）・重複抑制を混ぜる。突き合わせは返り値の全体（点数は小数 4 桁に丸める＝計算の順の違いで
末尾が揺れることがある）。捕捉ログは一時ファイルへ＝全部の呼び出しの行が欠けず、どの行も 1 行の JSON として読める。

合否は終了コード（release.sh のゲート）：全部の呼び出しが同じ・捕捉ログの行が全部そろうときだけ 0。

実行：
    python -m recommendations.eval.concurrency
"""
import json
import os
import random
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 捕捉ログは一時ファイルへ（本番の燃料を汚さない）。query_capture が import 時に読むので、先に置く。
_TMP_LOG = Path(tempfile.gettempdir()) / "polyarchy_concurrency_qlog.jsonl"
os.environ["POLYARCHY_QUERY_LOG"] = str(_TMP_LOG)

CONCURRENCY = int(os.environ.get("CONCURRENCY_GATE_N", "3"))   # 箱の unit と同じ数（deploy/systemd/polyarchy-mcp.service）


def _round(x):
    if isinstance(x, float):
        return round(x, 4)
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_round(v) for v in x]
    return x


def calls() -> list[tuple[str, dict]]:
    from recommendations.eval._common import FILTER_EVAL_PATH, load_eval_set
    out: list[tuple[str, dict]] = []
    for q in json.loads(FILTER_EVAL_PATH.read_text(encoding="utf-8")):
        f = q["filter"]
        out.append(("search_policy_docs", {"query": q["question"], "orgs": f.get("orgs"), "since": f.get("date_from"),
                                           "until": f.get("date_to"), "field": f.get("field_tag")}))
    qs = load_eval_set("canonical")
    for i, q in enumerate(qs[::4]):
        out.append(("search_policy_docs", {"query": q["question"], "diversify": i % 2 == 0, "top_k": 5 + i % 3 * 5}))
    multi = [q for q in qs if q.get("qtype") in ("coverage_multi", "aggregation")]
    for q in multi[::3]:
        out.append(("sweep_policy_docs", {"query": q["question"]}))
    out.append(("sweep_policy_docs", {"query": multi[0]["question"], "mode": "coverage", "orgs": ["keidanren", "rengo"], "per_org": 2}))
    out.append(("list_orgs", {}))
    return out


def main() -> int:
    from polyarchy_common.logsetup import configure_quiet_logging
    from recommendations.core.search_api import PolicySearchService
    from recommendations.serving.mcp_server import build_server

    configure_quiet_logging()
    _TMP_LOG.unlink(missing_ok=True)
    service = PolicySearchService()
    mcp, _ = build_server(service)
    fns = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
    cs = calls()

    def run(i: int):
        name, args = cs[i]
        return i, _round(json.loads(json.dumps(fns[name](**{k: v for k, v in args.items() if v is not None}),
                                               ensure_ascii=False, default=str)))

    t0 = time.monotonic()
    serial = dict(run(i) for i in range(len(cs)))
    t1 = time.monotonic()
    order = list(range(len(cs)))
    random.Random(20261014).shuffle(order)
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        conc = dict(ex.map(run, order))
    t2 = time.monotonic()

    diff = [i for i in range(len(cs)) if serial[i] != conc[i]]
    lines = _TMP_LOG.read_text(encoding="utf-8").splitlines() if _TMP_LOG.exists() else []
    bad_lines = 0
    for ln in lines:
        try:
            json.loads(ln)
        except json.JSONDecodeError:
            bad_lines += 1
    kinds = {n: sum(1 for c in cs if c[0] == n) for n in fns}
    print(f"[同時実行] 呼び出し {len(cs)} 本（{'・'.join(f'{k} {v}' for k, v in kinds.items() if v)}）"
          f"・1 本ずつ {t1 - t0:.1f} 秒・同時 {CONCURRENCY} 本 {t2 - t1:.1f} 秒")
    print(f"結果の食い違い {len(diff)} 本・捕捉ログ {len(lines)} 行（期待 {2 * len(cs)}）・壊れた行 {bad_lines}")
    for i in diff[:10]:
        print(f"  ✗ {cs[i][0]} {json.dumps(cs[i][1], ensure_ascii=False)[:120]}")
    ok = not diff and len(lines) == 2 * len(cs) and bad_lines == 0
    print("PASS" if ok else "FAIL")
    _TMP_LOG.unlink(missing_ok=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
