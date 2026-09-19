"""
Phase 13：多段対応 参照 Web UI（Streamlit・限定PoC）。

Phase 8（§24）の一発QA UI を、Phase 10 の多段オーケストレーション（`recommendations.core.multistage`）に
作り替えたもの。設問の形に応じて検索戦略を自動で選ぶ：
  - 単一団体を名指し（"経団連は…"）        → targeted（その団体に絞る）
  - 横断・網羅（"各団体は…"／複数団体名指し）→ coverage（団体別 fan-out・偏り回避）
  - 集約/最上級（"最初に/唯一/最も…"）      → aggregation（全団体走査→argmin か健全な棄却）
  - 上記以外の単一トピック                  → baseline（通常の関連度検索）

設計方針（引き継ぎ §31.7 / ロードマップ Phase 13）：
- 検索は**本番の多段経路をそのまま**呼ぶ：`recommendations.core.multistage.MultiStageSearcher.orchestrate`
  （内部は `recommendations.core.search_api.PolicySearchService`＝ハイブリッド＋比較型マルチクエリ＋
  jp_reranker_xsmall_v1）。UI は検索/生成ロジックを持たず本番モジュールに委譲＝単一の真実源。
- **層＝公開固定＋フェイルクローズ**：検索は `PolicySearchService` を通じて常に公開層で走り、
  境界で非公開を物理的に落とす（機密は UI から広げる術が無い＝多人数化で価値が増す設計）。
  Phase 8 の「機密層 opt-in」は PoC では撤去（公開固定を維持）。
- 生成は集約型のみ決定論（scan の判定＝クレジット0）、それ以外は本番と同一の SYSTEM_PROMPT で
  多段が選んだチャンクを compact 合成（1回だけ・Anthropic）。回答が棄却語を含めば明示する。
- **利用者質問の捕捉（第2チャネル）**：app.py も MCP と同様に実質問を `capture_query(source="app")`
  で永続 JSONL に残す（Phase 12 の器の燃料）。共有層 `search_api` には置かない＝eval 197問の非混入。
- モデル・DB・BM25 索引・リランカーは起動時に1回だけロードしセッションで使い回す（@st.cache_resource）。

起動：
    conda activate polyarchy
    streamlit run recommendations/serving/app.py
※生成（集約型以外）は Anthropic API のクレジットを消費する。動作確認は少数クエリで。
"""
# _ui が TOKENIZERS_PARALLELISM の衛生設定を担う（transformers を引く重い import より前に置く）。
# ※本 UI のプロセス落ちの真因は別で、st.table 経由の pyarrow(mimalloc) がスレッドで segfault する件
# ＝Markdown 表描画で回避済み。
from recommendations.serving._ui import (  # noqa: I001
    ORG_LABELS,
    render_source_meta,
    render_source_text,
    source_headline,
    to_int_date as _to_int_date,
)

import datetime as _dt

import streamlit as st
from llama_index.core import Settings, get_response_synthesizer
from llama_index.core.schema import NodeWithScore, TextNode
from llama_index.llms.anthropic import Anthropic

from recommendations.core.config import (
    ANTHROPIC_API_KEY,
    COLLECTION_NAME,
    HYBRID_SEARCH,
    LLM_MODEL,
    PRODUCTION_RERANKER,
    TOP_K,
)
from recommendations.core.multistage import ALL_ORGS, MultiStageSearcher, StageResult
from recommendations.core.prompts import SYSTEM_PROMPT  # 生成プロンプトは eval と同一物を使う
from recommendations.core.query_capture import capture_query  # Phase 13：app.py 側の捕捉（第2チャネル）
from recommendations.core.search_api import PolicySearchService

# 団体コード → 表示名は recommendations.core.orgs の 1 表（_ui 経由）。
ORG_ORDER = list(ALL_ORGS)  # 多段検索の走査順と同順

# 戦略 → （見出し, 一言説明）。多段が発火したことを利用者に可視化する（PoC の要）。
STRATEGY_INFO = {
    "targeted": ("🎯 指定団体検索", "名指しされた団体に絞って検索しました。"),
    "coverage": ("🌐 網羅検索（団体別 fan-out）",
                 "全団体を個別に検索し、各団体の代表箇所を横断的に集めました"
                 "（単一検索が関連度で1団体に偏るのを回避）。"),
    "aggregation": ("⚖️ 集約走査（全団体を横断）",
                    "全団体を走査し、各社の最古の言及日を突き合わせて判定しました"
                    "（単一文書では最上級を確定できないため、断定せず健全に棄却することがあります）。"),
    "baseline": ("🔎 通常検索", "関連度の高い箇所を横断的に検索しました。"),
}

# 棄却語（生成／集約判定のどちらが出しても検知）。SYSTEM_PROMPT＝「確認できません」、
# 集約判定＝「確定できません」等。
ABSTENTION_MARKS = ("確認できません", "確定できません", "特定できません", "判断できません")


st.set_page_config(page_title="政策文書 参照システム（多段）", page_icon="📑", layout="wide")


# ---------------------------------------------------------------------------
# 起動時1回だけロード（多段検索サービス・LLM・合成器）
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def load_engine():
    """本番の多段検索サービス（層=公開固定）と生成 LLM を常駐化する。

    PolicySearchService が埋め込み(ruri)・Qdrant・ハイブリッド retriever・BM25 sparse・
    リランカーを1回だけ構築する（層は公開固定＋フェイルクローズ）。生成用に本文は全文
    （text_chars=0）で返す。MultiStageSearcher がそれを団体ごとに叩いて orchestrate する。
    """
    # 生成は多段が選んだチャンクを SYSTEM_PROMPT で compact 合成する。retriever とは別に LLM を持つ
    # （PolicySearchService は Settings.llm=None にする＝検索はローカル完結。生成はここだけ）。
    svc = PolicySearchService(text_chars=0)
    searcher = MultiStageSearcher(svc)

    llm = Anthropic(model=LLM_MODEL, api_key=ANTHROPIC_API_KEY, system_prompt=SYSTEM_PROMPT)
    Settings.llm = llm  # 合成器が拾う既定 LLM
    synthesizer = get_response_synthesizer(llm=llm, response_mode="compact")

    return {
        "searcher": searcher,
        "synthesizer": synthesizer,
        "count": svc.chunk_count,
        "pool_k": svc.pool_k,
    }


def _chunk_to_node(c) -> NodeWithScore:
    """多段の Chunk を生成合成器が食える NodeWithScore に包む（メタも渡す）。"""
    node = TextNode(text=c.text, metadata={
        "file_name": c.file_name, "org": c.org, "org_type": c.org_type,
        "title": c.title, "date": c.date, "date_int": c.date_int,
        "doc_type": c.doc_type, "field_tags": c.field_tags,
        "page_label": c.page, "layer": c.layer, "source_url": c.source_url,
    })
    return NodeWithScore(node=node, score=c.score)


def _generate(synthesizer, question: str, chunks) -> str:
    """多段が選んだチャンクから本番 SYSTEM_PROMPT で回答を合成する（Anthropic・課金）。"""
    nodes = [_chunk_to_node(c) for c in chunks]
    resp = synthesizer.synthesize(question, nodes)
    return str(resp).strip()


# ---------------------------------------------------------------------------
# サイドバー：フィルタ（多段に渡す primitive を組み立てるだけ）
# ---------------------------------------------------------------------------
def sidebar_filter() -> dict:
    st.sidebar.header("絞り込み（任意）")

    picked = st.sidebar.multiselect(
        "団体",
        options=ORG_ORDER,
        format_func=lambda o: ORG_LABELS.get(o, o),
        help="指定した団体だけを対象にします（複数選択可・未指定なら全団体を横断）。"
             "複数選ぶと、その団体群に限定して網羅検索します。",
    )

    since = until = None
    if st.sidebar.checkbox("日付で絞る", value=False):
        c1, c2 = st.sidebar.columns(2)
        d_from = c1.date_input(
            "From", value=_dt.date(2018, 1, 1),
            min_value=_dt.date(2000, 1, 1), max_value=_dt.date(2030, 12, 31),
        )
        d_to = c2.date_input(
            "To", value=_dt.date(2026, 12, 31),
            min_value=_dt.date(2000, 1, 1), max_value=_dt.date(2030, 12, 31),
        )
        since, until = _to_int_date(d_from), _to_int_date(d_to)
        st.sidebar.caption("※日付メタデータを持たない文書は日付指定時に除外されます。")

    field = st.sidebar.text_input(
        "分野タグ（部分一致・任意）", value="",
        placeholder="例：環境 / 労働 / 税制",
        help="分野タグの部分一致で絞ります。付与カバレッジは約半分（best-effort）。",
    ).strip() or None

    return {"orgs_scope": tuple(picked), "since": since, "until": until, "field": field}


def _filter_desc(f: dict) -> str:
    parts = []
    if f["orgs_scope"]:
        parts.append("団体=" + "・".join(ORG_LABELS.get(o, o) for o in f["orgs_scope"]))
    if f["since"] is not None or f["until"] is not None:
        parts.append(f"期間={f['since'] or '-'}〜{f['until'] or '-'}")
    if f["field"]:
        parts.append(f"分野~{f['field']}")
    return "／".join(parts) if parts else "なし（全団体・全期間）"


# ---------------------------------------------------------------------------
# 出典・判定の描画
# ---------------------------------------------------------------------------
def render_sources(chunks) -> None:
    st.subheader(f"参照した出典（{len(chunks)}件）")
    if not chunks:
        st.caption("参照ソースがありません。")
        return
    for c in chunks:
        with st.expander(source_headline(c, c.rank), expanded=(c.rank == 1)):
            render_source_meta(c)
            render_source_text(c, 2000)


def render_aggregation_panel(scan: dict) -> None:
    """集約走査の判定（決定論・クレジット0）を表・根拠つきで示す。"""
    decided = scan["decision"] == "resolve"
    st.markdown("#### ⚖️ 集約走査の判定")
    if decided:
        org = ORG_LABELS.get(scan["answer_org"], scan["answer_org"])
        st.success(f"確定：**{org}**", icon="✅")
    else:
        st.warning("この設問はコーパスからは確定できません（健全な棄却）。", icon="⚖️")
    # 各団体の最古言及テーブル（argmin の根拠）。Markdown で描く＝st.table の pyarrow(mimalloc)
    # 変換が Streamlit スレッドで segfault する既知問題を回避（プロセス落ち防止）。
    lines = ["| 団体 | 言及 | 最古の言及日 | 根拠ファイル |", "|---|---|---|---|"]
    for e in scan["evidence"]:
        org = ORG_LABELS.get(e.org, e.org)
        mention = "あり" if e.covered else "なし"
        date = e.earliest_date or ("日付なし" if e.covered else "—")
        lines.append(f"| {org} | {mention} | {date} | `{e.earliest_file or '—'}` |")
    st.markdown("\n".join(lines))


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main() -> None:
    st.title("📑 政策文書 参照システム（多段検索）")
    st.caption(
        "経団連・政府・連合・日商・経済同友会の公開政策文書を、設問の形に応じて"
        "**多段検索**（団体別 fan-out／全団体走査）し、出典付きで回答します。"
    )

    if not ANTHROPIC_API_KEY:
        st.error("ANTHROPIC_API_KEY が .env に設定されていません。生成を実行できません。")
        st.stop()

    with st.spinner("モデルとインデックスを読み込み中…（初回のみ十数秒）"):
        eng = load_engine()

    f = sidebar_filter()

    st.sidebar.divider()
    st.sidebar.caption(
        f"コレクション: `{COLLECTION_NAME}`（{eng['count']:,} チャンク）\n\n"
        f"検索構成: 多段オーケストレーション ／ hybrid={HYBRID_SEARCH}"
        + (f" / rerank={PRODUCTION_RERANKER}" if PRODUCTION_RERANKER else " / rerank=なし")
        + f"（候補{eng['pool_k']}→top-{TOP_K}）"
    )
    st.sidebar.caption(f"適用フィルタ: {_filter_desc(f)}")
    st.sidebar.success("🔒 層＝**公開固定**（機密は物理遮断・フェイルクローズ）", icon="🔒")
    st.sidebar.info("回答生成（集約型以外）は Anthropic API のクレジットを消費します。", icon="💳")

    question = st.text_input(
        "質問を入力してください",
        placeholder="例：各団体は中小企業の価格転嫁についてどのような立場か？",
    )
    go = st.button("多段検索して回答を生成", type="primary")

    if not (go and question.strip()):
        st.info(
            "設問の形で検索戦略が自動で切り替わります：\n"
            "- 「**各団体は**〜」→ 団体別に横断（網羅）\n"
            "- 「〜を**最初に**提言したのは」→ 全団体走査（確定できなければ健全に棄却）\n"
            "- 「**経団連は**〜」→ その団体に絞る\n"
            "- それ以外 → 関連度で通常検索",
            icon="💡",
        )
        return

    q = question.strip()

    # ① 多段オーケストレーション（検索＝層公開固定・クレジット0）。
    with st.spinner("多段検索中…（団体ごとに検索して統合）"):
        result: StageResult = eng["searcher"].orchestrate(
            q, top_k=TOP_K, per_org=3,
            since=f["since"], until=f["until"], field=f["field"],
            orgs_scope=f["orgs_scope"],
        )

    # ② 生成（集約型は決定論の判定＝クレジット0、それ以外は多段チャンクを合成＝課金）。
    if result.strategy == "aggregation":
        answer = result.scan["rationale"]
    elif result.chunks:
        with st.spinner("回答生成中…（多段が選んだ出典から合成）"):
            answer = _generate(eng["synthesizer"], q, result.chunks)
    else:
        answer = "提供文書からは該当情報を確認できません。"

    # ③ 利用者質問を捕捉（第2チャネル・source=app・fail-open・共有層には置かない）。
    capture_query(
        q,
        orgs=list(f["orgs_scope"]) or None,
        since=f["since"], until=f["until"], field=f["field"], top_k=TOP_K,
        result_count=len(result.chunks),
        top_files=[c.file_name for c in result.chunks],
        top_orgs=[c.org for c in result.chunks],
        source="app",
    )

    # ── 検索戦略の可視化（多段が発火したことを利用者に示す＝PoC の要）。
    label, desc = STRATEGY_INFO.get(result.strategy, ("検索", ""))
    scanned = "・".join(ORG_LABELS.get(o, o) for o in result.scanned_orgs) or "関連度上位"
    st.caption(f"**検索戦略：{label}** — {desc}　走査＝{scanned}")

    # ── 集約型は判定パネル（決定論・根拠テーブル）を先に。
    if result.strategy == "aggregation" and result.scan:
        render_aggregation_panel(result.scan)

    # ── 回答本文＋棄却バナー。
    st.subheader("回答")
    if any(m in answer for m in ABSTENTION_MARKS):
        st.warning(
            "提供文書からは確定できませんでした（該当情報が見つからないか、根拠が不十分です）。",
            icon="⚠️",
        )
    st.markdown(answer)

    # ── 出典。
    render_sources(result.chunks)


if __name__ == "__main__":
    main()
