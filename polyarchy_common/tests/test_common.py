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
        assert od.box_memory().startswith("3.81 GB ／ 15.26 GB（25%）"), od.box_memory()
        mem = {u: (cur, file_, peak, since) for u, cur, file_, peak, since in od.service_memory()}
        assert mem == {"polyarchy-companies": ("0.29 GB", "0.10 GB", "0.78 GB", "2026-10-01T10:35:40+09:00"),
                       "qdrant": ("—", "—", "—", "—")}, mem   # 止まっているユニットは載せない・ファイルキャッシュは memory.stat の file
        fs = od.fuelsync_status()
        assert fs["services"]["companies"] == {"result": "failed", "last_ok": "2026-10-01T21:28:00+09:00"}
        assert fs["services"]["recommendations"]["result"] == "—" and fs["last_run"] == "2026-10-01T22:28:00+09:00"
        assert od._jst("2026-10-01T22:28:00+09:00") == "2026-10-01T22:28:00+09:00" and od._jst("") == "" and od._jst("—") == "—"
    finally:
        od.MEMINFO_PATH, od.CGROUP_ROOT, od.FUELSYNC_DIR, od.subprocess.run, od.shutil.which, od.companies_enabled = saved


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
    prune = set(re.findall(r"POLYARCHY_QUERY_LOG=/opt/polyarchy/polyarchy/(\S+)/queries\.jsonl",
                           read("deploy/systemd/polyarchy-logprune.service")))
    assert prune == set(local.values()), prune
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
    files = {"fuelsync": read("deploy/bootstrap/fuelsync.sh"), "logprune": read("deploy/systemd/polyarchy-logprune.service"),
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
            "logprune": f"/{s}/data/query_log/queries.jsonl" in files["logprune"],
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
