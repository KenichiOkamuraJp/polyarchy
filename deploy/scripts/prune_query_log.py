"""捕捉ログ（クエリログ）の保持期間を過ぎたレコードを削除する。

プライバシーポリシーの約束＝**保存期間30日・以降自動削除**を機械的に担保する。
systemd タイマー（polyarchy-logprune.timer）から毎日実行される。

仕様：
- 対象は JSONL（1行1レコード・`ts` は ISO8601 ローカル時刻／`recommendations/core/query_capture.py` が書く）。
- `ts` が保持期間より古い行を削除。**`ts` を解釈できない行も削除**（年齢不明のものを残すと
  「30日で消える」という約束を満たせないため。正常な writer は必ず `ts` を書く）。
- **追記との競合対策**：読み取り時のファイルサイズを覚えておき、書き換え直前にその位置以降へ
  追記された分を末尾に継ぎ足す（削除中に来たクエリを失わない）。
- 書き換えは一時ファイル＋`os.replace` で原子的に行う（途中で落ちても壊れない）。

環境変数：
  POLYARCHY_QUERY_LOG   対象ファイル（既定 /opt/polyarchy/polyarchy/recommendations/data/query_log/queries.jsonl）
- 同じディレクトリに `s3_mirror/`（手元用 usage_report のミラー）があれば丸ごと削除する＝生ログの複製を 30 日の外に残さない（2026-09-03）。
  LOG_RETENTION_DAYS    保持日数（既定 30）
  DRY_RUN               "1" なら削除せず件数だけ表示
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

DEFAULT_LOG = "/opt/polyarchy/polyarchy/recommendations/data/query_log/queries.jsonl"  # B6 再編後のレイアウト（2026-09-03 修正）


def main() -> int:
    path = os.environ.get("POLYARCHY_QUERY_LOG", DEFAULT_LOG)
    days = int(os.environ.get("LOG_RETENTION_DAYS", "30"))
    dry = os.environ.get("DRY_RUN", "") == "1"
    cutoff = datetime.now() - timedelta(days=days)

    mirror = os.path.join(os.path.dirname(path) or ".", "s3_mirror")
    if os.path.isdir(mirror):
        if dry:
            print(f"[prune] DRY_RUN: {mirror} を削除する予定（生ログの複製）")
        else:
            import shutil
            shutil.rmtree(mirror, ignore_errors=True)
            print(f"[prune] {mirror} を削除（生ログの複製＝保持対象外）")
    if not os.path.exists(path):
        print(f"[prune] 対象なし: {path}")
        return 0

    size = os.path.getsize(path)
    with open(path, "rb") as f:
        raw = f.read(size)

    lines = raw.decode("utf-8", errors="replace").splitlines()
    kept, dropped = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            ts = datetime.fromisoformat(json.loads(line)["ts"])
        except Exception:
            dropped += 1          # 年齢不明＝保持を保証できないので削除する
            continue
        if ts >= cutoff:
            kept.append(line)
        else:
            dropped += 1

    print(f"[prune] {path}: 保持{days}日 / 総{len(lines)}行 → 残{len(kept)}行・削除{dropped}行")
    if dry:
        print("[prune] DRY_RUN のため書き換えない")
        return 0
    if dropped == 0:
        print("[prune] 削除対象なし＝書き換えない")
        return 0

    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".prune-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            for line in kept:
                out.write(line + "\n")
            # 読み取り後に追記された分（削除処理中に来たクエリ）を失わないよう継ぎ足す。
            with open(path, "rb") as src:
                src.seek(size)
                tail = src.read()
            if tail:
                out.write(tail.decode("utf-8", errors="replace"))
                print(f"[prune] 実行中の追記 {len(tail)} バイトを保全")
        os.replace(tmp, path)     # 原子的に差し替え
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print("[prune] 完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
