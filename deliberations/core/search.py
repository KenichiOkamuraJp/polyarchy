"""
検索サービス：search（ハイブリッド検索→リランク）と list_meeting（1 つの回の資料と記録の一覧＝決定論の参照）。

部品は共有ライブラリ polyarchy_retrieval（分かち書き・BM25・RRF・Qdrant ストア・モデル）。本モジュールが
持つのは審議会議事録DB の固有部分＝絞り込みの意味（会議体・期間・文書の種類・区分）と、返り値の帰属と注記。

層は公開固定（引数に layer を持たない）。返す断片には毎回、会議体・回次・開催日・資料番号・提出者／発言者と
区分・出典 URL を付け、構成員（政府外）・外部・不明の断片には「会議・政府の決定ではない」の注記を付ける
（説明文ではなく返り値に置く＝説明文を変えるとコネクタの作り直しが要るため）。
"""
import json
from dataclasses import dataclass, field

from qdrant_client import models

from deliberations.core import config
from deliberations.core.paths import DOCUMENTS, MANIFEST
from polyarchy_common.logsetup import get_logger
from polyarchy_retrieval.bm25 import BM25Index, QdrantBM25
from polyarchy_retrieval.hybrid import HybridSearcher
from polyarchy_retrieval.models import HF_EMBEDDING_MODELS, RERANKERS
from polyarchy_retrieval.qdrant import QdrantCorpusStore

log = get_logger("polyarchy.deliberations.search")

ROLES = ("政務", "事務局", "府省・会議体", "構成員（政府外）", "外部（ヒアリング）", "不明")
DOC_KINDS = ("資料", "参考資料", "議事録", "議事要旨")
NOT_DECISION = "提出者・発言者の見解であり、会議・政府の決定ではない。"
NOTES = {
    "構成員（政府外）": NOT_DECISION,
    "外部（ヒアリング）": NOT_DECISION,
    "不明": "提出者・発言者は記録・資料に書かれていない（推定しない）。会議・政府の決定ではない。",
}
MODE_NOTES = {
    "written": "欠席者が書面で提出した発言要旨（会議の場の発言ではない）。",
    "narration": "記録の地の文（議事要旨の要約の文）。",
    "anonymous": "記録に発言者の名前が無い（匿名の要約）。",
}


@dataclass(frozen=True)
class DelibFilter:
    orgs: tuple[str, ...] = ()
    date_from: int | None = None
    date_to: int | None = None
    doc_kinds: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    layers: tuple[str, ...] = config.DEFAULT_LAYERS

    def qdrant(self) -> models.Filter:
        must = [models.FieldCondition(key="layer", match=models.MatchAny(any=list(self.layers)))]
        for key, vals in (("org", self.orgs), ("doc_kind", self.doc_kinds), ("role", self.roles)):
            if vals:
                must.append(models.FieldCondition(key=key, match=models.MatchAny(any=list(vals))))
        if self.date_from is not None or self.date_to is not None:
            must.append(models.FieldCondition(key="date_int", range=models.Range(gte=self.date_from, lte=self.date_to)))
        return models.Filter(must=must)

    def matches(self, m: dict) -> bool:
        if m.get("layer") not in self.layers:
            return False
        for key, vals in (("org", self.orgs), ("doc_kind", self.doc_kinds), ("role", self.roles)):
            if vals and m.get(key) not in vals:
                return False
        di = m.get("date_int")
        if self.date_from is not None and not (isinstance(di, int) and di >= self.date_from):
            return False
        if self.date_to is not None and not (isinstance(di, int) and di <= self.date_to):
            return False
        return True


@dataclass
class Hit:
    score: float
    meta: dict
    notes: list[str] = field(default_factory=list)

    def to_dict(self, text_chars: int = 800) -> dict:
        m = self.meta
        out = {
            "org": m["org"], "meeting": m["org_name"], "session_no": m["session_no"], "date": m["date"],
            "doc_kind": m["doc_kind"], "material_no": m.get("material_no", ""),
            "title": m["title"], "page": m.get("page"),
            "role": m["role"], "who": m["who"],
            "source_url": m["source_url"] + (f"#page={m['page']}" if m.get("page") and m["unit"] == "page" else ""),
            "text": (m["body"][:text_chars] + ("…" if len(m["body"]) > text_chars else "")) if text_chars else "",
            "license": m.get("license", ""),
            "score": round(self.score, 4),
        }
        if m["unit"] == "utterance":
            out["speaker"], out["speaker_role"], out["mode"] = m["speaker"], m["speaker_role"], m["mode"]
            if m.get("end_page") and m["end_page"] != m.get("page"):
                out["end_page"] = m["end_page"]
        else:
            out["presenter"], out["presenter_type"] = m["presenter"], m["presenter_type"]
            out["presenter_basis"] = m.get("presenter_basis", "")
            if m.get("text_quality") == "sparse":
                out["text_quality"] = "sparse"
        out["notes"] = self.notes
        return out


def notes_for(m: dict) -> list[str]:
    ns = []
    if m.get("role") in NOTES:
        ns.append(NOTES[m["role"]])
    if m.get("mode") in MODE_NOTES:
        ns.append(MODE_NOTES[m["mode"]])
    if m.get("presenter_basis") == "既定":
        ns.append("資料に提出者の記載が無い（会議資料の慣行により事務局として扱う）。")
    if m.get("text_quality") == "sparse":
        ns.append("このページは文字がほとんど取れない（図・画像中心）＝内容は原文で確認。")
    if m.get("license") == "第三者の著作物":
        ns.append("提出者が権利を持つ資料＝所在の案内のための抜粋（引用するときは出典を示し、全文は原文で）。")
    return ns


class DeliberationsSearch:
    def __init__(self, collection: str = config.COLLECTION, url: str = config.QDRANT_URL):
        self.collection = collection
        self.store = QdrantCorpusStore({collection: collection}, url=url)
        g = self.store.get_all(collection)
        self.text = dict(zip(g["ids"], g["documents"]))
        self.meta = dict(zip(g["ids"], g["metadatas"]))
        try:
            sparse = QdrantBM25(self.store, collection, config.VOCAB_DIR / f"{collection}_vocab.json.gz")
        except FileNotFoundError as e:
            # 語彙サイドカーの無いコレクション（層ゲートの一時コレクション等）＝in-memory 版。本番で出たら束の復元漏れ
            log.warning("BM25 語彙が無いため in-memory 版で代替（絞り込みの候補の取り方が本番と違う）: %s", e)
            sparse = BM25Index(g["ids"], g["documents"], [m.get("path", "") for m in g["metadatas"]])
        self.searcher = HybridSearcher(self.store, collection, sparse, self.meta, config.HYBRID_KV,
                                       config.HYBRID_KB, widen=config.FILTER_WIDEN, exact=config.QDRANT_EXACT)
        self.embed = HF_EMBEDDING_MODELS[config.EMBEDDING]()
        self.rerank = RERANKERS[config.RERANKER]()
        log.info("審議会議事録DB 検索の初期化: %s %d 点", collection, len(self.meta))

    @property
    def count(self) -> int:
        return len(self.meta)

    def search(self, query: str, flt: DelibFilter | None = None, top_k: int = config.TOP_K,
               per_doc: int = 0, per_third_doc: int = config.PER_THIRD_DOC) -> list[Hit]:
        """per_doc>0 なら同じ文書から返す件数の上限（0＝上限なし）。per_third_doc＝第三者の著作物（構成員・外部の
        提出資料）の同じ文書から返す上限＝所在検索に伴う軽微な利用の担保（deliberations/docs/再配布条件.md）。"""
        flt = flt or DelibFilter()
        qemb = self.embed.get_query_embedding(query)
        ids = self.searcher.fused_ids(qemb, query, flt.qdrant(), flt.matches, needs_post=False)[: config.POOL_K]
        if not ids:
            return []
        scores = self.rerank(query, [self.text[i] for i in ids])
        ranked = sorted(zip(ids, scores), key=lambda x: x[1], reverse=True)
        out, per = [], {}
        for cid, s in ranked:
            m = self.meta[cid]
            if m.get("layer") not in config.DEFAULT_LAYERS:  # 境界の二重の守り（公開固定）
                continue
            k = m["path"]
            if per_doc and per.get(k, 0) >= per_doc:
                continue
            if per_third_doc and m.get("license") == "第三者の著作物" and per.get(k, 0) >= per_third_doc:
                continue
            per[k] = per.get(k, 0) + 1
            out.append(Hit(float(s), m, notes=notes_for(m)))
            if len(out) >= top_k:
                break
        return out


def list_meeting(org: str, session_no: int) -> dict | None:
    """1 つの回の資料と記録の一覧（目録と解析結果から・検索ではない）。無ければ None。"""
    rows = [r for r in map(json.loads, MANIFEST.open(encoding="utf-8"))
            if r["org"] == org and r["session_no"] == session_no]
    if not rows:
        return None
    docs = {d["path"]: d for d in map(json.loads, DOCUMENTS.open(encoding="utf-8"))
            if d["org"] == org and d["session_no"] == session_no}
    page = next((r for r in rows if r["doc_kind"] == "議事次第ページ"), rows[0])
    items = []
    for r in rows:
        if r["doc_kind"] == "議事次第ページ":
            continue
        d = docs.get(r.get("path") or "", {})
        it = {"material_no": r.get("material_no", ""), "name": r["material_name"],
              "doc_kind": d.get("doc_kind", r["doc_kind"]), "public": r.get("public", True),
              "source_url": r["source_url"] if r.get("path") else ""}
        if d.get("record_type"):
            it["record_type"] = d["record_type"]
        if d.get("presenter_type"):
            it.update(presenter_type=d["presenter_type"], presenter=d["presenter"],
                      presenter_basis=d["presenter_basis"])
            if d.get("page_presenters"):
                it["page_presenters"] = d["page_presenters"]
            if d.get("origin_body"):
                it["origin_body"] = d["origin_body"]
        if not r.get("public", True):
            it["note"] = "非公開（会議の一覧に名前だけが載っている）"
        elif r.get("superseded"):
            it["note"] = "同じ回の議事録（逐語）を検索の対象にしている＝この議事要旨は一覧にだけ載せる"
        items.append(it)
    return {"org": org, "session_no": session_no, "date": page.get("date"), "title": page.get("title"),
            "mochimawari": page.get("mochimawari", False), "page_url": page["source_url"], "items": items}


def coverage() -> dict:
    """収録範囲（目録と解析結果から数える）＝会議体ごとの回・期間・資料と記録の数・非公開・文字の少ないページ。"""
    from deliberations.ingest.sources import BY_ORG
    rows = list(map(json.loads, MANIFEST.open(encoding="utf-8")))
    docs = list(map(json.loads, DOCUMENTS.open(encoding="utf-8")))
    out = {}
    for org, src in BY_ORG.items():
        rs = [r for r in rows if r["org"] == org]
        if not rs:
            continue
        ds = [d for d in docs if d["org"] == org]
        sess = sorted({r["session_no"] for r in rs})
        dates = sorted(r["date"] for r in rs if r.get("date"))
        out[org] = {
            "name": src.name, "ministry": src.ministry, "batch": src.batch,
            "sessions": [sess[0], sess[-1]], "session_count": len(sess),
            "mochimawari": sorted({r["session_no"] for r in rs if r.get("mochimawari")}),
            "period": [dates[0], dates[-1]] if dates else [],
            "materials": sum(1 for d in ds if d.get("doc_kind") == "資料"),
            "references": sum(1 for d in ds if d.get("doc_kind") == "参考資料"),
            "records": {t: sum(1 for d in ds if d.get("record_type") == t)
                        for t in ("逐語", "名前つき要約", "匿名要約", "発言の記録なし")},
            "nonpublic": sum(1 for r in rs if not r.get("public", True)),
            "third_party_docs": sum(1 for d in ds if d.get("license") == "第三者の著作物"),
            "sparse_pages": sum(d.get("sparse_pages", 0) for d in ds),
            "pages": sum(d.get("pages", 0) for d in ds),
        }
    return out


def coverage_note() -> str:
    parts = [f"{c['name']}（第{c['sessions'][0]}〜{c['sessions'][1]}回・{c['period'][0]}〜{c['period'][1]}）"
             for c in coverage().values() if c["period"]]
    return ("該当なし。収録＝" + "・".join(parts) + "の公開の配布資料と記録。会議の決定文書は政策主張DB"
            "（search_policy_docs）。上の会議体以外の審議会・会議（規制改革推進会議の本会議・ほかの WG など）は未収録。")

