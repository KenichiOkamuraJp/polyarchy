"""
検索のアンカー（開発計画 §6）：M0 で確定したアンカー問の hit@5・MRR。

    python -m deliberations.eval.retrieval                 # 全問
    python -m deliberations.eval.retrieval --dump-topk out.json

正解の判定（M0 の確定）：
- 議事録・議事要旨の問＝返った単位が正解の発言の全体（answer_span）にかかる＝同じファイルで、ページの範囲が
  重なり、根拠の引用（または answer_span の最初の行）の断片を本文に含む。
- スライド・資料の問＝同じファイルの同じページ。
- 初めて出た回の型（D）＝正解の文書（acceptable_paths）のどれか（回と文書。発言者は見ない）。
アンカーは回帰を見つけるためのもの（最大化する KPI ではない＝固定の問に合わせた検索側の調整をしない）。
"""
import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict

from deliberations.core.paths import EVAL_DIR
from deliberations.core.search import DeliberationsSearch


def n(s: str) -> str:
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", s or ""))


def frags(q: dict) -> list[str]:
    out = []
    for ev in q["evidence"]:
        for line in ev["quote"]:
            f = re.sub(r"^[○〇・･]|^\([^()]{1,30}\)", "", n(line))  # デジタル庁の議事要旨の(名前)も外す
            if len(f) >= 12:
                out.append(f[:20])
    sp = q["expected"].get("answer_span")
    if sp:
        f = re.sub(r"^[○〇・･]", "", n(sp["first_line"]))
        f = re.sub(r"^【[^】]*】|^\([^()]{1,30}\)", "", f)
        if len(f) >= 12:
            out.append(f[-15:])
    return out


def is_hit(q: dict, h: dict) -> bool:
    e = q["expected"]
    if "acceptable_paths" in e:
        return h["path"] in {a["path"] for a in e["acceptable_paths"]}
    if h["path"] != e["path"]:
        return False
    sp = e.get("answer_span")
    if sp:
        lo, hi = sp["start_page"], sp["end_page"]
        hlo, hhi = h.get("page") or 0, h.get("end_page") or h.get("page") or 0
        if hhi < lo or hlo > hi:
            return False
        body = n(h.get("body", ""))
        return any(f in body for f in frags(q)) or not frags(q)
    return h.get("page") == e.get("page")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--dump-topk", default=None)
    ap.add_argument("--per-doc", type=int, default=None, help="同じ文書から返す上限（省略＝サービスの既定）")
    args = ap.parse_args()
    qs = json.load(open(EVAL_DIR / "anchor_questions.json", encoding="utf-8"))["questions"]
    svc = DeliberationsSearch()
    by = defaultdict(lambda: {"hit": [], "rr": []})
    misses, dump = [], {}
    for q in qs:
        hits = svc.search(q["question"], top_k=args.top_k,
                          **({} if args.per_doc is None else {"per_doc": args.per_doc}))
        metas = [h.meta for h in hits]
        rank = next((i for i, m in enumerate(metas, 1) if is_hit(q, m)), None)
        t = q["type"][0]
        by[t]["hit"].append(1.0 if rank else 0.0)
        by[t]["rr"].append(1.0 / rank if rank else 0.0)
        dump[q["id"]] = [f"{m['path']}#p{m.get('page')}" for m in metas]
        if not rank:
            misses.append(f"{q['id']}: {q['question'][:40]}")
    allh = [v for b in by.values() for v in b["hit"]]
    allr = [v for b in by.values() for v in b["rr"]]
    m = lambda x: sum(x) / len(x) if x else float("nan")
    print(f"[検索のアンカー] {svc.collection}: {svc.count} 点・top_k={args.top_k}")
    print(f"{'型':6}{'n':>4}{'hit@'+str(args.top_k):>9}{'MRR':>8}")
    for t in sorted(by):
        print(f"{t:6}{len(by[t]['hit']):>4}{m(by[t]['hit'])*100:>8.1f}%{m(by[t]['rr']):>8.3f}")
    print(f"{'ALL':6}{len(allh):>4}{m(allh)*100:>8.1f}%{m(allr):>8.3f}")
    if misses:
        print(f"ミス {len(misses)} 問:")
        for x in misses:
            print("  " + x)
    if args.dump_topk:
        json.dump(dump, open(args.dump_topk, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
