"""
運用ダッシュボード（静的 HTML 1 枚・毎時生成 → S3 ops/dashboard/）。

設計＝docs/運用設計.md §1.3（2026-09-03 追加）・閲覧設計＝docs/導入団体側_監査ガイド.md §4。
目的＝**運用時に箱のシェルへ入らずに状態が見える**こと：導入団体側の担当者が自分の ReadOnly アカウントで
S3 コンソールから開くだけで、稼働・鮮度・バージョン・利用の現況を確認できる。

載せるもの（すべて数字・状態のみ＝検索語・文書名・個人を含まない＝usage_report と同じ約束）：
  ① 稼働：systemd ユニットの状態・/healthz 応答・ディスク使用率
  ② バージョン：コード版（VERSION＝upload 時に git describe を刻印）・検索構成・データ規模
  ③ 鮮度：recommendations 団体別 last_ingested・stats freshness・QE edition
  ④ 利用：直近 4 週の集計（ops/usage/weekly.jsonl＝usage_report が恒久蓄積している数字のみ）

実行（リポジトリ root）：
    python -m polyarchy_common.ops_dashboard                # ops/dashboard/index.html を生成
箱では polyarchy-dashboard.timer（毎時）が生成し S3 ops/dashboard/index.html へ保存する。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from polyarchy_common.capture import load_records
from polyarchy_common.logsetup import configure_quiet_logging, get_logger
from polyarchy_common.usage_report import (companies_enabled, freshness_summary,
                                           healthz_targets, pct, probe_healthz)

configure_quiet_logging()
log = get_logger("polyarchy.ops_dashboard")

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "ops" / "dashboard"
HTML_PATH = OUT_DIR / "index.html"
WEEKLY_PATH = ROOT / "ops" / "usage" / "weekly.jsonl"
VERSION_PATH = ROOT / "VERSION"
SERIES_PATH = ROOT / "stats" / "data" / "registry" / "series.jsonl"
QE_EDITION_PATH = ROOT / "stats" / "data" / "registry" / "qe_edition.json"
UPDATE_CHECK_PATH = ROOT / "recommendations" / "data" / "cache" / "update_check.json"
FRESHNESS_RUN_PATH = ROOT / "stats" / "data" / "cache" / "freshness_last_run.json"
APPLIED_RELEASE_PATH = ROOT / "ops" / "dashboard" / "applied_data_release.json"
COMPANIES_REGISTRY_PATH = ROOT / "companies" / "data" / "store" / "companies.json"
UNITS = ("qdrant", "polyarchy-mcp", "polyarchy-stats", "cloudflared",
         "polyarchy-health.timer", "polyarchy-fuelsync.timer",
         "polyarchy-logprune.timer", "polyarchy-usagereport.timer", "polyarchy-dashboard.timer",
         "polyarchy-updatecheck.timer", "polyarchy-dataapply.timer")


# ── 収集（全て best-effort＝取れない項目は「—」で載せる） ─────────────────────

def unit_states() -> list[tuple[str, str]]:
    """systemd ユニットの状態（systemctl が無い環境＝ローカル Mac では空）。"""
    if not shutil.which("systemctl"):
        return []
    out = []
    units = UNITS + (("polyarchy-companies",) if companies_enabled() else ())
    for u in units:
        try:
            rc = subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True, timeout=5)
            out.append((u, rc.stdout.strip() or "unknown"))
        except Exception:  # noqa: BLE001
            out.append((u, "unknown"))
    return out


def disk_usage() -> str:
    try:
        du = shutil.disk_usage("/")
        return f"{100 * du.used / du.total:.0f}%（{du.free // 2**30} GB 空き）"
    except Exception:  # noqa: BLE001
        return "—"


def code_version() -> str:
    try:
        return VERSION_PATH.read_text(encoding="utf-8").strip() or "—"
    except Exception:  # noqa: BLE001
        return "—（VERSION 未刻印＝ローカル実行 or 旧 tar）"


def data_scale() -> dict:
    d = {}
    try:
        d["stats_series"] = sum(1 for _ in open(SERIES_PATH, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    try:
        cat = ROOT / "recommendations" / "data" / "catalog.csv"
        d["recommendations_docs"] = max(0, sum(1 for _ in open(cat, encoding="utf-8")) - 1)
    except Exception:  # noqa: BLE001
        pass
    if companies_enabled():
        try:  # mcp_server が n_companies として返す数と同じ（companies.json の社数）
            d["companies"] = len(json.loads(COMPANIES_REGISTRY_PATH.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return d


def rec_freshness() -> dict:
    """recommendations 団体別 {org: {last_ingested, latest_doc_date}}（coverage が正）。"""
    try:
        from recommendations.core.coverage import org_freshness
        return org_freshness()
    except Exception as e:  # noqa: BLE001
        log.warning("recommendations 鮮度の取得失敗: %s", e)
        return {}


def qe_edition() -> str:
    try:
        state = json.loads(QE_EDITION_PATH.read_text(encoding="utf-8"))
        return f"{state.get('edition', '—')}（{state.get('label', '')}・適用 {state.get('applied_at', '—')}）"
    except Exception:  # noqa: BLE001
        return "—"


def applied_release() -> dict:
    """箱に適用済みのデータリリース（polyarchy-dataapply が書くマーク。無ければ空）。"""
    try:
        return json.loads(APPLIED_RELEASE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def update_check_summary() -> dict:
    """提言の新着チェック結果（check_new が書く JSON。無ければ空）。"""
    try:
        d = json.loads(UPDATE_CHECK_PATH.read_text(encoding="utf-8"))
        orgs = d.get("orgs", {})
        return {"checked_at": d.get("checked_at", ""),
                "new_total": sum(o.get("new", 0) for o in orgs.values()),
                "by_org": {k: o.get("new", 0) for k, o in orgs.items() if o.get("new")},
                "errors": [k for k, o in orgs.items() if o.get("error")]}
    except Exception:  # noqa: BLE001
        return {}


def stats_changed_summary() -> dict:
    """stats freshness --json の直近実行結果から changed=true の件数（無ければ空）。"""
    try:
        rows = json.loads(FRESHNESS_RUN_PATH.read_text(encoding="utf-8"))
        if isinstance(rows, dict):
            rows = rows.get("rows", [])
        changed = [r for r in rows if r.get("changed") is True]
        return {"n": len(rows), "changed": len(changed),
                "datasets": sorted({str(r.get("dataset", r.get("key", "?"))) for r in changed})[:6]}
    except Exception:  # noqa: BLE001
        return {}


def recent_weeks(n: int = 4) -> list[dict]:
    rows = load_records(WEEKLY_PATH)
    return rows[-n:] if rows else []


def _release_cell(e) -> str:
    r = applied_release()
    if not r:
        return "—（自動適用の実績なし＝初回リリース前）"
    smoke = r.get("smoke", "—")
    status = r.get("status") or ("APPLIED" if smoke == "PASS" else "FAILED")
    g = r.get("gates", {})
    head = (f"released {e(str(r.get('released_at', '—')))}・applied {e(str(r.get('applied_at', '—')))}・"
            f"{e(str(r.get('code_version', '—')))}・出荷時 hit@5 {g.get('hit5_pct', '—')}%")
    if status == "APPLIED":
        return f"{head}・<span class=\"ok\">smoke {e(str(smoke))}</span>"
    # 切り戻し（apply_data_update.sh ④）＝このリリースは不採用・serving に旧版を表示（RUNBOOK §5「切り戻し後」）
    sv = r.get("serving") or {}
    rb = r.get("rollback_smoke", "—")
    rb_cls = "ok" if rb == "PASS" else "ng"
    return (f"{head}・<span class=\"ng\">{e(str(status))}（smoke {e(str(smoke))}）</span><br>"
            f"⏪ 旧版で稼働中: {e(str(sv.get('released_at', '—')))}・{e(str(sv.get('code_version', '—')))}・"
            f"<span class=\"{rb_cls}\">切り戻し後 smoke {e(str(rb))}</span>")


# ── HTML（静的 1 枚・外部アセットなし・usage_report と同トーン） ─────────────

def render(generated_at: str, env_name: str) -> str:
    e = html.escape
    units = unit_states()
    health = probe_healthz(healthz_targets())
    scale = data_scale()
    fresh_rec = rec_freshness()
    fresh_stats = freshness_summary()
    weeks = recent_weeks()

    parts = [f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polyarchy 運用ダッシュボード</title>
<style>
 body {{ font-family: "Hiragino Sans", "Noto Sans JP", sans-serif; margin: 2rem auto; max-width: 64rem;
        padding: 0 1rem; color: #1a1a1a; background: #fff; }}
 h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
 table {{ border-collapse: collapse; font-size: .85rem; }}
 th, td {{ border: 1px solid #ccc; padding: .25rem .6rem; text-align: left; white-space: nowrap; }}
 th {{ background: #f0f0f0; }}
 .ok {{ color: #0a7a2f; font-weight: 600; }} .ng {{ color: #b00020; font-weight: 600; }}
 .note {{ color: #555; font-size: .8rem; }}
 .grid {{ display: flex; gap: 2rem; flex-wrap: wrap; }}
</style></head><body>
<h1>Polyarchy 運用ダッシュボード（{e(env_name)}）</h1>
<p class="note">生成 {e(generated_at)}（毎時自動更新）。数字・状態のみ＝検索語・文書名・個人情報を含まない。
生成元＝<code>polyarchy_common.ops_dashboard</code>（polyarchy-dashboard.timer）。</p>"""]

    # ① 稼働
    parts.append("<h2>① 稼働</h2><div class=\"grid\"><table><tr><th>ユニット</th><th>状態</th></tr>")
    if units:
        for u, st in units:
            cls = "ok" if st == "active" else "ng"
            parts.append(f"<tr><td>{e(u)}</td><td class=\"{cls}\">{e(st)}</td></tr>")
    else:
        parts.append("<tr><td colspan=2 class=\"note\">systemctl なし（ローカル生成）</td></tr>")
    parts.append("</table><table><tr><th>healthz</th><th>結果</th></tr>")
    for h in health:
        if h["ok"] is None:
            parts.append(f"<tr><td>{e(h['url'])}</td><td class=\"note\">応答なし</td></tr>")
        else:
            cls, txt = ("ok", "ok") if h["ok"] else ("ng", "NG")
            parts.append(f"<tr><td>{e(h['url'])}（{e(h['service'])}）</td><td class=\"{cls}\">{txt}</td></tr>")
    parts.append(f"</table></div><p>ディスク使用率：{e(disk_usage())}</p>")

    # ② バージョン
    parts.append(f"""<h2>② バージョン・規模</h2>
<table><tr><th>コード版</th><td>{e(code_version())}</td></tr>
<tr><th>検索構成</th><td>COLLECTION_NAME={e(os.environ.get('COLLECTION_NAME', 'policy_claims_v7'))}／VECTOR_BACKEND=qdrant（固定）</td></tr>
<tr><th>データ版</th><td>{_release_cell(e)}</td></tr>
<tr><th>stats 収録</th><td>{scale.get('stats_series', '—')} 系列</td></tr>
<tr><th>recommendations 収録</th><td>{scale.get('recommendations_docs', '—')} 文書</td></tr>""")
    if companies_enabled():
        parts.append(f"<tr><th>companies 収録</th><td>{scale.get('companies', '—')} 社</td></tr>")
    parts.append("</table>")

    # ③ 鮮度
    parts.append("<h2>③ データ鮮度</h2>")
    if fresh_rec:
        parts.append("<table><tr><th>団体</th><th>最終取込（last_ingested）</th><th>最新文書日付</th></tr>")
        for org in sorted(fresh_rec):
            v = fresh_rec[org]
            parts.append(f"<tr><td>{e(org)}</td><td>{v.get('last_ingested', '—')}</td><td>{v.get('latest_doc_date', '—')}</td></tr>")
        parts.append("</table>")
    if fresh_stats:
        parts.append(f"<p>stats freshness：{fresh_stats['datasets']} dataset・最終確認 {e(fresh_stats.get('last_checked') or '—')}／"
                     f"QE edition：{e(qe_edition())}</p>")
    uc = update_check_summary()
    if uc:
        by = "・".join(f"{e(k)} {v}" for k, v in uc["by_org"].items()) or "なし"
        err = f"／チェック失敗：{e('・'.join(uc['errors']))}" if uc["errors"] else ""
        parts.append(f"<p><b>提言の新着チェック</b>（毎日・catalog 未収録の目次件数＝取込は RUNBOOK §5 で）："
                     f"<b>新着 {uc['new_total']} 件</b>（{by}）・最終チェック {e(uc['checked_at'])}{err}</p>")
    sc = stats_changed_summary()
    if sc:
        ds = "・".join(e(x) for x in sc["datasets"]) or "—"
        parts.append(f"<p><b>stats 取得元の更新検知</b>（Last-Modified/ETag の変化）："
                     f"<b>変化 {sc['changed']} 件</b>／{sc['n']} 確認（{ds}）</p>")

    # ④ 利用（直近 4 週）
    parts.append("<h2>④ 利用（直近 4 週・詳細は ops/report/usage_report.html）</h2>")
    if weeks:
        parts.append("<table><tr><th>週</th><th>計</th><th>recommendations</th><th>stats</th><th>利用者</th><th>found=false</th><th>0件率(c)</th></tr>")
        for r in reversed(weeks):
            parts.append(f"<tr><td>{e(r['week'])}</td><td>{r['total']}</td><td>{r['recommendations']}</td>"
                         f"<td>{r['stats']}</td><td>{r['users']}</td><td>{r['stats_found_false']}</td>"
                         f"<td>{e(pct(r['recommendations_zero'], r['recommendations']))}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class=\"note\">週次集計なし（usage_report 未実行）</p>")

    parts.append('<p class="note">凡例・約束は週次レポートと同じ（数字のみ・30 日で消える生ログの集計）。'
                 '障害対応は deploy/RUNBOOK_OPS.md。</p></body></html>')
    return "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="運用ダッシュボード（静的 HTML 1 枚・数字と状態のみ）")
    ap.add_argument("--env", default="", help="表示する環境名（既定＝deploy.env の ENVIRONMENT か local）")
    a = ap.parse_args(argv)
    env_name = a.env or os.environ.get("ENVIRONMENT", "local")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().isoformat(timespec="seconds")
    HTML_PATH.write_text(render(generated_at, env_name), encoding="utf-8")
    print(f"運用ダッシュボード: {HTML_PATH.relative_to(ROOT)}（env={env_name}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
