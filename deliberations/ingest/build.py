"""
検索の単位への組み立て：目録の全文書を解析し、文書ごとの結果（documents.jsonl）と検索の単位（units.jsonl）を書く。

    python -m deliberations.ingest.build          # data/raw/manifest.jsonl → data/cache/{documents,units}.jsonl

単位（開発計画 §4.2）：スライド・文章の資料は 1 ページ、記録は 1 発言（逐語）・同じ発言者の箇条のまとまり
（名前つき要約）・1 箇条（匿名の要約）・書面の発言要旨 1 件・地の文 1 文。上限を超える発言は文の切れ目で分け、
発言者を引き継ぐ。単位 ID は文書・単位の種類・連番から作る決定的な uuid5（取り込み直しても変わらない）。
"""
import json
import re
import uuid
from collections import Counter

from deliberations.core.paths import CACHE_DIR, DOCUMENTS, MANIFEST, UNITS
from polyarchy_common.metadata_core import validate_payload
from deliberations.ingest import records
from deliberations.ingest.attribution import clean_name, material_presenter, speaker_role
from deliberations.ingest.extract import slide_pages
from deliberations.ingest.sources import BY_ORG

PAGE_MAX = 800    # これを超えるページ（文章主体の資料）は段落・文の切れ目で分ける（開発計画 §4.2＝文章の資料は意味段落）
PART_CHARS = 600
NS = uuid.UUID("6f1d3c2a-0d1e-5b7a-9a52-2b7f0c4e8d10")  # 審議会議事録DB の単位 ID の名前空間（変えない）
KIND_OF_MODE = {"verbatim": "議事録", "written": "議事録", "named": "議事要旨", "anonymous": "議事要旨", "narration": "議事要旨"}


LICENSE_PDL = "PDL1.0"            # 公共データ利用規約 第 1.0 版（内閣官房・内閣府のサイトの規約）
LICENSE_THIRD = "第三者の著作物"  # 構成員・外部の提出資料＝PDL1.0 §1.2 の対象外として扱う（deliberations/docs/再配布条件.md）
THIRD_PARTY = ("構成員（政府外）", "外部（ヒアリング）")


def license_of(presenter_type: str) -> str:
    """資料の提出者区分 → 利用条件。記録（議事録・議事要旨）は政府が作成・公開する文書＝PDL1.0。"""
    return LICENSE_THIRD if presenter_type in THIRD_PARTY else LICENSE_PDL


def unit_id(path: str, kind: str, n: int) -> str:
    return str(uuid.uuid5(NS, f"{path}#{kind}#{n}"))


def base_meta(row: dict) -> dict:
    """共通コア 8 欄（polyarchy_common.metadata_core）＋審議会議事録DB の拡張（開発計画 §4.1）。"""
    src = BY_ORG[row["org"]]
    date = row.get("date") or ""
    title = clean_name(row.get("material_name", "")) or row.get("title", "")
    return {"corpus": "deliberations", "org": row["org"], "title": f"{src.name}（第{row['session_no']}回）{title}",
            "date": date, "date_int": int(date.replace("-", "")) if date else None,
            "org_name": src.name, "ministry": src.ministry,
            "session_no": row["session_no"], "mochimawari": row.get("mochimawari", False),
            "material_no": row.get("material_no", ""), "material_name": row.get("material_name", ""),
            "source_url": row["source_url"], "path": row["path"], "layer": "公開", "lang": "ja"}


def split_page(text: str) -> list[str]:
    """長いページを行（段落）の切れ目で PART_CHARS 程度に分ける。1 行が長ければ文（。）で分ける。"""
    pieces = []
    for ln in text.split("\n"):
        if len(ln) <= PART_CHARS:
            pieces.append(ln)
        else:
            pieces += [x for x in re.split(r"(?<=。)", ln) if x]
    parts, buf = [], ""
    for x in pieces:
        if buf and len(buf) + len(x) > PART_CHARS:
            parts.append(buf)
            buf = ""
        buf += (("\n" if buf else "") + x)
    if buf:
        parts.append(buf)
    return parts


def build_material(row: dict) -> tuple[dict, list[dict]]:
    src = BY_ORG[row["org"]]
    pr = material_presenter(row, src)
    pages = slide_pages(row["path"])
    doc = {**base_meta(row), "doc_kind": row["doc_kind"], "presenter_type": pr.presenter_type, "presenter": pr.presenter,
           "presenter_basis": pr.basis, "presenter_rule": pr.rule, "page_presenters": pr.page_presenters,
           "origin_body": pr.origin_body, "origin_basis": pr.origin_basis, "pages": len(pages),
           "license": license_of(pr.presenter_type),
           "sparse_pages": sum(p.quality == "sparse" for p in pages)}
    units = []
    for pg in pages:
        pres = pr.page_presenters.get(str(pg.no)) if pr.page_presenters else None
        parts = split_page(pg.text) if pg.chars > PAGE_MAX else [pg.text]
        for k, part in enumerate(parts, 1):
            uid = unit_id(row["path"], "page", pg.no) if len(parts) == 1 else unit_id(row["path"], "page", f"{pg.no}-{k}")
            units.append({**base_meta(row), "id": uid, "unit": "page", "page": pg.no, "part": k if len(parts) > 1 else 0,
                          "doc_kind": row["doc_kind"], "presenter_type": pr.presenter_type,
                          "presenter": pres or pr.presenter, "presenter_basis": pr.basis,
                          "license": license_of(pr.presenter_type),
                          "speaker": "", "speaker_role": "", "mode": "", "text": part,
                          "text_quality": pg.quality, "chars": len(re.sub(r"\s", "", part))})
    return doc, units


def build_record(row: dict) -> tuple[dict, list[dict]]:
    src = BY_ORG[row["org"]]
    us, head = records.parse(row["path"])
    head_text = "".join(s for _, s in head)
    us = [x for u in records.merge_named(us) for x in records.split_long(u)]
    rtype = records.record_type(us)
    doc = {**base_meta(row), "doc_kind": "議事録" if rtype == "逐語" else "議事要旨", "record_type": rtype,
           "license": LICENSE_PDL,
           "units": len(us), "modes": dict(Counter(u.mode for u in us))}
    units = []
    for n, u in enumerate(us, 1):
        role = speaker_role(u.speaker, src, head_text)
        units.append({**base_meta(row), "id": unit_id(row["path"], "utt", n), "unit": "utterance", "page": u.start_page,
                      "end_page": u.end_page, "doc_kind": doc["doc_kind"], "presenter_type": "", "presenter": "",
                      "presenter_basis": "", "license": LICENSE_PDL,
                      "speaker": u.speaker, "speaker_role": role, "mode": u.mode,
                      "text": u.text, "text_quality": "ok", "chars": len(re.sub(r"\s", "", u.text))})
    return doc, units


def main() -> None:
    rows = [json.loads(l) for l in MANIFEST.open(encoding="utf-8")]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    docs, units = [], []
    for row in rows:
        if not row.get("path") or not row["path"].lower().endswith(".pdf"):
            continue
        if row["doc_kind"] == "議事次第":  # 議事次第の PDF（議題の一覧）は検索の単位にしない＝回のページと list_meeting で足りる
            continue
        d, u = (build_record if row["doc_kind"] == "記録" else build_material)(row)
        docs.append(d)
        units += u
    with DOCUMENTS.open("w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    with UNITS.open("w", encoding="utf-8") as f:
        for u in units:
            f.write(json.dumps(u, ensure_ascii=False) + "\n")
    ids = [u["id"] for u in units]
    assert len(ids) == len(set(ids)), "単位 ID が重複"
    errs = [(u["id"], e) for u in units for e in validate_payload(u)]
    assert not errs, f"共通コアの違反 {len(errs)} 件: {errs[:3]}"
    print(f"[build] 文書 {len(docs)}・単位 {len(units)} → {UNITS}")
    print("  提出者区分（資料）:", dict(Counter(d["presenter_type"] for d in docs if "presenter_type" in d)))
    print("  根拠（資料）:", dict(Counter(d["presenter_basis"] for d in docs if "presenter_basis" in d)))
    print("  発言者区分（発言）:", dict(Counter(u["speaker_role"] for u in units if u["unit"] == "utterance")))
    print("  記録の型:", dict(Counter(d["record_type"] for d in docs if "record_type" in d)))
    print("  文字の少ないページ:", sum(u["text_quality"] == "sparse" for u in units), "/", sum(u["unit"] == "page" for u in units))


if __name__ == "__main__":
    main()
