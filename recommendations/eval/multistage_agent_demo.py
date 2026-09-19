"""
Phase 10：多段検索のライブ実証（Claude に MCP ツールを実際に叩かせる／任意・クレジット消費）。

`recommendations.serving.mcp_server` を stdio で起動し、Claude（Anthropic API）に `search_policy_docs` を
ツールとして渡して**多段検索**させる。生成側の采配（網羅回答／健全な棄却）が一発QAに対して
改善することを、Phase 9 の失敗ケースで直接示す：
  195/196（網羅・Phase 9 で偏り fail）… 団体ごとに叩いて横断できるか。
  205/206（集約・Phase 9 で断定して棄却 fail）… 全社走査の上で健全に棄却できるか。
  217   （答え可能）… 名指し団体を targeted 検索して正しく答えるか。

■ ローカル完結要件：検索はローカル（MCP サーバ＝公開層固定）。外へ出るのは生成（LLM）のみ。
■ クレジット：LLM を呼ぶため課金される。既定は上記5問・ツール呼び出し上限つき。

実行：
    python -m recommendations.eval.multistage_agent_demo                       # 既定＝失敗5問のショーケース
    python -m recommendations.eval.multistage_agent_demo --ids 205 206         # eval設問を絞って実証
    python -m recommendations.eval.multistage_agent_demo --q "道州制について各団体の立場は？"   # 自由入力（1問）
    python -m recommendations.eval.multistage_agent_demo --q "..." --q "..."   # 自由入力（複数）
    python -m recommendations.eval.multistage_agent_demo -i                    # 対話モード（好きに質問）
    python -m recommendations.eval.multistage_agent_demo --model claude-sonnet-4-6
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from recommendations.core.config import ANTHROPIC_API_KEY, DATA_DIR, LLM_MODEL
from polyarchy_common.logsetup import configure_quiet_logging, get_logger

configure_quiet_logging()
log = get_logger("polyarchy.agent")
HERE = Path(__file__).resolve().parents[2]  # リポジトリ root（python -m recommendations.… の cwd）

# 多段検索を促すシステムプロンプト（根拠規律＋戦略の明示＝ツール契約で縛る）。
SYSTEM_PROMPT = """あなたは日本の政策文書コーパスを調べるアナリストです。検索ツール
search_policy_docs だけを根拠に日本語で答えます。コーパスは公開文書のみ（機密は取得不可）で、
内容は各団体（経団連/連合/日商/経済同友会）と政府（骨太方針/規制改革/財政審）の
**経済・財政・社会保障の提言が中心**です。政治・外交・憲法などのテーマは手薄なことがあります。

進め方（厳守）：
1. 横断・網羅／「各団体は」→ **団体ごとに orgs を1つずつ指定して複数回**検索し、各団体を
   個別に確かめてから統合する（1回の全体検索は関連度で1〜2団体に偏る）。query には必ず
   具体的な語を入れる（**空文字で検索しない**）。
2. **スコアを"無い証拠"として読む**：ある団体の上位結果が軒並み低スコア（目安 0.45 未満）で
   トピックからずれていれば、無理に立場を作らず「本コーパスでは立場を確認できない／対象外」
   と明言する（低スコアの事実も根拠として書く）。弱い一致を立場に膨らませない。
3. **本人の主張と、他者の紹介・調査を区別する**：ある団体が"各政党の比較"や"海外事例"を
   紹介している文書は、その団体自身のスタンスではない。混同せず、その旨を注記する。
4. 集約・最上級（「最初に/最も早く/唯一/最も強く」）→ 団体ごとに叩いて各社の最古の言及日
   （date_int）や有無を突き合わせる。単一文書で最上級は確定できないことが多く、複数団体が
   言及していて確定できない場合は「文書からは確定できません」と**健全に棄却**する（断定しない）。
5. 特定団体を名指しした問いは、その団体に orgs を絞って検索する。
6. 全ての主張に出典（file_name・日付）を付ける。裏づけの無いことは述べない。日付が空
   （date_int が無い）等のデータ上の限界に気づいたら明記する。

まず必要な検索を行い、最後に**団体ごとに立場の有無を明示した表＋要点**で簡潔にまとめる。"""

DEFAULT_IDS = ("195", "196", "205", "206", "217")
MAX_TOOL_CALLS = 15  # 1問あたりのツール呼び出し上限（暴走・課金の歯止め）。5団体+追い検索の余裕。

ABSTAIN_MARKERS = ("確認できません", "確定できません", "特定できません",
                   "判断できません", "明示されていません", "情報はありません")
ORG_NAMES = {"経団連": "keidanren", "日本経済団体連合会": "keidanren",
             "政府": "gov", "連合": "rengo", "日本商工会議所": "nissho",
             "日商": "nissho", "経済同友会": "doyukai", "同友会": "doyukai"}


def _load_questions(ids):
    qs = json.load(open(DATA_DIR / "eval" / "eval_set.json", encoding="utf-8"))
    by_id = {q["id"]: q for q in qs}
    return [by_id[i] for i in ids if i in by_id]


def _compact_tool_result(payload: dict) -> str:
    """Claude に返すツール結果を軽量化（本文は短い抜粋に）。"""
    out = {"count": payload.get("count"), "applied_filter": payload.get("applied_filter"),
           "results": []}
    for c in payload.get("results", []):
        out["results"].append({
            "file_name": c["file_name"], "org": c["org"], "date": c["date"],
            "page": c["page"], "score": c["score"],
            "snippet": (c.get("text") or "")[:280],
        })
    return json.dumps(out, ensure_ascii=False)


def _extract_payload(result) -> dict:
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc
    for b in getattr(result, "content", []) or []:
        if getattr(b, "text", None):
            try:
                return json.loads(b.text)
            except json.JSONDecodeError:
                return {}
    return {}


async def run_one(client, session, tools, model, q) -> dict:
    """1問を多段検索させ、軌跡と最終回答・簡易判定を返す。"""
    print("\n" + "=" * 74)
    print(f"[{q['id']}/{q.get('qtype')}] {q['question']}")
    print("-" * 74)
    messages = [{"role": "user", "content": q["question"]}]
    tool_calls = []
    final_text = ""
    for _turn in range(MAX_TOOL_CALLS + 2):
        resp = await client.messages.create(
            model=model, max_tokens=3000, system=SYSTEM_PROMPT,
            tools=tools, messages=messages)
        assistant_content = resp.content
        # テキスト出力を拾う
        for b in assistant_content:
            if b.type == "text" and b.text.strip():
                final_text = b.text.strip()
        tool_uses = [b for b in assistant_content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_uses:
            break
        messages.append({"role": "assistant", "content": assistant_content})
        results_msg = []
        for tu in tool_uses:
            if len(tool_calls) >= MAX_TOOL_CALLS:
                results_msg.append({"type": "tool_result", "tool_use_id": tu.id,
                                    "content": "（ツール呼び出し上限に達しました。手持ちの結果で結論を述べてください）"})
                continue
            args = tu.input or {}
            org_disp = ("・".join(args["orgs"]) if args.get("orgs") else "全")
            print(f"  🔎 search_policy_docs(query={args.get('query','')!r}, 団体={org_disp}"
                  f"{', since='+str(args['since']) if args.get('since') else ''}"
                  f"{', until='+str(args['until']) if args.get('until') else ''})")
            tool_calls.append(args)
            r = await session.call_tool(tu.name, args)
            payload = _extract_payload(r)
            files = "・".join(f"{c['org']}:{c['file_name']}" for c in payload.get("results", [])[:5])
            print(f"     → {payload.get('count', 0)}件 {files}")
            results_msg.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": _compact_tool_result(payload)})
        messages.append({"role": "user", "content": results_msg})

    print(f"\n  💬 回答: {final_text}")
    # 簡易判定
    orgs_queried = sorted({o for a in tool_calls for o in (a.get("orgs") or [])})
    cited_orgs = sorted({code for name, code in ORG_NAMES.items() if name in final_text})
    abstained = any(m in final_text for m in ABSTAIN_MARKERS)
    judge = {"id": q["id"], "qtype": q.get("qtype"), "n_calls": len(tool_calls),
             "orgs_queried": orgs_queried, "cited_orgs": cited_orgs,
             "abstained": abstained}
    if q.get("qtype") == "coverage_multi":
        exp = set(q.get("expected_orgs", []))
        judge["coverage_hit"] = len(set(cited_orgs) & exp)
        judge["coverage_need"] = int(q.get("min_coverage", 2))
        print(f"  📊 網羅: 引用団体={cited_orgs}（期待{sorted(exp)}）"
              f" 呼び出し{len(tool_calls)}回")
    elif q.get("qtype") == "aggregation":
        if q.get("expected_sources") or q.get("expected_source"):
            print(f"  📊 答え可能: 呼び出し{len(tool_calls)}回 引用={cited_orgs}")
        else:
            print(f"  📊 集約(棄却が正解): 棄却={'○ 健全' if abstained else '× 断定した'}"
                  f" 走査団体={orgs_queried} 呼び出し{len(tool_calls)}回")
    else:  # 自由入力（gold ラベルなし）＝軌跡の要約だけ出す
        print(f"  📊 呼び出し{len(tool_calls)}回 走査団体={orgs_queried or '（名指し/全）'}"
              f" 引用={cited_orgs or '（未特定）'}"
              f"{' 棄却' if abstained else ''}")
    return judge


def _free_item(text: str, i: int) -> dict:
    """自由入力の質問を run_one が食える設問 dict に包む（gold ラベルなし）。"""
    return {"id": f"自由{i}", "qtype": "free", "question": text}


async def _repl(client, session, tools, model) -> None:
    """対話モード：質問を入力→多段検索→回答。空行/Ctrl-D で終了。"""
    loop = asyncio.get_event_loop()
    print("\n[対話モード] 政策文書について質問を入力（空行 / Ctrl-D で終了）")
    n = 0
    while True:
        try:
            text = (await loop.run_in_executor(None, input, "\n質問> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n終了します")
            break
        if not text:
            print("終了します")
            break
        n += 1
        await run_one(client, session, tools, model, _free_item(text, n))


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="多段検索ライブ実証／自由入力（Claude に MCP search_policy_docs を多段で叩かせる）")
    ap.add_argument("--ids", nargs="*", help="eval設問IDで実証（例: --ids 195 205）")
    ap.add_argument("--q", "--question", dest="q", action="append", metavar="質問",
                    help="自由入力の質問（複数可: --q '...' --q '...'）")
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="対話モード（質問を入力→多段検索、空行で終了）")
    ap.add_argument("--model", default=LLM_MODEL)
    a = ap.parse_args()
    if not ANTHROPIC_API_KEY:
        sys.exit("ERROR: ANTHROPIC_API_KEY が .env にありません（生成にのみ使用）")

    from anthropic import AsyncAnthropic
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # モード決定：--q（自由入力）＞ --ids（eval）＞ -i（対話）＞ 既定（ショーケース5問）。
    if a.q:
        questions = [_free_item(t, i) for i, t in enumerate(a.q, 1)]
    elif a.ids:
        questions = _load_questions(a.ids)
    elif a.interactive:
        questions = []
    else:
        questions = _load_questions(list(DEFAULT_IDS))

    print(f"=== 多段検索ライブ実証（model={a.model}）===")
    print("※ 検索はローカル MCP（公開層固定）。外へ出るのは生成のみ。クレジットを消費します。")

    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    params = StdioServerParameters(command=sys.executable, args=["-m", "recommendations.serving.mcp_server"],
                                   cwd=str(HERE))
    judges = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mtools = (await session.list_tools()).tools
            tools = [{"name": t.name, "description": t.description or "",
                      "input_schema": t.inputSchema} for t in mtools]
            for q in questions:
                judges.append(await run_one(client, session, tools, a.model, q))
            if a.interactive:
                await _repl(client, session, tools, a.model)

    # まとめ（eval設問を回したときだけ・自由入力は各問の📊で十分）
    eval_judges = [j for j in judges if j["qtype"] in ("coverage_multi", "aggregation")]
    if eval_judges:
        print("\n" + "=" * 74)
        print("まとめ（ライブ多段検索の采配）")
        print("=" * 74)
        for j in eval_judges:
            if j["qtype"] == "coverage_multi":
                print(f"  {j['id']} 網羅: 引用団体{j.get('coverage_hit')}／"
                      f"need{j.get('coverage_need')} 呼び出し{j['n_calls']}回")
            else:
                tag = ("棄却" + ("○" if j["abstained"] else "×")) if not_answerable(j) else "答え可能"
                print(f"  {j['id']} 集約({tag}): 走査{len(j['orgs_queried'])}団体 呼び出し{j['n_calls']}回")
    return 0


def not_answerable(j) -> bool:
    return j["id"] in ("205", "206", "207", "208", "209", "210",
                       "211", "212", "213", "214", "215", "216")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
