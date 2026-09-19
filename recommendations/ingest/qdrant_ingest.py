"""
B4-①：ingest の Qdrant 対応（当時の設計メモは公開リポジトリに含めていない）。

Qdrant コレクションへ doc（file_name）単位の増分 ingest を行う**取込の本線**（バッチ2 段4
〔2026-08-28〕の Chroma 全廃で唯一の取込経路になった）。チャンク化・埋め込みは ingest.py の
共有部（load_documents）＋chunking＋production_embed_model を再利用し、**格納は
qdrant-client 直叩き**（llama_index のベクトルストア形式＝_node_content 等は
持ち込まない。qdrant 経路の serving は payload の text／メタデータを直接読むため不要
＝B1 で実証済）。payload は metadata_core の共通コア8欄＋issuer を ingest 時に一発で刻印
（date_int の int 化も含む＝旧 backfill_meta の後追いを廃止）。

サブコマンド：

  clone     既存コレクションの複製（vectors ごと・legacy キー除去・語彙サイドカーコピー）。
            v6 → v7 の土台作りに使う。冪等（件数一致ならスキップ）
  ingest    未投入 doc だけをチャンク化 → ruri 埋め込み → upsert。冪等（投入済み doc は
            スキップ）・再開可能（1 doc = 1 upsert 呼び＝実質アトミック。中断後は同コマンド
            再実行のみ）。完了後に finalize を自動実行
  finalize  BM25 sparse の全再構築（大域統計依存＝増分不可・qdrant_bm25）
            ＋ metadata_core 全点監査＋issuer 分布レポート

実行（コレクションは COLLECTION_NAME で指定＝CORPUS_REGISTRY の自己写像で解決）：
    COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.qdrant_ingest clone
    COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.qdrant_ingest ingest [--limit N] [--dry-run]
    COLLECTION_NAME=policy_claims_v7 python -m recommendations.ingest.qdrant_ingest ingest --files A.pdf --redo
"""
import argparse
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

from qdrant_client import QdrantClient, models

from recommendations.core.config import COLLECTION_NAME, DATA_DIR, INGEST_NUM_WORKERS, OPENAI_API_KEY, PDF_DIR
from polyarchy_common.logsetup import configure_quiet_logging, get_logger
from recommendations.core.qdrant_store import DENSE_NAME, ensure_collection
from recommendations.core.qdrant_store import QDRANT_URL

log = get_logger("polyarchy.qdrant_ingest")

CLONE_SRC_DEFAULT = "policy_claims_v6"
DST_CORPUS = "policy_claims"  # 共通コア corpus 刻印（CORPUS_REGISTRY のキーと一致させる）

# llama_index／ローカル環境の内部キー。qdrant 経路の serving は読まない（B1 実証）ため
# 新規点の payload に書かず、クローン時は既存点からも除去する（B4 設計 §5・承認済）。
LEGACY_KEYS = ("_node_content", "_node_type", "file_path")

# 発行体ファセット（バッチ1）。org→名称は recommendations.core.orgs の 1 表（gov のみ file_name 接頭辞で細分化）。
# 新収集で gov の会議体が増えたらまず ISSUER_OF_GOV_PREFIX を拡張する（判別不能は ingest が即エラーで止める）。
from recommendations.core.orgs import ISSUER_OF_ORG  # noqa: E402

ISSUER_OF_GOV_PREFIX = {
    "cefp": "経済財政諮問会議",
    "kisei": "規制改革推進会議",
    "zaiseishin": "財政制度等審議会",
}

SCROLL_BATCH = 512
CLIENT_TIMEOUT = 300


def issuer_of(payload: dict) -> str:
    """payload（org・file_name）から発行体名を決める。判別不能は空文字（呼び出し側で扱う）。"""
    org = payload.get("org") or ""
    if org in ISSUER_OF_ORG:
        return ISSUER_OF_ORG[org]
    if org == "gov":
        parts = (payload.get("file_name") or "").split("_")
        return ISSUER_OF_GOV_PREFIX.get(parts[1] if len(parts) > 1 else "", "")
    return ""


def _client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, timeout=CLIENT_TIMEOUT)


def _ensure_target(qc: QdrantClient, collection: str, recreate: bool = False) -> None:
    """コレクション＋B4 で必要な payload index（issuer・file_name）を用意する（冪等）。"""
    ensure_collection(qc, collection, recreate)
    qc.create_payload_index(collection, "issuer", models.PayloadSchemaType.KEYWORD)
    # file_name は増分 ingest の doc 単位冪等判定（設計 D3）に使う
    qc.create_payload_index(collection, "file_name", models.PayloadSchemaType.KEYWORD)


def _fn_filter(file_name: str) -> models.Filter:
    return models.Filter(must=[models.FieldCondition(
        key="file_name", match=models.MatchValue(value=file_name))])


def _file_count(qc: QdrantClient, collection: str, file_name: str) -> int:
    return qc.count(collection, count_filter=_fn_filter(file_name), exact=True).count


def _vocab_path(collection: str) -> Path:
    return DATA_DIR / "bm25" / f"{collection}_vocab.json.gz"


# ──────────────────────────────── clone ────────────────────────────────

def clone(src: str, dst: str, recreate: bool = False) -> None:
    """src コレクションを dst へ複製する（dense/sparse とも運搬・legacy キー除去・冪等）。"""
    qc = _client()
    if not qc.collection_exists(src):
        raise RuntimeError(f"複製元がありません: {src}")
    total = qc.count(src, exact=True).count
    if total == 0:
        raise RuntimeError(f"複製元が空です: {src}")

    _ensure_target(qc, dst, recreate)
    have = qc.count(dst, exact=True).count
    if have == total:
        log.info("複製済み（%d 件一致）＝コピーはスキップ。作り直すには --recreate", total)
    else:
        if have not in (0, total):
            log.warning("複製先が中途半端（%d/%d 件）。--recreate での作り直しを推奨", have, total)
        log.info("クローン開始: %s(%d件) → %s（再埋め込みなし・legacy キー除去: %s）",
                 src, total, dst, "・".join(LEGACY_KEYS))
        t0 = time.time()
        done = stripped = 0
        offset = None
        while True:
            points, offset = qc.scroll(src, limit=SCROLL_BATCH, offset=offset,
                                       with_payload=True, with_vectors=True)
            buf = []
            for p in points:
                raw = p.payload or {}
                payload = {k: v for k, v in raw.items() if k not in LEGACY_KEYS}
                if len(payload) != len(raw):
                    stripped += 1
                buf.append(models.PointStruct(id=str(p.id), vector=p.vector, payload=payload))
            if buf:
                qc.upsert(dst, points=buf, wait=True)
                done += len(buf)
                if done % 10_000 < SCROLL_BATCH:
                    log.info("  %d/%d（%.0f秒）", done, total, time.time() - t0)
            if offset is None:
                break
        have = qc.count(dst, exact=True).count
        if have != total:
            raise RuntimeError(f"件数不一致: {src} {total} != {dst} {have}")
        log.info("クローン完了: %d 件・legacy キー除去 %d 点・%.0f秒", have, stripped, time.time() - t0)

    # BM25 語彙サイドカー：本文同一のためコピーで足りる（増分後は finalize が作り直す）
    src_vocab, dst_vocab = _vocab_path(src), _vocab_path(dst)
    if src_vocab.exists():
        shutil.copyfile(src_vocab, dst_vocab)
        log.info("語彙サイドカーをコピー: %s → %s", src_vocab.name, dst_vocab.name)
    else:
        log.warning("複製元の語彙サイドカーがありません: %s（finalize で構築可能）", src_vocab)


# ──────────────────────────────── ingest ───────────────────────────────

def candidate_files() -> list[Path]:
    """投入候補ファイル（PDF_DIR 配下の .pdf/.txt から catalog の status 除外を先に落とす）。

    status の意味論は ingest.py と同一（INGEST_OK_STATUS）。カタログ未登録は候補に残す
    ＝load_documents の層フェイルクローズ（provenance 無し→機密→中断）に判定を委ねる。
    """
    from recommendations.ingest.ingest import INGEST_OK_STATUS, load_catalog
    catalog = load_catalog()
    out = []
    for p in sorted(PDF_DIR.rglob("*")):
        if p.suffix not in (".pdf", ".txt"):
            continue
        row = catalog.get(p.name)
        if row is not None and (row.get("status") or "ok") not in INGEST_OK_STATUS:
            continue
        out.append(p)
    return out


def build_payload(node, corpus: str, text: str) -> dict:
    """チャンク node → Qdrant payload（共通コア刻印＋issuer＋text。設計 D5）。"""
    meta = {k: v for k, v in node.metadata.items() if k not in LEGACY_KEYS}
    if "date_int" in meta:
        meta["date_int"] = int(meta["date_int"])  # 空日付はキー自体が無い（既存意味論）
    meta["corpus"] = corpus
    meta.setdefault("lang", "ja")
    issuer = issuer_of(meta)
    if not issuer:
        raise RuntimeError(f"issuer 判別不能: {meta.get('file_name')}"
                           "（ISSUER_OF_ORG / ISSUER_OF_GOV_PREFIX の拡張が必要）")
    meta["issuer"] = issuer
    meta["text"] = text
    return meta


def ingest(collection: str, corpus: str = DST_CORPUS, limit: int | None = None,
           files: list[str] | None = None, redo: bool = False,
           dry_run: bool = False, skip_finalize: bool = False) -> None:
    """doc（file_name）単位の増分 ingest（冪等・再開可能。設計 §3）。"""
    qc = _client()
    _ensure_target(qc, collection)

    cands = candidate_files()
    if files:
        want = set(files)
        cands = [p for p in cands if p.name in want]
        missing = want - {p.name for p in cands}
        if missing:
            sys.exit(f"ERROR: 指定ファイルが投入候補にありません: {sorted(missing)}")

    # 投入済み判定：1 doc = 1 upsert 呼び（実質アトミック）のため「点がある＝完投済み」
    remaining, skipped = [], 0
    for p in cands:
        if not redo and _file_count(qc, collection, p.name) > 0:
            skipped += 1
            continue
        remaining.append(p)
    log.info("投入対象: 候補 %d doc・投入済みスキップ %d・残 %d%s",
             len(cands), skipped, len(remaining),
             f"（--limit {limit} 適用）" if limit and len(remaining) > limit else "")
    if dry_run:
        for p in remaining[:20]:
            log.info("  残: %s", p.name)
        if len(remaining) > 20:
            log.info("  …ほか %d doc", len(remaining) - 20)
        return
    if limit:
        remaining = remaining[:limit]
    if not remaining:
        log.info("新規投入なし＝finalize もスキップ")
        return

    if not OPENAI_API_KEY:
        sys.exit("ERROR: OPENAI_API_KEY が未設定です（semantic 境界検出に必要）")

    # ── チャンク化（残 doc 分だけ・境界検出はページ並列＝ingest.py と同一条件）
    from llama_index.core.ingestion import IngestionPipeline
    from llama_index.core.schema import MetadataMode

    from recommendations.ingest.chunking import STRATEGIES
    from recommendations.core.config import DEFAULT_STRATEGY
    from recommendations.core.embeddings import production_embed_model
    from recommendations.ingest.ingest import load_documents

    documents = load_documents(input_files=remaining)
    t0 = time.time()
    log.info("チャンク化開始: %d doc・%dページ（num_workers=%d・境界検出の並列往復）",
             len(remaining), len(documents), INGEST_NUM_WORKERS)
    pipeline = IngestionPipeline(transformations=STRATEGIES[DEFAULT_STRATEGY](),
                                 disable_cache=True)
    nodes = pipeline.run(
        documents=documents,
        num_workers=(INGEST_NUM_WORKERS if INGEST_NUM_WORKERS > 1 else None),
        show_progress=True,
    )
    by_file: dict[str, list] = defaultdict(list)
    for nd in nodes:
        by_file[nd.metadata["file_name"]].append(nd)
    log.info("チャンク化完了: %d doc・%d チャンク（%.0f秒）",
             len(by_file), len(nodes), time.time() - t0)

    # ── doc ごとに埋め込み → upsert（1 doc = 1 呼び。中断時の中途半端は残らない）
    embed_model = production_embed_model()
    total_chunks = 0
    for i, fn in enumerate(sorted(by_file), 1):
        fnodes = by_file[fn]
        if redo:
            qc.delete(collection, points_selector=models.FilterSelector(filter=_fn_filter(fn)),
                      wait=True)
        # 埋め込み入力は VectorStoreIndex と同一（EMBED モード＝メタデータ除外済＝生本文）
        embed_texts = [nd.get_content(metadata_mode=MetadataMode.EMBED) for nd in fnodes]
        embeddings = embed_model.get_text_embedding_batch(embed_texts)
        points = [
            models.PointStruct(
                id=nd.node_id,
                vector={DENSE_NAME: [float(x) for x in emb]},
                payload=build_payload(nd, corpus,
                                      text=nd.get_content(metadata_mode=MetadataMode.NONE)),
            )
            for nd, emb in zip(fnodes, embeddings)
        ]
        qc.upsert(collection, points=points, wait=True)
        have = _file_count(qc, collection, fn)
        if have != len(points):
            raise RuntimeError(f"{fn}: 投入数不一致 {have} != {len(points)}")
        total_chunks += len(points)
        log.info("  %d/%d %s: %d チャンク（累計 %d・%.0f秒）",
                 i, len(by_file), fn, len(points), total_chunks, time.time() - t0)

    log.info("増分投入完了: %d doc・%d チャンク・%.0f秒（コレクション実数 %d）",
             len(by_file), total_chunks, time.time() - t0,
             qc.count(collection, exact=True).count)
    if skip_finalize:
        log.info("--skip-finalize 指定＝BM25 再構築と監査は後で finalize を実行すること")
    else:
        finalize(collection)


# ─────────────────────────────── finalize ──────────────────────────────

def finalize(collection: str) -> None:
    """BM25 sparse 全再構築（大域統計依存＝増分不可）＋共通コア監査＋issuer 分布。"""
    from recommendations.core.qdrant_store import CORPUS_REGISTRY
    if collection not in CORPUS_REGISTRY:
        sys.exit(f"ERROR: {collection} が CORPUS_REGISTRY で解決できません。"
                 f"COLLECTION_NAME={collection} を環境変数に付けて実行してください")

    from recommendations.core.qdrant_bm25 import build_sparse_bm25
    build_sparse_bm25(collection)

    from recommendations.ingest.metadata_audit import audit
    if not audit(collection):
        raise SystemExit("ERROR: 共通コア監査 不適合")

    qc = _client()
    dist = {}
    for name in sorted(set(ISSUER_OF_ORG.values()) | set(ISSUER_OF_GOV_PREFIX.values())):
        c = qc.count(collection, count_filter=models.Filter(must=[models.FieldCondition(
            key="issuer", match=models.MatchValue(value=name))]), exact=True).count
        if c:
            dist[name] = c
    log.info("issuer 分布: %s", dist)


# ───────────────────────────────── CLI ─────────────────────────────────

def main() -> None:
    configure_quiet_logging()
    ap = argparse.ArgumentParser(description="Qdrant への doc 単位増分 ingest（B4-①）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("clone", help="既存コレクションを複製（legacy キー除去・語彙コピー）")
    c.add_argument("--src", default=CLONE_SRC_DEFAULT, help=f"複製元（既定 {CLONE_SRC_DEFAULT}）")
    c.add_argument("--recreate", action="store_true", help="複製先を削除して作り直す")

    i = sub.add_parser("ingest", help="未投入 doc の増分 ingest（完了後 finalize 自動実行）")
    i.add_argument("--corpus", default=DST_CORPUS, help=f"corpus 刻印（既定 {DST_CORPUS}）")
    i.add_argument("--limit", type=int, help="残 doc の先頭 N 件だけ投入（スモーク用）")
    i.add_argument("--files", nargs="+", help="file_name を指定して対象を絞る")
    i.add_argument("--redo", action="store_true",
                   help="--files 指定の doc を削除して再投入（--files 必須）")
    i.add_argument("--dry-run", action="store_true", help="残 doc の一覧表示のみ")
    i.add_argument("--skip-finalize", action="store_true",
                   help="finalize（BM25 再構築＋監査）を実行しない")

    f = sub.add_parser("finalize", help="BM25 sparse 全再構築＋共通コア監査のみ実行")

    for p in (c, i, f):
        p.add_argument("--collection", default=COLLECTION_NAME,
                       help=f"対象コレクション（既定 COLLECTION_NAME={COLLECTION_NAME}）")
    args = ap.parse_args()

    if args.cmd == "clone":
        clone(args.src, args.collection, recreate=args.recreate)
    elif args.cmd == "ingest":
        if args.redo and not args.files:
            sys.exit("ERROR: --redo は --files と併用してください（全 doc 再投入の誤爆防止）")
        ingest(args.collection, corpus=args.corpus, limit=args.limit, files=args.files,
               redo=args.redo, dry_run=args.dry_run, skip_finalize=args.skip_finalize)
    elif args.cmd == "finalize":
        finalize(args.collection)


if __name__ == "__main__":
    main()
