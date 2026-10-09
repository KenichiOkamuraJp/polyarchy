"""本番の HF モデルの重みを、配布物（S3）経由で箱の HF_HOME に置く（箱を HF Hub に接続させない・B17 (3)）。

対象＝本番の埋め込みとリランカー＝polyarchy_retrieval.models.PINNED_REVISIONS（リポジトリ → commit）の全部。
読み込み側（同じ models.py）と同じ commit を扱う。一覧を写さない。

2 つの使い方（どちらも同じ S3 の形＝`<DATA_PREFIX>/models/<org>--<name>/<commit>/` に重みのファイルと SHA256SUMS）：
- 手元（配布の上り・upload_to_s3.sh）：`--stage-upload <dir>`＝手元の HF キャッシュの snapshot（ゲートが測った重みそのもの＝
  測るもの＝配るもの）を <dir> に symlink で並べ、SHA256SUMS を書く。上りの `aws s3 sync` は symlink の先を送る。
  手元のキャッシュに無ければ止める（HF から取りに行かない＝local_files_only）。
- 箱（bootstrap ⑤）：`--staged <dir>`＝bootstrap が S3 から <dir> へ同期した重みを SHA256SUMS で照合し、HF キャッシュの
  snapshots/<commit>/ に hardlink で置く（同じ EBS＝容量は二重にならない）。照合が合わなければ止める（rc=1）。
  S3 にまだ無い commit（上りの前の環境）だけは HF から取る（旧来の snapshot_download・★の行を出す）。
読み込み（サービス・箱上 smoke）は HF_HUB_OFFLINE=1 で走る＝snapshots/<commit>/ だけで読める（2026-10-10 に手元で、
重みのファイルだけを置いたキャッシュから読み、普段のキャッシュと同じベクトルになることを確認）。ログは日本語。
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

SUMS = "SHA256SUMS"


def log(msg: str) -> None:
    print(f"[prefetch] {msg}", file=sys.stderr)


def s3_key(repo: str) -> str:
    return repo.replace("/", "--")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_sums(path: Path) -> dict[str, str]:
    sums = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, rel = line.split("  ", 1)
            sums[rel] = digest
    return sums


def stage_upload(out: Path, pinned: dict[str, str]) -> int:
    from huggingface_hub import snapshot_download
    for repo, rev in pinned.items():
        try:
            snap = Path(snapshot_download(repo_id=repo, revision=rev, local_files_only=True))
        except Exception as e:  # noqa: BLE001
            log(f"手元の HF キャッシュに {repo}@{rev[:12]} が無い＝ゲートを回した環境で上げる（{e}）")
            return 1
        dest = out / s3_key(repo) / rev
        lines = []
        for f in sorted(p for p in snap.rglob("*") if p.is_file()):
            rel = f.relative_to(snap).as_posix()
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(f.resolve(), dest / rel)
            lines.append(f"{sha256(f)}  {rel}")
        (dest / SUMS).write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"上りの準備: {repo}@{rev[:12]}（{len(lines)} ファイル）")
    return 0


def install_staged(staged: Path, pinned: dict[str, str]) -> int:
    from huggingface_hub.constants import HF_HUB_CACHE
    for repo, rev in pinned.items():
        src = staged / s3_key(repo) / rev
        if not (src / SUMS).is_file():
            log(f"★S3 に {repo}@{rev[:12]} の重みが無い＝HF から取る（release.sh の上りの後は S3 から取る）")
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=repo, revision=rev, tqdm_class=None)
            continue
        sums = read_sums(src / SUMS)
        bad = [rel for rel, d in sums.items() if not (src / rel).is_file() or sha256(src / rel) != d]
        if bad:
            log(f"{repo}@{rev[:12]} の重みが SHA256SUMS と合わない＝止める: {bad}")
            return 1
        snap = Path(HF_HUB_CACHE) / f"models--{s3_key(repo)}" / "snapshots" / rev
        for rel in sums:
            dst = snap / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and not dst.is_symlink() and os.path.samefile(src / rel, dst):
                continue
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.link(src / rel, dst)
        log(f"S3 の重みを照合して配置: {repo}@{rev[:12]}（{len(sums)} ファイル・sha256 一致）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--stage-upload", type=Path, help="手元：上りの形を <dir> に作る")
    g.add_argument("--staged", type=Path, help="箱：S3 から同期した <dir> を照合して HF キャッシュに置く")
    args = ap.parse_args()
    try:
        from polyarchy_retrieval.models import PINNED_REVISIONS
    except Exception as e:  # bootstrap ③ で本パッケージを pip -e 済み
        log(f"polyarchy_retrieval が読めません: {e}")
        return 1
    if args.stage_upload:
        return stage_upload(args.stage_upload, PINNED_REVISIONS)
    log(f"HF_HOME={os.environ.get('HF_HOME', '(未設定)')} へ配置します")
    rc = install_staged(args.staged, PINNED_REVISIONS)
    if rc == 0:
        log("全モデルの配置完了")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
