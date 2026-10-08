"""
freshness＝取得元に新しいデータが出たかの**存否確認**（読み取りのみ・値は取り込まない）。

設計＝docs/運用設計.md §2.2。公開ページの「値は月次で更新」を守るための一次情報を出す。
- ファイル系（ESRI・日銀・社人研・財務省・総務省 等）＝HTTP HEAD の `Last-Modified`/`ETag` を
  前回値（stats/data/cache/freshness.json・git 外）と比較＝「原本が更新された可能性」を検知。
- API 系＝e-Stat：getMetaInfo の TIME 最大コードを収録 last_period と比較（要 ESTAT_APP_ID）。
  IMF DataMapper／世銀：最新 period を取得して比較。OECD：lastNObservations=1 で最新 period。
- 判定は保守的：分からないものは changed=None で正直に返す。行の status＝ok／error（probe 失敗・解析失敗・
  unsupported）／config_missing（e-Stat の appId なし）／excluded（EXCLUDED_TYPES＝理由つきで自動確認の対象外）。
  失敗の数え方は status で行う（note の文言で判定しない＝2026-10-09）。
- changed は「前回の確認からの差分」＝日次で回すと翌日には消える。取り込まれずに溜まっている分は **pending**
  （取込待ち）で見る＝変化を検知した日（pending_since）を状態に残し、その日以降に取り込まれる（その dataset の値の
  retrieved_at が追いつく）まで消さない。収録と取得元を直接比べる型（QE・見通し・総務省の次版・国際機関の最新年）は
  今の比較がそのまま pending（2026-10-06・運用側からの依頼）。

実行（リポジトリ root・月次目安）：
    python -m stats.ops.freshness            # 日本語サマリ（--json で機械可読）
    python -m stats.ops.freshness --only 消費者物価指数
終了コード：0=変化なし／1=更新あり（取込待ちを含む）／2=probe 失敗あり（更新ありが混在すれば 1 を優先）。
次の一手＝差分更新（設計 §4b）：該当 `python -m stats.ingest.<module> --all` → ゲート 3 本 → コミット。
"""
import argparse
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.paths import CACHE_DIR
from stats.core.registry import Registry, default_registry
from stats.core.values import ValueStore
from stats.ingest._base import USER_AGENT as UA

configure_quiet_logging()
log = get_logger("polyarchy.stats.ops")

STATE_PATH = CACHE_DIR / "freshness.json"
TIMEOUT = 60
# 収録と取得元を直接比べる型（状態ファイルの差分ではない）＝pending は今の比較だけで決める
STATELESS_KINDS = ("qe", "mitoshi", "soumu_next", "imf", "wb", "oecd")
# 自動の更新確認・再取込（stats.ops.refresh）から理由つきで外す accessor.type。ここに無い registered の type は、
# refresh.TYPE_TO_MODULE と build_probes の両方に配線する（test_core が網羅を検査する＝取得元を足して配線を忘れると落ちる）。
EXCLUDED_TYPES = {
    "pdf_hakusho_sme": "中小企業白書の PDF は私用領域の文字の写像が年版で変わり得る＝年版ごとに手で確かめて取り込む",
}


def _fail(row: dict, note: str, status: str = "error") -> None:
    """probe の失敗を行に刻む（status で数える＝note の文言は表示用）。"""
    row["note"] = note
    row["status"] = status


def mitoshi_changed(doc: str, held, info: dict) -> bool:
    """政府経済見通し：dataset が持つ版（doc＝mitoshi〔閣議決定〕／shisan〔年央試算〕）だけを取得元の最新と比べる。
    収録 edition の先頭（YYYY-MM-DD）が最新に一致しなければ更新あり。"""
    return not any(str(e).startswith(info[doc]["edition"]) for e in held)


def carry_pending(since, changed, ingested_at: str, today: str, stateless: bool = False):
    """取込待ちの開始日を返す（無ければ None）。since＝前回までの開始日・ingested_at＝その dataset の値の retrieved_at の最大。
    検知の日以降に取り込まれていれば消す（同じ日の取込も取り込み済みとみなす）。"""
    if stateless and changed is not None:  # probe 失敗（None）の日は前回の状態を持ち越す
        since = (since or today) if changed else None
    elif changed:
        since = since or today
    if since and ingested_at and ingested_at[:10] >= since:
        return None
    return since


# ── 低レベル probe ──────────────────────────────────────────────────────────

def head(url: str) -> dict:
    """HEAD（不可なら Range 付き GET）で Last-Modified/ETag を取る。失敗は {'error': ...}。"""
    for method, rng in (("HEAD", None), ("GET", "bytes=0-0")):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **({"Range": rng} if rng else {})})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return {"last_modified": r.headers.get("Last-Modified", ""), "etag": r.headers.get("ETag", ""),
                        "content_length": r.headers.get("Content-Length", "") if method == "HEAD" else "",
                        "status": r.status}
        except Exception as e:  # noqa: BLE001
            err = str(e)
    return {"error": err}


def _jst_date(http_date: str) -> str:
    """Last-Modified（HTTP 日付）→ JST の日付（値の retrieved_at と同じ物差し）。読めなければ空。"""
    from email.utils import parsedate_to_datetime
    from zoneinfo import ZoneInfo
    try:
        return parsedate_to_datetime(http_date).astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


def get_json(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("probe 失敗: %s ＝ %s", url, e)
        return None


# ── probe の組み立て（dataset → 確認する対象）───────────────────────────────
# 方針：確認対象の URL は各 ingest モジュールの定数から組む（複製しない）。

def build_probes(registry: Registry) -> dict:
    """{dataset_key: {"kind": "head"|"estat"|"imf"|"wb"|"oecd"|"soumu_next", ...}} を系列から導出。"""
    from stats.ingest import boj_flat, esri, mof, mof_zaisei, soumu_hakusho
    probes: dict = {}
    for s in registry.series.values():
        if s.is_derived or s.status in ("planned",):
            continue
        key = f"{s.org}:{s.dataset}"
        p = probes.setdefault(key, {"dataset": s.dataset, "org": s.org, "urls": set(), "kind": None,
                                    "series": 0, "type": s.accessor.get("type", "")})
        p["series"] += 1
        t = s.accessor.get("type", "")
        a = s.accessor
        if t == "esri_qe_csv":
            # QE は公表回ごとに別 URL＝固定 URL の HEAD では新公表を検知できない（第 9 弾 段 3）。
            # 年次索引から最新公表回を発見して収録 edition と比較する（適用は stats.ops.qe_update）。
            p["kind"] = "qe"; p["edition"] = str(a.get("edition", ""))
        elif t in ("esri_xlsx", "esri_xls", "esri_xlsx_yearsheets"):
            f = str(a.get("file", ""))
            if "{Y}" in f:
                f = f.replace("{Y}", str(a.get("edition", "")))
            p["kind"] = "head"; p["urls"].add(esri.BASE + f)
        elif t == "cao_gap_xlsx":
            # 内閣府 GDPギャップ＝ファイル名が QE 公表回に連動して変わる＝索引から発見した最新ファイル名を signal に（第 9 弾 段 4）
            p["kind"] = "cao_gap"
        elif t in ("boj_file", "ipss_xlsx"):
            p["kind"] = "head"; p["urls"].add(str(a.get("url", "")))
        elif t == "boj_mtshtml":
            from stats.ingest.boj_mtshtml import BASE as MTS_BASE
            p["kind"] = "head"; p["urls"].add(MTS_BASE + str(a.get("page", "")))
        elif t == "bls_api":
            # BLS API v1＝当年 1 リクエストで最新期を見る（日次 25 リクエスト枠を消費＝dataset 単位で 1 回）
            p["kind"] = "bls"; p.setdefault("codes", set()).add(str(a.get("series", "")))
        elif t == "eurostat_api":
            # dataset ごとに公表の鮮度が違う（HICP は止まり・失業率は現行＝2026-08-29 実測）＝全 dataset を見る。
            # クエリは ingest と同じ組み立て（eurostat.API＋seed の params・geo の先頭 1 国）＝URL を複製しない
            p["kind"] = "eurostat"
            p.setdefault("queries", {})[str(a.get("dataset", ""))] = (dict(a.get("params") or {}), (a.get("geo") or ["DE"])[0])
        elif t == "boj_flat":
            p["kind"] = "head"; p["urls"].add(boj_flat.BASE + str(a.get("zip", "")))
        elif t == "mof_csv":
            p["kind"] = "head"; p["urls"].add(mof.JGB_CUR)
        elif t == "mof_zaisei":
            p["kind"] = "head"; p["urls"].add(mof_zaisei.BASE + str(a.get("file", "")))
        elif t == "mof_zaimu_xlsx":
            # 国の財務書類（第 12 弾 第 3 便）＝新しい年版は索引ページに頁が足される＝索引の変化を signal に（年版の追加は seed の ZAIMU_EDITIONS）
            p["kind"] = "head"; p["urls"].add(str(s.source_url))
        elif t == "soumu_tokitsu_xlsx":
            # 統一的な基準による財務書類（第 12 弾 第 4 便）＝新しい年版は索引に頁が足される（年版→ファイル番号は ingest の FILES）
            p["kind"] = "head"; p["urls"].add(str(s.source_url))
        elif t == "estat_catalog_xlsx":
            # e-Stat カタログ経由（一般職業紹介状況）＝最新データセットの SURVEY_DATE／RELEASE_DATE が signal（第 11 弾 第 6 便）
            p["kind"] = "estat_catalog"; p["catalog"] = (str(a.get("catalog_word", "")), str(a.get("title_prefix", "")), int(a.get("table_no", 1)))
        elif t == "cao_mitoshi_pdf":
            # 政府経済見通し＝索引ページの最新 PDF の日付（edition）と収録版を比較（第 11 弾 第 7 便）
            # dataset ごとに 1 つの版＝mitoshi は閣議決定（doc=mitoshi）・mitoshi_mid は年央試算（doc=shisan）
            p["kind"] = "mitoshi"; p["doc"] = str(a.get("doc", "")); p.setdefault("editions", set()).add(str(s.edition))
        elif t == "maikin_csv":
            # e-Stat file-download は Last-Modified/ETag を返さない＝Content-Length を signal に（head() の第 3 候補）
            from stats.ingest.maikin import url_of as _maikin_url
            p["kind"] = "head"; p["urls"].add(_maikin_url(str(a.get("statInfId", ""))))
        elif t == "cao_infra_xlsx":
            # 社会資本ストック推計（第 12 弾 第 6 便）＝Excel の更新（Last-Modified）を signal に
            from stats.ingest.cao_infra import BASE as _INFRA_BASE
            p["kind"] = "head"; p["urls"].add(_INFRA_BASE + str(a.get("file", "")))
        elif t == "pdf_chart":
            # 財投（第 12 弾・guide）＝PDF のグラフ＝PDF 自体の更新を signal に（値は持たないので更新時は guide の年度範囲の記述を見直す）
            p["kind"] = "head"; p["urls"].add(str(s.source_url))
        elif t == "pdf_table":
            p["kind"] = "head"
            g = a.get("guide", {}) or {}
            u = g.get("url") or s.source_url
            if u:
                p["urls"].add(u)
        elif t == "estat":
            p["kind"] = "estat"
            ids = {str(a.get("statsDataId", ""))} | {str(x.get("statsDataId", "")) for x in (a.get("parts") or [])}
            p.setdefault("statsDataIds", set()).update(x for x in ids if x)
        elif t == "imf_dm":
            p["kind"] = "imf"; p.setdefault("indicators", set()).add(str(a.get("indicator", "")))
        elif t == "wb_api":
            p["kind"] = "wb"; p.setdefault("indicators", set()).add(str(a.get("indicator", "")))
        elif t == "oecd_sdmx":
            p["kind"] = "oecd"; p.setdefault("flows", set()).add(str(a.get("dataflow", "")))
        elif t == "soumu_hakusho":
            p["kind"] = "soumu_next"
        elif t in EXCLUDED_TYPES:
            p["kind"] = p["kind"] or "excluded"; p["excluded_reason"] = EXCLUDED_TYPES[t]
        else:
            p["kind"] = p["kind"] or "unsupported"
    return probes


def stored_last_period(registry: Registry, store: ValueStore, org: str, dataset: str) -> tuple[str, str]:
    """dataset の収録済み最終 period（系列の実データ最大値・粒度混在は文字列 max の目安）と、値の retrieved_at の最大（＝最後に取り込んだ日）。"""
    last, ingested = "", ""
    for s in registry.series.values():
        if s.org != org or s.dataset != dataset or not store.has_data(s.series_id):
            continue
        ps = store.all_periods(s.series_id)
        if ps:
            last = max(last, ps[-1])
        ingested = max(ingested, store.last_retrieved(s.series_id))
    return last, ingested


# ── 各 probe の実行 ──────────────────────────────────────────────────────────

def run_probe(key: str, p: dict, state: dict) -> dict:
    row = {"dataset": p["dataset"], "org": p["org"], "series": p["series"], "type": p["type"],
           "changed": None, "signal": "", "note": "", "status": "ok"}
    prev = state.get(key, {})
    if p["kind"] == "head":
        sigs, errs = [], []
        for u in sorted(p["urls"]):
            h = head(u)
            if "error" in h:
                errs.append(f"{u} ＝ {h['error']}")
            else:
                if (d := _jst_date(h["last_modified"])):
                    row["source_date"] = max(row.get("source_date", ""), d)
                sigs.append(f"{h['last_modified'] or h['etag'] or (('len=' + h['content_length']) if h.get('content_length') else '')}")
        row["signal"] = " / ".join(sorted(set(sigs)))
        if errs:
            _fail(row, "probe 失敗: " + errs[0])
        if sigs:
            row["changed"] = (prev.get("signal") != row["signal"]) if prev.get("signal") else None
            state[key] = {"signal": row["signal"], "checked_at": datetime.now().isoformat(timespec="seconds")}
            if row["changed"] is None:
                row["note"] = (row["note"] + " " if row["note"] else "") + "初回記録（次回から差分判定）"
    elif p["kind"] == "qe":
        # QE＝年次索引から最新公表回を発見して収録 edition と比較（第 9 弾 段 3・状態ファイル不要＝比較対象が registry にある）
        from stats.ops import qe_update
        try:
            latest = qe_update.discover()
        except Exception as e:  # noqa: BLE001
            latest = None
            _fail(row, f"probe 失敗: {e}")
        cur = p.get("edition", "")
        if latest is None:
            _fail(row, row["note"] or "probe 失敗: 年次索引（toukei_{年}.html）を取得・解析できない")
        else:
            row["changed"] = latest["edition"] != cur
            row["signal"] = f"最新 {latest['edition']}（{latest['label']}）／収録 {cur}"
            if row["changed"]:
                row["note"] = "適用は python -m stats.ops.qe_update --apply（refresh からは自動適用）"
    elif p["kind"] == "bls":
        # 当年ウィンドウ 1 リクエストで最新期（BLS API v1・キー不要）。state 比較で差分判定。
        import json as _json
        import urllib.request as _rq
        try:
            code = sorted(p.get("codes", {""}))[0]
            req = _rq.Request("https://api.bls.gov/publicAPI/v1/timeseries/data/", method="POST",
                              data=_json.dumps({"seriesid": [code], "startyear": datetime.now().strftime("%Y"),
                                                "endyear": datetime.now().strftime("%Y")}).encode(),
                              headers={"Content-Type": "application/json", "User-Agent": UA})
            with _rq.urlopen(req, timeout=TIMEOUT) as r:
                d = _json.loads(r.read())
            data = d.get("Results", {}).get("series", [{}])[0].get("data", [])
            latest = max((f"{x['year']}-{x['period'][1:]}" for x in data if x.get("period", "").startswith("M")), default="")
            row["signal"] = f"最新 {latest}（{code}）"
            row["changed"] = (prev.get("signal") != row["signal"]) if prev.get("signal") else None
            state[key] = {"signal": row["signal"], "checked_at": datetime.now().isoformat(timespec="seconds")}
            if row["changed"] is None:
                row["note"] = "初回記録（次回から差分判定）"
        except Exception as e:  # noqa: BLE001
            _fail(row, f"probe 失敗: {e}")
    elif p["kind"] == "eurostat":
        import urllib.parse as _up

        from stats.ingest.eurostat import API as EUROSTAT_API
        try:
            sigs = []
            for ds, (params, geo) in sorted(p.get("queries", {}).items()):
                q = [("format", "JSON"), ("lang", "en"), ("lastTimePeriod", "1"), ("geo", geo)] + sorted((k, str(v)) for k, v in params.items())
                d = get_json(EUROSTAT_API + ds + "?" + _up.urlencode(q))
                latest = max(d["dimension"]["time"]["category"]["index"].keys()) if d else ""
                sigs.append(f"最新 {latest}（{ds}）")
            row["signal"] = " / ".join(sigs)
            row["changed"] = (prev.get("signal") != row["signal"]) if prev.get("signal") else None
            state[key] = {"signal": row["signal"], "checked_at": datetime.now().isoformat(timespec="seconds")}
            if row["changed"] is None:
                row["note"] = "初回記録（次回から差分判定）"
        except Exception as e:  # noqa: BLE001
            _fail(row, f"probe 失敗: {e}")
    elif p["kind"] == "estat_catalog":
        from stats.ingest.shokugyo import discover as _sk_discover
        try:
            cw, tp, tn = p["catalog"]
            info = _sk_discover(cw, tp, tn)
            row["signal"] = f"最新 {info['title']}（{info['statInfId']}・{info['release_date']}）"
            row["changed"] = (prev.get("signal") != row["signal"]) if prev.get("signal") else None
            state[key] = {"signal": row["signal"], "checked_at": datetime.now().isoformat(timespec="seconds")}
            if row["changed"] is None:
                row["note"] = "初回記録（次回から差分判定）"
        except Exception as e:  # noqa: BLE001
            _fail(row, f"probe 失敗: {e}")
    elif p["kind"] == "mitoshi":
        from stats.ingest.cao_mitoshi import discover as _mt_discover
        try:
            info = _mt_discover()
            doc = p.get("doc", "")
            held = sorted(p.get("editions", set()))
            row["signal"] = f"最新 {'閣議決定' if doc == 'mitoshi' else '年央試算'} {info[doc]['edition']}／収録 {held}"
            # 比較対象が registry にある（QE と同型・状態ファイル不要）。dataset が持つ版だけと比べる（両方を要求すると常に更新あり＝2026-10-05 の誤検知）
            row["changed"] = mitoshi_changed(doc, held, info)
            if row["changed"]:
                row["note"] = "seed_registry の MITOSHI_EDITION／SHISAN_EDITION を更新して再取込（版が変わると first_period も動く）"
        except Exception as e:  # noqa: BLE001
            _fail(row, f"probe 失敗: {e}")
    elif p["kind"] == "cao_gap":
        from stats.ingest.cao_gap import discover_file
        from stats.ingest._base import today as _today
        try:
            fname, edition = discover_file(_today())
            row["signal"] = f"最新 {fname}"
            row["changed"] = (prev.get("signal") != row["signal"]) if prev.get("signal") else None
            state[key] = {"signal": row["signal"], "checked_at": datetime.now().isoformat(timespec="seconds")}
            if row["changed"] is None:
                row["note"] = "初回記録（次回から差分判定）"
        except Exception as e:  # noqa: BLE001
            _fail(row, f"probe 失敗: {e}")
    elif p["kind"] == "estat":
        from stats.ingest.estat import BASE, load_app_id
        try:
            app_id = load_app_id()
        except Exception as e:  # noqa: BLE001
            _fail(row, f"ESTAT_APP_ID なし（{e}）", status="config_missing")
            return row
        latest = ""
        for sid in sorted(p.get("statsDataIds", set())):
            d = get_json(f"{BASE}getMetaInfo?appId={app_id}&statsDataId={sid}")
            try:
                objs = d["GET_META_INFO"]["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"]
                tobj = next(o for o in objs if o.get("@id") == "time")
                cls = tobj["CLASS"] if isinstance(tobj["CLASS"], list) else [tobj["CLASS"]]
                latest = max(latest, max(str(c.get("@code", "")) for c in cls))
            except Exception:  # noqa: BLE001
                _fail(row, f"getMetaInfo 解析失敗（statsDataId={sid}）")
        row["signal"] = f"最新 time コード {latest}" if latest else ""
        if latest:
            row["changed"] = prev.get("signal") != row["signal"] if prev.get("signal") else None
            state[key] = {"signal": row["signal"], "checked_at": datetime.now().isoformat(timespec="seconds")}
            if row["changed"] is None:
                row["note"] = "初回記録（次回から差分判定）"
    elif p["kind"] == "imf":
        from stats.ingest.intl import IMF_BASE
        years = []
        for ind in sorted(p.get("indicators", set())):
            d = get_json(IMF_BASE + ind)
            try:
                jp = d["values"][ind]["JPN"]
                years.append(max(jp.keys()))
            except Exception:  # noqa: BLE001
                _fail(row, f"IMF 解析失敗（{ind}）")
        if years:
            row["signal"] = f"最新 {max(years)}"  # changed 判定は main の「収録との比較」で行う
    elif p["kind"] == "wb":
        from stats.ingest.intl import WB_BASE
        for ind in sorted(p.get("indicators", set()))[:1]:
            d = get_json(WB_BASE.format(iso3="JPN", ind=ind).replace("per_page=200", "per_page=1") + "&mrnev=1")
            try:
                row["signal"] = f"最新 {d[1][0]['date']}"
            except Exception:  # noqa: BLE001
                _fail(row, f"世銀 解析失敗（{ind}）")
    elif p["kind"] == "oecd":
        from stats.ingest.intl import OECD_BASE
        import csv as _csv, io as _io
        year = datetime.now().year
        for flow in sorted(p.get("flows", set()))[:1]:
            url = f"{OECD_BASE}{flow}/all?startPeriod={year - 2}&format=csvfilewithlabels"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=TIMEOUT * 3) as r:
                    rows_ = list(_csv.DictReader(_io.StringIO(r.read().decode("utf-8"))))
                periods = [x.get("TIME_PERIOD", "") for x in rows_ if x.get("REF_AREA") == "JPN"] or                           [x.get("TIME_PERIOD", "") for x in rows_]
                row["signal"] = f"最新 {max(periods)}" if periods else ""
            except Exception as e:  # noqa: BLE001
                _fail(row, f"OECD 解析失敗（{e}）")
    elif p["kind"] == "soumu_next":
        from stats.ingest.soumu_hakusho import BASE, EDITIONS
        prefix, year = EDITIONS[0]
        nxt = (f"r{int(prefix[1:]) + 1:02d}", year + 1)
        h = head(f"{BASE}{nxt[0]}data/{nxt[1]}data/{nxt[0]}czs00-00.html")
        if "error" in h:
            row["signal"] = f"次版 {nxt[0]}（{nxt[1]}）未公開"
            row["changed"] = False
        else:
            row["signal"] = f"次版 {nxt[0]}（{nxt[1]}）公開あり"
            row["changed"] = True
    elif p["kind"] == "excluded":
        row["note"] = f"自動確認の対象外（{p.get('excluded_reason', '')}）"
        row["status"] = "excluded"
    else:
        _fail(row, "unsupported（手動確認）")
    return row


stored_last_period_cache: dict = {}


def check(only: str = "") -> list[dict]:
    """全 dataset を probe して rows を返す（state 保存込み）。`stats.ops.refresh` からも使う。"""
    registry = default_registry()
    store = ValueStore()
    probes = build_probes(registry)
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except Exception:  # noqa: BLE001
            state = {}

    rows = []
    today = datetime.now().date().isoformat()
    for key, p in sorted(probes.items()):
        if only and only not in p["dataset"]:
            continue
        stored_last_period_cache[(p["org"], p["dataset"])], ingested = stored_last_period(registry, store, p["org"], p["dataset"])
        since = state.get(key, {}).get("pending_since")
        row = run_probe(key, p, state)
        row["stored_last"] = stored_last_period_cache[(p["org"], p["dataset"])]
        # API 系で「取得元の最新 year > 収録の最新 year」なら更新あり（年の文字列比較で足りる範囲のみ・IMF/世銀/OECD）
        if row["changed"] is None and row["signal"].startswith("最新 ") and row["stored_last"]:
            src_year = row["signal"][3:].strip()[:4]
            stored_year = row["stored_last"].removeprefix("FY")[:4]
            if src_year.isdigit() and stored_year.isdigit():
                row["changed"] = src_year > stored_year
        # 取込待ち（日次の差分では翌日に消える分を、取り込まれるまで残す）。probe 失敗の日は前回の状態をそのまま持ち越す
        row["ingested_at"] = ingested
        # 検知の日＝取得元の更新日が分かれば（Last-Modified）その日・無ければ今日（久しぶりの実行で取込済みの変化を待ちにしない）
        src_date = row.pop("source_date", "")
        detected = min(today, src_date or today)
        if since and src_date:
            since = min(since, src_date)
        row["pending_since"] = carry_pending(since, row["changed"], ingested, detected, stateless=p["kind"] in STATELESS_KINDS)
        if row["pending_since"]:
            state.setdefault(key, {})["pending_since"] = row["pending_since"]
        else:
            state.get(key, {}).pop("pending_since", None)
        rows.append(row)

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="取得元の更新の存否確認（読み取りのみ・値は取り込まない）")
    ap.add_argument("--json", action="store_true", help="機械可読 JSON で出力")
    ap.add_argument("--only", default="", help="dataset 名の部分一致で絞る")
    a = ap.parse_args(argv)

    rows = check(a.only)

    changed = [r for r in rows if r["changed"]]
    pending = [r for r in rows if r["pending_since"]]
    errors = [r for r in rows if r["status"] == "error"]
    unconfigured = [r for r in rows if r["status"] == "config_missing"]
    excluded = [r for r in rows if r["status"] == "excluded"]
    if a.json:
        print(json.dumps({"checked_at": datetime.now().isoformat(timespec="seconds"),
                          "changed": len(changed), "pending": len(pending), "errors": len(errors),
                          "config_missing": len(unconfigured), "excluded": len(excluded), "rows": rows}, ensure_ascii=False, indent=1))
    else:
        print("=== stats freshness（取得元の更新の存否確認）===", file=sys.stderr)
        w = max(len(r["dataset"]) for r in rows) if rows else 10
        for r in rows:
            mark = "更新あり" if r["changed"] else ("取込待ち" if r["pending_since"] else ("—" if r["changed"] is False else "？"))
            wait = f"（{r['pending_since']} に検知・最終取込 {r['ingested_at'] or '-'}）" if r["pending_since"] else ""
            print(f"  [{mark}] {r['dataset']:<{w}} 収録={r['stored_last'] or '-':<9} 取得元={r['signal'] or '-'}{wait} {r['note']}",
                  file=sys.stderr)
        print(f"総合: 更新あり {len(changed)}・取込待ち {len(pending)} dataset・probe 不能 {len(errors)}"
              f"・設定なし {len(unconfigured)}・対象外 {len(excluded)}（詳細は --json）", file=sys.stderr)
    return 1 if (changed or pending) else (2 if errors else 0)


if __name__ == "__main__":
    raise SystemExit(main())
