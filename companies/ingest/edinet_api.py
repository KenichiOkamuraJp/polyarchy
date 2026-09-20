"""EDINET API v2 の取得と XBRL インスタンスの読み取り（取込・検証・評価問づくりで共用）。

作法（companies/CLAUDE.md）：API だけを使う（画面のスクレイピングはしない）・1 リクエスト／秒・取得物は data/cache/ に置き同じ書類を二度取りに行かない。
キーは環境変数 EDINET_API_KEY か companies/.env（git 外）。取込側（作業用 PC）だけで使い、箱には運ばない。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from lxml import etree

HERE = Path(__file__).resolve().parent.parent   # companies/（.env・data/ の置き場）
DATA = HERE / "data"
CACHE = DATA / "cache"
API = "https://api.edinet-fsa.go.jp/api/v2"
WAIT_SEC = 1.0  # 規約「短時間における大量のアクセス」回避

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"


# ---------------------------------------------------------------- API

def _key() -> str:
    k = os.getenv("EDINET_API_KEY")
    if not k:
        envf = HERE / ".env"
        if envf.exists():
            for line in envf.read_text().splitlines():
                if line.startswith("EDINET_API_KEY="):
                    k = line.split("=", 1)[1].strip().strip('"')
    if not k:
        sys.exit("EDINET_API_KEY が未設定（環境変数か companies/.env）。キーは EDINET API 利用申請で発行（無料・本人登録）。")
    return k


def _get(url: str, params: dict, *, binary: bool = False) -> bytes:
    params = {**params, "Subscription-Key": _key()}
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "curl/8.7.1", "Accept": "*/*"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = r.read()
            time.sleep(WAIT_SEC)
            return body
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise
            time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 ** attempt)
    raise RuntimeError(f"取得失敗: {full.replace(_key(), '***')}")


def list_docs(day: dt.date) -> list[dict]:
    p = CACHE / "list" / f"{day}.json"
    if p.exists():
        return json.loads(p.read_text())["results"]
    body = _get(f"{API}/documents.json", {"date": day.isoformat(), "type": 2})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return json.loads(body)["results"]


def fetch_zip(doc_id: str) -> Path:
    p = CACHE / "xbrl" / f"{doc_id}.zip"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_get(f"{API}/documents/{doc_id}", {"type": 1}, binary=True))
    return p


def is_yuho(d: dict) -> bool:
    # 企業内容等の開示に関する内閣府令（010）・有価証券報告書（030000）・XBRL あり・訂正は除く
    return d.get("ordinanceCode") == "010" and d.get("formCode") == "030000" and d.get("xbrlFlag") == "1"


# ---------------------------------------------------------------- XBRL

def parse_instance(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.startswith("XBRL/PublicDoc/") and n.endswith(".xbrl")]
        if len(names) != 1:
            raise RuntimeError(f"{zip_path.name}: PublicDoc の .xbrl が {len(names)} 個（1 個のはず）")
        root = etree.fromstring(z.read(names[0]))
    # contexts: id -> (period, dims)
    ctx = {}
    for c in root.iter(f"{{{XBRLI}}}context"):
        dims = {}
        for m in c.iter(f"{{{XBRLDI}}}explicitMember"):
            dims[m.get("dimension")] = m.text
        per = c.find(f"{{{XBRLI}}}period")
        inst = per.findtext(f"{{{XBRLI}}}instant")
        period = inst if inst else f"{per.findtext(f'{{{XBRLI}}}startDate')}/{per.findtext(f'{{{XBRLI}}}endDate')}"
        ctx[c.get("id")] = (period, dims)
    facts = []
    for el in root.iter():
        cref = el.get("contextRef")
        if cref is None or not isinstance(el.tag, str):
            continue
        ns, local = el.tag[1:].split("}")
        prefix = root.nsmap and next((k for k, v in root.nsmap.items() if v == ns), ns)
        text = (el.text or "") if len(el) == 0 else etree.tostring(el, method="text", encoding="unicode")
        facts.append({
            "prefix": prefix, "name": local, "context": cref, "unit": el.get("unitRef"),
            "decimals": el.get("decimals"), "nil": el.get(f"{{{XBRLI}}}nil") == "true",
            "value": text.strip(),
        })
    return {"contexts": ctx, "facts": facts}
