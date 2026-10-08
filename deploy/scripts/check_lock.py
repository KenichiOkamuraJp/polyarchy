"""ゲートを回す Python の依存が、箱のロック（deploy/requirements/lock-*.txt）と同じ版かを照合する。

release.sh の「配る前の確認」が呼ぶ（ゲートが測った依存＝箱で動く依存、を機械で確かめる）。開発側で基準値を測る前に
手で回してもよい（deliberations/CLAUDE.md の「開発用の env もロックの版に合わせてから基準値を測る」の担保）。

  python deploy/scripts/check_lock.py [ロックファイル]     # 既定＝deploy/requirements/lock-recommendations-stats.txt

照合の向きはロック → 走っている env だけ（env にだけある開発用の道具は問わない）。torch の +cpu のような
ローカル版の表記は落として比べる（Mac の torch には付かない＝同じ版）。合えば 0、違えば差を並べて 1。
2026-10-09 起票＝開発用 env は sentence-transformers 5.6／ロック 6.0 などがずれたまま測っていた。
"""
import re
import sys
from importlib import metadata
from pathlib import Path

DEFAULT_LOCK = Path(__file__).resolve().parents[1] / "requirements" / "lock-recommendations-stats.txt"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _base(ver: str) -> str:
    return ver.split("+", 1)[0]


def pins(lock: Path) -> dict[str, str]:
    """ロックの name==ver（継続行の --hash・コメント・オプション行は読まない）。"""
    out = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s;\\]+)", line)
        if m:
            out[_norm(m.group(1))] = m.group(3)
    return out


def mismatches(lock: Path) -> list[str]:
    installed = {_norm(d.metadata["Name"]): d.version for d in metadata.distributions() if d.metadata["Name"]}
    bad = []
    for name, want in sorted(pins(lock).items()):
        have = installed.get(name)
        if have is None:
            bad.append(f"{name}: ロック {want}・env に無い")
        elif _base(have) != _base(want):
            bad.append(f"{name}: ロック {want}・env {have}")
    return bad


def main() -> int:
    lock = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOCK
    n = len(pins(lock))
    bad = mismatches(lock)
    if bad:
        print(f"[check_lock] ✗ {sys.executable} の依存がロック {lock.name} と {len(bad)}/{n} 件違う：", file=sys.stderr)
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        print("[check_lock]   ゲートはロックから作った env で回す（deploy/RUNBOOK_OPS.md §7）。", file=sys.stderr)
        return 1
    print(f"[check_lock] ✓ {sys.executable} の依存はロック {lock.name} と一致（{n} 件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
