"""
生成込み eval（`python -m recommendations.eval.eval` の既定＝クレジット消費）。

評価セット全問を本番相当のクエリエンジン（ハイブリッド→リランカー→Anthropic 生成）で回し、
① キーワード再現率 ② 検索ヒット率/MRR ③ 棄却正解率 ④ 網羅（coverage_multi）を採点して
`data/eval/results/eval_<stamp>.json` に保存する。検索のみは `retrieval.py`、フィルタ＋層ゲートは
`filters.py`（所見 2026-08-19 段 4：`eval.py` からの機械分割）。
バッチ2 段4（2026-08-28・Chroma 全廃）でコレクション参照を Qdrant 一本化（旧利用元 phase2.py は archive へ）。
"""

import json
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime

from llama_index.core import Settings
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.llms.anthropic import Anthropic

from recommendations.core.config import (
    ANTHROPIC_API_KEY,
    COLLECTION_NAME,
    HYBRID_SEARCH,
    LLM_MODEL,
    OPENAI_API_KEY,
    PRODUCTION_EMBEDDING,
    PRODUCTION_RERANKER,
    RERANK_RETRIEVE_K,
)
from recommendations.core.embeddings import production_embed_model
from recommendations.core.hybrid import production_retriever
from recommendations.core.prompts import SYSTEM_PROMPT
from recommendations.core.rerankers import production_reranker_postprocessor
from recommendations.eval._common import RESULTS_DIR, source_name
from recommendations.eval._scoring import expected_sources, normalize, org_of

# 1問あたりのハード上限（秒）。まれに API 呼び出しが無反応でハングするため、
# シグナルで打ち切ってリトライできるようにする。
QUERY_TIMEOUT_S = 150
QUERY_MAX_ATTEMPTS = 3


class QueryTimeout(Exception):
    pass


@contextmanager
def time_limit(seconds: int):
    """SIGALRM で処理を打ち切る（メインスレッド前提）。"""
    def _handler(signum, frame):
        raise QueryTimeout(f"query exceeded {seconds}s")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# 「文書からは確認できない」と回答したことを示す表現。棄却設問(expected_source=null)の判定に使う。
ABSTENTION_MARKERS = (
    "確認できません",
    "確認できない",
    "記載がありません",
    "記載されていません",
    "情報はありません",
    "見当たりません",
    "含まれていません",
    "言及されていません",
)

# Phase 9 で追加した「失敗クラスの物差し」型。既存の org/qtype 集計（141問の
# レガシー・ベースライン）を不変に保つため、これらは専用メトリクスで別集計する。
NEW_QTYPES = ("coverage_multi", "aggregation")


def build_query_engine(top_k: int, collection_name: str = COLLECTION_NAME, embed_model=None):
    """本番と同じ設定（旧 serving/query.py・archive 済）でクエリエンジンを構築する。

    collection_name を切り替えることで、Phase 2/3 の別コレクションを評価できる。
    embed_model を渡すと検索に使う埋め込みモデルを差し替えられる（Phase 2/3 の比較用）。
    ※コレクションは構築時と同じ埋め込みモデルで検索すること（空間が異なると無意味）。
    省略時は本番の PRODUCTION_EMBEDDING（ruri）。openai-small 系コレクションを評価する
    呼び出し側（phase2.py 等）は embed_model を明示すること。
    """
    if not OPENAI_API_KEY or not ANTHROPIC_API_KEY:
        sys.exit("ERROR: APIキーが .env に設定されていません")

    # 既定は本番埋め込み。OpenAI 埋め込みには timeout を付けてハング対策済み（embeddings.py）、
    # ローカル埋め込みは API を叩かないためハングしない。
    if embed_model is None:
        embed_model = production_embed_model()
    Settings.embed_model = embed_model
    Settings.llm = Anthropic(
        model=LLM_MODEL,
        api_key=ANTHROPIC_API_KEY,
        system_prompt=SYSTEM_PROMPT,
        timeout=120.0,
        max_retries=3,
    )

    # バッチ2 段4（2026-08-28・Chroma 全廃）：コレクション参照を Qdrant 一本化
    # （collection はコーパス名の str・index は None＝ハイブリッド経路のみ）。
    from recommendations.core.qdrant_store import QdrantCorpusStore
    n_chunks = QdrantCorpusStore().count(collection_name)
    if n_chunks == 0:
        sys.exit(
            f"ERROR: Qdrant コレクション {collection_name} が空です。先に qdrant_ingest が必要です"
        )
    # Phase 5：本番検索は「ハイブリッド(BM25＋ベクトル→RRF)で候補を広く取り→リランカーで
    # top_k に絞る」。HYBRID_SEARCH/PRODUCTION_RERANKER を無効化すると各段を外せる。
    reranker = production_reranker_postprocessor(top_n=top_k)
    pool_k = max(RERANK_RETRIEVE_K, top_k) if reranker else top_k
    retriever = production_retriever(None, collection_name, embed_model, pool_k)
    note = (f", hybrid={HYBRID_SEARCH}"
            f"{', rerank=' + PRODUCTION_RERANKER if reranker else ''}"
            f"(pool {pool_k})")
    print(
        f"インデックス読み込み完了（{collection_name}: "
        f"{n_chunks}チャンク, top_k={top_k}{note}）"
    )
    return RetrieverQueryEngine.from_args(
        retriever,
        llm=Settings.llm,
        node_postprocessors=[reranker] if reranker else [],
        response_mode="compact",
    )


def run_eval(query_engine, eval_set: list[dict]) -> tuple[list[dict], dict]:
    """評価セット全問を採点し、(個別結果, サマリー) を返す。

    ハング等で1問がタイムアウトした場合、および一時的な API 障害（接続断・レート
    制限・過負荷）の場合は数回リトライする。1問の恒久失敗で全体を落とさない
    （145問の長時間ジョブ中の瞬断でそれまでの生成コストが無駄になるのを防ぐ）。
    """
    import anthropic

    RETRYABLE = (QueryTimeout, anthropic.APIConnectionError,
                 anthropic.RateLimitError, anthropic.InternalServerError)
    results = []
    for i, item in enumerate(eval_set, 1):
        print(f"[{i}/{len(eval_set)}] {item['id']}: {item['question'][:30]}...", flush=True)
        last_err = "timeout"
        for attempt in range(1, QUERY_MAX_ATTEMPTS + 1):
            try:
                results.append(evaluate_one(query_engine, item))
                break
            except RETRYABLE as e:
                last_err = type(e).__name__
                print(f"    {last_err}（{attempt}/{QUERY_MAX_ATTEMPTS}回目）: "
                      f"{item['id']} リトライ", flush=True)
                time.sleep(min(10 * attempt, 30))
        else:
            # 全リトライ失敗。集計から除外されるよう全指標を None にして記録。
            print(f"    !! {item['id']} は {QUERY_MAX_ATTEMPTS} 回とも失敗。スキップして続行",
                  flush=True)
            results.append({
                "id": item["id"], "question": item["question"],
                "qtype": item.get("qtype", "baseline"), "answer": None,
                "expected_keywords": item.get("expected_keywords") or [], "hit_keywords": [],
                "keyword_recall": None, "expected_source": item.get("expected_source"),
                "retrieved_sources": [], "retrieval_hit": None, "reciprocal_rank": None,
                "abstention_correct": None, "coverage": None, "error": last_err,
            })
    return results, summarize(results)


def evaluate_one(query_engine, item: dict) -> dict:
    """1設問を評価して結果 dict を返す。"""
    question = item["question"]
    expected_keywords = item.get("expected_keywords") or []
    expected_source = item.get("expected_source")  # None = 棄却すべき設問
    targets = expected_sources(item)  # 比較・横断型は複数（any-of 採点）

    with time_limit(QUERY_TIMEOUT_S):
        response = query_engine.query(question)
    answer = str(response)
    norm_answer = normalize(answer)

    # ① キーワード再現率
    hit_keywords = [kw for kw in expected_keywords if normalize(kw) in norm_answer]
    keyword_recall = len(hit_keywords) / len(expected_keywords) if expected_keywords else None

    # ② 検索ヒット率（正解PDFが検索結果に含まれるか）＋ MRR（正解の最上位順位の逆数）
    retrieved = [source_name(n) for n in response.source_nodes]
    retrieval_hit = any(s in retrieved for s in targets) if targets else None
    # MRR: 正解PDFが最初に現れる順位 rank（1始まり）の 1/rank。含まれなければ 0。
    # Phase 3 の主指標。生成を挟まない検索専用なので実行ごとのブレが小さい。
    reciprocal_rank = None
    if targets:
        rank = next((i for i, s in enumerate(retrieved, 1) if s in targets), None)
        reciprocal_rank = 1.0 / rank if rank else 0.0

    # ③ 棄却正解率（null 設問で「確認できません」と言えているか）
    abstained = any(marker in answer for marker in ABSTENTION_MARKERS)
    abstention_correct = abstained if expected_source is None else None

    # ④ Phase 9 網羅メトリクス（coverage_multi のみ）: top_k 内に拾えた「異なる団体」の数が
    #    min_coverage 以上か（retrieval-only と同じ団体幅 primary）。生成込みでも網羅を出す。
    coverage = None
    if item.get("qtype") == "coverage_multi":
        exp_orgs = set(item.get("expected_orgs", [])) or {org_of(s) for s in targets}
        breadth = len({org_of(s) for s in retrieved} & exp_orgs)
        need = int(item.get("min_coverage", 2))
        coverage = {
            "breadth": breadth, "orgs_total": len(exp_orgs), "need": need,
            "pass": breadth >= need,
            "doc_hits": len({s for s in targets if s in retrieved}),
        }

    return {
        "id": item["id"],
        "question": question,
        "qtype": item.get("qtype", "baseline"),
        "answer": answer,
        "expected_keywords": expected_keywords,
        "hit_keywords": hit_keywords,
        "keyword_recall": keyword_recall,
        "expected_source": expected_source,
        "retrieved_sources": retrieved,
        "retrieval_hit": retrieval_hit,
        "reciprocal_rank": reciprocal_rank,
        "abstention_correct": abstention_correct,
        "coverage": coverage,
    }


def summarize(results: list[dict]) -> dict:
    """個別結果を集計する。"""
    recalls = [r["keyword_recall"] for r in results if r["keyword_recall"] is not None]
    hits = [r["retrieval_hit"] for r in results if r["retrieval_hit"] is not None]
    rrs = [r["reciprocal_rank"] for r in results if r.get("reciprocal_rank") is not None]
    absts = [r["abstention_correct"] for r in results if r["abstention_correct"] is not None]

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    # Phase 9: 設問型別の kw再現・棄却・網羅 内訳（新型＝新基準を1回で確定するため）。
    by_qtype: dict[str, dict] = {}
    for r in results:
        qt = r.get("qtype", "baseline")
        b = by_qtype.setdefault(qt, {"kw": [], "abst": [], "cov": []})
        if r["keyword_recall"] is not None:
            b["kw"].append(r["keyword_recall"])
        if r["abstention_correct"] is not None:
            b["abst"].append(1.0 if r["abstention_correct"] else 0.0)
        if r.get("coverage") is not None:
            b["cov"].append(1.0 if r["coverage"]["pass"] else 0.0)
    qtype_summary = {
        qt: {
            "n": sum(1 for r in results if r.get("qtype", "baseline") == qt),
            "kw_recall": mean(b["kw"]), "kw_num": len(b["kw"]),
            "abstention": mean(b["abst"]), "abstention_num": len(b["abst"]),
            "coverage_pass": mean(b["cov"]), "coverage_num": len(b["cov"]),
        }
        for qt, b in by_qtype.items()
    }

    return {
        "num_questions": len(results),
        "keyword_recall_mean": mean(recalls),
        "fully_correct": sum(1 for r in results if r["keyword_recall"] == 1.0),
        "retrieval_hit_rate": mean([1.0 if h else 0.0 for h in hits]),
        "retrieval_num": len(hits),
        "mrr": mean(rrs),
        "abstention_accuracy": mean([1.0 if a else 0.0 for a in absts]),
        "abstention_num": len(absts),
        "by_qtype": qtype_summary,
    }


def print_report(results: list[dict], summary: dict) -> None:
    print("\n" + "=" * 72)
    print("個別結果")
    print("=" * 72)
    print(f"{'id':<5}{'kw再現':>8}{'検索':>6}{'棄却':>6}  {'期待ソース':<18}質問")
    print("-" * 72)
    for r in results:
        recall = r["keyword_recall"]
        recall_s = f"{recall*100:5.0f}%" if recall is not None else "   -- "
        hit = r["retrieval_hit"]
        hit_s = " ○ " if hit else ("×" if hit is False else "-")
        abst = r["abstention_correct"]
        abst_s = " ○ " if abst else ("×" if abst is False else "-")
        src = r["expected_source"] or "(棄却)"
        q = r["question"][:24]
        print(f"{r['id']:<5}{recall_s:>8}{hit_s:>6}{abst_s:>6}  {src:<18}{q}")

    print("\n" + "=" * 72)
    print("サマリー")
    print("=" * 72)
    kr = summary["keyword_recall_mean"]
    rh = summary["retrieval_hit_rate"]
    aa = summary["abstention_accuracy"]
    print(f"設問数                    : {summary['num_questions']}")
    print(f"① キーワード再現率(平均)  : {kr*100:.1f}%" if kr is not None else "① キーワード再現率(平均)  : -")
    print(f"   全キーワード的中の設問  : {summary['fully_correct']} / {summary['num_questions']}")
    print(
        f"② 検索ヒット率@k          : {rh*100:.1f}%  ({summary['retrieval_num']}問中)"
        if rh is not None else "② 検索ヒット率@k          : -"
    )
    mrr = summary.get("mrr")
    print(f"   MRR@k                  : {mrr:.3f}" if mrr is not None else "   MRR@k                  : -")
    print(
        f"③ 棄却正解率              : {aa*100:.1f}%  ({summary['abstention_num']}問中)"
        if aa is not None else "③ 棄却正解率              : -"
    )

    # Phase 9: 設問型別の内訳（kw再現／棄却／網羅pass）。新型込みで基準を確定する。
    qs = summary.get("by_qtype") or {}
    if qs:
        print("\n設問型別内訳")
        print(f"{'qtype':<14}{'n':>4}{'kw再現':>9}{'棄却':>9}{'網羅pass':>10}")
        order = ["baseline", "paraphrase", "temporal", "comparative",
                 "coverage", "coverage_multi", "aggregation", "abstention"]
        for qt in order + [k for k in qs if k not in order]:
            if qt not in qs:
                continue
            s = qs[qt]
            kw = f"{s['kw_recall']*100:.1f}%" if s["kw_recall"] is not None else "  -"
            ab = f"{s['abstention']*100:.1f}%" if s["abstention"] is not None else "  -"
            cv = f"{s['coverage_pass']*100:.1f}%" if s["coverage_pass"] is not None else "  -"
            print(f"{qt:<14}{s['n']:>4}{kw:>9}{ab:>9}{cv:>10}")


def run_full(eval_set: list[dict], top_k: int) -> None:
    """全問を生成込みで評価し、レポート表示＋結果 JSON 保存（従来の eval.py main の本体）。"""
    query_engine = build_query_engine(top_k)

    results, summary = run_eval(query_engine, eval_set)
    print_report(results, summary)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"eval_{stamp}.json"
    payload = {
        "timestamp": stamp,
        "config": {
            "embedding_model": PRODUCTION_EMBEDDING,
            "llm_model": LLM_MODEL,
            "top_k": top_k,
            "collection": COLLECTION_NAME,
        },
        "summary": summary,
        "results": results,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存しました: {out_path}")
