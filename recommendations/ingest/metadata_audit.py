"""
Phase B（B2）：メタデータ共通コアの機械検証（Qdrant コレクション全点監査）。

**定義は `polyarchy_common.metadata_core` が唯一の場所**（B6 で共通契約へ抽出）。
本モジュールは recommendations のコレクションに対する監査 CLI と、後方互換の再エクスポートのみ。

実行（Qdrant コレクションの全点監査・クレジット0）:
    python -m recommendations.ingest.metadata_audit            # COLLECTION_NAME を監査
"""
import argparse
from collections import Counter

from polyarchy_common.metadata_core import (  # noqa: F401  後方互換の再エクスポート
    ALLOWED_LAYERS,
    COMMON_CORE,
    validate_payload,
)
from polyarchy_common.logsetup import configure_quiet_logging, get_logger
from recommendations.core.config import COLLECTION_NAME

log = get_logger("polyarchy.metadata_core")


def audit(collection: str = COLLECTION_NAME, batch: int = 2_000) -> bool:
    """Qdrant コレクション全点の共通コア適合を監査する。True=全点適合。"""
    from recommendations.core.qdrant_store import QdrantCorpusStore

    store = QdrantCorpusStore(timeout=300)
    n = viol_points = 0
    viol_kinds: Counter = Counter()
    offset = None
    while True:
        points, offset = store.qc.scroll(collection, limit=batch, offset=offset,
                                         with_payload=True, with_vectors=False)
        for p in points:
            n += 1
            errs = validate_payload(dict(p.payload or {}))
            if errs:
                viol_points += 1
                viol_kinds.update(errs)
                if viol_points <= 5:
                    log.warning("違反 id=%s: %s", p.id, "・".join(errs))
        if offset is None:
            break
    ok = viol_points == 0
    log.info("共通コア監査: %s 全%d点・違反%d点 → %s", collection, n, viol_points,
             "適合 ✅" if ok else "不適合")
    for kind, c in viol_kinds.most_common(10):
        log.info("  内訳: %s × %d", kind, c)
    return ok


def main() -> None:
    configure_quiet_logging()
    ap = argparse.ArgumentParser(description="メタデータ共通コアの監査（B2）")
    ap.add_argument("--collection", default=COLLECTION_NAME)
    args = ap.parse_args()
    raise SystemExit(0 if audit(args.collection) else 1)


if __name__ == "__main__":
    main()
