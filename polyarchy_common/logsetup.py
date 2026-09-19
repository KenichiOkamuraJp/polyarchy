"""
Phase 10：ログ整備（日本語ログ＋英語ノイズ抑制＋MCP stdio 保護）。

本プロジェクトは検索スタックの読み込み時に transformers / llama_index の英語ログや
`print()`（例: "LLM is explicitly disabled. Using MockLLM." / "Loading weights ..."）を
吐く。これらは検索・生成の挙動に無影響のノイズだが、**MCP は stdio（標準入出力）を
JSON-RPC の伝送路に使う**ため、stdout への混入はプロトコルを破壊する（致命的）。

そこで本モジュールは 3 つを提供する：
1. `configure_quiet_logging()` … 英語ノイズ（HF/transformers/llama_index/httpx 等）を抑制。
2. `get_logger()`              … **stderr** に日本語で出すロガー（時刻・レベル・本文）。
3. `guard_stdout_for_stdio()`  … 実 stdout を MCP トランスポート専用に確保し、以後の
                                  ライブラリ `print()` を stderr に逃がす（stdio 保護）。

いずれもローカル完結・依存追加なし（標準 logging のみ）。
"""
import contextlib
import logging
import os
import sys

# transformers/HF のログ・進捗バーは import 前に環境変数で黙らせるのが最も確実
# （import 後は programmatic API で追随する）。tqdm の "Loading weights" は既定で
# stderr に出るため stdio は汚さないが、視認性のため合わせて抑制する。
_QUIET_ENV = {
    "TRANSFORMERS_VERBOSITY": "error",
    "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    "TOKENIZERS_PARALLELISM": "false",  # フォーク時の警告抑制
}

# 明示的に静音化する（英語ノイズを出す）ロガー群。WARNING 以上のみ通す。
_NOISY_LOGGERS = (
    "transformers",
    "sentence_transformers",
    "llama_index",
    "llama_index.core",
    "httpx",
    "httpcore",
    "chromadb",
    "urllib3",
    "fsspec",
    "filelock",
    "asyncio",
    # MCP フレームワーク自身の英語ログ（"Processing request of type ..." 等）。
    # FastMCP は __init__ で root を INFO＋RichHandler に設定するため個別に黙らせる。
    "mcp",
    "mcp.server",
    "mcp.server.lowlevel",
    "mcp.server.lowlevel.server",
)

_configured = False


def configure_quiet_logging() -> None:
    """英語ノイズ（HF/transformers/llama_index/httpx/mcp 等）を抑制する。

    ロガーのレベル引き下げは**毎回再適用**する（FastMCP の configure_logging など
    サードパーティが後から root/ハンドラを張り替えても確実に黙らせるため）。env と
    transformers の初期化だけは 1 度きり（冪等ガード）。
    """
    global _configured
    # ロガーレベルは毎回下げる（他ライブラリの後追い再設定に勝つ）。
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if _configured:
        return
    for k, v in _QUIET_ENV.items():
        os.environ.setdefault(k, v)
    # 既に import 済みでも programmatic に静音化（環境変数が効かない後追いケース）。
    try:
        import transformers

        transformers.logging.set_verbosity_error()
        try:
            from transformers.utils import logging as _hf_logging

            _hf_logging.disable_progress_bar()
        except Exception:
            pass
    except Exception:
        pass
    _configured = True


def get_logger(name: str = "polyarchy") -> logging.Logger:
    """**stderr** に日本語で出すロガーを返す（時刻・レベル・本文）。

    stdout を汚さない（MCP stdio 安全）。二重ハンドラ・二重出力を避けるため
    propagate=False とし、初回のみ StreamHandler(stderr) を付ける。
    """
    logger = logging.getLogger(name)
    if not getattr(logger, "_polyarchy_ready", False):
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.StreamHandler(sys.stderr)  # 実 stderr を保持（stdout 退避後も不変）
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(handler)
        logger._polyarchy_ready = True  # type: ignore[attr-defined]
    return logger


@contextlib.contextmanager
def quiet_stdout():
    """このブロック中の stdout への `print()` を捨てる（llama_index の生 print 抑制用）。

    llama_index は `Settings.llm/embed_model` 解決時に "LLM is explicitly disabled. Using
    MockLLM." 等を **print()** で吐く（logging ではないのでレベル調整で消せない）。検索
    スタック構築の間だけ stdout を devnull に逃がして飲み込む。日本語ログは logger 経由で
    実 stderr に出るため影響を受けない。
    """
    devnull = open(os.devnull, "w")
    try:
        with contextlib.redirect_stdout(devnull):
            yield
    finally:
        devnull.close()


def guard_stdout_for_stdio():
    """実 stdout を確保して返し、以後のライブラリ `print()` を stderr へ逃がす。

    MCP stdio サーバは `sys.stdout.buffer` を JSON-RPC 伝送路として掴む。ライブラリの
    `print()`（MockLLM/MockEmbedding など）が同じ stdout に出るとプロトコルが壊れる。
    そこで **実 stdout を戻り値として呼び出し側に渡し**（＝トランスポート専用に確保）、
    プロセスの `sys.stdout` は `sys.stderr` に差し替える（ライブラリ print は stderr へ）。

    返り値の実 stdout を `mcp.server.stdio.stdio_server(stdout=...)` に渡すことで、
    プロトコルは実 stdout に、ノイズは stderr に、と物理的に分離できる。
    """
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    return real_stdout
