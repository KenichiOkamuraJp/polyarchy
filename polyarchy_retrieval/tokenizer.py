"""日本語の分かち書き（BM25 の語彙・クエリの両側で同じものを使う）。

fugashi（MeCab, ローカル/無料）＝リランカーと同じ日本語形態素。辞書は unidic-lite。
"""

_TAGGER = None


def _tagger():
    """fugashi Tagger を遅延生成（プロセス内で1回だけロード）。"""
    global _TAGGER
    if _TAGGER is None:
        import fugashi
        _TAGGER = fugashi.Tagger()
    return _TAGGER


def tokenize_ja(text: str) -> list[str]:
    """日本語を形態素の表層形に分割（空白のみのトークンは除外）。BM25 用。"""
    return [w.surface for w in _tagger()(text) if w.surface.strip()]
