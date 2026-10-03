"""
取り込み：検索の単位（data/cache/units.jsonl）を Qdrant に入れ、BM25 の sparse 重みと語彙を作る。

    python -m deliberations.ingest.qdrant_ingest            # コレクションを作り直して全件（第 1 便は数分）
    DELIB_COLLECTION=deliberations_tmp python -m ...         # 別名のコレクションへ
    python -m deliberations.ingest.qdrant_ingest --payload-only  # メタデータだけ上書き（本文・単位が同じとき。埋め込みはやり直さない）

検索の本文（payload の text＝BM25・リランカーが読む）は、会議体・回次・提出者／発言者の見出しを先頭に付けた文：
「デジタル行財政改革会議 第10回（2025-04-22）中室構成員：…」。問は「第何回の誰が」を含むことが多く、本文だけ
だと回次や発言者の語が当たらないため。利用者に返すのは見出しの無い本文（payload の body）。
単位 ID（build.py の uuid5）をそのまま点の ID にする＝取り込み直しても同じ ID。
"""
import json
import sys
import time

from qdrant_client import QdrantClient, models

from deliberations.core import config
from deliberations.core.paths import UNITS
from polyarchy_common.logsetup import configure_quiet_logging, get_logger
from polyarchy_common.metadata_core import validate_payload
from polyarchy_retrieval.bm25 import build_sparse_bm25
from polyarchy_retrieval.models import HF_EMBEDDING_MODELS
from polyarchy_retrieval.qdrant import DENSE_NAME, QdrantCorpusStore, ensure_collection

log = get_logger("polyarchy.deliberations.ingest")
BATCH = 32


def heading(u: dict) -> str:
    who = u["speaker"] if u["unit"] == "utterance" else u["presenter"]
    where = f"{u['org_name']} 第{u['session_no']}回（{u['date'] or '日付なし'}）"
    if u["unit"] == "page":
        mat = " ".join(x for x in (u["material_no"], u["title"].split("）", 1)[-1]) if x)
        return f"{where}{mat} p{u['page']}・{who}："
    return f"{where}{u['doc_kind']}・{who}："


def payload(u: dict) -> dict:
    p = {k: v for k, v in u.items() if k not in ("id", "text")}
    p["role"] = u["speaker_role"] if u["unit"] == "utterance" else u["presenter_type"]
    p["who"] = u["speaker"] if u["unit"] == "utterance" else u["presenter"]
    p["body"] = u["text"]
    p["text"] = heading(u) + u["text"]
    return p


def _scroll(qc: QdrantClient):
    off = None
    while True:
        pts, off = qc.scroll(config.COLLECTION, limit=2000, offset=off, with_payload=["text"], with_vectors=False)
        yield from pts
        if off is None:
            break


def main() -> int:
    configure_quiet_logging()
    units = [json.loads(l) for l in UNITS.open(encoding="utf-8")]
    keep = [u for u in units if u["chars"] >= config.MIN_UNIT_CHARS or u["unit"] == "page"]
    log.info("単位 %d（短い発言を外して %d）→ %s", len(units), len(keep), config.COLLECTION)
    errs = [(u["id"], e) for u in keep for e in validate_payload(payload(u))]
    if errs:
        log.error("共通コアの違反 %d 件: %s", len(errs), errs[:3])
        return 1
    qc = QdrantClient(url=config.QDRANT_URL, timeout=300)
    if "--payload-only" in sys.argv:
        have = {str(p.id): p.payload.get("text") for p in _scroll(qc)}
        if set(have) != {u["id"] for u in keep}:
            log.error("索引の点と単位の集合が違う（単位が変わった）＝--payload-only ではなく作り直す")
            return 1
        changed = [u for u in keep if have[u["id"]] != payload(u)["text"]]
        if changed:
            log.error("検索の本文が %d 件変わっている＝埋め込みのやり直しが要る（--payload-only は使えない）", len(changed))
            return 1
        for i in range(0, len(keep), 256):
            for u in keep[i:i + 256]:
                qc.overwrite_payload(config.COLLECTION, payload=payload(u), points=[u["id"]], wait=False)
        qc.update_collection(config.COLLECTION)  # 書き込みを確定
        log.info("メタデータを上書き: %d 点", len(keep))
        return 0
    ensure_collection(qc, config.COLLECTION, recreate=True, dense_dim=config.DENSE_DIM,
                      keyword_fields=config.KEYWORD_FIELDS, integer_fields=config.INTEGER_FIELDS)
    embed = HF_EMBEDDING_MODELS[config.EMBEDDING]()
    keep.sort(key=lambda u: len(u["text"]))  # 長さの近いもの同士で束ねる（埋め込みの詰め物を減らす）
    t0 = time.time()
    for i in range(0, len(keep), BATCH):
        chunk = keep[i:i + BATCH]
        pls = [payload(u) for u in chunk]
        vecs = embed.get_text_embedding_batch([p["text"] for p in pls])
        qc.upsert(config.COLLECTION, points=[
            models.PointStruct(id=u["id"], vector={DENSE_NAME: [float(x) for x in v]}, payload=p)
            for u, v, p in zip(chunk, vecs, pls)], wait=True)
        if (i // BATCH) % 20 == 0:
            log.info("  %d/%d（%.0f 秒）", i + len(chunk), len(keep), time.time() - t0)
    store = QdrantCorpusStore({config.COLLECTION: config.COLLECTION}, url=config.QDRANT_URL, timeout=300)
    build_sparse_bm25(store, config.COLLECTION, config.VOCAB_DIR / f"{config.COLLECTION}_vocab.json.gz")
    n = qc.count(config.COLLECTION, exact=True).count
    log.info("取り込み完了: %s に %d 点（%.0f 秒）", config.COLLECTION, n, time.time() - t0)
    return 0 if n == len(keep) else 1


if __name__ == "__main__":
    sys.exit(main())
