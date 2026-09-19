"""
MCP Streamable HTTP 待受の定型（リモート公開用・全サービス共通）。

Cloudflare Tunnel 経由で `<service>.<ドメイン>` に出し、Cloudflare Access／IP 許可／秘密パスで守る
構成の"原点"。既定は **127.0.0.1 バインド**＝ローカル/トンネル経由のみ到達可（直接インターネット
には晒さない）。各サービスは FastMCP を組み立ててから本関数に渡すだけでよい。

環境変数（サービス横断で同名）：
- `MCP_ALLOWED_HOSTS`      … 設定時は DNS リバインディング保護 ON＋許可ホスト限定
                              （例 "recommendations.example.com,127.0.0.1:8765"）。未設定＝保護 OFF（トンネル前提）。
- `MCP_ACCESS_TEAM_DOMAIN` / `MCP_ACCESS_AUD`
                            … 両方設定時は Cloudflare Access JWT 検証（`access.access_jwt_middleware`）を装着（案 A）。
- `MCP_AUTH_ISSUER` / `MCP_AUTH_AUD` / `MCP_AUTH_RESOURCE_URL`
                            … issuer 設定時は外部 IdP の Bearer JWT 検証＋PRM 配信
                              （`access.oidc_bearer_middleware`）を装着（案 B・個人認証＝`docs/個人認証_案B設計.md`）。
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
    # ラップ順（外→内）：healthz →（Access JWT 検証：env があるとき）→ アクセスログ → MCP アプリ。
    import uvicorn
    from polyarchy_common.httplog import access_log_middleware, healthz_middleware
    app = access_log_middleware(mcp.streamable_http_app(), logger_name=logger_name)

    # Cloudflare Access の Managed OAuth を使う本番では、team/aud を env で渡すと
    # Cf-Access-Jwt-Assertion 検証（多層防御）が有効化。未設定ならローカル/トンネル前提の素の HTTP。
    team = os.environ.get("MCP_ACCESS_TEAM_DOMAIN")  # 例 xxxx.cloudflareaccess.com
    aud = os.environ.get("MCP_ACCESS_AUD")           # Access アプリの Application Audience(AUD) タグ
    if team and aud:
        from polyarchy_common.access import access_jwt_middleware
        app = access_jwt_middleware(app, team, aud, path, logger_name)
        log.info("Access JWT 検証=有効（team=%s・Cf-Access-Jwt-Assertion 必須の多層防御）", team)
    else:
        log.info("Access JWT 検証=無効（env 未設定＝ローカル/トンネル前提の素 HTTP）")

    # 外部 IdP（案 B・個人認証）。issuer を env で渡すと Bearer JWT 検証＋Protected Resource
    # Metadata 配信が有効化（Claude/ChatGPT は 401 の resource_metadata から IdP を発見する）。
    issuer = os.environ.get("MCP_AUTH_ISSUER")            # 例 https://xxxx.authkit.app（トークンの iss と完全一致）
    if issuer:
        from polyarchy_common.access import oidc_bearer_middleware
        resource = os.environ.get("MCP_AUTH_RESOURCE_URL") or f"http://{host}:{port}{path}"
        app = oidc_bearer_middleware(app, issuer, os.environ.get("MCP_AUTH_AUD"), path,
                                     resource, logger_name)
        log.info("IdP Bearer 検証=有効（issuer=%s・resource=%s・案 B 個人認証）", issuer, resource)
    if health_check is not None:
        app = healthz_middleware(app, health_check, service=logger_name, logger_name=logger_name)
        log.info("healthz=有効（GET /healthz・認証不要）")
    uvicorn.run(app, host=host, port=port, log_level="warning")
