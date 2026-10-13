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
    assert "<td>2026-W34</td><td>4</td><td>3</td><td>2・2・1・1</td><td>2</td><td>55%</td><td>0・0・1・1</td><td>1・1・0</td><td>5</td><td>3</td><td>2</td><td>1・1・1・1</td><td>1・1・0</td>" in page, page
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

    assert httplog.CLIENT_LABELS == ("claude", "chatgpt", "other")
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
            "apply の起動": re.search(rf"systemctl restart [^\n]*\bpolyarchy-{s}\b", apply),
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
                "deploy/bootstrap/edge_probe.sh"):
        sent |= set(re.findall(r"MetricName=(\w+)", read(src)))
    tf = read("deploy/terraform/observability.tf")
    alarms = re.findall(r'resource "aws_cloudwatch_metric_alarm" "\w+" \{(.*?)\n\}', tf, re.S)
    custom = [re.search(r'metric_name\s*=\s*"([^"]+)"', a).group(1) for a in alarms if re.search(r'namespace\s*=\s*"polyarchy"', a)]
    from_logs = set(re.findall(r'metric_transformation \{\s*name\s*=\s*"([^"]+)"', tf))
    assert from_logs and sent, (from_logs, sent)
    assert set(custom) == sent | from_logs, (sorted(set(custom) ^ (sent | from_logs)))
    assert {"health", "disk_used_percent", "mem_available_percent", "service_restarts", "unit_failed", "dataapply",
            "edge", "idp_reachable"} == sent, sent


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

if __name__ == "__main__":
    import sys

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
