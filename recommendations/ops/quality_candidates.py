"""
recommendations の品質問題の候補抽出＝捕捉ログ（燃料）から Phase 12 の「次の問」を起こす（2026-09-03・B15 論点2）。
`stats.ops.quality_candidates` と同型＝**実使用が評価セットを太らせる**回路の recommendations 版。

入力（queries.jsonl・複数可）：
- 既定＝ローカル `recommendations/data/query_log/queries.jsonl`（POLYARCHY_QUERY_LOG で上書き可）
- `--s3`＝箱の燃料（fuelsync が `s3://<bucket>/data/query_log/` へ定期バックアップ）を一時取得して合算（読み取りのみ）
- `--file <path>`＝任意のログ（複数可）

処理（決定論・LLM 不使用・本番検索も既定では呼ばない＝軽い）：
1. search/sweep イベントを (query, orgs, field, since, until) 単位に集計（回数・result_count の最新値・返却団体）
2. **既に評価セット（正典 197＋ユーザー由来 u###）にある設問と同文のクエリは除外**（カバー済み）。
   smoke の定型句・テスト用ダミー（top_orgs=test / public_dummy）も除外
3. 分類：
   - `zero`＝返却 0 件：棄却型（abstention）ゴールドの候補か・語彙欠落（言い換えで当たる）か・未収録（取込候補）かを**人が判定**
   - `low` ＝返却が top_k の半分未満：低ヒット＝収録の薄い領域か絞り込み条件（orgs/since/field）の過剰かを判定
   - `frequent`＝3 回以上の反復（正常ヒット）：利用者の定番＝baseline 型の定点候補
4. 出力＝草稿 JSONL（`kind`・回数・根拠・**次の一手＝phase12 scaffold のコマンド**を添付）。`--out` でファイル。
   `--recheck` を付けると zero/low について本番検索を今の索引で再実行し、現時点の件数を添える（ruri 読込＝十数秒＋α）

**自動では評価セットに追記しない**＝ゴールド（本文 verbatim の keyword・qtype）を確定するのは人（Phase 10 教訓）。運用は：
    python -m recommendations.ops.quality_candidates --s3            # 草稿を眺める（運用者＝週次 triage.sh から）
    python -m recommendations.eval.phase12_pipeline scaffold --query "…" --out cand.json   # 開発者＝雛形→careful 編集
    python -m recommendations.eval.phase12_pipeline append cand.json                        # ゲート→バイト不変追記
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from recommendations.core.config import DATA_DIR
from recommendations.core.query_capture import QUERY_LOG_PATH, load_queries

EVAL_DIR = DATA_DIR / "eval"
EVAL_FILES = (EVAL_DIR / "eval_set.json", EVAL_DIR / "eval_set_userderived.json")
# mcp_smoke（recommendations/eval/mcp_smoke.py）の定型句＝ゲート・smoke 由来の行は燃料ではない
SMOKE_QUERIES = {
    "最低賃金の全国加重平均1500円への引き上げについて各団体の立場は？", "最低賃金 1500円", "カーボンニュートラル GX",
    "最低賃金", "賃上げ", "各団体は賃上げについてどのような立場か", "最低賃金の引き上げを最初に提言したのはどの団体か",
    "エネルギー政策についてそれぞれの団体の主張は",
}
SMOKE_SINCE = 20990101  # smoke の「未来日付で 0 件」テスト
FREQUENT_MIN = 3


def existing_questions() -> set[str]:
    qs: set[str] = set()
    for p in EVAL_FILES:
        if not p.exists():
            continue
        try:
            for e in json.load(open(p, encoding="utf-8")):
                q = (e.get("question") or "").strip()
                if q:
                    qs.add(q)
        except Exception:  # noqa: BLE001
            continue
    return qs


def is_noise(ev: dict) -> bool:
    q = (ev.get("query") or "").strip()
    if not q or q in SMOKE_QUERIES or ev.get("since") == SMOKE_SINCE:
        return True
    if (ev.get("top_orgs") or []) == ["test"]:
        return True
    if any("public_dummy" in str(f) for f in (ev.get("top_files") or [])):
        return True
    return False


def _key(ev: dict) -> tuple:
    return ((ev.get("query") or "").strip(), tuple(ev.get("orgs") or []), ev.get("field") or "",
            ev.get("since") or 0, ev.get("until") or 0)


def candidates_from(events: list[dict]) -> list[dict]:
    """イベント列 → 草稿（純関数・決定論）。同一 (query, filters) は集計。"""
    covered = existing_questions()
    agg: dict[tuple, dict] = {}
    for ev in events:
        if is_noise(ev):
            continue
        k = _key(ev)
        if k[0] in covered:
            continue
        c = agg.setdefault(k, {"query": k[0], "orgs": list(k[1]), "field": k[2], "since": k[3], "until": k[4],
                               "count": 0, "result_count": None, "top_k": None, "top_orgs": [], "top_files": [],
                               "sources": Counter(), "last_ts": ""})
        c["count"] += 1
        c["sources"][ev.get("source") or "mcp"] += 1
        ts = str(ev.get("ts") or "")
        if ts >= c["last_ts"]:  # 最新の応答を代表値に
            c["last_ts"] = ts
            c["result_count"] = int(ev.get("result_count") or 0)
            c["top_k"] = int(ev.get("top_k") or 0)
            c["top_orgs"] = list(ev.get("top_orgs") or [])
            c["top_files"] = list(ev.get("top_files") or [])[:3]
    out = []
    for c in agg.values():
        rc, tk = c["result_count"] or 0, c["top_k"] or 5
        if rc == 0:
            kind, why = "zero", f"返却 0 件（{c['count']} 回）＝棄却型ゴールド候補か・語彙欠落（言い換えで当たる）か・未収録（取込候補）か"
        elif rc * 2 < tk:
            kind, why = "low", f"低ヒット（{rc}/{tk}・{c['count']} 回）＝収録の薄い領域か・絞り込み（orgs/since/field）の過剰か"
        elif c["count"] >= FREQUENT_MIN:
            kind, why = "frequent", f"反復 {c['count']} 回（正常ヒット）＝利用者の定番＝baseline 型の定点候補"
        else:
            continue  # 当たっていて反復も無い＝定点にする動機が薄い
        filters = {k: v for k, v in (("orgs", c["orgs"]), ("field", c["field"]), ("since", c["since"]),
                                     ("until", c["until"])) if v}
        did = f"log-{abs(hash((c['query'], json.dumps(filters, sort_keys=True)))) % 10**8:08d}"
        out.append({
            "id": did, "kind": kind, "query": c["query"], **({"filters": filters} if filters else {}),
            "count": c["count"], "sources": dict(c["sources"]), "last_ts": c["last_ts"],
            "result_count": rc, "top_orgs": c["top_orgs"], "top_files": c["top_files"],
            "note": f"【草稿・要判定】{why}。最終観測 {c['last_ts']}",
            "next": f"python -m recommendations.eval.phase12_pipeline scaffold --query {json.dumps(c['query'], ensure_ascii=False)} --out cand.json",
        })
    order = {"zero": 0, "low": 1, "frequent": 2}
    return sorted(out, key=lambda x: (order[x["kind"]], -x["count"], x["id"]))


def recheck(cands: list[dict]) -> None:
    """zero/low の候補を今の索引で再検索し now_count を添える（重い＝明示時のみ）。"""
    targets = [c for c in cands if c["kind"] in ("zero", "low")]
    if not targets:
        return
    print("[recheck] 本番検索スタック読み込み中（ruri＋BM25・十数秒）…", file=sys.stderr)
    from recommendations.core.search_api import PolicySearchService  # 重い import は遅延
    service = PolicySearchService()
    for c in targets:
        f = c.get("filters", {})
        try:
            chunks = service.search(c["query"], top_k=5, orgs=f.get("orgs") or None, since=f.get("since") or None,
                                    until=f.get("until") or None, field=f.get("field") or None)
            c["now_count"] = len(chunks)
            c["now_top_orgs"] = sorted({ch.to_dict().get("org", "") for ch in chunks})
        except TypeError:  # search_api の署名差（フィルタ引数を持たない版）＝素の検索で代替
            c["now_count"] = len(service.search(c["query"], top_k=5))
        except Exception as e:  # noqa: BLE001
            c["now_count_error"] = str(e)[:120]


def fetch_s3(bucket: str, region: str = "ap-northeast-1", profile: str = "polyarchy-staging") -> list[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="rec-fuel-"))
    cmd = ["aws", "s3", "sync", f"s3://{bucket}/data/query_log/", str(tmp), "--region", region, "--only-show-errors"]
    if profile:
        cmd += ["--profile", profile]
    subprocess.run(cmd, check=True)
    return sorted(tmp.glob("*.jsonl"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="捕捉ログ → Phase 12 候補の草稿（自動追記はしない）")
    ap.add_argument("--file", action="append", default=[], help="queries.jsonl のパス（複数可）")
    ap.add_argument("--s3", action="store_true", help="箱の燃料（fuelsync の S3 バックアップ）も読む")
    ap.add_argument("--bucket", default=os.environ.get("POLYARCHY_BUCKET", ""),
                    help="--s3 の取得元バケット（既定＝環境変数 POLYARCHY_BUCKET）")
    ap.add_argument("--profile", default="polyarchy-staging")
    ap.add_argument("--recheck", action="store_true", help="zero/low を今の索引で再検索して now_count を添える（重い）")
    ap.add_argument("--out", default="", help="草稿 JSONL の書き出し先（省略時は表示のみ）")
    a = ap.parse_args(argv)
    paths = [Path(f) for f in a.file] or [QUERY_LOG_PATH]
    if a.s3 and not a.bucket:
        print("--s3 には --bucket か環境変数 POLYARCHY_BUCKET が必要（ローカルのみで続行）", file=sys.stderr)
    elif a.s3:
        try:
            paths += fetch_s3(a.bucket, profile=a.profile)
        except Exception as e:  # noqa: BLE001
            print(f"S3 取得失敗（ローカルのみで続行）: {e}", file=sys.stderr)
    events: list[dict] = []
    for p in paths:
        events += load_queries(p)
    cands = candidates_from(events)
    if a.recheck:
        recheck(cands)
    for c in cands:
        print(json.dumps(c, ensure_ascii=False))
    if a.out:
        Path(a.out).write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cands) + ("\n" if cands else ""),
                               encoding="utf-8")
    kinds = Counter(c["kind"] for c in cands)
    print(f"── 候補 {len(cands)} 件（zero {kinds['zero']}・low {kinds['low']}・frequent {kinds['frequent']}／"
          f"イベント {len(events)}・ログ {len(paths)} 本）。採用は phase12 scaffold → careful 編集 → append（自動追記はしない）",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
