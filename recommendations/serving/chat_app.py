"""
Phase 13+：エージェント型 対話UI（会話しながら多段検索）。

`app.py`（一発QA・決定論・クレジット軽）に対し、本ファイルは **Claude 自身に検索ツール
`search_policy_docs` を多段で叩かせる会話UI**。ドッグフーディングで「実用レベル」と評価された
MCP エージェント体験（`multistage_agent_demo.py`）を Streamlit チャットに載せたもの。

設計方針：
- 検索は**ローカル**（`recommendations.core.search_api.PolicySearchService`＝層は**公開固定＋フェイルクローズ**）。
  外に出るのは生成（Anthropic）のみ。機密は物理的にツール経由でも返らない。
- 会話履歴を保持し、追い質問・掘り下げ・横断を Claude が自律的に判断（1ターンに複数回検索）。
- 各**ユーザー発話**を捕捉（`source="app"`）＝Phase 12 の燃料（LLM 内部の検索は捕捉しない）。
- クレジット：1ターンに複数回の LLM／ツール呼び出し（上限つき）。動作確認は少数で。

起動：
    conda activate polyarchy && cd polyarchy
    streamlit run recommendations/serving/chat_app.py --server.headless true   # http://localhost:8502
"""
# _ui が TOKENIZERS_PARALLELISM の衛生設定を担う（transformers を引く重い import より前に置く）。
from recommendations.serving._ui import (  # noqa: I001
    ORG_LABELS,
    render_source_meta,
    render_source_text,
    source_headline,
    to_int_date as _to_int_date,
)

import datetime as _dt
import json

import streamlit as st
from anthropic import Anthropic

from recommendations.core.config import (
    ANTHROPIC_API_KEY,
    COLLECTION_NAME,
    HYBRID_SEARCH,
    LLM_MODEL,
    PRODUCTION_RERANKER,
    TOP_K,
)
from recommendations.core.query_capture import capture_query
from recommendations.core.search_api import Chunk, PolicySearchService

ABSTAIN_MARKS = ("確認できません", "確定できません", "特定できません", "対象外")
MAX_TOOL_CALLS = 12  # 1ユーザー発話あたりのツール呼び出し上限（暴走・課金の歯止め）
MAX_TURNS_CONTEXT = 8  # LLM に渡す直近やり取り数（古い履歴は落として context を抑える）

# ドッグフーディングで良かった多段エージェントのシステムプロンプト（agent_demo と同系・会話版）。
SYSTEM_PROMPT = """あなたは日本の政策文書コーパスを調べるアナリストです。検索ツール
search_policy_docs だけを根拠に、日本語で会話形式で答えます。コーパスは公開文書のみ
（機密は取得不可）で、各団体（経団連/連合/日商/経済同友会）と政府（骨太方針/規制改革/
財政審）の**経済・財政・社会保障の提言が中心**です。政治・外交・憲法などは手薄なことがあります。

進め方（厳守）：
1. 横断・網羅／「各団体は」→ **団体ごとに orgs を1つずつ指定して複数回**検索し、各団体を
   個別に確かめてから統合する（1回の全体検索は関連度で1〜2団体に偏る）。query には具体的な語を
   入れる（**空文字で検索しない**）。
2. **スコアを"無い証拠"として読む**：ある団体の上位が軒並み低スコア（目安0.45未満）でトピックから
   ずれていれば、無理に立場を作らず「本コーパスでは確認できない／対象外」と明言する。
3. **本人の主張と、他者の声の転載を区別する**。引用の前にチャンクの title と doc_type で文書の
   性格を確認する。doc_type=資料（アンケート調査結果・公開質問状への各政党の回答・政党の
   政策比較 等）は**団体自身の提言ではない**：政党回答の転載から引用するときは発言主体
   （どの政党か）を必ず明記し、団体の主張として引用しない。アンケート結果は「会員企業の
   回答集計」であり団体の公式見解と区別する。
4. 集約・最上級（最初に/最も早く/唯一/最も強く）→ 団体ごとに叩いて最古の言及日や有無を
   突き合わせる。単一文書で最上級は確定できないことが多く、複数団体が言及し確定できなければ
   「文書からは確定できません」と**健全に棄却**する（断定しない）。
5. 特定団体を名指しした問いは、その団体に orgs を絞る。
6. **全ての主張に出典（file_name・日付）を付ける**。裏づけの無いことは述べない。日付が空等の
   データ上の限界に気づいたら明記する。
7. 会話の文脈（前の質問・回答）を踏まえて追い質問に答える。フォローアップでも必要な検索を行う。

簡潔で構造化された日本語で、根拠（出典）を添えて答える。挨拶や曖昧な指示には、検索せず
何を調べられるか短く案内してよい。"""

TOOLS = [{
    "name": "search_policy_docs",
    "description": (
        "日本の政策文書コーパス（経団連/政府/連合/日商/経済同友会の公開提言・意見・答申・資料、"
        "約2,600文書/約156,000チャンク）を検索し、関連度順のチャンク（本文抜粋＋出典メタ）を返す。"
        "層は公開固定（機密は返らない）。網羅型は団体ごとに1つずつ orgs を指定して複数回叩くとよい。"
        "doc_type=資料 の文書（アンケート結果・政党回答集等）は団体自身の提言ではない点に注意。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "検索クエリ（日本語・具体語を入れる）"},
            "orgs": {"type": "array", "items": {"type": "string"},
                     "description": "団体コードで絞る（keidanren/gov/rengo/nissho/doyukai）。省略で全団体"},
            "since": {"type": "integer", "description": "この日付以降 YYYYMMDD（例 20240101）"},
            "until": {"type": "integer", "description": "この日付以前 YYYYMMDD"},
            "field": {"type": "string", "description": "分野タグの部分一致（例 労働）"},
            "top_k": {"type": "integer", "description": "返却件数（既定5・上限5。超過は5に制限される）"},
            "diversify": {"type": "boolean",
                          "description": "文書単位の重複抑制（既定 true＝同一文書は最良チャンク1件・"
                                         "他ヒット数は same_doc_hits）。特定文書の深掘り・原文精読で"
                                         "同一文書の複数チャンクが欲しいときだけ false を指定"},
        },
        "required": ["query"],
    },
}]


st.set_page_config(page_title="政策文書 対話システム", page_icon="💬", layout="wide")


@st.cache_resource(show_spinner=False)
def load_service():
    """本番検索サービス（層=公開固定）と Anthropic クライアントを常駐化する。"""
    svc = PolicySearchService(text_chars=1200)  # ツール payload を軽く
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return svc, client


def run_search(svc: PolicySearchService, args: dict, constraints: dict) -> list[Chunk]:
    """ツール呼び出しを本番検索に流す。サイドバーの日付/分野は**ハード制約**として常に適用。

    top_k は上限 TOP_K（既定5）へクランプ（MCP 側と同一規律・制限はツール結果に明示）。
    """
    return svc.search(
        args.get("query", ""),
        orgs=args.get("orgs") or None,
        since=constraints.get("since"),
        until=constraints.get("until"),
        field=constraints.get("field"),
        top_k=max(1, min(int(args.get("top_k") or TOP_K), TOP_K)),
        # dev では既定 on（opt-in が自発されない実測を受けての A/B・mcp_server と同一規律）
        diversify=bool(args.get("diversify", True)),
    )


def _compact(chunks: list[Chunk]) -> str:
    """Claude に返すツール結果を軽量化（本文は短い抜粋に）。

    title・doc_type は**帰属判断の材料として必ず含める**（B4 §9 の帰属事故対策。
    これらを落とすと、システムプロンプトの「title と doc_type で文書の性格を確認」
    規則が実行不能になる＝2026-08-14 の再発の真因）。doc_type=資料 には caution を
    明示的に添える（該当チャンクのみ＝トークン増は僅少）。
    """
    results = []
    for c in chunks:
        r = {
            "file_name": c.file_name, "org": c.org, "date": c.date or "日付なし",
            "title": c.title, "doc_type": c.doc_type or "不明",
            "page": c.page, "score": round(c.score, 3),
            "snippet": (c.text or "")[:300],
        }
        if (c.doc_type or "") == "資料":
            r["caution"] = ("この文書は資料（アンケート結果・政党回答集・調査等）であり"
                            "団体自身の提言ではない。引用する場合は発言主体（どの政党/回答者か）を"
                            "本文から確認し、必ず明記すること")
        if c.same_doc_hits:
            r["same_doc_hits"] = c.same_doc_hits  # diversify 時のみ：同一文書の他ヒット数
        results.append(r)
    return json.dumps({"count": len(chunks), "results": results}, ensure_ascii=False)


def _dedup(chunks: list[Chunk]) -> list[Chunk]:
    """(ファイル, ページ) で重複排除し、スコア降順に（出典表示・捕捉用）。"""
    best: dict[tuple, Chunk] = {}
    for c in chunks:
        k = (c.file_name, c.page)
        if k not in best or c.score > best[k].score:
            best[k] = c
    return sorted(best.values(), key=lambda c: c.score, reverse=True)


def agent_turn(client, svc, api_messages: list, status, constraints: dict) -> tuple[str, list[Chunk]]:
    """Claude に多段検索させて最終回答＋その turn で参照したチャンクを返す。

    api_messages を in-place で伸ばす（assistant の tool_use／user の tool_result／最終 assistant）。
    """
    retrieved: list[Chunk] = []
    calls = 0
    final_text = ""
    for _ in range(MAX_TOOL_CALLS + 2):
        resp = client.messages.create(
            model=LLM_MODEL, max_tokens=3000, system=SYSTEM_PROMPT,
            tools=TOOLS, messages=api_messages)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if text:
            final_text = text
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        # 最終 assistant（tool_use を伴わない）→ 会話継続のため履歴に残して終了
        if resp.stop_reason != "tool_use" or not tool_uses:
            api_messages.append({"role": "assistant", "content": resp.content})
            break
        api_messages.append({"role": "assistant", "content": resp.content})
        results_msg = []
        for tu in tool_uses:
            if calls >= MAX_TOOL_CALLS:
                results_msg.append({"type": "tool_result", "tool_use_id": tu.id,
                                    "content": "（検索回数の上限に達しました。手持ちの結果で答えてください）"})
                continue
            args = tu.input or {}
            org_disp = "・".join(ORG_LABELS.get(o, o) for o in args["orgs"]) if args.get("orgs") else "全団体"
            chunks = run_search(svc, args, constraints)
            retrieved.extend(chunks)
            calls += 1
            status.write(f"🔎 「{args.get('query','')}」（{org_disp}） → {len(chunks)}件")
            results_msg.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": _compact(chunks)})
        api_messages.append({"role": "user", "content": results_msg})
    return final_text or "（回答を生成できませんでした）", _dedup(retrieved)


def render_sources(chunks: list[Chunk]) -> None:
    if not chunks:
        return
    with st.expander(f"参照した出典（{len(chunks)}件）"):
        for i, c in enumerate(chunks, 1):
            st.markdown(source_headline(c, i, with_doc_type=True))
            render_source_meta(c, warn_shiryo=True)
            render_source_text(c, 1200)


def sidebar(eng_count: int) -> dict:
    st.sidebar.header("政策文書 対話システム")
    st.sidebar.caption(
        f"コレクション: `{COLLECTION_NAME}`（{eng_count:,} チャンク）\n\n"
        f"検索: 多段（Claude がツールを自律実行）／hybrid={HYBRID_SEARCH}"
        + (f" / rerank={PRODUCTION_RERANKER}" if PRODUCTION_RERANKER else "")
    )
    st.sidebar.success("🔒 層＝**公開固定**（機密は物理遮断・フェイルクローズ）", icon="🔒")

    st.sidebar.divider()
    st.sidebar.caption("絞り込み（任意・全検索に適用）")
    since = until = field = None
    if st.sidebar.checkbox("日付で絞る", value=False):
        c1, c2 = st.sidebar.columns(2)
        d1 = c1.date_input("From", value=_dt.date(2018, 1, 1),
                           min_value=_dt.date(2000, 1, 1), max_value=_dt.date(2030, 12, 31))
        d2 = c2.date_input("To", value=_dt.date(2026, 12, 31),
                           min_value=_dt.date(2000, 1, 1), max_value=_dt.date(2030, 12, 31))
        since, until = _to_int_date(d1), _to_int_date(d2)
    field = st.sidebar.text_input("分野タグ（部分一致）", value="",
                                  placeholder="例：労働 / 環境 / 税制").strip() or None

    st.sidebar.divider()
    if st.sidebar.button("🗑 会話をリセット", use_container_width=True):
        st.session_state.pop("api_messages", None)
        st.session_state.pop("display", None)
        st.rerun()
    st.sidebar.info("回答は Anthropic API のクレジットを消費します（1発話に複数回の検索・生成）。", icon="💳")
    return {"since": since, "until": until, "field": field}


def main() -> None:
    st.title("💬 政策文書 対話システム")
    st.caption(
        "経団連・政府・連合・日商・経済同友会の公開政策文書について、"
        "**会話しながら多段検索**して出典付きで答えます。追い質問・掘り下げ・横断もどうぞ。"
    )
    if not ANTHROPIC_API_KEY:
        st.error("ANTHROPIC_API_KEY が .env に設定されていません。")
        st.stop()

    with st.spinner("モデルとインデックスを読み込み中…（初回のみ十数秒）"):
        svc, client = load_service()

    constraints = sidebar(svc.chunk_count)

    st.session_state.setdefault("api_messages", [])  # Anthropic 形式（tool 往復込み）
    st.session_state.setdefault("display", [])       # 表示用 [{role, text, chunks}]

    # これまでの会話を再描画
    for m in st.session_state.display:
        with st.chat_message(m["role"]):
            st.markdown(m["text"])
            if m["role"] == "assistant":
                render_sources(m.get("chunks") or [])

    prompt = st.chat_input("質問を入力（例：各団体は中小企業の価格転嫁についてどう述べている？）")
    if not prompt:
        if not st.session_state.display:
            st.info(
                "例）「政府と経団連はスタートアップ支援でどう違う？」→ そのあと「連合はどう？」と"
                "追い質問できます。「〜を最初に提言したのは？」のような最上級は、確定できなければ"
                "健全に棄却します。", icon="💡")
        return

    # ユーザー発話を表示・履歴へ
    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state.display.append({"role": "user", "text": prompt})
    st.session_state.api_messages.append({"role": "user", "content": prompt})

    # 直近 MAX_TURNS_CONTEXT×2 メッセージに絞って context を抑える（古い tool 往復を落とす）
    if len(st.session_state.api_messages) > MAX_TURNS_CONTEXT * 3:
        st.session_state.api_messages = st.session_state.api_messages[-MAX_TURNS_CONTEXT * 3:]

    with st.chat_message("assistant"):
        with st.status("考え中…（多段検索）", expanded=True) as status:
            answer, chunks = agent_turn(client, svc, st.session_state.api_messages, status, constraints)
            status.update(label=f"完了（{len(chunks)}件を参照）", state="complete", expanded=False)
        if any(mk in answer for mk in ABSTAIN_MARKS):
            st.warning("提供文書からは確定できない部分がありました（健全な棄却）。", icon="⚠️")
        st.markdown(answer)
        render_sources(chunks)

    st.session_state.display.append({"role": "assistant", "text": answer, "chunks": chunks})

    # Phase 12【捕捉】：ユーザー発話を1レコード（LLM 内部検索ではなく人の質問を燃料に）。
    capture_query(
        prompt, orgs=None, since=constraints["since"], until=constraints["until"],
        field=constraints["field"], top_k=TOP_K, result_count=len(chunks),
        top_files=[c.file_name for c in chunks], top_orgs=[c.org for c in chunks],
        source="app_chat",
    )


if __name__ == "__main__":
    main()
