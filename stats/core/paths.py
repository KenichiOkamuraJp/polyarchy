"""
stats のデータ置き場（パス決定の唯一の定義）。各モジュールは `Path(__file__)` からの相対計算をせず、ここを参照する。

    stats/data/                DATA_DIR（accessor.cache はここからの相対パス＝exact_match の照合対象。変えない）
      registry/series.jsonl    REGISTRY_PATH（seed_registry の生成物）
      values/<series_id>.jsonl VALUES_DIR（値ストア）
      cache/<kind>/<取得日>/   CACHE_DIR（原本）
      eval/                    EVAL_DIR（exact_match.jsonl／fail_closed.jsonl）
      query_log/queries.jsonl  QUERY_LOG_PATH（捕捉ログ。env STATS_QUERY_LOG で上書き）
"""
from __future__ import annotations

import os
from pathlib import Path

STATS_DIR = Path(__file__).resolve().parents[1]          # stats/
ROOT_DIR = STATS_DIR.parent                                # リポジトリ root
DATA_DIR = STATS_DIR / "data"
REGISTRY_DIR = DATA_DIR / "registry"
REGISTRY_PATH = REGISTRY_DIR / "series.jsonl"
VALUES_DIR = DATA_DIR / "values"
CACHE_DIR = DATA_DIR / "cache"
EVAL_DIR = DATA_DIR / "eval"
QUERY_LOG_PATH = Path(os.environ.get("STATS_QUERY_LOG") or (DATA_DIR / "query_log" / "queries.jsonl"))
ENV_PATH = STATS_DIR / ".env"                              # ESTAT_APP_ID（git 外）
