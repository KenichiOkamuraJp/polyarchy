"""
MCP Streamable HTTP 待受の定型（リモート公開用・全サービス共通）。

Cloudflare Tunnel 経由で `<service>.<ドメイン>` に出し、ログイン（`access.install_auth`＝現行は外部 IdP の
Bearer 検証）＋秘密パス＋エッジのレート制限で守る構成の"原点"（IP 許可リストは使えない＝ルート CLAUDE.md §5）。
既定は **127.0.0.1 バインド**＝ローカル/トンネル経由のみ到達可（直接インターネット
には晒さない）。各サービスは FastMCP を組み立ててから本関数に渡すだけでよい。

環境変数（サービス横断で同名）：
- `MCP_ALLOWED_HOSTS`      … 設定時は DNS リバインディング保護 ON＋許可ホスト限定
                              （例 "recommendations.example.com,127.0.0.1:8765"）。未設定＝保護 OFF（トンネル前提）。
- 認証の env（`MCP_ACCESS_*`＝案 A・`MCP_AUTH_*`＝案 B）は `access.install_auth` が読む（装着点はそこ 1 か所。
  契約と差し替えの約束は同関数の docstring・`docs/個人認証_案B設計.md`）。
"""
import os

from polyarchy_common.logsetup import get_logger


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

    # ASGI は常に自前で組む（mcp.run(streamable-http) と同じ uvicorn 経路）。
    # ラップ順（外→内）：healthz →（認証：install_auth）→ アクセスログ → MCP アプリ。
    import uvicorn
    from polyarchy_common.httplog import access_log_middleware, healthz_middleware
    app = access_log_middleware(mcp.streamable_http_app(), logger_name=logger_name)

    # 認証は access.install_auth の 1 か所で装着する（env を見て案 A／案 B・差し替え点もそこ）。
    from polyarchy_common.access import install_auth
    app = install_auth(app, host=host, port=port, path=path, logger_name=logger_name)
    if health_check is not None:
        app = healthz_middleware(app, health_check, service=logger_name, logger_name=logger_name)
        log.info("healthz=有効（GET /healthz・認証不要）")
    uvicorn.run(app, host=host, port=port, log_level="warning")
