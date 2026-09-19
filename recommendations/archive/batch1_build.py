"""
B3（バッチ1の検証）：policy_docs_v5 → policy_claims_v6 をローカル Qdrant 内でクローン構築する。

バッチ1（長期開発計画 §7）の3項目をデータに反映した「適用待機」コレクションを作る：

1. **政策主張DBへの改名**：corpus 刻印を `policy_claims` に（コレクション名は policy_claims_v6）。
   サービング文言（MCP 説明文・Web UI）の改名は C2 適用時（本スクリプトはデータのみ）。
2. **gov ファセットの発行体別細分化**：新ファセット `issuer`（発行体名）を**全文書**に付与。
   - org は変えない（正典 eval の expected_org・MCP の orgs 引数・多段 fan-out の互換を維持）
   - gov は file_name 接頭辞で機械判別（cefp/kisei/zaiseishin。カタログ確認済＝8/11/9 の28本）
3. **統計資料の扱い**：据え置き（doc_type=資料 の14本として機械識別可能。統計DB新設時の
   引っ越し候補。データ変更なし＝判断の記録のみ）。

チャンク本文・埋め込み（dense/sparse とも）は v5 と同一＝**再埋め込みなしのクローン**。
BM25 語彙サイドカーも本文同一のためコピーで足りる。

実行（冪等・件数一致ならスキップ）:
    python -m recommendations.ingest.batch1_build            # v5 → policy_claims_v6 クローン＋issuer 付与
    python -m recommendations.ingest.batch1_build --recreate # 作り直し
検証は COLLECTION_NAME=policy_claims_v6 VECTOR_BACKEND=qdrant でゲート4種＋metadata_core 監査。
"""
import argparse
import shutil
import time
from collections import Counter

from qdrant_client import models

from recommendations.core.config import DATA_DIR
from polyarchy_common.logsetup import configure_quiet_logging, get_logger
from recommendations.ingest.qdrant_migrate import ensure_collection

SRC_COLLECTION = "policy_docs_v5"
DST_COLLECTION = "policy_claims_v6"
DST_CORPUS = "policy_claims"  # 政策主張DB（CORPUS_REGISTRY のキーと一致させる）

# 発行体ファセットの対応表と判定は qdrant_ingest と共用（B4 で定義をそちらへ集約。
# 挙動は移動前と同一）。
from recommendations.ingest.qdrant_ingest import issuer_of  # noqa: E402

SCROLL_BATCH = 512

log = get_logger("polyarchy.batch1")


def build(recreate: bool = False) -> None:
    from recommendations.core.qdrant_store import QdrantCorpusStore

    store = QdrantCorpusStore(timeout=300)
    qc = store.qc
    total = qc.count(SRC_COLLECTION, exact=True).count
    if total == 0:
        raise RuntimeError(f"{SRC_COLLECTION} が空です（先に qdrant_migrate）")

    ensure_collection(qc, DST_COLLECTION, recreate)
    qc.create_payload_index(DST_COLLECTION, "issuer", models.PayloadSchemaType.KEYWORD)
    have = qc.count(DST_COLLECTION, exact=True).count
    if have == total:
        log.info("構築済み（%d 件一致）＝何もしません。作り直すには --recreate", total)
        return

    log.info("バッチ1クローン開始: %s(%d件) → %s（再埋め込みなし・issuer 付与・corpus=%s）",
             SRC_COLLECTION, total, DST_COLLECTION, DST_CORPUS)
    t0 = time.time()
    issuers: Counter = Counter()
    done = 0
    offset = None
    while True:
        points, offset = qc.scroll(SRC_COLLECTION, limit=SCROLL_BATCH, offset=offset,
                                   with_payload=True, with_vectors=True)
        buf = []
        for p in points:
            payload = dict(p.payload or {})
            payload["corpus"] = DST_CORPUS          # 改名（バッチ1-1）
            payload["issuer"] = issuer_of(payload)  # 細分化（バッチ1-2）
            issuers[payload["issuer"]] += 1
            buf.append(models.PointStruct(id=str(p.id), vector=p.vector, payload=payload))
        if buf:
            qc.upsert(DST_COLLECTION, points=buf, wait=True)
            done += len(buf)
            if done % 10_000 < SCROLL_BATCH:
                log.info("  %d/%d（%.0f秒）", done, total, time.time() - t0)
        if offset is None:
            break

    have = qc.count(DST_COLLECTION, exact=True).count
    if have != total:
        raise RuntimeError(f"件数不一致: {SRC_COLLECTION} {total} != {DST_COLLECTION} {have}")
    if issuers.get("", 0):
        raise RuntimeError(f"issuer 判別不能が {issuers['']} 件（gov 接頭辞の想定漏れ）")

    # BM25 語彙サイドカー：本文同一＝v5 のものをコピー
    src_vocab = DATA_DIR / "bm25" / f"{SRC_COLLECTION}_vocab.json.gz"
    dst_vocab = DATA_DIR / "bm25" / f"{DST_COLLECTION}_vocab.json.gz"
    shutil.copyfile(src_vocab, dst_vocab)

    log.info("構築完了: %d 件・%.0f秒・語彙コピー済", have, time.time() - t0)
    log.info("issuer 分布: %s", dict(issuers.most_common()))


def main() -> None:
    configure_quiet_logging()
    ap = argparse.ArgumentParser(description="バッチ1コレクションの構築（B3・適用待機）")
    ap.add_argument("--recreate", action="store_true")
    args = ap.parse_args()
    build(recreate=args.recreate)


if __name__ == "__main__":
    main()
