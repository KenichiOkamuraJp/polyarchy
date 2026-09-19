"""
stats MCP サーバ（統計参照DB・骨格）。

公開ツール（いずれも読み取り専用・層は公開固定・fail-closed）：
- `find_statistics(query?, tags?, org?, limit?)` … 発見層：どんな統計系列があるかを返す
- `lookup_statistic(series_id, period, region?)` … 参照層：値の**厳密参照**。無ければ found=false（近似・補間しない）
- `list_sources()` … 調査主体コードの一覧

■ 状態（D3 最小版）
レジストリ＝ `stats/data/registry/series.jsonl`（`stats.ingest.seed_registry` が生成）。値＝ `stats/data/values/`（`stats.ingest.estat` 等が取込）。
found=false の理由は細分化（docs/参照粒度設計.md §8）。ゲート＝ `stats.eval.mcp_smoke`（fail-closed）＋ `stats.eval.exact_match`（原典完全一致）。

■ stdio 安全 / HTTP
recommendations と同型：起動時に実 stdout をトランスポート専用に確保（polyarchy_common.logsetup）。
HTTP 公開は polyarchy_common.mcp_http（stateless・Access JWT は env で自動装着）。

起動（リポジトリ root を cwd に）：
    python -m stats.serving.mcp_server                       # stdio
    python -m stats.serving.mcp_server --http --port 8766    # Streamable HTTP（配信形）
"""
import json
import re
import os
from typing import Optional

from polyarchy_common.capture import append_record
from polyarchy_common.logsetup import configure_quiet_logging, get_logger, guard_stdout_for_stdio
from polyarchy_common.mcp_http import serve_streamable_http

from stats.core.paths import QUERY_LOG_PATH
from stats.core.periods import FREQ_EXAMPLE, hint_for, matches_freq, parse
from stats.core.registry import SECTORS, Registry, default_registry
from stats.core.values import ValueStore

configure_quiet_logging()

SERVER_NAME = "polyarchy-stats"
TOOLS_DESC = "公開ツール=[list_datasets, find_statistics, lookup_statistic, lookup_panel, list_sources] 層=公開固定 fail-closed"

# 参照層の範囲指定（"開始..終了"）：1 応答の値数上限。超えたら found=false reason=range_too_wide（黙って切らない）。
RANGE_LIMIT = int(os.environ.get("STATS_RANGE_LIMIT", "100"))
PANEL_LIMIT = int(os.environ.get("STATS_PANEL_LIMIT", "2500"))      # lookup_panel の 1 応答の値数上限
PANEL_SERIES_LIMIT = int(os.environ.get("STATS_PANEL_SERIES_LIMIT", "200"))  # lookup_panel の系列数上限
RANGE_EXAMPLE = {"a": "2000..2024", "fy": "FY2000..FY2024", "q": "2023Q1..2024Q4", "fq": "FY2023Q1..FY2024Q4",
                 "h": "FY2020H1..FY2024H2", "m": "2023-01..2024-12", "d": "2024-03-01..2024-03-31", "irr": "2000..2021"}

# e-Stat API 利用規約が求めるクレジット文（サービス公開時に利用者が参照できる場所へ掲出＝サーバ説明文・公開ドキュメント）。
ESTAT_CREDIT = ("このサービスは、政府統計総合窓口(e-Stat)のAPI機能を使用していますが、"
                "サービスの内容は国によって保証されたものではありません。")

# 利用者 Claude に渡すサーバ説明（MCP initialize の instructions）。振る舞い指示ではなく「何を返し・何を返さないか」。
SERVER_INSTRUCTIONS = (
    "Polyarchy 統計参照DB（stats）。日本の公的統計・国際機関統計の「値の厳密参照」サービス（読み取り専用・公開データのみ）。"
    "2層構造：発見層（list_datasets＝目録／find_statistics＝系列検索）と参照層（lookup_statistic＝値の完全一致参照）。"
    "値は公表どおりの文字列で返し（換算・丸め・季調・接続なし）、該当が無ければ found=false と理由を返す（近似値・補間値は返さない）。"
    "系列は scope＝jp（日本の統計）と intl（国際比較＝国別 ISO3・IMF/OECD/世銀）に分かれ、発見層は scope で絞れる（指定なしは jp が先）＝"
    "国際比較を明示的に求められていないときは jp を使う（国際系列は定義が各機関のもので日本の国内統計と混ぜない）。"
    "各値には出典（統計名・調査主体・表・アクセサ・URL・取得日・引用1行）と license（再配布・商用利用条件：◎＝出典明示で商用可／"
    "○＝可だが条件あり／△＝商用は要相談）が付く。status=guide の系列は値を持たず、原典の読み方だけを返す。"
    "収録は GDP・四半期速報（QE）・需給ギャップ・潜在成長率・物価・賃金・労働・財政・資金循環・人口・法人企業統計（業種×規模）・"
    "国際機関統計（IMF/OECD/世銀）まで広い＝経済統計の値や有無を扱うときは Web 検索より先にまず find_statistics／list_datasets で"
    "収録を確認する（未収録なら found=false が理由付きで返る＝それが確認の根拠になる）。"
    f"{ESTAT_CREDIT} 統計値そのものは各公表機関に帰属し、本サービスは値を保証しない（原典で確認すること）。"
)


def build_server(registry: Registry, store: Optional[ValueStore] = None):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    log = get_logger("polyarchy.stats")
    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS, log_level="WARNING")
    # 公式コネクタの審査要件：全ツールに title と readOnlyHint（読み取り専用）。
    RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    configure_quiet_logging()
    store = store or ValueStore()

    def _capture(rec: dict) -> None:
        append_record(QUERY_LOG_PATH, rec, logger_name="polyarchy.stats.capture")

    @mcp.tool(title="統計系列を探す（発見層）", annotations=RO)
    def find_statistics(query: str = "", tags: Optional[list[str]] = None, org: Optional[str] = None,
                        sector: Optional[str] = None, kind: Optional[str] = None,
                        freq: Optional[str] = None, dataset: Optional[str] = None,
                        industry: Optional[str] = None, size: Optional[str] = None, scope: Optional[str] = None, limit: int = 20) -> dict:
        """統計系列を**探す**（発見層）。値は返さない＝値は lookup_statistic で厳密参照する。

        引数：query=語（統計名・系列ID・表題の部分一致）／tags=分野タグ（21分類・政策主張DBと同じ語彙）／
        org=調査主体コード（mof soumu cao boj mhlw imf …）／sector=部門（完全一致。有効値＝マクロ／企業／家計・労働／物価・金融／財政／人口／対外・国際。
        「家計・労働」「物価・金融」「対外・国際」は中黒込みで 1 部門＝「労働」「物価」単独は語彙外）／
        kind=observation|projection（推計値を区別）／freq=a|fy|q|fq|h|m|d（期間粒度コード）／dataset=目録（list_datasets）の dataset 名で絞る／
        industry・size＝dims `<業種>-<規模>` の各トークンで絞る（法人企業統計 hojin：業種 62 スラグ＝allexfin・mfg・nonmfg・mfg_food・wholesale・construction …、
        規模＝allsize・cap1b・cap100m-1b・cap10m-100m。系列 ID の dims 部分と同じ語）／
        **scope＝jp|intl**（jp＝日本の統計〔全国・都道府県〕／intl＝国際比較〔国別・IMF/OECD/世銀〕。指定が無ければ両方返すが **jp が先**＝
        国際比較は明示的に求められたときだけ scope=intl を付ける）／limit（≤50）。
        **並びは決定的**：全体（全産業・全規模）の系列が先・業種/規模の細分は後 → 表題一致 → 系列 ID。
        **細分は畳む**：families（kind=series_family）に 1 枚で返す。畳み軸は dataset ごとに 3 種：
        (1) dims 軸＝業種・規模の細分（hojin 等。全体系列は series 行に残す。id_pattern に dims の slug を埋めて lookup。
        例 mof.hojin.sales.mfg_food-allsize.fy）／(2) variant 軸＝QE（qe2020）の需要項目×種別の直積（axis=variant・items に項目 slug。
        GDP は series 行に残す）／(3) freq 軸＝同一指標の周期違い（cao_gap。axis=freq・freqs に各周期の series_id。最も細かい周期を行に残す）。
        industry または size（dims 軸）・freq（freq 軸）を指定すると畳まずに行で返す。total＝全該当数・truncated＝limit で切れたとき true。
        **「どんなデータがあるか」の全体像は先に list_datasets**（目録）を呼び、そこから dataset を指定してここで掘る。
        返り値：系列一覧（series_id・title・unit・freq・granularity・first_period/last_period・basis・status・accessor.type・タグ）。
        status=planned の系列は**未収録**（取得元は特定済みだが値を持たず、取込時期も未定＝「取込予定」ではない。lookup は found=false）。
        status=guide の系列は**値を持たない**（取得元の再配布条件により発見層のみ）＝lookup は found=false・reason=guide_only と
        原典の読み方（URL・表・行・列）を返すので、値は利用側で原典から読む。
        層は公開固定。該当が無ければ count=0（推測で近い系列を返さない）。
        """
        limit = max(1, min(int(limit or 20), 50))
        if scope and scope not in ("jp", "intl"):
            return {"count": 0, "total": 0, "truncated": False, "layer": "公開（固定）", "series": [],
                    "hint": "scope は jp（日本の統計）か intl（国際比較・国別）"}
        # sector は完全一致の語彙（registry 検証で語彙外は系列側に存在し得ない）＝語彙外指定は決定的に 0 件。
        # フィルタとして回さず入口で有効値一覧を返す（M4：実利用で「労働」「物価」の空振りが 2 モデル 3 実測）。
        if sector and sector not in SECTORS:
            return {"count": 0, "total": 0, "truncated": False, "layer": "公開（固定）", "series": [],
                    "hint": ("sector は次のいずれか（完全一致）＝" + "／".join(SECTORS)
                             + "。「家計・労働」「物価・金融」「対外・国際」は中黒込みで 1 部門"
                             + "（例：「労働」→「家計・労働」・「物価」→「物価・金融」）。")}
        # dataset も完全一致の語彙（第 11 弾 第 5 便・2026-09-15：実利用ログで `mof.hojin`×3・`法人企業統計`×2・`国民経済計算`×3 が無言の 0 件）
        # ＝sector（M4）と同型で、語彙外は入口で有効値一覧を返す。
        if dataset and dataset not in registry.dataset_ids():
            return {"count": 0, "total": 0, "truncated": False, "layer": "公開（固定）", "series": [],
                    "hint": (f"dataset={dataset!r} は存在しない。dataset は series_id の 2 番目の要素（完全一致・list_datasets の dataset 欄）＝"
                             + "／".join(sorted(registry.dataset_ids())) + "。統計名で探すなら query に入れる（例 query=\"法人企業統計 売上高\"）。")}
        found, families, total = registry.search_collapsed(query, tags=tags, org=org, sector=sector, kind=kind, freq=freq,
                                                           dataset=dataset, industry=industry, size=size, scope=scope, limit=limit)
        n_coll = sum(f["series_count"] for f in families)
        log.info("発見層: query=%r tags=%s org=%s sector=%s dataset=%s industry=%s size=%s → %d/%d件（カード %d・畳み %d）",
                 query, tags, org, sector, dataset, industry, size, len(found), total, len(families), n_coll)
        # 捕捉＝result_count は series 行の数。ファミリーカードに畳まれた分は family_count／collapsed に別記する
        # （第 11 弾 第 5 便：畳まれて 0 行になった検索が週次レポート・triage で「0 件」に数えられていた＝利用者はカードで到達している）。
        _capture({"tool": "find_statistics", "query": query, "tags": tags, "org": org, "sector": sector,
                  "kind": kind, "freq": freq, "dataset": dataset, "industry": industry, "size": size, "scope": scope,
                  "result_count": len(found), "family_count": len(families), "collapsed": n_coll, "total": total, "source": "mcp"})
        out = []
        for s in found:
            p = s.payload()
            ps = store.all_periods(s.series_id)
            p["last_period"] = p.get("last_period") or (ps[-1] if ps else "")
            p["has_values"] = store.has_data(s.series_id)
            p["period_example"] = FREQ_EXAMPLE.get(s.freq, "")
            if p.get("breaks"):  # 発見層では簡約（全文は lookup の quality）
                p["breaks"] = [{"period": b["period"], "kind": b["kind"], "treatment": b["treatment"], "label": b["label"]} for b in p["breaks"]]
            out.append(p)
        res = {"count": len(out), "total": total, "truncated": total > len(out) + n_coll, "layer": "公開（固定）", "series": out}
        if families:
            res["families"] = families
            res["applied_filter"] = (f"family collapse: {n_coll} 系列を {len(families)} 枚のファミリーカードに畳んだ"
                                     "（dims 軸＝全体系列・variant 軸＝GDP・freq 軸＝最も細かい周期を series 行に残す。"
                                     "展開は industry／size／freq を指定。variant はカードの items＋id_pattern から series_id を直接組める）")
        if res["truncated"]:
            res["hint"] = (f"該当 {total} 件のうち series {len(out)} 件＋ファミリー {n_coll} 件（全体系列が先・細分は後）。"
                           "dataset／industry／size／freq で絞るか limit（≤50）を上げてください。")
        if total == 0 and query.strip():
            # 0 件の診断（S-1）：語ごとの単独該当数。「語を減らせば当たる 0」と「本当に無い 0」を区別できるようにする。
            facets = {"tags": tags, "org": org, "sector": sector, "kind": kind, "freq": freq, "dataset": dataset, "industry": industry, "size": size, "scope": scope}
            th = registry.token_hits(query, **facets)
            res["matched_tokens"] = th
            alive = [t for t, n in th.items() if n]
            used = {k: v for k, v in facets.items() if v}
            # 絞り込み（dataset/org/…）を外した単独該当数（2026-08-22 利用側指摘：絞り込み後だけだと「語はあるが絞り込みで落ちた」が「本当に無い」に見える）
            th0 = registry.token_hits(query) if used else th
            if used:
                res["matched_tokens_unfiltered"] = th0
            alive0 = [t for t, n in th0.items() if n]
            if len(th) > 1 and alive:
                res["hint"] = ("語の AND で 0 件。単独なら該当する語＝" + "・".join(f"{t}({th[t]})" for t in alive)
                               + "。語を減らして再検索してください（近い系列を推測では返しません）。")
            elif used and alive0:
                res["hint"] = ("語は収録されているが絞り込み " + "・".join(f"{k}={v}" for k, v in used.items())
                               + " では 0 件（絞り込みを外すと " + "・".join(f"{t}({th0[t]})" for t in alive0)
                               + "）。絞り込みを外すか dataset を変えて再検索してください。")
            else:
                res["hint"] = "どの語も収録系列に当たりません（本当に未収録の可能性が高い）。list_datasets で目録を確認してください。"
            # 国際系列（M3）：国・年は系列の次元であって表題の語ではない＝AND に入れると決定的に 0 件。
            if scope == "intl" or (org in ("imf", "oecd", "wb")):
                res["hint"] = (res.get("hint", "") + "国際系列では国名・年を検索語に含めない"
                               "（国は lookup_statistic の region 引数〔ISO3〕・年は period 引数。"
                               "表題は指標名のみ＝例：query=名目GDP）。")
        return res

    _sample_cache: dict = {}

    def _sample_quality(series_id: str) -> dict:
        """生成物 quality.json（stats.ops.series_quality）の系列エントリ。無ければ {}。起動後 1 回読む。"""
        if not _sample_cache:
            from stats.core.paths import REGISTRY_DIR
            qp = REGISTRY_DIR / "quality.json"
            try:
                _sample_cache.update(json.loads(qp.read_text(encoding="utf-8")) if qp.exists() else {})
            except Exception as e:  # noqa: BLE001
                log.warning("quality.json 読込失敗: %s", e)
            _sample_cache.setdefault("__loaded__", {})
        return _sample_cache.get(series_id, {})

    def _quality(s, periods: Optional[list[str]] = None) -> dict:
        """品質メタ（第6弾 段3）：usable_from＝分類改定由来の連続利用開始期／breaks＝断層（kind で性質を区別）。
        periods を渡すと範囲内に入る断層を breaks_in_range に抜き出す（比率系の断層は利用側が機械的に除外できる）。"""
        sq = _sample_quality(s.series_id)
        sample = None
        if sq.get("flags"):
            sample = {k: sq[k] for k in ("flags", "dispersion", "dispersion_ratio", "dispersion_vs", "n_obs", "population_latest", "population_period") if k in sq}
            sample["note"] = ("データから計算した系列自身の性質（原典の注記ではない）：volatile＝対数差分の散らばりが上位集計（dispersion_vs）の 2 倍以上かつ ≥0.15"
                              "（景気変動ではなく標本誤差由来の振れ）／small_cell＝母集団法人数<100 社。単年の値より数年平均か上位集計で見る。")
        if not s.breaks and (not s.usable_from or s.usable_from == s.first_period) and not sample:
            return {}  # 断層なし・usable_from が first_period と同じ・標本注意なし＝載せない（ノイズにしない）
        q = {"usable_from": s.usable_from or s.first_period, "breaks": [dict(b) for b in s.breaks]}
        if sample:
            q["sample"] = sample
        if periods:
            lo, hi = periods[0], periods[-1]
            inr = [b for b in s.breaks if lo < b["period"] <= hi]
            q["breaks_in_range"] = [b["period"] for b in inr]
            # 幅（affects_from..affects_to）が範囲と重なるが、適用点は範囲外＝早期適用等の部分的な影響
            aff = [b for b in s.breaks if b not in inr and b.get("affects_from")
                   and b["affects_from"] <= hi and b.get("affects_to", b["period"]) >= lo]
            q["breaks_affecting_range"] = [{"period": b["period"], "affects_from": b["affects_from"], "affects_to": b.get("affects_to", b["period"])} for b in aff]
            acc = [b for b in inr if b["kind"] == "accounting"]
            if acc:
                q["caution"] = ("範囲内に会計基準の断層（kind=accounting）の適用点。壊れるのは " + (acc[0].get("affected") or "比率の前後比較")
                                + "。不変＝" + (acc[0].get("invariant") or "（定義参照）"))
            elif any(b["kind"] == "accounting" for b in aff):
                q["notice"] = ("範囲は会計断層の適用点を含まないが、早期適用等の影響幅（affects_from〜affects_to）と重なる＝一部の法人（規模が大きいほど多い）"
                               "に影響が入りうる。規模間・業種間の比率比較では脚注を付ける")
        return q

    @mcp.tool(title="統計値を厳密参照する（参照層）", annotations=RO)
    def lookup_statistic(series_id: str, period: str, region: str = "JP") -> dict:
        """統計の値を**厳密に参照**する（参照層）。

        引数：series_id（find_statistics が返す ID）／period（系列の粒度に厳密一致：暦年 "2024"・年度 "FY2024"・
        四半期 "2024Q1"・年度四半期 "FY2024Q1"・半期 "FY2024H1"・月 "2024-03"・日 "2024-03-31"。
        **範囲指定も可＝"開始..終了"**（例 "FY2000..FY2024"・両端含む・同一粒度・1 応答 100 値まで）＝時系列を 1 回で引ける）／
        region（既定 "JP"、都道府県は JIS 2桁、国は ISO3）。
        返り値：found=true なら value（公表どおりの文字列・換算なし）・unit・period・region・kind（observation|projection）・
        vintage・source（統計名・表・アクセサ＝原典の同じセルに戻れる・URL・取得日・引用1行）・
        **quality**（系列に断層があるとき：usable_from＝分類改定由来の連続利用開始期／breaks＝断層の一覧。各断層の kind＝
        classification（分類改定・別系列）|population（母集団推計・水準補正可）|accounting（会計基準・企業形態＝**比率は断層を跨いで比較不可**）|
        coverage（表章範囲の注記）／sample＝標本の薄さ（データから計算：volatile＝散らばりが上位集計の 2 倍以上・small_cell＝母集団法人数<100 社＝単年より数年平均で）。
        範囲指定では breaks_in_range（適用点が範囲内）と breaks_affecting_range（早期適用等の影響幅が重なる）・
        caution（会計断層の適用点を含む＝比率は跨がない）／notice（影響幅のみ重なる＝脚注）も返す）。
        範囲指定では values=[{period,value,…}…]（収録がある期のみ＝欠測期は補間せず単に含まれない）・count・
        source（**系列単位**＝セル位置アクセサは単一 period の lookup で取得可）。
        **該当が無ければ found=false と reason**（unknown_series／period_format／out_of_range／region_not_available／
        not_published／no_values／derived_see_components／guide_only／range_too_wide＝該当が 100 値超）と
        hint（正しい表記・提供範囲・構成系列。値は含めない）。
        reason=guide_only は値を保持しない系列（再配布条件により発見層のみ）＝guide（原典 URL・文書名・表・行・列・単位・注意）を返す。
        **値は利用側が原典を読んで取得する。stats はその値を保証しない**（原典で確認すること）。
        近い年・隣の指標・補間値・派生計算値は返さない（捏造防止）。層は公開固定。
        """
        r = _lookup_core(series_id, period, region)
        rec = {"tool": "lookup_statistic", "series_id": series_id, "period": period, "region": region, "source": "mcp"}
        if r.get("found"):
            log.info("参照層: %s period=%s region=%s → found=true%s", series_id, period, region,
                     f"（範囲 {r['count']} 値）" if "count" in r else "")
            _capture({**rec, "found": True, **({"range_count": r["count"]} if "count" in r else {})})
        else:
            log.info("参照層: %s period=%s region=%s → found=false（%s）", series_id, period, region, r.get("reason"))
            _capture({**rec, "found": False, "reason": r.get("reason")})
        return r

    def _lookup_core(series_id: str, period: str, region: str = "JP") -> dict:
        """lookup_statistic の本体（ログ・捕捉なし＝lookup_panel からも系列ごとに呼ぶ）。"""
        s = registry.get(series_id)

        def nf(reason: str, **extra) -> dict:
            return {"found": False, "reason": reason, "series_id": series_id, "period": period, "region": region,
                    "note": "該当データはありません。近似値・補間値・派生計算値は返しません。", **extra}

        if s is None:
            cov = registry.diagnose_unknown(series_id)
            if cov:  # measure は収録済みで dims の組合せだけが無い（S-3）＝収録済みの組合せを返す
                return nf("unknown_series", coverage=cov,
                          hint=f"measure {cov['measure']!r} は収録済みだが dims {cov['requested_dims']!r} の系列は無い。"
                               "収録済みの業種×規模の組合せは coverage.combinations（ここに無い組合せは未収録＝近似しない）。")
            hint = "find_statistics で系列の存在を確認してください。"
            if series_id.startswith("mof.hojin."):
                hint += ("法人企業統計の系列 ID は mof.hojin.<measure>.<業種slug>-<規模slug>.fy（業種 slug は `_` 区切り＝mfg_food・wholesale、"
                         "規模＝allsize・cap1b・cap100m-1b・cap10m-100m・capu10m）。収録済みの measure は list_datasets(org=\"mof\") の measures。")
            return nf("unknown_series", hint=hint)
        if s.is_derived:
            return nf("derived_see_components", hint="派生値は計算しません。構成系列を lookup してください。",
                      components=list(s.components), definition=s.notes)
        if s.status == "guide":
            return nf("guide_only", hint="この系列は値を保持しません（取得元の再配布条件により発見層のみ）。guide に従って原典を読んでください。"
                                        "stats は読み取った値を保証しません＝原典で確認すること。",
                      guide={**s.accessor.get("guide", {}), "unit": s.unit, "period_format": hint_for(s.freq),
                             "source_url": s.source_url, "notes": s.notes, "license": s.license})
        is_range = ".." in period
        if is_range:
            lo_t, _, hi_t = period.partition("..")
            lo_t, hi_t = lo_t.strip(), hi_t.strip()
            if not (matches_freq(lo_t, s.freq) and matches_freq(hi_t, s.freq)):
                return nf("period_format", hint=f"{hint_for(s.freq)}。範囲指定は \"開始..終了\"＝例 \"{RANGE_EXAMPLE.get(s.freq, '')}\"")
            lo, hi = parse(lo_t), parse(hi_t)
            if lo.sort_key() > hi.sort_key():
                return nf("period_format", hint="範囲は \"開始..終了\" の順（開始 ≤ 終了）")
        elif not matches_freq(period, s.freq):
            return nf("period_format", hint=f"{hint_for(s.freq)}。範囲指定は \"開始..終了\"＝例 \"{RANGE_EXAMPLE.get(s.freq, '')}\"")
        if region not in s.region_codes:
            hint = f"提供地域: {list(s.region_codes)}"
            if s.region_level == "city" and re.fullmatch(r"\d{2}", region or ""):
                # 都道府県コードで市レベルの系列を引いた（第 11 弾 第 3 便・実利用ログ region=13）＝同じ都道府県の市コードを示す
                cands = [c for c in s.region_codes if c.startswith(region)]
                hint = (f"この系列の地域は市の JIS 5 桁（都道府県コード {region} では引けない）。同じ都道府県の候補: {cands}" if cands else hint)
            return nf("region_not_available", hint=hint)
        if not store.has_data(series_id):
            return nf("no_values", hint=f"取込未実装（status={s.status}）。取得元: {s.accessor.get('type')}", status=s.status)
        if is_range:
            ps = store.periods(series_id, region)
            if not ps:
                return nf("not_published", hint=f"region={region} の値は取得元に収録されていません（提供地域: {list(s.region_codes)}）")
            rng = f"{ps[0]}〜{ps[-1]}"
            hits = [p for p in ps if (pp := parse(p)) is not None and lo.sort_key() <= pp.sort_key() <= hi.sort_key()]
            if not hits:
                return nf("out_of_range", hint=f"提供範囲: {rng}（region={region}）")
            if len(hits) > RANGE_LIMIT:
                return nf("range_too_wide", hint=f"該当 {len(hits)} 値 > 上限 {RANGE_LIMIT}。範囲を狭めて複数回に分けてください（提供範囲: {rng}）",
                          available=len(hits), limit=RANGE_LIMIT)
            vals = [store.lookup(series_id, p, region) for p in hits]
            rows, vintages, retrieved, any_proj = [], set(), "", False
            for v in vals:
                k = v.kind or s.kind
                row = {"period": v.period, "value": v.value}
                if v.status:
                    row["status"] = v.status
                if k == "projection":
                    row["kind"] = "projection"; any_proj = True
                rows.append(row)
                vintages.add(v.vintage); retrieved = max(retrieved, v.retrieved_at)
            src = {
                "org": s.org, "org_name": s.org_name, "stat_name": s.stat_name, "table_id": s.table_id,
                "table_title": s.table_title, "basis": s.basis, "url": s.source_url,
                "vintage": "・".join(sorted(v for v in vintages if v)), "retrieved_at": retrieved,
                "license": s.license,
                "citation": (s.citation_template or "{org_name}「{stat_name}」{table_title}（{period}）取得 {retrieved_at}")
                            .format(org_name=s.org_name, stat_name=s.stat_name, table_title=s.table_title, item=s.title,
                                    period=period, retrieved_at=retrieved),
                "note": "出典は系列単位。原典セル位置のアクセサが要るときは単一 period で lookup する。",
            }
            return {"found": True, "series_id": series_id, "title": s.title, "period": period, "region": region,
                    "count": len(rows), "values": rows, "unit": s.unit, "layer": "公開（固定）", "source": src,
                    **({"quality": q} if (q := _quality(s, [r["period"] for r in rows])) else {}),
                    "note": "収録がある期のみ（欠測期は補間せず含まれない）。値は公表どおりの文字列。",
                    **({"note_projection": "kind=projection の値は推計・予測値（観測値ではない）"} if any_proj else {})}
        v = store.lookup(series_id, period, region)
        if v is None:
            ps = store.periods(series_id, region)
            if not ps:
                return nf("not_published", hint=f"region={region} の値は取得元に収録されていません（提供地域: {list(s.region_codes)}）")
            rng = f"{ps[0]}〜{ps[-1]}"
            p_in = parse(period)
            reason = "out_of_range"
            hint = f"提供範囲: {rng}（region={region}）"
            if ps and p_in is not None:
                first, last = parse(ps[0]), parse(ps[-1])
                if first and last and first.sort_key() <= p_in.sort_key() <= last.sort_key():
                    reason = "not_published"  # 範囲内だが公表値がない（欠測・秘匿・未公表）
                elif last and p_in.sort_key() > last.sort_key() and s.kind == "observation":
                    # 提供範囲より先の期＝同じ measure の政府見通し（kind=projection）があれば案内（第 11 弾 第 7 便・実利用ログ FY2026 ×18）
                    proj = registry.get(f"cao.mitoshi.{s.measure}.fy")
                    if proj is not None:
                        hint += f"。当年度・翌年度の政府見通しは {proj.series_id}（kind=projection・閣議決定）"
            return nf(reason, hint=hint)
        src = {
            "org": s.org, "org_name": s.org_name, "stat_name": s.stat_name, "table_id": s.table_id,
            "table_title": s.table_title, "basis": s.basis, "accessor": v.accessor, "url": s.source_url,
            "published_at": v.published_at, "vintage": v.vintage, "retrieved_at": v.retrieved_at,
            "license": s.license,
            "citation": (s.citation_template or "{org_name}「{stat_name}」{table_title}（{period}）取得 {retrieved_at}")
                        .format(org_name=s.org_name, stat_name=s.stat_name, table_title=s.table_title, item=s.title,
                                period=period, retrieved_at=v.retrieved_at),
        }
        kind = v.kind or s.kind
        return {"found": True, "series_id": series_id, "title": s.title, "period": period, "region": region,
                "value": v.value, "unit": s.unit, "kind": kind, "status": v.status, "vintage": v.vintage,
                "layer": "公開（固定）", "source": src,
                **({"quality": q} if (q := _quality(s)) else {}),
                **({"projection_by": s.projection_by or s.org_name, "edition": s.edition or v.vintage, "scenario": s.scenario,
                    "note_projection": "推計・予測値（観測値ではない）"} if kind == "projection" else {})}

    @mcp.tool(title="複数系列×期間を一括参照する（参照層・パネル）", annotations=RO)
    def lookup_panel(period: str, series_ids: Optional[list[str]] = None, id_pattern: Optional[str] = None,
                     dims: Optional[dict] = None, region: str = "JP", verbose: bool = True) -> dict:
        """複数の系列を**1 回で**参照する（参照層のパネル版）。lookup_statistic を系列ごとに繰り返すのと同じ値・同じ fail-closed。

        引数：period＝lookup_statistic と同じ（単一 "FY2024" か範囲 "FY2009..FY2024"）／
        series_ids＝系列 ID の一覧、**または** id_pattern＋dims＝find_statistics の families が返す id_pattern
        （例 "mof.hojin.sales.{industry}-{size}.fy"）に dims（例 {"industry": ["mfg","wholesale"], "size": ["allsize"]}）を埋めて展開
        （dims に無い位置は収録済みの全 slug）。両方を指定すれば和集合／region＝全系列に共通。
        上限＝系列 200・値 2,500（超えると found=false reason=range_too_wide と該当数を返す＝黙って切らない。分割して呼ぶ）。
        返り値：found＝1 系列でも値があれば true／values＝{series_id: {period: value}}（収録がある期のみ＝欠測は補間しない。
        pandas なら DataFrame.from_dict(values) で 期×系列 の表）／cell_flags＝status や kind=projection が付くセルだけ／
        series＝系列ごとの {title, unit, source{ref（＋表と異なるときだけ vintage/retrieved_at/citation）}, quality＝quality_groups のキー, sample（標本の薄さ・系列固有）}／
        sources＝出典の共通部分（表ごとに 1 回・series[].source.ref で参照。citation_template の {item}{period}{retrieved_at} に系列の title・period・retrieved_at を埋めると系列の引用 1 行）／
        quality_groups＝同じ品質メタ（usable_from・breaks・caution）を持つ系列をまとめた定義（series[].quality で参照）／breaks_catalog＝断層の定義（"期|kind" → 定義）／
        verbose=false＝series を省き units（単位→系列 ID）と quality_of（品質グループ→系列 ID）だけ返す（値・出典・品質サマリは同じ。大きなパネル向け）／
        errors＝値を返せなかった系列の {series_id, reason, hint}（unknown_series・out_of_range 等＝他の系列は返す）／
        **quality_summary**＝パネル全体の判定：caution（会計断層の適用点を範囲内に含む系列）・notice（早期適用等の影響幅のみ重なる系列）・
        usable_from_max（全系列が分類改定を跨がずに使える最も遅い開始期）・breaks_by_period（期→系列数）・sample_caution（標本の薄い系列）＝分析コードは最初にこれだけ見れば弾ける。
        値は公表どおりの文字列（換算・丸め・季調・接続なし）。stats は値を保証しない（原典で確認すること）。層は公開固定。
        """
        rec = {"tool": "lookup_panel", "period": period, "region": region, "source": "mcp",
               "n_ids": len(series_ids or []), "id_pattern": id_pattern, "dims": dims}
        ids: list[str] = list(series_ids or [])
        dim_errors: list[dict] = []
        if id_pattern:
            more, dim_errors = _expand_pattern(id_pattern, dims or {})
            ids += more
        seen: set[str] = set()
        ids = [i for i in ids if not (i in seen or seen.add(i))]
        if not ids:
            # 展開 0 件でも診断（dim_errors・収録済みの組合せ）を捨てない（S-3・2026-08-22：利用者が land／fixed_assets で 2 回踏んだ）
            _capture({**rec, "found": False, "reason": "no_series", "n_errors": len(dim_errors)})
            res = {"found": False, "reason": "no_series", "period": period, "region": region, "errors": dim_errors}
            if not (series_ids or id_pattern):
                res["hint"] = "series_ids か id_pattern（＋dims）を指定してください。id_pattern は find_statistics の families が返します。"
            elif id_pattern:
                cov = _pattern_coverage(id_pattern, dims or {})
                if cov:
                    res["coverage"] = cov
                    res["hint"] = ("id_pattern の measure は収録済みだが、指定した dims の組合せに系列が無い。"
                                   "収録済みの組合せは coverage（ここに無い組合せは未収録＝近似しない）。errors に無い slug の一覧。")
                else:
                    res["hint"] = ("id_pattern に合致する登録系列が無い（measure 名か ID の形を確認）。"
                                   "収録済みの measure は list_datasets の measures、id_pattern は find_statistics の families。")
            return res
        if len(ids) > PANEL_SERIES_LIMIT:
            _capture({**rec, "found": False, "reason": "range_too_wide", "n_series": len(ids)})
            return {"found": False, "reason": "range_too_wide", "period": period, "region": region, "requested_series": len(ids),
                    "limit_series": PANEL_SERIES_LIMIT, "hint": "系列数が上限を超えています。dims を絞るか分割して呼んでください。"}
        values: dict[str, dict] = {}       # {series_id: {period: value}}（pandas の DataFrame.from_dict にそのまま渡せる）
        cell_flags: dict[str, dict] = {}   # status／kind=projection が付くセルだけ {series_id: {period: {...}}}
        n_values = 0
        meta: dict[str, dict] = {}
        errors: list[dict] = list(dim_errors)
        sources: dict[str, dict] = {}        # 出典の共通部分（表ごとに 1 回）
        breaks_catalog: dict[str, dict] = {}  # 断層の定義（period|kind|label ごとに 1 回）
        for sid in ids:
            r = _lookup_core(sid, period, region)
            if not r.get("found"):
                errors.append({"series_id": sid, "reason": r.get("reason"), **({"hint": r["hint"]} if r.get("hint") else {})})
                continue
            rows = r["values"] if "values" in r else [{"period": r["period"], "value": r["value"],
                                                      **({"status": r["status"]} if r.get("status") else {}),
                                                      **({"kind": "projection"} if r.get("kind") == "projection" else {})}]
            values[sid] = {row["period"]: row["value"] for row in rows}
            n_values += len(rows)
            fl = {row["period"]: {k: row[k] for k in ("status", "kind") if k in row} for row in rows if "status" in row or "kind" in row}
            if fl:
                cell_flags[sid] = fl
            src = dict(r["source"])
            skey = str(src.get("table_id") or src.get("url"))
            reg_s = registry.get(sid)
            tmpl = (reg_s.citation_template if reg_s and reg_s.citation_template else "{org_name}「{stat_name}」{table_title}（{period}）取得 {retrieved_at}")
            tmpl = tmpl.replace("{org_name}", src.get("org_name", "")).replace("{stat_name}", src.get("stat_name", "")).replace("{table_title}", src.get("table_title", ""))
            sources.setdefault(skey, {k: v for k, v in src.items() if k not in ("citation", "vintage", "retrieved_at", "accessor")})
            sources[skey].setdefault("citation_template", tmpl)
            # 系列側は表と異なる要素だけ持つ（A-4・2026-08-22：citation の全文反復が応答の 15% を占めていた）
            per = {"ref": skey, "vintage": src.get("vintage"), "retrieved_at": src.get("retrieved_at")}
            if sources[skey]["citation_template"] != tmpl:
                per["citation"] = src.get("citation")
            m = {"title": r["title"], "unit": r["unit"], "source": per}
            if r.get("quality"):
                q = dict(r["quality"])
                refs = []
                for b in q.get("breaks", []):
                    bk = f"{b['period']}|{b['kind']}"
                    breaks_catalog.setdefault(bk, b)
                    refs.append(bk)
                q["breaks"] = refs  # 定義は breaks_catalog に 1 回
                for flag in ("caution", "notice"):
                    if flag in q:
                        q[flag] = True  # 本文は quality_summary に 1 回
                if "sample" in q:
                    q["sample"] = {k: v for k, v in q["sample"].items() if k != "note"}  # 本文は quality_summary に 1 回
                m["quality"] = q
            meta[sid] = m
        if n_values > PANEL_LIMIT:
            _capture({**rec, "found": False, "reason": "range_too_wide", "n_series": len(ids), "available": n_values})
            return {"found": False, "reason": "range_too_wide", "period": period, "region": region, "available": n_values,
                    "limit": PANEL_LIMIT, "series_count": len(ids),
                    "hint": f"該当 {n_values} 値 > 上限 {PANEL_LIMIT}。期間か系列を分割して複数回に分けてください（黙って切り捨てません）。"}
        # 出典の vintage/retrieved_at は表内で一様なら sources へ（系列側から消す）
        for skey in sources:
            members = [m for m in meta.values() if m["source"]["ref"] == skey]
            for k in ("vintage", "retrieved_at"):
                vals = {m["source"].get(k) for m in members}
                if len(vals) == 1:
                    sources[skey][k] = vals.pop()
                    for m in members:
                        m["source"].pop(k, None)
        # 品質メタは署名でグループ化（同じ usable_from・breaks・caution の系列は 1 定義）。sample は系列固有なので series 側に残す
        quality_groups: dict[str, dict] = {}
        sig_to_gid: dict[str, str] = {}
        for sid, m in meta.items():
            q = m.pop("quality", None)
            if not q:
                continue
            sample = q.pop("sample", None)
            if sample:
                m["sample"] = sample
            sig = json.dumps(q, ensure_ascii=False, sort_keys=True)
            gid = sig_to_gid.get(sig)
            if gid is None:
                gid = f"q{len(quality_groups) + 1}"
                sig_to_gid[sig] = gid
                quality_groups[gid] = q
            m["quality"] = gid
        # パネル全体の品質サマリ
        def _q(m: dict) -> dict:
            return quality_groups.get(m.get("quality", ""), {})
        caution = [sid for sid, m in meta.items() if _q(m).get("caution")]
        notice = [sid for sid, m in meta.items() if _q(m).get("notice")]
        by_period: dict[str, int] = {}
        for m in meta.values():
            for pp in _q(m).get("breaks_in_range", []):
                by_period[pp] = by_period.get(pp, 0) + 1
        usable = [_q(m)["usable_from"] for m in meta.values() if _q(m).get("usable_from")]
        sample_caution = [sid for sid, m in meta.items() if m.get("sample")]
        summary = {"series_with_values": len(meta), "series_with_errors": len(errors),
                   "caution": caution, "notice": notice, "breaks_by_period": dict(sorted(by_period.items())),
                   "sample_caution": sample_caution,
                   **({"sample_note": "sample_caution の系列は標本が薄い（volatile＝散らばりが上位集計の 2 倍以上／small_cell＝母集団<100 社）＝単年の値で語らず数年平均か上位集計で見る"} if sample_caution else {}),
                   **({"usable_from_max": max(usable)} if usable else {})}
        if caution:
            by_measure: dict[str, int] = {}
            for sid in caution:
                ms = registry.get(sid)
                by_measure[ms.measure if ms else "?"] = by_measure.get(ms.measure if ms else "?", 0) + 1
            summary["caution_by_measure"] = by_measure
            inv = next((b.get("invariant") for b in breaks_catalog.values() if b.get("kind") == "accounting" and b.get("invariant")), "")
            summary["caution_note"] = (f"{len(caution)} 系列が会計基準の断層（kind=accounting）の適用点を範囲内に含む。"
                                       "壊れるのは**売上高を分母にする比率**（粗利率・原価率・売上高営業利益率・付加価値率）と売上高/売上原価/販管費の水準の前後比較。"
                                       + (f"不変＝{inv}。" if inv else "") + "比率を使うなら breaks_by_period の期で窓を切る")
        elif notice:
            summary["notice_note"] = f"{len(notice)} 系列が会計断層の影響幅（早期適用等）と重なる＝規模間・業種間の比率比較では脚注を付ける"
        found = n_values > 0
        log.info("参照層(パネル): %d 系列 period=%s region=%s → %d 値・エラー %d", len(ids), period, region, n_values, len(errors))
        _capture({**rec, "found": found, "n_series": len(ids), "range_count": n_values, "n_errors": len(errors)})
        if verbose:
            body = {"series": meta}
        else:
            units: dict[str, list[str]] = {}
            q_of: dict[str, list[str]] = {}
            for sid, m in meta.items():
                units.setdefault(m["unit"], []).append(sid)
                if m.get("quality"):
                    q_of.setdefault(m["quality"], []).append(sid)
            body = {"units": units, "quality_of": q_of,
                    "verbose_note": "verbose=false：series（title・出典差分・sample の数値）は省略。必要なら verbose=true で同じ引数を呼ぶ"}
        return {"found": found, "period": period, "region": region, "series_count": len(ids), "count": n_values,
                "values": values, **({"cell_flags": cell_flags} if cell_flags else {}),
                **body, "sources": sources, "quality_groups": quality_groups, "breaks_catalog": breaks_catalog,
                "errors": errors, "quality_summary": summary, "layer": "公開（固定）",
                "note": "収録がある期のみ（欠測期は補間せず含まれない）。値は公表どおりの文字列。出典は系列単位（セル位置は単一 period の lookup_statistic）。",
                **({"reason": "no_values_in_panel", "hint": "全系列が errors（reason を参照）"} if not found else {})}

    def _pattern_coverage(pattern: str, dims: dict) -> list[dict]:
        """id_pattern の {industry}/{size} を除いた measure 部分が収録済みなら、その収録済み組合せ（Registry.coverage_of）を返す。"""
        import re
        fixed = re.sub(r"\{[a-z_]+\}", "X", pattern)
        parts = fixed.split(".")
        if len(parts) < 5:
            return []
        out = []
        measures = dims.get("measure") if "{measure}" in pattern else [parts[2]]
        for meas in measures or []:
            cov = registry.coverage_of(parts[0], parts[1], meas, parts[-1])
            if cov:
                out.append(cov)
        return out

    def _expand_pattern(pattern: str, dims: dict) -> tuple[list[str], list[dict]]:
        """id_pattern の {industry}/{size}/{measure} 等（任意の {名前}）を dims の slug で展開し、**登録済みの系列 ID だけ**返す
        （無い組合せは作らない）。dims に無い位置は登録系列に合致する全 slug。返り値＝(ids, dims の誤 slug の一覧)。"""
        import re
        keys = re.findall(r"\{([a-z_]+)\}", pattern)
        if not keys:
            return [pattern], []
        rx_src = "^" + re.escape(pattern) + "$"
        for k in keys:
            # industry 等の先頭トークンは `-` を含まない（業種スラグは `_` 区切り）。size は cap100m-1b のように `-` を含む
            cls = r"[a-z0-9_]" if k in ("industry", "measure", "dataset", "org") else r"[a-z0-9_\-]"
            rx_src = rx_src.replace(re.escape("{" + k + "}"), rf"(?P<{k}>{cls}+)")
        rx = re.compile(rx_src)
        out, seen_slugs = [], {k: set() for k in keys}
        for sid in registry.series:
            m = rx.match(sid)
            if not m:
                continue
            for k in keys:
                seen_slugs[k].add(m.group(k))
            if all((not dims.get(k)) or m.group(k) in set(dims[k]) for k in keys):
                out.append(sid)
        bad = [{"dim": k, "slug": v, "reason": "unknown_series", "hint": f"この id_pattern で {k}={v!r} の登録系列は無い（この measure の収録済み組合せは coverage／find_statistics の families）"}
               for k in keys for v in (dims.get(k) or []) if v not in seen_slugs[k]]
        return sorted(out), bad

    @mcp.tool(title="統計の目録（発見層の入口）", annotations=RO)
    def list_datasets(sector: Optional[str] = None, org: Optional[str] = None, scope: Optional[str] = None) -> dict:
        """利用できる統計の**目録**（発見層の入口）。「どんなデータがあるか」と聞かれたら**まずこれ**を呼ぶ。

        系列を dataset（統計表の単位：法人企業統計・CPI・SNA 年次推計・IMF WEO・地方財政計画 …）ごとに集約して返す：
        統計名・調査主体・部門（sector）・系列数（値あり／派生／planned）・粒度・提供期間（値ストアの実範囲 min〜max）・地域粒度・単位・基準・
        分野タグ・代表 series_id・measure 一覧・**license**（再配布・商用利用条件：◎＝出典明示で商用可／○＝可だが要再確認／△＝商用は要相談・要確認）。
        引数 sector／org／**scope（jp＝日本の統計／intl＝国際比較・国別）**で絞れる。各行に scope が付く。全体で 30〜40 行＝1 回で全体像が収まる。
        **国際比較を求められていないときは scope=jp で引く**（国際系列＝IMF/OECD/世銀は国別 ISO3 で region が要る・定義も各機関のもの＝日本の国内分析と混ぜない）。
        次の一手：気になる dataset を find_statistics(dataset=…) で掘り、series_id を確定して lookup_statistic で値を引く。
        値は返さない。planned は未収録（取得元は特定済み・値なし・取込時期未定）＝lookup は found=false。層は公開固定。
        """
        rows = []
        for d in registry.catalog():
            if sector and sector not in d["sector"]:
                continue
            if org and d["org"] != org:
                continue
            if scope and d["scope"] != scope:
                continue
            firsts, lasts = [], []
            for x in registry.series.values():
                if x.org != d["org"] or x.dataset != d["dataset"] or not store.has_data(x.series_id):
                    continue
                ps = store.periods(x.series_id, x.region_codes[0] if x.region_codes else "JP")
                if ps:
                    firsts.append(ps[0]); lasts.append(ps[-1])
            d["period_range"] = {"first": min(firsts) if firsts else "", "last": max(lasts) if lasts else "",
                                 "note": "値ストアの実範囲（系列により異なる。粒度が混在する dataset は表記が混ざる）"}
            rows.append(d)
        _capture({"tool": "list_datasets", "sector": sector, "org": org, "scope": scope, "result_count": len(rows), "source": "mcp"})
        return {"count": len(rows), "datasets": rows,
                "sectors": list(SECTORS), "scopes": {"jp": "日本の統計（全国・都道府県）", "intl": "国際比較（国別 ISO3・IMF/OECD/世銀）"},
                "note": "目録。値は lookup_statistic（完全一致のみ）。派生（derived）は値を持たず構成系列を返す。"}

    @mcp.tool(title="調査主体コードの一覧", annotations=RO)
    def list_sources() -> dict:
        """登録済みの調査主体コード（org）ごとの系列数・値ありの系列数を返す。"""
        counts: dict[str, dict] = {}
        for s in registry.series.values():
            c = counts.setdefault(s.org, {"code": s.org, "org_name": s.org_name or s.org, "series_count": 0, "with_values": 0})
            c["series_count"] += 1
            if store.has_data(s.series_id):
                c["with_values"] += 1
        return {"sources": sorted(counts.values(), key=lambda x: x["code"]),
                "note": "層は常に公開固定。値の参照は lookup_statistic（完全一致のみ）。"}

    return mcp, log


def main() -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    real_stdout = guard_stdout_for_stdio()
    log = get_logger("polyarchy.stats")
    log.info("stats MCP サーバ起動中: 名称=%s", SERVER_NAME)
    mcp, log = build_server(default_registry())
    log.info("stats MCP サーバ待受開始: %s", TOOLS_DESC)

    async def _serve() -> None:
        async with stdio_server(stdout=anyio.wrap_file(real_stdout)) as (read_stream, write_stream):
            await mcp._mcp_server.run(read_stream, write_stream,
                                      mcp._mcp_server.create_initialization_options())

    anyio.run(_serve)


def _health_check(registry: Registry, store: ValueStore):
    """/healthz 用の軽い検査（運用設計 §1）：レジストリ読込数＋代表 1 系列に値があること。"""
    rep = "cao.sna2020.gdp_nominal.fy"  # 代表系列（本線 dataset・値ストア読込の生存確認）

    def check() -> dict:
        n = len(registry.series)
        values_ok = store.has_data(rep) and bool(store.all_periods(rep))
        return {"ok": n >= 500 and values_ok, "registry_series": n, "values_ok": values_ok, "probe": rep}

    return check


def main_http(host: str = "127.0.0.1", port: int = 8766, path: str = "/mcp") -> None:
    configure_quiet_logging()
    log = get_logger("polyarchy.stats")
    log.info("stats MCP サーバ起動中(HTTP)")
    registry = default_registry()
    store = ValueStore()
    mcp, log = build_server(registry, store)
    serve_streamable_http(mcp, host=host, port=port, path=path, tools_desc=TOOLS_DESC,
                          logger_name="polyarchy.stats", health_check=_health_check(registry, store))


if __name__ == "__main__":
    import argparse

    _p = argparse.ArgumentParser(description="Polyarchy stats MCP サーバ（既定=stdio・--http で Streamable HTTP）")
    _p.add_argument("--http", action="store_true")
    _p.add_argument("--host", default=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"))
    _p.add_argument("--port", type=int, default=int(os.environ.get("MCP_HTTP_PORT", "8766")))
    _p.add_argument("--path", default=os.environ.get("MCP_HTTP_PATH", "/mcp"))
    _a = _p.parse_args()
    if _a.http:
        main_http(_a.host, _a.port, _a.path)
    else:
        main()
