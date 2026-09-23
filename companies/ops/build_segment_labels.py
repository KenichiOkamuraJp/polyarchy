"""セグメント別の値に使う標準要素の公式ラベル表（companies/core/segment_labels.json）を、取得済みの公式 CSV から作る。

  python -m companies.ops.build_segment_labels
公式 CSV（API type=5＝金融庁による XBRL→CSV 変換）の「項目名」は標準要素の公式ラベル。会社の zip には標準要素のラベルが入っていない
＝取込（XBRL）だけでは日本語の呼び名が付かない。第 1 便の語彙（core/items.py）のラベルを公式 CSV で確かめたのと同じ出所。
表に載せるのは、値の置き場（data/store/segments/）に実際に出てくる標準要素だけ（タクソノミのラベルを丸ごと持たない）＝取込の後に回す。
書類でラベルが割れる要素（タクソノミの版の違い＝「リース債務」→「リース負債」）は、提出日が最新の書類のラベルを採る。
取得済みの CSV に無い要素はラベルなし（要素 ID だけ）＝足すときは、その要素を含む書類の CSV を取ってから回す。
"""
from __future__ import annotations

import json
import sys

from companies.core import store
from companies.core.segments import LABELS, STANDARD_PREFIXES
from companies.eval.make_candidates import official_csv
from companies.ingest import verify_xbrl as v


def main() -> int:
    used = {f["element"] for code in store.segment_codes() for f in store.segments_of(code)["facts"]
            if f["element"].split(":")[0] in STANDARD_PREFIXES}
    submitted = {d: meta["submitted"] for c in store.registry().values() for d, meta in c.get("documents", {}).items()}
    labels: dict[str, tuple[str, str]] = {}  # 要素 → （提出日, ラベル）
    docs = sorted(p.stem for p in (v.CACHE / "csv").glob("*.zip"))
    for d in docs:
        for el, label, *_rest in official_csv(d):
            if el in used and label and (el not in labels or submitted.get(d, "") > labels[el][0]):
                labels[el] = (submitted.get(d, ""), label)
    LABELS.write_text(json.dumps({el: lab for el, (_, lab) in sorted(labels.items())}, ensure_ascii=False, indent=0))
    missing = sorted(used - labels.keys())
    print(f"標準要素 {len(labels)}/{len(used)} 件のラベル（公式 CSV {len(docs)} 書類から）→ {LABELS}")
    if missing:
        print(f"  ラベルの無い標準要素 {len(missing)} 件（CSV を足すと埋まる）: {missing[:10]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
