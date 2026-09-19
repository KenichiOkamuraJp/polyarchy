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

    asyncio.run(run())


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
    ]
    rows = aggregate(recs)
    assert len(rows) == 1 and rows[0]["week"] == "2026-W34"
    w = rows[0]
    assert w["total"] == 5 and w["recommendations"] == 2 and w["stats"] == 3 and w["users"] == 1
    assert w["by_source"] == {"app_chat": 1, "mcp": 4}
    assert w["recommendations_zero"] == 1 and w["recommendations_low"] == 1 and w["recommendations_orgs_filter"] == 1 and w["recommendations_period_filter"] == 1
    assert w["stats_find"] == 1 and w["stats_find_filtered"] == 1 and w["stats_lookup"] == 1
    assert w["stats_found_false"] == 1 and w["stats_nf_reasons"] == {"no_values": 1} and w["stats_catalog"] == 1
    # 検索語は集計行にも HTML にも現れない（数字のみ＝恒久蓄積できる根拠）
    import json as _json
    page = render_html(rows, {}, [], "2026-08-21T00:00:00")
    assert secret not in _json.dumps(rows, ensure_ascii=False) and secret not in page
    assert "2026-W34" in page

    # マージ：同じ週は再計算で置換。ただし total が痩せた再計算（ログ 30 日削除起因）は既存を残す
    old = [{"week": "2026-W30", "total": 100}, {"week": "2026-W34", "total": 3}]
    merged = merge_weekly(old, rows)
    assert [r["week"] for r in merged] == ["2026-W30", "2026-W34"]
    assert merged[1]["total"] == 5                                     # 増えた再計算は置換
    shrunk = merge_weekly(merged, [{"week": "2026-W30", "total": 2}])
    assert shrunk[0]["total"] == 100                                   # 痩せた再計算は既存優先


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
