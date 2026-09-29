"""業種の置き場（data/store/industries.json）を EDINET コードリストから作る（第 1c 便）。

  python -m companies.ops.build_industries
コードリスト（金融庁 EDINET・git 外）を data/cache/codelist/ に置いてから回す：
  curl -sS -o companies/data/cache/codelist/Edinetcode.zip https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip
  unzip -o companies/data/cache/codelist/Edinetcode.zip -d companies/data/cache/codelist/
CSV は CP932・1 行目が「ダウンロード実行日」の行・2 行目が見出し。写すのは登録簿の会社の「提出者業種」と「上場区分」だけ（寄せない）。
語彙の外の業種・コードリストに無い会社があれば止める（黙って空にしない）。
"""
from __future__ import annotations

import csv
import io
import json
import sys

from companies.core import store
from companies.core.industries import INDUSTRIES, OUTSIDE_FILER
from companies.ingest import verify_xbrl as v

CSV_PATH = v.CACHE / "codelist" / "EdinetcodeDlInfo.csv"


def main() -> int:
    rows = list(csv.reader(io.StringIO(CSV_PATH.read_bytes().decode("cp932"))))
    as_of, head, body = rows[0], rows[1], rows[2:]
    col = {h: i for i, h in enumerate(head)}
    idx = {r[col["ＥＤＩＮＥＴコード"]]: r for r in body}
    reg = store.registry()
    missing = [c for c in reg if c not in idx]
    comp = {}
    for c in (c for c in reg if c in idx):
        ind = idx[c][col["提出者業種"]]
        # 提出義務が無くなった会社（第 1d 便の遡りで入る上場廃止 等）は業種の欄が「提出義務者以外」＝業種なしとして写す（推して埋めない）
        comp[c] = ({"industry": None, "listing": idx[c][col["上場区分"]], "filer_status": ind} if ind == OUTSIDE_FILER else
                   {"industry": ind, "listing": idx[c][col["上場区分"]]})
    outside = sorted({x["industry"] for x in comp.values() if x["industry"] is not None} - set(INDUSTRIES))
    if missing or outside:
        print(f"停止：コードリストに無い会社 {len(missing)}（例 {missing[:5]}）・語彙の外の業種 {outside}", file=sys.stderr)
        return 1
    src = {"name": "EDINET コードリスト（EdinetcodeDlInfo.csv）", "provider": "金融庁 EDINET", "as_of": as_of[1],
           "url": "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip", "fields": ["提出者業種", "上場区分"]}
    (store.STORE / "industries.json").write_text(json.dumps({"source": src, "companies": comp}, ensure_ascii=False, indent=1, sort_keys=True))
    print(f"業種: {len(comp)} 社（{src['as_of']}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
