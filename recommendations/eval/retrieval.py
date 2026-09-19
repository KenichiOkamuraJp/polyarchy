"""
検索専用 eval（`python -m recommendations.eval.eval --retrieval-only`＝クレジット0・確定的アンカー）。

バッチ2 段1（2026-08-28・所見 2026-08-19 段 5-1 の決定を実施）：測定経路を**利用者が実際に通る
本番経路＝`PolicySearchService.search()`＋diversify=True（MCP／chat_app の既定）**に一本化した。
retriever＋reranker の eval 直組みは削除（層ゲート自己検証だけ retriever 直叩きを残す＝filters.py）。
hit@k / MRR を団体別・設問型別に出し、Phase 9 の網羅（coverage_multi）・集約（aggregation）
メトリクスを併記する。合格ライン（アンカー数値）はルート README「品質の担保」が正典。
"""

import sys

from recommendations.core.config import (
    COLLECTION_NAME,
    HYBRID_SEARCH,
    OPENAI_API_KEY,
    PRODUCTION_RERANKER,
)
from recommendations.core.orgs import ORG_DISPLAY_ORDER
from recommendations.eval._scoring import expected_sources, org_of


def run_retrieval_only(eval_set: list[dict], top_k: int) -> None:
    """検索専用評価（LLM/Anthropic不要＝クレジット0）。

    本番経路＝`PolicySearchService.search(diversify=True)`（ハイブリッド retrieve→rerank→
    文書単位の重複抑制→公開層フェイルクローズ）で、answerable設問の hit@k / MRR をドメイン別に
    出す。生成系①③は測らない（それらは Anthropic API が要る）。rerank/hybrid の高速反復・
    クレジット無しの回帰監視に使う。
    """
    from collections import defaultdict

    if not OPENAI_API_KEY:
        sys.exit("ERROR: OPENAI_API_KEY が .env に設定されていません")

    from recommendations.core.search_api import PolicySearchService
    svc = PolicySearchService(default_top_k=top_k, text_chars=0)
    print(f"[retrieval-only] {COLLECTION_NAME}: {svc.chunk_count}チャンク, top_k={top_k}"
          f", hybrid={HYBRID_SEARCH}, rerank={PRODUCTION_RERANKER}"
          f" [service pool={svc.pool_k} diversify=True]")

    def retrieve_top(question: str) -> list[str]:
        """本番経路（service＋diversify）で top_k のファイル名列を返す。"""
        return [c.file_name for c in svc.search(question, top_k=top_k, diversify=True)]

    by = defaultdict(lambda: {"hit": [], "rr": []})
    byt = defaultdict(lambda: {"hit": [], "rr": []})  # Phase 6.5: 設問型別（qtype）
    misses = []
    cov_rows = []   # Phase 9 coverage_multi（網羅メトリクス）
    agg_rows = []   # Phase 9 aggregation 答え可能分（通常 hit@k）
    agg_abstain = 0  # Phase 9 aggregation のうち棄却が正解＝retrieval採点対象外の件数
    for q in eval_set:
        qt = q.get("qtype", "baseline")
        targets = expected_sources(q)

        # Phase 9【網羅型】: 「横断テーマで異なる“団体”を top_k 内に min_coverage 以上拾えたか」。
        # 知見1（＝関連度勝ちの1団体に偏る）の物差し。primary=団体幅（expected_orgs のうち top_k
        # に現れた数）。exact-file の distinct は付随情報（別ファイルでも同団体を拾えれば網羅は成立）。
        if qt == "coverage_multi":
            got = retrieve_top(q["question"])
            exp_orgs = set(q.get("expected_orgs", [])) or {org_of(s) for s in targets}
            breadth = len({org_of(s) for s in got} & exp_orgs)   # primary: 拾えた団体数
            doc_hits = len({s for s in targets if s in got})     # secondary: 正解ファイル一致数
            need = int(q.get("min_coverage", 2))
            cov_rows.append({
                "id": q["id"], "breadth": breadth, "orgs_total": len(exp_orgs),
                "need": need, "pass": breadth >= need,
                "recall": breadth / len(exp_orgs) if exp_orgs else 0.0,
                "doc_hits": doc_hits, "doc_total": len(targets),
            })
            continue

        # Phase 9【集約/最上級型】: 多くは棄却が正解（生成側の采配＝full evalで棄却採点）。
        # retrieval では測れないので対象外にカウントするだけ。日付確定できる答え可能分のみ hit@k。
        if qt == "aggregation":
            if targets:
                got = retrieve_top(q["question"])
                rank = next((i for i, s in enumerate(got, 1) if s in targets), None)
                agg_rows.append({"id": q["id"], "hit": 1.0 if rank else 0.0,
                                 "rr": 1.0 / rank if rank else 0.0,
                                 "src": targets[0]})
            else:
                agg_abstain += 1
            continue

        if not targets:
            continue  # 棄却設問は検索対象なし
        got = retrieve_top(q["question"])
        rank = next((i for i, s in enumerate(got, 1) if s in targets), None)
        hit, rr = (1.0 if rank else 0.0), (1.0 / rank if rank else 0.0)
        by[q.get("expected_org", "?")]["hit"].append(hit)
        by[q.get("expected_org", "?")]["rr"].append(rr)
        # 既存78問は qtype を持たない＝"baseline"。追加分（101〜）は各設問型。
        byt[qt]["hit"].append(hit)
        byt[qt]["rr"].append(rr)
        if not rank:
            misses.append(f"{q['id']}[{qt}] {targets[0]}: {q['question'][:34]}")

    def m(x):
        return sum(x) / len(x) if x else float("nan")

    allhit = [v for b in by.values() for v in b["hit"]]
    allrr = [v for b in by.values() for v in b["rr"]]
    print(f"\n{'org':12}{'n':>4}{'hit@'+str(top_k):>9}{'MRR':>9}")
    for org in ORG_DISPLAY_ORDER:
        b = by[org]
        print(f"{org:12}{len(b['hit']):>4}{m(b['hit'])*100:>8.1f}%{m(b['rr']):>9.3f}")
    print(f"{'ALL':12}{len(allhit):>4}{m(allhit)*100:>8.1f}%{m(allrr):>9.3f}")
    if len(byt) > 1:
        print(f"\n{'qtype':12}{'n':>4}{'hit@'+str(top_k):>9}{'MRR':>9}")
        for qt in ["baseline", "paraphrase", "temporal", "comparative", "coverage"]:
            if qt in byt:
                b = byt[qt]
                print(f"{qt:12}{len(b['hit']):>4}{m(b['hit'])*100:>8.1f}%{m(b['rr']):>9.3f}")
    if misses:
        print(f"\nミス {len(misses)}問:")
        for line in misses:
            print(f"  {line}")

    # Phase 9【網羅メトリクス】coverage_multi ＝ top_k 内に拾えた「異なる団体」の数（primary）。
    if cov_rows:
        n = len(cov_rows)
        n_pass = sum(r["pass"] for r in cov_rows)
        print(f"\n[網羅メトリクス coverage_multi] top_k={top_k}・"
              f"「横断テーマで異なる団体を N以上」を pass とする（primary=団体幅）")
        print(f"{'id':<5}{'団体幅/need':>12}{'/総団体':>8}{'正解file':>10}{'判定':>6}")
        for r in cov_rows:
            mark = "○" if r["pass"] else "×"
            print(f"{r['id']:<5}{str(r['breadth'])+'/'+str(r['need']):>12}"
                  f"{'/'+str(r['orgs_total']):>8}"
                  f"{str(r['doc_hits'])+'/'+str(r['doc_total']):>10}{mark:>6}")
        print(f"coverage_multi: n={n}  網羅pass {n_pass/n*100:.1f}%  "
              f"平均団体recall {m([r['recall'] for r in cov_rows]):.3f}  "
              f"平均団体幅 {m([r['breadth'] for r in cov_rows]):.2f}")

    # Phase 9【集約/最上級】aggregation ＝ 棄却が正解（retrieval対象外）＋日付確定できる答え可能分。
    if agg_rows or agg_abstain:
        print(f"\n[集約/最上級 aggregation] 棄却が正解（full evalで採点）={agg_abstain}問 / "
              f"答え可能（retrieval採点）={len(agg_rows)}問")
        if agg_rows:
            for r in agg_rows:
                mark = "○" if r["hit"] else "×"
                print(f"  {r['id']}  hit@{top_k} {mark}  MRR {r['rr']:.3f}  {r['src']}")
            print(f"  答え可能 hit@{top_k}: {m([r['hit'] for r in agg_rows])*100:.1f}%  "
                  f"MRR {m([r['rr'] for r in agg_rows]):.3f}")
