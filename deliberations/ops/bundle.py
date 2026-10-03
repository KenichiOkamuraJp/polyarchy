"""
箱へ運ぶ束（M6 の下準備・2026-10-04）：審議会議事録DB のコレクションを Qdrant のスナップショットで書き出し、
検索に要るファイル（BM25 の語彙・目録・文書ごとの解析結果）と一緒に 1 つのフォルダにまとめる。復元はその逆。

    python -m deliberations.ops.bundle export                      # data/bundle/<collection>/ に書き出す
    python -m deliberations.ops.bundle restore --url http://127.0.0.1:6333 [--as 別名]   # 束から復元

なぜスナップショットか：手元の審議会DB は政策主張DB の qdrant-dev と別の Qdrant（qdrant-delib）に入っている。
政策主張DB の箱へのデータ同期は Qdrant のストレージのフォルダごとの S3 ミラー（--delete）なので、そこに混ぜると
政策主張DB の配布に巻き込まれる。スナップショットならコレクション 1 つだけを運べ、箱の Qdrant（同じ v1.19.0）に
別コレクションとして復元できる。束の置き場（S3 の prefix）と箱での復元の呼び出しは M6 で決める（ここでは配線しない）。
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
    snap = qc.create_snapshot(collection, wait=True)
    dest = out / "collection.snapshot"
    urllib.request.urlretrieve(f"{url}/collections/{collection}/snapshots/{snap.name}", dest)
    qc.delete_snapshot(collection, snap.name, wait=True)  # Qdrant 側の作業用の写しは消す
    vocab = config.VOCAB_DIR / f"{collection}_vocab.json.gz"
    for src, name in ((vocab, "vocab.json.gz"), (MANIFEST, "manifest.jsonl"), (DOCUMENTS, "documents.jsonl")):
        shutil.copy2(src, out / name)
    meta = {"collection": collection, "points": qc.count(collection, exact=True).count,
            "qdrant_version": json.load(urllib.request.urlopen(f"{url}/"))["version"],
            "files": {p.name: {"bytes": p.stat().st_size, "sha256": _sha(p)} for p in sorted(out.iterdir())}}
    (out / "bundle.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[bundle] 書き出し {collection}: {meta['points']} 点 → {out}"
          f"（{sum(f['bytes'] for f in meta['files'].values()) / 1e6:.1f} MB）")
    return out


def restore(bundle: Path, url: str, as_name: str | None) -> int:
    meta = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
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
    return 0 if n == meta["points"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["export", "restore"])
    ap.add_argument("--collection", default=config.COLLECTION)
    ap.add_argument("--url", default=config.QDRANT_URL)
    ap.add_argument("--bundle", default=None, help="restore の束のフォルダ（省略＝data/bundle/<collection>）")
    ap.add_argument("--as", dest="as_name", default=None, help="restore で別名のコレクションに復元（試験用）")
    a = ap.parse_args()
    if a.cmd == "export":
        export(a.collection, a.url)
        return 0
    return restore(Path(a.bundle) if a.bundle else BUNDLE_DIR / a.collection, a.url, a.as_name)


if __name__ == "__main__":
    sys.exit(main())
