"""
期間表記（`docs/参照粒度設計.md` §2）——厳密一致のための正規化と、取得元の時間コードからの決定論変換。

表記（利用者入力・値ストア共通）：
    暦年 "2024" / 年度 "FY2024" / 暦年四半期 "2024Q1" / 年度四半期 "FY2024Q1"（4-6月）/
    半期 "FY2024H1" / 月 "2024-03" / 日 "2024-03-31" / 時点(末残) "2024-03-31E"
freq コード： a fy q fq h m d irr
原則：近似しない。表記が系列の freq と合わなければ「合わない」と言うだけ（値は返さない）。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Optional

FREQS = ("a", "fy", "q", "fq", "h", "m", "d", "irr")
FREQ_LABEL = {"a": "暦年", "fy": "年度", "q": "四半期（暦年）", "fq": "四半期（年度）", "h": "半期（年度）",
              "m": "月次", "d": "日次", "irr": "不定期（調査年）"}
FREQ_EXAMPLE = {"a": "2024", "fy": "FY2024", "q": "2024Q1", "fq": "FY2024Q1", "h": "FY2024H1",
                "m": "2024-03", "d": "2024-03-31", "irr": "2021"}

_PATTERNS = [
    ("fq", re.compile(r"^FY(\d{4})Q([1-4])$")),
    ("h", re.compile(r"^FY(\d{4})H([12])$")),
    ("fy", re.compile(r"^FY(\d{4})$")),
    ("q", re.compile(r"^(\d{4})Q([1-4])$")),
    ("d", re.compile(r"^(\d{4})-(\d{2})-(\d{2})E?$")),
    ("m", re.compile(r"^(\d{4})-(\d{2})$")),
    ("a", re.compile(r"^(\d{4})$")),
]


@dataclass(frozen=True)
class Period:
    text: str      # 正規表記
    freq: str      # a fy q fq h m d
    year: int
    sub: int = 0   # 四半期/半期/月/日の番号（a fy は 0）

    def sort_key(self) -> tuple:
        return (self.year, self.sub)


def parse(text: str) -> Optional[Period]:
    """表記を厳密に解釈。合わなければ None（推測しない）。"""
    t = (text or "").strip()
    for freq, pat in _PATTERNS:
        m = pat.match(t)
        if not m:
            continue
        g = m.groups()
        if freq in ("a", "fy"):
            return Period(t, freq, int(g[0]))
        if freq in ("q", "fq", "h"):
            return Period(t, freq, int(g[0]), int(g[1]))
        if freq == "m":
            mm = int(g[1])
            if not 1 <= mm <= 12:
                return None
            return Period(t, freq, int(g[0]), mm)
        if freq == "d":
            return Period(t, freq, int(g[0]), int(g[1]) * 100 + int(g[2]))
    return None


def matches_freq(period_text: str, freq: str) -> bool:
    """利用者の period 表記が系列の freq と一致するか（irr は暦年表記を受ける）。"""
    p = parse(period_text)
    if p is None:
        return False
    if freq == "irr":
        return p.freq == "a"
    return p.freq == freq


def hint_for(freq: str) -> str:
    return f"この系列の期間表記は {FREQ_LABEL.get(freq, freq)}＝例 \"{FREQ_EXAMPLE.get(freq, '')}\""


# ---------------------------------------------------------------------------
# 和暦 → 西暦（取込モジュール共通。ERA 辞書はここ 1 つ。漢字／半角・全角英字／「元」年に対応）
# ---------------------------------------------------------------------------

ERA_BASE = {"明治": 1867, "大正": 1911, "昭和": 1925, "平成": 1988, "令和": 2018,
            "M": 1867, "T": 1911, "S": 1925, "H": 1988, "R": 2018,
            "Ｍ": 1867, "Ｔ": 1911, "Ｓ": 1925, "Ｈ": 1988, "Ｒ": 2018}
_ERA = "(明治|大正|昭和|平成|令和|[MTSHRＭＴＳＨＲ])"
_WAREKI_WS = re.compile(r"\s+")
_WAREKI_FY_RE = re.compile(rf"^{_ERA}(元|\d{{1,2}})(年度)?$")
_WAREKI_DATE_RE = re.compile(rf"^{_ERA}(元|\d{{1,2}})[.年](\d{{1,2}})[.月](\d{{1,2}})日?$")


def wareki_year(era: str, year: str) -> Optional[int]:
    """元号＋年（'元' または 1〜2 桁）→ 西暦。元号が語彙外なら None。"""
    base = ERA_BASE.get(era)
    if base is None:
        return None
    return base + (1 if year == "元" else int(year))


def from_wareki_fy(text: str, *, require_suffix: bool = True) -> Optional[str]:
    """'令和8年度'／'平成元年度'／'令和 8 年度' → 'FY2026'…。表記外は None。
    require_suffix=False なら「年度」無し（財政統計のシート名 'Ｓ60'・'Ｈ元'・'R6'・'昭和42'）も受ける。'年' だけは受けない。"""
    m = _WAREKI_FY_RE.match(_WAREKI_WS.sub("", text or ""))
    if not m or (require_suffix and not m.group(3)):
        return None
    y = wareki_year(m.group(1), m.group(2))
    return f"FY{y}" if y is not None else None


def from_wareki_date(text: str) -> Optional[str]:
    """'S49.9.24'／'H10.4.1'／'R8.7.31'／'令和8年7月31日' → '1974-09-24'…（ISO 日付）。不正な日付・表記外は None。"""
    m = _WAREKI_DATE_RE.match((text or "").strip())
    if not m:
        return None
    y = wareki_year(m.group(1), m.group(2))
    if y is None:
        return None
    try:
        return dt.date(y, int(m.group(3)), int(m.group(4))).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 取得元の時間コード → 正規表記（決定論・dataset ごとに固定。テスト対象）
# ---------------------------------------------------------------------------

def from_estat_hojin_fy(code: str) -> Optional[str]:
    """法人企業統計 時系列（年度次 0003060791）: '19600' → 'FY1960'"""
    m = re.match(r"^(\d{4})0$", code or "")
    return f"FY{m.group(1)}" if m else None


def from_estat_hojin_fq(code: str) -> Optional[str]:
    """法人企業統計 時系列（四半期 0003060191）: '19542' = 1954年4-6月 → 'FY1954Q1'。
    e-Stat の末尾 1=1-3月, 2=4-6月, 3=7-9月, 4=10-12月（暦年四半期番号）。年度四半期へ：
    4-6月→FY同年Q1, 7-9→Q2, 10-12→Q3, 1-3月→FY前年Q4。"""
    m = re.match(r"^(\d{4})([1-4])$", code or "")
    if not m:
        return None
    y, cq = int(m.group(1)), int(m.group(2))
    if cq == 1:
        return f"FY{y - 1}Q4"
    return f"FY{y}Q{cq - 1}"


def from_estat_cpi_time(code: str) -> Optional[str]:
    """CPI 2020年基準（0003427113）の時間コード：
    '2026000707' → '2026-07'（年+000000+月月）／'2025100000' → 'FY2025'／'2025000000' → '2025'"""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})$", code or "")
    if not m:
        return None
    y, kind, mm1, mm2 = m.group(1), m.group(2), m.group(3), m.group(4)
    if kind == "10" and mm1 == "00":
        return f"FY{y}"
    if kind == "00" and mm1 == "00" and mm2 == "00":
        return y
    if kind == "00" and mm1 == mm2 and 1 <= int(mm1) <= 12:
        return f"{y}-{mm1}"
    return None


def from_estat_quarter(code: str) -> Optional[str]:
    """e-Stat の四半期コード（労働力調査 詳細集計等）：'2002000103' → '2002Q1'（年+00+開始月+終了月）。
    年平均・月次のコードは None（＝この系列では採らない）。"""
    m = re.match(r"^(\d{4})00(\d{2})(\d{2})$", code or "")
    if not m:
        return None
    y, m1, m2 = m.group(1), int(m.group(2)), int(m.group(3))
    if m2 - m1 != 2 or m1 not in (1, 4, 7, 10):
        return None
    return f"{y}Q{(m1 - 1) // 3 + 1}"


def from_estat_yyyymm(code: str) -> Optional[str]:
    """汎用：'202407' → '2024-07'／'2024' → '2024'"""
    if re.match(r"^\d{6}$", code or ""):
        return f"{code[:4]}-{code[4:6]}"
    if re.match(r"^\d{4}$", code or ""):
        return code
    return None


def from_estat_roudou_time(code: str) -> Optional[str]:
    """労働力調査 基本集計 月次（0002060002 等）: '2024000707'（年+0007+07）系は CPI と同型、
    '202407' 系は yyyymm。どちらでもなければ None。"""
    return from_estat_cpi_time(code) or from_estat_yyyymm(code)


def from_esri_year_fy(code: str) -> Optional[str]:
    """ESRI 年次推計 年度表（ffm*）の列見出し：'1994' → 'FY1994'（見出し行に西暦の年度が入る）"""
    return f"FY{code}" if re.match(r"^\d{4}$", code or "") else None


def from_esri_year_a(code: str) -> Optional[str]:
    """ESRI 年次推計 暦年表（fcm*）の列見出し：'1994' → '1994'"""
    return code if re.match(r"^\d{4}$", code or "") else None


def from_esri_year_paren(code: str) -> Optional[str]:
    """ESRI ストック編の列見出し：'令和6暦年末（2024）' → '2024'（括弧内の西暦だけを採る。和暦は換算しない）"""
    m = re.search(r"[（(](\d{4})[）)]", code or "")
    return m.group(1) if m else None


def from_boj_gap_q(code: str) -> Optional[str]:
    """日銀 需給ギャップ（gap.xlsx data1）: '1983.1Q' → '1983Q1'（暦年四半期）"""
    m = re.match(r"^(\d{4})\.([1-4])Q$", (code or "").strip())
    return f"{m.group(1)}Q{m.group(2)}" if m else None


def from_boj_gap_h(code: str) -> Optional[str]:
    """日銀 潜在成長率（gap.xlsx data2）: '1983.1 : 1983.2Q' → 'FY1983H1'／'1983.2 : 1983.4Q' → 'FY1983H2'（年度半期）"""
    m = re.match(r"^(\d{4})\.([12])\s*:", (code or "").strip())
    return f"FY{m.group(1)}H{m.group(2)}" if m else None


def from_esri_qe_quarter(code: str) -> Optional[str]:
    """ESRI QE CSV の行ラベル（年を補完済み）'1994/ 1- 3.' → '1994Q1'。月範囲 1-3→Q1, 4-6→Q2, 7-9→Q3, 10-12→Q4"""
    m = re.match(r"^(\d{4})/\s*(\d{1,2})-\s*(\d{1,2})\.?$", (code or "").strip())
    if not m:
        return None
    q = {"1": 1, "4": 2, "7": 3, "10": 4}.get(m.group(2))
    return f"{m.group(1)}Q{q}" if q else None


CONVERTERS = {
    "boj_gap_q": from_boj_gap_q,
    "boj_gap_h": from_boj_gap_h,
    "esri_qe_quarter": from_esri_qe_quarter,
    "esri_year_fy": from_esri_year_fy,
    "esri_year_a": from_esri_year_a,
    "esri_year_paren": from_esri_year_paren,
    "estat_hojin_fy": from_estat_hojin_fy,
    "estat_hojin_fq": from_estat_hojin_fq,
    "estat_cpi_time": from_estat_cpi_time,
    "estat_quarter": from_estat_quarter,
    "estat_yyyymm": from_estat_yyyymm,
    "estat_roudou_time": from_estat_roudou_time,
}


def convert(converter: str, code: str) -> Optional[str]:
    fn = CONVERTERS.get(converter)
    return fn(code) if fn else None
