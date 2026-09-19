"""
値ストア（参照層）。`stats/data/values/<series_id>.jsonl`（1行＝1値）。設計は docs/参照粒度設計.md §4–§6。

- value は公表どおりの**文字列**（換算・丸めなし）。
- 1行： {"period","region","value","status","vintage","retrieved_at","accessor":{...具体値},"published_at"?}
- lookup は (period, region) 完全一致のみ。無ければ None。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from stats.core.paths import VALUES_DIR


@dataclass(frozen=True)
class ValueRecord:
    series_id: str
    period: str
    region: str
    value: str
    status: str
    vintage: str
    retrieved_at: str
    accessor: dict
    published_at: str = ""
    kind: str = ""        # 値単位の区別（"projection" のときのみ設定。空＝系列の kind を継承）

    def to_json(self) -> dict:
        d = {"period": self.period, "region": self.region, "value": self.value, "status": self.status,
             "vintage": self.vintage, "retrieved_at": self.retrieved_at, "published_at": self.published_at,
             "accessor": self.accessor}
        if self.kind:
            d["kind"] = self.kind
        return d


def _path(series_id: str) -> Path:
    return VALUES_DIR / f"{series_id}.jsonl"


def write_values(series_id: str, records: Iterable[ValueRecord]) -> int:
    """系列の値ファイルを丸ごと書き換える（最新 vintage のみ保持＝設計 §5）。"""
    VALUES_DIR.mkdir(parents=True, exist_ok=True)
    recs = sorted(records, key=lambda r: (r.region, r.period))
    with _path(series_id).open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r.to_json(), ensure_ascii=False) + "\n")
    return len(recs)


class ValueStore:
    def __init__(self, base: Path = VALUES_DIR):
        self.base = base
        self._cache: dict[str, dict[tuple[str, str], ValueRecord]] = {}

    def _load(self, series_id: str) -> dict[tuple[str, str], ValueRecord]:
        if series_id in self._cache:
            return self._cache[series_id]
        idx: dict[tuple[str, str], ValueRecord] = {}
        p = self.base / f"{series_id}.jsonl"
        if p.exists():
            with p.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    r = ValueRecord(series_id=series_id, period=d["period"], region=d.get("region", "JP"),
                                    value=str(d["value"]), status=d.get("status", ""), vintage=d.get("vintage", ""),
                                    retrieved_at=d.get("retrieved_at", ""), accessor=d.get("accessor", {}),
                                    published_at=d.get("published_at", ""), kind=d.get("kind", ""))
                    idx[(r.period, r.region)] = r
        self._cache[series_id] = idx
        return idx

    def has_data(self, series_id: str) -> bool:
        return bool(self._load(series_id))

    def lookup(self, series_id: str, period: str, region: str = "JP") -> Optional[ValueRecord]:
        return self._load(series_id).get((period, region))

    def periods(self, series_id: str, region: str = "JP") -> list[str]:
        return sorted(p for (p, r) in self._load(series_id) if r == region)

    def all_periods(self, series_id: str) -> list[str]:
        """全地域を通じた期間の一覧（地域粒度が national/pref/cty のどれでも空にならない）。"""
        return sorted({p for (p, _r) in self._load(series_id)})

    def count(self, series_id: str) -> int:
        return len(self._load(series_id))
