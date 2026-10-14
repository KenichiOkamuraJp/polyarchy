"""
MCP Streamable HTTP 待受の定型（リモート公開用・全サービス共通）。

Cloudflare Tunnel 経由で `<service>.<ドメイン>` に出し、ログイン（`access.install_auth`＝現行は外部 IdP の
Bearer 検証）＋秘密パス＋エッジのレート制限で守る構成の"原点"（IP 許可リストは使えない＝ルート CLAUDE.md §5）。
既定は **127.0.0.1 バインド**＝ローカル/トンネル経由のみ到達可（直接インターネット
には晒さない）。各サービスは FastMCP を組み立ててから本関数に渡すだけでよい。

環境変数（サービス横断で同名）：
- `MCP_ALLOWED_HOSTS`      … 設定時は DNS リバインディング保護 ON＋許可ホスト限定
                              （例 "recommendations.example.com,127.0.0.1:8765"）。未設定＝保護 OFF（トンネル前提）。
- `MCP_OPENAI_APPS_CHALLENGE` … 掲載先（OpenAI のプラグインのディレクトリ）が発行するドメイン確認の文字列。設定時だけ
                              GET `/.well-known/openai-apps-challenge` がその文字列を返す（認証不要）。未設定＝404 のまま。
- `MCP_TOOL_OFFLOAD`       … ツールの実行をイベントループの外（別のスレッド）へ出す（既定＝出す。`0` で切る＝元の動き）。
- `MCP_TOOL_QUEUE_MAX`     … 実行中の 1 本のほかに並べる呼び出しの上限（既定 20）。超えた呼び出しは「混んでいる」と返す。
- `MCP_TOOL_WAIT_SECONDS`  … 並んだ呼び出しが待つ上限の秒数（既定 45）。超えたら「混んでいる」と返す。
- 認証の env（`MCP_ACCESS_*`＝案 A・`MCP_AUTH_*`＝案 B）は `access.install_auth` が読む（装着点はそこ 1 か所。
  契約と差し替えの約束は同関数の docstring・`docs/個人認証_案B設計.md`）。
"""
import os

from polyarchy_common.logsetup import get_logger


# ── ツールの実行をイベントループの外へ（2026-10-14・docs/性能と同時実行_開発計画.md 段 1）────────────────────
# ツールは同期の関数で、SDK はイベントループの上で直に呼ぶ＝重い検索の間は、ほかの利用者の接続の手続きも /healthz も待った
# （運営者 1 人の検索の重なりで health が 2 分続けて 0・アクセスログの所要時間にも待ちが写らなかった）。登録済みのツールを、
# 別のスレッドで動かす async の関数に包む。
# - ツールの定義（名前・説明・引数の型・注釈）は登録のときに決まっていて、ここでは触らない＝コネクタの作り直しは要らない。
# - ★同時に動かすのは 1 本のまま。検索の部品は直列に呼ぶ前提（フィルタを差し替えて使う検索器・分かち書き・モデル）＝
#   ここの数を増やしても同時実行にはならず壊れる。検索そのものの同時実行は、プロセスを分ける形で（同計画 段 3）。
# - 要求ごとの値（利用者ハッシュ・経路のラベル＝contextvar）は、スレッドへ写されて届く＝捕捉ログは欠けない。
# - 捌ききれない呼び出しは黙って並べない：並びすぎ（MCP_TOOL_QUEUE_MAX）・待ちすぎ（MCP_TOOL_WAIT_SECONDS）は、エラーの結果で
#   「混んでいる・実行していない」と返す。「該当なし」（found=false・results 空）と形を分ける＝利用側の LLM が「無い」と読まない。
#   既定値は暫定（箱での実測で決め直す＝同計画 §7）。断った回数はログの目印 `tool_busy=` で数える（observability.tf のフィルタと
#   アラーム busy-<サービス>＝箱が足りていない合図）。
# - 混み具合を読めるように、実行した呼び出しは 1 回ごとに 1 行を出す：`TOOL <名前> wait_ms <待った時間> run_ms <実行の時間> queue <着いたとき
#   に並んでいた数>`。アクセスログの所要時間では待ちと実行が分かれず、ツールの名前も無い。利用者・引数は出さない（検索語は捕捉ログの
#   領分）。行の形を変えるときは RUNBOOK §1 の集計の式も同じ変更で（`test_common` が突き合わせる）。
# FastMCP の内側の名前（_tool_manager・Tool.fn・Tool.is_async）に頼る＝`mcp<2` の固定の内側。`test_common` が形と動きを固定する。
BUSY_MARK = "[busy]"
BUSY_MESSAGE = (BUSY_MARK + " いま混み合っていて、この呼び出しは実行していません（該当なしという意味ではありません）。"
                "30 秒ほどおいて同じ呼び出しをやり直すか、混雑していることを利用者に伝えてください。"
                "ほかの情報源で代わりに埋めないでください。")
TOOL_QUEUE_MAX_DEFAULT, TOOL_WAIT_SECONDS_DEFAULT = 20, 45.0


class ToolBusy(Exception):
    """混んでいて実行しなかった呼び出し（SDK がエラーの結果〔isError〕にして返す）。"""


def offload_tools(mcp, *, enabled: bool = True, queue_max: int = TOOL_QUEUE_MAX_DEFAULT,
                  wait_seconds: float = TOOL_WAIT_SECONDS_DEFAULT, logger_name: str = "polyarchy.mcp") -> list[str]:
    """登録済みの同期のツールを、別のスレッドで 1 本ずつ動かす形に包む。包んだツールの名前を返す（enabled=False なら何もしない）。"""
    if not enabled:
        return []
    import functools
    import time

    import anyio
    log = get_logger(logger_name)
    state = {"gate": None, "waiting": 0}

    def busy(name: str, why: str):
        log.warning("混雑で断った [tool_busy=%s] tool=%s waiting=%d", why, name, state["waiting"])
        raise ToolBusy(BUSY_MESSAGE)

    def wrap(name: str, fn):
        @functools.wraps(fn)
        async def run(**kwargs):
            if state["gate"] is None:              # イベントループの中で作る
                state["gate"] = anyio.Semaphore(1)
            gate = state["gate"]
            arrived, queue = time.monotonic(), state["waiting"]
            try:
                gate.acquire_nowait()
            except anyio.WouldBlock:
                if state["waiting"] >= queue_max:
                    busy(name, "queue")
                state["waiting"] += 1
                got = False
                try:
                    with anyio.move_on_after(wait_seconds):
                        await gate.acquire()
                        got = True
                finally:
                    state["waiting"] -= 1
                if not got:
                    busy(name, "wait")
            started = time.monotonic()
            try:                                   # 利用側が切れても、スレッドの実行が終わるまで次を始めない（1 本ずつを保つ）
                return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))
            finally:
                gate.release()
                log.info("TOOL %s wait_ms %d run_ms %d queue %d", name, (started - arrived) * 1000,
                         (time.monotonic() - started) * 1000, queue)
        return run

    wrapped = []
    for tool in mcp._tool_manager.list_tools():
        if not tool.is_async:
            tool.fn, tool.is_async = wrap(tool.name, tool.fn), True
            wrapped.append(tool.name)
    return wrapped


def _env_number(name: str, default, cast, log):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = cast(raw)
        if v < 0:
            raise ValueError(raw)
        return v
    except ValueError:
        log.warning("%s の値が数として読めない＝既定の %s を使う", name, default)
        return default


def serve_streamable_http(mcp, *, host: str = "127.0.0.1", port: int = 8765, path: str = "/mcp",
                          tools_desc: str = "", logger_name: str = "polyarchy.mcp",
                          health_check=None) -> None:
    """FastMCP を Streamable HTTP（stateless）で待ち受ける。戻らない（サーバ終了まで）。

    health_check（任意）＝サービス固有の軽い検査を返す callable。渡すと GET /healthz が生える
    （認証不要・最外＝運用の生き死に判定。docs/運用設計.md §1.1）。アクセスログは常に装着。"""
    log = get_logger(logger_name)
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.streamable_http_path = path
    mcp.settings.stateless_http = True  # トンネル/プロキシ越しに堅牢（セッション親和性不要）

    # DNS リバインディング保護（Host ヘッダ検証）。トンネル経由だと Host=公開ホスト名で来るため、
    # 既定の localhost 限定だと 421 Misdirected Request になる。トンネル前提＋公開データのみ
    # なので既定は保護OFF＝任意 Host 許可。MCP_ALLOWED_HOSTS を設定すれば保護ONで許可ホスト限定。
    from mcp.server.transport_security import TransportSecuritySettings
    _allowed = os.environ.get("MCP_ALLOWED_HOSTS")
    if _allowed:
        _hosts = [h.strip() for h in _allowed.split(",") if h.strip()]
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=_hosts, allowed_origins=_hosts)
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False)

    log.info("MCPサーバHTTP待受: http://%s:%d%s %sDNS保護=%s", host, port, path,
             (tools_desc + " ") if tools_desc else "",
             "許可ホスト限定" if _allowed else "off(トンネル前提)")

    off = (os.environ.get("MCP_TOOL_OFFLOAD") or "1").strip().lower() not in ("0", "false", "off", "no")
    queue_max = _env_number("MCP_TOOL_QUEUE_MAX", TOOL_QUEUE_MAX_DEFAULT, int, log)
    wait_seconds = _env_number("MCP_TOOL_WAIT_SECONDS", TOOL_WAIT_SECONDS_DEFAULT, float, log)
    wrapped = offload_tools(mcp, enabled=off, queue_max=queue_max, wait_seconds=wait_seconds, logger_name=logger_name)
    if wrapped:
        log.info("ツールの実行=別スレッド（1 本ずつ・並びの上限 %d・待ちの上限 %.0f 秒・%d 本）", queue_max, wait_seconds, len(wrapped))
    else:
        log.info("ツールの実行=イベントループの上（MCP_TOOL_OFFLOAD=0）")

    # ASGI は常に自前で組む（mcp.run(streamable-http) と同じ uvicorn 経路）。
    # ラップ順（外→内）：ドメインの確認（env があるときだけ）→ healthz →（認証：install_auth）→ アクセスログ → MCP アプリ。
    import uvicorn
    from polyarchy_common.httplog import access_log_middleware, domain_challenge_middleware, healthz_middleware
    app = access_log_middleware(mcp.streamable_http_app(), logger_name=logger_name)

    # 認証は access.install_auth の 1 か所で装着する（env を見て案 A／案 B・差し替え点もそこ）。
    from polyarchy_common.access import install_auth
    app = install_auth(app, host=host, port=port, path=path, logger_name=logger_name)
    if health_check is not None:
        app = healthz_middleware(app, health_check, service=logger_name, logger_name=logger_name)
        log.info("healthz=有効（GET /healthz・認証不要）")
    challenged = domain_challenge_middleware(app, os.environ.get("MCP_OPENAI_APPS_CHALLENGE"), logger_name=logger_name)
    if challenged is not app:
        app = challenged
        log.info("ドメインの確認の口=有効（GET /.well-known/openai-apps-challenge・認証不要）")
    uvicorn.run(app, host=host, port=port, log_level="warning")
