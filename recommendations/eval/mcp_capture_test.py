"""Phase 12：捕捉配線テスト（実プロトコル越しに永続 JSONL が育つことの証明）。

`mcp_smoke.py` と同じく `python -m recommendations.serving.mcp_server` を stdio サブプロセスで起動して
ツールを叩く。狙いは1点＝**MCP 捕捉点（search_policy_docs）を通った実クエリが
`data/query_log/queries.jsonl` に永続追記される**ことを、実プロトコル経由で確かめる。

なぜサブプロセスか：稼働中の harness 管理サーバは古いコード（捕捉なし）でホットリロード
できない。**新鮮なサブプロセス**なら新コードで起動するので、捕捉配線をそのまま検証できる。

副次効果：ここで流す3クエリ（通常／団体絞り／0件）はそのまま Phase 12 の「燃料」として
ログに残り、triage→scaffold→gate→append の実証に使える。

実行：
    python -m recommendations.eval.mcp_capture_test
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# テストは**本番クエリログを汚さない**：捕捉先を一時ファイルへ向ける
# （query_capture の import 前に環境変数を設定＝モジュール定数が一時パスを拾う）。
_TEST_LOG = Path(tempfile.gettempdir()) / "polyarchy_capture_test.jsonl"
os.environ["POLYARCHY_QUERY_LOG"] = str(_TEST_LOG)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from recommendations.core.query_capture import QUERY_LOG_PATH, load_queries  # noqa: E402

HERE = Path(__file__).resolve().parents[2]  # リポジトリ root（python -m recommendations.… の cwd）


def _count() -> int:
    return len(load_queries())


def _payload(result) -> dict:
    """CallToolResult から dict ペイロードを取り出す（structuredContent 優先→content 本文）。"""
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
    if _TEST_LOG.exists():
        _TEST_LOG.unlink()  # 決定論のため毎回まっさら（before=0）
    before = _count()
    print("=== Phase 12 捕捉テスト開始（stdio 越しに recommendations.serving.mcp_server へ接続）===")
    print(f"テスト用ログ={QUERY_LOG_PATH}（本番ログは汚しません）  開始時の捕捉件数={before}")

    # 実運用らしい3クエリ（通常／団体絞り／0件＝棄却候補シグナル）。
    sent = [
        {"query": "カーボンニュートラル実現に向けて経団連はどのようなGX投資やエネルギー政策を求めているか",
         "top_k": 5},
        {"query": "中小企業の賃上げ原資を確保するための価格転嫁対策として日商は何を求めているか",
         "orgs": ["nissho"], "top_k": 5},
        {"query": "実在しない未来日付での該当なしを意図的に誘発するクエリ",
         "since": 20990101, "top_k": 5},  # 2099 以降は存在しない→0件（棄却候補）
    ]

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "recommendations.serving.mcp_server"], cwd=str(HERE),
        env={**os.environ})  # POLYARCHY_QUERY_LOG を子プロセスへ引き継ぐ（テスト用ログへ）
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for i, call in enumerate(sent, 1):
                r = await session.call_tool("search_policy_docs", call)
                cnt = _payload(r).get("count")
                print(f"[{i}] 検索 count={cnt}  Q={call['query'][:34]}…")

    after = _count()
    rows = load_queries()
    print(f"終了時の捕捉件数={after}（+{after - before}）")

    # 検証：3件増えている／送ったクエリが全て永続化されている／0件クエリが cnt=0 で残る。
    ok = True
    if after - before < len(sent):
        print(f"FAIL: 捕捉件数が {len(sent)} 件増えていない（+{after - before}）")
        ok = False
    logged_queries = [r.get("query") for r in rows[before:]]
    for call in sent:
        if call["query"] not in logged_queries:
            print(f"FAIL: クエリが永続化されていない: {call['query'][:34]}…")
            ok = False
    zero_rows = [r for r in rows[before:]
                 if r.get("query") == sent[2]["query"]]
    if not zero_rows or zero_rows[-1].get("result_count") != 0:
        print("FAIL: 0件クエリが result_count=0 で残っていない（棄却候補シグナル欠落）")
        ok = False
    else:
        print(f"    0件クエリの記録: result_count={zero_rows[-1]['result_count']}（棄却候補として捕捉）")

    # 記録スキーマの健全性（最新行を1つ表示）。
    if rows:
        print("最新の捕捉レコード:")
        print("  " + json.dumps(rows[-1], ensure_ascii=False))

    print("=" * 60)
    print(f"総合: {'PASS ✅' if ok else 'FAIL ❌'}"
          f"（MCP 捕捉点→永続 JSONL の配線を実プロトコルで確認）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
