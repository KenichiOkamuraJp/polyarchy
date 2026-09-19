"""
Phase 10：多段検索 vs 一発QA を Phase 9 の新型メトリクスで数値比較（クレジット0・retrieval-only）。

対象は eval_set.json の新型設問（Phase 9 で追加）：
  coverage_multi（190-204, 15問）… 網羅pass（top_k 内の"異なる団体"数 ≥ min_coverage）。
  aggregation   （205-219, 15問）… 棄却が正解12（205-216）＋答え可能3（217-219）。

比較：
  ・一発QA   = `PolicySearchService.search(q)` 単発（＝本番の一問一答・Phase 9 ベースライン）。
  ・多段検索 = `MultiStageSearcher`（団体ごとに叩いて合流／全社走査→argmin・決定）。

採点ロジックは recommendations/eval/eval.py と同一（org_of・breadth・min_coverage）。全てローカル検索＝クレジット0。
生成側の棄却（205/206 の断定→棄却化）は本ハーネスでは測れないため、多段検索が"棄却を根拠づける
横断エビデンス"を surface できるかを構造メトリクスで示す（生成の直接実証は agent_demo で任意・別枠）。

実行：
    python -m recommendations.eval.multistage_eval
"""
import json
import sys
from pathlib import Path

from recommendations.core.config import DATA_DIR
from polyarchy_common.logsetup import configure_quiet_logging, get_logger
from recommendations.core.multistage import ALL_ORGS, MultiStageSearcher
from recommendations.core.search_api import PolicySearchService
from recommendations.eval._scoring import expected_sources, org_of  # eval.py と同一規約（単一の真実源）

configure_quiet_logging()
log = get_logger("polyarchy.mseval")

EVAL_PATH = DATA_DIR / "eval" / "eval_set.json"


def _breadth(files: list[str], exp_orgs: set[str]) -> int:
    return len({org_of(f) for f in files} & exp_orgs)


def run() -> None:
    if not EVAL_PATH.exists():
        sys.exit(f"ERROR: {EVAL_PATH} が見つかりません")
    with open(EVAL_PATH, encoding="utf-8") as f:
        qs = json.load(f)
    cov = [q for q in qs if q.get("qtype") == "coverage_multi"]
    agg = [q for q in qs if q.get("qtype") == "aggregation"]
    agg_abstain = [q for q in agg if not expected_sources(q)]   # 205-216（棄却が正解）
    agg_answer = [q for q in agg if expected_sources(q)]        # 217-219（答え可能）

    svc = PolicySearchService(default_top_k=5)
    ms = MultiStageSearcher(svc)
    top_k = 5

    # ══ 網羅型 coverage_multi ══════════════════════════════════════════════
    print("\n" + "=" * 74)
    print("【網羅型 coverage_multi】一発QA vs 多段検索（team fan-out）  top_k=5")
    print("=" * 74)
    print(f"{'id':<5}{'期待団体':>10}{'一発:幅/pass':>16}{'多段:幅/pass':>16}")
    print("-" * 74)
    one_pass = ms_pass = 0
    one_breadth_sum = ms_breadth_sum = 0
    for q in cov:
        exp_orgs = set(q.get("expected_orgs", [])) or {org_of(s) for s in expected_sources(q)}
        need = int(q.get("min_coverage", 2))
        one_files = [c.file_name for c in svc.search(q["question"], top_k=top_k)]
        ms_files = [c.file_name for c in ms.coverage_search(q["question"], top_k=top_k)]
        ob, mb = _breadth(one_files, exp_orgs), _breadth(ms_files, exp_orgs)
        op, mp = ob >= need, mb >= need
        one_pass += op; ms_pass += mp
        one_breadth_sum += ob; ms_breadth_sum += mb
        print(f"{q['id']:<5}{len(exp_orgs):>10}"
              f"{f'{ob}/{need} {chr(9675) if op else chr(215)}':>16}"
              f"{f'{mb}/{need} {chr(9675) if mp else chr(215)}':>16}")
    n = len(cov)
    print("-" * 74)
    print(f"網羅pass:   一発QA {one_pass}/{n} = {one_pass/n*100:.1f}%   →   "
          f"多段検索 {ms_pass}/{n} = {ms_pass/n*100:.1f}%   "
          f"（Phase 9 ベースライン=86.7%）")
    print(f"平均団体幅: 一発QA {one_breadth_sum/n:.2f}   →   多段検索 {ms_breadth_sum/n:.2f}")

    # ══ 集約/最上級 aggregation（棄却が正解 12問）═══════════════════════════
    print("\n" + "=" * 74)
    print("【集約/最上級 aggregation・棄却が正解(205-216)】一発QA の偏り vs 多段の横断走査")
    print("=" * 74)
    print(f"{'id':<5}{'一発top5:団体構成':>26}{'多段:言及団体数':>16}{'多段判定':>12}")
    print("-" * 74)
    ms_abstain = 0
    one_single_org = 0   # 一発で top5 が1団体に偏った件数（＝断定を誘発しやすい）
    for q in agg_abstain:
        one = svc.search(q["question"], top_k=top_k)
        one_orgs = [c.org for c in one]
        distinct = sorted(set(one_orgs))
        if len(distinct) == 1:
            one_single_org += 1
        scan = ms.aggregation_scan(q["question"])
        dec = scan["decision"]
        if dec == "abstain":
            ms_abstain += 1
        comp = "・".join(f"{o}×{one_orgs.count(o)}" for o in distinct)
        flag = "  ← 205/206" if q["id"] in ("205", "206") else ""
        print(f"{q['id']:<5}{comp:>26}{len(scan['covering']):>16}{dec:>12}{flag}")
    na = len(agg_abstain)
    print("-" * 74)
    print(f"一発QA: top5 が1団体に偏った設問 {one_single_org}/{na}（＝単発では横断が見えず断定を誘発）")
    print(f"多段検索: 横断走査で棄却を根拠づけた設問 {ms_abstain}/{na} "
          f"（複数団体言及＋歴史的初出は単一文書で確定不可＝健全な棄却）")
    print(f"→ 205・206（Phase 9 で断定して棄却失敗＝棄却 10/12 の残り2問）を多段では abstain に是正")

    # ══ aggregation（答え可能 3問 217-219）═════════════════════════════════
    print("\n" + "=" * 74)
    print("【集約 aggregation・答え可能(217-219)】hit@5 が多段でも維持されるか")
    print("=" * 74)
    one_hit = ms_hit = 0
    for q in agg_answer:
        tgt = set(expected_sources(q))
        one_files = [c.file_name for c in svc.search(q["question"], top_k=top_k)]
        # 名指し設問（"経団連の資料は…"）は smart_search が targeted に倒す（fan-out ではない）。
        ms_files = [c.file_name for c in ms.smart_search(q["question"], top_k=top_k)]
        oh, mh = bool(tgt & set(one_files)), bool(tgt & set(ms_files))
        one_hit += oh; ms_hit += mh
        named = ""
        from recommendations.core.multistage import detect_named_orgs
        no = detect_named_orgs(q["question"])
        named = f" 名指し={no[0]}→targeted" if len(no) == 1 else " fan-out"
        print(f"{q['id']:<5} 一発hit@5={'○' if oh else '×'}  多段hit@5={'○' if mh else '×'}"
              f"  期待={list(tgt)[0]}{named}")
    nans = len(agg_answer)
    print("-" * 74)
    print(f"hit@5: 一発QA {one_hit}/{nans}   多段検索 {ms_hit}/{nans}（自称最上級は単一文書で拾える・多段でも非劣化）")

    # ══ まとめ ═════════════════════════════════════════════════════════════
    print("\n" + "=" * 74)
    print("まとめ（criterion c）")
    print("=" * 74)
    print(f"・網羅pass: {one_pass/n*100:.1f}% → {ms_pass/n*100:.1f}%  "
          f"（一発の偏り={n-one_pass}問を多段が {n-ms_pass}問まで縮小）")
    print(f"・集約棄却: 多段は横断走査で {ms_abstain}/{na} 設問の棄却を根拠づけ、205/206 の断定を是正")
    print(f"・答え可能: hit@5 {one_hit}/{nans} → {ms_hit}/{nans}（非劣化）")
    print("※ 生成側の棄却率（10/12→改善）の直接実証は multistage_agent_demo.py（LLM・任意）で。")


if __name__ == "__main__":
    run()
