"""
帰属：資料の提出者区分と、発言者の区分（開発計画 §4.1・2026-10-03 改定＝その会議での役割）。

区分＝政務／事務局／府省・会議体／構成員（政府外）／外部（ヒアリング）／不明。推定しない＝記録・資料に
書かれた文字列（資料名・表紙・本文のページ表示・開催要領の出席者欄）からだけ決める。

資料の提出者の探し方：資料名 → 表紙の提出者の行 → 本文（作成者の表明・ページごとの「○○提出資料」）
→ どこにも無ければ会議資料の慣行により事務局（presenter_basis＝既定）。資料名が「…提出資料」なのに
型に当たらないものは既定にせず不明（誰かが出したと書いてある＝事務局ではない）。
"""
import re
from dataclasses import dataclass, field

from deliberations.ingest.extract import page_lines, slide_pages
from deliberations.ingest.sources import Source

UNKNOWN = "不明"
SEIMU = "政務"
JIMU = "事務局"
FUSHO = "府省・会議体"
KOSEI = "構成員（政府外）"
GAIBU = "外部（ヒアリング）"

SEIMU_RE = re.compile(r"(内閣総理大臣|総理大臣|総理|大臣政務官|政務官|副大臣|大臣|内閣官房長官|官房長官)(\(代理\))?$|大臣|国家公安委員会委員長")
KOSEI_RE = re.compile(r"(構成員|委員|座長代理|座長|主査)$")
OFFICIAL_RE = re.compile(r"(統括官|審議官|参事官|事務局長|局長|次長|室長|部長|課長)$")
BODY_RE = re.compile(r"(省|庁|府|事務局|推進室|委員会|会議|研究会|本部|審議会)$")
EXTERNAL_RE = re.compile(r"株式会社|有限会社|代表取締役|取締役|社長|理事長|会長|協会|連合会|会議所|財団|社団|大学|機構|法人")
DATE_RE = re.compile(r"(\d{4}|令和\s*(\d+|元)|[○〇]+)\s*年\s*[\d○〇]+\s*月(\s*[\d○〇]+\s*日)?")


def _ns(s: str) -> str:
    return re.sub(r"\s", "", s or "")


def _is_secretariat(name: str, src: Source) -> bool:
    """その会議の事務局か：記録で確かめた事務局の名を含む、または会議名を含んで「事務局」で終わる。"""
    n = _ns(name)
    return any(_ns(x) in n for x in src.secretariat) or (_ns(src.name) in n and n.endswith("事務局"))


# ── 発言者の区分 ───────────────────────────────────────────────────────────

def speaker_role(speaker: str, src: Source, head_text: str = "") -> str:
    """発言者（記録の見出しに書かれたとおり）→ 区分。head_text＝本文より前の出席者欄（開催要領）。"""
    s = _ns(speaker)
    if not s or s == UNKNOWN:
        return UNKNOWN
    if "国家公安委員会委員長" in s or SEIMU_RE.search(s):
        return SEIMU
    if re.search(r"(会議|本部|審議会)議長(代理)?$", s):  # 「…会議議長」＝別の会議体の代表
        return FUSHO
    if re.search(r"議長(代理)?$", s):
        # 議長の肩書を出席者欄で読む：別の会議体の議長（「…会議議長」）は会議体の代表、総理なら政務
        sur = re.sub(r"議長(代理)?$", "", s)
        m = re.search(re.escape(sur) + r".{0,8}?([^\s]{0,30}?(会議|本部|審議会)議長(代理)?)", _ns(head_text)) if sur else None
        if m:
            return FUSHO
        return UNKNOWN
    if KOSEI_RE.search(s):
        return KOSEI
    if OFFICIAL_RE.search(s):  # 官職＝その会議の事務局の名が付いていれば事務局、それ以外は府省
        if _is_secretariat(s, src):
            return JIMU
        # 「濱野事務局長」のように所属が無いときは、出席者欄で同じ姓の肩書を読む
        sur = OFFICIAL_RE.sub("", s)
        m = re.search(re.escape(sur) + r".{0,6}?([^\s]{0,40}?" + OFFICIAL_RE.pattern.rstrip("$") + ")", _ns(head_text)) if sur else None
        return JIMU if m and _is_secretariat(m.group(1), src) else FUSHO
    if s == "事務局" or _is_secretariat(s, src):
        return JIMU
    if EXTERNAL_RE.search(s):
        return GAIBU
    if BODY_RE.search(s):
        return FUSHO
    return UNKNOWN


# ── 資料の提出者 ───────────────────────────────────────────────────────────

@dataclass
class Presenter:
    presenter_type: str
    presenter: str
    basis: str            # 資料名／表紙／本文／既定／なし
    rule: str
    page_presenters: dict[str, str] = field(default_factory=dict)
    origin_body: str = ""
    origin_basis: str = ""
    evidence: str = ""


def clean_name(name: str) -> str:
    return re.sub(r"\s*[（(]PDF[^）)]*[）)]\s*$", "", name or "").strip()


def by_name(name: str) -> Presenter | None:
    n = _ns(clean_name(name))
    rules = [
        (r"^(.*?国家公安委員会委員長)提出資料", SEIMU, "「…国家公安委員会委員長提出資料」"),
        (r"^(.+?大臣)(提出)?資料", SEIMU, "「…大臣提出資料」"),
        (r"^(構成員)提出資料", KOSEI, "「構成員提出資料」（どの構成員かはページごと）"),
        (r"^(.+?(構成員|委員))(提出)?資料", KOSEI, "「…構成員・委員（提出）資料」"),
        (r"^(.+?座長(代理)?)(提出)?資料", KOSEI, "「…座長資料」"),
        (r"^(事務局)", JIMU, "「事務局…」"),
        (r"^(.+?(省|庁))提出資料", FUSHO, "「…省・庁提出資料」"),
        (r"^(.+?(会議|本部|研究会|審議会))提出資料", FUSHO, "「…会議提出資料」＝別の政府の会議体"),
        (r"^(.*?(株式会社|法人|協会|連合会|会議所|大学|財団|機構).*?)提出資料", GAIBU, "「法人・団体名＋提出資料」"),
    ]
    for pat, typ, rule in rules:
        if m := re.match(pat, n):
            pres = m.group(1)
            return Presenter(typ, pres if pres != "構成員" else UNKNOWN, "資料名", rule)
    if re.match(r"^(.+?)提出資料$", n):
        return Presenter(UNKNOWN, re.sub(r"提出資料$", "", n), "資料名", "「…提出資料」だが区分の型に当たらない＝既定にしない")
    return None


def _classify_line(line: str, src: Source) -> tuple[str, str] | None:
    s = _ns(line)
    if not s or re.match(r"^(参考)?資料[\d\-－の・]*$", s):
        return None
    if ":" in s or re.match(r"^(議長|副議長|本部長|副本部長|構成員|座長|委員)", s):
        return None  # 名簿・組織図の役割の行（提出者の行ではない）
    if "大臣" in s and len(s) <= 30 and not re.search(r"[、。()「」]", s):
        return SEIMU, line.strip()
    if "事務局" in s and len(s) <= 40:
        return (JIMU if _is_secretariat(s, src) else FUSHO), line.strip()
    if re.search(r"(省|庁)$", s) and len(s) <= 20:
        return FUSHO, line.strip()
    if re.search(r"(研究会|会議|本部|審議会)$", s) and len(s) <= 40:
        return FUSHO, line.strip()
    return None


def _is_meeting_line(line: str, src: Source) -> bool:
    s = _ns(line)
    return bool(re.fullmatch(r"(第\S{1,4}回)?" + re.escape(_ns(src.name)), s))


def by_cover(path: str, src: Source) -> Presenter | None:
    """表紙（1 ページ目）の提出者の行：日付の行の後で、会議名だけの行を除いた最初の提出者の行。
    日付の無い 1 枚資料は、ページの中の「…大臣（氏名）」だけの行。"""
    lines = page_lines(path, 1)
    d = next((i for i, ln in enumerate(lines) if DATE_RE.fullmatch(_ns(ln)) or
              (DATE_RE.search(_ns(ln)) and len(_ns(ln)) <= 16)), None)
    cands = lines[d + 1: d + 5] if d is not None else [ln for ln in lines if "大臣" in ln and len(_ns(ln)) <= 30]
    # 表紙の中の組織名だけの短い行（【連絡先】の下の発表元など）も候補に足す（日付の後の行を優先）
    cands += [ln for ln in lines if ln not in cands and len(_ns(ln)) <= 30 and not re.search(r"[、。,()「」:]", ln)
              and re.search(r"(事務局|省|庁)$", _ns(ln))]
    for i, ln in enumerate(cands):
        if _is_meeting_line(ln, src):
            continue
        if c := _classify_line(ln, src):
            typ, pres = c
            nxt = cands[i + 1].strip() if i + 1 < len(cands) else ""
            if typ == SEIMU and pres.endswith("大臣") and re.fullmatch(r"[^\s\d資料]{2,6}", _ns(nxt)):
                pres = f"{pres} {nxt}"  # 「…担当大臣」の次の行の氏名
            elif typ in (JIMU, FUSHO) and re.fullmatch(r"\S{1,15}(室|課|部|グループ)", _ns(nxt)):
                pres = f"{pres} {nxt}"  # 「…事務局」の次の行の室・課
            rule = "表紙の提出者の行（日付・会議名の行の後）" if d is not None else "1 枚資料の提出者の行"
            if typ == JIMU:
                rule += "。その会議の事務局（出席者欄の【事務局】と同じ組織）"
            return Presenter(typ, pres, "表紙", rule, evidence=" / ".join(cands[i:i + 2]) if pres != ln.strip() else ln.strip())
    return None


PAGE_LABEL = re.compile(r"^([^\s、。「」]{1,15}?(構成員|委員))提出資料$")  # ページの中の、その表示だけの行
AUTHORED = re.compile(r"(構成員|委員)が.{0,60}?(整理|作成|取りまとめ|とりまとめ)したもの")
ORIGIN = re.compile(r"「([^「」]{2,40}?(会議|本部|審議会|研究会))(\(以下[^)]*\))?」における議論を経て")


def by_body(path: str, src: Source) -> Presenter | None:
    pages = slide_pages(path)
    labels = {}
    for pg in pages:
        for ln in pg.text.splitlines():
            if m := PAGE_LABEL.match(_ns(ln)):
                labels[str(pg.no)] = m.group(1)
                break
    if labels:
        pp = {str(pg.no): labels.get(str(pg.no), UNKNOWN if pg.no > 1 else "（表紙）") for pg in pages}
        who = sorted(set(labels.values()))
        return Presenter(KOSEI, who[0] if len(who) == 1 else "ページごと（page_presenters）", "本文",
                         "各ページの「○○構成員・委員提出資料」の表示", page_presenters=pp if len(who) > 1 or len(labels) < len(pages) - 1 else {})
    for pg in pages[:3]:
        if AUTHORED.search(_ns(pg.text)):
            return Presenter(KOSEI, f"{src.name}の構成員（連名）", "本文", f"本文 p{pg.no} の作成者の表明（構成員が…整理したもの）")
    return None


def origin_of(path: str) -> tuple[str, str]:
    for pg in slide_pages(path):
        if m := ORIGIN.search(_ns(pg.text)):
            return m.group(1), f"本文 p{pg.no}「…{m.group(0)[:40]}…」"
    return "", ""


def material_presenter(row: dict, src: Source) -> Presenter:
    """目録の 1 行（資料・参考資料）→ 提出者。"""
    name = _ns(clean_name(row.get("material_name", "")))
    p = by_name(row.get("material_name", ""))
    if not p and (re.search(r"名簿", name) or re.search(r"(会議|本部|チーム)構成員$", name)):
        res = Presenter(JIMU, JIMU, "既定", "名簿＝会議資料の慣行により事務局（M0 の確認）")
    elif p and p.presenter_type != KOSEI or (p and p.presenter != UNKNOWN):
        res = p
    elif p and p.presenter_type == KOSEI:  # 「構成員提出資料」＝区分は資料名・提出者はページごと
        b = by_body(row["path"], src)
        res = Presenter(KOSEI, b.presenter if b else UNKNOWN, "資料名" if not b else "本文",
                        p.rule + ("／" + b.rule if b else ""), page_presenters=b.page_presenters if b else {})
    else:
        res = by_cover(row["path"], src) or by_body(row["path"], src) or \
            Presenter(JIMU, JIMU, "既定", "提出者の記載なし（資料名・表紙・本文）＝会議資料の慣行により事務局")
    res.origin_body, res.origin_basis = origin_of(row["path"])
    return res
