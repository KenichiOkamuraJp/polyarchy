"""
polyarchy_common の最小単体テスト（依存ゼロ・クレジット0）。

実行：  python -m polyarchy_common.tests.test_common
       （pytest があれば pytest polyarchy_common/tests でも可）
"""
import json
import tempfile
from pathlib import Path


def test_metadata_core_validate():
    from polyarchy_common.metadata_core import ALLOWED_LAYERS, COMMON_CORE, validate_payload
    assert set(COMMON_CORE) == {"corpus", "org", "title", "date", "date_int", "source_url", "layer", "lang"}
    good = {"corpus": "recommendations", "org": "keidanren", "title": "t", "date": "", "date_int": None,
            "source_url": "https://x", "layer": "公開", "lang": "ja"}
    assert validate_payload(good) == []
    bad = dict(good, layer="社外秘", org="")
    errs = validate_payload(bad)
    assert any(e.startswith("layer:") for e in errs) and any(e.startswith("org:") for e in errs)
    assert "公開" in ALLOWED_LAYERS and "機密" in ALLOWED_LAYERS and len(ALLOWED_LAYERS) == 2
    assert validate_payload({}) and "corpus: 欠落" in validate_payload({})


def test_taxonomy():
    from polyarchy_common.taxonomy import POLICY_TAGS, TAGS, is_valid_tag
    assert len(POLICY_TAGS) == 21 and len(set(POLICY_TAGS)) == 21
    assert tuple(TAGS.values()) == POLICY_TAGS and TAGS["tax_fiscal"] == "税制・財政" and len(set(TAGS)) == 21
    assert POLICY_TAGS[0] == "マクロ経済・経済財政運営" and POLICY_TAGS[-1] == "農林水産・食料"   # 順序不変（recommendations/stats が依存）
    assert is_valid_tag("税制・財政") and not is_valid_tag("税制")


def test_capture_roundtrip_and_fail_open():
    from polyarchy_common.capture import append_record, dedup, load_records
    assert dedup(["a", "b", "a", "", None]) == ["a", "b"]
    assert dedup([]) is None and dedup(None) is None
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / "q.jsonl"          # 親ディレクトリ未作成でも書ける
        assert append_record(p, {"query": "q1", "n": 1})
        assert append_record(p, {"query": "q2", "n": 2})
        with open(p, "a", encoding="utf-8") as f:
            f.write("{broken\n")                    # 壊れ行は読み飛ばす
        rows = load_records(p)
        assert [r["query"] for r in rows] == ["q1", "q2"]
        assert list(rows[0].keys())[0] == "ts"      # ts は先頭
        raw = p.read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(raw)["n"] == 1
    # fail-open：書けない場所でも例外を投げず False
    assert append_record(Path("/nonexistent_root_dir_for_test/x.jsonl"), {"q": 1}) is False
    assert load_records(Path("/nonexistent_root_dir_for_test/x.jsonl")) == []


def test_logsetup_stdio_guard():
    import sys

    from polyarchy_common.logsetup import get_logger, guard_stdout_for_stdio, quiet_stdout
    log = get_logger("polyarchy.test")
    assert log.handlers  # stderr ハンドラが付く
    with quiet_stdout():
        print("this must not reach stdout")
    saved = sys.stdout
    try:
        real = guard_stdout_for_stdio()
        assert real is saved and sys.stdout is sys.stderr  # 実 stdout を返し、以後の print は stderr へ
    finally:
        sys.stdout = saved


def test_access_middleware_user_context():
    """Access JWT ミドルウェア：欠如/不正は 401・検証成功で current_user が置かれ・終了後に消え・捕捉ログに user_hash が付く。"""
    import asyncio

    from polyarchy_common.access import access_jwt_middleware, current_user, user_hash
    from polyarchy_common.capture import append_record, load_records

    seen: dict = {}

    async def inner(scope, receive, send):
        seen["user"] = current_user()
        seen["hash"] = user_hash()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.jsonl"
            append_record(p, {"tool": "t"})
            seen["rec"] = load_records(p)[0]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    def verify(token: str) -> dict:
        if token != "good":
            raise ValueError("bad token")
        return {"email": "Alice@Example.com", "sub": "sub-1"}

    mw = access_jwt_middleware(inner, "team.example", "aud1", "/mcp", verify=verify)

    async def call(headers, path="/mcp"):
        out = []

        async def send(msg):
            out.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await mw({"type": "http", "path": path, "headers": headers}, receive, send)
        return out[0]["status"]

    async def run():
        assert await call([]) == 401                                          # 欠如
        assert await call([(b"cf-access-jwt-assertion", b"evil")]) == 401     # 不正
        assert await call([(b"cf-access-jwt-assertion", b"good")]) == 200     # 成功
        assert seen["user"] == {"email": "Alice@Example.com", "sub": "sub-1"}
        assert seen["hash"] == user_hash("alice@example.com") and len(seen["hash"]) == 16
        assert seen["rec"]["user_hash"] == seen["hash"] and "email" not in seen["rec"]
        assert current_user() is None and user_hash() is None                # 終了後は消える
        assert await call([], path="/other") == 200                          # 保護対象外は素通し

    asyncio.run(run())


def test_oidc_middleware_user_context():
    """IdP Bearer ミドルウェア（案 B）：PRM は認証不要・欠如/不正は 401＋WWW-Authenticate・
    検証成功で current_user が置かれ・email 無しトークンは sub で user_hash が立つ。"""
    import asyncio

    from polyarchy_common.access import current_user, oidc_bearer_middleware, user_hash

    seen: dict = {}

    async def inner(scope, receive, send):
        seen["user"] = current_user()
        seen["hash"] = user_hash()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    def verify(token: str) -> dict:
        if token == "good":
            return {"email": "Alice@Example.com", "sub": "sub-1"}
        if token == "no-email":
            return {"sub": "user_01ABC"}
        raise ValueError("bad token")

    mw = oidc_bearer_middleware(inner, "https://idp.example", "aud1", "/mcp",
                                "https://stats.example.com/mcp", verify=verify)

    async def call(headers, path="/mcp"):
        out = []

        async def send(msg):
            out.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await mw({"type": "http", "path": path, "headers": headers}, receive, send)
        return out

    async def run():
        # PRM は認証不要＝resource と authorization_servers を配る
        out = await call([], path="/.well-known/oauth-protected-resource")
        assert out[0]["status"] == 200
        prm = json.loads(out[1]["body"])
        assert prm["resource"] == "https://stats.example.com/mcp"
        assert prm["authorization_servers"] == ["https://idp.example"]
        # 欠如/不正は 401＋WWW-Authenticate: Bearer resource_metadata=…
        out = await call([])
        assert out[0]["status"] == 401
        www = dict(out[0]["headers"])[b"www-authenticate"].decode()
        assert 'resource_metadata="https://stats.example.com/.well-known/oauth-protected-resource"' in www
        assert (await call([(b"authorization", b"Bearer evil")]))[0]["status"] == 401
        # 成功＝contextvar に利用者・大文字 Bearer 以外の表記も許す
        assert (await call([(b"authorization", b"bearer good")]))[0]["status"] == 200
        assert seen["user"] == {"email": "Alice@Example.com", "sub": "sub-1"}
        assert seen["hash"] == user_hash("alice@example.com")
        # email 無しトークンは sub で利用者キーが立つ（user_hash の sub フォールバック）
        assert (await call([(b"authorization", b"Bearer no-email")]))[0]["status"] == 200
        assert seen["user"] == {"email": "", "sub": "user_01ABC"}
        assert seen["hash"] == user_hash("sub:user_01ABC") and len(seen["hash"]) == 16
        assert current_user() is None and user_hash() is None  # 終了後は消える
        assert (await call([], path="/other"))[0]["status"] == 200  # 保護対象外は素通し
        # 守る範囲は前方一致（`protect_path` 配下）＝/mcp/・/mcpx もトークンなしでは 401（狭い側に倒さない）。
        # トークンが通れば内側のアプリが答える（実サーバでは /mcp 以外は 404）。PRM は RFC 9728 の末尾つきの形でも配る
        for p in ("/mcp/", "/mcpx", "/mcp/extra"):
            assert (await call([], path=p))[0]["status"] == 401, p
            assert (await call([(b"authorization", b"Bearer good")], path=p))[0]["status"] == 200, p
        assert (await call([], path="/.well-known/oauth-protected-resource/mcp"))[0]["status"] == 200

    asyncio.run(run())



def test_install_auth_single_hook():
    """認証の装着点は access.install_auth の 1 か所：env 無し＝素通し・MCP_AUTH_ISSUER で案 B（PRM 配信）・
    認証の env を読む .py は access.py だけ・systemd は待受アドレスを直書きしない（MCP_HTTP_HOST で渡す）。"""
    import asyncio
    import os
    import re

    from polyarchy_common.access import install_auth

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call(app, path):
        out = []

        async def send(msg):
            out.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await app({"type": "http", "path": path, "headers": []}, receive, send)
        return out

    keys = ("MCP_ACCESS_TEAM_DOMAIN", "MCP_ACCESS_AUD", "MCP_AUTH_ISSUER", "MCP_AUTH_AUD", "MCP_AUTH_RESOURCE_URL")
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        assert install_auth(inner, host="127.0.0.1", port=8765, path="/mcp") is inner  # env 無し＝素通し
        os.environ["MCP_AUTH_ISSUER"] = "https://idp.example"
        os.environ["MCP_AUTH_RESOURCE_URL"] = "https://stats.example.com/mcp-x"
        app = install_auth(inner, host="127.0.0.1", port=8766, path="/mcp-x")
        assert app is not inner
        out = asyncio.run(call(app, "/.well-known/oauth-protected-resource"))
        assert out[0]["status"] == 200 and json.loads(out[1]["body"])["resource"] == "https://stats.example.com/mcp-x"
        assert asyncio.run(call(app, "/mcp-x"))[0]["status"] == 401  # トークン無し＝遮断（IdP へは問い合わせない）
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    root = Path(__file__).resolve().parents[2]
    readers = sorted(str(f.relative_to(root)) for d in ("polyarchy_common", "recommendations", "stats", "companies", "deliberations")
                     for f in (root / d).rglob("*.py")
                     if "tests" not in f.parts and re.search(r"MCP_(AUTH|ACCESS)_[A-Z_]+[\"']", f.read_text(encoding="utf-8")))
    assert readers == ["polyarchy_common/access.py"], readers
    for unit in sorted((root / "deploy" / "systemd").glob("polyarchy-*.service")):
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart=") and " --http" in line:
                assert "--host" not in line, f"{unit.name}: 待受アドレスは MCP_HTTP_HOST（deploy.env）で渡す"

def test_httplog_and_healthz():
    """アクセスログ（1 行/リクエスト・user=- で落ちない）と /healthz（ok=200・ok=False/例外=503・他パス素通し）。"""
    import asyncio

    from polyarchy_common.httplog import access_log_middleware, healthz_middleware

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call(app, path="/mcp", method="GET"):
        out = []

        async def send(msg):
            out.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await app({"type": "http", "path": path, "method": method, "headers": [(b"cf-ray", b"abc123")]},
                  receive, send)
        return out

    async def run():
        logged = await call(access_log_middleware(inner))
        assert logged[0]["status"] == 200                                  # ログは応答を変えない

        state = {"ok": True}

        def check():
            if state.get("raise"):
                raise RuntimeError("broken")
            return {"ok": state["ok"], "n": 1}

        app = healthz_middleware(access_log_middleware(inner), check, service="polyarchy.test")
        out = await call(app, path="/healthz")
        assert out[0]["status"] == 200 and b'"ok": true' in out[1]["body"]
        state["ok"] = False
        assert (await call(app, path="/healthz"))[0]["status"] == 503     # ok=False は 503
        state.update(ok=True, raise_=None)
        state["raise"] = True
        assert (await call(app, path="/healthz"))[0]["status"] == 503     # 検査の例外も 503
        state["raise"] = False
        assert (await call(app, path="/mcp"))[0]["status"] == 200          # 他パスは素通し
        assert (await call(app, path="/healthz", method="HEAD"))[1]["body"] == b""

    asyncio.run(run())


def test_domain_challenge():
    """掲載先によるドメインの確認の口：文字列があればそのパスだけ素の文字列で返す（JSON にしない・改行を足さない・認証の外）・
    無い／形が想定外なら装着しない（従来どおり内側へ＝404）・入口（mcp_http）が env から装着し、bootstrap が SSM から env に渡す。"""
    import asyncio

    from polyarchy_common.httplog import OPENAI_APPS_CHALLENGE_PATH, domain_challenge_middleware

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Not Found"})

    async def call(app, path, method="GET"):
        out = []

        async def send(msg):
            out.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await app({"type": "http", "path": path, "method": method, "headers": []}, receive, send)
        return out

    assert OPENAI_APPS_CHALLENGE_PATH == "/.well-known/openai-apps-challenge"
    for empty in (None, "", "  "):
        assert domain_challenge_middleware(inner, empty) is inner                 # 無ければ装着しない
    for bad in ("tok en-with-space", "line1\nline2xx", '{"token":"abcdefgh"}', "short"):
        assert domain_challenge_middleware(inner, bad) is inner, bad               # 形が想定外＝返さない
    app = domain_challenge_middleware(inner, " abcDEF123_-.~xyz \n")
    out = asyncio.run(call(app, OPENAI_APPS_CHALLENGE_PATH))
    assert out[0]["status"] == 200 and out[1]["body"] == b"abcDEF123_-.~xyz"       # 前後の空白は落とし、文字列だけ
    assert dict(out[0]["headers"])[b"content-type"].startswith(b"text/plain")
    assert asyncio.run(call(app, OPENAI_APPS_CHALLENGE_PATH, "HEAD"))[1]["body"] == b""
    assert asyncio.run(call(app, OPENAI_APPS_CHALLENGE_PATH, "POST"))[0]["status"] == 404
    assert asyncio.run(call(app, OPENAI_APPS_CHALLENGE_PATH + "/x"))[0]["status"] == 404   # 完全一致だけ
    assert asyncio.run(call(app, "/mcp"))[0]["status"] == 404                      # 他パスは素通し

    root = Path(__file__).resolve().parents[2]
    serve = (root / "polyarchy_common/mcp_http.py").read_text(encoding="utf-8")
    assert 'domain_challenge_middleware(app, os.environ.get("MCP_OPENAI_APPS_CHALLENGE")' in serve
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    assert 'ssm_get "openai_apps_challenge_${svc}"' in boot and "MCP_OPENAI_APPS_CHALLENGE=%s" in boot
    # ガードの除外（/.well-known/）がこの口をエッジで止めない＝test_guard_path_rule が式を固定している


def test_usage_report_aggregate():
    """週次集計＝数字のみ（検索語を含まない）・週キー・recommendations/stats の分類・マージ規則（痩せた週は既存優先）。"""
    from polyarchy_common.usage_report import aggregate, merge_weekly, render_html, week_key

    assert week_key("2026-08-17T09:00:00") == ("2026-W34", "2026-08-17")
    assert week_key("壊れたts") is None

    secret = "秘密の検索語その1"
    recs = [
        ("recommendations", {"ts": "2026-08-17T09:00:00", "query": secret, "orgs": ["keidanren"], "since": 2020,
                    "until": None, "result_count": 0, "source": "mcp"}),
        ("recommendations", {"ts": "2026-08-18T10:00:00", "query": secret, "orgs": None, "since": None,
                    "until": None, "result_count": 12, "source": "app_chat"}),
        ("stats", {"ts": "2026-08-18T11:00:00", "tool": "find_statistics", "query": secret, "org": "boj",
                   "result_count": 2, "source": "mcp", "user_hash": "abcd1234abcd1234"}),
        ("stats", {"ts": "2026-08-19T11:00:00", "tool": "lookup_statistic", "series_id": "x.y", "period": "FY2024",
                   "found": False, "reason": "no_values", "source": "mcp", "user_hash": "abcd1234abcd1234"}),
        ("stats", {"ts": "2026-08-19T11:01:00", "tool": "list_datasets", "result_count": 26, "source": "mcp"}),
        # companies の横断検索＝使えなかった入力は、サーバ側の固定の語（unavailable_vocab）だけを件数にする。利用者が書いた語（unavailable）は載せない
        ("companies", {"ts": "2026-08-19T12:00:00", "tool": "screen_companies", "args": {"conditions": [{"expr": secret}]},
                       "unavailable": [{"term": secret, "level": "unknown_item"}],
                       "unavailable_vocab": [{"term": "gross_profit", "level": "input_not_ingested"}]}),
        # 時系列の横断検索（第 1e 便）＝回数は別に数え、使えなかった入力は同じ欄で数える
        ("companies", {"ts": "2026-08-19T12:01:00", "tool": "screen_trend", "args": {"conditions": [{"metric": "operating_profit", "aggregate": "cagr"}]},
                       "unavailable_vocab": [{"term": "operating_profit", "level": "input_not_ingested"}]}),
        # 各サービスが実際に書く形（stats の lookup_panel・companies の参照層の found=false・審議会DB）＝集計の数え漏れを固定する
        ("stats", {"ts": "2026-08-19T13:00:00", "tool": "lookup_panel", "period": "FY2024", "region": None, "source": "mcp",
                   "found": False, "reason": "no_series", "n_errors": 1}),
        ("companies", {"ts": "2026-08-19T13:01:00", "tool": "lookup_company_facts", "args": {"company": secret, "item": "net_sales"},
                       "found": False, "reason": "item_not_disclosed"}),
        ("deliberations", {"ts": "2026-08-19T13:02:00", "tool": "search_deliberations", "query": secret, "hits": 0}),
        ("stats", {"ts": "2026-08-19T13:03:00", "tool": "lookup_newtool", "source": "mcp"}),   # 集計が知らない tool
        # 引数の無い一覧のツール（2026-10-10〜捕捉）＝問いではないので 0 件率・低ヒット率の母数に入れず、回数を別に数える
        ("recommendations", {"ts": "2026-08-19T14:00:00", "query": None, "result_count": 13, "source": "mcp_list_orgs"}),
        ("stats", {"ts": "2026-08-19T14:01:00", "tool": "list_sources", "result_count": 13, "source": "mcp"}),
        ("companies", {"ts": "2026-08-19T14:02:00", "tool": "list_items", "args": {}, "found": None, "reason": None}),
        ("companies", {"ts": "2026-08-19T14:03:00", "tool": "list_metrics", "args": {}, "found": None, "reason": None}),
    ]
    rows = aggregate(recs)
    assert len(rows) == 1 and rows[0]["week"] == "2026-W34"
    w = rows[0]
    assert w["total"] == 15 and w["recommendations"] == 3 and w["stats"] == 6 and w["companies"] == 5 and w["users"] == 1
    assert w["by_source"] == {"app_chat": 1, "mcp": 7, "mcp_list_orgs": 1, "unknown": 6}
    assert w["recommendations_list"] == 1 and w["companies_list"] == 2
    assert w["stats_panel"] == 1 and w["stats_panel_found_false"] == 1
    assert w["companies_found_false"] == 1 and w["companies_nf_reasons"] == {"lookup_company_facts:item_not_disclosed": 1}
    assert w["deliberations"] == 1 and w["deliberations_zero"] == 1
    assert w["unknown_tools"] == {"stats:lookup_newtool": 1}
    assert w["companies_screen"] == 1 and w["companies_trend"] == 1
    assert w["companies_unavailable"] == {"input_not_ingested:gross_profit": 1, "input_not_ingested:operating_profit": 1}
    assert w["recommendations_zero"] == 1 and w["recommendations_low"] == 1 and w["recommendations_orgs_filter"] == 1 and w["recommendations_period_filter"] == 1
    assert w["stats_find"] == 1 and w["stats_find_filtered"] == 1 and w["stats_lookup"] == 1
    assert w["stats_found_false"] == 1 and w["stats_nf_reasons"] == {"no_values": 1, "panel:no_series": 1} and w["stats_catalog"] == 2
    # 検索語は集計行にも HTML にも現れない（数字のみ＝恒久蓄積できる根拠）
    import json as _json
    page = render_html(rows, {}, [], "2026-08-21T00:00:00")
    assert secret not in _json.dumps(rows, ensure_ascii=False) and secret not in page
    assert "2026-W34" in page
    # 0 件率の母数は検索（一覧の呼び出しを除く）＝検索 2 件のうち 0 件 1 件
    assert "recommendations 0 件率 50%" in page, "0 件率の母数に一覧の呼び出しが入っている"

    # マージ：同じ週は再計算で置換。ただし total が痩せた再計算（ログ 30 日削除起因）は既存を残す
    old = [{"week": "2026-W30", "total": 100}, {"week": "2026-W34", "total": 3}]
    merged = merge_weekly(old, rows)
    assert [r["week"] for r in merged] == ["2026-W30", "2026-W34"]
    assert merged[1]["total"] == 15                                    # 増えた再計算は置換
    shrunk = merge_weekly(merged, [{"week": "2026-W30", "total": 2}])
    assert shrunk[0]["total"] == 100                                   # 痩せた再計算は既存優先



def test_usage_report_people():
    """利用者の見え方（2026-10-12・B43 第 1 便）＝サービスごとの利用者数・新規と継続・偏り（最多の 1 人の件数・1 人あたりの
    中央値）・一覧だけで終わった人数・運営者を除いた数字。恒久に残すのは人数と件数だけ＝利用者ハッシュは集計にも HTML にも
    ダッシュボードにも出さない。新規＝直前 3 週に現れていない利用者（生ログは 30 日＝4 週は遡れない）で、遡る分のログが
    残っていない週は数えない（None）＝後の再計算が前に数えた値を消さない。"""
    import json as _json
    from polyarchy_common import ops_dashboard as od
    from polyarchy_common.usage_report import aggregate, merge_weekly, render_html

    OP, U1, U2, U3 = "a" * 16, "1" * 16, "2" * 16, "3" * 16

    def rec(svc, ts, user, **kw):
        return (svc, {"ts": ts, **kw, **({"user_hash": user} if user else {})})

    recs = [rec("stats", "2026-07-27T09:00:00", U1, tool="find_statistics", result_count=3, source="mcp")]     # W31＝W34 の 3 週前
    recs += [rec("stats", f"2026-08-17T09:0{i}:00", OP, tool="find_statistics", result_count=3, source="mcp") for i in range(4)]
    recs += [rec("recommendations", f"2026-08-17T10:0{i}:00", OP, query="q", result_count=5, source="mcp") for i in range(2)]
    # 経路（client）は 2026-10-12 から記録＝付いている記録だけを経路別に数える（U1 は Claude・U2 は ChatGPT・ほかは記録なし）
    recs += [rec("stats", "2026-08-18T09:00:00", U1, tool="find_statistics", result_count=3, source="mcp", client="claude"),
             rec("stats", "2026-08-18T09:01:00", U1, tool="lookup_statistic", found=True, source="mcp", client="claude"),
             rec("companies", "2026-08-19T09:00:00", U2, tool="list_items", args={}, client="chatgpt"),       # 一覧だけで終わった
             rec("recommendations", "2026-08-20T09:00:00", U3, query="q", result_count=5, source="mcp"),
             rec("deliberations", "2026-08-20T09:01:00", U3, tool="list_meeting", hits=8),                    # 審議会DB は一覧だけ
             rec("stats", "2026-08-21T09:00:00", None, tool="find_statistics", result_count=1, source="mcp")]  # 利用者ハッシュなし

    rows = {r["week"]: r for r in aggregate(recs, operator_hashes={OP})}
    w = rows["2026-W34"]
    assert w["total"] == 12 and w["users"] == 4                      # 既存の数字は運営者を含む（変えない）
    assert w["people"] == {
        "calls": 11, "users": 4, "users_new": 3, "user_calls_max": 6, "user_calls_median": 2.0,
        "clients": {"claude": {"calls": 2, "users": 1}, "chatgpt": {"calls": 1, "users": 1}},   # 経路つきの記録だけ（下の 3 件）
        "services": {"recommendations": {"calls": 3, "users": 2, "list_only_users": 0},
                     "stats": {"calls": 6, "users": 2, "list_only_users": 0},
                     "companies": {"calls": 1, "users": 1, "list_only_users": 1},
                     "deliberations": {"calls": 1, "users": 1, "list_only_users": 1}}}, w["people"]
    assert w["people_ext"] == {
        "calls": 5, "users": 3, "users_new": 2, "user_calls_max": 2, "user_calls_median": 2.0,
        "clients": {"claude": {"calls": 2, "users": 1}, "chatgpt": {"calls": 1, "users": 1}},
        "services": {"recommendations": {"calls": 1, "users": 1, "list_only_users": 0},
                     "stats": {"calls": 2, "users": 1, "list_only_users": 0},
                     "companies": {"calls": 1, "users": 1, "list_only_users": 1},
                     "deliberations": {"calls": 1, "users": 1, "list_only_users": 1}}}, w["people_ext"]
    assert rows["2026-W31"]["people"]["users_new"] is None            # 遡る分のログが無い週は数えない
    assert aggregate(recs)[-1]["people_ext"] is None                  # 運営者の指定なし＝除いた数字は作らない

    weekly = list(rows.values())
    dump = _json.dumps(weekly, ensure_ascii=False)
    page = render_html(weekly, {}, [], "2026-08-24T00:20:00")
    saved = od.recent_weeks
    try:
        od.recent_weeks = lambda n=4: weekly
        dash = od.render("2026-08-24T09:05:00+09:00", "test")
    finally:
        od.recent_weeks = saved
    for h in (OP, U1, U2, U3):
        assert h not in dump and h not in page and h not in dash, "利用者ハッシュが出力に出ている"
    assert "直近の完了した週（2026-W34" in page                       # 月曜の朝の生成で、始まったばかりの週を見出しにしない
    assert "今週（途中・2026-W34" in render_html([w], {}, [], "2026-08-21T00:00:00")                    # 完了した週が無ければ今週
    assert "<td>2026-W34</td><td>4</td><td>3</td><td>2・2・1・1</td><td>2</td><td>55%</td><td>0・0・1・1</td><td>1・1・0・0</td><td>5</td><td>3</td><td>2</td><td>1・1・1・1</td><td>1・1・0・0</td>" in page, page
    assert "<td>2026-W31</td><td>1</td><td>—</td>" in page and "<th>経路別の利用者</th>" in page
    assert "<th>companies</th><th>deliberations</th>" in page and "<th>companies</th><th>deliberations</th>" in dash
    assert "<td>2026-W34</td><td>12</td><td>3</td><td>7</td><td>1</td><td>1</td><td>4</td><td>5</td><td>3</td>" in dash, dash
    old_style = [{"week": "2026-W20", "week_start": "2026-05-11", "total": 9, "recommendations": 4, "stats": 5, "users": 1, "by_source": {},
                  "recommendations_zero": 0, "recommendations_low": 0, "recommendations_orgs_filter": 0, "recommendations_period_filter": 0,
                  "stats_find": 0, "stats_find_zero": 0, "stats_find_filtered": 0, "stats_lookup": 0, "stats_found_false": 0,
                  "stats_nf_reasons": {}, "stats_catalog": 0}]
    assert "<td>2026-W20</td><td>1</td><td>—</td>" in render_html(old_style, {}, [], "2026-08-24T00:20:00")   # 足す前に蓄積した行

    # マージ＝遡る分のログが消えた後の再計算（新規が None）・運営者の指定なしの再計算は、前に数えた値を消さない
    later = aggregate([r for r in recs if r[1]["ts"] >= "2026-08-17"])
    assert later[0]["people"]["users_new"] is None and later[0]["people_ext"] is None
    merged = merge_weekly([w], later)[0]
    assert merged["people"]["users_new"] == 3 and merged["people_ext"]["users_new"] == 2 and merged["people_ext"]["users"] == 3


def test_usage_operator_hashes_wiring():
    """運営者を除いた数字の配線（2026-10-12）＝SSM usage_operator_hashes → bootstrap が形を確かめて /etc/polyarchy/usage.env →
    週次レポートのシェルが読む → usage_report が env から受け取る。値（運営者の利用者ハッシュ）をリポジトリに書かない。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    read = lambda p: (root / p).read_text(encoding="utf-8")   # noqa: E731
    boot, weekly, report = read("deploy/bootstrap/bootstrap.sh"), read("deploy/bootstrap/usage_report_weekly.sh"), read("polyarchy_common/usage_report.py")
    assert '"$(ssm_get usage_operator_hashes)"' in boot
    m = re.search(r'\[\[ "\$OP_HASHES" =~ (\S+) \]\]', boot)
    assert m, "bootstrap が値の形を確かめていない"
    pat = re.compile(m.group(1))
    assert pat.fullmatch("a" * 16) and pat.fullmatch("a" * 16 + "," + "0123456789abcdef")
    for bad in ("", "abc", "a" * 16 + ",", "a" * 16 + " " + "b" * 16, "$(reboot)", "A" * 16):
        assert not pat.fullmatch(bad), bad                       # env ファイルは source される＝形の外は書かない
    assert "printf 'USAGE_OPERATOR_HASHES=%s\\n' \"$OP_HASHES\" > /etc/polyarchy/usage.env" in boot
    assert "rm -f /etc/polyarchy/usage.env" in boot              # SSM から消したら箱からも消える
    assert "source /etc/polyarchy/usage.env" in weekly
    assert 'os.environ.get("USAGE_OPERATOR_HASHES"' in report


def test_client_label_capture():
    """経路の記録（2026-10-12・B43 第 2 便）＝捕捉ログに接続元のクライアントの種別（決まったラベル）を足す。User-Agent の
    文字列そのものは残さない。ラベルは実測した 2 つ（Claude のコネクタ＝Claude-User・ChatGPT＝openai-mcp/…）とそれ以外。
    stateless の設定ではツールの呼び出しに clientInfo が付かない＝手がかりは User-Agent だけ（送る側が名乗る値＝目安）。"""
    import asyncio
    import re
    import tempfile
    from pathlib import Path
    from polyarchy_common import httplog
    from polyarchy_common.capture import append_record, load_records

    assert httplog.CLIENT_LABELS == ("claude", "chatgpt", "other", "relay")   # relay＝中継の口（User-Agent ではなく着いた口で決まる）
    cases = {"Claude-User": "claude", "claude-user/1.0": "claude", "openai-mcp/1.0.0": "chatgpt", "OpenAI-MCP/2": "chatgpt",
             "polyarchy-edge-probe/1": "other", "Mozilla/5.0 (Macintosh)": "other", "curl/8.4.0": "other", "": "other", None: "other",
             "x-Claude-User": "other", "not-openai-mcp/1": "other"}
    for ua, want in cases.items():
        assert httplog.client_label(ua) == want, (ua, want)
    assert httplog.current_client() is None                            # 要求の外（stdio・集計の実行）では付かない

    d = Path(tempfile.mkdtemp())
    log_path = d / "q.jsonl"

    async def inner(scope, receive, send):                             # ツールの中で捕捉する（各サービスの MCP と同じ位置）
        append_record(log_path, {"tool": "t", "query": "q"})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = httplog.access_log_middleware(inner, logger_name="polyarchy.test_client")

    async def call(ua):
        async def send(_):
            pass
        headers = [(b"user-agent", ua.encode())] if ua is not None else []
        await app({"type": "http", "path": "/mcp", "method": "POST", "headers": headers}, None, send)

    ua_secret = "openai-mcp/1.0.0 (build 12345; trace abcdef)"
    for ua in ("Claude-User", ua_secret, "SomeAgent/9", None):
        asyncio.run(call(ua))
    append_record(log_path, {"tool": "t"})                             # 要求の外
    recs = load_records(log_path)
    assert [r.get("client") for r in recs] == ["claude", "chatgpt", "other", "other", None], recs
    raw = log_path.read_text(encoding="utf-8")
    assert "build 12345" not in raw and "SomeAgent" not in raw and "user-agent" not in raw.lower()   # 文字列そのものは残さない
    assert httplog.current_client() is None                            # 要求が終われば消える

    # 記録する項目を足したら、公開のプライバシーページの「収集する情報」にも書く（同じ変更で）
    root = Path(__file__).resolve().parents[2]
    page = (root / "deploy/pages/privacy.html").read_text(encoding="utf-8")
    row = re.search(r"<tr><td>利用記録（捕捉ログ）</td><td>(.*?)</td>", page, re.S)
    assert row and "接続元のクライアントの種別" in row.group(1) and "利用者ハッシュ" in row.group(1), row and row.group(1)


def test_ops_dashboard_memory_and_fuelsync():
    """ダッシュボード①のメモリ（サービスごとの現在と起動からの最大・箱全体）と捕捉ログの S3 同期（2026-10-01）。
    箱の Ubuntu 22.04 の systemd 249 には MemoryPeak が無い＝最大は cgroup v2 の memory.peak を読む。
    時刻は 1 枚の中で JST にそろえる（2026-10-02＝起動の「Thu … UTC」と同期の ISO の UTC が冒頭の JST と混ざっていた）。"""
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from polyarchy_common import ops_dashboard as od

    d = Path(tempfile.mkdtemp())
    (d / "meminfo").write_text("MemTotal:       16000000 kB\nMemFree:  1000 kB\nMemAvailable:   12000000 kB\n")
    (d / "cg" / "system.slice" / "polyarchy-companies.service").mkdir(parents=True)
    (d / "cg" / "system.slice" / "polyarchy-companies.service" / "memory.peak").write_text(str(800 * 2**20) + "\n")
    (d / "cg" / "system.slice" / "polyarchy-companies.service" / "memory.stat").write_text(f"anon {200 * 2**20}\nfile {100 * 2**20}\nkernel 0\n")
    (d / "fs").mkdir()
    (d / "fs" / "companies.result").write_text("failed\n")
    (d / "fs" / "companies.last_ok").write_text("2026-10-01T12:28:00+00:00\n")
    (d / "fs" / "stats.result").write_text("absent\n")
    (d / "fs" / "last_run").write_text("2026-10-01T13:28:00+00:00\n")

    def fake_run(cmd, **kw):
        u = cmd[2]
        if u == "polyarchy-companies":
            out = ("ActiveState=active\nMemoryCurrent=" + str(300 * 2**20) + "\nControlGroup=/system.slice/polyarchy-companies.service\n"
                   "ActiveEnterTimestamp=Thu 2026-10-01 01:35:40 UTC\n")
        elif u == "qdrant":
            out = "ActiveState=active\nMemoryCurrent=[not set]\nControlGroup=\nActiveEnterTimestamp=\n"
        else:
            out = "ActiveState=inactive\n"
        return SimpleNamespace(stdout=out, returncode=0)

    saved = (od.MEMINFO_PATH, od.CGROUP_ROOT, od.FUELSYNC_DIR, od.subprocess.run, od.shutil.which, od.companies_enabled)
    try:
        od.MEMINFO_PATH, od.CGROUP_ROOT, od.FUELSYNC_DIR = d / "meminfo", d / "cg", d / "fs"
        od.subprocess.run, od.shutil.which, od.companies_enabled = fake_run, (lambda _: "/bin/systemctl"), (lambda: True)
        # 使用の率に並べて空きの率（2026-10-13＝アラーム mem は空きの率で数える＝メールから来た人が読み替えずに済むように。
        # 空きは health_metric.sh が送る値と同じ切り捨て）
        assert od.box_memory() == "3.81 GB ／ 15.26 GB（使用 25%・空き 75%）", od.box_memory()
        od.MEMINFO_PATH.write_text("MemTotal:       15420000 kB\nMemAvailable:   9380000 kB\n")
        assert od.box_memory() == "5.76 GB ／ 14.71 GB（使用 39%・空き 60%）", od.box_memory()   # 60.83 → 60（メトリクスと同じ）
        assert "アラーム mem は空きの率" in od.render("2026-10-13T12:00:00+09:00", "test")
        mem = {u: (cur, file_, peak, since) for u, cur, file_, peak, since in od.service_memory()}
        assert mem == {"polyarchy-companies": ("0.29 GB", "0.10 GB", "0.78 GB", "2026-10-01T10:35:40+09:00"),
                       "qdrant": ("—", "—", "—", "—")}, mem   # 止まっているユニットは載せない・ファイルキャッシュは memory.stat の file
        fs = od.fuelsync_status()
        assert fs["services"]["companies"] == {"result": "failed", "last_ok": "2026-10-01T21:28:00+09:00"}
        assert fs["services"]["recommendations"]["result"] == "—" and fs["last_run"] == "2026-10-01T22:28:00+09:00"
        assert od._jst("2026-10-01T22:28:00+09:00") == "2026-10-01T22:28:00+09:00" and od._jst("") == "" and od._jst("—") == "—"
    finally:
        od.MEMINFO_PATH, od.CGROUP_ROOT, od.FUELSYNC_DIR, od.subprocess.run, od.shutil.which, od.companies_enabled = saved


def test_ops_dashboard_failed_units_and_restarts():
    """ダッシュボード①＝失敗したユニットと自動再起動の回数（2026-10-12）。アラーム unit-failed・restarts は箱で 1 本ずつ＝
    どのユニットかは、箱のシェルに入らずここで見る。"""
    from types import SimpleNamespace
    from polyarchy_common import ops_dashboard as od

    def fake_run(cmd, **kw):
        if cmd[1] == "is-active":
            out = "active\n"
        elif cmd[1] == "show":
            out = {"polyarchy-mcp": "2\n", "qdrant": "0\n"}.get(cmd[2], "\n")
        else:   # list-units --state=failed
            assert cmd[1:3] == ["list-units", "--state=failed"], cmd
            out = "polyarchy-logprune.service loaded failed failed Polyarchy 捕捉ログの保持期間超過分を削除\n"
        return SimpleNamespace(stdout=out, returncode=0)

    saved = (od.subprocess.run, od.shutil.which, od.companies_enabled, od.deliberations_enabled)
    try:
        od.subprocess.run, od.shutil.which = fake_run, (lambda _: "/bin/systemctl")
        od.companies_enabled = od.deliberations_enabled = lambda: False
        st = {u: (state, n) for u, state, n in od.unit_states()}
        assert st["polyarchy-mcp"] == ("active", "2") and st["qdrant"] == ("active", "0"), st
        assert st["polyarchy-stats"] == ("active", "—") and st["polyarchy-logprune.timer"] == ("active", "—"), st   # 取れない・タイマー
        assert od.failed_units() == ["polyarchy-logprune.service"]
        page = od.render("2026-10-12T12:00:00+09:00", "test")
        assert "失敗したユニット：<span class=\"ng\">polyarchy-logprune.service</span>" in page
        assert "<td>polyarchy-mcp</td><td class=\"ok\">active</td><td class=\"ng\">2</td>" in page
        od.shutil.which = lambda _: None
        assert od.unit_states() == [] and od.failed_units() is None
        assert "失敗したユニット" not in od.render("2026-10-12T12:00:00+09:00", "test")   # ローカル生成では出さない
    finally:
        od.subprocess.run, od.shutil.which, od.companies_enabled, od.deliberations_enabled = saved


def test_ops_dashboard_lists_every_timer():
    """ダッシュボード①のユニットの表（ops_dashboard.UNITS）に、bootstrap が置いて有効にする timer が全部ある（2026-10-16＝
    polyarchy-topuser.timer を足したとき固定の一覧に足し忘れ、staging の表に行が出なかった）。timer のファイル・bootstrap の
    install と enable・表の 4 つが揃っていること。"""
    import re
    from pathlib import Path
    from polyarchy_common import ops_dashboard as od
    root = Path(__file__).resolve().parents[2]
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    files = {t.name for t in (root / "deploy/systemd").glob("polyarchy-*.timer")}
    installed = set(re.findall(r'^install [^\n]*"\$UNIT_SRC/(polyarchy-[\w-]+\.timer)"', boot, re.M))
    enabled = set(re.findall(r"^systemctl enable --now (polyarchy-[\w-]+\.timer)", boot, re.M))
    shown = {u for u in od.UNITS if u.endswith(".timer")}
    assert files and files == installed == enabled == shown, {
        "ファイルだけ": files - shown, "表に無い": (installed | enabled) - shown,
        "install だけ": installed - enabled, "enable だけ": enabled - installed, "表だけ": shown - files}


def _sync_cmds(path) -> list[str]:
    """deploy のシェルの aws s3 sync を 1 コマンドずつ（行末の \\ でつないだ継続行を 1 行に・コメント行は除く）。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    text = re.sub(r"\\\n\s*", " ", (root / path).read_text(encoding="utf-8"))
    return [ln for ln in text.splitlines() if "s3 sync" in ln and not ln.lstrip().startswith("#")]


# 箱で生まれるもの＝手元から S3 に上げない・S3 から箱へ戻さない（S3 の写しは古い＝戻すと箱の最新を上書きする）。
BOX_BORN = ("cache/*", "query_log/*")


def test_box_born_not_shipped():
    """箱で生まれるもの（cache/＝新着チェックの結果 等・query_log/＝捕捉ログ）を、サービスのデータの丸ごとの同期で
    手元から S3 に上げない・S3 から箱へ戻さない（2026-10-02 staging＝手元の 09-03 の update_check.json が upload で S3 に載り、
    自動適用の bootstrap 再走行の同期が箱の最新の値を上書きした。2026-10-09＝政策主張DB の同期だけ query_log/ を除外しておらず、
    自動適用のたびに S3 の写し〔fuelsync の最長 1 時間前〕が箱の原本の追記分を消していた）。"""
    import re
    # 上り＝<svc>/data/ の丸ごと（qdrant のミラーと審議会DB の束はサブディレクトリだけ＝対象外）
    up = [c for c in _sync_cmds("deploy/scripts/upload_to_s3.sh") if re.search(r'/data/" "s3://', c)]
    down = [c for c in _sync_cmds("deploy/bootstrap/bootstrap.sh") if re.search(r'" "\$(APP_DIR|REPO_DIR/\w+)/data/"', c)]
    assert len(up) == 3 and len(down) == 3, (up, down)   # recommendations・stats・companies
    bad = [(b, c) for c in up + down for b in BOX_BORN if f'--exclude "{b}"' not in c]
    assert not bad, bad


def test_box_s3_pull_exact_timestamps():
    """S3→箱の aws s3 sync は全部 --exact-timestamps（既定は大きさが同じファイルを取り直さない＝中身が変わっても古いまま
    残る。2026-10-07＝審議会DB の束の目印 bundle.json が同じ 614 バイトで 10-04 以降の配布で復元されなかった・B28）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    pulls = [f"{p.name}: {c.strip()}" for p in sorted((root / "deploy/bootstrap").glob("*.sh"))
             for c in _sync_cmds(p.relative_to(root)) if "s3 sync \"s3://" in c]
    assert len(pulls) >= 5, pulls   # bootstrap の 4 本＋apply の qdrant のミラー
    bad = [c for c in pulls if "--exact-timestamps" not in c]
    assert not bad, bad


def test_guard_path_rule():
    """入口ガードの「正しいパス以外をエッジで遮断」の式（deploy/scripts/cloudflare-guard.sh の path_rule_expr）。
    秘密パスあり＝そのパスで始まるもの以外を遮断／値として `/mcp` を入れた構成（秘密パスなしで公開）＝`/mcp` と `/mcp/` 以外を遮断
    （2026-10-10＝それまでは `/mcp` のとき規則を作らず、無関係なパスも旧い秘密パスも箱まで届いていた）／値が無い＝規則を作らない
    （SSM の取得に失敗したときに「`/mcp` 以外は遮断」を作ると、秘密パスで動いている箱の正規の要求が全部止まる）。
    どの式も認証の生命線 3 つ（/.well-known/・/cdn-cgi/・/healthz）を除外する。"""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]

    def expr(path: str) -> str:
        r = subprocess.run(["bash", "-c", 'source deploy/scripts/cloudflare-guard.sh; path_rule_expr'], cwd=root,
                           capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "MCP_HOST": "svc.example.com",
                                "ZONE_NAME": "example.com", "MCP_PATH": path})
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    keep = ('not starts_with(http.request.uri.path, "/.well-known/")', 'not starts_with(http.request.uri.path, "/cdn-cgi/")',
            'not http.request.uri.path eq "/healthz"')
    public = expr("/mcp")
    assert 'http.host eq "svc.example.com"' in public and 'not http.request.uri.path in {"/mcp" "/mcp/"}' in public, public
    assert all(k in public for k in keep) and "starts_with(http.request.uri.path, \"/mcp\")" not in public, public
    secret = expr("/mcp-0123abcd")
    assert 'not starts_with(http.request.uri.path, "/mcp-0123abcd")' in secret and all(k in secret for k in keep), secret
    assert expr("") == ""


def test_capture_log_services_aligned():
    """捕捉ログを持つサービスの一覧（usage_report の S3_PREFIXES／LOG_DIRS＝正）が、箱の fuelsync（S3 への保護コピー）・
    logprune（30 日で削除）・箱の書込権限（iam.tf）・S3 の保持ルール（storage.tf）に揃っている（2026-10-09＝便を足すたびに
    写しが漏れていた。漏れると保持 30 日の約束か燃料の保護が黙って外れる）。"""
    import re
    from pathlib import Path
    from polyarchy_common import usage_report as ur
    root = Path(__file__).resolve().parents[2]
    read = lambda p: (root / p).read_text(encoding="utf-8")   # noqa: E731
    want = {s: p.removeprefix("data/") for s, p in ur.S3_PREFIXES.items()}          # query_log・stats/query_log …
    local = {s: str(p.relative_to(ur.ROOT)) for s, p in ur.LOG_DIRS.items()}       # recommendations/data/query_log …
    assert set(want) == set(local), (want, local)
    fuel = re.findall(r'^sync_one (\w+) "\$REPO_DIR/([^"]+)" (\S+)\s*$', read("deploy/bootstrap/fuelsync.sh"), re.M)
    assert {s: (l, r) for s, l, r in fuel} == {s: (local[s], want[s]) for s in want}, fuel
    restore = re.findall(r'^restore_fuel "\$(?:APP_DIR|REPO_DIR)/([^"]+)" (\S+)\s*$', read("deploy/bootstrap/bootstrap.sh"), re.M)
    assert {r: l for l, r in restore} == {want[s]: local[s].removeprefix("recommendations/") if s == "recommendations" else local[s]
                                          for s in want}, restore   # 新しい箱で燃料を戻す（bootstrap ⑥）も同じ一覧
    prune = re.findall(r'^prune_one (\w+) "\$REPO_DIR/([^"]+)"\s*$', read("deploy/bootstrap/logprune.sh"), re.M)
    assert dict(prune) == local, prune
    iam = set(re.findall(r'"\$\{var\.data_s3_prefix\}/(\S*query_log)/\*"', read("deploy/terraform/iam.tf")))
    assert iam == set(want.values()), iam
    rules = set(re.findall(r'prefix = "(\S*query_log)/"', read("deploy/terraform/storage.tf")))
    assert rules == set(want.values()), rules


def test_box_service_list_aligned():
    """opt-in のサービス（user_data の ENABLE_<X>_APP＝正・廃止した WEB は除く）が、自動適用の停止・起動・smoke・/healthz・
    退避、health のメトリクス、fuelsync・logprune、ログ転送、アラーム、rollback.sh の status に揃っている（2026-10-09＝
    サービス追加の配線は deploy/ の 26〜30 ファイルに及び、漏れても何も落ちない）。ポートは health と apply で一致すること。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    read = lambda p: (root / p).read_text(encoding="utf-8")   # noqa: E731
    svcs = {m.lower() for m in re.findall(r"^ENABLE_(\w+)_APP=", read("deploy/terraform/user_data.sh.tftpl"), re.M)} - {"web"}
    assert svcs == {"stats", "companies", "deliberations"}, svcs   # サービスを足したら、この関数の各所にも揃える
    apply, health = read("deploy/bootstrap/apply_data_update.sh"), read("deploy/bootstrap/health_metric.sh")
    files = {"fuelsync": read("deploy/bootstrap/fuelsync.sh"), "logprune": read("deploy/bootstrap/logprune.sh"),
             "cwagent": read("deploy/bootstrap/cloudwatch-agent.json"), "alarm": read("deploy/terraform/observability.tf"),
             "rollback": read("deploy/scripts/rollback.sh")}
    bad = []
    for s in sorted(svcs):
        checks = {
            "apply の停止": re.search(rf"systemctl stop [^\n]*\bpolyarchy-{s}\b", apply),
            "apply の起動": re.search(rf'systemctl (?:restart|"\$d?verb") [^\n]*\bpolyarchy-{s}\b', apply),   # svc_start は restart か start
            "apply の smoke": f"-m {s}.eval.mcp_smoke" in apply,
            "apply の退避": f'"$REPO_DIR/{s}/data/"' in apply,
            "fuelsync": f"sync_one {s} " in files["fuelsync"],
            "logprune": f"prune_one {s} " in files["logprune"],
            "ログ転送": f'"polyarchy/{s}"' in files["cwagent"],
            "アラーム": f'"health_{s}"' in files["alarm"],
            "rollback status": re.search(rf"for s in [^;]*\bpolyarchy-{s}\b", files["rollback"]),
        }
        bad += [f"{s}: {k}" for k, ok in checks.items() if not ok]
        hp = re.search(rf"Value={s}\}}\],Value=\$\(probe (\d+)\)", health)
        ap = re.search(rf"wait_healthz {s} (\d+)", apply)
        if not (hp and ap and hp.group(1) == ap.group(1)):
            bad.append(f"{s}: health と apply の /healthz のポート（{hp and hp.group(1)}・{ap and ap.group(1)}）")
    assert not bad, bad


def test_auth_reject_filter_matches_log_line():
    """「認証の失敗の急増」のメトリクスフィルタ（observability.tf）の文字列が、認証が実際に出す警告の行に含まれる（2026-10-12・
    B35＝旧フィルタはアクセスログの ` 401 ` を数えていたが、アクセスログは認証の内側＝拒否は行にならず、30 日で 0 件だった）。
    数えるのは「トークンを出したのに通らない」だけ＝トークン無し（新しい接続の最初の 1 回に必ず出る）は数えない。"""
    import asyncio
    import logging
    import re
    from pathlib import Path
    from polyarchy_common.access import access_jwt_middleware, oidc_bearer_middleware
    root = Path(__file__).resolve().parents[2]
    tf = (root / "deploy/terraform/observability.tf").read_text(encoding="utf-8")
    m = re.search(r'resource "aws_cloudwatch_log_metric_filter" "auth_invalid_token" \{.*?pattern\s*=\s*"\\"([^"\\]+)\\""', tf, re.S)
    assert m, "auth_invalid_token のフィルタが無い"
    pat = m.group(1)
    assert pat.isascii(), pat   # フィルタの文字列は ASCII（日本語の文言の一致に頼らない）
    assert '" 401 "' not in tf.replace("\\", ""), "アクセスログの 401 を数える旧フィルタが残っている"

    records: list[str] = []

    class H(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())

    def bad(token: str) -> dict:
        raise ValueError("bad token")

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def call(app, headers):
        async def send(_):
            pass
        await app({"type": "http", "path": "/mcp", "method": "POST", "headers": headers}, None, send)

    lg = logging.getLogger("polyarchy.test_auth_reject")
    h = H()
    lg.addHandler(h)
    lg.setLevel(logging.WARNING)
    try:
        oidc = oidc_bearer_middleware(inner, "https://idp.example", "aud", "/mcp", "https://x.example/mcp", logger_name=lg.name, verify=bad)
        asyncio.run(call(oidc, [(b"authorization", b"Bearer evil")]))
        asyncio.run(call(oidc, []))
        acc = access_jwt_middleware(inner, "team.example", "aud", "/mcp", logger_name=lg.name, verify=bad)
        asyncio.run(call(acc, [(b"cf-access-jwt-assertion", b"evil")]))
        asyncio.run(call(acc, []))
    finally:
        lg.removeHandler(h)
    assert len(records) == 4, records
    assert [pat in r for r in records] == [True, False, True, False], (pat, records)   # 不正＝数える・欠如＝数えない


def test_timer_units_notify_on_failure():
    """定期実行のユニット（polyarchy-*.timer の相手）は、失敗をメトリクス unit_failed に送る（2026-10-12＝logprune が止まると
    保持 30 日の約束が黙って外れるが、知らせる経路が無かった）。例外は理由つきでここに書く。失敗を無視する ExecStart=- を
    置かない（OnFailure が働かない）。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    sysd = root / "deploy/systemd"
    exempt = {
        "polyarchy-health": "毎分＝止まれば health が missing になり health のアラームが鳴る（失敗の知らせも同じ API に頼る）",
        "polyarchy-dataapply": "切り戻し・中止は自分のメトリクス dataapply=0 で知らせる（同じ出来事でメールを 2 通にしない）",
    }
    timers = sorted(t.stem for t in sysd.glob("polyarchy-*.timer"))
    assert timers and set(exempt) <= set(timers), timers
    bad = []
    for name in timers:
        unit = (sysd / f"{name}.service").read_text(encoding="utf-8")
        has = re.search(r"^OnFailure=polyarchy-unit-failed@%p\.service$", unit, re.M)
        if name in exempt:
            if has:
                bad.append(f"{name}: 例外なのに OnFailure がある")
            continue
        if not has:
            bad.append(f"{name}: OnFailure が無い")
        if re.search(r"^ExecStart=-", unit, re.M):
            bad.append(f"{name}: ExecStart=-（失敗を無視）")
    assert not bad, bad
    handler = (sysd / "polyarchy-unit-failed@.service").read_text(encoding="utf-8")
    assert re.search(r"^ExecStart=/usr/local/bin/polyarchy-unit-failed %i$", handler, re.M), handler
    assert not re.search(r"^OnFailure=", handler, re.M), handler   # 知らせる側の失敗で自分を呼ばない
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    assert '"$UNIT_SRC/polyarchy-unit-failed@.service"' in boot and "/usr/local/bin/polyarchy-unit-failed" in boot


def test_box_metrics_and_alarms_paired():
    """箱が送るメトリクス（health_metric.sh・unit_failed.sh・apply_data_update.sh の MetricName）と、アラーム（observability.tf の
    namespace polyarchy）が 1 対 1（2026-10-12＝送っているのに鳴らない・鳴らす先のメトリクスが来ない、を配る前に止める）。
    ログから数えるメトリクスは、フィルタとアラームの名前が対になっていること。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    read = lambda p: (root / p).read_text(encoding="utf-8")   # noqa: E731
    sent = set()
    for src in ("deploy/bootstrap/health_metric.sh", "deploy/bootstrap/unit_failed.sh", "deploy/bootstrap/apply_data_update.sh",
                "deploy/bootstrap/edge_probe.sh", "deploy/bootstrap/top_user_metric.sh"):
        sent |= set(re.findall(r"MetricName=(\w+)", read(src)))
    tf = read("deploy/terraform/observability.tf")
    alarms = re.findall(r'resource "aws_cloudwatch_metric_alarm" "\w+" \{(.*?)\n\}', tf, re.S)
    custom = [re.search(r'metric_name\s*=\s*"([^"]+)"', a).group(1) for a in alarms if re.search(r'namespace\s*=\s*"polyarchy"', a)]
    from_logs = set(re.findall(r'metric_transformation \{\s*name\s*=\s*"([^"]+)"', tf))
    assert from_logs and sent, (from_logs, sent)
    assert set(custom) == sent | from_logs, (sorted(set(custom) ^ (sent | from_logs)))
    assert {"health", "disk_used_percent", "mem_available_percent", "service_restarts", "unit_failed", "dataapply",
            "edge", "idp_reachable", "top_user_calls_1h"} == sent, sent


def test_health_metric_script_runs():
    """health_metric.sh を偽の curl・aws・systemctl・df で実行する（2026-10-12）。このスクリプトが壊れると health が全部 missing に
    なり全サービスのアラームが鳴る＝足した分（メモリ・再起動）は別の送信に分け、取れない値は送らない。再起動は前回との差
    （初回・数え直しは 0）。"""
    import os
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    d = Path(tempfile.mkdtemp())
    (d / "bin").mkdir()
    (d / "deploy.env").write_text("AWS_REGION=ap-northeast-1\nENABLE_STATS_APP=true\nENABLE_COMPANIES_APP=false\n")
    (d / "meminfo").write_text("MemTotal:       16000000 kB\nMemFree:  1000 kB\nMemAvailable:   4000000 kB\n")
    stubs = {
        "curl": 'case "$*" in *8766*) exit 22;; esac; exit 0',
        "df": 'printf "Use%%\n 42%%\n"',
        "aws": f'echo "$*" >> "{d}/aws.log"',
        "systemctl": f'cat "{d}/restarts"',
    }
    for name, body in stubs.items():
        f = d / "bin" / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}", "POLYARCHY_DEPLOY_ENV": str(d / "deploy.env"),
           "POLYARCHY_MEMINFO": str(d / "meminfo"), "POLYARCHY_RESTART_STATE": str(d / "state")}

    def run(restarts: str) -> list[str]:
        (d / "restarts").write_text(restarts)
        (d / "aws.log").write_text("")
        rc = subprocess.run(["bash", str(root / "deploy/bootstrap/health_metric.sh")], env=env, capture_output=True, text=True)
        assert rc.returncode == 0, rc.stderr
        return (d / "aws.log").read_text().splitlines()

    first = run("2\n\n0\n\n1\n")
    assert len(first) == 2, first                                  # 生き死に＋ディスク／メモリ＋再起動＝送信を分ける
    assert "Value=mcp}],Value=1," in first[0] and "Value=stats}],Value=0," in first[0] and "companies" not in first[0], first[0]
    assert "MetricName=disk_used_percent,Dimensions=[{Name=service,Value=box}],Value=42," in first[0], first[0]
    assert "MetricName=mem_available_percent,Dimensions=[{Name=service,Value=box}],Value=25," in first[1], first[1]
    assert "MetricName=service_restarts,Dimensions=[{Name=service,Value=box}],Value=0," in first[1], first[1]   # 初回は差を取らない
    assert "MetricName=service_restarts,Dimensions=[{Name=service,Value=box}],Value=2," in run("3\n\n1\n\n1\n")[1]   # 3 → 5
    assert "MetricName=service_restarts,Dimensions=[{Name=service,Value=box}],Value=0," in run("0\n\n0\n\n1\n")[1]   # 数え直し（手で再起動）
    (d / "meminfo").write_text("")
    last = run("0\n\n0\n\n1\n")
    assert len(last) == 2 and "mem_available_percent" not in last[1] and "service_restarts" in last[1], last   # 取れない値は送らない


def _edge_fetch(table: dict):
    """edge_probe の偽の取得＝(メソッド, URL) → (status, ヘッダ, 本文)。表に無い URL は届かない（status 0）。"""
    def fetch(url, method="GET"):
        st, hdr, body = table.get((method, url), (0, {}, b""))
        return st, hdr, body if isinstance(body, bytes) else json.dumps(body).encode()
    return fetch


def test_edge_probe_checks():
    """外からの到達の確認（2026-10-12・B42 第 2 便）＝公開 URL の /healthz が 200・守るパスがトークン無しで 401＋
    WWW-Authenticate・ディスカバリ（PRM）の resource と発行者が env と一致・env の aud が resource と同じ文字列（三点一致の
    うち箱の側の 2 点）。IdP は発行者のディスカバリと公開鍵。失敗は確認の名前で返し、パス（秘密パスの構成では合言葉）を出さない。"""
    from polyarchy_common import edge_probe as ep
    base, path, iss = "https://stats.example.com", "/secret-path-xyz", "https://idp.example"
    res = base + path
    t = {"service": "stats", "resource_url": res, "issuer": iss, "aud": res}
    prm = {"resource": res, "authorization_servers": [iss]}
    www = {"www-authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'}
    good = {("GET", base + "/healthz"): (200, {}, {"ok": True}),
            ("POST", res): (401, www, {"error": "missing bearer token"}),
            ("GET", base + "/.well-known/oauth-protected-resource"): (200, {}, prm)}
    assert ep.check_service(t, _edge_fetch(good)) == []

    def bad(key, value, target=t):
        return ep.check_service(target, _edge_fetch({**good, key: value}))

    assert bad(("GET", base + "/healthz"), (503, {}, b"")) == ["healthz(503)"]
    assert bad(("GET", base + "/healthz"), (0, {}, b"")) == ["healthz(届かない)"]                  # トンネル・DNS
    assert bad(("POST", res), (403, {}, b"")) == ["mcp(403)"]                                       # エッジの規則が止めている
    assert bad(("POST", res), (200, {}, b"{}")) == ["mcp(200＝認証なしで通る)"]
    assert bad(("POST", res), (401, {}, b"")) == ["mcp(WWW-Authenticate)"]                          # クライアントが IdP を見つけられない
    assert bad(("GET", base + "/.well-known/oauth-protected-resource"), (403, {}, b"")) == ["prm(403)"]
    other = {"resource": "https://companies.example.com/mcp", "authorization_servers": [iss]}
    assert bad(("GET", base + "/.well-known/oauth-protected-resource"), (200, {}, other)) == ["prm(resource)"]   # 別のサービスに届いている
    assert bad(("GET", base + "/.well-known/oauth-protected-resource"), (200, {}, {**prm, "authorization_servers": ["https://x"]})) == ["prm(issuer)"]
    assert bad(("GET", base + "/.well-known/oauth-protected-resource"), (200, {}, b"<html>")) == ["prm(本文)"]
    assert ep.check_service({**t, "aud": base + "/mcp"}, _edge_fetch(good)) == ["aud≠resource"]    # パスだけ替えて aud を替え忘れた
    assert ep.check_service({**t, "aud": ""}, _edge_fetch(good)) == []                              # aud を検証しない構成
    every = ep.check_service({**t, "aud": "x"}, _edge_fetch({}))
    assert every == ["healthz(届かない)", "mcp(届かない)", "prm(届かない)", "aud≠resource"], every
    assert not any(path in f for f in every)

    jwks = "https://idp.example/oauth2/jwks"
    idp_ok = {("GET", iss + "/.well-known/oauth-authorization-server"): (200, {}, {"jwks_uri": jwks}),   # 1 つ目（OIDC）は無い IdP
              ("GET", jwks): (200, {}, {"keys": [{"kid": "a"}]})}
    assert ep.check_idp(iss, _edge_fetch(idp_ok)) == ""
    assert ep.check_idp(iss + "/", _edge_fetch(idp_ok)) == ""                                       # 発行者の末尾のスラッシュ
    assert ep.check_idp(iss, _edge_fetch({})) == "discovery(届かない)"
    assert ep.check_idp(iss, _edge_fetch({**idp_ok, ("GET", jwks): (500, {}, b"")})) == "jwks(500)"
    assert ep.check_idp(iss, _edge_fetch({**idp_ok, ("GET", jwks): (200, {}, {"keys": []})})) == "jwks(鍵が無い)"


def test_edge_probe_run_and_dashboard():
    """確認の対象は箱のサービスごとの env（MCP_AUTH_RESOURCE_URL のあるもの）から読む＝サービスの一覧を持たない。結果は 1 行ずつ
    （シェルがメトリクスにする）と状態ファイル（ダッシュボード①が読む）。秘密パスはどちらにも出さない。"""
    import io
    import tempfile
    from pathlib import Path
    from polyarchy_common import edge_probe as ep
    from polyarchy_common import ops_dashboard as od
    d = Path(tempfile.mkdtemp())
    iss, secret = "https://idp.example", "/secret-path-xyz"
    (d / "deploy.env").write_text("AWS_REGION=ap-northeast-1\nTUNNEL_HOST_MCP=recommendations.example.com\n")
    (d / "mcp.env").write_text(f"MCP_HTTP_PATH=/mcp\nMCP_AUTH_ISSUER={iss}\nMCP_AUTH_AUD=https://recommendations.example.com/mcp\n"
                               "MCP_AUTH_RESOURCE_URL=https://recommendations.example.com/mcp\n")
    (d / "stats.env").write_text(f"MCP_HTTP_PATH={secret}\nMCP_AUTH_ISSUER={iss}\nMCP_AUTH_AUD=https://stats.example.com{secret}\n"
                                 f"MCP_AUTH_RESOURCE_URL=https://stats.example.com{secret}\n")
    (d / "companies.env").write_text("MCP_HTTP_PATH=/mcp\n")                                       # 認証の無い env は対象外
    targets = ep.load_targets(d)
    assert [t["service"] for t in targets] == ["mcp", "stats"], targets
    assert targets[1] == {"service": "stats", "resource_url": f"https://stats.example.com{secret}", "issuer": iss,
                          "aud": f"https://stats.example.com{secret}"}

    def table_for(t):
        base = t["resource_url"].rsplit("/", 1)[0]
        return {("GET", base + "/healthz"): (200, {}, {"ok": True}),
                ("POST", t["resource_url"]): (401, {"www-authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'}, b""),
                ("GET", base + "/.well-known/oauth-protected-resource"): (200, {}, {"resource": t["resource_url"], "authorization_servers": [iss]})}
    table = {**table_for(targets[0]), **table_for(targets[1]),
             ("GET", iss + "/.well-known/openid-configuration"): (200, {}, {"jwks_uri": iss + "/jwks"}),
             ("GET", iss + "/jwks"): (200, {}, {"keys": [{}]})}
    table[("POST", targets[1]["resource_url"])] = (403, {}, b"")                                     # stats だけエッジで止まっている
    out, state = io.StringIO(), d / "state" / "edge_probe.json"
    assert ep.run(d, state, fetch=_edge_fetch(table), out=out, now="2026-10-12T03:02:10+00:00") == 0
    assert out.getvalue().splitlines() == ["edge mcp 1", "edge stats 0", "idp 1"], out.getvalue()
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["services"]["stats"] == {"host": "stats.example.com", "ok": False, "failed": ["mcp(403)"]}, saved
    assert saved["idp"] == {"ok": True, "failed": ""} and secret not in state.read_text(encoding="utf-8") and secret not in out.getvalue()

    keep = (od.EDGE_PROBE_PATH, od.shutil.which)
    try:
        od.EDGE_PROBE_PATH, od.shutil.which = state, (lambda _: None)
        page = od.render("2026-10-12T12:05:00+09:00", "test")
        assert "<td>stats</td><td>stats.example.com</td><td class=\"ng\">NG</td><td>mcp(403)</td>" in page
        assert "<td>mcp</td><td>recommendations.example.com</td><td class=\"ok\">ok</td><td></td>" in page
        assert "最終確認 2026-10-12T12:02:10+09:00" in page and secret not in page
        od.EDGE_PROBE_PATH = d / "none.json"
        assert "外からの確認" not in od.render("2026-10-12T12:05:00+09:00", "test")                  # まだ 1 回も走っていない箱
    finally:
        od.EDGE_PROBE_PATH, od.shutil.which = keep

    out2 = io.StringIO()
    assert ep.run(d / "nowhere", d / "state2.json", fetch=_edge_fetch({}), out=out2, now="2026-10-12T03:02:10+00:00") == 0
    assert out2.getvalue() == ""                                                                    # 公開していない箱＝何も送らない


def test_edge_probe_script_sends_metrics():
    """edge_probe.sh＝確認の結果の行をメトリクスにして送る（偽の python・aws で実行）。結果が 1 行も無い箱（公開していない）は
    送らず 0 で終わる。確認そのものが落ちたら非 0（→ OnFailure → unit-failed）。"""
    import os
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    d = Path(tempfile.mkdtemp())
    (d / "bin").mkdir()
    (d / "deploy.env").write_text(f"AWS_REGION=ap-northeast-1\nREPO_DIR={d}\n")
    (d / "bin" / "aws").write_text(f'#!/bin/bash\necho "$*" >> "{d}/aws.log"\n')
    (d / "bin" / "fakepy").write_text(f'#!/bin/bash\ncat "{d}/lines"; exit $(cat "{d}/rc")\n')
    for f in (d / "bin").iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}", "POLYARCHY_DEPLOY_ENV": str(d / "deploy.env"),
           "POLYARCHY_EDGE_PY": str(d / "bin" / "fakepy")}

    def run(lines: str, rc: int = 0):
        (d / "lines").write_text(lines)
        (d / "rc").write_text(str(rc))
        (d / "aws.log").write_text("")
        r = subprocess.run(["bash", str(root / "deploy/bootstrap/edge_probe.sh")], env=env, capture_output=True, text=True)
        return r.returncode, (d / "aws.log").read_text().splitlines()

    rc, sent = run("edge mcp 1\nedge stats 0\nidp 1\n")
    assert rc == 0 and len(sent) == 1, (rc, sent)
    assert "MetricName=edge,Dimensions=[{Name=service,Value=mcp}],Value=1," in sent[0]
    assert "MetricName=edge,Dimensions=[{Name=service,Value=stats}],Value=0," in sent[0]
    assert "MetricName=idp_reachable,Dimensions=[{Name=service,Value=box}],Value=1," in sent[0]
    assert run("") == (0, [])
    rc, sent = run("", rc=1)
    assert rc != 0 and sent == []


def test_preflight_checks_qdrant_response():
    """配る前の確認は、手元の Qdrant のマウント元に加えて応答を見る（2026-10-13）。止まったコンテナでも docker inspect は通る＝
    マウント元だけでは ✓ が出て、ゲートが全部通った後（審議会DB の手前）で初めて止まった（2026-10-12 staging）。
    偽の docker・curl・aws で preflight を実行する（aws は失敗させる＝呼ばれていれば a 段を通った）。"""
    import os
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    d = Path(tempfile.mkdtemp())
    repo = d / "repo"
    (repo / "recommendations/data/qdrant").mkdir(parents=True)
    (repo / "deliberations/data/qdrant").mkdir(parents=True)
    (d / "bin").mkdir()
    stubs = {
        "docker": f'case "$2" in qdrant-dev) echo "{repo}/recommendations/data/qdrant";; qdrant-delib) echo "{repo}/deliberations/data/qdrant";; esac',
        "curl": f'echo "$*" >> "{d}/curl.log"; for p in $(cat "{d}/down"); do case "$*" in *:"$p"/*) exit 7;; esac; done; exit 0',
        "aws": f'echo "$*" >> "{d}/aws.log"; exit 1',
    }
    for name, body in stubs.items():
        f = d / "bin" / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)

    def run(delib: str, down: str, **extra):
        (d / "down").write_text(down)
        (d / "curl.log").write_text("")
        (d / "aws.log").write_text("")
        base = {k: v for k, v in os.environ.items() if k != "DELIB_QDRANT_URL"}
        env = {**base, "PATH": f"{d / 'bin'}:{os.environ['PATH']}", "REPO_DIR": str(repo), "AWS_PROFILE": "x",
               "ENABLE_DELIBERATIONS_APP": delib, **extra}
        r = subprocess.run(["bash", "-c", f'source "{root}/deploy/scripts/release_preflight.sh"; preflight'],
                           env=env, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr, (d / "curl.log").read_text(), (d / "aws.log").read_text()

    rc, out, err, curls, aws = run("true", "6340")            # 審議会DB の Qdrant が止まっている＝a 段で止まる
    assert rc != 0 and aws == "", (rc, out, err)
    assert "qdrant-delib" in err and "応答しない" in err and "docker start qdrant-delib" in err, err
    assert "✓ qdrant-dev が応答する" in out and "✓ qdrant-delib が応答する" not in out, out
    rc, out, err, curls, aws = run("false", "6340")           # 無効な箱の配布では見ない
    assert aws != "" and "6340" not in curls and "qdrant-delib" not in out + err, (out, err, curls)
    rc, out, err, curls, aws = run("true", "")                # 両方応答＝a 段を通る
    assert aws != "" and "✓ qdrant-dev が応答する" in out and "✓ qdrant-delib が応答する" in out, (out, err)
    rc, out, err, curls, aws = run("true", "6333")            # 政策主張DB の側も同じ段で
    assert rc != 0 and aws == "" and "qdrant-dev" in err and "応答しない" in err, (rc, out, err)
    rc, out, err, curls, aws = run("true", "6399", DELIB_QDRANT_URL="http://localhost:6399")   # release.sh と同じ変数で接続先を読む
    assert rc != 0 and aws == "" and ":6399/" in curls and "6340" not in curls, (out, err, curls)


def test_deploy_plan_then_apply_saved_plan():
    """deploy.sh の plan／apply-plan（2026-10-13）＝対話なしで流す形は「保存した plan をそのまま適用する」経路だけ。
    apply は対話で yes を待つ＝セッションからは流せず、運用側が lib.sh の中の名前に頼って手で組んでいた（2026-10-12 staging）。
    plan は読み取りだけ・apply-plan は保存した plan が無ければ止まる（plan を読む段を飛ばせない）・適用の直後の plan に
    差分が残れば非 0。偽の terraform・aws で、スクリプトの写しを実行する（env ファイルは写しの側に置く）。"""
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    d = Path(tempfile.mkdtemp())
    for sub in ("deploy/scripts", "deploy/env", "deploy/terraform", "bin"):
        (d / sub).mkdir(parents=True)
    for name in ("deploy.sh", "lib.sh"):
        shutil.copy(root / "deploy/scripts" / name, d / "deploy/scripts" / name)
    (d / "deploy/env/t.env").write_text("ENVIRONMENT=t\nAWS_PROFILE=x\nAWS_REGION=ap-northeast-1\n")
    stubs = {
        "aws": 'echo 123456789012',
        "terraform": (f'echo "$*" >> "{d}/tf.log"\n'
                      'for a in "$@"; do case "$a" in -out=*) : > "${a#-out=}";; esac; done\n'
                      f'case "$*" in *-detailed-exitcode*) exit $(cat "{d}/left");; esac; exit 0'),
    }
    for name, body in stubs.items():
        f = d / "bin" / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}"}
    plan_file = d / "deploy/terraform/.terraform/polyarchy-t.tfplan"

    def run(phase: str, left: int = 0):
        (d / "left").write_text(str(left))
        (d / "tf.log").write_text("")
        r = subprocess.run(["bash", str(d / "deploy/scripts/deploy.sh"), "t", phase], env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        return r.returncode, [ln for ln in (d / "tf.log").read_text().splitlines() if not ln.startswith("init")], r.stderr

    rc, tf, err = run("apply-plan")                     # 保存した plan が無い＝適用しない
    assert rc != 0 and tf == [] and "plan" in err, (rc, tf, err)
    rc, tf, err = run("plan", left=2)                   # 差分あり＝読み取りだけ＋保存
    assert rc == 0 and len(tf) == 1 and tf[0].startswith("plan ") and f"-out={plan_file}" in tf[0], (rc, tf, err)
    assert "-var project=polyarchy" in tf[0] and "-var environment=t" in tf[0] and plan_file.exists(), tf
    rc, tf, err = run("apply-plan")                     # 保存した plan だけを渡す（-var を付けない）→ 直後の plan で差分 0 を確かめる
    assert rc == 0 and tf[0] == f"apply -input=false {plan_file}", (rc, tf, err)
    assert len(tf) == 2 and tf[1].startswith("plan ") and "-detailed-exitcode" in tf[1] and "-out=" not in tf[1], tf
    assert not plan_file.exists()                       # 使った plan は残さない（2 回は適用できない）
    run("plan", left=2)
    rc, tf, err = run("apply-plan", left=2)             # 適用の直後の plan に差分が残る＝非 0
    assert rc != 0 and "差分" in err, (rc, err)
    # 差分の無い plan は残さない（2026-10-13 staging＝No changes の plan のファイルが残り、後日 plan を取り直さずに apply-plan を
    # 流すと古い plan が当たる形だった）。残っていた古い plan も、差分なしの plan を取れば消える。plan が失敗したときも残さない。
    run("plan", left=2)
    rc, tf, err = run("plan", left=0)
    assert rc == 0 and len(tf) == 1 and "-detailed-exitcode" in tf[0] and not plan_file.exists(), (rc, tf, err)
    rc, tf, err = run("apply-plan")
    assert rc != 0 and tf == [] and "plan" in err, (rc, tf, err)
    rc, tf, err = run("plan", left=1)
    assert rc != 0 and not plan_file.exists(), (rc, tf, err)
    # 全体を対話なしで適用する経路は、保存した plan だけ（-auto-approve は初回構築のバケットの先行作成＝-target つきの 1 か所）
    lib = (root / "deploy/scripts/lib.sh").read_text(encoding="utf-8")
    assert lib.count("-auto-approve") == 1 and "-target=aws_s3_bucket_versioning.data" in lib.split("-auto-approve")[1].split(")")[0], "lib.sh"
    assert "-auto-approve" not in (root / "deploy/scripts/deploy.sh").read_text(encoding="utf-8")


def test_instance_type_from_env():
    """箱の機種は env の INSTANCE_TYPE から（2026-10-15・B45 段 4）＝書けば plan に -var instance_type が渡り、空なら渡さない
    （既定は variables.tf の 1 か所）。Graviton（arm64）は variables.tf の validation が止める。env のひな形の両方に行があり、
    RUNBOOK に上げる・戻す手順がある。偽の terraform・aws で、スクリプトの写しを実行する。"""
    import os
    import re
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    d = Path(tempfile.mkdtemp())
    for sub in ("deploy/scripts", "deploy/env", "deploy/terraform", "bin"):
        (d / sub).mkdir(parents=True)
    for name in ("deploy.sh", "lib.sh"):
        shutil.copy(root / "deploy/scripts" / name, d / "deploy/scripts" / name)
    (d / "bin/aws").write_text("#!/bin/bash\necho 123456789012\n")
    (d / "bin/terraform").write_text(f'#!/bin/bash\necho "$*" >> "{d}/tf.log"\nexit 0\n')
    for f in ("aws", "terraform"):
        (d / "bin" / f).chmod(0o755)
    env = {**os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}"}
    env.pop("INSTANCE_TYPE", None)

    def plan_line(extra: str) -> str:
        (d / "deploy/env/t.env").write_text("ENVIRONMENT=t\nAWS_PROFILE=x\nAWS_REGION=ap-northeast-1\n" + extra)
        (d / "tf.log").write_text("")
        subprocess.run(["bash", str(d / "deploy/scripts/deploy.sh"), "t", "plan"], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
        return next(ln for ln in (d / "tf.log").read_text().splitlines() if ln.startswith("plan "))

    assert "-var instance_type=c7i.2xlarge" in plan_line("INSTANCE_TYPE=c7i.2xlarge\n")
    assert "instance_type" not in plan_line("INSTANCE_TYPE=\n")
    assert "instance_type" not in plan_line("")
    var = (root / "deploy/terraform/variables.tf").read_text(encoding="utf-8").split('variable "instance_type"')[1].split("\n}")[0]
    assert 'default     = "t3.xlarge"' in var and "validation" in var, var
    rx = re.search(r'regex\("([^"]+)"', var).group(1)
    for t, ok in (("t3.xlarge", True), ("c7i.2xlarge", True), ("c7i.4xlarge", True), ("m7i-flex.large", True),
                  ("c7g.2xlarge", False), ("t4g.xlarge", False), ("m7gd.large", False)):
        assert (re.match(rx, t) is None) == ok, t
    for ex in ("staging", "prod"):
        assert re.search(r"^INSTANCE_TYPE=$", (root / f"deploy/env/{ex}.env.example").read_text(encoding="utf-8"), re.M), ex
    rb = (root / "deploy/RUNBOOK_OPS.md").read_text(encoding="utf-8")
    sec = rb.split("### 機種を上げる・戻す")[1].split("\n### ")[0]
    assert "INSTANCE_TYPE" in sec and "L-1216C47A" in sec and "update in-place" in sec and "戻す" in sec, "RUNBOOK"


def test_edge_probe_tick_clear_of_apply_start():
    """外からの到達の確認の刻みを、自動適用の始まりから離す（2026-10-13）。自動適用は 15 分の刻みで始まり、約 2 分後に
    サービスが起動する＝始まりの 2 分後に定刻があると、毎回、起動の間際に当たって APPLIED の直後のダッシュボードに NG が載る
    （2026-10-12 staging＝アラームは鳴らないが、配布の確認でまずここを読む人は障害と読む）。timer のファイルから読んで固定する。"""
    import re
    from pathlib import Path
    units = Path(__file__).resolve().parents[2] / "deploy" / "systemd"

    def minutes(name: str) -> list[int]:
        cal = re.search(r"^OnCalendar=(.+)$", (units / name).read_text(encoding="utf-8"), re.M).group(1).split()[-1]
        mm = cal.split(":")[1]                                  # 時:分[:秒] の分
        if "/" in mm:
            start, step = (int(x) for x in mm.split("/"))
            return list(range(start, 60, step))
        return [int(mm)]

    apply_at, probe_at, dash_at = minutes("polyarchy-dataapply.timer"), minutes("polyarchy-edgeprobe.timer"), minutes("polyarchy-dashboard.timer")
    assert len(probe_at) == 12, probe_at                       # 5 分ごと＝CloudWatch の 5 分の期間に 1 点ずつ（アラームの前提）
    for a in apply_at:
        gap = min((p - a) % 60 for p in probe_at)              # 適用の始まりから最初の確認まで
        assert gap >= 4, (a, gap)
    assert not set(probe_at) & set(dash_at), (probe_at, dash_at)   # 毎時のダッシュボードの生成と重ねない


def test_health_timer_on_clock_minutes():
    """生き死にの送信は時計の分ごと＝CloudWatch の 1 分の期間に 1 点ずつ（2026-10-13 staging＝「前の実行から 60 秒」の刻みは
    実行の所要と起動の幅のぶん延び、点は 1 日に約 1,280＝点の無い分があった。アラームは鳴らないが、「0 になった分」を数える
    集計が欠けと 0 を分けなければならない）。稼働中の timer に刻みの変更を効かせるため、bootstrap は restart する。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    timer = (root / "deploy/systemd/polyarchy-health.timer").read_text(encoding="utf-8")
    lines = [ln for ln in timer.splitlines() if ln and not ln.startswith("#")]
    assert "OnCalendar=*:*:00" in lines, lines
    assert not [ln for ln in lines if re.match(r"On(UnitActive|UnitInactive|Boot|Startup|Active)Sec=", ln)], lines
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    assert boot.index("systemctl enable --now polyarchy-health.timer") < boot.index("systemctl restart polyarchy-health.timer"), "bootstrap"
    # 送信のスクリプトは時刻を付けずに送る＝点は送った分に入る（1 分に 1 回の実行が 1 分に 1 点になる前提）
    assert "--timestamp" not in (root / "deploy/bootstrap/health_metric.sh").read_text(encoding="utf-8")


def test_tools_run_off_event_loop_and_say_busy():
    """ツールの実行をイベントループの外へ出す（2026-10-14・B45 段 1）。ツールは同期の関数で、SDK はイベントループの上で直に呼ぶ＝
    重い検索の間は、ほかの利用者の接続の手続きも `/healthz` も待った（運営者 1 人の検索の重なりで health が 2 分続けて 0）。
    待受の 1 か所で、登録済みのツールを別のスレッドで動かす形に包む。固定すること＝① ツールの定義は変わらない（掲載中＝
    コネクタの作り直しをしない）② 実行中もイベントループが回る ③ ツールは 1 本ずつのまま（検索の部品は直列の前提）
    ④ 要求ごとの値（利用者・経路）がスレッドの中でも読める＝捕捉ログが欠けない ⑤ 並びすぎ・待ちすぎは「混んでいる」と返し、
    その形は「該当なし」（found=false・results 空）と違う＝エラーの結果で、実行していないと書く ⑥ 空けば元どおり通る
    ⑦ 切れば元の動き（イベントループの上で直に）。"""
    import asyncio
    import threading
    import time
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from polyarchy_common import httplog, mcp_http

    def build():
        mcp = MCPServer("t")
        state = {"now": 0, "max": 0, "threads": set(), "client": None}

        @mcp.tool(title="遅い検索")
        def slow(seconds: float = 0.3, query: str = "") -> dict:
            """遅い検索の代わり。"""
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            state["threads"].add(threading.get_ident())
            state["client"] = httplog.current_client()
            time.sleep(seconds)
            state["now"] -= 1
            return {"found": False, "results": [], "query": query}

        return mcp, state

    async def defs(mcp):
        return [t.model_dump() for t in await mcp.list_tools()]

    async def run():
        mcp, state = build()
        before = await defs(mcp)
        wrapped = mcp_http.offload_tools(mcp, queue_max=2, wait_seconds=5.0)
        assert wrapped == ["slow"] and await defs(mcp) == before                    # ① 定義（名前・説明・引数・注釈）は同じ
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        tk = asyncio.create_task(ticker())
        tok = httplog._current_client.set("chatgpt")
        t0 = time.monotonic()
        await asyncio.gather(mcp.call_tool("slow", {"seconds": 0.3}), mcp.call_tool("slow", {"seconds": 0.3}))
        dt = time.monotonic() - t0
        httplog._current_client.reset(tok)
        assert ticks >= 20, ticks                                                   # ② 0.6 秒の実行の間もループが回った
        assert state["max"] == 1 and dt >= 0.55, (state, dt)                        # ③ 1 本ずつ
        assert threading.get_ident() not in state["threads"] and state["client"] == "chatgpt", state   # ④ 別スレッド・値は届く
        # ⑤ 並びすぎ＝実行中 1 本＋待ち 2 本まで。4 本目は「混んでいる」（待たずにすぐ返る）
        t0 = time.monotonic()
        rs = await asyncio.gather(*[mcp.call_tool("slow", {"seconds": 0.2}) for _ in range(4)], return_exceptions=True)
        busy = [r for r in rs if isinstance(r, Exception)]
        assert len(busy) == 1 and isinstance(busy[0], ToolError) and mcp_http.BUSY_MARK in str(busy[0]), rs
        assert "実行していません" in str(busy[0]) and "該当なし" in str(busy[0]), str(busy[0])
        assert "found" not in str(busy[0]) and "results" not in str(busy[0]), str(busy[0])   # 「無い」の形と混ざらない
        assert state["max"] == 1 and 0.55 <= time.monotonic() - t0 < 2.0
        ok = await mcp.call_tool("slow", {"seconds": 0.01, "query": "q"})           # ⑥ 空けば通る
        assert "\"query\": \"q\"" in str(ok) or "'query': 'q'" in str(ok), ok
        tk.cancel()
        # ⑤' 待ちすぎ＝並んでいる数が上限の内側でも、決めた秒数を超えて待った呼び出しは「混んでいる」
        mcp2, st2 = build()
        mcp_http.offload_tools(mcp2, queue_max=10, wait_seconds=0.15)
        rs = await asyncio.gather(mcp2.call_tool("slow", {"seconds": 0.5}), mcp2.call_tool("slow", {"seconds": 0.01}),
                                  return_exceptions=True)
        assert not isinstance(rs[0], Exception) and isinstance(rs[1], ToolError) and mcp_http.BUSY_MARK in str(rs[1]), rs
        assert st2["max"] == 1
        # ⑦ 切る＝包まない（イベントループの上で直に＝元の動き）
        mcp3, st3 = build()
        assert mcp_http.offload_tools(mcp3, enabled=False) == []
        await mcp3.call_tool("slow", {"seconds": 0.01})
        assert st3["threads"] == {threading.get_ident()}, st3

    asyncio.run(run())
    # 待受の関数が env を読んで装着する（切る口＝MCP_TOOL_OFFLOAD=0）。既定は装着・上限は env で変えられる
    import inspect
    src = inspect.getsource(mcp_http.serve_streamable_http) + inspect.getsource(mcp_http.build_streamable_http_app)
    assert "offload_tools(" in src and all(k in src for k in ("MCP_TOOL_OFFLOAD", "MCP_TOOL_QUEUE_MAX", "MCP_TOOL_WAIT_SECONDS")), "serve"


def test_tool_load_lines_and_busy_filter():
    """混み具合を数字で読めるようにする（2026-10-14・B45 段 2）。別のスレッドで動かしたツールの呼び出しは、1 回ごとに
    「待った時間・実行の時間・着いたときに並んでいた数」を 1 行で出す（利用者・検索語は出さない）＝アクセスログの所要時間では
    待ちと実行が分かれず、ツールの名前も無い。「混んでいる」を返した行は目印で数えてアラームにする＝箱が足りていない合図。
    固定すること＝行の形・RUNBOOK の集計の式がその行を読めること・フィルタの目印が断った行だけに当たること・フィルタとアラームの対。"""
    import asyncio
    import logging
    import re
    import time
    from pathlib import Path
    from mcp.server.mcpserver import MCPServer
    from polyarchy_common import mcp_http
    root = Path(__file__).resolve().parents[2]
    records: list[tuple[str, str]] = []

    class H(logging.Handler):
        def emit(self, r):
            records.append((r.levelname, r.getMessage()))

    mcp = MCPServer("t")

    @mcp.tool()
    def slow(seconds: float = 0.2, fail: bool = False) -> dict:
        """遅い検索の代わり。"""
        time.sleep(seconds)
        if fail:
            raise RuntimeError("boom")
        return {"found": False}

    lg = logging.getLogger("polyarchy.test_tool_load")
    h = H()
    lg.addHandler(h)
    lg.setLevel(logging.INFO)

    async def run():
        mcp_http.offload_tools(mcp, queue_max=1, wait_seconds=5.0, logger_name=lg.name)
        return await asyncio.gather(mcp.call_tool("slow", {"seconds": 0.2}), mcp.call_tool("slow", {"seconds": 0.05, "fail": True}),
                                    mcp.call_tool("slow", {"seconds": 0.05}), return_exceptions=True)
    try:
        asyncio.run(run())
    finally:
        lg.removeHandler(h)
    line = re.compile(r"^TOOL (\S+) wait_ms (\d+) run_ms (\d+) queue (\d+)$")
    tool = [line.match(m).groups() for lv, m in records if lv == "INFO" and line.match(m)]
    busy = [m for lv, m in records if lv == "WARNING"]
    assert len(tool) == 2 and len(busy) == 1, records                 # 実行した 2 本（失敗した呼び出しも 1 行）・断った 1 本
    first, second = sorted(tool, key=lambda t: int(t[1]))
    assert first[0] == "slow" and int(first[1]) < 100 and int(first[2]) >= 190 and first[3] == "0", first     # すぐ始まった 1 本
    assert int(second[1]) >= 150 and int(second[2]) >= 40 and second[3] == "0", second   # 前の 1 本を待った＝待ちと実行が分かれて出る
    assert not any(k in m for _, m in records for k in ("user=", "query", "seconds", "boom")), records   # 引数・利用者・例外の文言は出さない
    # RUNBOOK の集計の式（Logs Insights の parse）が、この行を読める
    rb = (root / "deploy/RUNBOOK_OPS.md").read_text(encoding="utf-8")
    m = re.search(r"parse @message /(TOOL [^\n]+?)/\n", rb)
    assert m, "RUNBOOK に TOOL の行の集計の式が無い"
    insights = re.compile(m.group(1).replace("(?<", "(?P<"))
    got = insights.search("Oct 14 01:02:03 ip-10-0-0-1 polyarchy-mcp[123]: 01:02:03 [INFO] TOOL %s wait_ms %s run_ms %s queue %s" % second)
    assert got and got.groupdict() == {"tool": "slow", "wait": second[1], "run": second[2], "queue": second[3]}, got
    # フィルタの目印は、断った行だけに当たる（実行した行・アクセスログの行には当たらない）。フィルタとアラームは対
    tf = (root / "deploy/terraform/observability.tf").read_text(encoding="utf-8")
    f = re.search(r'resource "aws_cloudwatch_log_metric_filter" "tool_busy" \{.*?pattern\s*=\s*"\\"([^"\\]+)\\"".*?name\s*=\s*"(tool_busy_\$\{each\.key\})"', tf, re.S)
    assert f and f.group(1).isascii(), "tool_busy のフィルタが無い"
    assert f.group(1) in busy[0] and not any(f.group(1) in m for lv, m in records if lv == "INFO"), (f.group(1), records)
    a = re.search(r'resource "aws_cloudwatch_metric_alarm" "tool_busy" \{(.*?)\n\}', tf, re.S)
    assert a and 'metric_name         = "tool_busy_${each.key}"' in a.group(1) and "-busy-${each.key}" in a.group(1), "tool_busy のアラーム"
    assert "notBreaching" in a.group(1) and "「busy-" in rb, "読み方（RUNBOOK）"


def test_reranker_scores_in_small_batches():
    """リランカーは候補を小分けにして計算する（2026-10-14）。1 度に計算する組は、いちばん長い組の長さまで引き伸ばされる＝
    候補 30 組を 1 度に渡すと、短い組のぶんが無駄な計算になる（開発機の実測＝小分けで並べ替えが約 1.7 倍・点数の差は 1e-7・
    順位は同じ）。文書検索のサービスが共有する読み込みの 1 か所で決める。偽の CrossEncoder で、渡し方と点数の並びを固定する。"""
    import sys
    import types
    calls = []

    class FakeCrossEncoder:
        def __init__(self, name, **kw):
            self.init = (name, kw)

        def predict(self, pairs, **kw):
            calls.append((list(pairs), kw))
            return [float(len(p)) for _, p in pairs]

    saved = sys.modules.get("sentence_transformers")
    sys.modules["sentence_transformers"] = types.SimpleNamespace(CrossEncoder=FakeCrossEncoder)
    try:
        from polyarchy_retrieval import models
        rerank = models.cross_encoder("org/model", max_length=512)()
        passages = ["あ" * n for n in (50, 400, 3, 120, 7)]
        scores = rerank("問", passages)
    finally:
        if saved is None:
            sys.modules.pop("sentence_transformers", None)
        else:
            sys.modules["sentence_transformers"] = saved
    assert len(calls) == 1 and calls[0][0] == [("問", p) for p in passages], calls          # 1 回の呼び出し・組は渡した順
    assert calls[0][1].get("batch_size") == models.RERANK_BATCH_SIZE and 1 <= models.RERANK_BATCH_SIZE <= 8, calls[0][1]
    assert scores == [50.0, 400.0, 3.0, 120.0, 7.0] and all(type(x) is float for x in scores), scores    # 点数は渡した順に対応


def test_tools_run_concurrently_up_to_the_limit():
    """検索そのものの同時実行（2026-10-14・B45 段 3）。待受は、同時に動かしても安全だと宣言したサービス（文書検索の 2 つ）だけ、
    ツールを N 本まで同時に別のスレッドで動かす（N＝unit の MCP_TOOL_CONCURRENCY）。1 本ずつだと、重い団体横断の後ろで軽い検索も
    待った（staging の 3 人の形で、1 本の待ちが最大 22 秒）。固定すること＝① 同時に動くのは N 本まで ② 並べる上限・「混んでいる」
    の形は段 1 と同じ（N 本の実行中のほかに並んだ数で数える）③ 要求ごとの値（利用者・経路）は、同時に動く呼び出しのそれぞれに
    自分の値が届く＝捕捉ログが取り違えない ④ 宣言の無いサービスは env が 2 以上でも 1 本ずつ（統計DB・企業情報DB は未検証）
    ⑤ 配線＝宣言は政策主張DB と審議会DB だけ・unit の N も両者だけ。"""
    import asyncio
    import logging
    import re
    import threading
    import time
    from pathlib import Path
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from polyarchy_common import httplog, mcp_http
    root = Path(__file__).resolve().parents[2]

    mcp = MCPServer("t")
    state = {"now": 0, "max": 0, "seen": [], "lock": threading.Lock()}

    @mcp.tool()
    def slow(seconds: float = 0.3, tag: str = "") -> dict:
        """遅い検索の代わり。"""
        with state["lock"]:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(seconds)
        with state["lock"]:
            state["now"] -= 1
            state["seen"].append((tag, httplog.current_client()))
        return {"found": False, "tag": tag}

    async def call(tag: str, seconds: float = 0.3):
        httplog._current_client.set(tag)          # タスクごとの文脈＝要求ごとの値の代わり
        return await mcp.call_tool("slow", {"seconds": seconds, "tag": tag})

    async def run():
        assert mcp_http.offload_tools(mcp, concurrency=2, queue_max=1, wait_seconds=5.0) == ["slow"]
        t0 = time.monotonic()
        rs = await asyncio.gather(*[asyncio.create_task(call(f"c{i}")) for i in range(4)], return_exceptions=True)
        dt = time.monotonic() - t0
        busy = [r for r in rs if isinstance(r, Exception)]
        assert state["max"] == 2, state                                              # ① 2 本まで同時
        assert len(busy) == 1 and isinstance(busy[0], ToolError) and mcp_http.BUSY_MARK in str(busy[0]), rs   # ② 実行 2＋待ち 1 の後は「混んでいる」
        assert 0.55 <= dt < 1.2, dt                                                  # 2 本ずつ 2 巡（1 本ずつなら 0.9 秒）
        assert len(state["seen"]) == 3 and all(tag == cl for tag, cl in state["seen"]), state["seen"]   # ③ 自分の値が届く
    asyncio.run(run())

    # ④ 同時に動かす数は env から。宣言の無いサービスは 1 本ずつに抑える（警告を出す）
    lg = logging.getLogger("polyarchy.test_concurrency")
    warned: list[str] = []

    class H(logging.Handler):
        def emit(self, r):
            warned.append(r.getMessage())
    h = H()
    lg.addHandler(h)
    import os
    saved = os.environ.get("MCP_TOOL_CONCURRENCY")
    try:
        os.environ.pop("MCP_TOOL_CONCURRENCY", None)
        assert mcp_http.tool_concurrency(True, lg) == 1                              # 既定＝1 本ずつ（いまの動き）
        os.environ["MCP_TOOL_CONCURRENCY"] = "3"
        assert mcp_http.tool_concurrency(True, lg) == 3 and not warned
        assert mcp_http.tool_concurrency(False, lg) == 1 and warned, warned          # 宣言なし＝1
        for bad in ("x", "0", "-2"):
            os.environ["MCP_TOOL_CONCURRENCY"] = bad
            assert mcp_http.tool_concurrency(True, lg) == 1, bad
    finally:
        lg.removeHandler(h)
        if saved is None:
            os.environ.pop("MCP_TOOL_CONCURRENCY", None)
        else:
            os.environ["MCP_TOOL_CONCURRENCY"] = saved
    import inspect
    src = inspect.getsource(mcp_http.serve_streamable_http) + inspect.getsource(mcp_http.build_streamable_http_app)
    assert "parallel_safe" in inspect.signature(mcp_http.serve_streamable_http).parameters
    assert "tool_concurrency(" in src and "concurrency=" in src, "serve が数を読んで渡す"
    assert inspect.signature(mcp_http.serve_streamable_http).parameters["parallel_safe"].default is False   # 既定は宣言なし

    # ⑤ 配線：宣言は文書検索の 2 サービスだけ（同時に流しても結果が同じことをゲート＝各 eval/concurrency で確かめたもの）
    serving = {"recommendations": True, "deliberations": True, "stats": False, "companies": False}
    for svc, safe in serving.items():
        s = (root / svc / "serving/mcp_server.py").read_text(encoding="utf-8")
        assert ("parallel_safe=True" in s) == safe, svc
        assert (root / svc / "eval/concurrency.py").exists() == safe, f"{svc} の同時実行のゲート"
    units = {"polyarchy-mcp": True, "polyarchy-deliberations": True, "polyarchy-stats": False, "polyarchy-companies": False}
    for u, on in units.items():
        t = (root / f"deploy/systemd/{u}.service").read_text(encoding="utf-8")
        m = re.search(r"^Environment=MCP_TOOL_CONCURRENCY=(\d+)$", t, re.M)
        assert bool(m) == on and (not m or int(m.group(1)) >= 2), u
    # 同時実行のゲートは release.sh が回し、README「品質の担保」に載っている
    rel = (root / "deploy/scripts/release.sh").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    for g in ("recommendations.eval.concurrency", "deliberations.eval.concurrency"):
        assert f"python -m {g}" in rel and f"python -m {g}" in readme, g


def test_tokenizer_tagger_per_thread():
    """分かち書き（fugashi の Tagger）はスレッドごとに持つ（2026-10-14・B45 段 3）。検索を同時に動かすと、1 つの Tagger を複数の
    スレッドから同時に呼ぶことになる（MeCab の解析器は同時に呼ばれる前提ではない）。偽の fugashi で、スレッドごとに 1 つ・同じ
    スレッドでは使い回すことを固定する。"""
    import sys
    import threading
    import types
    made: list[int] = []

    class FakeTagger:
        def __init__(self):
            made.append(threading.get_ident())

        def __call__(self, text):
            return [types.SimpleNamespace(surface=w) for w in text.split()]

    from polyarchy_retrieval import tokenizer as tk
    saved_mod, saved_local = sys.modules.get("fugashi"), getattr(tk, "_LOCAL", None)
    sys.modules["fugashi"] = types.SimpleNamespace(Tagger=FakeTagger)
    tk._LOCAL = threading.local()
    try:
        out: list[list[str]] = []
        lock = threading.Lock()
        together = threading.Barrier(3)              # 3 本を最後まで並ばせる（先に終わったスレッドの番号が使い回されない）

        def work():
            for _ in range(3):
                r = tk.tokenize_ja("経団連 は 提言 した")
                with lock:
                    out.append(r)
            together.wait()
        th = [threading.Thread(target=work) for _ in range(3)]
        [t.start() for t in th]
        [t.join() for t in th]
    finally:
        if saved_mod is None:
            sys.modules.pop("fugashi", None)
        else:
            sys.modules["fugashi"] = saved_mod
        tk._LOCAL = saved_local if saved_local is not None else threading.local()
    assert out == [["経団連", "は", "提言", "した"]] * 9, out
    assert len(made) == 3 and len(set(made)) == 3, made                               # スレッドごとに 1 つ・使い回す


def test_shell_var_not_followed_by_multibyte():
    """シェルの変数展開の直後に全角の文字を置かない＝${VAR} と波括弧で囲む（2026-10-02 staging＝macOS の /bin/bash 3.2 は
    UTF-8 のロケールで全角の先頭バイトを変数名の一部として読み、set -u の release.sh が「GATE_LOG?: unbound variable」で止まった）。"""
    import re
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    # 追跡済み＋まだ追跡していない（.gitignore で外していない）ファイル＝コミットの前に回したゲートでも新しいスクリプトを見る
    # （2026-10-09＝新規の release_preflight.sh が未追跡のままゲートを通り、コミットした後でこの検査に当たるところだった）
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.sh"],
                           cwd=root, capture_output=True, text=True, check=True).stdout.split()
    pat = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]")
    bad = [f"{f}:{i}" for f in files for i, ln in enumerate((root / f).read_text(encoding="utf-8").splitlines(), 1) if pat.search(ln)]
    assert files and not bad, bad


def test_box_referenced_files_exist():
    """箱の bootstrap・自動適用が設置・実行するリポジトリのファイル（deploy/ の下・systemd のユニット）が、リポジトリにある
    （2026-10-09＝新しいファイルを git に足し忘れると、tar＝git archive HEAD に入らず箱の bootstrap が set -e で止まり、
    自動適用が切り戻しになる。配布用のクローンで回すゲートなら、足し忘れはここで止まる）。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    missing = []
    for src in ("deploy/bootstrap/bootstrap.sh", "deploy/bootstrap/apply_data_update.sh"):
        text = (root / src).read_text(encoding="utf-8")
        for rel in re.findall(r'"\$REPO_DIR/(deploy/[^"$]+\.[a-z]+|deploy/bootstrap/[^"$/]+)"', text):
            if not (root / rel).exists():
                missing.append(f"{src}: {rel}")
        for unit in re.findall(r'"\$UNIT_SRC/([^"$]+)"', text):
            if not (root / "deploy/systemd" / unit).exists():
                missing.append(f"{src}: deploy/systemd/{unit}")
    assert not missing, missing


def test_env_backup_not_shipped():
    """env の控え（deploy/env/staging.env.bak-<日付>）・社内ノート等の追跡していないものを tar で箱へ運ばない・git に載せない
    （2026-10-04 staging＝旧方式〔作業フォルダを除外リストで固める〕の *.env にも .gitignore にも当たらず、次の配布で箱に
    入るところだった）。2026-10-09 から tar は git archive HEAD＝追跡ファイルだけ（除外リストの後追いをやめた）。"""
    import re
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    up = (root / "deploy/scripts/upload_to_s3.sh").read_text(encoding="utf-8")
    cmd = [ln for ln in re.sub(r"\\\n\s*", " ", up).splitlines() if re.search(r"^\s*git .* archive ", ln)]
    assert len(cmd) == 1 and "HEAD" in cmd[0] and "**/data/**" in cmd[0], cmd   # 追跡ファイルだけ・データは S3 経由
    assert not re.search(r"^\s*tar [^\n]*-c", up, re.M), "作業フォルダを tar で固める旧方式に戻っている"
    ign = subprocess.run(["git", "check-ignore", "--no-index", "deploy/env/staging.env.bak-20261004", "deploy/env/staging.env.example"],
                         cwd=root, capture_output=True, text=True).stdout.split()
    assert ign == ["deploy/env/staging.env.bak-20261004"], ign   # 控えは外れ、ひな型は追跡のまま


def test_top_user_counts():
    """直近 1 時間の最多の 1 人の呼び出しの数（B44 第 1 便・2026-10-15）。数える単位はツールの呼び出し＝捕捉ログの行（§E-B44 の
    判断 1）。全サービスを合わせて利用者ごとに数え、利用者の無い行（未認証・stdio）・窓の外の行・壊れた行は数えない。箱が送るのは
    最多の数だけ＝利用者のハッシュは出さない（運営者が手元で特定するときだけ --show）。捕捉ログは 30 日ぶん溜まる＝末尾から
    窓の外に出るまでだけ読む（ブロックの境目で行を取り違えない）。"""
    import io
    import json
    import tempfile
    from datetime import datetime, timedelta
    from pathlib import Path
    from polyarchy_common import top_user as tu
    from polyarchy_common.access import user_hash
    d = Path(tempfile.mkdtemp())
    now = datetime(2026, 10, 15, 12, 0, 0)

    def rec(minutes_ago: float, uh: str | None, **kw) -> str:
        r = {"ts": (now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds"), "query": "q" * 40, **kw}
        if uh:
            r["user_hash"] = uh
        return json.dumps(r, ensure_ascii=False)

    a, b, c = "a" * 16, "b" * 16, "c" * 16
    rows_r = [rec(60 * 24 * 3 - i, a) for i in range(3000)]                  # 3 日前の山（窓の外）＝ブロックを何度もまたぐ
    rows_r += [rec(61, b), rec(59, a), rec(30, a), rec(1, a), rec(0.5, None), "{壊れた行", rec(0, b)]
    rows_s = [rec(45, a), rec(10, c), rec(5, b)]
    (d / "r.jsonl").write_text("\n".join(rows_r) + "\n", encoding="utf-8")
    (d / "s.jsonl").write_text("\n".join(rows_s) + "\n", encoding="utf-8")
    paths = [d / "r.jsonl", d / "s.jsonl", d / "none.jsonl"]                  # 無いファイルは 0 件
    counts = tu.count_by_user(paths, now=now, block=257)                       # 小さいブロック＝境目の取り違えを確かめる
    assert counts == {a: 4, b: 2, c: 1}, counts                                # b の 61 分前は窓の外
    assert tu.count_by_user(paths, now=now, block=1 << 16) == counts
    assert tu.count_by_user([d / "none.jsonl"], now=now) == {}

    out = io.StringIO()
    assert tu.main(["--log", str(paths[0]), "--log", str(paths[1]), "--at", now.isoformat()], out=out) == 0
    assert out.getvalue() == "top_user 4\n", out.getvalue()                    # 箱が送る形＝数だけ・ハッシュは出ない
    out = io.StringIO()
    tu.main(["--log", str(d / "none.jsonl"), "--at", now.isoformat()], out=out)
    assert out.getvalue() == "top_user 0\n", out.getvalue()                    # 利用の無い時間帯は 0（アラームは鳴らない）
    out = io.StringIO()
    tu.main(["--log", str(paths[0]), "--log", str(paths[1]), "--at", now.isoformat(), "--show", "2"], out=out)
    assert out.getvalue().splitlines() == [f"{a} 4", f"{b} 2"], out.getvalue()   # 運営者が手元で特定するとき
    # 捕捉ログの ts は箱の時計＝UTC。手元（JST の PC）の「いま」を既定にすると窓が 9 時間ずれて黙って 0 件になる（2026-10-16 の
    # 運用側の照合）＝--show は --at を求める。--at に時差を書けば UTC に直して比べる（JST のまま書ける）。
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            tu.main(["--log", str(paths[0]), "--show", "2"], out=io.StringIO())
            raise AssertionError("--show に --at が無いのに通った")
        except SystemExit as e:
            assert e.code == 2, e.code
    out = io.StringIO()
    tu.main(["--log", str(paths[0]), "--log", str(paths[1]), "--at", "2026-10-15T21:00:00+09:00", "--show", "2"], out=out)
    assert out.getvalue().splitlines() == [f"{a} 4", f"{b} 2"], out.getvalue()   # JST の 21 時＝UTC の 12 時
    out = io.StringIO()
    tu.main(["--hash-of", " Someone@Example.COM "], out=out)
    assert out.getvalue().strip() == user_hash("someone@example.com"), out.getvalue()   # 捕捉ログと同じハッシュ


def test_top_user_script_sends_metric():
    """top_user_metric.sh＝最多の 1 人の数をメトリクス top_user_calls_1h にして送る（偽の python・aws で実行）。数えるのが
    落ちたら送らずに非 0（→ OnFailure → unit-failed＝数えるのが止まったまま「静か」に見えない）。数でない出力も送らない。"""
    import os
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    d = Path(tempfile.mkdtemp())
    (d / "bin").mkdir()
    (d / "deploy.env").write_text(f"AWS_REGION=ap-northeast-1\nREPO_DIR={d}\n")
    (d / "bin" / "aws").write_text(f'#!/bin/bash\necho "$*" >> "{d}/aws.log"\n')
    (d / "bin" / "fakepy").write_text(f'#!/bin/bash\necho "$*" > "{d}/py.args"\ncat "{d}/lines"; exit $(cat "{d}/rc")\n')
    for f in (d / "bin").iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}", "POLYARCHY_DEPLOY_ENV": str(d / "deploy.env"),
           "POLYARCHY_TOPUSER_PY": str(d / "bin" / "fakepy")}

    def run(lines: str, rc: int = 0):
        (d / "lines").write_text(lines)
        (d / "rc").write_text(str(rc))
        (d / "aws.log").write_text("")
        r = subprocess.run(["bash", str(root / "deploy/bootstrap/top_user_metric.sh")], env=env, capture_output=True, text=True)
        return r.returncode, (d / "aws.log").read_text().splitlines()

    rc, sent = run("top_user 37\n")
    assert rc == 0 and len(sent) == 1, (rc, sent)
    assert "MetricName=top_user_calls_1h,Dimensions=[{Name=service,Value=box}],Value=37,Unit=Count" in sent[0], sent
    args = (d / "py.args").read_text()
    for svc in ("recommendations", "stats", "companies", "deliberations"):    # 捕捉ログを持つ 4 サービスを合わせて数える
        assert f"{svc}/data/query_log/queries.jsonl" in args, args
    assert run("top_user 0\n")[1] and "Value=0," in run("top_user 0\n")[1][0]
    rc, sent = run("", rc=1)
    assert rc != 0 and sent == [], (rc, sent)
    rc, sent = run("top_user abc\n")
    assert rc != 0 and sent == [], (rc, sent)


def test_top_user_wiring():
    """第 1 便の配線（2026-10-15）＝5 分ごとの timer（毎時のダッシュボードの生成と重ねない）・bootstrap が置いて有効にする・
    アラーム top-user の閾値は terraform の変数（env の TOP_USER_CALLS_THRESHOLD から渡せる）・RUNBOOK に読み方と
    「特定の利用者を止める」（止めるのは IdP の画面だけ＝§E-B44 の決定）・運用設計の表に 1 行。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    read = lambda p: (root / p).read_text(encoding="utf-8")   # noqa: E731
    timer = read("deploy/systemd/polyarchy-topuser.timer")
    cal = re.search(r"^OnCalendar=\*:(\d+)/5$", timer, re.M)
    assert cal, timer
    dash = re.search(r"^OnCalendar=\S+ \*:(\d+)", read("deploy/systemd/polyarchy-dashboard.timer"), re.M).group(1)
    assert dash == "05", dash
    assert int(dash) % 5 != int(cal.group(1)), (dash, cal.group(1))
    unit = read("deploy/systemd/polyarchy-topuser.service")
    assert "ExecStart=/usr/local/bin/polyarchy-top-user" in unit and "User=polyarchy" in unit, unit
    boot = read("deploy/bootstrap/bootstrap.sh")
    assert 'deploy/bootstrap/top_user_metric.sh" /usr/local/bin/polyarchy-top-user' in boot
    assert '"$UNIT_SRC/polyarchy-topuser.service"' in boot and '"$UNIT_SRC/polyarchy-topuser.timer"' in boot
    assert "systemctl enable --now polyarchy-topuser.timer" in boot
    tf = read("deploy/terraform/observability.tf")
    alarm = re.search(r'resource "aws_cloudwatch_metric_alarm" "top_user" \{(.*?)\n\}', tf, re.S).group(1)
    assert 'metric_name         = "top_user_calls_1h"' in alarm and "var.top_user_calls_threshold" in alarm, alarm
    assert "GreaterThanOrEqualToThreshold" in alarm and 'treat_missing_data  = "notBreaching"' in alarm, alarm
    var = read("deploy/terraform/variables.tf").split('variable "top_user_calls_threshold"')[1].split("\n}")[0]
    assert re.search(r"default\s*=\s*\d+", var), var
    assert 'TF_ARGS+=(-var "top_user_calls_threshold=$TOP_USER_CALLS_THRESHOLD")' in read("deploy/scripts/lib.sh")
    for ex in ("staging", "prod"):
        assert re.search(r"^TOP_USER_CALLS_THRESHOLD=$", read(f"deploy/env/{ex}.env.example"), re.M), ex
    rb = read("deploy/RUNBOOK_OPS.md")
    sec = rb.split("### アラーム「top-user」")[1].split("\n### ")[0]
    assert "--show" in sec and "--hash-of" in sec and "IdP" in sec, "RUNBOOK"
    assert "--hash-of <" not in sec and "UTC" in sec, "RUNBOOK"   # <メール> を貼ると zsh が止まる・ts は UTC（2026-10-16）
    assert "### 特定の利用者を止める" in rb
    assert "polyarchy-topuser" in read("docs/運用設計.md")



def test_tool_offload_switch_from_ssm():
    """ツールの実行をイベントループの上に戻す（`MCP_TOOL_OFFLOAD=0`）のは、運営者の箱の SSM `tool_offload_<サービス>`＝`0` からだけ
    （2026-10-15）。unit に書くと、リポジトリを複製した導入団体の箱にもそのまま入る（複製は 1 回きり）＝運営者の一時の事情
    （企業情報DB の掲載の審査のあいだ）を、複製に運ばない。bootstrap の write_service_env を取り出して、偽の SSM で実行する。"""
    import re
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for unit in sorted((root / "deploy/systemd").glob("*.service")):
        assert not re.search(r"^Environment=MCP_TOOL_OFFLOAD=", unit.read_text(encoding="utf-8"), re.M), unit.name
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    fn = re.search(r"^write_service_env\(\) \{.*?^\}\n", boot, re.M | re.S).group(0)
    d = Path(tempfile.mkdtemp())

    def run(ssm: dict) -> str:
        f = d / "svc.env"
        f.unlink(missing_ok=True)
        stub = "ssm_get() { case \"$1\" in " + " ".join(f'{k}) printf %s "{v}";;' for k, v in ssm.items()) + " esac; }\n"
        script = (stub + "auth_env_lines() { :; }\nchown() { :; }\nSVC_USER=x\n" + fn +
                  f'write_service_env companies "{f}" "" "" "" ""\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return f.read_text() if f.exists() else ""

    assert run({"tool_offload_companies": "0"}) == "MCP_TOOL_OFFLOAD=0\n"
    assert run({}) == ""                                             # 既定＝別のスレッド（ファイルも作らない）
    assert run({"tool_offload_companies": "1"}) == ""                # 0 以外は書かない（既定のまま）
    assert run({"tool_offload_stats": "0"}) == ""                    # ほかのサービスの値は効かない
    rb = (root / "deploy/RUNBOOK_OPS.md").read_text(encoding="utf-8")
    assert "tool_offload_<サービス>" in rb and "unit に `MCP_TOOL_OFFLOAD=0`" not in rb, "RUNBOOK"



def test_privacy_url_from_ssm():
    """サーバの説明が案内するプライバシーページは、箱の SSM `privacy_url` から（2026-10-15）。リポジトリを複製した導入団体の箱が、
    上流のページを利用者に案内し続けないように。無ければ上流のページ（運営者の箱の説明は変わらない）。https で始まり空白を
    含まない値だけを書く（env の行が壊れない）。bootstrap の write_service_env を取り出して、偽の SSM で実行する。"""
    import os
    import re
    import subprocess
    import sys
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    fn = re.search(r"^write_service_env\(\) \{.*?^\}\n", boot, re.M | re.S).group(0)
    d = Path(tempfile.mkdtemp())

    def run(ssm: dict) -> str:
        f = d / "svc.env"
        f.unlink(missing_ok=True)
        stub = "ssm_get() { case \"$1\" in " + " ".join(f'{k}) printf %s "{v}";;' for k, v in ssm.items()) + " esac; }\n"
        script = stub + "auth_env_lines() { :; }\nchown() { :; }\nSVC_USER=x\n" + fn + f'write_service_env mcp "{f}" "" "" "" ""\n'
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return f.read_text() if f.exists() else ""

    assert run({"privacy_url": "https://example.org/privacy"}) == "MCP_PRIVACY_URL=https://example.org/privacy\n"
    assert run({}) == ""
    assert run({"privacy_url": "http://example.org/p"}) == ""          # https 以外は書かない
    assert run({"privacy_url": "https://a b"}) == ""                   # 空白を含む値は書かない

    def instructions(env_url):
        env = {k: v for k, v in os.environ.items() if k != "MCP_PRIVACY_URL"}
        if env_url:
            env["MCP_PRIVACY_URL"] = env_url
        r = subprocess.run([sys.executable, "-c", "from recommendations.serving.mcp_server import SERVER_INSTRUCTIONS as s; print(s)"],
                           cwd=root, env={**env, "PYTHONPATH": str(root)}, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-500:]
        return r.stdout
    assert "https://example.org/privacy" in instructions("https://example.org/privacy")
    assert "https://docs.polyarchy.net/privacy" in instructions(None)  # 既定＝上流（運営者の箱は変わらない）
    assert "docs.polyarchy.net" not in instructions("https://example.org/privacy")


def test_crawler_contact_from_env():
    """団体のサイトを取りに行くときの UA の連絡先は、コードに書かず環境ごとに渡す（2026-10-30）。複製した導入団体の箱・手元が、上流の
    運営者の連絡先を名乗らないように。固定すること＝① collect.py にメールアドレスの文字列が無い ② UA は env の CRAWLER_CONTACT から
    （空・形が想定外なら連絡先なし）③ bootstrap は SSM crawler_contact をメールアドレスか URL の形のときだけ crawler.env に書く
    （更新チェックが source する＝記号を通さない）④ 更新チェックが crawler.env を読み、deploy.sh secrets が env から SSM へ入れる。"""
    import os
    import re
    import shlex
    import subprocess
    import sys
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "recommendations/ingest/collect.py").read_text(encoding="utf-8")
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", src), "collect.py に連絡先の文字列がある"          # ①

    def ua(contact):                                                                                       # ②
        env = {**os.environ, "PYTHONPATH": str(root), "CRAWLER_CONTACT": contact}   # 空でも入れる＝手元の .env に上書きさせない
        r = subprocess.run([sys.executable, "-c", "from recommendations.ingest.collect import UA; print(UA['User-Agent'])"],
                           cwd=root, env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-500:]
        return r.stdout.strip()
    assert ua("ops@example.org") == "Mozilla/5.0 (policy-RAG research; contact: ops@example.org)"
    assert ua("https://example.org/contact") == "Mozilla/5.0 (policy-RAG research; contact: https://example.org/contact)"
    assert ua("") == "Mozilla/5.0 (policy-RAG research)"
    assert ua("a b") == ua("x;rm") == "Mozilla/5.0 (policy-RAG research)"

    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")                              # ③
    block = re.search(r'^CONTACT="\$\(ssm_get crawler_contact\)"\n.*?^fi\n', boot, re.M | re.S).group(0)
    d = Path(tempfile.mkdtemp())

    def run(value):
        f = d / "crawler.env"
        f.write_text("old\n")
        stub = f"ssm_get() {{ printf %s {shlex.quote(value)}; }}\n"
        script = stub + block.replace("/etc/polyarchy/crawler.env", str(f))
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return f.read_text() if f.exists() else None
    assert run("ops@example.org") == "CRAWLER_CONTACT=ops@example.org\n"
    assert run("") is None                                   # 未登録＝ファイルを消す（古い値を残さない）
    assert run("a$(id)") is None and run("x;y") is None      # 形が想定外＝書かない

    upd = (root / "deploy/bootstrap/update_check.sh").read_text(encoding="utf-8")                            # ④
    assert upd.index("source /etc/polyarchy/crawler.env") < upd.index("recommendations.ingest.check_new")
    lib = (root / "deploy/scripts/lib.sh").read_text(encoding="utf-8")
    assert re.search(r'--name "\$SSM_PREFIX/crawler_contact" --value "\$CRAWLER_CONTACT"', lib)
    for env in ("staging", "prod"):
        assert re.search(r"^CRAWLER_CONTACT=$", (root / f"deploy/env/{env}.env.example").read_text(encoding="utf-8"), re.M)


def test_secrets_without_tunnel_credentials_file():
    """`deploy.sh <env> secrets` は、トンネルの資格情報のファイルが手元に無くても、SSM に登録済みなら止まらない（2026-10-15）。
    資格情報は登録の後に作業用 PC から消す（ルート CLAUDE.md §5）＝消した後に認証の値などを登録し直せなかった。登録済みでも
    なければ止まる（初回はファイルが要る）。偽の aws で、スクリプトの写しを実行する。"""
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    d = Path(tempfile.mkdtemp())
    for sub in ("deploy/scripts", "deploy/env", "deploy/terraform", "bin"):
        (d / sub).mkdir(parents=True)
    for name in ("deploy.sh", "lib.sh"):
        shutil.copy(root / "deploy/scripts" / name, d / "deploy/scripts" / name)
    (d / "bin/aws").write_text(f'#!/bin/bash\necho "$*" >> "{d}/aws.log"\n'
                               f'case "$*" in "sts "*) echo 123456789012;; *get-parameter*cloudflared_credentials*) exit $(cat "{d}/has");; esac\n'
                               'exit 0\n')
    (d / "bin/aws").chmod(0o755)
    env = {**os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}"}
    (d / "deploy/env/t.env").write_text(f"ENVIRONMENT=t\nAWS_PROFILE=x\nAWS_REGION=ap-northeast-1\nTUNNEL_ID=u\n"
                                        f"CLOUDFLARED_CRED_FILE={d}/none.json\nAUTH_ISSUER=https://idp.example\n")

    def run(has: int):
        (d / "has").write_text(str(has))
        (d / "aws.log").write_text("")
        r = subprocess.run(["bash", str(d / "deploy/scripts/deploy.sh"), "t", "secrets"], env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (d / "aws.log").read_text(), r.stderr

    rc, log, err = run(0)                      # 登録済み＝資格情報は触らずに先へ（認証の値は登録する）
    assert rc == 0, err[-400:]
    assert "--name /polyarchy/t/cloudflared_credentials --value" not in log and "auth_issuer" in log, log
    rc, log, err = run(1)                      # 未登録＝ファイルが要る
    assert rc != 0 and "auth_issuer" not in log, (rc, log)


def _asgi_call(app, *, port, client="10.0.1.5", headers=(), path="/mcp", method="POST", server_host="0.0.0.0"):
    """ASGI アプリを 1 回呼んで (status, body) を返す（中継の口の検査用）。"""
    import asyncio
    out = {"status": 0, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    scope = {"type": "http", "method": method, "path": path, "headers": [(k.lower(), v) for k, v in headers],
             "server": (server_host, port), "client": (client, 50000), "query_string": b""}
    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"]


def test_relay_entrance():
    """中継の口（2026-10-16・B44 第 2 便 ①）。千人規模の問を 1 つの bot が中継する導入団体のために、同じネットワークの中の bot
    だけが使う、ログインの無い 2 つ目の口を、同じプロセス・同じアプリに開く（同時に動かす数と「混んでいる」は公開の口と共有）。
    ルート CLAUDE.md §3「利用者はログイン必須」の唯一の例外＝固定すること：① env が無ければ開かない（運営者の箱は閉じたまま）・
    許可の範囲が無い／公開の範囲・広すぎる・ポートが公開の口と同じなら開かない ② 中継の口は、送り元が許可の範囲の外なら 403・
    会員の印（16 桁の 16 進）が無い／形が違えば 400＝中身に渡さない ③ 印があれば Bearer なしで通り、要求の中の利用者は印・
    経路は relay（捕捉ログ・アクセスログ・top-user がそのまま会員ごとに数える）④ 公開の口は印を付けてきてもログインを求める
    （公開の口から会員を名乗れない）⑤ 断った行に目印 relay_reject= ⑥ 待受は 2 つのソケットを 1 つのサーバで。"""
    import inspect
    import logging
    import os
    from polyarchy_common import access, httplog, mcp_http, relay

    log = logging.getLogger("t.relay")
    ok_env = {"MCP_RELAY_PORT": "8865", "MCP_RELAY_ALLOW_CIDRS": "10.0.1.0/24,172.31.0.0/16"}
    assert relay.relay_config({}, public_port=8765, log=log) is None                                  # ① 既定は閉
    assert relay.relay_config({"MCP_RELAY_PORT": "8865"}, public_port=8765, log=log) is None          # 範囲が無い
    for bad in ("0.0.0.0/0", "8.8.8.0/24", "10.0.0.0/7", "10.0.1.0/24,203.0.113.0/24", "x", "10.0.1.0/24;rm"):
        assert relay.relay_config({**ok_env, "MCP_RELAY_ALLOW_CIDRS": bad}, public_port=8765, log=log) is None, bad
    assert relay.relay_config({**ok_env, "MCP_RELAY_PORT": "8765"}, public_port=8765, log=log) is None   # 公開の口と同じ
    assert relay.relay_config({**ok_env, "MCP_RELAY_PORT": "x"}, public_port=8765, log=log) is None
    cfg = relay.relay_config(ok_env, public_port=8765, log=log)
    assert cfg["port"] == 8865 and cfg["host"] == "0.0.0.0" and len(cfg["allow"]) == 2, cfg

    seen = []

    async def inner(scope, receive, send):                             # MCP アプリの代わり＝要求の中の利用者と経路を記録
        seen.append((access.user_hash(), httplog.current_client()))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    auth_env = {"MCP_AUTH_ISSUER": "https://idp.example", "MCP_AUTH_AUD": "aud", "MCP_AUTH_RESOURCE_URL": "https://h.example/mcp"}
    saved = {k: os.environ.get(k) for k in auth_env}
    os.environ.update(auth_env)
    try:
        app = mcp_http.build_asgi_app(inner, host="127.0.0.1", port=8765, path="/mcp", logger_name="t.relay", relay=cfg)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    member = "0123456789abcdef"
    hdr = ((b"x-polyarchy-member", member.encode()),)
    # ② 送り元・印
    assert _asgi_call(app, port=8865, client="203.0.113.9", headers=hdr)[0] == 403
    assert _asgi_call(app, port=8865, client="not-an-ip", headers=hdr)[0] == 403
    assert _asgi_call(app, port=8865)[0] == 400
    for bad in (b"0123456789ABCDEF", b"0123456789abcde", b"0123456789abcdef0", b"g123456789abcdef", b""):
        assert _asgi_call(app, port=8865, headers=((b"x-polyarchy-member", bad),))[0] == 400, bad
    assert seen == []                                                  # 断ったものは中身に届かない
    # ③ 通る＝Bearer なし・利用者は印・経路は relay
    st, body = _asgi_call(app, port=8865, client="10.0.1.5", headers=hdr)
    assert st == 200 and body == b"ok" and seen == [(member, "relay")], (st, seen)
    assert access.user_hash() is None and httplog.current_client() is None   # 要求の外へ漏れない
    # ④ 公開の口は印を付けてきてもログインを求める
    seen.clear()
    st, _ = _asgi_call(app, port=8765, client="127.0.0.1", headers=hdr, server_host="127.0.0.1")
    assert st == 401 and seen == [], st
    # healthz は両方の口で認証なし（最外）・中継の口で印は要らない
    app_h = mcp_http.build_asgi_app(inner, host="127.0.0.1", port=8765, path="/mcp", logger_name="t.relay", relay=cfg,
                                    health_check=lambda: {"ok": True})
    assert _asgi_call(app_h, port=8865, path="/healthz", method="GET")[0] == 200
    # 中継の口が無いときは、どのポートでも公開の口と同じ（印は信じない）
    seen.clear()
    plain = mcp_http.build_asgi_app(inner, host="127.0.0.1", port=8765, path="/mcp", logger_name="t.relay")
    _asgi_call(plain, port=8865, headers=hdr)
    assert seen == [(None, "other")], seen
    # ⑤ 断った行の目印
    src = inspect.getsource(relay)
    assert "relay_reject=source" in src and "relay_reject=member" in src
    # ⑥ 待受＝env を読んで、開くときは 2 つのソケットを 1 つのサーバで
    serve = inspect.getsource(mcp_http.serve_streamable_http) + inspect.getsource(mcp_http.build_streamable_http_app)
    assert "relay_config(" in serve and "build_asgi_app(" in serve and "sockets=" in serve, "serve"


def test_relay_wiring():
    """中継の口の配線（2026-10-16・B44 第 2 便 ①）。値の正は env ファイルの RELAY_SOURCE_CIDRS の 1 つ＝① terraform の -var
    （箱の網の入口の規則）と ② `deploy.sh secrets` が SSM relay_allow_cidrs（空にしたら off）→ ③ bootstrap がサービスの env に
    MCP_RELAY_PORT（公開の口＋100）と MCP_RELAY_ALLOW_CIDRS を書く（off・無い・形が違う＝書かない）。④ 中継のポートはトンネルの
    規則に載せない ⑤ terraform は範囲が空なら規則を作らず、公開の範囲は plan で止める ⑥ トンネルを張らない箱（公開ホストが 1 つも
    無く TUNNEL_ID も空＝中継の口だけで出す構成・PROD_MIGRATION §2.6）では `secrets` がトンネルの登録を警告つきで飛ばして
    relay_allow_cidrs の登録に進む。公開ホストがあるのに TUNNEL_ID が無いのは従来どおり止まる（2026-10-17）。"""
    import json
    import os
    import re
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    fn = re.search(r"^write_service_env\(\) \{.*?^\}\n", boot, re.M | re.S).group(0)
    pf = re.search(r"^relay_port\(\) \{.*?^\}\n", boot, re.M | re.S)
    assert pf, "bootstrap に relay_port が無い"
    d = Path(tempfile.mkdtemp())

    def run(svc: str, ssm: dict) -> str:
        f = d / "svc.env"
        f.unlink(missing_ok=True)
        stub = "ssm_get() { case \"$1\" in " + " ".join(f'{k}) printf %s "{v}";;' for k, v in ssm.items()) + " esac; }\n"
        script = stub + "auth_env_lines() { :; }\nchown() { :; }\nSVC_USER=x\n" + pf.group(0) + fn + f'write_service_env {svc} "{f}" "" "" "" ""\n'
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return f.read_text() if f.exists() else ""

    # ③ ポートは各サービスの公開の口（unit の --port）＋100
    for svc, unit in (("mcp", "polyarchy-mcp"), ("stats", "polyarchy-stats"), ("companies", "polyarchy-companies"),
                      ("deliberations", "polyarchy-deliberations")):
        pub = int(re.search(r"--port (\d+)", (root / f"deploy/systemd/{unit}.service").read_text()).group(1))
        got = run(svc, {"relay_allow_cidrs": "10.0.1.0/24"})
        assert got == f"MCP_RELAY_PORT={pub + 100}\nMCP_RELAY_ALLOW_CIDRS=10.0.1.0/24\n", (svc, got)
    assert run("mcp", {}) == "" and run("mcp", {"relay_allow_cidrs": "off"}) == ""
    assert run("mcp", {"relay_allow_cidrs": "10.0.1.0/24 x"}) == "" and run("mcp", {"relay_allow_cidrs": "10.0.0.0/8;id"}) == ""
    # ④ トンネルの規則（⑦ の ingress）に中継のポートが無い
    seventh = boot[boot.index("# ── ⑦ cloudflared"):boot.index("# ── ⑧")]
    assert not re.search(r"localhost:88\d\d", seventh), "中継のポートがトンネルの規則にある"
    # ⑤ terraform
    tf = (root / "deploy/terraform/network.tf").read_text(encoding="utf-8")
    rule = re.search(r'resource "aws_vpc_security_group_ingress_rule" "relay" \{.*?\n\}', tf, re.S)
    assert rule and "for_each" in rule.group(0) and "var.relay_source_cidrs" in rule.group(0), "network.tf"
    assert re.search(r"from_port\s*=\s*8865\b", rule.group(0)) and re.search(r"to_port\s*=\s*8868\b", rule.group(0)), rule.group(0)
    var = (root / "deploy/terraform/variables.tf").read_text(encoding="utf-8")
    vb = re.search(r'variable "relay_source_cidrs" \{.*?\n\}', var, re.S)
    assert vb and re.search(r"default\s*=\s*\[\]", vb.group(0)) and "validation" in vb.group(0), "variables.tf"
    outs = (root / "deploy/terraform/outputs.tf").read_text(encoding="utf-8")
    assert all(f'output "{o}"' in outs for o in ("relay_vpc_id", "relay_subnet_id", "relay_private_ip")), "outputs.tf"
    # ① ② lib.sh＝偽の aws と terraform で、deploy.sh の写しを流す
    for sub in ("deploy/scripts", "deploy/env", "deploy/terraform", "bin"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    for name in ("deploy.sh", "lib.sh"):
        shutil.copy(root / "deploy/scripts" / name, d / "deploy/scripts" / name)
    (d / "bin/aws").write_text(f'#!/bin/bash\necho "$*" >> "{d}/aws.log"\n'
                               f'case "$*" in "sts "*) echo 123456789012;; *get-parameter*relay_allow_cidrs*) exit $(cat "{d}/has");; esac\n'
                               'exit 0\n')
    (d / "bin/terraform").write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" >> "{d}/tf.log"\nexit 0\n')
    for b in ("aws", "terraform"):
        (d / "bin" / b).chmod(0o755)
    env = {**os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}"}
    (d / "cred.json").write_text("{}")
    base = (f"ENVIRONMENT=t\nAWS_PROFILE=x\nAWS_REGION=ap-northeast-1\nTUNNEL_ID=u\nCLOUDFLARED_CRED_FILE={d}/cred.json\n")

    def deploy(phase: str, extra: str, has: int = 1, base_: str = base):
        (d / "deploy/env/t.env").write_text(base_ + extra)
        (d / "has").write_text(str(has))
        for lg in ("aws.log", "tf.log"):
            (d / lg).write_text("")
        r = subprocess.run(["bash", str(d / "deploy/scripts/deploy.sh"), "t", phase], env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (d / "aws.log").read_text(), (d / "tf.log").read_text(), r.stderr

    rc, aws, _, err = deploy("secrets", "RELAY_SOURCE_CIDRS=10.0.1.0/24,10.0.2.0/24\n")
    assert rc == 0, err[-400:]
    assert "--name /polyarchy/t/relay_allow_cidrs --value 10.0.1.0/24,10.0.2.0/24" in aws, aws
    rc, aws, _, err = deploy("secrets", "", has=0)                    # 空にした＋登録がある＝off に
    assert rc == 0 and "--name /polyarchy/t/relay_allow_cidrs --value off" in aws, aws
    rc, aws, _, err = deploy("secrets", "", has=1)                    # 空のまま・登録も無い＝作らない
    assert rc == 0 and "relay_allow_cidrs --value" not in aws, aws
    rc, _, tf, err = deploy("plan", "RELAY_SOURCE_CIDRS=10.0.1.0/24,10.0.2.0/24\n")
    assert "relay_source_cidrs=" + json.dumps(["10.0.1.0/24", "10.0.2.0/24"], separators=(",", ":")) in tf, tf[-600:]
    rc, _, tf, err = deploy("plan", "")
    assert "relay_source_cidrs" not in tf, tf[-600:]
    # ⑥ トンネルを張らない箱＝TUNNEL_ID も TUNNEL_HOST_* も無い env で secrets が 0 で終わり、トンネルの登録を飛ばし（警告）、
    #    relay_allow_cidrs は登録される。公開ホストがあるのに TUNNEL_ID が無いのは止まる（relay_allow_cidrs に進まない）。
    notunnel = "ENVIRONMENT=t\nAWS_PROFILE=x\nAWS_REGION=ap-northeast-1\n"
    rc, aws, _, err = deploy("secrets", "RELAY_SOURCE_CIDRS=10.0.1.0/24\n", base_=notunnel)
    assert rc == 0, err[-400:]
    assert "cloudflared_tunnel_id" not in aws and "cloudflared_credentials" not in aws, aws
    assert "--name /polyarchy/t/relay_allow_cidrs --value 10.0.1.0/24" in aws, aws
    assert "トンネル" in err and "warn" in err, err[-400:]
    rc, aws, _, err = deploy("secrets", "TUNNEL_HOST_MCP=r.example.org\n", base_=notunnel)
    assert rc != 0 and "TUNNEL_ID" in err and "relay_allow_cidrs" not in aws, (rc, err[-400:], aws)


def test_user_call_limit():
    """利用者ごとの上限（2026-10-16・B44 第 2 便 ②）。1 人（公開の口＝メールのハッシュ・中継の口＝会員の印）が直近 1 時間に呼べる
    ツールの数を、サービスごと（プロセスの中）に数えて止める。固定すること＝① 既定は上限なし（運営者の箱の動きは変わらない）
    ② 上限までは通る・超えた 1 本は実行せずに「上限」と返す・1 時間たてば戻る・ほかの利用者は影響を受けない（合成の時計）
    ③ 利用者の無い呼び出し（stdio・手元）は数えない ④ 会員の印も 1 人として数える ⑤ 「上限」の形は「該当なし」（found=false・
    results 空）とも「混んでいる」とも違う＝エラーの結果・実行していない・ほかで埋めない ⑥ 「混んでいる」で断った呼び出しは
    数えない ⑦ ツールの定義は変わらない ⑧ 断った行に目印 tool_limited=（利用者のハッシュは出さない）・フィルタで数える
    ⑨ 待受が env を読み、bootstrap が SSM から書く。"""
    import asyncio
    import logging
    import re
    import subprocess
    import tempfile
    from pathlib import Path
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from polyarchy_common import access, mcp_http
    root = Path(__file__).resolve().parents[2]
    now = {"t": 1000.0}
    ran = {"n": 0}
    records = []

    class H(logging.Handler):
        def emit(self, r):
            records.append((r.levelname, r.getMessage()))

    def build():
        mcp = MCPServer("t")

        @mcp.tool(title="検索")
        def q(query: str = "") -> dict:
            """検索の代わり。"""
            ran["n"] += 1
            return {"found": False, "results": [], "query": query}

        return mcp

    async def defs(mcp):
        return [t.model_dump() for t in await mcp.list_tools()]

    lg = logging.getLogger("polyarchy.test_user_limit")
    lg.addHandler(H())
    lg.setLevel(logging.INFO)
    A, B = {"email": "a@example.org", "sub": "a"}, {"email": "b@example.org", "sub": "b"}

    async def call(mcp, user=None, member=None):
        t1 = access._current_user.set(user)
        t2 = access._relay_member.set(member)
        try:
            return await mcp.call_tool("q", {"query": "x"})
        finally:
            access._current_user.reset(t1)
            access._relay_member.reset(t2)

    async def run():
        mcp = build()
        assert mcp_http.limit_tools(mcp, per_hour=0) == []                                  # ① 既定は上限なし
        before = await defs(mcp)
        assert mcp_http.limit_tools(mcp, per_hour=3, clock=lambda: now["t"], logger_name=lg.name) == ["q"]
        assert await defs(mcp) == before                                                    # ⑦ 定義は同じ
        for _ in range(3):                                                                  # ② 上限まで通る
            await call(mcp, A)
        n0 = ran["n"]
        try:
            await call(mcp, A)
            raise AssertionError("4 本目が通った")
        except ToolError as e:
            msg = str(e)
        assert ran["n"] == n0                                                               # 実行していない
        assert mcp_http.LIMIT_MARK in msg and mcp_http.BUSY_MARK not in msg, msg            # ⑤
        assert "実行していません" in msg and "該当なし" in msg and "埋めない" in msg and "60 分" in msg, msg
        assert "found" not in msg and "results" not in msg, msg
        await call(mcp, B)                                                                  # ほかの利用者は通る
        for _ in range(10):                                                                 # ③ 利用者の無い呼び出しは数えない
            await call(mcp)
        now["t"] += 1800
        try:
            await call(mcp, A)
            raise AssertionError("30 分後に通った")
        except ToolError as e:
            assert "30 分" in str(e), str(e)
        now["t"] += 1801                                                                    # 1 時間たてば戻る
        await call(mcp, A)
        member = "0123456789abcdef"                                                         # ④ 会員の印も 1 人
        for _ in range(3):
            await call(mcp, member=member)
        try:
            await call(mcp, member=member)
            raise AssertionError("会員の 4 本目が通った")
        except ToolError:
            pass
        # ⑥ 「混んでいる」で断った呼び出しは数えない＝並べられない状態で 2 本 → 1 本は busy → その分は上限に数えない
        mcp2 = build()
        mcp_http.offload_tools(mcp2, queue_max=0, wait_seconds=1.0)
        mcp_http.limit_tools(mcp2, per_hour=2, clock=lambda: now["t"], logger_name=lg.name)
        rs = await asyncio.gather(call(mcp2, A), call(mcp2, A), return_exceptions=True)   # 2 本目は並べられない＝busy
        busy_n = sum(isinstance(r, ToolError) and mcp_http.BUSY_MARK in str(r) for r in rs)
        assert busy_n == 1, rs
        for _ in range(busy_n):                     # busy の分は数えていない＝もう 1 本通る
            await call(mcp2, A)
        try:
            await call(mcp2, A)
            raise AssertionError("上限を超えて通った")
        except ToolError as e:
            assert mcp_http.LIMIT_MARK in str(e), str(e)

    asyncio.run(run())
    # ⑧ 目印・ハッシュを出さない・フィルタ
    warn = [m for lv, m in records if lv == "WARNING"]
    assert warn and all("tool_limited=hour" in m for m in warn), records
    assert not any(access.user_hash(u["email"]) in m for _, m in records for u in (A, B)), records
    assert not any("0123456789abcdef" in m or "example.org" in m for _, m in records), records
    tf = (root / "deploy/terraform/observability.tf").read_text(encoding="utf-8")
    f = re.search(r'resource "aws_cloudwatch_log_metric_filter" "tool_limited" \{.*?pattern\s*=\s*"\\"([^"\\]+)\\"".*?name\s*=\s*"(tool_limited_\$\{each\.key\})"', tf, re.S)
    assert f and f.group(1) in warn[0], "tool_limited のフィルタ"
    # ⑨ 待受と bootstrap
    import inspect
    serve = inspect.getsource(mcp_http.serve_streamable_http) + inspect.getsource(mcp_http.build_streamable_http_app)
    assert "limit_tools(" in serve and "MCP_USER_CALLS_PER_HOUR" in serve, "serve"
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    fn = re.search(r"^write_service_env\(\) \{.*?^\}\n", boot, re.M | re.S).group(0)
    pf = re.search(r"^relay_port\(\) \{.*?^\}\n", boot, re.M | re.S).group(0)
    d = Path(tempfile.mkdtemp())

    def env_of(ssm: dict) -> str:
        f = d / "svc.env"
        f.unlink(missing_ok=True)
        stub = "ssm_get() { case \"$1\" in " + " ".join(f'{k}) printf %s "{v}";;' for k, v in ssm.items()) + " esac; }\n"
        script = stub + "auth_env_lines() { :; }\nchown() { :; }\nSVC_USER=x\n" + pf + fn + f'write_service_env mcp "{f}" "" "" "" ""\n'
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return f.read_text() if f.exists() else ""
    assert env_of({"user_calls_per_hour": "60"}) == "MCP_USER_CALLS_PER_HOUR=60\n"
    for bad in ("", "0", "abc", "6 0", "-1"):
        assert env_of({"user_calls_per_hour": bad} if bad else {}) == "", bad


def _rsa_jwk(kid: str):
    """テスト用の RSA 鍵＝（秘密鍵, JWKS に載せる公開鍵の dict）。"""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True)
    pub.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return priv, pub


def test_jwks_keyring_keeps_old_keys():
    """IdP の公開鍵（JWKS）の覚え方（2026-10-17・導入直前の点検 ①）。PyJWKClient の既定＝5 分で忘れる・取り直しの待ちは
    30 秒＝IdP の JWKS が取れなくなると 5 分後に全利用者が invalid token になる。固定すること＝① 鍵は長く（24 時間以上）覚える
    ② 覚えている鍵があれば、取り直しに失敗しても古い鍵で検証を続ける（運用設計 §1.2「稼働中のサービスは公開鍵を覚えている」）
    ③ 取り直すのは、覚えた鍵が古くなったときと、トークンの kid に合う鍵が無いとき（鍵の入れ替え）だけ＝合わない kid を
    投げ続けられても、決めた間隔より短くは IdP を叩かない ④ 取得の待ちは数秒 ⑤ 鍵を 1 つも覚えていないときの失敗は失敗のまま。"""
    import time

    import jwt

    from polyarchy_common import access

    assert access.JWKS_LIFESPAN_SECONDS >= 24 * 3600 and access.JWKS_FETCH_TIMEOUT_SECONDS <= 10          # ①④
    assert 10 <= access.JWKS_REFRESH_MIN_INTERVAL_SECONDS <= 600
    import inspect
    assert "PyJWKClient(" not in inspect.getsource(access), "PyJWKClient の既定（5 分・30 秒）に戻さない"

    priv_a, pub_a = _rsa_jwk("a")
    priv_b, pub_b = _rsa_jwk("b")
    jwks = {"keys": [pub_a]}
    calls = {"n": 0, "fail": False}
    clock = {"t": 1000.0}

    def fetch() -> dict:
        calls["n"] += 1
        if calls["fail"]:
            raise OSError("IdP down")
        return json.loads(json.dumps(jwks))

    def sign(priv, kid, **over) -> str:
        claims = {"iss": "https://idp.example", "aud": "aud1", "sub": "u1", "email": "a@example.com",
                  "exp": int(time.time()) + 600, **over}
        return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": kid})

    verify = access.make_oidc_verifier("https://idp.example", "aud1", jwks_fetch=fetch, clock=lambda: clock["t"])
    assert verify(sign(priv_a, "a"))["sub"] == "u1" and calls["n"] == 1
    assert verify(sign(priv_a, "a"))["email"] == "a@example.com" and calls["n"] == 1                       # 覚えている＝取らない
    # ② 古くなった鍵の取り直しに失敗＝古い鍵で通る
    clock["t"] += access.JWKS_LIFESPAN_SECONDS + 1
    calls["fail"] = True
    assert verify(sign(priv_a, "a"))["sub"] == "u1" and calls["n"] == 2
    assert verify(sign(priv_a, "a"))["sub"] == "u1" and calls["n"] == 2                                   # ③ 間隔の内側は叩かない
    clock["t"] += access.JWKS_REFRESH_MIN_INTERVAL_SECONDS + 1
    assert verify(sign(priv_a, "a"))["sub"] == "u1" and calls["n"] == 3                                   # 間隔が空けばもう一度
    # 鍵の入れ替え＝合う kid が無いときだけ取り直す
    calls["fail"] = False
    jwks["keys"] = [pub_a, pub_b]
    clock["t"] += access.JWKS_REFRESH_MIN_INTERVAL_SECONDS + 1
    assert verify(sign(priv_b, "b"))["sub"] == "u1" and calls["n"] == 4
    assert verify(sign(priv_a, "a"))["sub"] == "u1" and calls["n"] == 4
    # 合わない kid を投げ続けられても、間隔より短くは叩かない（取り直しは 1 回・結果は失敗）
    clock["t"] += access.JWKS_REFRESH_MIN_INTERVAL_SECONDS + 1
    for _ in range(3):
        try:
            verify(sign(priv_a, "zzz"))
            raise AssertionError("合わない kid が通った")
        except jwt.exceptions.PyJWTError:
            pass
    assert calls["n"] == 5, calls
    # 署名・aud の検証は変わらない（kid が合えば取り直さない）
    other, _ = _rsa_jwk("a")
    for bad in (sign(other, "a"), sign(priv_a, "a", aud="aud2"), sign(priv_a, "a", iss="https://evil.example")):
        try:
            verify(bad)
            raise AssertionError("不正なトークンが通った")
        except jwt.exceptions.PyJWTError:
            pass
    assert calls["n"] == 5, calls
    # ⑤ 1 つも覚えていなければ失敗のまま（起動直後に IdP が不達＝案内どおり落ちる）。取れれば以後は通る
    calls2 = {"n": 0, "fail": True}

    def fetch2() -> dict:
        calls2["n"] += 1
        if calls2["fail"]:
            raise OSError("IdP down")
        return {"keys": [pub_a]}

    v2 = access.make_oidc_verifier("https://idp.example", None, jwks_fetch=fetch2, clock=lambda: clock["t"])
    try:
        v2(sign(priv_a, "a"))
        raise AssertionError("鍵が無いのに通った")
    except Exception:
        pass
    calls2["fail"] = False
    assert v2(sign(priv_a, "a"))["sub"] == "u1" and calls2["n"] == 2
    # 案 A（Cloudflare Access・温存）も同じ覚え方
    v3 = access.make_access_verifier("team.example", "audA", jwks_fetch=fetch)
    assert v3(sign(priv_a, "a", iss="https://team.example", aud="audA"))["sub"] == "u1"


def test_jwks_keyring_logs_refresh():
    """鍵の取り直しの成功もログに 1 行残す（2026-10-19 staging・運用側の 24 時間後の確認から）。失敗の warning だけだと、
    取り直しが走ったことは「失敗 0 件＋200」からの推測になり、IdP が戻った後に回復したかも読めない。固定すること＝
    初回・鍵が古い・合わない kid のどの取り直しも、成功すると理由と覚えた kid を info で 1 行出す（覚えている鍵で通るときは出さない）。"""
    import logging

    from polyarchy_common import access

    _, pub_a = _rsa_jwk("a")
    _, pub_b = _rsa_jwk("b")
    jwks = {"keys": [pub_a]}
    clock = {"t": 1000.0}
    records: list = []

    class _Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger("polyarchy.test_jwks_refresh")
    grab = _Grab()
    log.addHandler(grab)
    try:
        ring = access.JwksKeyring(lambda: json.loads(json.dumps(jwks)), clock=lambda: clock["t"],
                                  logger_name="polyarchy.test_jwks_refresh")
        infos = lambda: [r.getMessage() for r in records if r.levelno == logging.INFO]
        ring.signing_key("a")
        assert len(infos()) == 1 and "初回" in infos()[0] and "'a'" in infos()[0], infos()
        ring.signing_key("a")
        assert len(infos()) == 1, infos()                                      # 覚えている鍵で通る＝出さない
        clock["t"] += access.JWKS_LIFESPAN_SECONDS + 1
        ring.signing_key("a")
        assert len(infos()) == 2 and "鍵が古い" in infos()[1], infos()
        jwks["keys"] = [pub_a, pub_b]
        clock["t"] += access.JWKS_REFRESH_MIN_INTERVAL_SECONDS + 1
        ring.signing_key("b")
        assert len(infos()) == 3 and "kid='b'" in infos()[2] and "'b'" in infos()[2].split("＝")[-1], infos()
    finally:
        log.removeHandler(grab)


def test_auth_and_health_run_off_event_loop():
    """認証の検証と healthz の検査をイベントループの外へ出す（2026-10-17・導入直前の点検 ②）。verify() は鍵の取得・初回の
    ディスカバリで HTTP を待つ＝ループの上で呼ぶと、その間ほかの利用者の接続も /healthz も止まる。health_check() も同じ
    （recommendations の Qdrant の count は timeout 60 秒＝固まると毎分の /healthz がループを止める）。固定すること＝
    ① 検証の最中もループが回る・検証は別のスレッド ② 検査の最中もループが回る ③ 検査が決めた秒数を超えたら 503（timeout）
    ＝箱の毎分の確認（health_metric.sh の curl -m）より短い ④ 結果（200／401／503）は変わらない。"""
    import asyncio
    import re
    import threading
    import time
    from pathlib import Path

    from polyarchy_common import httplog
    from polyarchy_common.access import access_jwt_middleware, oidc_bearer_middleware

    root = Path(__file__).resolve().parents[2]
    m = re.search(r"curl\s+\S*\s+-m\s+(\d+)", (root / "deploy/bootstrap/health_metric.sh").read_text(encoding="utf-8"))
    assert m and 0 < httplog.HEALTH_TIMEOUT_SECONDS < int(m.group(1)), (m, httplog.HEALTH_TIMEOUT_SECONDS)   # ③ 箱の確認より短い

    main_thread = threading.get_ident()
    seen: dict = {}

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call(app, path="/mcp", headers=()):
        out = []

        async def send(msg):
            out.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await app({"type": "http", "path": path, "method": "GET", "headers": list(headers)}, receive, send)
        return out

    def slow_verify(token: str) -> dict:
        seen["thread"] = threading.get_ident()
        time.sleep(0.3)
        if token != "good":
            raise ValueError("bad")
        return {"email": "a@example.com", "sub": "s"}

    async def run():
        ticks = {"n": 0}

        async def ticker():
            while True:
                await asyncio.sleep(0.01)
                ticks["n"] += 1

        tk = asyncio.create_task(ticker())
        try:
            # ① 認証（案 B・案 A）
            oidc = oidc_bearer_middleware(inner, "https://idp.example", "aud1", "/mcp", "https://x.example/mcp", verify=slow_verify)
            acc = access_jwt_middleware(inner, "team.example", "audA", "/mcp", verify=slow_verify)
            for app, hdr in ((oidc, b"authorization"), (acc, b"cf-access-jwt-assertion")):
                val = b"Bearer good" if hdr == b"authorization" else b"good"
                seen.clear()
                t0 = ticks["n"]
                assert (await call(app, headers=[(hdr, val)]))[0]["status"] == 200
                assert ticks["n"] - t0 >= 20 and seen["thread"] != main_thread, (ticks["n"] - t0, seen)
                bad = b"Bearer evil" if hdr == b"authorization" else b"evil"
                assert (await call(app, headers=[(hdr, bad)]))[0]["status"] == 401                       # ④ 結果は同じ
            # ② healthz の検査
            state = {"sleep": 0.3}

            def check() -> dict:
                seen["hthread"] = threading.get_ident()
                time.sleep(state["sleep"])
                return {"ok": True, "n": 1}

            app = httplog.healthz_middleware(inner, check, service="polyarchy.test", timeout_seconds=0.5)
            seen.clear()
            t0 = ticks["n"]
            out = await call(app, path="/healthz")
            assert out[0]["status"] == 200 and ticks["n"] - t0 >= 20 and seen["hthread"] != main_thread, (out[0], ticks["n"] - t0, seen)
            # ③ 超過＝503・timeout と書く・待たない
            state["sleep"] = 3.0
            t1 = time.monotonic()
            out = await call(app, path="/healthz")
            assert out[0]["status"] == 503 and time.monotonic() - t1 < 1.5, out[0]
            assert "timeout" in json.loads(out[1]["body"])["checks"].get("error", ""), out[1]
            assert (await call(app, path="/mcp"))[0]["status"] == 200                                     # ④ 他パスは素通し
        finally:
            tk.cancel()

    asyncio.run(run())


def test_health_checks_hit_qdrant_live():
    """生き死にの判定は Qdrant へ live に count を取る（2026-10-17・導入直前の点検 ③）。審議会DB の health_check は
    `svc().count`＝起動時に読んだ点数（len(self.meta)）で、専用の Qdrant（:6340）の生死を見ていなかった。固定すること＝
    ① 審議会DB・政策主張DB の `_health` は QdrantCorpusStore の count を毎回取る（起動時の数を返さない）② その Qdrant の
    クライアントの待ちは短い（timeout を渡す＝既定の 60 秒のまま固まったスレッドを毎分残さない）③ ok は count>0。"""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in ("deliberations/serving/mcp_server.py", "recommendations/serving/mcp_server.py"):
        src = (root / rel).read_text(encoding="utf-8")
        fn = re.search(r"^( *)def _health\(\) -> dict:\n(?:\1    .*\n|\n)+", src, re.M)   # 関数の中の定義（字下げ）も可
        assert fn, f"{rel}: _health が無い"
        body = fn.group(0)
        assert "QdrantCorpusStore(" in body and ".count(" in body, f"{rel}: Qdrant へ count を取っていない"   # ①
        assert "timeout=" in body, f"{rel}: Qdrant の待ちを短くしていない"                                      # ②
        assert re.search(r'"ok":\s*n > 0', body), f"{rel}: ok は count>0"                                        # ③
        assert "health_check=_health" in src, f"{rel}: _health を渡していない"
        assert "svc().count" not in src, f"{rel}: 起動時の点数で判定している"


def test_bootstrap_waits_for_qdrant_before_mcp():
    """bootstrap ⑩ は Qdrant を上げ直した直後に政策主張DB を上げ直していた（unit の After= は起動の順を決めるだけで、応答できる
    状態を待たない）＝配布のたびに 1 回目の起動が Connection refused の Traceback を出し、Restart=always で上げ直っていた
    （2026-10-14・10-16・10-18 の staging の配布で同じ行＝運用側の気づき）。害は無いが障害のときに読み違える行＝審議会DB の側と
    同じく、6333 の応答を待ってから上げ直す。固定すること＝① qdrant.service の restart と polyarchy-mcp.service の restart の間に、
    6333 の /collections を待つ繰り返しがある ② 審議会DB の側（6340）は従来どおり。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    seg = boot[boot.index("systemctl restart qdrant.service"):boot.index("systemctl restart polyarchy-mcp.service")]
    assert re.search(r"curl -fsS -m \d+ http://127\.0\.0\.1:6333/collections", seg) and "sleep" in seg, seg   # ①
    seg2 = boot[boot.index("systemctl restart qdrant-deliberations.service"):boot.index("systemctl restart polyarchy-deliberations.service")]
    assert re.search(r"curl -fsS -m \d+ http://127\.0\.0\.1:6340/collections", seg2) and "sleep" in seg2, seg2   # ②


def test_apply_does_not_restart_what_bootstrap_just_started():
    """自動適用（apply_data_update.sh の apply_new）は bootstrap の再走行 → svc_start の順で、どちらも Qdrant とアプリを上げ直していた
    ＝bootstrap ⑩ の後に起きた政策主張DB が、起動の途中で svc_start に Qdrant を止められて Connection refused の Traceback で落ち、
    どのサービスも起動の行が 2 回出ていた（2026-10-14〜18 の staging・1e94b3b は bootstrap の側だけを直していた＝運用側の気づき）。
    固定すること＝svc_start を偽の systemctl・curl で動かして ① Qdrant が 2 つとも応答している（bootstrap の後）＝Qdrant を
    restart せず、アプリは start（restart しない）② 止まっている（切り戻し＝svc_stop の後）＝Qdrant を先に restart してからアプリを
    restart ③ どちらも戻り値 0。"""
    import re
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "deploy/bootstrap/apply_data_update.sh").read_text(encoding="utf-8")
    funcs = "\n".join(m.group(0) for m in (re.search(r"^qdrant_live\(\) \{.*\}$", src, re.M),   # 1 行の関数（あれば）
                                           re.search(r"^svc_start\(\) \{.*?^\}$", src, re.M | re.S)) if m)
    assert "svc_start()" in funcs, "svc_start が見つからない"
    d = Path(tempfile.mkdtemp())
    (d / "bin").mkdir()
    stubs = {   # up＝応答する Qdrant の unit 名（1 行 1 つ）。restart で起き、is-active と curl はそれを見る
        "systemctl": f'echo "$*" >> "{d}/calls"; case "$1" in '
                     f'is-active) grep -qx "${{@: -1}}" "{d}/up";; restart) for u in "${{@:2}}"; do echo "$u" >> "{d}/up"; done;; esac; exit 0',
        "curl": f'case "$*" in *:6333/*) grep -qx qdrant "{d}/up";; *:6340/*) grep -qx qdrant-deliberations "{d}/up";; *) exit 0;; esac',
        "sleep": "exit 0",
    }
    for name, body in stubs.items():
        f = d / "bin" / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)

    def run(up: str):
        (d / "up").write_text(up)
        (d / "calls").write_text("")
        script = (f'export PATH="{d / "bin"}:$PATH"; COMPANIES_ON=1; DELIB_ON=1\n'
                  'log() { echo "$*"; }; reload_ingress() { :; }\n' + funcs + "\nsvc_start")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        calls = [c for c in (d / "calls").read_text().splitlines() if not c.startswith("is-active")]
        return r.returncode, calls, r.stdout + r.stderr

    rc, calls, out = run("qdrant\nqdrant-deliberations\npolyarchy-mcp\n")     # ① bootstrap ⑩ の後
    assert rc == 0, (rc, calls, out)                                                                    # ③
    assert not any(c.startswith("restart") for c in calls), calls                                       # ①
    assert "start polyarchy-mcp polyarchy-stats" in calls and "start polyarchy-deliberations" in calls, calls
    rc, calls, out = run("")                                                    # ② 切り戻し（svc_stop の後）
    assert rc == 0, (rc, calls, out)                                                                    # ③
    assert calls.index("restart qdrant") < calls.index("restart polyarchy-mcp polyarchy-stats"), calls  # ②
    assert calls.index("restart qdrant-deliberations") < calls.index("restart polyarchy-deliberations"), calls
    assert "restart polyarchy-companies" in calls, calls


def test_ops_dirs_owned_by_service_user():
    """新しい環境の最初の構築で、ダッシュボードが Permission denied で落ちた（2026-10-28・unit-failed・B47）。
    自動適用（root）が走るたびに最初に `ops/dashboard`（適用の記録の置き場）を mkdir する＝まだ一度も release.sh で配っていない環境では
    コードの展開（その後の chown -R）が走らず、`ops/` が root の持ち物のまま残る → polyarchy で動くダッシュボード・同期・週次レポートが
    書けない（上流の staging は配布のたびに chown されるので出なかった）。固定すること＝① 自動適用は `ops/` と適用の記録の置き場を
    polyarchy の持ち物として作る（mkdir で作らない）② bootstrap は、タイマーを動かす前に、polyarchy で動く生成物の置き場
    （polyarchy_common の `ROOT / "ops" / "<名前>"` と箱のシェルの `ops/<名前>/`）と `ops/` を polyarchy の持ち物として作る。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    apply = (root / "deploy/bootstrap/apply_data_update.sh").read_text(encoding="utf-8")
    for line in re.findall(r"^\s*mkdir -p (.+)$", apply, re.M):                                              # ①
        assert "$MARK_DIR" not in line and "$REPO_DIR/ops" not in line, line
    owned = [l for l in re.findall(r"^\s*install -d -o polyarchy -g polyarchy (.+)$", apply, re.M)]
    assert any('"$REPO_DIR/ops"' in l and '"$MARK_DIR"' in l for l in owned), owned
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    first_timer = boot.index("systemctl enable --now polyarchy-")
    made = set()
    for line in re.findall(r'^\s*install -d -o "\$SVC_USER" -g "\$SVC_USER" (.+)$', boot[:first_timer], re.M):
        made.update(re.findall(r'"\$REPO_DIR/([^"]+)"', line))
    need = {"ops"}
    for py in (root / "polyarchy_common").glob("*.py"):                                                       # ②
        need.update("ops/" + m for m in re.findall(r'ROOT / "ops" / "([a-z_]+)"', py.read_text(encoding="utf-8")))
    for sh in (root / "deploy/bootstrap").glob("*.sh"):     # 箱で動くシェルの書き先も（fuelsync.sh など）。S3 のキー（…/ops/report/）は数えない
        t = sh.read_text(encoding="utf-8")
        need.update("ops/" + m for m in re.findall(r'\$REPO_DIR/ops/([a-z_]+)', t))
        need.update("ops/" + m for m in re.findall(r'(?<![\w/$])ops/([a-z_]+)/', t))
    assert {"ops/dashboard", "ops/usage"} <= need, need      # 生成物の置き場を見つけられていること（検査そのものの確かめ）
    assert need <= made, (sorted(need - made), "bootstrap がタイマーより前に polyarchy の持ち物として作らない")


HARDENED_UNITS = ("polyarchy-mcp", "polyarchy-stats", "polyarchy-companies", "polyarchy-deliberations",
                  "qdrant", "qdrant-deliberations")


def test_units_are_confined():
    """公開面を持つ 4 つの MCP と Qdrant 2 つの封じ込め（B46 (a)・2026-10-19）。固定すること＝① 4 つの指定（NoNewPrivileges・
    ProtectSystem=strict・ProtectHome・PrivateTmp）② 書ける先（ReadWritePaths）が、その unit が env で指す書き込み先を覆う＝MCP は
    捕捉ログ（*_QUERY_LOG）のフォルダ・Qdrant は索引（STORAGE_PATH）と写し（SNAPSHOTS_PATH）③ Qdrant の起動の目印は
    RuntimeDirectory の下（既定の作業フォルダは読むだけ）④ 書ける先はどれも bootstrap が先に作る（strict の下ではサービスが自分で
    作れない・無いと unit が起動しない）。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    made = set()
    for line in re.findall(r"^\s*mkdir -p (.+?)(?:;|$)", boot, re.M):
        for a in re.findall(r'"([^"]+)"', line):
            made.add(a.replace("$APP_DIR", "/opt/polyarchy/polyarchy/recommendations")
                      .replace("$REPO_DIR", "/opt/polyarchy/polyarchy"))
    for name in HARDENED_UNITS:
        u = (root / f"deploy/systemd/{name}.service").read_text(encoding="utf-8")
        body = [l for l in u.splitlines() if l and not l.startswith("#")]
        for d in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes"):        # ①
            assert d in body, (name, d)
        rw = [p for l in body if l.startswith("ReadWritePaths=") for p in l.split("=", 1)[1].split()]
        env = dict(re.findall(r"^Environment=([A-Z0-9_]+)=(\S+)$", u, re.M))
        if name.startswith("qdrant"):
            need = [env["QDRANT__STORAGE__STORAGE_PATH"], env["QDRANT__STORAGE__SNAPSHOTS_PATH"]]
            rd = re.search(r"^RuntimeDirectory=(\S+)$", u, re.M)
            assert rd and env.get("QDRANT_INIT_FILE_PATH", "").startswith(f"/run/{rd.group(1)}/"), name       # ③
        else:
            need = [str(Path(v).parent) for k, v in env.items() if k.endswith("_QUERY_LOG")]
            assert len(need) == 1, (name, need)
        for p in need:                                                                                       # ②
            assert any(p == r or p.startswith(r.rstrip("/") + "/") for r in rw), (name, p, rw)
        for r in rw:                                                                                         # ④
            assert r.lstrip("-") in made, (name, r, "bootstrap が作らない")


def test_rollback_restores_units():
    """切り戻しで unit も旧版に戻す（2026-10-19・B46 (a) の前に）。unit を置くのは bootstrap で、切り戻しは bootstrap を走らせない
    ＝新しい版の unit で起動できなかったとき、旧版のコードに戻しても unit が新しいままで起動しない。固定すること＝① 切り戻しの
    段で、旧版の tar の展開の後・起動の前に units_restore を呼ぶ ② units_restore は、置いてある unit のうち戻したコードと中身が違う
    ものだけを置き直して daemon-reload する ③ 置いていない unit（無効のサービス）は置かない ④ 違いが無ければ daemon-reload もしない。"""
    import re
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "deploy/bootstrap/apply_data_update.sh").read_text(encoding="utf-8")
    rb = src[src.index("# ── ④ 自動切り戻し"):]
    i_ex, i_start = rb.find('extract_code "$REL_DIR/prev.tar.gz"'), rb.find("if svc_start")
    assert 0 <= i_ex < rb.find("units_restore", i_ex) < i_start, "切り戻しで旧版の展開の後・起動の前に units_restore を呼ぶ"  # ①
    m = re.search(r"^units_restore\(\) \{.*?^\}$", src, re.M | re.S)
    assert m, "units_restore が見つからない"
    d = Path(tempfile.mkdtemp())
    etc, repo = d / "etc", d / "repo"
    (repo / "deploy/systemd").mkdir(parents=True)
    etc.mkdir()
    (d / "bin").mkdir()
    (d / "bin/systemctl").write_text(f'#!/bin/bash\necho "$*" >> "{d}/calls"\n')
    (d / "bin/systemctl").chmod(0o755)
    for n, old, new in (("polyarchy-mcp.service", "old-mcp", "new-mcp"),        # 新しい版で変わった＝戻す
                        ("qdrant.service", "same", "same"),                    # 変わっていない＝触らない
                        ("polyarchy-stats.service", "old-stats", None),        # 置いていない（無効）＝置かない
                        ("polyarchy-health.timer", "old-timer", "new-timer")):
        (repo / "deploy/systemd" / n).write_text(old)
        if new is not None:
            (etc / n).write_text(new)
    fn = m.group(0).replace("/etc/systemd/system", str(etc))

    def run():
        (d / "calls").write_text("")
        r = subprocess.run(["bash", "-c", f'set -euo pipefail; export PATH="{d / "bin"}:$PATH"; REPO_DIR="{repo}"\n'
                            'log() { echo "$*"; }\n' + fn + "\nunits_restore"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return (d / "calls").read_text().split()

    assert run() == ["daemon-reload"]                                                                         # ②
    assert (etc / "polyarchy-mcp.service").read_text() == "old-mcp" and (etc / "polyarchy-health.timer").read_text() == "old-timer"
    assert (etc / "qdrant.service").read_text() == "same" and not (etc / "polyarchy-stats.service").exists()  # ③
    assert run() == []                                                                                        # ④


def test_box_lock_has_no_genai_clients():
    """箱のロックに生成 AI の API のクライアントを入れない（B46 (b)・2026-10-19）。配信のサーバは生成 AI を呼ばない（ルート CLAUDE.md
    §3）のに、取込・評価の依存が同じ extras にあって箱に入っていた。固定すること＝① 箱のロック（lock-recommendations-stats.txt・
    lock-stats.txt）に anthropic・openai・llama-index の LLM／OpenAI 系・streamlit が無い ② 作業用 PC のロック（lock-all.txt）は
    箱のロックの全部を同じ版で含む（ゲートが測る依存＝箱で動く依存・check_lock.py は箱のロックで照合する）③ pyproject の箱の
    extras（recommendations・stats）にも書かない。"""
    import re
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    req = root / "deploy/requirements"

    def pins(name: str) -> dict:
        out = {}
        for line in (req / name).read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line)
            if m:
                out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
        return out

    banned = re.compile(r"^(anthropic|openai|streamlit|llama-index-llms-.*|llama-index-embeddings-openai|llama-index-agent-openai"
                        r"|llama-index-multi-modal-llms-openai|llama-index-program-openai|llama-index-question-gen-openai)$")
    box = pins("lock-recommendations-stats.txt")
    for name in ("lock-recommendations-stats.txt", "lock-stats.txt"):                                          # ①
        bad = sorted(n for n in pins(name) if banned.match(n))
        assert not bad, (name, bad)
    work = pins("lock-all.txt")                                                                                # ②
    diff = sorted(f"{n} 箱={v} 作業用={work.get(n)}" for n, v in box.items() if work.get(n) != v)
    assert not diff, diff
    assert "anthropic" in work, "作業用 PC のロックに取込の依存（ingest）が無い"
    extras = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]  # ③
    for ex in ("recommendations", "stats"):
        names = [re.split(r"[<>=\[ ;]", d, maxsplit=1)[0].lower() for d in extras[ex]]
        assert not [n for n in names if banned.match(n) or n == "llama-index"], (ex, names)
    # ④ 稼働中の箱からも外す＝pip install -r はロックから外れたものを消さない。bootstrap ③ が、ロックから入れた後に env にあって
    #   ロックに無いもの（pip と本体は残す）を pip uninstall する。
    import importlib.util
    import tempfile
    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    i_req = boot.index('install --no-input --require-hashes -r "$LOCK_FILE"')
    i_nil = boot.find('check_lock.py" --not-in-lock "$LOCK_FILE"', i_req)
    assert i_req < i_nil < boot.find('uninstall --no-input -y "${NOT_IN_LOCK[@]}"', i_nil), "bootstrap ③ がロックに無いものを外さない"
    spec = importlib.util.spec_from_file_location("check_lock", root / "deploy/scripts/check_lock.py")
    cl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cl)
    from importlib import metadata
    have = sorted({cl._norm(d.metadata["Name"]) for d in metadata.distributions() if d.metadata["Name"]})
    keep = [n for n in have if n not in cl.KEEP][:3]
    lock = Path(tempfile.mkdtemp()) / "lock.txt"
    lock.write_text("".join(f"{n}==0 \\\n    --hash=sha256:00\n" for n in keep), encoding="utf-8")
    extra = cl.not_in_lock(lock)
    assert not set(extra) & set(keep) and not set(extra) & cl.KEEP, extra
    assert set(extra) == set(have) - set(keep) - cl.KEEP


def test_box_entrypoints_import_from_box_lock():
    """箱で動く Python の入口から辿れる import が、箱のロック（lock-recommendations-stats.txt）だけで揃う（B48・2026-10-30）。
    B46 (b) で beautifulsoup4 を作業用 PC だけの extras に移し、箱の更新チェックの新着チェック（check_new → collect）が import で
    止まっていた（best-effort で続行＝配布もゲートも通り、ダッシュボードの欄が黙って古くなった）。入口＝systemd のユニットと
    deploy/bootstrap/*.sh が `-m` で呼ぶモジュールと、そこが呼ぶ deploy/ の .py。リポジトリのモジュールを関数の中の import も
    含めて辿る（呼ばれる枝かどうかは見ない＝try/except ImportError の中も数える＝check_new はそこで落ちた）。"""
    import ast
    import re
    import sys
    from importlib import metadata
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    tops = {p.name for p in root.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    entries, scripts = set(), set()
    for f in sorted((root / "deploy/bootstrap").glob("*.sh")) + sorted((root / "deploy/systemd").glob("*.service")):
        text = "\n".join(ln for ln in f.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
        entries |= set(re.findall(r"-m ((?:%s)(?:\.\w+)+)" % "|".join(sorted(tops)), text))
        scripts |= set(re.findall(r"\$REPO_DIR/(deploy/[\w/]+\.py)", text))
    for must in ("recommendations.ingest.check_new", "stats.ops.freshness", "recommendations.serving.mcp_server",
                 "stats.serving.mcp_server", "companies.serving.mcp_server", "deliberations.serving.mcp_server"):
        assert must in entries, f"入口の読み取りが壊れている（{must} が無い）"

    def modfile(mod):
        p = root.joinpath(*mod.split("."))
        return p / "__init__.py" if (p / "__init__.py").exists() else (p.with_suffix(".py") if p.with_suffix(".py").exists() else None)

    ext, seen = {}, set()
    todo = [(m, modfile(m)) for m in entries] + [("", root / s) for s in scripts]
    while todo:
        mod, f = todo.pop()
        if f is None or f in seen:
            continue
        seen.add(f)
        pkg = mod if f.name == "__init__.py" else mod.rpartition(".")[0]
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    parts = pkg.split(".")
                    base = ".".join(parts[:len(parts) - n.level + 1] + ([n.module] if n.module else []))
                names = [base] + [f"{base}.{a.name}" for a in n.names]
            else:
                continue
            for nm in names:
                top = nm.split(".")[0]
                if top in tops:
                    parts = nm.split(".")
                    todo += [(".".join(parts[:i]), modfile(".".join(parts[:i]))) for i in range(1, len(parts) + 1)]
                elif top and top not in sys.stdlib_module_names and top != "__future__":
                    ext.setdefault(nm, set()).add(str(f.relative_to(root)))
    assert any("check_new.py" in str(f) for f in seen) and any(f.name == "collect.py" for f in seen), "関数の中の import を辿れていない"

    norm = lambda s: re.sub(r"[-_.]+", "-", s).lower()   # noqa: E731
    lock = {norm(m.group(1)) for m in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==",
                                                  (root / "deploy/requirements/lock-recommendations-stats.txt").read_text(encoding="utf-8"), re.M)}
    pd = metadata.packages_distributions()
    other_name = {"bs4": "beautifulsoup4", "fitz": "pymupdf", "jwt": "pyjwt", "dotenv": "python-dotenv", "yaml": "pyyaml"}

    def dists(nm):
        parts = nm.split(".")
        if parts[0] == "llama_index":   # 名前空間パッケージ＝下の 2 段で配布が分かれる（llms.anthropic は箱に入れない）
            if len(parts) < 2:
                return None
            return ["llama-index-core"] if parts[1] == "core" else ([f"llama-index-{parts[1]}-{parts[2]}"] if len(parts) > 2 else None)
        return pd.get(parts[0]) or [other_name.get(parts[0], parts[0])]
    # 例外＝箱では呼ばれないと分かっている選択肢だけ（配布名・ファイルの組で。ほかのファイルが使い始めたら落ちる）。
    #   embeddings.py の openai_*＝取込・比較の埋め込みの選択肢（呼んだときだけ作る）。箱の本番は ruri＝B46 (b) で意図して外した。
    allowed = {("llama-index-embeddings-openai", "recommendations/core/embeddings.py")}
    from recommendations.core.config import PRODUCTION_EMBEDDING
    assert not PRODUCTION_EMBEDDING.startswith("openai"), "本番の埋め込みが API 側＝箱で動かない（例外の前提が崩れた）"
    missing = {}
    for nm, files in ext.items():
        ds = dists(nm)
        if ds is not None and not any(norm(d) in lock for d in ds):
            key = ", ".join(sorted({norm(d) for d in ds}))
            rest = {f for f in files if (key, f) not in allowed}
            if rest:
                missing.setdefault(key, set()).update(rest)
    assert not missing, {k: sorted(v) for k, v in missing.items()}


def test_check_new_import_failure_is_visible():
    """新着チェックの目次の取得（collect）が import で落ちたら、全団体を「チェック失敗」として結果に書き、終了コード 1（B48）。
    前は結果のファイルを書かずに落ち、ダッシュボードが前の「最終チェック」を出し続けた（更新チェックは best-effort で続行）。"""
    import json
    import os
    import re
    import subprocess
    import sys
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    d = Path(tempfile.mkdtemp())
    (d / "catalog.csv").write_text("doc_id\n", encoding="utf-8")
    code = ("import sys; sys.modules['bs4'] = None\n"            # bs4 の無い箱と同じ＝import が落ちる（取得は走らない）
            "from pathlib import Path\n"
            "from recommendations.ingest import check_new as c\n"
            "c.CATALOG = Path(sys.argv[1]) / 'catalog.csv'; c.OUT_PATH = Path(sys.argv[1]) / 'update_check.json'\n"
            "raise SystemExit(c.main([]))\n")
    r = subprocess.run([sys.executable, "-c", code, str(d)], cwd=root, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(root)})
    assert r.returncode == 1, (r.returncode, r.stderr[-800:])
    out = json.loads((d / "update_check.json").read_text(encoding="utf-8"))
    assert "bs4" in out["import_error"], out
    src = (root / "recommendations/ingest/check_new.py").read_text(encoding="utf-8")
    plans = re.findall(r'^        "(\w+)": lambda', src, re.M)
    from recommendations.ingest.check_new import ORGS
    assert tuple(plans) == ORGS and sorted(out["orgs"]) == sorted(ORGS), (plans, ORGS)
    assert all("bs4" in o["error"] for o in out["orgs"].values()), out
    from polyarchy_common import ops_dashboard as od                       # ダッシュボードの欄に「チェック失敗」が出る
    keep = od.UPDATE_CHECK_PATH
    try:
        od.UPDATE_CHECK_PATH = d / "update_check.json"
        assert sorted(od.update_check_summary()["errors"]) == sorted(ORGS)
    finally:
        od.UPDATE_CHECK_PATH = keep


def test_check_new_empty_index_is_failure():
    """目次が 0 件の団体は「新着 0」ではなく「チェック失敗」（2026-10-30・手元の実走で連合のサイトがメンテナンス中＝HTTP 200 で
    メンテナンスのページを返し、目次の解析が 0 件・error なし＝「新着 0」と書いていた）。取れていないものを「無い」と書かない。
    取得は本物の解析の関数のまま、_get だけをメンテナンスのページに差し替える（通信しない）。1 件でも取れた団体は失敗にしない。"""
    import types
    import tempfile
    from pathlib import Path
    from recommendations.ingest import check_new as c
    from recommendations.ingest import collect as col
    d = Path(tempfile.mkdtemp())
    (d / "catalog.csv").write_text("doc_id\n", encoding="utf-8")
    maint = ("<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"UTF-8\"><title>メンテナンス中</title></head>"
             "<body><h1>ただいまメンテナンス中です</h1></body></html>")
    keep = (c.CATALOG, col._get, col.nissho_index)
    try:
        c.CATALOG = d / "catalog.csv"
        def no_json():
            raise ValueError("not JSON")                        # requests と同じ＝HTML に .json() は例外
        col._get = lambda url: types.SimpleNamespace(text=maint, status_code=200, url=url, json=no_json)
        col.nissho_index = lambda: [{"year": 2026, "stem": "x", "title": "取れた 1 件"}]   # 対照＝取れた団体
        out = c.check()
    finally:
        c.CATALOG, col._get, col.nissho_index = keep
    rows = out["orgs"]
    assert rows["nissho"]["error"] is None and rows["nissho"]["total_index"] == 1, rows["nissho"]
    bad = {k: r for k, r in rows.items() if k != "nissho" and not r["error"]}
    assert not bad, bad                                    # 0 件（か解析の例外）＝全部「チェック失敗」
    assert "0 件" in rows["rengo"]["error"], rows["rengo"]   # 連合の解析は例外を出さず 0 件を返す＝この判定で拾う
    assert all(r["new"] == 0 for k, r in rows.items() if k != "nissho"), rows


def test_box_born_not_tracked():
    """箱で生まれるもの（<svc>/data/cache/・query_log/）を git で追跡しない（2026-10-30＝政策主張DB の新着チェックの結果
    update_check.json が公開の最初のコミットから追跡されていた＝手元で流すたびに差分になり、公開リポに 09-03 の結果が残っていた。
    code tar は */data を除くので箱には影響しない）。"""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    tracked = subprocess.run(["git", "ls-files", "--", "*/data/cache/*", "*/data/query_log/*"],
                             cwd=root, capture_output=True, text=True, check=True).stdout.split()
    assert not tracked, tracked
    probe = [f"{s}/data/{b}/x.json" for s in ("recommendations", "stats", "companies", "deliberations")
             for b in ("cache", "query_log")]
    ign = subprocess.run(["git", "check-ignore", "--no-index", *probe], cwd=root, capture_output=True, text=True).stdout.split()
    assert sorted(ign) == sorted(probe), sorted(set(probe) - set(ign))


def test_bootstrap_uninstall_zero_and_some():
    """bootstrap ③ の「ロックに無いものを外す」を、外すものが 0 件のときも動かす（2026-10-20・9a4b864 の配布で切り戻し）。依存が
    変わらない配布で check_lock.py --not-in-lock が空の行を 1 行出し、mapfile がそれを 1 件と数えて `pip uninstall ''` で止まった
    （前の 2 回は外すものがあって通っていた＝検査は文字の並びだけだった）。固定すること＝① check_lock.py --not-in-lock は外すものが
    無ければ何も出さない ② bootstrap ③ の実物の行を偽の sudo・pip の上で動かす：空の出力・空の行だけの出力では uninstall を呼ばない・
    「外す」の行も出さない ③ 名前があれば、その名前だけを（空の名前を混ぜずに）uninstall に渡す。"""
    import re
    import subprocess
    import sys
    import tempfile
    from importlib import metadata
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    d = Path(tempfile.mkdtemp())
    names = sorted({re.sub(r"[-_.]+", "-", m.metadata["Name"]).lower() for m in metadata.distributions() if m.metadata["Name"]})
    lock = d / "lock.txt"                                                                                   # ① env の全部を載せたロック
    lock.write_text("".join(f"{n}==0 \\\n    --hash=sha256:00\n" for n in names), encoding="utf-8")
    r = subprocess.run([sys.executable, str(root / "deploy/scripts/check_lock.py"), "--not-in-lock", str(lock)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "", repr(r.stdout)

    boot = (root / "deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")                            # ②③ 実物の行
    m = re.search(r"^NOT_IN_LOCK=\(\)$.*?^fi$", boot, re.M | re.S)
    assert m, "bootstrap ③ の外す段が見つからない"
    (d / "bin").mkdir()
    for name, body in {"sudo": 'shift 2; exec "$@"',                                                         # sudo -u <user> … を素通し
                       "fakepy": f'cat "{d}/out"',                                                           # check_lock の出力の代わり
                       "fakepip": f'for a in "$@"; do [[ -n "$a" ]] || {{ echo "空の名前" >&2; exit 1; }}; done; echo "$*" >> "{d}/pip"'}.items():
        (d / "bin" / name).write_text("#!/bin/bash\n" + body + "\n")
        (d / "bin" / name).chmod(0o755)

    def run(out: str):
        (d / "out").write_text(out)
        (d / "pip").write_text("")
        script = (f'set -euo pipefail; export PATH="{d / "bin"}:$PATH"\n'
                  f'SVC_USER=svc ENV_PY="{d}/bin/fakepy" ENV_PIP="{d}/bin/fakepip" REPO_DIR=/x LOCK_FILE=/x/lock.txt\n' + m.group(0))
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        return p.returncode, p.stdout, (d / "pip").read_text().splitlines()

    for out in ("", "\n", "\n\n"):                                                                          # ② 0 件
        rc, so, pip = run(out)
        assert rc == 0 and pip == [] and "外す" not in so, (repr(out), rc, so, pip)
    rc, so, pip = run("httpx-sse\npydantic-settings\n")                                                    # ③ 2 件
    assert rc == 0 and pip == ["uninstall --no-input -y httpx-sse pydantic-settings"] and "（2 件）" in so, (rc, so, pip)


def test_terraform_state_on_s3():
    """terraform の state を S3 に置く（B46 (c)・2026-10-19）。local の state は deploy.sh を流したフォルダにだけあり、失うと apply と
    destroy ができなかった。固定すること＝① versions.tf の backend は s3（値は持たない）で、deploy の各スクリプトの terraform init は
    tf_init（値を渡す）と state-migrate だけ ② バケットとキーは環境ごと ③ state のバケットを作るときは版・公開の遮断・暗号化・
    TLS 以外の拒否 ④ state-migrate は S3 に同じ環境の state が既にあれば止める（上書きしない）・local の state が無ければ止める
    ⑤ 移すときは手元の state を残してから -migrate-state で移し、移した後の plan まで流す。"""
    import re
    import subprocess
    import tempfile
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    ver = (root / "deploy/terraform/versions.tf").read_text(encoding="utf-8")
    assert re.search(r'^\s*backend "s3" \{\}', ver, re.M), "versions.tf の backend が s3 でない"                    # ①
    lib = (root / "deploy/scripts/lib.sh").read_text(encoding="utf-8")
    funcs = {n: re.search(rf"^{n}\(\) \{{.*?^\}}$", lib, re.M | re.S) for n in ("ensure_state_bucket", "tf_init", "do_state_migrate")}
    assert all(funcs.values()), [n for n, m in funcs.items() if not m]
    allowed = funcs["tf_init"].group(0) + funcs["do_state_migrate"].group(0)
    for f in sorted((root / "deploy/scripts").glob("*.sh")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if re.search(r"\bterraform init\b", line) and not line.lstrip().startswith("#"):
                assert line in allowed, (f.name, line)                                                         # ①
    assert re.search(r'^\s*TF_STATE_BUCKET="\$\{PROJECT\}-tfstate-\$\{ENVIRONMENT\}-\$\{ACCOUNT_ID\}"', lib, re.M)     # ②
    assert re.search(r'^\s*TF_STATE_KEY="\$\{PROJECT\}/\$\{ENVIRONMENT\}/terraform.tfstate"', lib, re.M)
    init_args = re.search(r"^\s*TF_INIT_ARGS=\((.*?)^\s*\)", lib, re.M | re.S).group(1)
    for k in ("bucket=$TF_STATE_BUCKET", "key=$TF_STATE_KEY", "encrypt=true", "use_lockfile=true"):
        assert k in init_args, k

    d = Path(tempfile.mkdtemp())
    (d / "bin").mkdir()
    tf = d / "tf"
    tf.mkdir()
    stubs = {   # remote＝S3 に state があるか・bucket＝state のバケットがあるか（ファイルの有無で切り替える）
        "aws": f'echo "aws $*" >> "{d}/calls"; case "$*" in *head-bucket*) [[ -e "{d}/bucket" ]];; '
               f'*head-object*) [[ -e "{d}/remote" ]];; *create-bucket*) touch "{d}/bucket";; esac',
        "terraform": f'echo "terraform $*" >> "{d}/calls"; exit 0',
    }
    for name, body in stubs.items():
        (d / "bin" / name).write_text("#!/bin/bash\n" + body + "\n")
        (d / "bin" / name).chmod(0o755)
    m_args = re.search(r"^\s*TF_INIT_ARGS=\(.*?^\s*\)", lib, re.M | re.S).group(0)

    def run(local: bool, remote: bool, bucket: bool):
        for n, on in (("remote", remote), ("bucket", bucket)):
            (d / n).unlink(missing_ok=True)
            if on:
                (d / n).touch()
        for p in tf.glob("terraform.tfstate*"):
            p.unlink()
        if local:
            (tf / "terraform.tfstate").write_text('{"version": 4}')
        (d / "calls").write_text("")
        script = (f'set -euo pipefail; export PATH="{d / "bin"}:$PATH"\n'
                  f'PROJECT=polyarchy ENVIRONMENT=staging ACCOUNT_ID=111122223333 AWS_REGION=ap-northeast-1 TF_DIR="{tf}"\n'
                  'TF_STATE_BUCKET="${PROJECT}-tfstate-${ENVIRONMENT}-${ACCOUNT_ID}"\n'
                  'TF_STATE_KEY="${PROJECT}/${ENVIRONMENT}/terraform.tfstate"\n' + m_args + "\nTF_ARGS=(-var x=1)\n"
                  'log() { echo "$*"; }; die() { echo "DIE $*" >&2; exit 1; }\n'
                  + "\n".join(m.group(0) for m in funcs.values()) + "\ndo_state_migrate")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        return r.returncode, (d / "calls").read_text().splitlines(), r.stderr

    rc, calls, err = run(local=True, remote=True, bucket=True)                                                  # ④
    assert rc != 0 and "既に state がある" in err and not any(c.startswith("terraform") for c in calls), (rc, calls, err)
    assert not list(tf.glob("terraform.tfstate.pre-s3-*"))
    rc, calls, err = run(local=False, remote=False, bucket=True)
    assert rc != 0 and "移す local の state が無い" in err and not calls, (rc, calls, err)
    rc, calls, err = run(local=True, remote=False, bucket=False)                                                # ③⑤
    assert rc == 0, (rc, calls, err)
    aws = " ".join(c for c in calls if c.startswith("aws"))
    for k in ("create-bucket --bucket polyarchy-tfstate-staging-111122223333", "LocationConstraint=ap-northeast-1",
              "Status=Enabled", "BlockPublicPolicy=true", "AES256", '"aws:SecureTransport":"false"'):
        assert k in aws, k
    tcalls = [c for c in calls if c.startswith("terraform")]
    assert "-migrate-state" in tcalls[0] and "key=polyarchy/staging/terraform.tfstate" in tcalls[0], tcalls
    assert tcalls[1].startswith("terraform plan"), tcalls
    assert len(list(tf.glob("terraform.tfstate.pre-s3-*"))) == 1                                                 # ⑤


def test_release_counts_series_like_dashboard():
    """release.sh がマニフェスト（release/data.json）に書く統計の系列数は、ダッシュボード（ops_dashboard.data_scale）と同じ規則で
    数える＝空行と先頭の「# 生成物」の行は系列ではない（2026-10-18 の staging の配布で data.json 11008・ダッシュボードと healthz 11007
    ＝release.sh が wc -l で行数をそのまま数えていた・運用側の気づき）。固定すること＝release.sh の SERIES_N の式を、コメント行と
    空行を含む偽の series.jsonl に当てた数が、ダッシュボードの規則で数えた数と同じ。"""
    import re
    import subprocess
    import tempfile
    from pathlib import Path
    from polyarchy_common import ops_dashboard
    root = Path(__file__).resolve().parents[2]
    rel = (root / "deploy/scripts/release.sh").read_text(encoding="utf-8")
    m = re.search(r'^SERIES_N=.*$', rel, re.M)
    assert m, "release.sh に SERIES_N の行が無い"
    d = Path(tempfile.mkdtemp())
    p = d / "stats/data/registry/series.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("# 生成物: python -m stats.ingest.seed_registry\n"
                 '{"series_id": "a"}\n\n   \n{"series_id": "b"}\n  # 途中のコメント\n{"series_id": "c"}\n', encoding="utf-8")
    r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + m.group(0) + '\nprintf %s "$SERIES_N"'], cwd=d,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    saved = ops_dashboard.SERIES_PATH
    try:
        ops_dashboard.SERIES_PATH = p
        want = ops_dashboard.data_scale()["stats_series"]
    finally:
        ops_dashboard.SERIES_PATH = saved
    assert want == 3 and r.stdout.strip() == "3", (want, r.stdout, r.stderr)


def test_s3_buckets_deny_insecure_transport():
    """S3 の 2 つのバケット（データ・監査証跡）は、暗号化されていない接続（aws:SecureTransport=false）を Deny するバケットポリシーを
    持つ（2026-10-18・残タスク B46 (d)＝導入直前チェックの残り）。箱・fuelsync・release.sh・CloudTrail はすべて TLS で話す＝動きは
    変わらない。固定すること＝① storage.tf の data と trail.tf の trail の両方に aws_s3_bucket_policy がある ② その中に
    Effect=Deny・Condition Bool aws:SecureTransport=false の文がある ③ 対象はバケットとその中身の両方。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for f, bucket in (("deploy/terraform/storage.tf", "data"), ("deploy/terraform/trail.tf", "trail")):
        tf = (root / f).read_text(encoding="utf-8")
        m = re.search(r'resource "aws_s3_bucket_policy" "%s" \{.*?\n\}' % bucket, tf, re.S)
        assert m, f"{f} に aws_s3_bucket_policy.{bucket} が無い"                                  # ①
        body = m.group(0)
        deny = re.search(r'Effect\s*=\s*"Deny".*?"aws:SecureTransport"\s*=\s*"false"', body, re.S)
        assert deny, f"{f}: TLS を強制する Deny の文が無い"                                        # ②
        assert body.count(f"aws_s3_bucket.{bucket}.arn") >= 2, f"{f}: バケットと中身の両方を対象に"   # ③


LEGACY_VERSION, MODERN_VERSION = "2025-11-25", "2026-07-28"   # 接続の挨拶（initialize）で話す最新の版／要求ごとに名乗る新しい版
TOOL_DEFS_DIR = Path(__file__).resolve().parent / "tool_definitions"
SERVICES = ("recommendations", "stats", "companies", "deliberations")


def _serve_in_thread(app):
    """ASGI アプリを手元の空いたポートで待ち受ける（lifespan 込み＝SDK の stateless の受け口が動く・本番と同じ uvicorn）。
    (URL の頭, 止める関数) を返す。"""
    import socket
    import threading
    import time
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    end = time.monotonic() + 30
    while not server.started:
        assert t.is_alive() and time.monotonic() < end, "待受が始まらない"
        time.sleep(0.02)

    def stop():
        server.should_exit = True
        t.join(15)
    return f"http://127.0.0.1:{sock.getsockname()[1]}", stop


def _rpc(base: str, method: str, params: dict = None, *, era: str, name: str = None):
    """JSON-RPC の要求を 1 本送って (HTTP の状態, 応答の JSON or None) を返す。era＝legacy（接続の挨拶の版を名乗る）／modern
    （要求ごとに新しい版と利用側の情報を _meta とヘッダに載せる＝2026-07-28 の形）。応答が SSE なら data の行を読む。"""
    import json
    import httpx
    params = dict(params or {})
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if era == "modern":
        params["_meta"] = {"io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
                           "io.modelcontextprotocol/clientInfo": {"name": "test_common", "version": "0"},
                           "io.modelcontextprotocol/clientCapabilities": {}}
        headers.update({"mcp-protocol-version": MODERN_VERSION, "mcp-method": method})
        if name:
            headers["mcp-name"] = name
    elif method != "initialize":
        headers["mcp-protocol-version"] = LEGACY_VERSION
    r = httpx.post(base + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                   headers=headers, timeout=60)
    text = r.text.strip()
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:") and ln[5:].strip()]
        text = datas[-1] if datas else ""
    try:
        return r.status_code, json.loads(text) if text else None
    except ValueError:
        return r.status_code, None


def _legacy_session(base: str):
    """接続の挨拶（initialize）→ ツールの一覧。いまの Claude・ChatGPT の手順。(挨拶の結果, ツールの一覧) を返す。"""
    st, init = _rpc(base, "initialize", {"protocolVersion": LEGACY_VERSION, "capabilities": {},
                                         "clientInfo": {"name": "test_common", "version": "0"}}, era="legacy")
    assert st == 200 and init and "result" in init, (st, init)
    st, tl = _rpc(base, "tools/list", era="legacy")
    assert st == 200 and tl and "result" in tl, (st, tl)
    return init["result"], tl["result"]["tools"]


def _service_mcp(svc: str):
    """各サービスの MCP サーバを、データを読まずに組み立てる（ツールの定義は登録のときに決まる＝データは呼び出しのときにだけ要る）。"""
    import os
    import sys
    os.environ.pop("MCP_PRIVACY_URL", None)          # 説明文の既定のページで比べる
    if svc == "recommendations":
        import logging
        import types
        from recommendations.serving.mcp_server import build_server
        return build_server(types.SimpleNamespace(log=logging.getLogger("t.defs"), collection_name="t"))[0]
    if svc == "stats":
        from stats.core.registry import Registry
        from stats.core.values import ValueStore
        from stats.serving.mcp_server import build_server
        return build_server(Registry(), ValueStore())[0]
    if svc == "companies":                            # import のときに stdout を差し替えない（--http の形で読む）
        saved = sys.argv
        sys.argv = [saved[0], "--http"]
        try:
            from companies.serving import mcp_server
        finally:
            sys.argv = saved
        return mcp_server.mcp
    if svc == "deliberations":
        from deliberations.serving import mcp_server
        return mcp_server.mcp
    raise ValueError(svc)


def _tool_definitions(svc: str, era: str = "legacy") -> dict:
    """サービスを本番と同じ組み立て（mcp_http.build_streamable_http_app）で待ち受け、利用側に見える形で読む＝サーバの名前・説明・
    ツールの一覧（並びは名前順）。"""
    import os
    from polyarchy_common import mcp_http
    for k in [k for k in os.environ if k.startswith(("MCP_AUTH_", "MCP_ACCESS_", "MCP_RELAY_", "MCP_USER_CALLS"))]:
        os.environ.pop(k)
    app, _ = mcp_http.build_streamable_http_app(_service_mcp(svc), logger_name=f"t.defs.{svc}")
    base, stop = _serve_in_thread(app)
    try:
        if era == "legacy":
            init, tools = _legacy_session(base)
            head = {"name": init["serverInfo"]["name"], "instructions": init.get("instructions")}
        else:
            st, disc = _rpc(base, "server/discover", era="modern")
            assert st == 200 and disc and "result" in disc, (st, disc)
            st, tl = _rpc(base, "tools/list", era="modern")
            assert st == 200 and tl and "result" in tl, (st, tl)
            r = disc["result"]                       # 新しい版はサーバの名前を _meta で名乗る
            head = {"name": r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "instructions": r.get("instructions")}
            tools = tl["result"]["tools"]
    finally:
        stop()
    return {**head, "tools": sorted(tools, key=lambda t: t["name"])}


def test_tool_definitions_unchanged():
    """ツールの定義は、利用側から見て記録した形のまま（B36・2026-10-20＝MCP SDK を 2.x へ移す前に固定）。ChatGPT のコネクタは
    公開の後にツールの定義を更新できない（変えたら作り直し）・企業情報DB は掲載の審査中で定義を動かさない＝SDK の版を変えても、
    利用側に見える形は 1 文字も変えない。固定すること＝4 サービスとも、本番と同じ組み立てで待ち受け、接続の挨拶（いまの Claude・
    ChatGPT の手順）で読んだ サーバの名前・説明・ツールの一覧（名前・title・説明・引数の型・返り値の型・注釈）が、
    tool_definitions/<サービス>.json と同じ。記録は 1.x（mcp 1.29.1・箱のロック）のときに取った。ツールの定義を意図して
    変えるとき（コネクタの作り直しを伴う）だけ `python -m polyarchy_common.tests.test_common --write-tool-definitions` で取り直す。"""
    import json
    for svc in SERVICES:
        want = json.loads((TOOL_DEFS_DIR / f"{svc}.json").read_text(encoding="utf-8"))
        got = _tool_definitions(svc)
        assert got == want, f"{svc}: ツールの定義が記録と違う（{_first_diff(want, got)}）"


def _first_diff(a, b, at="") -> str:
    if type(a) is not type(b):
        return f"{at or '/'}: {type(a).__name__} → {type(b).__name__}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{at}/{k}: {'無くなった' if k not in b else '増えた'}"
            if a[k] != b[k]:
                return _first_diff(a[k], b[k], f"{at}/{k}")
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{at}: 数 {len(a)} → {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return _first_diff(x, y, f"{at}[{i}]")
    return f"{at}: {str(a)[:80]!r} → {str(b)[:80]!r}"


def test_mcp_both_protocol_eras():
    """接続の手順は、いまの挨拶（initialize・2025-11-25 まで）と、新しい版（2026-07-28＝最初に server/discover を送り、要求ごとに
    版と利用側の情報を名乗る）の両方に応じる（B36・2026-10-20）。1.x は新しい版を「未対応の版」として 400 で断り、利用側が挨拶に
    落ちて続けていた＝落ちなくなればつながらない。固定すること＝① 挨拶の手順でツールの一覧と呼び出しが通る（いまの利用側）
    ② server/discover が 200 で新しい版を名乗る ③ 新しい版のツールの一覧は、挨拶の手順と同じツールの定義（4 サービスとも＝
    記録と同じ）④ 新しい版のツールの呼び出しも、本番と同じ包み（別のスレッドで 1 本ずつ）を通って結果が返る。"""
    import json
    import threading
    from mcp.types import ToolAnnotations
    from polyarchy_common import mcp_http
    from mcp.server.mcpserver import MCPServer

    ran_in = []
    mcp = MCPServer("t-eras")

    @mcp.tool(title="こだま", annotations=ToolAnnotations(title="こだま", readOnlyHint=True))
    def echo(text: str) -> dict:
        """受け取った文字列をそのまま返す。"""
        ran_in.append(threading.current_thread() is threading.main_thread())
        return {"echo": text}

    app, _ = mcp_http.build_streamable_http_app(mcp, logger_name="t.eras")
    base, stop = _serve_in_thread(app)
    try:
        init, tools = _legacy_session(base)                                                       # ①
        assert init["protocolVersion"] == LEGACY_VERSION and [t["name"] for t in tools] == ["echo"], init
        st, r = _rpc(base, "tools/call", {"name": "echo", "arguments": {"text": "あ"}}, era="legacy")
        assert st == 200 and "あ" in json.dumps(r["result"], ensure_ascii=False), (st, r)
        st, disc = _rpc(base, "server/discover", era="modern")                                   # ②
        assert st == 200 and disc and MODERN_VERSION in disc["result"]["supportedVersions"], (st, disc)
        st, tl = _rpc(base, "tools/list", era="modern")                                          # ③
        assert st == 200 and tl["result"]["tools"] == tools, (st, tl)
        st, r = _rpc(base, "tools/call", {"name": "echo", "arguments": {"text": "い"}}, era="modern", name="echo")   # ④
        assert st == 200 and "い" in json.dumps(r["result"], ensure_ascii=False), (st, r)
        assert ran_in and not any(ran_in), "ツールは別のスレッドで動く（本番と同じ包み）"
    finally:
        stop()
    for svc in SERVICES:                                                                          # ③ 4 サービス
        want = json.loads((TOOL_DEFS_DIR / f"{svc}.json").read_text(encoding="utf-8"))
        got = _tool_definitions(svc, era="modern")
        assert got == want, f"{svc}: 新しい版のツールの定義が記録と違う（{_first_diff(want, got)}）"


def _listen_held_open(base: str, seconds: float = 5.0) -> tuple:
    """新しい版の subscriptions/listen を送り、seconds 秒のうちに応答が閉じるか（(閉じた, 最後に読んだ JSON)）。開いたまま＝(False, …)。"""
    import json
    import time
    import httpx
    body = {"jsonrpc": "2.0", "id": 9, "method": "subscriptions/listen",
            "params": {"notifications": {"toolsListChanged": True, "promptsListChanged": True, "resourcesListChanged": True},
                       "_meta": {"io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
                                 "io.modelcontextprotocol/clientInfo": {"name": "test_common", "version": "0"},
                                 "io.modelcontextprotocol/clientCapabilities": {}}}}
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream",
               "mcp-protocol-version": MODERN_VERSION, "mcp-method": "subscriptions/listen"}
    end, last = time.monotonic() + seconds, None
    try:
        with httpx.Client(timeout=seconds) as c, c.stream("POST", base + "/mcp", json=body, headers=headers) as r:
            for line in r.iter_lines():
                text = line[5:].strip() if line.startswith("data:") else line.strip()
                if text.startswith("{"):
                    last = json.loads(text)
                    if "error" in last or "result" in last:
                        return True, last
                if time.monotonic() > end:
                    return False, last
        return True, last
    except httpx.ReadTimeout:
        return False, last


def test_no_subscription_streams():
    """新しい版（2026-07-28）の subscriptions/listen を受け付けない（B36 の続き・2026-10-20）。2.x は既定でこの要求を受け、応答を
    開けたまま「ツールの一覧が変わった」などの知らせを待たせる（staging で claude.ai の 1 本が 295 秒開いていた・ツールの行の無い長い
    POST＝RUNBOOK §1 の「同じ時刻に長い POST」の読み分けを濁す）。こちらのツールは動いている間に変わらない＝知らせは 1 本も来ない
    ＝名乗りを実態に合わせる。固定すること＝4 サービスとも ① 新しい版の server/discover は一覧の変化も購読も名乗らない
    （listChanged・subscribe が false）② 挨拶の手順の名乗りも false のまま（1.x からの形）③ subscriptions/listen は開けたままに
    せず、すぐ断る。ツールの定義は変えない（test_tool_definitions_unchanged）。"""
    from polyarchy_common import mcp_http
    for svc in SERVICES:
        app, _ = mcp_http.build_streamable_http_app(_service_mcp(svc), logger_name=f"t.subs.{svc}")
        base, stop = _serve_in_thread(app)
        try:
            st, disc = _rpc(base, "server/discover", era="modern")
            assert st == 200 and disc and "result" in disc, (svc, st, disc)
            init, _tools = _legacy_session(base)
            for era, caps in (("modern", disc["result"]["capabilities"]), ("legacy", init["capabilities"])):   # ①②
                flags = {f"{k}.{f}": v for k, c in caps.items() if isinstance(c, dict)
                         for f, v in c.items() if f in ("listChanged", "subscribe")}
                assert not any(flags.values()), f"{svc}: {era} の名乗りが一覧の変化・購読を知らせると言っている（{flags}）"
            closed, last = _listen_held_open(base)                                                          # ③
            assert closed and last and "error" in last, f"{svc}: subscriptions/listen を開けたままにした（{last}）"
        finally:
            stop()


if __name__ == "__main__":
    import json
    import sys

    if "--write-tool-definitions" in sys.argv:          # ツールの定義を意図して変えたときだけ（コネクタの作り直しを伴う）
        TOOL_DEFS_DIR.mkdir(exist_ok=True)
        for svc in SERVICES:
            (TOOL_DEFS_DIR / f"{svc}.json").write_text(
                json.dumps(_tool_definitions(svc), ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            print(f"wrote {svc}", file=sys.stderr)
        raise SystemExit(0)
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}", file=sys.stderr)
    raise SystemExit(1 if fails else 0)
