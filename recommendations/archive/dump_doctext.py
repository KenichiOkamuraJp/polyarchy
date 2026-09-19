"""本番コレクションの全チャンクを file_name 別に連結して per-doc テキストを出力する。

評価セット起草の読み物 兼 evalset_gate.py（keyword-in-source ゲート）の照合ソース。
出力は data/eval/doctext/（コレクションから数秒で再生成できる派生物）。

使い方: python -m recommendations.ingest.dump_doctext
"""
import csv
from collections import defaultdict
from pathlib import Path

import chromadb

from recommendations.core.config import CHROMA_DIR, COLLECTION_NAME

OUT = Path(__file__).resolve().parents[1] / "data" / "eval" / "doctext"
OUT.mkdir(parents=True, exist_ok=True)

client = chromadb.PersistentClient(path=str(CHROMA_DIR))
col = client.get_collection(COLLECTION_NAME)
n = col.count()
print(f"{COLLECTION_NAME}: {n} chunks")

texts = defaultdict(list)
BATCH = 2000
for off in range(0, n, BATCH):
    r = col.get(include=["documents", "metadatas"], limit=BATCH, offset=off)
    for doc, meta in zip(r["documents"], r["metadatas"]):
        fn = meta.get("file_name") or "UNKNOWN"
        texts[fn].append(doc)

rows = []
for fn, chunks in sorted(texts.items()):
    body = "\n".join(chunks)
    (OUT / (fn + ".txt")).write_text(body, encoding="utf-8")
    rows.append((fn, len(chunks), len(body)))

with open(OUT / "manifest.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["file_name", "chunks", "chars"])
    w.writerows(rows)

print(f"docs={len(rows)} -> {OUT}")
