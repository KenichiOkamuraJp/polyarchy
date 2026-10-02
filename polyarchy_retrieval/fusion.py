"""順位リストの融合（RRF）と交互合流。どちらも id 列（best-first）だけを扱う。"""


def rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[str]:
    """複数の順位リスト（各々 best-first の id 列）を RRF で融合し、id を降順で返す。

    RRF スコア = Σ 1/(k + rank)。k はスコア平滑化の定数（60 が慣例）。
    順位のみ使うため、ベクトル距離と BM25 スコアのスケール差を気にせず融合できる。
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, _id in enumerate(ranking, 1):
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda i: scores[i], reverse=True)


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
