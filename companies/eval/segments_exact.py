"""第 1b 便（セグメント別）の原典完全一致ゲート。正例＝区分×要素の値が文字列として一致／負例＝fail-closed。

  python -m companies.eval.segments_exact          # 全問 PASS で exit 0
問＝companies/data/eval/segments.jsonl・segments_fail_closed.jsonl（作り方は make_segment_candidates.py）。

判定に使う参照層の契約（実装は `companies.core.segments`・未実装のあいだは全問 FAIL＝docs/第1b便_計画.md §3）：
  lookup_segments(company: str, period: str, *, basis: str|None = None, doc_id: str|None = None)
    -> {"found": True, "basis": str, "period": str,
        "segments": [{"member": 要素 ID, "label": str, "kind": str}],            # kind＝company_defined／reconciling／corporate／total 等（全種類を kind_note で説明）
        "kind_note": str,
        "facts": [{"member", "element", "label", "section", "value", "unit", "decimals", "context"}],
        "source": {"doc_id", "submitted", "url", "citation", "other_documents"}, "license": {...}}
    -> {"found": False, "reason": str, "quote": str|None, "other_sections": [...], "segments": [...], ...}   # segments＝other_sections に出る区分（found=true と同じ形）
       reason＝no_segment_figures（数値が無い＝単一か省略かは分類せず quote に会社の文 1 行）／not_tagged（米国基準＝注記が XBRL に無い）／
               no_consolidated_statements／out_of_range／bad_period／unknown_company／ambiguous_company
  unlabeled_members() -> list[str]   # 値の置き場の全件で、ラベルが空の区分（0 件であること）
  unlabeled_elements() -> list[str]  # 値の置き場の全件で、ラベルが空の要素（標準要素を含む・0 件であること）
  basis を省いたときは、連結を作成している会社は連結・作成していない会社は単体。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"


def _load(name: str) -> list[dict]:
    return [json.loads(l) for l in (EVAL / name).read_text().splitlines() if l.strip()]


def check_positive(q: dict, lookup) -> str | None:
    e = q["expected"]
    # 書類を固定して引く＝新しい書類を取り込んでも問が動かない
    r = lookup(q["company"]["edinet_code"], q["period"], basis=q["basis"], doc_id=q["doc_id"])
    if not r.get("found"):
        return f"found=false（reason={r.get('reason')}）"
    if r.get("basis") != q["basis"]:
        return f"連結／単体が不一致: {r.get('basis')!r}"
    hits = [f for f in r.get("facts", []) if f.get("member") == e["member"] and f.get("element") == e["element"]
            and f.get("context") == e["context"]]
    if len(hits) != 1:
        return f"区分×要素の値が {len(hits)} 件（1 件のはず）: {e['member']} {e['element']}"
    f = hits[0]
    if f.get("value") != e["value"]:
        return f"値が不一致: {f.get('value')!r} ≠ {e['value']!r}"
    if f.get("section") != e["section"]:
        return f"欄が不一致: {f.get('section')!r} ≠ {e['section']!r}"
    if "element_label" in e and f.get("label") != e["element_label"]:
        return f"要素のラベルが不一致: {f.get('label')!r} ≠ {e['element_label']!r}"
    seg = next((s for s in r.get("segments", []) if s.get("member") == e["member"]), None)
    if seg is None:
        return f"区分の一覧（segments）に {e['member']} が無い"
    if seg.get("kind") != e["member_kind"]:
        return f"区分の種類が不一致: {seg.get('kind')!r} ≠ {e['member_kind']!r}"
    # ラベルは区分の種類を問わず空にしない＝会社が定義した区分は会社のラベル・標準の区分は会社のラベルが無ければタクソノミの標準ラベル
    # （2026-09-23 staging で実測＝標準の区分の label が null だった）
    if not seg.get("label"):
        return f"区分のラベルが空（kind={seg.get('kind')}）"
    if "member_label" in e and seg.get("label") != e["member_label"]:
        return f"区分のラベルが不一致: {seg.get('label')!r} ≠ {e['member_label']!r}"
    # 返した kind はすべて kind_note に説明がある（列挙に無い kind を返さない＝2026-09-23 staging で other_reportable が列挙から漏れていた）
    note = r.get("kind_note") or ""
    undocumented = sorted({s.get("kind") for s in r.get("segments", []) if f"{s.get('kind')}（" not in note})
    if undocumented:
        return f"kind_note に説明の無い kind: {undocumented}"
    if "kind_note_contains" in q and q["kind_note_contains"] not in note:
        return f"kind_note に {q['kind_note_contains']!r} が無い"
    if (r.get("source") or {}).get("doc_id") != q["doc_id"]:
        return f"出所の書類が不一致: {(r.get('source') or {}).get('doc_id')!r}"
    return None


def check_negative(q: dict, lookup) -> str | None:
    who = q["company"]["edinet_code"] or q["company"]["name"]
    r = lookup(who, q["period"], basis=q.get("basis"))
    if r.get("found"):
        return f"値を返した（期待＝not_found・{q['reason']}）"
    if r.get("facts"):
        return "found=false なのに facts がある"
    if r.get("reason") != q["reason"]:
        return f"理由が不一致: {r.get('reason')!r} ≠ {q['reason']!r}"
    if "quote_contains" in q and q["quote_contains"] not in (r.get("quote") or ""):
        return f"会社の文の引用（quote）に {q['quote_contains']!r} が無い: {r.get('quote')!r}"
    if q.get("expect_other_sections"):
        other = r.get("other_sections") or []
        if not other:
            return "タグのある他の欄（other_sections）が無い"
        if any(f.get("section") == "segment_information" for f in other):
            return "other_sections にセグメント情報の注記の値が混ざっている"
    # other_sections の区分にも found=true と同じ形でラベルを返す（2026-09-23 staging で member の ID だけだった）
    segs = {s.get("member"): s for s in r.get("segments") or []}
    bare = sorted({f.get("member") for f in r.get("other_sections") or []
                   if not (segs.get(f.get("member")) or {}).get("label") or not segs[f.get("member")].get("kind")})
    if bare:
        return f"other_sections の区分にラベル・kind が無い: {bare[:3]}"
    for m, lab in (q.get("expect_member_labels") or {}).items():
        if (segs.get(m) or {}).get("label") != lab:
            return f"区分のラベルが不一致: {m} {(segs.get(m) or {}).get('label')!r} ≠ {lab!r}"
    return None


def main() -> int:
    pos, neg = _load("segments.jsonl"), _load("segments_fail_closed.jsonl")
    try:
        from companies.core.segments import lookup_segments as lookup, unlabeled_elements, unlabeled_members
    except ImportError:
        print(f"FAIL: 参照層（companies.core.segments）が未実装＝正例 0/{len(pos)}・負例 0/{len(neg)}")
        return 1
    fails = []
    for qs, check in ((pos, check_positive), (neg, check_negative)):
        for q in qs:
            try:
                err = check(q, lookup)
            except Exception as e:  # 1 問の例外で全体を止めない
                err = f"例外: {e!r}"
            if err:
                fails.append((q["id"], err))
    unlabeled = unlabeled_members()
    for u in unlabeled[:10]:
        print(f"  FAIL ラベルが空の区分: {u}")
    if unlabeled:
        fails.append(("store", f"ラベルが空の区分 {len(unlabeled)} 件"))
    # 要素のラベルも種類を問わず空にしない＝標準要素は公式 CSV の項目名・無ければタクソノミの冗長ラベル（CSV の項目名と同じ文字列）
    # （2026-09-23 staging で実測＝取得済みの CSV に無い標準要素の label が null だった）
    bare = unlabeled_elements()
    for u in bare[:10]:
        print(f"  FAIL ラベルが空の要素: {u}")
    if bare:
        fails.append(("store", f"ラベルが空の要素 {len(bare)} 件"))
    for i, e in fails[:40]:
        print(f"  FAIL {i}: {e}")
    print(f"{'PASS' if not fails else 'FAIL'}: 正例 {len(pos)}・負例 {len(neg)}・失敗 {len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
