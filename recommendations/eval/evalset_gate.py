"""決定論的 keyword-in-source ゲート（評価セット拡充用・Phase 6.5 で作成）。

使い方: python -m recommendations.eval.evalset_gate <candidates.json>

candidates.json は eval_set.json と同スキーマのリスト。追加フィールド:
  qtype: "paraphrase" | "temporal" | "comparative" | "coverage"
         | "coverage_multi" | "aggregation" | "abstention"
  expected_sources: 比較・横断型 / coverage_multi 型のみ、複数ファイル名のリスト
                    （expected_source は筆頭を入れる）
  expected_orgs:  coverage_multi 型のみ、横断先の団体（2団体以上・網羅メトリクスの母集団）
  min_coverage:   coverage_multi 型のみ、top_k 内に拾うべき「異なる正解ソース」の下限 N（2以上）
  trap_terms: 棄却型 / aggregation(棄却) 型のみ、コーパスに実在する周辺語彙（トラップの根拠）

チェック内容（全て決定論的・LLM不使用）:
  A. スキーマ: id/question/qtype 必須。答え可能型は expected_keywords 3-4個 + expected_source。
  B. keyword-in-source: 各 expected_keyword が expected_source(s) いずれかの本文
     （normalize後）に実在すること。
  C. paraphrase型: expected_keywords が設問文に一語も現れないこと
     （設問=言い換え・keywords=本文語彙 の設計担保）。
  D. abstention型: expected_source=null, expected_keywords=[] であり、
     trap_terms の各語がコーパスのどこかに実在すること（周辺言及トラップの根拠）。
  E. 全 expected_source(s) が doctext/ に実在すること。id 重複なし（既存 eval_set.json とも衝突なし）。
  F. coverage_multi型（Phase 9・網羅型）: expected_sources を3件以上・2団体以上にまたがせ、
     min_coverage は 2〜len(expected_sources)。expected_orgs はソースの団体集合と一致し2団体以上。
     各 keyword は expected_sources の和集合に実在（union keyword-in-source）。
  G. aggregation型（Phase 9・最上級/集約型）: expected_source=null なら棄却採点＝Dと同じ
     （keywords=[], trap_terms 実在）。expected_source があれば答え可能＝A/B と同じ通常採点。
"""
import json
import sys
from pathlib import Path

from recommendations.eval._scoring import normalize, org_of  # eval.py と同一規約（単一の真実源）

SCRATCH = Path(__file__).resolve().parents[1] / "data" / "eval"
DOCTEXT = SCRATCH / "doctext"
EVAL_SET = Path(__file__).resolve().parents[1] / "data" / "eval" / "eval_set.json"
# Phase 12：ユーザー由来ゴールドの別名前空間。id 衝突検査は正典＋この union で行う
# （未存在なら従来どおり正典のみ＝後方互換）。
USERDERIVED_SET = Path(__file__).resolve().parents[1] / "data" / "eval" / "eval_set_userderived.json"

ANSWERABLE_TYPES = {"paraphrase", "temporal", "comparative", "coverage"}

_doc_cache: dict[str, str] = {}


def doc_norm(fn: str) -> str | None:
    if fn not in _doc_cache:
        p = DOCTEXT / (fn + ".txt")
        _doc_cache[fn] = normalize(p.read_text(encoding="utf-8")) if p.exists() else None
    return _doc_cache[fn]


def main() -> None:
    cands = json.load(open(sys.argv[1], encoding="utf-8"))
    # id 衝突は正典＋ユーザー由来の union で検査（Phase 12・後方互換＝未存在なら正典のみ）。
    existing_ids = {q["id"] for q in json.load(open(EVAL_SET, encoding="utf-8"))}
    if USERDERIVED_SET.exists():
        existing_ids |= {q["id"] for q in json.load(open(USERDERIVED_SET, encoding="utf-8"))}
    corpus = None  # 棄却チェック用の全文（遅延構築）

    seen_ids = set()
    n_fail = 0
    for q in cands:
        errs = []
        qid = q.get("id", "??")
        qtype = q.get("qtype")
        if qid in existing_ids:
            errs.append("id が既存eval（正典＋ユーザー由来）と衝突")
        if qid in seen_ids:
            errs.append("id が候補内で重複")
        seen_ids.add(qid)

        def check_answerable(kws_required=True):
            """A/B: expected_keywords 3-4 が expected_source(s) の和集合に実在。"""
            kws = q.get("expected_keywords") or []
            if kws_required and not (3 <= len(kws) <= 4):
                errs.append(f"expected_keywords は3-4個（現在{len(kws)}）")
            srcs = q.get("expected_sources") or ([q["expected_source"]] if q.get("expected_source") else [])
            if not srcs:
                errs.append("expected_source なし")
            if q.get("expected_sources") and q.get("expected_source") != q["expected_sources"][0]:
                errs.append("expected_source は expected_sources の筆頭と一致させる")
            texts = []
            for s in srcs:
                t = doc_norm(s)
                if t is None:
                    errs.append(f"doctext に無いファイル: {s}")
                else:
                    texts.append(t)
            for kw in kws:
                nk = normalize(kw)
                if texts and not any(nk in t for t in texts):
                    errs.append(f"keyword-in-source 失敗: 「{kw}」")
            return kws

        def check_abstention():
            """D: 棄却採点（expected_source=null, keywords=[], trap_terms 実在）。"""
            nonlocal corpus
            if q.get("expected_source") is not None:
                errs.append("棄却採点型は expected_source=null")
            if q.get("expected_keywords"):
                errs.append("棄却採点型は expected_keywords=[]")
            traps = q.get("trap_terms") or []
            if not traps:
                errs.append("trap_terms なし（周辺言及の根拠を1語以上）")
            else:
                if corpus is None:
                    corpus = "".join(
                        normalize(p.read_text(encoding="utf-8"))
                        for p in DOCTEXT.glob("*.txt"))
                for t in traps:
                    if normalize(t) not in corpus:
                        errs.append(f"trap_terms がコーパスに無い: 「{t}」")

        if qtype in ANSWERABLE_TYPES:
            kws = check_answerable()
            if qtype == "paraphrase":
                nq = normalize(q["question"])
                for kw in kws:
                    if normalize(kw) in nq:
                        errs.append(f"paraphrase違反: 「{kw}」が設問文に含まれる")
        elif qtype == "coverage_multi":
            # F: 網羅型。複数ソース(3+)・2団体以上・min_coverage・expected_orgs 整合。
            check_answerable()
            srcs = q.get("expected_sources") or []
            if len(srcs) < 3:
                errs.append(f"coverage_multi は expected_sources を3件以上（現在{len(srcs)}）")
            src_orgs = {org_of(s) for s in srcs}
            if len(src_orgs) < 2:
                errs.append(f"coverage_multi は2団体以上にまたがること（現在 {sorted(src_orgs)}）")
            mc = q.get("min_coverage")
            if not isinstance(mc, int) or not (2 <= mc <= max(len(srcs), 2)):
                errs.append(f"min_coverage は 2〜{len(srcs)} の整数（現在 {mc}）")
            eorgs = q.get("expected_orgs") or []
            if len(set(eorgs)) < 2:
                errs.append("expected_orgs は2団体以上")
            if set(eorgs) != src_orgs:
                errs.append(f"expected_orgs {sorted(set(eorgs))} が"
                            f"ソースの団体 {sorted(src_orgs)} と不一致")
        elif qtype == "aggregation":
            # G: 集約/最上級。棄却が正解（既定）か、日付確定できる答え可能かで分岐。
            if q.get("expected_source") is None:
                check_abstention()
            else:
                check_answerable()
        elif qtype == "abstention":
            check_abstention()
        else:
            errs.append(f"qtype 不正: {qtype}")

        if errs:
            n_fail += 1
            print(f"FAIL {qid} [{qtype}] {q.get('question','')[:40]}")
            for e in errs:
                print(f"     - {e}")

    n_ok = len(cands) - n_fail
    print(f"\n{n_ok}/{len(cands)} PASS" + ("  ← 全問PASS" if n_fail == 0 else f"  ({n_fail} FAIL)"))
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
