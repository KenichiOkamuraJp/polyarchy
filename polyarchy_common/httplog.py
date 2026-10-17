"""
HTTP 入口の運用部品（全サービス共通・純 ASGI）：アクセスログと /healthz・掲載先によるドメインの確認の口。

設計＝docs/運用設計.md §1.1・§1.2。`mcp_http.serve_streamable_http` が自動装着する。
ラップ順（外→内）：ドメインの確認（env があるときだけ）→ healthz → （認証＝access.install_auth）→ アクセスログ → MCP アプリ。
- healthz は最外＝認証不要・内側が壊れていても応答できる（生き死にの判定はここ）。
- アクセスログは認証の内側＝認証済みリクエストに `user_hash` が付く
  （未認証の拒否は access 側が warning を出す）。認証を装着しないローカル開発では全リクエストが対象。

ログは journald（stderr）へ 1 行 1 リクエスト：
  HTTP <method> <path> <status> <ms>ms user=<hash|-> ray=<cf-ray|->
CloudWatch Logs に載せた後のメトリクスフィルタ（5xx）はこの形式に依存する＝変えるときは
deploy/terraform/observability.tf と RUNBOOK を同時に（認証の失敗は、この行ではなく access 側の警告の行の目印を数える）。保持はプライバシーポリシーどおり 30 日（Logs 側の設定）。
"""
import contextvars
import json
import re
import time
from typing import Callable, Optional

from polyarchy_common.logsetup import get_logger

# ── 接続元のクライアントの種別（捕捉ログの `client`・2026-10-12）────────────────────
# 本番と同じ stateless の設定では、ツールの呼び出しのたびに新しいセッションが作られ、MCP の clientInfo は付いてこない
# （届くのは別の要求の initialize だけ）＝呼び出しごとに経路を知る手がかりは HTTP の User-Agent だけ。
# 残すのは決まったラベルだけ＝User-Agent の文字列そのものは捕捉ログにもアクセスログにも残さない。
# ラベルは実測したものだけ（2026-10-12・エッジの記録で確認）：Claude のコネクタ（Web・アプリ・claude.ai のコネクタを使う
# Claude Code＝Anthropic のサーバ経由）は `Claude-User`、ChatGPT は `openai-mcp/<版>`。それ以外（サーバを直に登録した
# クライアント等）は other。★User-Agent は送る側が名乗る値＝利用の内訳を読む目安であって、不正利用の判定には使わない。
# ラベルを足すときは実測してから・公開のプライバシーページ（deploy/pages/privacy.html）の記述も同じ変更で。
# relay＝中継の口（polyarchy_common.relay）に着いた要求。User-Agent ではなく着いた口で決まる（運営者の箱では口が閉じていて出ない）。
CLIENT_LABELS = ("claude", "chatgpt", "other", "relay")
_current_client: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("polyarchy_current_client", default=None)


def client_label(user_agent: Optional[str]) -> str:
    ua = (user_agent or "").strip().lower()
    if ua == "claude-user" or ua.startswith("claude-user/"):
        return "claude"
    if ua.startswith("openai-mcp/"):
        return "chatgpt"
    return "other"


def current_client() -> Optional[str]:
    """この要求の接続元のクライアントの種別（`CLIENT_LABELS` のどれか）。HTTP の要求の外（stdio・集計の実行）では None。"""
    return _current_client.get()


def access_log_middleware(app, logger_name: str = "polyarchy.http"):
    """1 リクエスト 1 行のアクセスログ（stderr）。本文・クエリ値は記録しない（検索語は捕捉ログの領分）。"""
    log = get_logger(logger_name)

    async def middleware(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        t0 = time.monotonic()
        status = {"code": 0}
        headers = dict(scope.get("headers") or [])
        # 捕捉ログが読む（利用者の contextvar と同じ渡し方）。User-Agent はここでラベルに丸め、文字列は持ち回らない。
        from polyarchy_common.access import relay_member
        label = "relay" if relay_member() else client_label((headers.get(b"user-agent") or b"").decode("latin-1"))
        tok = _current_client.set(label)

        async def send_wrap(msg):
            if msg.get("type") == "http.response.start":
                status["code"] = int(msg.get("status", 0))
            await send(msg)

        try:
            return await app(scope, receive, send_wrap)
        finally:
            _current_client.reset(tok)
            ms = (time.monotonic() - t0) * 1000
            ray = (headers.get(b"cf-ray") or b"-").decode("latin-1")
            try:
                from polyarchy_common.access import user_hash
                uh = user_hash() or "-"
            except Exception:
                uh = "-"
            log.info("HTTP %s %s %d %.0fms user=%s ray=%s",
                     scope.get("method", "-"), scope.get("path", "-"), status["code"], ms, uh, ray)

    return middleware


# healthz の検査の待ち（2026-10-17・導入直前の点検 ②）＝箱の毎分の確認（deploy/bootstrap/health_metric.sh の curl -m 5）より短く
# ＝超えたら箱には 503 が届く（打ち切りの 0 ではなく、サービスが「検査が終わらない」と答えた 0）。`test_common` が大小を固定する。
HEALTH_TIMEOUT_SECONDS = 3.0


def healthz_middleware(app, health_check: Callable[[], dict], *, service: str,
                       path: str = "/healthz", logger_name: str = "polyarchy.http",
                       version: Optional[str] = None, timeout_seconds: float = HEALTH_TIMEOUT_SECONDS):
    """GET/HEAD <path> に {ok, service, checks} を返す（認証不要・最外に置く）。

    `health_check()` はサービスが渡す軽い検査（例：レジストリ読込数・代表 1 系列の lookup・Qdrant count）。
    dict を返し、キー `ok`（bool）が無ければ「例外なし＝ok」とみなす。例外・ok=False は 503。
    検査は別のスレッドで動かす＝検査が固まっても（例：Qdrant が応えない）イベントループは回り、`timeout_seconds` を超えたら
    503（`checks.error` に timeout）を返す。超えた検査のスレッドは置き去り（終わり次第消える）＝検査の側の待ちも短くしておく。
    """
    import anyio

    log = get_logger(logger_name)

    async def middleware(scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != path \
                or scope.get("method") not in ("GET", "HEAD"):
            return await app(scope, receive, send)
        try:
            with anyio.fail_after(timeout_seconds):
                checks = await anyio.to_thread.run_sync(health_check, abandon_on_cancel=True) or {}
            ok = bool(checks.pop("ok", True))
        except TimeoutError:  # 検査が終わらない＝異常（固まった検査を待ち続けない）
            checks, ok = {"error": f"timeout({timeout_seconds:g}s)"}, False
            log.warning("healthz 検査が %g 秒で終わらない＝503", timeout_seconds)
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


# 掲載先（ディレクトリ）によるドメインの確認＝確認用の文字列を決まったパスでそのまま返す口。
OPENAI_APPS_CHALLENGE_PATH = "/.well-known/openai-apps-challenge"
_CHALLENGE_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{8,512}")


def domain_challenge_middleware(app, token: Optional[str], *, path: str = OPENAI_APPS_CHALLENGE_PATH,
                                logger_name: str = "polyarchy.http"):
    """GET/HEAD <path> に `token` だけを text/plain で返す（認証不要・最外に置く）。

    掲載先がホストの持ち主を確かめるための口（文字列は掲載先の画面が発行する＝秘密ではなく、公開して確かめさせる値）。
    `token` が空なら装着しない（そのパスは従来どおり 404）。形が想定外（空白・改行・記号）のときも装着せず警告だけ出す
    ＝env の書き損じを外へ返さない。返すのは文字列そのもの（JSON にしない・改行を足さない）。
    """
    token = (token or "").strip()
    if not token:
        return app
    log = get_logger(logger_name)
    if not _CHALLENGE_TOKEN_RE.fullmatch(token):
        log.warning("ドメイン確認の文字列の形が想定外＝返さない（%s は 404 のまま）", path)
        return app
    body = token.encode("ascii")

    async def middleware(scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != path \
                or scope.get("method") not in ("GET", "HEAD"):
            return await app(scope, receive, send)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else body})

    return middleware
