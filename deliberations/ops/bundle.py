"""
箱へ運ぶ束（M6 の下準備・2026-10-04）：審議会議事録DB のコレクションを Qdrant のスナップショットで書き出し、
検索に要るファイル（BM25 の語彙・目録・文書ごとの解析結果）と一緒に 1 つのフォルダにまとめる。復元はその逆。

    python -m deliberations.ops.bundle export                      # data/bundle/<collection>/ に書き出す
    python -m deliberations.ops.bundle restore --url http://127.0.0.1:6340 [--as 別名]   # 束から復元
    python -m deliberations.ops.bundle restore --if-changed --url ...   # 束が前回の復元から変わったか、コレクションが欠けたときだけ（箱の bootstrap）

なぜスナップショットか：手元の審議会DB は政策主張DB の qdrant-dev と別の Qdrant（qdrant-delib）に入っている。
政策主張DB の箱へのデータ同期は Qdrant のストレージのフォルダごとの S3 ミラー（--delete）なので、そこに混ぜると
配布のたびに消える。束は S3 `data/deliberations/bundle/` に置き（upload_to_s3.sh）、箱では審議会DB 専用の Qdrant
（qdrant-deliberations.service・:6340・同じ v1.19.0）に bootstrap が束の変わったときだけ復元する（restore --if-changed）。
"""
import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path

from qdrant_client import QdrantClient

from deliberations.core import config
from deliberations.core.paths import DATA_DIR, DOCUMENTS, MANIFEST

BUNDLE_DIR = DATA_DIR / "bundle"


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def export(collection: str, url: str) -> Path:
    out = BUNDLE_DIR / collection
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    qc = QdrantClient(url=url, timeout=600)
    vocab = config.VOCAB_DIR / f"{collection}_vocab.json.gz"
    import gzip
    n_docs = json.load(gzip.open(vocab, "rt", encoding="utf-8"))["params"]["n_docs"]
    n_points = qc.count(collection, exact=True).count
    if n_docs != n_points:  # 語彙が索引と同じ時点のものでない＝運ぶと箱の検索の重みがずれる
        raise SystemExit(f"[bundle] 語彙の n_docs {n_docs} と索引の点 {n_points} が違う＝書き出さない（取り込みをやり直す）")
    snap = qc.create_snapshot(collection, wait=True)
    dest = out / "collection.snapshot"
    urllib.request.urlretrieve(f"{url}/collections/{collection}/snapshots/{snap.name}", dest)
    qc.delete_snapshot(collection, snap.name, wait=True)  # Qdrant 側の作業用の写しは消す
    for src, name in ((vocab, "vocab.json.gz"), (MANIFEST, "manifest.jsonl"), (DOCUMENTS, "documents.jsonl")):
        shutil.copy2(src, out / name)
    meta = {"collection": collection, "points": qc.count(collection, exact=True).count,
            "qdrant_version": json.load(urllib.request.urlopen(f"{url}/"))["version"],
            "files": {p.name: {"bytes": p.stat().st_size, "sha256": _sha(p)} for p in sorted(out.iterdir())}}
    (out / "bundle.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[bundle] 書き出し {collection}: {meta['points']} 点 → {out}"
          f"（{sum(f['bytes'] for f in meta['files'].values()) / 1e6:.1f} MB）")
    return out


def _restored_mark(bundle: Path) -> Path:
    return bundle.parent / f".restored_{bundle.name}"


def restore(bundle: Path, url: str, as_name: str | None, if_changed: bool = False) -> int:
    meta = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    sig = _sha(bundle / "bundle.json")
    if if_changed and as_name is None:
        mark = _restored_mark(bundle)
        try:
            n = QdrantClient(url=url).count(meta["collection"], exact=True).count
        except Exception:
            n = -1
        if mark.exists() and mark.read_text().strip() == sig and n == meta["points"]:
            print(f"[bundle] 束は前回の復元から変わっていない（{meta['collection']} {n} 点）＝何もしない")
            return 0
    for name, f in meta["files"].items():
        if _sha(bundle / name) != f["sha256"]:
            print(f"[bundle] sha256 が合わない: {name}", file=sys.stderr)
            return 1
    target = as_name or meta["collection"]
    ver = json.load(urllib.request.urlopen(f"{url}/"))["version"]
    if ver != meta["qdrant_version"]:
        print(f"[bundle] Qdrant の版が違う（束 {meta['qdrant_version']}・復元先 {ver}）＝中止", file=sys.stderr)
        return 1
    import requests
    with (bundle / "collection.snapshot").open("rb") as f:
        r = requests.post(f"{url}/collections/{target}/snapshots/upload?priority=snapshot&wait=true",
                          files={"snapshot": ("collection.snapshot", f)}, timeout=1800)
    r.raise_for_status()
    n = QdrantClient(url=url).count(target, exact=True).count
    vocab = config.VOCAB_DIR / f"{target}_vocab.json.gz"
    vocab.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle / "vocab.json.gz", vocab)
    if as_name is None:  # 本番の名前で復元するときだけ、目録と解析結果も置き換える（list_meeting・収録範囲が読む）
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        DOCUMENTS.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle / "manifest.jsonl", MANIFEST)
        shutil.copy2(bundle / "documents.jsonl", DOCUMENTS)
    print(f"[bundle] 復元 {target}: {n} 点（束 {meta['points']} 点）")
    if n == meta["points"] and as_name is None:
        _restored_mark(bundle).write_text(sig)
    return 0 if n == meta["points"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["export", "restore"])
    ap.add_argument("--collection", default=config.COLLECTION)
    ap.add_argument("--url", default=config.QDRANT_URL)
    ap.add_argument("--bundle", default=None, help="restore の束のフォルダ（省略＝data/bundle/<collection>）")
    ap.add_argument("--as", dest="as_name", default=None, help="restore で別名のコレクションに復元（試験用）")
    ap.add_argument("--if-changed", action="store_true", help="restore を束が変わったか欠けたときだけ行う")
    a = ap.parse_args()
    if a.cmd == "export":
        export(a.collection, a.url)
        return 0
    return restore(Path(a.bundle) if a.bundle else BUNDLE_DIR / a.collection, a.url, a.as_name, a.if_changed)


if __name__ == "__main__":
    sys.exit(main())
