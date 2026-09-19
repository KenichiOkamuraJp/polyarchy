"""
発見層 品質問題の候補抽出＝捕捉ログ（燃料）から find_quality の「次の問」を起こす（2026-08-29）。
recommendations の低ヒット照会トリアージと同型＝**実使用が評価セットを太らせる**回路の stats 版。

入力（queries.jsonl・複数可）：
- 既定＝ローカル `stats/data/query_log/queries.jsonl`
- `--s3`＝箱の燃料（fuelsync が `s3://<bucket>/data/stats/query_log/` へ定期バックアップ）を一時取得して合算（読み取りのみ）
- `--file <path>`＝任意のログ（複数可）

処理（決定論・LLM 不使用）：
1. find_statistics イベントを (query, facets) 単位に集計（回数・result_count/total の最新値）
2. **既存の find_quality の問と同じ (query, facets) は除外**（カバー済み）。スモーク・評価実行の定型句も除外
3. 分類：
   - `zero_or_alias`＝total 0：語彙欠落（→別名を足して到達問）か・本当に無い（→0 件問）か・取込候補かを**人が判定**
   - `flood`＝打切り（total > result_count）：目的系列に到達できるかの問に
   - 直後（同ログ内の後続 {PAIR_WINDOW} イベント以内）に lookup_statistic found=true があれば、その series_id を
     **target 候補**として草稿に添付（検索→参照の連鎖＝実使用が明かした意図。lookup_panel は series_ids を記録しないため対象外）
4. 出力＝find_quality 形式の**草稿 JSONL**（status=todo・source=usage-log・note に回数と根拠）を表示（`--out` でファイル）

**自動では find_quality.jsonl に追記しない**＝問を立てる（target の妥当性を決める）のは人。運用は：
    python -m stats.ops.quality_candidates --s3          # 草稿を眺める
    # → 妥当な行を編集して data/eval/find_quality.jsonl へ追記（todo）→ 直して pass 昇格（TDD の型）
    python -m stats.ops.quality_candidates --accept ops/triage/<日付>/stats_candidates.jsonl --ids <id,id>   # 採用の 1 コマンド（2026-09-03）
参考として lookup の found=false 集計（取込優先順位の一次情報）も末尾に出す。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Optional

from stats.core.paths import QUERY_LOG_PATH
from stats.eval.find_quality import QUALITY_PATH

PAIR_WINDOW = 10  # 検索→参照の連鎖とみなす後続イベント数
SMOKE_QUERIES = {""}  # 完全一致で捨てる定型句——必要になったら足す
SMOKE_SUBSTRINGS = ("存在しない", "テストクエリ")  # ゲート・スモークのテスト語（部分一致で捨てる）
FACET_KEYS = ("scope", "freq", "dataset", "industry", "size", "org", "sector")


def load_events(paths: list[Path]) -> list[dict]:
    events: list[dict] = []
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def existing_keys() -> set:
    if not QUALITY_PATH.exists():
        return set()
    keys = set()
    for line in QUALITY_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        keys.add((e["query"], tuple(sorted((e.get("facets") or {}).items()))))
    return keys


def candidates_from(events: list[dict]) -> list[dict]:
    """イベント列 → 草稿（純関数・決定論）。同一 (query, facets) は集計し、直後の lookup を target 候補に添付。"""
    covered = existing_keys()
    agg: dict[tuple, dict] = {}
    for i, ev in enumerate(events):
        if ev.get("tool") != "find_statistics":
            continue
        query = (ev.get("query") or "").strip()
        if not query or query in SMOKE_QUERIES or any(x in query for x in SMOKE_SUBSTRINGS):
            continue
        facets = {k: ev[k] for k in FACET_KEYS if ev.get(k)}
        key = (query, tuple(sorted(facets.items())))
        if key in covered:
            continue
        total = int(ev.get("total") or 0)
        shown = int(ev.get("result_count") or 0) + int(ev.get("collapsed") or 0)  # カードに畳まれた分も「見せた」に数える（2026-09-15〜記録）
        kind = "zero_or_alias" if total == 0 else ("flood" if total > shown else "")
        # 検索→参照の連鎖：直後 PAIR_WINDOW 以内の lookup_statistic found=true を意図の証拠として拾う
        targets: list[str] = []
        for nxt in events[i + 1:i + 1 + PAIR_WINDOW]:
            if nxt.get("tool") == "lookup_statistic" and nxt.get("found") and nxt.get("series_id"):
                if nxt["series_id"] not in targets:
                    targets.append(nxt["series_id"])
        c = agg.setdefault(key, {"query": query, "facets": facets, "count": 0, "kind": kind,
                                 "total": total, "shown": shown, "targets": [], "last_ts": ""})
        c["count"] += 1
        c["kind"] = kind or c["kind"]
        c["total"], c["shown"] = total, shown
        c["last_ts"] = max(c["last_ts"], str(ev.get("ts") or ""))
        for t in targets:
            if t not in c["targets"]:
                c["targets"].append(t)
    # 現時点の再判定（ログは過去の応答＝S-4/S-5・別名整備で既に直っている語が混ざる）：
    #   targets あり＆今は到達 → **pass 問の草稿**（実使用ペアがそのまま回帰の定点になる）
    #   targets なし＆今は total>0 → 解消済みの可能性が高い＝草稿から落とす（定点にしたければ手で立てる）
    #   それ以外 → todo 草稿（未解決＝actionable）
    from stats.core.registry import default_registry
    from stats.eval.find_quality import check as fq_check
    reg = default_registry()
    out = []
    for c in agg.values():
        if not c["kind"] and not c["targets"]:
            continue  # 当たっていて打切りも無い＝定点にする動機が薄い（必要なら手で立てる）
        draft = {"id": f"log-{abs(hash((c['query'], tuple(sorted(c['facets'].items()))))) % 10**8:08d}",
                 "query": c["query"], **({"facets": c["facets"]} if c["facets"] else {}), "limit": 20,
                 "targets": c["targets"], "status": "todo", "note": "", "added": "", "source": "usage-log"}
        now_total = reg.search(c["query"], limit=0, **c["facets"])[1]
        if c["targets"]:
            ok, _memo = fq_check(reg, draft)
            if ok:
                draft["status"] = "pass"
        elif c["kind"] == "zero_or_alias" and now_total > 0:
            continue
        why = {"zero_or_alias": f"当時 0 件（{c['count']} 回）＝語彙欠落か未収録か取込候補かを判定",
               "flood": f"打切り（{c['shown']}/{c['total']}・{c['count']} 回）＝目的系列に到達できるかの定点候補",
               "": f"{c['count']} 回・直後に lookup あり"}[c["kind"]]
        draft["note"] = (f"【草稿・要判定】{why}。現時点 total={now_total}。最終観測 {c['last_ts']}"
                         + ("。target は直後の lookup（実使用の意図）＝妥当性を確認して採否"
                            + ("・**今は到達＝pass 問としてそのまま採れる**" if draft["status"] == "pass" else "・今も未到達")
                            if c["targets"] else "。target は人が張る"))
        out.append(draft)
    return sorted(out, key=lambda x: (x["status"] != "todo", x["id"]))


def accept(draft_path: Path, ids: list[str]) -> int:
    """草稿の採用＝指定 id の行に added（今日）を刻んで find_quality.jsonl へ追記し、ゲート（find_quality）を走らせる。
    意味の判断（target の妥当性）は呼ぶ人＝開発者が済ませている前提。既に同じ (query, facets) があれば重複追記しない。"""
    import datetime
    if not ids:
        print("--ids に採用する id をカンマ区切りで指定してください", file=sys.stderr)
        return 2
    drafts = {d["id"]: d for d in load_events([draft_path])}
    missing = [i for i in ids if i not in drafts]
    if missing:
        print(f"草稿に無い id: {missing}", file=sys.stderr)
        return 2
    covered = existing_keys()
    today = datetime.date.today().isoformat()
    rows = []
    for i in ids:
        d = dict(drafts[i])
        key = (d["query"], tuple(sorted((d.get("facets") or {}).items())))
        if key in covered:
            print(f"  既存と同じ (query, facets)＝スキップ: {i} {d['query']!r}", file=sys.stderr)
            continue
        d["added"] = today
        d["note"] = (d.get("note") or "").replace("【草稿・要判定】", "").strip()
        rows.append(d)
    if not rows:
        print("追記する行がありません", file=sys.stderr)
        return 0
    with open(QUALITY_PATH, "a", encoding="utf-8") as f:
        for d in rows:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"── {len(rows)} 件を {QUALITY_PATH} へ追記（status: {[d['status'] for d in rows]}）。ゲート実行 →", file=sys.stderr)
    rc = subprocess.run([sys.executable, "-m", "stats.eval.find_quality"]).returncode
    print("── 次＝git diff で確認してコミット（pass を足したら README「品質の担保」の件数記載も同一コミットで）", file=sys.stderr)
    return rc


def fetch_s3(bucket: str, region: str = "ap-northeast-1", profile: str = "polyarchy-staging") -> list[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="stats-fuel-"))
    cmd = ["aws", "s3", "sync", f"s3://{bucket}/data/stats/query_log/", str(tmp),
           "--region", region, "--only-show-errors"]
    if profile:
        cmd += ["--profile", profile]
    subprocess.run(cmd, check=True)
    return sorted(tmp.glob("*.jsonl"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="捕捉ログ → find_quality 候補の草稿（自動追記はしない）")
    ap.add_argument("--file", action="append", default=[], help="queries.jsonl のパス（複数可）")
    ap.add_argument("--s3", action="store_true", help="箱の燃料（fuelsync の S3 バックアップ）も読む")
    ap.add_argument("--bucket", default=os.environ.get("POLYARCHY_BUCKET", ""),
                    help="--s3 の取得元バケット（既定＝環境変数 POLYARCHY_BUCKET）")
    ap.add_argument("--profile", default="polyarchy-staging")
    ap.add_argument("--out", default="", help="草稿 JSONL の書き出し先（省略時は表示のみ）")
    ap.add_argument("--accept", default="", help="採用＝草稿 JSONL から --ids の行を find_quality.jsonl へ追記しゲートを実行（開発者の 1 コマンド）")
    ap.add_argument("--ids", default="", help="--accept で採用する id（カンマ区切り）")
    a = ap.parse_args(argv)
    if a.accept:
        return accept(Path(a.accept), [i.strip() for i in a.ids.split(",") if i.strip()])
    paths = [Path(f) for f in a.file] or [QUERY_LOG_PATH]
    if a.s3 and not a.bucket:
        print("--s3 には --bucket か環境変数 POLYARCHY_BUCKET が必要（ローカルのみで続行）", file=sys.stderr)
    elif a.s3:
        try:
            paths += fetch_s3(a.bucket, profile=a.profile)
        except Exception as e:  # noqa: BLE001
            print(f"S3 取得失敗（ローカルのみで続行）: {e}", file=sys.stderr)
    events = load_events(paths)
    cands = candidates_from(events)
    nf = Counter(ev.get("reason") or "no_reason" for ev in events
                 if ev.get("tool") == "lookup_statistic" and ev.get("found") is False)
    for c in cands:
        print(json.dumps(c, ensure_ascii=False))
    if a.out:
        Path(a.out).write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cands) + ("\n" if cands else ""),
                               encoding="utf-8")
    print(f"── 候補 {len(cands)} 件（イベント {len(events)}・ログ {len(paths)} 本）。"
          "採用する行は added を埋めて data/eval/find_quality.jsonl へ（自動追記はしない）", file=sys.stderr)
    if nf:
        print(f"── 参考: lookup found=false の内訳（取込優先順位の一次情報）: {dict(nf)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
