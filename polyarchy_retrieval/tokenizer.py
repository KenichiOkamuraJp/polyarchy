"""日本語の分かち書き（BM25 の語彙・クエリの両側で同じものを使う）。

fugashi（MeCab, ローカル/無料）＝リランカーと同じ日本語形態素。辞書は unidic-lite。
"""
import threading

# Tagger はスレッドごとに持つ（2026-10-14）。待受はツールを同時に別のスレッドで動かすことがある＝MeCab の解析器は
# 同時に呼ばれる前提ではない（docs/性能と同時実行_開発計画.md 段 3）。辞書の読み込みは 1 つ数十 ms。
_LOCAL = threading.local()


def _tagger():
    """fugashi Tagger を遅延生成（スレッドごとに 1 回だけロード）。"""
    t = getattr(_LOCAL, "tagger", None)
    if t is None:
        import fugashi
        t = _LOCAL.tagger = fugashi.Tagger()
    return t


def tokenize_ja(text: str) -> list[str]:
    """日本語を形態素の表層形に分割（空白のみのトークンは除外）。BM25 用。"""
    return [w.surface for w in _tagger()(text) if w.surface.strip()]
