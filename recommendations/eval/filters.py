"""
構造化フィルタ＋層ゲート eval（`python -m recommendations.eval.eval --filter-eval`＝クレジット0）。

`data/eval/eval_filter.json` の各設問に団体・日付・分野のフィルタを適用し keep/drop を検証し、
合成データで層フィルタの排他（公開既定で機密が出ない）を自己証明する。

バッチ2 段1（2026-08-28・所見 2026-08-19 段 5-1 の決定を実施）：フィルタ設問の測定経路を
本番経路＝`PolicySearchService.search()` に一本化（retriever＋reranker 直組みは削除）。
サービス API は layer 引数を持たない（公開固定＝物理遮断）ため、機密層指定の f08 は設問から外し、
層の排他は下の `_layer_gate_selftest`（retriever 直叩き＝意図的に残す）と
`mcp_layer_gate_test` に一本化した。
"""

import json
import sys

from llama_index.core import Settings

from recommendations.core.config import (
    COLLECTION_NAME,
    HYBRID_SEARCH,
    PRODUCTION_RERANKER,
)
from recommendations.eval._common import FILTER_EVAL_PATH, source_name


def _filter_from_dict(d: dict):
    """eval_filter.json の filter dict を SearchFilter に変換する（表示用・layers=公開固定）。"""
    from recommendations.core.filters import SearchFilter
    return SearchFilter(
        orgs=tuple(d.get("orgs", ())),
        date_from=d.get("date_from"),
        date_to=d.get("date_to"),
        field_tag=d.get("field_tag"),
    )


def _layer_gate_selftest(embed_model, top_k: int) -> bool:
    """合成データで層フィルタの排他を証明する（現状 prod に機密0件のため）。

    使い捨て Qdrant コレクションに 公開/機密 の2チャンクを入れ、(1) 既定（公開のみ）で機密が
    絶対に出ないこと、(2) layers=(機密,) で機密のみ出ることを確認する。True=合格。
    バッチ2 段4（2026-08-28・Chroma 全廃）：Chroma 側の並行シードを削除し Qdrant 一本化
    （層公開固定は全バックエンド共通の不変条件＝実際に使われるベクトル経路で証明する）。
    """
    import uuid

    from qdrant_client import models as qm

    from recommendations.core.filters import SearchFilter
    from recommendations.core.hybrid import build_hybrid_retriever
    from recommendations.core.qdrant_store import CORPUS_REGISTRY, DENSE_NAME, QdrantCorpusStore

    name = "tmp_layer_gate_selftest"
    docs = ["公開のダミー政策文書。賃金と雇用について述べる。",
            "機密のダミー政策文書。賃金と雇用について述べる。"]
    embs = [embed_model.get_text_embedding(t) for t in docs]
    # id は UUID（Qdrant は UUID/整数のみ）。
    ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, s)) for s in ("pub1", "sec1")]
    metas = [
        {"file_name": "pub.txt", "org": "test", "layer": "公開"},
        {"file_name": "sec.txt", "org": "test", "layer": "機密"},
    ]
    qstore = QdrantCorpusStore()
    if qstore.qc.collection_exists(name):
        qstore.qc.delete_collection(name)
    qstore.qc.create_collection(name, vectors_config={DENSE_NAME: qm.VectorParams(
        size=len(embs[0]), distance=qm.Distance.COSINE)})
    qstore.qc.upsert(name, points=[
        qm.PointStruct(id=i, vector={DENSE_NAME: e}, payload={**m, "text": d})
        for i, e, d, m in zip(ids, embs, docs, metas)], wait=True)
    CORPUS_REGISTRY[name] = name  # build_hybrid_retriever の store 生成前に登録が必要
    q = "賃金と雇用について"
    ok = True
    try:
        # (1) 既定（公開のみ）→ 機密は絶対に出ない。
        r = build_hybrid_retriever(name, embed_model, 10, 10, 10, SearchFilter())
        pub_files = [source_name(n) for n in r.retrieve(q)]
        leak = "sec.txt" in pub_files
        print(f"  [層ゲート] 既定(公開のみ) 取得={pub_files} 機密混入={'あり!!' if leak else 'なし'}")
        ok = ok and not leak and "pub.txt" in pub_files
        # (2) 明示的に機密のみ → 機密だけ出る。
        r.set_filter(SearchFilter(layers=("機密",)))
        sec_files = [source_name(n) for n in r.retrieve(q)]
        print(f"  [層ゲート] 機密のみ指定   取得={sec_files}")
        ok = ok and sec_files == ["sec.txt"]
    finally:
        qstore.qc.delete_collection(name)
        CORPUS_REGISTRY.pop(name, None)
    print(f"  [層ゲート] 判定: {'PASS' if ok else 'FAIL'}")
    return ok


def run_filter_eval(top_k: int) -> None:
    """Phase 6：構造化フィルタ検索の評価（検索専用・クレジット0）。

    eval_filter.json の各設問に filter を適用し、keep（絞っても正解が top_k に残る）/
    drop（フィルタで正解が除外される）を検証する。測定経路は本番経路＝
    `PolicySearchService.search(diversify=True)`（MCP／chat_app の既定と同一）。
    加えて合成データで層ゲートを自己検証する。既存 78問（無条件検索前提）とは別ハーネス。
    """
    if not FILTER_EVAL_PATH.exists():
        sys.exit(f"ERROR: フィルタ評価セットが見つかりません: {FILTER_EVAL_PATH}")
    with open(FILTER_EVAL_PATH, encoding="utf-8") as f:
        fset = json.load(f)

    from recommendations.core.search_api import PolicySearchService
    svc = PolicySearchService(default_top_k=top_k, text_chars=0)
    Settings.llm = None
    print(f"[filter-eval] {COLLECTION_NAME}: {svc.chunk_count}チャンク, top_k={top_k}, "
          f"hybrid={HYBRID_SEARCH}, rerank={PRODUCTION_RERANKER} "
          f"[service pool={svc.pool_k} diversify=True]")
    print("=" * 78)
    print(f"{'id':<5}{'mode':<6}{'判定':<6}フィルタ / 期待ソース")
    print("-" * 78)

    passed = 0
    for item in fset:
        sf = _filter_from_dict(item["filter"])
        d = item["filter"]
        got = [c.file_name for c in svc.search(
            item["question"], orgs=d.get("orgs"), since=d.get("date_from"),
            until=d.get("date_to"), field=d.get("field_tag"), top_k=top_k,
            diversify=True)]
        src = item["expected_source"]
        hit = src in got
        if item["mode"] == "keep":
            ok = hit
        else:  # drop
            ok = not hit
            if item.get("expect_empty"):
                ok = ok and len(got) == 0
        passed += ok
        mark = "○" if ok else "×"
        print(f"{item['id']:<5}{item['mode']:<6}{mark:<6}{sf.describe()}  → {src}")
        print(f"      {item.get('desc','')}")
        if not ok:
            print(f"      !! got={got[:5]}")

    print("-" * 78)
    print(f"フィルタ設問: {passed}/{len(fset)} PASS")
    print("\n[層フィルタ自己検証（合成データ）]")
    gate_ok = _layer_gate_selftest(Settings.embed_model, top_k)
    print("=" * 78)
    print(f"総合: フィルタ {passed}/{len(fset)} + 層ゲート {'PASS' if gate_ok else 'FAIL'}")
