"""
記録（議事録・議事要旨・議事概要）の解析：行の列を発言の単位に切り、発言者を記録の見出しからだけ取る。

型は表題ではなく中身で決まり、1 つの記録の中でも節ごとに違う（M0：AI 戦略会議は大臣の挨拶・座長の発言は
名前つき、構成員の意見は匿名）。そこで型は文書ではなく単位ごとに持つ（mode）。

| 行の形 | 例 | 扱い |
|---|---|---|
| 「○名前 本文」（○の直後に空白なし・名前に読点なし） | ○平デジタル行財政改革担当大臣 ただいまから… | 逐語の 1 発言（verbatim） |
| 「○団体名（氏名 役職）」・「○府省名」だけの行 | ○国土交通省（堤審議官）／○厚生労働省 | 逐語の 1 発言（括弧の中の空白も名前の一部・本文は次の行から） |
| 「〇 地の文」（「○」だけの行は次の行と合わせる） | 〇 高市内閣総理大臣より、以下のとおり発言があった。 | 地の文（narration）。続く箇条の文脈を決める |
| 【名前】 | 【伊藤委員】【デジタル庁】 | 見出し＝続く箇条の発言者（named） |
| 【提出者:名前】 | 【提出者:村岡構成員】 | 欠席者の書面の発言要旨（written） |
| 「・名前から、「…」」 | ・林総務大臣から、「…」といった発言があった。 | 名前つきの箇条（named） |
| 「・本文」 | ・クリエイティブ領域に関しては… | 直前の文脈の発言者。文脈が「構成員から意見」なら不明（anonymous） |

推定しない：地の文が複数の名前を挙げる・発言者の型に当たらない・文脈の無い箇条は、発言者を「不明」にする
（名簿・資料・内容から特定の構成員を連想できても埋めない）。本文は最初の「○／〇」の行から（それより前は
出席者の一覧＝attribution が区分の判定に使う）。
"""
import re
from dataclasses import dataclass, field

from deliberations.ingest.extract import record_lines

UNKNOWN = "不明"
MAX_CHARS = 600  # 1 単位の上限（超えたら文の切れ目で分け、発言者を引き継ぐ）

ROLE_SUFFIX = r"(内閣総理大臣|総理大臣|総理|大臣政務官|政務官|副大臣|大臣|内閣官房長官|官房長官|委員長|座長代理|座長|議長代理|議長|構成員|委員|主査|副本部長|本部長|統括官|審議官|参事官|事務局長|局長|次長|室長|部長|課長)"
BODY_SUFFIX = r"(省|庁|府|事務局|推進室|委員会|会議|研究会|本部)"
GROUP = re.compile(r"^(各|出席|関係|他の|一部の)?(構成員|委員|出席者|有識者|府省庁|関係府省庁|閣僚)(の皆様)?$")

VERBATIM = re.compile(r"^[○〇](?=\S)([^\s、。]{2,40})\s+(.*)$")
# 名前と本文の間に空白が無い逐語（書面で提出された発言の節など）＝最短の「…役職」までを名前とする
VERBATIM_TIGHT = re.compile(r"^[○〇]([^\s、。「」]{1,30}?" + ROLE_SUFFIX + r")(?!より|から)(\S.*)$")
# 名前だけの行（本文は次の行から）
VERBATIM_ALONE = re.compile(r"^[○〇]([^\s、。「」]{1,30}?" + ROLE_SUFFIX + r")$")
# 「団体名（氏名 役職）」の発言者（規制改革推進会議の WG の関係者）＝括弧の中に空白があっても名前の一部
# 括弧の中が 1 文字（「村上(文)専門委員」＝同姓の区別）は対象外＝名前の後に役職が続く逐語（VERBATIM_ALONE 等）
VERBATIM_PAREN = re.compile(r"^[○〇]([^\s、。「」()]{1,40}\([^()]{2,40}\))(?!専門委員|委員|座長)\s*(.*)$")
# 英字の社名は空白を含む（「○DAIWA CYCLE株式会社(伊藤営業本部長)」）＝英字で始まる名前に限って空白を許す
VERBATIM_PAREN_LATIN = re.compile(r"^[○〇]([A-Za-z0-9][^、。「」()]{0,40}?\([^()]{2,40}\))(?:\s+(.*))?$")
# 府省の名前だけの行（「○厚生労働省」＝本文は次の行から）
VERBATIM_BODY = re.compile(r"^[○〇]([^\s、。「」()]{1,20}?(省|庁))$")
NARRATION = re.compile(r"^[○〇]\s*(.*)$")
HEADING = re.compile(r"^【([^】]{1,40})】\s*(.*)$")
BULLET = re.compile(r"^[・･]\s*(.*)$")
NAMED_BULLET = re.compile(r"^(?:\d+\s*)?([^、。「」\s]{2,40}?)から、?\s*「")
NOTE = re.compile(r"^※\s*(.*)$")
SENT_SPEAKER = re.compile(r"([^、。「」\s]{2,40}?)(?:より|から)")


@dataclass
class Unit:
    speaker: str
    mode: str            # verbatim / named / written / anonymous / narration
    start_page: int
    lines: list[str] = field(default_factory=list)
    end_page: int = 0

    @property
    def text(self) -> str:
        return "".join(s if s == "\n" else s.strip() for s in self.lines)


def _merge_marker_only(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """「○」「・」だけの行を次の行と合わせる（議事概要の書式）。"""
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        p, s = lines[i]
        if s.strip() in ("○", "〇", "・") and i + 1 < len(lines):
            out.append((p, s.strip() + " " + lines[i + 1][1].strip()))
            i += 2
            continue
        out.append((p, s))
        i += 1
    return out


def _is_named(name: str) -> bool:
    """発言者の型に当たる名前か（役職・会議の役割か、府省・事務局などの組織）。集合名詞は除く。"""
    n = re.sub(r"^(次に|また|冒頭|最後に|続いて|その後)、?", "", name)
    if GROUP.match(n):
        return False
    return bool(re.search(ROLE_SUFFIX + r"$", n) or re.search(BODY_SUFFIX + r"$", n))


def _clean(name: str) -> str:
    return re.sub(r"^(次に|また|冒頭|最後に|続いて|その後|議論に先立ち)、?", "", name).strip()


def _sentence(lines: list[tuple[int, str]], i: int) -> str:
    """i 行目から最初の「。」までの地の文（次の印の行の前まで）。"""
    s = lines[i][1].strip()
    j = i + 1
    while "。" not in s and j < len(lines) and not re.match(r"^\s*[○〇【・･※]", lines[j][1]):
        s += lines[j][1].strip()
        j += 1
    return s.split("。")[0]


def narration_context(sentence: str) -> tuple[str | None, str | None]:
    """地の文 → (地の文そのものの発言者, 続く箇条の文脈)。文脈＝発言者名 / "ANON" / None。"""
    body = re.sub(r"^[○〇]\s*", "", sentence)
    names = [_clean(m.group(1)) for m in SENT_SPEAKER.finditer(body)]
    people = [n for n in dict.fromkeys(names) if _is_named(n)]
    groups = [n for n in names if GROUP.match(re.sub(r"^.*、", "", n))]
    single = people[0] if len(people) == 1 else None
    if groups or re.search(r"(構成員|委員|出席者)(から|より|の)(主な)?(意見|発言|御発言)", body):
        return single if not groups else None, "ANON"
    if single and re.search(r"以下のとおり", body):
        return single, single
    return single, None


PAREN_HEAD = re.compile(r"^[(（]([^()（）]{1,30})[)）]\s*(.*)$")
MARK_ONLY = re.compile(r"^[⚫●•◼■➢▶o・･]\s*$")
SECTION = re.compile(r"^<[^>]*>$")


def parse_paren(path: str) -> tuple[list[Unit], list[tuple[int, str]]]:
    """デジタル庁の議事要旨（Source.record_style="paren"）。本文は「◼ 議事」の後。「(名前)本文」＝その名前の
    発言の要約（named・名前が発言者の型に当たらなければ不明）。箇条の記号だけの行・「<…>」の節の見出しは区切り。
    区切りの後の地の文（「冒頭、…から、…ご発言があった。」「事務局より、…説明した。」）は narration
    （発言者は文の中の名前が 1 つのときだけ）。"""
    lines = record_lines(path)
    # 「◼ 議事」は 1 行のことも、「◼」と「議事」の 2 行に割れることもある（議事次第の「2. 議事」は除く）
    start = next((k + 1 for k, (_, s) in enumerate(lines) if re.fullmatch(r"[◼■]\s*議事", s.strip())
                  or (s.strip() == "議事" and k and lines[k - 1][1].strip() in ("◼", "■"))), len(lines))
    head, body = lines[:start], lines[start:]
    units: list[Unit] = []
    cur: Unit | None = None
    for k, (p, raw) in enumerate(body):
        s = raw.strip()
        if not s or MARK_ONLY.match(s) or SECTION.match(s):
            cur = None
            continue
        if m := PAREN_HEAD.match(s):
            name = m.group(1).strip()
            named = _is_named(name)
            cur = Unit(name if named else UNKNOWN, "named" if named else "anonymous", p, [m.group(2)], p)
            units.append(cur)
            continue
        if cur is None:
            who, _ = narration_context(_sentence(body, k))
            if who:  # 「意見募集の結果等を事務局から報告し」＝助詞の後ろだけを名前に
                who = re.split(r"[をはがにでと]", who)[-1]
                who = who if _is_named(who) else None
            cur = Unit(who or UNKNOWN, "narration", p, [s], p)
            units.append(cur)
            continue
        cur.lines.append(s)
        cur.end_page = p
    return [u for u in units if u.text], head


def parse(path: str, style: str = "") -> tuple[list[Unit], list[tuple[int, str]]]:
    """記録を解析して (単位の列, 本文より前の行＝出席者の一覧) を返す。style＝Source.record_style。"""
    if style == "paren":
        return parse_paren(path)
    lines = _merge_marker_only(record_lines(path))
    # 本文の始まり＝最初の「○／〇」の行のうち文になっているもの（出席者の一覧の「○AI 戦略会議 構成員」を除く）
    # 規制改革推進会議の WG のように発言者の行がすべて名前だけの記録は、名前だけの最初の行から（括弧の型・府省名・役職）
    start = next((k for k, (_, s) in enumerate(lines)
                  if re.match(r"^\s*[○〇]", s) and re.search(r"より|から|。|、", s)), len(lines))
    alone = next((k for k, (_, s) in enumerate(lines)
                  if VERBATIM_ALONE.match(s.strip()) or (VERBATIM_PAREN.match(s.strip()) and not VERBATIM_PAREN.match(s.strip()).group(2))), len(lines))
    start = min(start, alone)
    head, body = lines[:start], lines[start:]
    units: list[Unit] = []
    ctx: str | None = None      # 続く箇条の発言者（名前 / "ANON" / None）
    ctx_mode = "named"          # 文脈の発言者に付ける型（書面の節では written）
    written_section = False     # 「提出のあった発言は以下のとおり」の後＝逐語の行も書面の発言
    cur: Unit | None = None

    def new(speaker: str, mode: str, page: int, first: str) -> Unit:
        u = Unit(speaker, mode, page, [first] if first else [], page)
        units.append(u)
        return u

    for k, (p, raw) in enumerate(body):
        s = raw.strip()
        if m := (VERBATIM_PAREN_LATIN.match(s) or VERBATIM_PAREN.match(s)):
            cur, ctx, written_section = new(m.group(1), "verbatim", p, m.group(2) or ""), None, False
            continue
        if m := VERBATIM_BODY.match(s):
            cur, ctx, written_section = new(m.group(1), "verbatim", p, ""), None, False
            continue
        m = VERBATIM.match(s)
        if m and "より" not in m.group(1) and "から" not in m.group(1):
            # 名前の後に空白のある逐語＝会議の場の発言（書面の節はここで終わる）
            cur, ctx, written_section = new(m.group(1), "verbatim", p, m.group(2)), None, False
            continue
        if m := VERBATIM_TIGHT.match(s):
            cur, ctx = new(m.group(1), "written" if written_section else "verbatim", p, m.group(3)), None
            continue
        if m := VERBATIM_ALONE.match(s):
            cur, ctx = new(m.group(1), "written" if written_section else "verbatim", p, ""), None
            continue
        if m := HEADING.match(s):
            inner = m.group(1).replace("提出者:", "提出者：")
            if inner.startswith("提出者："):  # 書面の発言要旨＝見出しの下の箇条まで 1 つの発言
                cur, ctx = new(inner.split("：", 1)[1].strip(), "written", p, m.group(2)), None
            else:
                ctx, ctx_mode = (inner if _is_named(inner) else None), "named"
                cur = new(inner if ctx else UNKNOWN, "named" if ctx else "anonymous", p, m.group(2)) if m.group(2) else None
            continue
        if NARRATION.match(s):
            sent = _sentence(body, k)
            who, ctx = narration_context(sent)
            ctx_mode = "named"
            written_section = "提出のあった" in sent
            cur = new(who or UNKNOWN, "narration", p, re.sub(r"^[○〇]\s*", "", s))
            continue
        if (m := BULLET.match(s)) and cur is not None and cur.mode in ("verbatim", "written") and ctx is None:
            cur.lines.append(s)  # 逐語・書面の発言の中の箇条書き＝同じ発言の続き
            cur.end_page = p
            continue
        if m := BULLET.match(s):
            t = m.group(1)
            nb = NAMED_BULLET.match(t)
            if nb and _is_named(nb.group(1)):
                cur = new(nb.group(1), "named", p, t)
            elif ctx and ctx != "ANON":
                cur = new(ctx, ctx_mode, p, t)
            else:
                cur = new(UNKNOWN, "anonymous", p, t)
            continue
        if m := NOTE.match(s):
            cur, ctx = new(UNKNOWN, "narration", p, m.group(1)), None
            written_section = "提出のあった" in s
            continue
        if cur is None:  # 見出しの直後の本文など
            named = bool(ctx and ctx != "ANON")
            cur = new(ctx if named else UNKNOWN, ctx_mode if named else "anonymous", p, s)
        else:
            cur.lines.append(s)
            cur.end_page = p
    return [u for u in units if u.text], head


def merge_named(units: list[Unit]) -> list[Unit]:
    """名前つき要約で同じ発言者の箇条が続くとき、MAX_CHARS まで 1 単位にまとめる（検索の単位に文脈を持たせる）。
    匿名の箇条はまとめない（別の構成員の発言かもしれない）。逐語・書面・地の文はそのまま。"""
    out: list[Unit] = []
    for u in units:
        prev = out[-1] if out else None
        if (prev and u.mode == "named" and prev.mode == "named" and prev.speaker == u.speaker
                and len(prev.text) + len(u.text) <= MAX_CHARS):
            prev.lines.extend(["\n"] + u.lines)
            prev.end_page = u.end_page
        else:
            out.append(Unit(u.speaker, u.mode, u.start_page, list(u.lines), u.end_page))
    return out


def split_long(u: Unit) -> list[Unit]:
    """MAX_CHARS を超える単位を文の切れ目（。）で分ける（発言者・型は引き継ぐ）。ページは近似（元の範囲）。"""
    t = u.text
    if len(t) <= MAX_CHARS:
        return [u]
    parts, buf = [], ""
    for sent in re.split(r"(?<=。)", t):
        if buf and len(buf) + len(sent) > MAX_CHARS:
            parts.append(buf)
            buf = ""
        buf += sent
    if buf:
        parts.append(buf)
    return [Unit(u.speaker, u.mode, u.start_page, [x], u.end_page) for x in parts]


def record_type(units: list[Unit]) -> str:
    """文書としての記録の型（目録・返り値の表示用。判定は単位ごとの mode が正）。"""
    modes = [u.mode for u in units if u.mode != "narration"]
    if "verbatim" in modes:
        return "逐語"
    if not modes:
        return "発言の記録なし"
    named = sum(m in ("named", "written") for m in modes)
    return "名前つき要約" if named >= modes.count("anonymous") else "匿名要約"
