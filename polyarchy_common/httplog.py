"""
HTTP 入口の運用部品（全サービス共通・純 ASGI）：アクセスログと /healthz。

設計＝docs/運用設計.md §1.1・§1.2。`mcp_http.serve_streamable_http` が自動装着する。
ラップ順（外→内）：healthz → （Access JWT 検証）→ アクセスログ → MCP アプリ。
- healthz は最外＝認証不要・内側が壊れていても応答できる（生き死にの判定はここ）。
- アクセスログは Access 検証の内側＝認証済みリクエストに `user_hash` が付く
  （未認証の拒否は access 側が warning を出す）。authless 構成では全リクエストが対象。

ログは journald（stderr）へ 1 行 1 リクエスト：
  HTTP <method> <path> <status> <ms>ms user=<hash|-> ray=<cf-ray|->
CloudWatch Logs に載せた後のメトリクスフィルタ（5xx 率・レイテンシ・401 急増）はこの形式に依存する＝変えるときは
deploy/terraform/monitoring.tf と RUNBOOK を同時に。保持はプライバシーポリシーどおり 30 日（Logs 側の設定）。
"""
import json
import time
from typing import Callable, Optional

from polyarchy_common.logsetup import get_logger


def access_log_middleware(app, logger_name: str = "polyarchy.http"):
    """1 リクエスト 1 行のアクセスログ（stderr）。本文・クエリ値は記録しない（検索語は捕捉ログの領分）。"""
    log = get_logger(logger_name)

    async def middleware(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        t0 = time.monotonic()
        status = {"code": 0}

        async def send_wrap(msg):
            if msg.get("type") == "http.response.start":
                status["code"] = int(msg.get("status", 0))
            await send(msg)

        try:
            return await app(scope, receive, send_wrap)
        finally:
            ms = (time.monotonic() - t0) * 1000
            headers = dict(scope.get("headers") or [])
            ray = (headers.get(b"cf-ray") or b"-").decode("latin-1")
            try:
                from polyarchy_common.access import user_hash
                uh = user_hash() or "-"
            except Exception:
                uh = "-"
            log.info("HTTP %s %s %d %.0fms user=%s ray=%s",
                     scope.get("method", "-"), scope.get("path", "-"), status["code"], ms, uh, ray)

    return middleware


def healthz_middleware(app, health_check: Callable[[], dict], *, service: str,
                       path: str = "/healthz", logger_name: str = "polyarchy.http",
                       version: Optional[str] = None):
    """GET/HEAD <path> に {ok, service, checks} を返す（認証不要・最外に置く）。

    `health_check()` はサービスが渡す軽い検査（例：レジストリ読込数・代表 1 系列の lookup・Qdrant count）。
    dict を返し、キー `ok`（bool）が無ければ「例外なし＝ok」とみなす。例外・ok=False は 503。
    """
    log = get_logger(logger_name)

    async def middleware(scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != path \
                or scope.get("method") not in ("GET", "HEAD"):
            return await app(scope, receive, send)
        try:
            checks = health_check() or {}
            ok = bool(checks.pop("ok", True))
        except Exception as e:  # 検査自体の失敗＝異常
            checks, ok = {"error": str(e)}, False
            log.warning("healthz 検査で例外: %s", e)
        body = json.dumps({"ok": ok, "service": service,
                           **({"version": version} if version else {}), "checks": checks},
                          ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": 200 if ok else 503,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else body})

    return middleware
