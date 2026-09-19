"""
提言の新着チェック（読み取り専用・ダウンロードなし）。

設計＝docs/運用設計.md §1.4（2026-09-03）。5 団体の目次（collect.py の index 関数＝実装を複製しない）を
走査し、catalog.csv に**未収録**の文書を数える。PDF/本文の取得は一切しない＝「新着があるか」だけを毎日知るための
チェック。取込の実行は従来どおり RUNBOOK §5（品質ゲートを通してから）＝本モジュールは検出まで。

出力＝recommendations/data/cache/update_check.json（checked_at・団体別の目次件数/新着数/新着例）。
ダッシュボード（polyarchy_common.ops_dashboard）が新着数と時刻を表示する。

実行（リポジトリ root）：
    python -m recommendations.ingest.check_new           # 全団体
    python -m recommendations.ingest.check_new --json    # 結果を stdout にも JSON で
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

configure_quiet_logging()
log = get_logger("recommendations.check_new")

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "recommendations" / "data" / "catalog.csv"
OUT_PATH = ROOT / "recommendations" / "data" / "cache" / "update_check.json"


def catalog_ids() -> set[str]:
    with open(CATALOG, encoding="utf-8") as f:
        return {r["doc_id"] for r in csv.DictReader(f) if r.get("doc_id")}


def _known(ids: set[str], bases: list[str]) -> bool:
    """候補 base のいずれかが catalog に居るか（nissho 複数 PDF の _N サフィックスも一致とみなす）。"""
    for b in bases:
        if b in ids or any(i.startswith(b + "_") for i in ids):
            return True
    return False


def check() -> dict:
    ids = catalog_ids()
    result = {"checked_at": datetime.now().isoformat(timespec="seconds"), "orgs": {}}
    # 団体ごとに独立に走らせ、1 団体の失敗（サイト改修等）が他を止めないようにする。
    from recommendations.ingest.collect import (GOV_SOURCES, doyukai_index,
                                                keidanren_index, nissho_index,
                                                rengo_teigen_index)
    y = date.today().year
    plans = {
        "keidanren": lambda: [([f"keidanren_{yr}_{e['num']}"], e["title"])
                              for yr in (y, y - 1) for e in keidanren_index(yr)],
        "rengo": lambda: [([f"rengo_{e['num']}", f"rengo_teigen_{e['num']}"], e["title"])
                          for e in rengo_teigen_index()],
        "nissho": lambda: [([f"nissho_{e['year']}_{e['stem']}"], e["title"]) for e in nissho_index()],
        "doyukai": lambda: [([f"doyukai_{e['year']}_{e['stem']}"], e["title"]) for e in doyukai_index()],
        "gov": lambda: [([f"gov_{src}_{e['num']}"], e["title"])
                        for src, fn in GOV_SOURCES.items() for e in fn()],
    }
    for org, plan in plans.items():
        row = {"total_index": 0, "new": 0, "new_samples": [], "error": None}
        try:
            cands = plan()
            row["total_index"] = len(cands)
            for bases, title in cands:
                if not _known(ids, bases):
                    row["new"] += 1
                    if len(row["new_samples"]) < 3:
                        row["new_samples"].append(title[:60])
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)[:200]
            log.warning("新着チェック失敗（%s）: %s", org, e)
        result["orgs"][org] = row
        log.info("新着チェック %s: 目次 %d 件・新着 %d 件%s", org, row["total_index"], row["new"],
                 f"（error: {row['error']}）" if row["error"] else "")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="提言の新着チェック（読み取り専用・catalog 未収録を数えるだけ）")
    ap.add_argument("--json", action="store_true", help="結果を stdout にも JSON で出す")
    a = ap.parse_args(argv)
    result = check()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    total_new = sum(o["new"] for o in result["orgs"].values())
    errs = [k for k, o in result["orgs"].items() if o["error"]]
    print(f"新着チェック: 合計 新着 {total_new} 件"
          + (f"・チェック失敗 {','.join(errs)}" if errs else "") + f" → {OUT_PATH.relative_to(ROOT)}",
          file=sys.stderr)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
