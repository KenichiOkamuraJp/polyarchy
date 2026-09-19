"""
ゲート共通のクライアント部品：stdio 越しに stats.serving.mcp_server へ接続するパラメータと、ツール結果の payload 取り出し。
mcp_smoke／exact_match が共用する（アサーションは各ゲートに置く）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import StdioServerParameters

from stats.core.paths import ROOT_DIR


def server_params(query_log_name: str) -> tuple[StdioServerParameters, Path]:
    """サーバ起動パラメータ（cwd=リポ root・捕捉ログは一時ファイルへ）。返り値：(params, 捕捉ログのパス)。"""
    tmp_log = Path(tempfile.gettempdir()) / query_log_name
    params = StdioServerParameters(command=sys.executable, args=["-m", "stats.serving.mcp_server"], cwd=str(ROOT_DIR),
                                   env={**os.environ, "STATS_QUERY_LOG": str(tmp_log)})
    return params, tmp_log


def payload(result) -> dict:
    """call_tool の結果から dict を取り出す（structuredContent 優先・無ければ text block の JSON）。"""
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_text": text}
    return {}
