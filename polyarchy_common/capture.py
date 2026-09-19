"""
捕捉ログ（追記専用 JSONL）の共通実装。

実運用の質問を貯めて週次トリアージ→評価セットに育てる「燃料」の器（Phase 12 の思想）。
各サービスはレコードの**中身**（recommendations なら query/orgs/since/…、stats なら indicator/year/…）を
自分で決め、ここは「1行1レコードで壊れず追記し、壊れ行を無視して読む」だけを共通化する。

原則：
- **fail-open**：捕捉は検索の付随機能。失敗しても例外を投げず、stderr に warning を残して False。
- ログの置き場所はサービスが決める（環境変数 or 既定パス）。ここは受け取るだけ。
- `ts` は本関数が付与（ISO 8601・秒精度）＝全サービスで同じ時刻表現。
- 認証済みリクエスト（`polyarchy_common.access` の contextvar）では `user_hash` を自動付与（メール平文は残さない）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from polyarchy_common.logsetup import get_logger


def dedup(xs: Optional[Sequence[str]]) -> Optional[list[str]]:
    """順序を保った重複排除（None/空は None を返す）。"""
    if not xs:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out or None


def append_record(path: Path, record: dict, *, logger_name: str = "polyarchy.capture") -> bool:
    """`record` に ts を先頭付与して JSONL に1行 append（best-effort・戻り値=成功可否）。"""
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), **record}
        # 認証済み（Access Managed OAuth 経由）なら利用者キーを付与（メールの sha256 先頭16桁・平文なし）。
        # 未認証・stdio では付かない。用途＝契約者数・利用量の把握のみ（プライバシーポリシー記載）。
        try:
            from polyarchy_common.access import user_hash
            uh = user_hash()
            if uh and "user_hash" not in rec:
                rec["user_hash"] = uh
        except Exception:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception as e:  # fail-open：捕捉失敗でサービスを止めない
        try:
            get_logger(logger_name).warning("クエリ捕捉に失敗（処理は続行）: %s", e)
        except Exception:
            pass
        return False


def load_records(path: Path) -> list[dict]:
    """捕捉済みレコードをファイル順（=時系列）で読み出す（壊れ行はスキップ＝fail-open）。"""
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return rows
