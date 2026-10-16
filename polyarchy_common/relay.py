"""
中継の口（2026-10-16・B44 第 2 便 ①）＝同じネットワークの中の中継のクライアント（千人規模の問を 1 つの bot が中継する形）
だけが使う、ログインの無い 2 つ目の口。設計＝docs/運用設計.md §4.4・手順＝deploy/PROD_MIGRATION.md §2.6。

ルート CLAUDE.md §3「利用者はログイン必須」の唯一の例外。例外にしてよい条件を、ここで全部守る：
- **既定は閉**：env（`MCP_RELAY_PORT`・`MCP_RELAY_ALLOW_CIDRS`）が揃い、形が正しいときだけ開く（bootstrap が SSM
  `relay_allow_cidrs` から書く・値の正は env ファイルの `RELAY_SOURCE_CIDRS`）。運営者の箱は閉じたまま。
- **同じネットワークの中だけ**：許可の範囲はプライベートの範囲（10/8・172.16/12・192.168/16 の内側・/8 より狭い。127/8 は手元の
  確かめ用）だけ。
  箱の網の入口の規則（terraform の relay ingress）と、ここでの送り元の照合の二重。トンネルの規則には載せない。
- **会員の印が必須**：`X-Polyarchy-Member`＝16 桁の 16 進（bot が会員の ID から作る、元に戻せない文字列）。無い・形が違う
  要求は中身に渡さない（400）。印は要求の中の利用者になる（`access.user_hash()`）＝捕捉ログ・アクセスログ・top-user・
  週次の利用者数が、今の仕組みのまま会員ごとに数える。経路は `relay`（`httplog.current_client()`）。
- **公開の口では印を信じない**：どちらの口に着いたかは、着いたソケットのポート（ASGI の scope["server"]）で分ける。
  公開の口の要求は、印を付けてきても従来どおりログインを求める。

断った要求は warning の行に目印 `relay_reject=source`（送り元が範囲の外）／`relay_reject=member`（印が無い・形が違う）。
"""
import ipaddress
import re
from typing import Optional

from polyarchy_common.logsetup import get_logger

MEMBER_HEADER = b"x-polyarchy-member"
_MEMBER_RE = re.compile(r"[0-9a-f]{16}")
RELAY_HOST_DEFAULT = "0.0.0.0"
_PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))


def parse_allow(raw: Optional[str]) -> Optional[list]:
    """許可の範囲（カンマ区切りの CIDR）を読む。1 つでもプライベートの範囲の外・/8 より広い・読めない値があれば None（閉）。"""
    items = [x.strip() for x in (raw or "").split(",") if x.strip()]
    if not items:
        return None
    nets = []
    for x in items:
        try:
            n = ipaddress.ip_network(x, strict=False)
        except ValueError:
            return None
        if n.version != 4 or n.prefixlen < 8 or not any(n.subnet_of(p) for p in _PRIVATE):
            return None
        nets.append(n)
    return nets


def relay_config(env, *, public_port: int, log) -> Optional[dict]:
    """env から中継の口の設定（host・port・allow）を読む。開かないときは None（理由は warning に・値が無いだけなら黙る）。"""
    raw_port = (env.get("MCP_RELAY_PORT") or "").strip()
    if not raw_port:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        log.warning("MCP_RELAY_PORT が数として読めない＝中継の口は開かない")
        return None
    if not 1024 <= port <= 65535 or port == public_port:
        log.warning("MCP_RELAY_PORT=%s は使えない（1024〜65535・公開の口 %d と別）＝中継の口は開かない", raw_port, public_port)
        return None
    allow = parse_allow(env.get("MCP_RELAY_ALLOW_CIDRS"))
    if allow is None:
        log.warning("MCP_RELAY_ALLOW_CIDRS が無いか、プライベートの範囲の外・広すぎる値を含む＝中継の口は開かない")
        return None
    return {"host": (env.get("MCP_RELAY_HOST") or RELAY_HOST_DEFAULT).strip(), "port": port, "allow": allow}


def relay_dispatch(public_app, relay_app, *, port: int, allow: list, logger_name: str = "polyarchy.mcp"):
    """着いた口で分ける ASGI。中継の口（scope["server"] のポートが `port`）の要求は、送り元と会員の印を確かめて
    `relay_app`（認証を装着していない側）へ。それ以外（公開の口・lifespan）は `public_app`（認証つき）へ。"""
    from polyarchy_common.access import _relay_member
    log = get_logger(logger_name)

    async def _reject(send, status: int, msg: str):
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": ('{"error": "%s"}' % msg).encode()})

    async def middleware(scope, receive, send):
        if (scope.get("server") or (None, None))[1] != port or scope.get("type") == "lifespan":
            return await public_app(scope, receive, send)
        if scope.get("type") != "http":                    # 中継の口は HTTP だけ
            if scope.get("type") == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        src = (scope.get("client") or ("",))[0]
        try:
            inside = any(ipaddress.ip_address(src) in n for n in allow)
        except ValueError:
            inside = False
        if not inside:
            log.warning("中継の口：許可の範囲の外からの要求を遮断 [relay_reject=source]")
            return await _reject(send, 403, "forbidden")
        member = (dict(scope.get("headers") or []).get(MEMBER_HEADER) or b"").decode("latin-1")
        if not _MEMBER_RE.fullmatch(member):
            log.warning("中継の口：会員の印が無いか形が違う要求を遮断 [relay_reject=member]")
            return await _reject(send, 400, "member label required (16 lowercase hex)")
        tok = _relay_member.set(member)
        try:
            return await relay_app(scope, receive, send)
        finally:
            _relay_member.reset(tok)

    return middleware
