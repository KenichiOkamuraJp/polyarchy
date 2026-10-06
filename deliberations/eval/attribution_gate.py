"""
帰属の正しさのゲート（開発計画 §6）：M0 で確定した帰属ラベルと、取り込みの自動付与を突き合わせる。

    python -m deliberations.eval.attribution_gate     # 先に python -m deliberations.ingest.build

判定＝**取り違え 0 件**（自動が「不明」なのは可・ラベルと違う区分や発言者を付けたら不可）。あわせて
fail-closed（匿名の要約の単位に発言者が出ていない・付けた発言者の名前が記録の中に書かれている）を全件で見る。
アンカー問（M0）の発言者つきの問は、根拠の引用を含む単位の発言者を確かめる（C 型は不明であること）。
"""
import json
import re
import sys
import unicodedata
from collections import defaultdict

from deliberations.core.paths import DOCUMENTS, EVAL_DIR, UNITS
from deliberations.ingest.extract import record_lines

UNKNOWN = "不明"


def n(s: str) -> str:
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", s or ""))


def load():
    docs = {d["path"]: d for d in map(json.loads, DOCUMENTS.open(encoding="utf-8"))}
    units = defaultdict(list)
    for u in map(json.loads, UNITS.open(encoding="utf-8")):
        units[u["path"]].append(u)
    labels = json.load(open(EVAL_DIR / "attribution_labels.json", encoding="utf-8"))
    anchors = json.load(open(EVAL_DIR / "anchor_questions.json", encoding="utf-8"))
    return docs, units, labels, anchors


def find_unit(units: list[dict], quote: list[str], speaker: str = "", page: int | None = None) -> dict | None:
    """引用の行を含む単位。記号と発言者名を除いた断片の長いものから、ただ 1 つの単位に当たるものを使う。
    引用が見出しだけ（断片が 10 字未満）のときは、そのページにかかる単位のうち発言者がラベルと同じもの。"""
    frags = []
    for q in quote:
        frag = re.sub(r"^[○〇・･]", "", n(q))
        if speaker and frag.startswith(n(speaker)):
            frag = frag[len(n(speaker)):]
        frag = re.sub(r"^【[^】]*】|^\([^()]{1,30}\)", "", frag)  # 見出し【名前】・デジタル庁の議事要旨の(名前)
        if len(frag) >= 10:
            frags.append(frag)
    for frag in sorted(frags, key=len, reverse=True):
        hits = [u for u in units if frag[:40] in n(u["text"])]
        if len(hits) == 1:
            return hits[0]
    if page is not None and speaker:
        on = [u for u in units if u.get("page", 0) <= page <= (u.get("end_page") or u.get("page", 0))]
        same = [u for u in on if n(u["speaker"]) == n(speaker)]
        if same:
            return same[0]
        if on:
            return {**on[0], "speaker": "／".join(sorted({u["speaker"] for u in on}))}
    return None


def main() -> int:
    docs, units, labels, anchors = load()
    bad, unknown, ok = [], [], 0

    def judge(kind, lid, auto, want):
        nonlocal ok
        if n(auto) == n(want):
            ok += 1
        elif not auto or auto == UNKNOWN:
            unknown.append(f"{kind} {lid}: 自動＝不明（ラベル＝{want}）")
        else:
            bad.append(f"{kind} {lid}: 自動＝{auto}／ラベル＝{want}")

    for m in labels["materials"]:
        d = docs.get(m["path"])
        if not d:
            bad.append(f"資料 {m['id']}: 文書が無い {m['path']}")
            continue
        judge("資料の区分", m["id"], d["presenter_type"], m["presenter_type"])
        if m.get("presenter_basis") != "既定" and m["presenter"] not in (UNKNOWN, "ページごと（page_presenters）"):
            judge("資料の提出者", m["id"], d["presenter"], m["presenter"])
        for pg, who in (m.get("page_presenters") or {}).items():
            if who.startswith("（"):
                continue
            judge("ページの提出者", f"{m['id']} p{pg}", (d.get("page_presenters") or {}).get(pg, UNKNOWN), who)

    for u in labels["utterances_named"]:
        hit = find_unit(units[u["path"]], u["quote"], u["speaker"], u.get("page"))
        if not hit:
            bad.append(f"発言 {u['id']}: 引用を含む単位が無い")
            continue
        judge("発言者", u["id"], hit["speaker"], u["speaker"])
        judge("発言者の区分", u["id"], hit["speaker_role"], u["speaker_role"])

    for u in labels["utterances_anonymous"]:
        hit = find_unit(units[u["path"]], u["quote"])
        if not hit:
            bad.append(f"匿名 {u['id']}: 引用を含む単位が無い")
        elif hit["speaker"] != UNKNOWN:
            bad.append(f"匿名 {u['id']}: 発言者を付けた（{hit['speaker']}）＝fail-closed 違反")
        else:
            ok += 1

    for q in anchors["questions"]:
        e = q["expected"]
        if not e.get("speaker") or "acceptable_paths" in e:
            continue
        ev = q["evidence"][0]
        hit = find_unit(units[ev["path"]], ev["quote"], e["speaker"])
        if not hit:
            bad.append(f"問 {q['id']}: 根拠の引用を含む単位が無い")
            continue
        judge("問の発言者", q["id"], hit["speaker"], e["speaker"])

    # fail-closed（全件）：匿名の単位に名前なし・付けた名前は記録に書かれている
    fc = 0
    texts = {}
    for path, us in units.items():
        for u in us:
            if u["unit"] != "utterance":
                continue
            if u["mode"] == "anonymous" and u["speaker"] != UNKNOWN:
                bad.append(f"fail-closed {path}: 匿名の単位に発言者 {u['speaker']}")
            if u["speaker"] != UNKNOWN:
                if path not in texts:
                    texts[path] = n("".join(s for _, s in record_lines(path)))
                if n(u["speaker"]) not in texts[path]:
                    bad.append(f"fail-closed {path}: 記録に無い名前 {u['speaker']}")
            fc += 1

    print(f"[帰属ゲート] 一致 {ok}・自動が不明 {len(unknown)}・取り違え {len(bad)}（全単位の fail-closed 検査 {fc} 件）")
    for line in unknown:
        print("  （不明）", line)
    for line in bad:
        print("  ✗", line)
    print("PASS" if not bad else "FAIL")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
