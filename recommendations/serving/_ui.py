"""
Streamlit UI（`app.py`＝参照型・`chat_app.py`＝対話型）の共通部品。

団体の表示名は `recommendations.core.orgs` の 1 表を参照する。2 つの UI は用途・課金・ポートが違うため
統合しない（合意済）。共有するのは表示名・日付変換・出典描画の部品・tokenizers の衛生設定だけ。
"""
import datetime as _dt
import os

# HF tokenizers のフォーク並列警告/デッドロックを避ける衛生設定（Streamlit の再実行スレッドで
# 回すため）。transformers を引く重い import より前に効かせたいので、各 UI はこのモジュールを
# 最初に import する。
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import streamlit as st

from recommendations.core.orgs import ORG_LABELS  # noqa: E402  団体コード → 表示名（再 export）

__all__ = ["ORG_LABELS", "org_label", "to_int_date", "source_headline",
           "render_source_meta", "render_source_text"]


def org_label(code: str | None) -> str:
    """団体コードを表示名に（未知コードはそのまま・空は「不明」）。"""
    return ORG_LABELS.get(code, code or "不明")


def to_int_date(d: _dt.date | None) -> int | None:
    """date → YYYYMMDD の int（検索 API の since/until 形式）。None はそのまま。"""
    return int(d.strftime("%Y%m%d")) if d else None


def source_headline(c, idx: int, with_doc_type: bool = False) -> str:
    """出典 1 件の見出し行（Markdown）。"""
    kind = f"・{c.doc_type}" if (with_doc_type and c.doc_type) else ""
    return (f"**[{idx}] {c.title or c.file_name}** — {org_label(c.org)}／{c.date or '日付なし'}"
            f"／p.{c.page}（{c.layer}{kind}・関連度 {c.score:.3f}）")


def render_source_meta(c, warn_shiryo: bool = False) -> None:
    """ファイル名・分野・（資料の注意書き）・原文リンクを描画する。"""
    meta = f"ファイル: `{c.file_name}`"
    if c.field_tags:
        meta += f" ／ 分野: {c.field_tags}"
    st.caption(meta)
    if warn_shiryo and (c.doc_type or "") == "資料":
        st.caption("⚠️ 資料（アンケート結果・政党回答集等）＝本文が団体自身の主張とは限りません")
    if c.source_url:
        st.markdown(f"[原文リンク（外部）]({c.source_url})")


def render_source_text(c, limit: int) -> None:
    """チャンク本文を limit 文字で切って描画する。"""
    text = c.text or ""
    st.text(text[:limit] + ("…" if len(text) > limit else ""))
