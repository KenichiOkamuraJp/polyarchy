"""
発見層の品質評価＝「この語で引いたら目的の系列に到達できるか」100 問（第 9 弾 発展・2026-08-29）。

recommendations のアンカー率に相当する検索品質の定点。**到達**の定義＝limit 内の series 行に居る、
**または**返ってきたファミリーカードから series_id を機械的に組める（＝往復なしで lookup に進める）。
ファミリー畳み（dims／variant／freq 軸）後の実応答形で判定する。

TDD の回路（evalセット＝data/eval/find_quality.jsonl）：
- 新規エントリは **status=todo** で追加する＝FAIL してよい（RED）。発見層を直して到達したら **pass に昇格**（GREEN）。
- **status=pass のエントリが落ちたらゲート FAIL**（不退転）＝test_core が検証（ゲートは従来どおり 3 本）。
- 判定は行・カードベース。total（該当総数）は使わない＝発見層の修正は「照合を弱める方向のみ」で total は単調増加が正。

エントリ形式：{id, query, facets{scope/freq/…}, limit(=20), targets[series_id…]（全て到達で成功）,
              expect: "reachable"（既定）|"zero"（本当に無い語＝total 0 を期待）, status: pass|todo, note, added, source}

expect=zero の役割（2026-08-29 の問い「不要では？」への答え・残す）：
(1) **逆方向のブレーキ**＝発見層の修正は常に「照合を弱める＝当たりを増やす」方向（S-1/S-4/S-5・別名追記）で、到達問は全てその圧力側。
    漢字複合語の分割や別名の書き過ぎで「介護報酬」が雇用者報酬に当たるような**捏造側の劣化**を検知できるのは 0 件問だけ。
(2) **収録範囲との同期**＝未収録の統計を取り込むとその 0 件問が FAIL する＝到達問への書き換えを強制（評価が実態から乖離しない）。
0 件問が落ちたら 3 択を人が判定する：取り込んだ（→到達問に書き換え）／正当な notes 追記で当たった（→エントリ更新か語の調整）／
照合の弱め過ぎ（→修正を戻す）。なお lookup 層の fail-closed 契約そのものは exact_match の負例（fail_closed.jsonl）が守る＝役割が違う。

実行（リポジトリ root）：
    python -m stats.eval.find_quality            # レポート（到達率・落ちた語の内訳）
    python -m stats.eval.find_quality --verbose  # 全エントリの成否
終了コード：0＝pass エントリ全て到達／1＝pass エントリに脱落あり（todo の FAIL は exit に影響しない）。
"""
import argparse
import json
import sys

from stats.core.paths import EVAL_DIR
from stats.core.registry import Registry, default_registry

QUALITY_PATH = EVAL_DIR / "find_quality.jsonl"


def load_entries() -> list[dict]:
    return [json.loads(l) for l in QUALITY_PATH.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


def family_member_ids(reg: Registry, card: dict) -> set[str]:
    """ファミリーカードから機械的に組める series_id の集合（＝利用側が往復なしで到達できる範囲）。"""
    axis = card.get("axis", "dims")
    if axis == "freq":
        return {x["series_id"] for x in card.get("freqs", [])}
    if axis == "variant":
        pat = card.get("id_pattern", "")
        return {pat.replace("{item}", it["slug"]) for it in card.get("items", [])}
    # dims 軸：カードは id_pattern＋dims 語彙＝メンバーは同一 dataset+measure+freq の細分（畳み時の定義と同じ）
    return {x.series_id for x in reg.series.values()
            if x.dataset == card.get("dataset") and x.measure == card.get("measure")
            and x.freq == card.get("freq") and reg._specificity(x) > 0}


def check(reg: Registry, e: dict) -> tuple[bool, str]:
    """1 エントリの判定。返り値＝(成功?, 診断メモ)。"""
    facets = {k: v for k, v in (e.get("facets") or {}).items() if v}
    rows, fams, total = reg.search_collapsed(e["query"], limit=int(e.get("limit", 20)), **facets)
    if e.get("expect") == "zero":
        return total == 0, f"total={total}（0 を期待）"
    ids = {s.series_id for s in rows}
    via_card: set[str] = set()
    for f in fams:
        via_card |= family_member_ids(reg, f)
    missing = [t for t in e["targets"] if t not in ids and t not in via_card]
    how = "・".join(("行" if t in ids else "カード") for t in e["targets"] if t not in missing)
    return not missing, (f"未到達 {missing}（rows {len(rows)}・cards {len(fams)}・total {total}）" if missing
                        else f"到達（{how}）")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="発見層の品質評価（到達率）")
    ap.add_argument("--verbose", action="store_true", help="全エントリの成否を表示")
    a = ap.parse_args(argv)
    reg = default_registry()
    entries = load_entries()
    if not entries:
        print("評価セットが空（data/eval/find_quality.jsonl）", file=sys.stderr)
        return 1
    n_ok = n_pass_fail = n_todo_fail = 0
    for e in entries:
        ok, memo = check(reg, e)
        locked = e.get("status") == "pass"
        if ok:
            n_ok += 1
        elif locked:
            n_pass_fail += 1
        else:
            n_todo_fail += 1
        if a.verbose or not ok:
            mark = "✓" if ok else ("✗" if locked else "△")
            print(f"  {mark} [{e.get('status','todo')}] {e['id']}: {e['query']!r} {memo}")
    n = len(entries)
    print("=" * 60)
    print(f"到達率: {n_ok}/{n}（{100.0 * n_ok / n:.1f}%）＝pass 脱落 {n_pass_fail}・todo 未達 {n_todo_fail}（△＝RED のまま追加された改善対象）")
    if n_pass_fail:
        print("総合: FAIL ✗（status=pass のエントリが脱落＝発見層の回帰）")
        return 1
    print("総合: PASS ✅（pass エントリは全て到達。todo の未達は改善対象として許容）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
