"""
週次利用レポート（段 1＝ローカル生成・静的 HTML 1 枚）。

設計＝docs/運用設計.md §4.2。管理 Web アプリは作らない＝ログから静的レポートを生成する。
入力＝両サービス（recommendations / stats）の捕捉ログ JSONL（ローカル＋ `--s3-bucket` 指定時は箱の保護コピーを
ミラーしてマージ）＋ stats freshness の状態＋ /healthz 応答（best-effort）。
出力＝
  ① `ops/usage/weekly.jsonl` … 週次 1 行の集計（**数字のみ＝検索語・個人を含まない**）。
     生ログは 30 日で消えるが、この集計は恒久蓄積できる（プライバシーポリシー整合）。
     既存行とのマージ規則：同じ週は再計算で置換。ただし新計算の total が既存より小さいときは
     既存を残す（ログの 30 日削除で古い週が痩せて見えるのを防ぐ）。
  ② `ops/usage/usage_report.html` … 静的 HTML 1 枚（git 外・①から何度でも再生成できる）。

★検索語（query 文字列）・ヒット文書名は集計 JSON にも HTML にも**載せない**。数だけを数える。

実行（リポジトリ root・週次目安）：
    python -m polyarchy_common.usage_report                       # ローカルのログのみ
    BUCKET=... python -m polyarchy_common.usage_report --s3-bucket $BUCKET --profile polyarchy-staging
段 2（箱の週次 timer＋Access 内配信）は A1 と同時（残タスク B2a）。
"""
from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from polyarchy_common.capture import load_records
from polyarchy_common.logsetup import configure_quiet_logging, get_logger

configure_quiet_logging()
log = get_logger("polyarchy.usage_report")

ROOT = Path(__file__).resolve().parents[1]
LOG_DIRS = {"recommendations": ROOT / "recommendations" / "data" / "query_log",
            "stats": ROOT / "stats" / "data" / "query_log"}
# 箱の保護コピー（polyarchy-fuelsync.service が sync する先）→ ローカルミラー（query_log/ 配下＝git 外）
S3_PREFIXES = {"recommendations": "data/query_log", "stats": "data/stats/query_log"}
OUT_DIR = ROOT / "ops" / "usage"
WEEKLY_PATH = OUT_DIR / "weekly.jsonl"
HTML_PATH = OUT_DIR / "usage_report.html"
FRESHNESS_STATE = ROOT / "stats" / "data" / "cache" / "freshness.json"
HEALTHZ_DEFAULT = ("http://localhost:8765/healthz", "http://localhost:8766/healthz")
LOW_HIT = 3   # recommendations の低ヒット閾値（coverage_warning と同じ count<3）


# ── 収集 ────────────────────────────────────────────────────────────────────

def s3_mirror(bucket: str, profile: str = "", region: str = "ap-northeast-1") -> None:
    """箱の捕捉ログ保護コピーを query_log/s3_mirror/ へ同期（aws CLI・失敗しても続行）。"""
    for svc, prefix in S3_PREFIXES.items():
        dst = LOG_DIRS[svc] / "s3_mirror"
        cmd = ["aws", "s3", "sync", f"s3://{bucket}/{prefix}", str(dst), "--region", region]
        if profile:
            cmd += ["--profile", profile]
        try:
            rc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if rc.returncode != 0:
                log.warning("S3 ミラー失敗（%s）: %s", svc, rc.stderr.strip()[:200])
        except Exception as e:  # noqa: BLE001
            log.warning("S3 ミラー失敗（%s）: %s", svc, e)


def collect_records() -> list[tuple[str, dict]]:
    """両サービスの捕捉ログ（*.jsonl・s3_mirror 含む）を読み、完全一致の重複を除いて返す。"""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, dict]] = []
    for svc, d in LOG_DIRS.items():
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.jsonl")):
            for rec in load_records(f):
                key = (svc, json.dumps(rec, sort_keys=True, ensure_ascii=False))
                if key in seen:
                    continue
                seen.add(key)
                out.append((svc, rec))
    return out


# ── 集計（数字のみ＝検索語・文書名・個人を残さない）─────────────────────────

def week_key(ts: str) -> Optional[tuple[str, str]]:
    """ISO 週（'2026-W34'）とその月曜日の日付を返す。ts 不正は None。"""
    try:
        d = datetime.fromisoformat(ts).date()
    except Exception:  # noqa: BLE001
        return None
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}", date.fromisocalendar(y, w, 1).isoformat()


def aggregate(records: Iterable[tuple[str, dict]]) -> list[dict]:
    """週ごとの集計行（週昇順）。値は件数のみ＝率は表示側で計算する。"""
    weeks: dict[str, dict] = {}
    users: dict[str, set] = {}
    for svc, rec in records:
        wk = week_key(str(rec.get("ts", "")))
        if wk is None:
            continue
        key, monday = wk
        w = weeks.setdefault(key, {
            "week": key, "week_start": monday, "total": 0, "recommendations": 0, "stats": 0,
            "by_source": Counter(), "users": 0,
            "recommendations_zero": 0, "recommendations_low": 0, "recommendations_orgs_filter": 0, "recommendations_period_filter": 0,
            "stats_find": 0, "stats_find_zero": 0, "stats_find_filtered": 0,
            "stats_lookup": 0, "stats_found_true": 0, "stats_found_false": 0,
            "stats_nf_reasons": Counter(), "stats_catalog": 0,
        })
        w["total"] += 1
        w[svc] += 1
        w["by_source"][str(rec.get("source") or "unknown")] += 1
        if rec.get("user_hash"):
            users.setdefault(key, set()).add(rec["user_hash"])
        if svc == "recommendations":
            n = rec.get("result_count")
            if isinstance(n, int):
                if n == 0:
                    w["recommendations_zero"] += 1
                if n < LOW_HIT:
                    w["recommendations_low"] += 1
            if rec.get("orgs"):
                w["recommendations_orgs_filter"] += 1
            if rec.get("since") is not None or rec.get("until") is not None:
                w["recommendations_period_filter"] += 1
        else:  # stats
            tool = str(rec.get("tool", ""))
            if tool == "find_statistics":
                w["stats_find"] += 1
                # ファミリーカードに畳まれて series 行が 0 の検索は 0 件ではない（family_count は 2026-09-15 から記録）
                if rec.get("result_count") == 0 and not rec.get("family_count"):
                    w["stats_find_zero"] += 1
                if any(rec.get(k) for k in ("tags", "org", "sector", "kind", "freq", "dataset")):
                    w["stats_find_filtered"] += 1
            elif tool == "lookup_statistic":
                w["stats_lookup"] += 1
                if rec.get("found") is True:
                    w["stats_found_true"] += 1
                elif rec.get("found") is False:
                    w["stats_found_false"] += 1
                    w["stats_nf_reasons"][str(rec.get("reason") or "unknown")] += 1
            elif tool in ("list_datasets", "list_sources", "list_orgs"):
                w["stats_catalog"] += 1
    rows = []
    for key in sorted(weeks):
        w = weeks[key]
        w["users"] = len(users.get(key, ()))
        w["by_source"] = dict(sorted(w["by_source"].items()))
        w["stats_nf_reasons"] = dict(sorted(w["stats_nf_reasons"].items()))
        rows.append(w)
    return rows


def merge_weekly(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """恒久蓄積とのマージ。同じ週は再計算で置換（ただし痩せた再計算＝ログ削除起因は既存優先）。"""
    by_week = {r["week"]: r for r in existing if r.get("week")}
    for r in fresh:
        old = by_week.get(r["week"])
        if old and old.get("total", 0) > r["total"]:
            continue   # 生ログの 30 日削除で部分的に消えた週＝過去の集計の方が完全
        by_week[r["week"]] = r
    return [by_week[k] for k in sorted(by_week)]


# ── 現況（freshness / healthz）────────────────────────────────────────────────

def freshness_summary() -> dict:
    """stats freshness の状態ファイルから「いつ・いくつ確認したか」だけを読む（無ければ空）。"""
    try:
        state = json.loads(FRESHNESS_STATE.read_text())
        checked = [v.get("checked_at", "") for v in state.values() if isinstance(v, dict)]
        return {"datasets": len(state), "last_checked": max(checked) if checked else ""}
    except Exception:  # noqa: BLE001
        return {}


def probe_healthz(urls: Iterable[str]) -> list[dict]:
    """GET /healthz（timeout 2 秒・best-effort）。応答なしは ok=None。"""
    out = []
    for u in urls:
        row = {"url": u, "ok": None, "service": ""}
        try:
            with urllib.request.urlopen(u, timeout=2) as r:
                body = json.loads(r.read().decode("utf-8"))
                row.update(ok=bool(body.get("ok")), service=str(body.get("service", "")))
        except Exception:  # noqa: BLE001
            pass
        out.append(row)
    return out


# ── HTML（静的 1 枚・外部アセットなし）──────────────────────────────────────

def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "—"


def render_html(rows: list[dict], fresh: dict, health: list[dict], generated_at: str) -> str:
    e = html.escape
    latest = rows[-1] if rows else None
    head = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polyarchy 週次利用レポート</title>
<style>
 body {{ font-family: "Hiragino Sans", "Noto Sans JP", sans-serif; margin: 2rem auto; max-width: 72rem;
        padding: 0 1rem; color: #1a1a1a; background: #fff; }}
 h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
 table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
 th, td {{ border: 1px solid #ccc; padding: .3rem .5rem; text-align: right; white-space: nowrap; }}
 th {{ background: #f0f0f0; }} td:first-child, th:first-child {{ text-align: left; }}
 .note {{ color: #555; font-size: .8rem; }}
 .ok {{ color: #0a7a2f; }} .ng {{ color: #b00020; }}
 .wrap {{ overflow-x: auto; }}
</style></head><body>
<h1>Polyarchy 週次利用レポート</h1>
<p class="note">生成 {e(generated_at)}／入力＝recommendations・stats の捕捉ログ（ローカル＋箱の保護コピー）。
<b>検索語・ヒット文書名・個人を特定する情報は含まない</b>（数字のみ＝恒久蓄積可・プライバシーポリシー整合）。</p>"""
    parts = [head]
    if latest:
        parts.append(f"""<h2>直近週（{e(latest['week'])}・{e(latest['week_start'])} 週）</h2>
<p>クエリ {latest['total']} 件（recommendations {latest['recommendations']}・stats {latest['stats']}）／
識別利用者 {latest['users']} 人（user_hash・authless 分は数えない）／
recommendations 0 件率 {e(pct(latest['recommendations_zero'], latest['recommendations']))}・低ヒット率(&lt;{LOW_HIT}) {e(pct(latest['recommendations_low'], latest['recommendations']))}／
stats lookup found=false {latest['stats_found_false']} 件（拡充候補の一次情報）</p>""")
    parts.append("""<h2>週次推移</h2><div class="wrap"><table>
<tr><th>週</th><th>週初</th><th>計</th><th>recommendations</th><th>stats</th><th>source 内訳</th><th>利用者</th>
<th>c: 0件</th><th>c: 低ヒット</th><th>c: orgs指定</th><th>c: 期間指定</th>
<th>s: find</th><th>s: find 0件</th><th>s: find 絞込</th><th>s: lookup</th><th>s: found=false</th><th>s: false 内訳</th><th>s: 目録</th></tr>""")
    for r in reversed(rows):
        src = "・".join(f"{e(k)} {v}" for k, v in r["by_source"].items())
        nf = "・".join(f"{e(k)} {v}" for k, v in r["stats_nf_reasons"].items()) or "—"
        parts.append(
            f"<tr><td>{e(r['week'])}</td><td>{e(r['week_start'])}</td><td>{r['total']}</td>"
            f"<td>{r['recommendations']}</td><td>{r['stats']}</td><td>{src}</td><td>{r['users']}</td>"
            f"<td>{r['recommendations_zero']}</td><td>{r['recommendations_low']}</td><td>{r['recommendations_orgs_filter']}</td>"
            f"<td>{r['recommendations_period_filter']}</td><td>{r['stats_find']}</td><td>{r['stats_find_zero']}</td>"
            f"<td>{r['stats_find_filtered']}</td><td>{r['stats_lookup']}</td><td>{r['stats_found_false']}</td>"
            f"<td>{nf}</td><td>{r['stats_catalog']}</td></tr>")
    parts.append("</table></div>")
    parts.append("<h2>データ鮮度・生き死に（現況）</h2><ul>")
    if fresh:
        parts.append(f"<li>freshness（取得元の存否確認）：{fresh['datasets']} dataset・最終確認 {e(fresh.get('last_checked') or '—')}"
                     "（月次 <code>python -m stats.ops.freshness</code>）</li>")
    else:
        parts.append("<li>freshness：状態ファイルなし（未実行）</li>")
    for h in health:
        if h["ok"] is None:
            parts.append(f'<li>/healthz {e(h["url"])}：<span class="note">応答なし（未起動 or 公開 URL は Access 遮断＝仕様）</span></li>')
        else:
            cls, txt = ("ok", "ok") if h["ok"] else ("ng", "NG")
            parts.append(f'<li>/healthz {e(h["url"])}（{e(h["service"])}）：<span class="{cls}">{txt}</span></li>')
    parts.append("</ul>")
    parts.append('<p class="note">凡例：c:＝recommendations（政策主張DB）・s:＝stats（統計参照DB）。低ヒット＝result_count&lt;3'
                 '（coverage_warning と同じ閾値）。found=false は「収録がない」ことの正直な回答＝収録拡充の優先順位の一次情報。</p>')
    parts.append("</body></html>")
    return "\n".join(parts)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="週次利用レポート（静的 HTML 1 枚＋集計 JSONL・検索語は載せない）")
    ap.add_argument("--s3-bucket", default="", help="箱の保護コピーをミラーしてマージ（要 aws CLI）")
    ap.add_argument("--profile", default="", help="aws CLI のプロファイル")
    ap.add_argument("--region", default="ap-northeast-1")
    ap.add_argument("--healthz", action="append", default=[], help="/healthz の URL（複数可・既定 localhost:8765/8766）")
    ap.add_argument("--no-health", action="store_true", help="/healthz probe を行わない")
    ap.add_argument("--json", action="store_true", help="週次集計を stdout に JSON で出す")
    a = ap.parse_args(argv)

    if a.s3_bucket:
        s3_mirror(a.s3_bucket, a.profile, a.region)

    records = collect_records()
    fresh_rows = aggregate(records)
    existing = load_records(WEEKLY_PATH)
    rows = merge_weekly(existing, fresh_rows)

    generated_at = datetime.now().isoformat(timespec="seconds")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(WEEKLY_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    health = [] if a.no_health else probe_healthz(a.healthz or HEALTHZ_DEFAULT)
    HTML_PATH.write_text(render_html(rows, freshness_summary(), health, generated_at), encoding="utf-8")

    if a.json:
        print(json.dumps({"generated_at": generated_at, "weeks": rows}, ensure_ascii=False, indent=1))
    print(f"週次利用レポート: 生ログ {len(records)} 件 → {len(rows)} 週", file=sys.stderr)
    print(f"  集計 JSONL: {WEEKLY_PATH.relative_to(ROOT)}（恒久蓄積・数字のみ）", file=sys.stderr)
    print(f"  HTML:       {HTML_PATH.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
