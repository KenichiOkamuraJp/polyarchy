"""上る元が全部読めるかを確かめる（upload_to_s3.sh が S3 に何か書く前に、同期の 1 本ずつに呼ぶ）。

aws s3 sync は読めないファイルを警告して飛ばし、ほかのファイルの上りと --delete を済ませてから失敗する＝S3 側が
欠けた状態で残る（2026-10-30・Linux の Docker では Qdrant〔root〕が書いた 0600 のファイルが作業用のユーザーに読めない）。
読めないものが 1 つでもあれば非 0 で返し、upload_to_s3.sh は何も上げずに止まる。

  使い方: python check_readable.py <同期の元> [aws s3 sync の引数 ...]
  引数のうち --exclude <pattern> だけを読む（ほかは無視）＝同期と同じ除き方で数える（除く場所の読めないファイルでは止めない）。
  パターンは aws と同じく同期の元からの相対パスに当て、* は / もまたぐ。シンボリックリンクは aws と同じく先を辿る。
  終了コード 0＝全部読める・1＝読めないものがある（一覧を stderr）・2＝使い方の誤り
"""
from __future__ import annotations

import os
import sys
from fnmatch import fnmatchcase


def excludes_of(args: list[str]) -> list[str]:
    out = []
    for i, a in enumerate(args):
        if a == "--exclude" and i + 1 < len(args):
            out.append(args[i + 1])
    return out


def unreadable(src: str, excludes: list[str]) -> list[str]:
    bad: list[str] = []
    if not os.path.isdir(src):
        return [f"{src}（同期の元が無い）"]

    def skipped(rel: str) -> bool:
        return any(fnmatchcase(rel, p) for p in excludes)

    def dir_skipped(rel: str) -> bool:   # 「d/*」の形で丸ごと除く場所は降りない（中身はすべて除かれる）
        return any(p.endswith("*") and fnmatchcase(rel + "/", p) for p in excludes)

    def on_error(e: OSError) -> None:
        bad.append(f"{e.filename}（フォルダを読めない）")

    for top, dirs, files in os.walk(src, onerror=on_error, followlinks=True):
        rel_top = os.path.relpath(top, src)
        rel_top = "" if rel_top == "." else rel_top + "/"
        keep = []
        for dn in dirs:
            rel = rel_top + dn
            if dir_skipped(rel):
                continue
            if not os.access(os.path.join(top, dn), os.R_OK | os.X_OK):
                bad.append(f"{os.path.join(top, dn)}（フォルダを読めない）")
                continue
            keep.append(dn)
        dirs[:] = keep
        for fn in files:
            rel = rel_top + fn
            if skipped(rel):
                continue
            if not os.access(os.path.join(top, fn), os.R_OK):
                bad.append(os.path.join(top, fn))
    return bad


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    src, rest = argv[0], argv[1:]
    bad = unreadable(src, excludes_of(rest))
    if bad:
        print(f"[upload] ERROR: {src} に読めないものが {len(bad)} 件（同期はこれを飛ばし、ほかの上りと削除を済ませてから失敗する）:",
              file=sys.stderr)
        for b in bad[:20]:
            print(f"  {b}", file=sys.stderr)
        if len(bad) > 20:
            print(f"  …ほか {len(bad) - 20} 件", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
