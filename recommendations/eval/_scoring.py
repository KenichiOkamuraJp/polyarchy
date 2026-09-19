"""
eval 採点ヘルパの単一の真実源（`eval.py`・`multistage_eval.py`・`evalset_gate.py` が共用）。

以前は 3 ファイルに同文コピーがあり、片方だけ直すと数値が静かに乖離し得た（所見 2026-08-19 段 3）。
団体の判定はファイル名プレフィックス（`recommendations.core.orgs`）に従う。
"""
import unicodedata

from recommendations.core.orgs import ORG_DISPLAY_ORDER, org_of_filename

# 団体プレフィックス（ファイル名先頭）＝団体別集計表の行順でもある。
ORG_PREFIXES: tuple[str, ...] = ORG_DISPLAY_ORDER


def org_of(fn: str) -> str:
    """ファイル名から団体を推定する（例 keidanren_2026_003.txt → keidanren）。不明は "?"。"""
    return org_of_filename(fn)


def expected_sources(item: dict) -> list[str]:
    """設問の正解ソース一覧。比較・横断型（Phase 6.5）は expected_sources に複数持てる。

    採点は any-of（いずれかが top-k に入れば hit、MRR は最初に現れた正解の順位）。
    単一ソース設問は従来どおり expected_source 1件のリストになる。棄却設問は空リスト。
    """
    srcs = item.get("expected_sources")
    if srcs:
        return list(srcs)
    src = item.get("expected_source")
    return [src] if src else []


def normalize(text: str) -> str:
    """全角/半角・記号ゆれを吸収してキーワード照合しやすくする。

    NFKC で全角数字や全角％を半角化し、桁区切りカンマと空白を除去する。
    例: '４億6,605万' と '4億6605万' を一致させる。
    """
    text = unicodedata.normalize("NFKC", text)
    for ch in (",", "，", " ", "　", "\n", "\t"):
        text = text.replace(ch, "")
    return text
