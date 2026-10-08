"""
HTTP の MCP 入口の認証（純 ASGI ミドルウェア）＋**利用者 ID の取り出し**。装着点は `install_auth` の 1 関数だけ。

■ 現行＝案 B（外部 IdP・個人認証＝2026-09 設計。`docs/個人認証_案B設計.md`）
外部 IdP（WorkOS AuthKit / Auth0 等）を認可サーバ、Polyarchy を Resource Server とする。
`oidc_bearer_middleware` が `Authorization: Bearer` の JWT を IdP の JWKS で検証し、**利用者 ID を contextvar に置く**
（`current_user()`／`user_hash()`）。併せて MCP 認可仕様の Protected Resource Metadata（RFC 9728）を配り、
401 に `WWW-Authenticate: Bearer resource_metadata=…` を付けて認可サーバを発見させる。
捕捉ログには `user_hash`（メールの sha256 先頭 16 桁）だけを残し、メール平文は残さない（プライバシーポリシー記載どおり）。
利用者はログイン必須（ルート CLAUDE.md §3）＝公開する箱で認証が無い構成は bootstrap ⑧ が FAIL にする。

■ 温存＝案 A（Cloudflare Access の Managed OAuth・2026-08 設計・不採用）
数千人規模の名簿限定は Access の席課金では成立しないため採らなかった。点灯手順は残す（`access_jwt_middleware`＝
`Cf-Access-Jwt-Assertion` を RS256／JWKS=`https://<team>/cdn-cgi/access/certs`／`aud`=Access アプリの AUD タグ／
`iss`=`https://<team>` で検証し、同じ contextvar 境界に利用者を置く）。★案 A と案 B を同時に設定しない
（両方の env があると 2 つを重ねて装着する＝両方の検証が必須になる）。

HTTP で公開する全 MCP 入口（recommendations / stats / …）が同じ形で守られる（`mcp_http.serve_streamable_http`
が末尾の `install_auth` を呼び、そこが env `MCP_AUTH_ISSUER` / `MCP_AUTH_AUD` / `MCP_AUTH_RESOURCE_URL`（案 B）・
`MCP_ACCESS_TEAM_DOMAIN` / `MCP_ACCESS_AUD`（案 A）を見て装着する）。導入団体が自前の認証に替えるときは
`install_auth` の中身だけを替える（約束はその docstring）。
"""
import contextvars
import hashlib
import json
from typing import Callable, Optional

from polyarchy_common.logsetup import get_logger

# 認証済み利用者（{"email": str, "sub": str}）。未認証・stdio では None。
_current_user: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("polyarchy_current_user", default=None)


def current_user() -> Optional[dict]:
    """このリクエストの認証済み利用者（`email`・`sub`）。未認証／stdio では None。"""
    return _current_user.get()


def user_hash(email: Optional[str] = None) -> Optional[str]:
    """捕捉ログ用の利用者キー＝メール（小文字化）の sha256 先頭 16 桁。平文は残さない。未認証なら None。

    IdP のアクセストークンにメールが載らない構成（案 B で IdP 依存）では `sub` で代替する
    （`sub:` 接頭辞を付けて衝突空間を分ける）＝利用者キーが取れない状態を作らない。"""
    if email is None:
        u = current_user() or {}
        email = u.get("email") or (f"sub:{u['sub']}" if u.get("sub") else None)
    if not email:
        return None
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def make_access_verifier(team_domain: str, aud: str) -> Callable[[str], dict]:
    """Cloudflare Access 標準の検証関数（RS256・JWKS・aud・iss）を返す。失敗は例外。"""
    import jwt  # PyJWT（cryptography で RS256 検証）

    jwks_url = f"https://{team_domain}/cdn-cgi/access/certs"
    issuer = f"https://{team_domain}"
    jwk_client = jwt.PyJWKClient(jwks_url)

    def verify(token: str) -> dict:
        key = jwk_client.get_signing_key_from_jwt(token)
        return jwt.decode(token, key.key, algorithms=["RS256"], audience=aud, issuer=issuer)

    return verify


def access_jwt_middleware(app, team_domain: str, aud: str, protect_path: str,
                          logger_name: str = "polyarchy.mcp",
                          verify: Optional[Callable[[str], dict]] = None):
    """`protect_path` 配下の http リクエストに Access JWT 検証を課し、通れば利用者 ID を contextvar に置く。

    `verify` はテスト用の差し替え口（既定＝`make_access_verifier(team_domain, aud)`）。
    """
    verify = verify or make_access_verifier(team_domain, aud)
    log = get_logger(logger_name)

    async def _reject(send, msg: str):
        body = json.dumps({"error": msg}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    async def middleware(scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith(protect_path):
            return await app(scope, receive, send)
        token = dict(scope.get("headers") or []).get(b"cf-access-jwt-assertion")
        if not token:
            log.warning("Access JWT 欠如＝Cloudflare Access 非経由のリクエストを遮断")
            return await _reject(send, "missing Cf-Access-Jwt-Assertion")
        try:
            claims = verify(token.decode())
        except Exception as e:  # 署名/aud/iss/期限いずれか不正
            log.warning("Access JWT 検証失敗: %s", e)
            return await _reject(send, "invalid token")
        user = {"email": str(claims.get("email") or ""), "sub": str(claims.get("sub") or "")}
        tok = _current_user.set(user)
        try:
            return await app(scope, receive, send)
        finally:
            _current_user.reset(tok)

    return middleware


# ── 案 B：外部 IdP（個人認証）＝Bearer JWT 検証＋Protected Resource Metadata ────────

def make_oidc_verifier(issuer: str, aud: Optional[str]) -> Callable[[str], dict]:
    """外部 IdP の Bearer JWT を検証する関数を返す。失敗は例外。

    ディスカバリ（OIDC → RFC 8414 の順に試す）で `jwks_uri` を引き（初回検証時に取得・以後キャッシュ
    ＝IdP が一時不達でもサービス起動は落とさない）、署名・`iss`・期限を検証する。`issuer` はトークンの
    `iss` と**完全一致**で書く（Auth0 は末尾スラッシュ付き）。`aud` は与えられたときだけ検証する
    （IdP がトークンに aud を載せるかは dev 検証で確定＝載るなら必ず設定する。空でもエッジの
    レート制限・秘密パスの層は残る）。
    """
    import urllib.request

    import jwt  # PyJWT（cryptography で RS256/ES256 検証）

    state: dict = {}

    def _jwk_client():
        if "client" not in state:
            meta = None
            for well_known in ("openid-configuration", "oauth-authorization-server"):
                url = f"{issuer.rstrip('/')}/.well-known/{well_known}"
                try:
                    with urllib.request.urlopen(url, timeout=10) as r:
                        meta = json.loads(r.read().decode("utf-8"))
                    break
                except Exception:
                    continue
            if not isinstance(meta, dict) or not meta.get("jwks_uri"):
                raise RuntimeError(f"IdP ディスカバリ失敗（issuer={issuer}）")
            state["client"] = jwt.PyJWKClient(meta["jwks_uri"])
        return state["client"]

    def verify(token: str) -> dict:
        key = _jwk_client().get_signing_key_from_jwt(token)
        kwargs: dict = {"algorithms": ["RS256", "ES256"], "issuer": issuer}
        if aud:
            kwargs["audience"] = aud
        else:
            kwargs["options"] = {"verify_aud": False}
        return jwt.decode(token, key.key, **kwargs)

    return verify


def oidc_bearer_middleware(app, issuer: str, aud: Optional[str], protect_path: str, resource_url: str,
                           logger_name: str = "polyarchy.mcp",
                           verify: Optional[Callable[[str], dict]] = None):
    """`protect_path` 配下の http リクエストに外部 IdP の Bearer 検証を課し、通れば利用者 ID を contextvar に置く。

    MCP 認可仕様の要件も担う：`/.well-known/oauth-protected-resource`（RFC 9728）を認証不要で配り、
    401 に `WWW-Authenticate: Bearer resource_metadata=…` を付ける（Claude／ChatGPT はここから
    認可サーバ＝IdP を発見して DCR→ログインへ進む）。`verify` はテスト用の差し替え口
    （既定＝`make_oidc_verifier(issuer, aud)`）。
    """
    from urllib.parse import urlsplit

    verify = verify or make_oidc_verifier(issuer, aud)
    log = get_logger(logger_name)
    parts = urlsplit(resource_url)
    prm_url = f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource"
    prm_body = json.dumps({
        "resource": resource_url,
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
    }).encode()

    async def _send_json(send, status: int, body: bytes, extra_headers=()):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), *extra_headers]})
        await send({"type": "http.response.body", "body": body})

    async def _reject(send, msg: str):
        hdr = (b"www-authenticate", f'Bearer resource_metadata="{prm_url}"'.encode())
        await _send_json(send, 401, json.dumps({"error": msg}).encode(), (hdr,))

    async def middleware(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith("/.well-known/oauth-protected-resource"):
            return await _send_json(send, 200, prm_body)
        if not path.startswith(protect_path):
            return await app(scope, receive, send)
        auth = (dict(scope.get("headers") or []).get(b"authorization") or b"").decode()
        if not auth.lower().startswith("bearer "):
            log.warning("Bearer 欠如＝未認証リクエストを遮断（401＋resource_metadata で誘導）")
            return await _reject(send, "missing bearer token")
        try:
            claims = verify(auth[7:].strip())
        except Exception as e:  # 署名/iss/aud/期限いずれか不正
            log.warning("IdP トークン検証失敗: %s", e)
            return await _reject(send, "invalid token")
        user = {"email": str(claims.get("email") or ""), "sub": str(claims.get("sub") or "")}
        tok = _current_user.set(user)
        try:
            return await app(scope, receive, send)
        finally:
            _current_user.reset(tok)

    return middleware


# ── 認証の装着点（HTTP で公開する全 MCP 入口が通る唯一の場所）──────────────────

def install_auth(app, *, host: str, port: int, path: str, logger_name: str = "polyarchy.mcp"):
    """MCP アプリ（ASGI）に認証を装着して返す。`mcp_http.serve_streamable_http` がここだけを呼ぶ。

    env を見て装着する（サービス横断で同名）：
    - `MCP_ACCESS_TEAM_DOMAIN`＋`MCP_ACCESS_AUD` … 案 A（Cloudflare Access JWT・温存）
    - `MCP_AUTH_ISSUER`（＋`MCP_AUTH_AUD`・`MCP_AUTH_RESOURCE_URL`）… 案 B（外部 IdP の Bearer JWT＋PRM）
    どちらも無ければ素通し（ローカルの stdio 代わり・開発用）。★案 A と案 B を同時に設定しない（両方あれば
    2 つを重ねて装着する＝両方の検証が必須になる）。**公開する箱で認証が無い構成は
    bootstrap ⑧ が FAIL にする**（公開ホストがあるのに `auth_aud_<svc>` が無い＝配布を止める）。

    差し替えの約束（導入団体が自前の認証に替えるときは、この関数の中身だけを替える）：
    - ASGI アプリを受け取り ASGI アプリを返す。`path` 配下を守り、`/healthz` はこの外側（mcp_http が最外に置く）。
    - 利用者を識別するなら `_current_user` に `{"email", "sub"}` を置く（捕捉ログ・アクセスログの
      `user_hash` はそこから立つ。置かなければ `user_hash` が付かないだけで、動作は変わらない）。
    """
    import os

    log = get_logger(logger_name)
    team = os.environ.get("MCP_ACCESS_TEAM_DOMAIN")  # 例 xxxx.cloudflareaccess.com
    aud = os.environ.get("MCP_ACCESS_AUD")           # Access アプリの Application Audience(AUD) タグ
    if team and aud:
        app = access_jwt_middleware(app, team, aud, path, logger_name)
        log.info("Access JWT 検証=有効（team=%s・Cf-Access-Jwt-Assertion 必須の多層防御）", team)
    issuer = os.environ.get("MCP_AUTH_ISSUER")       # 例 https://xxxx.authkit.app（トークンの iss と完全一致）
    if issuer:
        resource = os.environ.get("MCP_AUTH_RESOURCE_URL") or f"http://{host}:{port}{path}"
        app = oidc_bearer_middleware(app, issuer, os.environ.get("MCP_AUTH_AUD"), path, resource, logger_name)
        log.info("IdP Bearer 検証=有効（issuer=%s・resource=%s・案 B 個人認証）", issuer, resource)
    if not (team and aud) and not issuer:
        log.info("認証=未装着（env 未設定＝ローカル開発の素 HTTP。公開する箱では bootstrap ⑧ が止める）")
    return app
