"""
Phase 7.5：比較・横断型クエリのマルチクエリ分解（団体別検索→候補合流）の検出部。

比較型（「AとBはそれぞれ〜か」）は単一クエリだと融合プールが片方の団体に占有され、
もう片方の正解文書がプール外や深部に沈む（§20.4 の comparative MRR 0.483 の主因）。
クエリ中の団体言及を検出し、団体ごとに org フィルタ付きで検索して候補を均等に合流する
（実行部は recommendations/core/hybrid.py の HybridRetriever）。ルールベース検出で LLM 不要＝
ローカル/無料/プライベート要件を維持する。
"""

from recommendations.core.orgs import ORG_MENTIONS  # 団体コード → クエリ中の言及語（1 表＝recommendations.core.orgs）

# 検出は全団体の言及語を長い順にマスクしながら行うので、
# 「日本経済団体連合会」の中の「連合」のような包含誤検出は起きない。

# 比較・対比の意図を示す表現。「団体2つ以上」だけだと「経団連は政府に何を求めるか」の
# ような単一意図の設問まで分解してしまうため、これらのどれかを併せて要求する。
COMPARATIVE_MARKERS = ("それぞれ", "異なる", "対立", "比較", "違い", "双方", "両者", "対比")


def detect_comparative_orgs(query: str, max_orgs: int = 3) -> tuple[str, ...]:
    """比較型クエリなら言及団体のコード列（出現順）を、そうでなければ空タプルを返す。"""
    if not any(m in query for m in COMPARATIVE_MARKERS):
        return ()
    pairs = [(org, kw) for org, kws in ORG_MENTIONS.items() for kw in kws]
    pairs.sort(key=lambda p: len(p[1]), reverse=True)
    masked = query
    first_pos: dict[str, int] = {}
    for org, kw in pairs:
        pos = masked.find(kw)
        if pos < 0:
            continue
        masked = masked.replace(kw, "\x00" * len(kw))
        if org not in first_pos or pos < first_pos[org]:
            first_pos[org] = pos
    if len(first_pos) < 2:
        return ()
    return tuple(sorted(first_pos, key=first_pos.get)[:max_orgs])


def interleave_unique(rankings: list[list[str]], limit: int) -> list[str]:
    """複数の順位リストを先頭から交互に取り（重複除去）、limit 件の合流プールを作る。

    RRF と違い「各リストの上位が必ず入る」ことを保証する＝比較型で両団体の候補枠を確保する。
    """
    out: list[str] = []
    seen: set[str] = set()
    depth = max((len(r) for r in rankings), default=0)
    for i in range(depth):
        for r in rankings:
            if i < len(r) and r[i] not in seen:
                seen.add(r[i])
                out.append(r[i])
                if len(out) >= limit:
                    return out
    return out
