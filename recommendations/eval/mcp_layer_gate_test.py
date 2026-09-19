"""
Phase 10：機密の物理遮断テスト（criterion b）。

「機密チャンクがコレクション内に存在しても、MCP 経路では物理的に返らない」ことを証明する。
prod は機密0件なので、使い捨てコレクションへ 公開/機密 の同一トピック2チャンクを入れて検証する
（機密の本文をより濃く作り、層ゲートが無ければ機密が1位に来る状況＝除外の理由が"層"だけに
なるよう仕込む）。

3段で証明：
  A. サービス経路（＝MCP が使う PolicySearchService.search）→ 機密は絶対に出ない・公開は出る。
  B. 生フィルタ（SearchFilter(layers=("機密",))）→ 機密が取得できる＝**データは在るのに
     境界が遮断している**（機密が無いから出ないのではない）ことを示す。
  C. 実 MCP プロトコル越し（COLLECTION_NAME=使い捨てで server 起動）→ search_policy_docs は
     機密ゼロ、かつ tool schema に layer 引数が無い（機密を要求する術がない）。

実行：
    python -m recommendations.eval.mcp_layer_gate_test

バッチ2 段4（2026-08-28・Chroma 全廃）：使い捨てコレクションを Qdrant に作る形へ書き換え
（旧実装は Chroma シード＋VECTOR_BACKEND=chroma 強制だった）。層の排他を「実際に使われる
ベクトル経路＝Qdrant」で証明する。BM25 sparse サイドカーの無い一時コレクションは hybrid が
in-memory BM25 にフォールバックする（本線と同じ分岐）。
"""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from polyarchy_common.logsetup import configure_quiet_logging, get_logger, quiet_stdout

configure_quiet_logging()
log = get_logger("polyarchy.gate")

GATE_COLLECTION = "tmp_mcp_layer_gate_test"
QUERY = "賃金と雇用の政策について"
PUB_FILE = "public_dummy.txt"
SEC_FILE = "secret_dummy.txt"
HERE = Path(__file__).resolve().parents[2]  # リポジトリ root（python -m recommendations.… の cwd）


def _seed_collection():
    """使い捨て Qdrant コレクションに 公開/機密 の同一トピック2チャンクを入れる（機密を濃く作る）。"""
    from qdrant_client import models as qm

    from recommendations.core.embeddings import production_embed_model
    from recommendations.core.qdrant_store import CORPUS_REGISTRY, DENSE_NAME, QdrantCorpusStore

    with quiet_stdout():
        embed = production_embed_model()
    qstore = QdrantCorpusStore()
    if qstore.qc.collection_exists(GATE_COLLECTION):
        qstore.qc.delete_collection(GATE_COLLECTION)
    # 機密側を "賃金" "雇用" を多く含む濃い本文にして、層ゲートが無ければ機密が上位に来るよう仕込む。
    docs = [
        "公開のダミー政策文書。賃金と雇用について一般的な立場を述べる。",
        "機密のダミー内部文書。賃金と雇用、賃金と雇用、賃金と雇用の政策方針を最も詳細に述べる。",
    ]
    with quiet_stdout():
        embs = [embed.get_text_embedding(t) for t in docs]
    metas = [
        {"file_name": PUB_FILE, "org": "test", "layer": "公開",
         "date": "2026-01-01", "date_int": 20260101, "source_url": "https://example/pub",
         "org_type": "テスト", "title": "公開ダミー", "doc_type": "提言", "field_tags": "労働"},
        {"file_name": SEC_FILE, "org": "test", "layer": "機密",
         "date": "2026-01-02", "date_int": 20260102, "source_url": "",
         "org_type": "テスト", "title": "機密ダミー", "doc_type": "内部", "field_tags": "労働"},
    ]
    ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, s)) for s in ("pub1", "sec1")]
    qstore.qc.create_collection(GATE_COLLECTION, vectors_config={
        DENSE_NAME: qm.VectorParams(size=len(embs[0]), distance=qm.Distance.COSINE)})
    qstore.qc.upsert(GATE_COLLECTION, points=[
        qm.PointStruct(id=i, vector={DENSE_NAME: e}, payload={**m, "text": d})
        for i, e, d, m in zip(ids, embs, docs, metas)], wait=True)
    # in-process（A/B）解決用の登録。子プロセス（C）は COLLECTION_NAME の自己写像で解決する。
    CORPUS_REGISTRY[GATE_COLLECTION] = GATE_COLLECTION
    return qstore, embed


def _drop_collection(qstore):
    from recommendations.core.qdrant_store import CORPUS_REGISTRY
    try:
        qstore.qc.delete_collection(GATE_COLLECTION)
    except Exception:
        pass
    CORPUS_REGISTRY.pop(GATE_COLLECTION, None)


def part_a_service() -> bool:
    """A. サービス経路（MCP が使う本体）で機密が出ないこと。"""
    from recommendations.core.search_api import PolicySearchService

    svc = PolicySearchService(collection_name=GATE_COLLECTION, default_top_k=5)
    res = svc.search(QUERY, top_k=5)
    files = [c.file_name for c in res]
    layers = sorted({c.layer for c in res})
    leak = SEC_FILE in files
    print(f"[A] サービス経路 search(): 取得={files} 層={layers}")
    print(f"    機密混入={'あり!! ❌' if leak else 'なし ✅'} / 公開取得={'あり ✅' if PUB_FILE in files else 'なし ❌'}")
    return (not leak) and (PUB_FILE in files) and (layers == ["公開"])


def part_b_raw_present(embed) -> bool:
    """B. 生フィルタ（機密指定）で機密が取れる＝データは在るのに境界が遮断していることを示す。"""
    from recommendations.core.filters import SearchFilter
    from recommendations.core.hybrid import build_hybrid_retriever

    with quiet_stdout():
        r = build_hybrid_retriever(GATE_COLLECTION, embed, 10, 10, 10,
                                   SearchFilter(layers=("機密",)))
        nodes = r.retrieve(QUERY)
    files = [n.node.metadata.get("file_name") for n in nodes]
    got_secret = SEC_FILE in files
    print(f"[B] 生フィルタ layers=(機密,): 取得={files}")
    print(f"    機密は物理的に存在し取得可能={'はい ✅（＝Aの非取得は"層ゲート"が理由）' if got_secret else 'いいえ ❌'}")
    return got_secret


async def part_c_mcp(col_present: bool) -> bool:
    """C. 実 MCP プロトコル越しに機密ゼロ＋layer 引数の非存在を確認。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # server を使い捨てコレクションに向けて起動（COLLECTION_NAME 環境変数）。
    # 子プロセスの CORPUS_REGISTRY は COLLECTION_NAME の自己写像で GATE_COLLECTION を解決する。
    env = {**os.environ, "COLLECTION_NAME": GATE_COLLECTION, "VECTOR_BACKEND": "qdrant"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "recommendations.serving.mcp_server"],
                                   cwd=str(HERE), env=env)
    ok = True
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            for tool_name in ("search_policy_docs", "sweep_policy_docs"):
                t = next(t for t in tools.tools if t.name == tool_name)
                props = list((t.inputSchema or {}).get("properties", {}))
                has_layer = any("layer" in p.lower() for p in props)
                print(f"[C] {tool_name} schema 引数={props}  layer 引数={'あり ❌' if has_layer else 'なし ✅'}")
                ok = ok and not has_layer

            r = await session.call_tool("search_policy_docs", {"query": QUERY, "top_k": 5})
            sc = getattr(r, "structuredContent", None) or {}
            if not sc:
                for b in getattr(r, "content", []) or []:
                    if getattr(b, "text", None):
                        sc = json.loads(b.text)
                        break
            files = [c["file_name"] for c in sc.get("results", [])]
            layers = sorted({c["layer"] for c in sc.get("results", [])})
            leak = SEC_FILE in files
            print(f"[C] MCP search_policy_docs: 取得={files} 層={layers} 機密混入={'あり!! ❌' if leak else 'なし ✅'}")
            ok = ok and not leak and layers in ([], ["公開"])
    return ok


async def main() -> int:
    print("=== 機密の物理遮断テスト（criterion b）===")
    qstore, embed = _seed_collection()
    try:
        a = part_a_service()
        b = part_b_raw_present(embed)
        c = await part_c_mcp(b)
    finally:
        _drop_collection(qstore)
    print("=" * 64)
    verdict = a and b and c
    print(f"A(サービスで機密非取得)={'✅' if a else '❌'}  "
          f"B(生フィルタで機密は存在)={'✅' if b else '❌'}  "
          f"C(MCPで機密ゼロ＋layer無)={'✅' if c else '❌'}")
    print(f"総合: {'PASS ✅ 機密は外部経路で物理的に返らない' if verdict else 'FAIL ❌'}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
