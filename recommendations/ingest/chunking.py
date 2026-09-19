"""
Phase 2：チャンク戦略の定義（レジストリ）。

各戦略は「名前 → 変換(transformations)のリストを返す関数」で登録する。
build_index 側で末尾に埋め込みモデルを足してインデックス化する。

比較条件をチャンク分割だけに絞るため、埋め込みモデル・検索条件(top_k)・
LLM は全戦略で共通（config.py の値）を使う。
"""
import re

from llama_index.core.node_parser import (
    SemanticSplitterNodeParser,
    SentenceSplitter,
)
from llama_index.embeddings.openai import OpenAIEmbedding

from recommendations.core.config import EMBED_TIMEOUT, EMBEDDING_MODEL, OPENAI_API_KEY

# OpenAI 埋め込みの入力上限は 8192 トークン。構造的分割が上限超えチャンクを
# 作ると 400 エラーになるため、安全側でこのトークン数を上限キャップにする。
MAX_CHUNK_TOKENS = 1024


def ja_sentence_splitter(text: str) -> list[str]:
    """日本語の文境界（。！？・改行）で分割する。

    SemanticSplitter の既定文分割器は英語想定（'. ' 区切り）で「。」を認識せず、
    日本語PDFの1ページ全体を1文と誤認して 8192 トークン超の埋め込みを投げてしまう。
    区切り文字は残したまま分割する。
    """
    parts = re.split(r"(?<=[。！？\n])", text)
    return [p for p in parts if p.strip()]


def _fixed(chunk_size: int, chunk_overlap: int) -> list:
    return [SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)]


def _semantic() -> list:
    """意味的まとまり（隣接文の埋め込み類似度の切れ目）で分割する構造的分割。

    SemanticSplitter は最大サイズ上限を持たず巨大チャンクを作り得るので、
    後段に SentenceSplitter を噛ませて MAX_CHUNK_TOKENS でキャップする。
    """
    # timeout は API ハング対策（eval の SIGALRM ガードと同趣旨、ingest には無かった）。
    embed = OpenAIEmbedding(model=EMBEDDING_MODEL, api_key=OPENAI_API_KEY, timeout=EMBED_TIMEOUT)
    return [
        SemanticSplitterNodeParser(
            buffer_size=1,
            breakpoint_percentile_threshold=95,
            embed_model=embed,
            sentence_splitter=ja_sentence_splitter,
        ),
        SentenceSplitter(chunk_size=MAX_CHUNK_TOKENS, chunk_overlap=0),
    ]


# 名前 → 変換リストを生成するファクトリ。比較対象はここを増減するだけで拡張できる。
STRATEGIES: dict[str, callable] = {
    "fixed_800_100": lambda: _fixed(800, 100),    # Phase 1 ベースライン（固定長）
    "fixed_512_64": lambda: _fixed(512, 64),      # 小さい固定長：数値の局所化狙い
    "fixed_1024_200": lambda: _fixed(1024, 200),  # 大きい固定長：数値＋文脈の同居狙い
    "semantic": _semantic,                         # 構造的分割（上限キャップ付き）
}

