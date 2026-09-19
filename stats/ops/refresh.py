"""
refresh＝月次データ更新の 1 コマンド化（半自動・品質関門つき）。

設計＝docs/運用設計.md §2.2。各 ingest は冪等（値ファイルは最新 vintage で丸ごと書換）
なので「該当系列の再取込」がそのまま差分更新になる。流れ：
  1. freshness（存否確認）→「更新あり」の dataset を特定（--dataset で probe を飛ばして直接指定も可）
  2. 該当 dataset の登録系列を ingest モジュール別にまとめて再取込
  3. レジストリ再生成（seed_registry＝last_period 反映）＋品質指標 quality.json（series_quality）
  4. ゲート 3 本（test_core・mcp_smoke・exact_match）
  5. `git diff --stat` を表示して停止＝**人が差分を確認してコミット**（自動コミットしない）

★関門の意味：改定（revision）で evaluation 正例の値が変わると exact_match が落ちる＝**仕様**
  （黙って上書きしない）。落ちたら評価セットの期待値を新 vintage で更新するか、改定を記録して判断する。

実行（リポジトリ root・月次目安）：
    python -m stats.ops.refresh                  # freshness → 更新ありだけ再取込 → ゲート
    python -m stats.ops.refresh --dry-run        # 計画（どの dataset をどのモジュールで）だけ表示
    python -m stats.ops.refresh --dataset cpi2020    # probe を飛ばして指定 dataset（コード名）を再取込
終了コード：0=更新なし or 全工程成功／1=ゲート失敗（差分は残る＝人が判断）／2=取込・probe 失敗あり。
"""
import argparse
import importlib
import subprocess
import sys

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.paths import ROOT_DIR
from stats.core.registry import default_registry
from stats.ops import freshness

configure_quiet_logging()
log = get_logger("polyarchy.stats.ops")

# accessor.type → ingest モジュール（stats/ingest/<name>.py）。planned/derived は対象外。
TYPE_TO_MODULE = {
    "esri_xlsx": "esri", "esri_xls": "esri", "esri_qe_csv": "esri",
    "estat": "estat",
    "boj_file": "boj_file", "ipss_xlsx": "boj_file",
    "cao_gap_xlsx": "cao_gap",
    "boj_flat": "boj_flat",
    "boj_mtshtml": "boj_mtshtml",
    "bls_api": "bls", "eurostat_api": "eurostat",
    "maikin_csv": "maikin", "estat_catalog_xlsx": "shokugyo", "cao_mitoshi_pdf": "cao_mitoshi",
    "mof_csv": "mof",
    "mof_zaisei": "mof_zaisei",
    "imf_dm": "intl", "oecd_sdmx": "intl", "wb_api": "intl",
    "soumu_hakusho": "soumu_hakusho",
    "pdf_table": "pdf_shunto",   # 系列指定不可＝ --all で回す（現収録は guide のみ＝実質 no-op）
}
GATES = ("stats.eval.test_core", "stats.eval.mcp_smoke", "stats.eval.exact_match")


def build_plan(datasets: list[tuple[str, str]]) -> dict[str, list[str]]:
    """更新対象 (org, dataset) → {ingest モジュール: [series_id, …]}（registered のみ・derived 除外）。"""
    registry = default_registry()
    plan: dict[str, list[str]] = {}
    for s in registry.series.values():
        if (s.org, s.dataset) not in datasets or s.status != "registered" or s.is_derived:
            continue
        mod = TYPE_TO_MODULE.get(s.accessor.get("type", ""))
        if mod is None:
            log.warning("%s: accessor.type=%s は自動再取込に非対応＝手動確認", s.series_id, s.accessor.get("type"))
            continue
        plan.setdefault(mod, []).append(s.series_id)
    return plan


def run_ingest(plan: dict[str, list[str]]) -> bool:
    """モジュールごとに再取込（in-process・run_cli の --series 経由）。全成功で True。"""
    ok = True
    for mod, sids in sorted(plan.items()):
        m = importlib.import_module(f"stats.ingest.{mod}")
        argv = ["--all"] if mod == "pdf_shunto" else [x for sid in sids for x in ("--series", sid)]
        print(f"── 再取込: stats.ingest.{mod}（{len(sids)} 系列）", file=sys.stderr)
        rc = m.main(argv)
        if rc != 0:
            log.warning("stats.ingest.%s が失敗（exit=%d）", mod, rc)
            ok = False
    return ok


def run_gates() -> bool:
    """ゲート 3 本をサブプロセスで実行（stdout/stderr は素通し）。全 PASS で True。"""
    ok = True
    for g in GATES:
        print(f"── ゲート: python -m {g}", file=sys.stderr)
        rc = subprocess.run([sys.executable, "-m", g], cwd=ROOT_DIR).returncode
        if rc != 0:
            log.warning("ゲート %s が失敗（exit=%d）", g, rc)
            ok = False
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="月次データ更新の 1 コマンド化（freshness→再取込→ゲート・自動コミットしない）")
    ap.add_argument("--dataset", action="append", default=[],
                    help="dataset 名の部分一致（複数可）。指定時は freshness probe を飛ばして直接更新対象にする")
    ap.add_argument("--only", default="", help="freshness probe を dataset 名の部分一致で絞る")
    ap.add_argument("--dry-run", action="store_true", help="計画（対象 dataset とモジュール）だけ表示して終了")
    ap.add_argument("--no-gates", action="store_true", help="ゲートを飛ばす（原則使わない）")
    a = ap.parse_args(argv)

    probe_failed = False
    if a.dataset:
        registry = default_registry()
        all_ds = {(s.org, s.dataset) for s in registry.series.values()}
        targets = sorted({(o, d) for (o, d) in all_ds if any(pat in d for pat in a.dataset)})
        if not targets:
            print(f"該当 dataset なし: {a.dataset}", file=sys.stderr)
            return 2
    else:
        print("── freshness（存否確認）", file=sys.stderr)
        rows = freshness.check(a.only)
        targets = sorted({(r["org"], r["dataset"]) for r in rows if r["changed"]})
        probe_failed = any(r["note"].startswith("probe 失敗") or "解析失敗" in r["note"] for r in rows)
        if probe_failed:
            log.warning("probe 不能の dataset あり＝手動確認（python -m stats.ops.freshness --json）")
        if not targets:
            print("更新なし（取得元に変化なし）", file=sys.stderr)
            return 2 if probe_failed else 0

    if ("cao", "qe2020") in targets and not a.dry_run:
        # QE は公表回ごとに別 URL＝先に edition を適用しないと再取込しても新しい期が入らない（第 9 弾 段 3）
        from stats.ops import qe_update
        if qe_update.ensure_latest():
            print("── QE 公表回を更新（qe_edition.json → レジストリ再生成）", file=sys.stderr)

    plan = build_plan(targets)
    print("── 更新計画", file=sys.stderr)
    for org, ds in targets:
        print(f"  {org}:{ds}", file=sys.stderr)
    for mod, sids in sorted(plan.items()):
        print(f"  → stats.ingest.{mod}: {len(sids)} 系列", file=sys.stderr)
    if a.dry_run:
        print("（--dry-run のためここまで）", file=sys.stderr)
        return 0
    if not plan:
        print("再取込できる系列なし（非対応 accessor＝手動対応）", file=sys.stderr)
        return 2

    ingest_ok = run_ingest(plan)

    print("── レジストリ再生成（last_period 反映）", file=sys.stderr)
    from stats.ingest.seed_registry import main as seed_main
    seed_main()
    print("── 品質指標の再生成（quality.json＝系列の散らばり・母集団）", file=sys.stderr)
    from stats.ops.series_quality import main as quality_main
    quality_main()

    gates_ok = True if a.no_gates else run_gates()

    print("── 差分（git diff --stat）", file=sys.stderr)
    subprocess.run(["git", "diff", "--stat"], cwd=ROOT_DIR)
    print("★ここで停止＝差分を確認して人がコミットする（自動コミットしない）。", file=sys.stderr)
    print("  exact_match が落ちた場合は改定（revision）の可能性＝評価セットの期待値更新か記録を判断。", file=sys.stderr)
    if not gates_ok:
        return 1
    return 2 if (not ingest_ok or probe_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
