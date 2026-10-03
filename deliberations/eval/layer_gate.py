"""
層のゲートと、索引の fail-closed（開発計画 §6）。

    python -m deliberations.eval.layer_gate

1. 索引の全点が公開層（layer≠公開 の点が 0）。
2. 自己試験：一時コレクションに公開の点と、同じ語を含む機密の点を入れ、通常の検索（層の引数なし）に機密が
   出ないこと・層を明示した生のフィルタでは機密が取れること（データは在るのに出ない＝仕組みで遮断）。
3. 共有ライブラリが layer の無い検索を拒むこと。
4. 索引の fail-closed：匿名の要約の単位（mode=anonymous）に発言者が付いていない・区分（role）が不明。
"""
import sys
import uuid

from qdrant_client import QdrantClient, models

from deliberations.core import config
from deliberations.core.search import DeliberationsSearch, DelibFilter
from polyarchy_retrieval.qdrant import DENSE_NAME, QdrantCorpusStore, ensure_collection

TMP = "deliberations_layertest"
PHRASE = "層ゲート自己試験用の特徴語ゼタ"


def main() -> int:
    qc = QdrantClient(url=config.QDRANT_URL, timeout=120)
    bad = []
    nonpub = qc.count(config.COLLECTION, count_filter=models.Filter(must_not=[
        models.FieldCondition(key="layer", match=models.MatchValue(value="公開"))]), exact=True).count
    if nonpub:
        bad.append(f"公開以外の点が {nonpub} 件")
    anon_named = qc.count(config.COLLECTION, count_filter=models.Filter(
        must=[models.FieldCondition(key="mode", match=models.MatchValue(value="anonymous"))],
        must_not=[models.FieldCondition(key="who", match=models.MatchValue(value="不明"))]), exact=True).count
    anon_role = qc.count(config.COLLECTION, count_filter=models.Filter(
        must=[models.FieldCondition(key="mode", match=models.MatchValue(value="anonymous"))],
        must_not=[models.FieldCondition(key="role", match=models.MatchValue(value="不明"))]), exact=True).count
    if anon_named or anon_role:
        bad.append(f"匿名の単位に発言者 {anon_named} 件・区分 {anon_role} 件")

    # 自己試験（一時コレクション）
    src, _ = qc.scroll(config.COLLECTION, limit=6, with_payload=True, with_vectors=[DENSE_NAME])
    ensure_collection(qc, TMP, recreate=True, dense_dim=config.DENSE_DIM,
                      keyword_fields=config.KEYWORD_FIELDS, integer_fields=config.INTEGER_FIELDS)
    pts = []
    for i, p in enumerate(src):
        pl = dict(p.payload)
        layer = "機密" if i < 2 else "公開"
        pl.update(layer=layer, text=f"{PHRASE}（{layer}）" + pl["text"], body=f"{PHRASE}（{layer}）" + pl["body"])
        pts.append(models.PointStruct(id=str(uuid.uuid4()), vector={DENSE_NAME: p.vector[DENSE_NAME]}, payload=pl))
    qc.upsert(TMP, points=pts, wait=True)
    try:
        s = DeliberationsSearch(collection=TMP)
        got = s.search(PHRASE, top_k=10)
        leaked = [h for h in got if h.meta["layer"] != "公開"]
        raw = s.searcher.fused_ids(s.embed.get_query_embedding(PHRASE), PHRASE,
                                   DelibFilter(layers=("機密",)).qdrant(), DelibFilter(layers=("機密",)).matches, False)
        if leaked:
            bad.append(f"通常の検索に機密が {len(leaked)} 件")
        if not raw:
            bad.append("自己試験の前提が崩れている（層を明示しても機密が取れない）")
        print(f"  自己試験：通常の検索 {len(got)} 件（機密 {len(leaked)}）・機密を明示した生のフィルタ {len(raw)} 件")
        try:
            QdrantCorpusStore({TMP: TMP}, url=config.QDRANT_URL).search_dense(
                TMP, pts[0].vector[DENSE_NAME], models.Filter(must=[]), 3)
            bad.append("layer の無い検索が拒まれなかった")
        except ValueError:
            pass
    finally:
        qc.delete_collection(TMP)

    print(f"[層ゲート・索引の fail-closed] {config.COLLECTION}: 取り違え {len(bad)}")
    for b in bad:
        print("  ✗", b)
    print("PASS" if not bad else "FAIL")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
