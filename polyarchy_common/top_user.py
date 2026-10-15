"""
直近 1 時間の最多の 1 人の呼び出しの数（B44 第 1 便・2026-10-15・運用設計 §1.2）。

掲載で利用者が不特定になった。エッジのレート制限は接続元 IP が鍵で、コネクタ経由では利用者を分けられない＝1 人の大量の利用に
気づける経路が週次レポートしか無かった。箱が 5 分ごとに、捕捉ログ（全サービス）を利用者ごとに数え、**最多の 1 人の数だけ**を
メトリクス `top_user_calls_1h` にする（deploy/bootstrap/top_user_metric.sh）＝アラーム top-user。

数える単位＝ツールの呼び出し（捕捉ログの行）。POST の本数はクライアントの作りで 4〜11 倍に揺れる（残タスク §E-B44 の判断 1）。
利用者の無い行（未認証・stdio）は数えない。★利用者のハッシュは箱から出さない（メトリクスにもログにも）＝誰かを特定するのは
運営者が手元で、S3 の捕捉ログの写しに対して `--show` を流すときだけ（RUNBOOK §1「top-user」）。

捕捉ログは 30 日ぶん溜まる＝ファイルの末尾からブロックで読み、窓より古い行に当たったら止める（行は時刻順に追記される）。

実行：python -m polyarchy_common.top_user --log <捕捉ログ> [--log …]            → `top_user <数>`（箱）
      python -m polyarchy_common.top_user --log … --at 2026-10-15T21:00+09:00 --show 5  → 多い順に `<ハッシュ> <数>`（手元）
      python -m polyarchy_common.top_user --hash-of <メール>                      → 捕捉ログと同じハッシュ（手元）

捕捉ログの ts は箱の時計（UTC・時差の表記なし）。手元の PC の「いま」は時差でずれる＝--show には --at が要る（2026-10-16）。
--at に時差を書けば UTC に直して比べる（書かなければ UTC として読む）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

WINDOW = timedelta(hours=1)
_PREFIX = '{"ts": "'   # capture.append_record は ts を先頭に置く＝時刻だけなら JSON を読まずに比べられる


def _lines_from_end(path: Path, block: int) -> Iterator[bytes]:
    """ファイルの行を末尾から 1 行ずつ返す（ブロックの境目で切れた行はつないでから返す）。"""
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos, rest = f.tell(), b""
        while pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            parts = (f.read(step) + rest).split(b"\n")
            rest = parts[0]                      # 先頭は前のブロックに続く（まだ切れているかもしれない）
            for ln in reversed(parts[1:]):
                if ln:
                    yield ln
        if rest:
            yield rest


def count_by_user(paths: Iterable[Path], now: datetime | None = None, window: timedelta = WINDOW,
                  block: int = 1 << 16) -> dict[str, int]:
    """窓（now から window さかのぼる）に入る行を、利用者のハッシュごとに数える（全ファイルを合わせて）。"""
    now = now or datetime.now()
    since = (now - window).isoformat(timespec="seconds")
    until = now.isoformat(timespec="seconds")
    counts: Counter = Counter()
    for p in paths:
        if not Path(p).exists():
            continue
        for raw in _lines_from_end(Path(p), block):
            ln = raw.decode("utf-8", errors="replace")
            ts = ln[len(_PREFIX):len(_PREFIX) + 19] if ln.startswith(_PREFIX) else ""
            if not ts:
                continue                         # 壊れた行・形の違う行は数えない（その先も読む）
            if ts < since:
                break                            # ここから前は窓の外
            if ts > until:
                continue
            try:
                uh = json.loads(ln).get("user_hash")
            except ValueError:
                continue
            if uh:
                counts[uh] += 1
    return dict(counts)


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description="直近 1 時間の最多の 1 人の呼び出しの数（捕捉ログの行）")
    ap.add_argument("--log", action="append", default=[], help="捕捉ログ（JSONL）。複数可＝合わせて数える")
    ap.add_argument("--at", help="窓の終わり（ISO）。時差なし＝UTC（箱の時計）・時差あり（+09:00 等）＝UTC に直す。既定＝いま（箱で流すとき）")
    ap.add_argument("--show", type=int, metavar="N", help="多い順に N 人のハッシュと数を出す（運営者が手元で特定するときだけ）")
    ap.add_argument("--hash-of", metavar="EMAIL", help="メールから、捕捉ログと同じハッシュを出す")
    a = ap.parse_args(argv)
    if a.hash_of:
        from polyarchy_common.access import user_hash
        print(user_hash(a.hash_of), file=out)
        return 0
    if a.show and not a.at:
        ap.error("--show には --at が要る（捕捉ログの時刻は UTC＝手元の「いま」では窓がずれる）")
    now = datetime.fromisoformat(a.at) if a.at else None
    if now and now.tzinfo:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    counts = count_by_user([Path(p) for p in a.log], now=now)
    if a.show:
        for uh, n in Counter(counts).most_common(a.show):
            print(f"{uh} {n}", file=out)
        return 0
    print(f"top_user {max(counts.values(), default=0)}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
