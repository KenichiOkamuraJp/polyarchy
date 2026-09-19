"""Phase 12：ユーザー質問→検証文パイプライン（半自動）の CLI。

実運用の質問を評価セットに育てる「器」。捕捉（MCP＝`recommendations/core/query_capture.py`）を入口に、
トリアージ→ゴールド構築（半自動）→ゲート（`evalset_gate.py`）→**ユーザー由来 eval への
バイト不変追記** までを1本の CLI に束ねる。

■ 正典とユーザー由来は別扱い（provenance 分離）
  - 正典 `data/eval/eval_set.json`（197問・sha256 4a51f5f5…）は**このツールが一切書かない**
    ＝自明にバイト不変。固定の物差し。
  - ユーザー由来ゴールドは別名前空間 `data/eval/eval_set_userderived.json` に蓄積。
    id は `u001, u002, …`（既存の数値 id と物理的に非衝突・由来が一目でわかる）。

■ 半自動の線引き（Phase 10 教訓「良い答えが出せる問い≠ベンチマークにできる問い」）
  - **自動**＝捕捉・トリアージ表示・本番検索での候補提示・ゲート検証・バイト不変追記。
  - **人手（careful）**＝解釈不変な keyword（本文 verbatim）と qtype の確定＝ゴールドの取捨。

使い方：
  python -m recommendations.eval.phase12_pipeline triage [--zero] [--org gov] [--contains 賃金] [--limit 20]
      … 捕捉済みクエリを一覧（#index で参照）。--zero=0件応答（棄却候補）だけ。
  python -m recommendations.eval.phase12_pipeline scaffold (--query "..." | --from-log 3) [--top-k 8] [--out cand.json]
      … 本番検索で候補ソース＋本文抜粋を提示し、候補ゴールドの雛形 JSON を出す。
        雛形の expected_keywords は空＝ここを本文 verbatim で careful に埋める。
  python -m recommendations.eval.phase12_pipeline append cand.json
      … evalset_gate.py で検証（PASS 必須）→ ユーザー由来ファイルへバイト不変追記。
        正典 eval_set.json の sha256 が前後で不変であることを assert（防御的多重化）。
        ★追記後の定型ステップ＝検索型を足したら `--eval-set both` を測り直し、アンカー記載
        （README「品質の担保」ほか＝append が印字する一覧）を同一コミットで更新する。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from recommendations.core.config import DATA_DIR
from recommendations.core.query_capture import QUERY_LOG_PATH, load_queries

HERE = Path(__file__).resolve().parents[2]  # リポジトリ root（サブプロセスの cwd）
EVAL_DIR = DATA_DIR / "eval"
CANONICAL = EVAL_DIR / "eval_set.json"
USERDERIVED = EVAL_DIR / "eval_set_userderived.json"
GATE_MODULE = "recommendations.eval.evalset_gate"  # 起動は python -m（cwd=リポ root 前提・ファイルパス直指定をやめた）

# 正典の不変アンカー（§30.7・引き継ぎ）。append は前後でこの一致を assert する。
CANONICAL_SHA256 = "4a51f5f519b92650a0747c004df2c8fa336bd72985daabf544871d44f9fee6b1"

_U_ID = re.compile(r"^u(\d+)$")


# ───────────────────────── 共通ヘルパ ─────────────────────────
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> list:
    return json.load(open(path, encoding="utf-8")) if path.exists() else []


def next_user_id(path: Path = USERDERIVED) -> str:
    """ユーザー由来ファイルの次 id（u001, u002, …）を返す。"""
    mx = 0
    for q in _load_json(path):
        m = _U_ID.match(str(q.get("id", "")))
        if m:
            mx = max(mx, int(m.group(1)))
    return f"u{mx + 1:03d}"


def splice_append(objs: list[dict], path: Path) -> bytes:
    """既存エントリを**一切再シリアライズせず**、末尾 `\\n]` の直前に objs を追記する。

    eval_set.json と同じ整形（json.dumps ensure_ascii=False, indent=1・末尾改行なし）を
    保ちつつ、既存バイト列を prefix として完全保存する。戻り値は書き込んだ全バイト。
    """
    if not objs:
        return path.read_bytes() if path.exists() else b""
    block = json.dumps(objs, ensure_ascii=False, indent=1)  # "[\n {..},\n {..}\n]"
    inner = block[2:-2]  # "[\n" と "\n]" を剥がす → " {..},\n {..}"

    existing = _load_json(path)
    if not existing:  # 未存在 or 空配列 → 正準形を確立
        data = block.encode("utf-8")
        path.write_bytes(data)
        return data

    raw = path.read_bytes()
    idx = raw.rfind(b"\n]")
    if idx == -1:
        raise ValueError(f"{path} の末尾 '\\n]' が見つからない（形式不正で追記不可）")
    head = raw[:idx]  # 最後のオブジェクトの `}` まで（＝既存バイト・不変）
    data = head + b",\n" + inner.encode("utf-8") + b"\n]"
    if not data.startswith(head):  # 既存バイトの保存を厳格検証
        raise AssertionError("既存バイトが保存されていない（スプライス不正）")
    path.write_bytes(data)
    return data


# ───────────────────────── triage ─────────────────────────
def cmd_triage(args: argparse.Namespace) -> int:
    rows = load_queries()
    if not rows:
        print(f"捕捉済みクエリはまだありません（{QUERY_LOG_PATH} が空／未作成）。")
        print("→ MCP サーバ経由で検索するとここに実質問が溜まります（捕捉点＝recommendations/serving/mcp_server.py）。")
        return 0

    # フィルタ（idx は「全体での位置＝時系列」で安定させ、絞り込み後も元 idx を保持）。
    indexed = list(enumerate(rows, 1))
    if args.zero:
        indexed = [(i, r) for i, r in indexed if not r.get("result_count")]
    if args.org:
        indexed = [(i, r) for i, r in indexed
                   if args.org in (r.get("top_orgs") or []) or args.org in (r.get("orgs") or [])]
    if args.contains:
        indexed = [(i, r) for i, r in indexed if args.contains in (r.get("query") or "")]
    if args.limit:
        indexed = indexed[-args.limit:]  # 直近 N 件

    print(f"捕捉クエリ {len(indexed)}/{len(rows)} 件"
          + (f"（フィルタ: {_filter_desc(args)}）" if _filter_desc(args) else "")
          + f"  ログ={QUERY_LOG_PATH}")
    print("凡例: cnt=返却チャンク数（0=棄却/未ヒット候補）・orgs=返却団体・#idx は scaffold --from-log で参照")
    print("-" * 88)
    for i, r in indexed:
        cnt = r.get("result_count")
        orgs = "・".join(r.get("top_orgs") or []) or "-"
        flag = " ⚠0件" if not cnt else ""
        ts = (r.get("ts") or "")[:19]
        print(f"#{i:<4} [{ts}] cnt={cnt}{flag}  団体={orgs}")
        print(f"      Q: {r.get('query','')}")
    print("-" * 88)
    print("次の一手: python -m recommendations.eval.phase12_pipeline scaffold --from-log <idx>  "
          "（実質問を本番検索してゴールド雛形を作る）")
    return 0


def _filter_desc(args: argparse.Namespace) -> str:
    parts = []
    if args.zero:
        parts.append("0件のみ")
    if args.org:
        parts.append(f"団体={args.org}")
    if args.contains:
        parts.append(f"含む「{args.contains}」")
    if args.limit:
        parts.append(f"直近{args.limit}")
    return "・".join(parts)


# ───────────────────────── scaffold ─────────────────────────
def cmd_scaffold(args: argparse.Namespace) -> int:
    # クエリの解決（--query 直指定 or --from-log で捕捉ログから）。
    if args.from_log is not None:
        rows = load_queries()
        if not (1 <= args.from_log <= len(rows)):
            print(f"ERROR: --from-log {args.from_log} は範囲外（1..{len(rows)}）")
            return 2
        query = rows[args.from_log - 1].get("query", "")
        print(f"[scaffold] 捕捉ログ #{args.from_log} のクエリを使用")
    else:
        query = args.query
    if not query:
        print("ERROR: --query か --from-log を指定してください")
        return 2

    # 本番検索スタックを読み込む（唯一の真実源＝recommendations.core.search_api。~十数秒）。
    print("[scaffold] 本番検索スタック読み込み中（ruri＋BM25索引・十数秒）…", file=sys.stderr)
    from recommendations.core.search_api import PolicySearchService  # 重い import は遅延
    service = PolicySearchService()
    chunks = service.search(query, top_k=args.top_k)

    print("=" * 88)
    print(f"クエリ: {query}")
    print(f"本番検索の上位 {len(chunks)} 候補（この本文から解釈不変な keyword を verbatim で拾う）:")
    print("=" * 88)
    for c in chunks:
        d = c.to_dict()
        print(f"#{d['rank']} {d['file_name']}  [{d['org']}/{d['date']}] "
              f"score={d['score']} p.{d['page']}  {d['title']}")
        excerpt = (d["text"] or "").replace("\n", " ")
        if len(excerpt) > 300:
            excerpt = excerpt[:300] + "…"
        print(f"    {excerpt}")
        print()

    if not chunks:
        print("（候補なし＝棄却型ゴールドの可能性。abstention/aggregation 型は expected_source=null・"
              "trap_terms でスキャフォールドしてください。）")

    # 候補ゴールドの雛形（expected_keywords は空＝careful に埋める）。
    top = chunks[0].to_dict() if chunks else {}
    new_id = next_user_id()
    skeleton = {
        "id": new_id,
        "question": query,  # 言い換え可（paraphrase 型にするなら設問文を作り替える）
        "expected_keywords": [],  # ★本文 verbatim で 3-4 語（answerable 型）
        "expected_source": top.get("file_name"),  # 上位候補（要吟味）
        "expected_org": top.get("org"),
        "qtype": "baseline",  # baseline/paraphrase/temporal/comparative/coverage/coverage_multi/aggregation/abstention
    }
    payload = [skeleton]
    print("=" * 88)
    print(f"候補ゴールド雛形（id={new_id}・要編集：expected_keywords を本文 verbatim で埋める）:")
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"\n→ 雛形を {args.out} に書き出しました。編集後: "
              f"python -m recommendations.eval.phase12_pipeline append {args.out}")
    else:
        print("\n（--out cand.json を付けるとファイルに書き出します）")
    return 0


# ───────────────────────── append ─────────────────────────
def cmd_append(args: argparse.Namespace) -> int:
    cand_path = Path(args.candidates)
    if not cand_path.exists():
        print(f"ERROR: 候補ファイルがありません: {cand_path}")
        return 2
    cands = _load_json(cand_path)
    if not cands:
        print("ERROR: 候補が空です")
        return 2

    # provenance 分離の強制：ユーザー由来 append は id が u### であること。
    bad = [str(q.get("id")) for q in cands if not _U_ID.match(str(q.get("id", "")))]
    if bad:
        print(f"ERROR: ユーザー由来 append の id は u### 形式に統一してください（違反: {bad}）")
        print("  → 正典 eval_set.json への追記はこのツールの対象外です（正典は不触＝バイト不変）。")
        return 2

    # ① ゲート（決定論・keyword-in-source・型別スキーマ・正典＋ユーザー由来との id 衝突）。
    print(f"[append] ゲート検証: python -m recommendations.eval.evalset_gate {cand_path}")
    gate = subprocess.run([sys.executable, "-m", GATE_MODULE, str(cand_path)],
                          cwd=str(HERE), capture_output=True, text=True)
    sys.stdout.write(gate.stdout)
    if gate.stderr:
        sys.stderr.write(gate.stderr)
    if gate.returncode != 0:
        print("[append] ゲート FAIL → 追記を中止（ゴールドを修正して再実行）。")
        return 1
    print("[append] ゲート PASS ✓")

    # ② 正典の sha256 を記録（追記前）。
    canon_before = _sha256(CANONICAL)
    if canon_before != CANONICAL_SHA256:
        print(f"WARN: 正典 sha256 が既知アンカーと不一致（現={canon_before[:12]}…）。"
              "正典が改変されています。中止。")
        return 1

    # ③ ユーザー由来ファイルへバイト不変スプライス追記。
    before_ids = {q["id"] for q in _load_json(USERDERIVED)}
    splice_append(cands, USERDERIVED)
    after = _load_json(USERDERIVED)
    after_ids = {q["id"] for q in after}
    added = sorted(after_ids - before_ids)

    # ④ 追記後の健全性＋正典不変を検証。
    canon_after = _sha256(CANONICAL)
    if canon_after != canon_before:
        print("FATAL: 正典 eval_set.json の sha256 が変化した（このツールは正典を触らないはず）。")
        return 1
    print(f"[append] 追記完了: +{len(added)}問 {added} → {USERDERIVED.name}"
          f"（総 {len(after)}問）")
    print(f"[append] 正典 eval_set.json は不変: sha256 {canon_after[:12]}…（アンカー一致）")
    print("[append] 回帰確認（クレジット0・確定的）:")
    print("  python -m recommendations.eval.eval --retrieval-only              # 正典のみ＝アンカー")
    print("  python -m recommendations.eval.eval --retrieval-only --eval-set both   # ユーザー由来も測定")
    print("  python -m recommendations.eval.eval --filter-eval                 # フィルタ26/26＋層ゲート")
    print("[append] ★定型ステップ（2026-08-27 導入）：検索型設問を追加した場合、--eval-set both の")
    print("  分母・ミス数・hit@5/MRR は変わる（劣化ではない）。測り直した値でアンカー記載を")
    print("  **同一コミットで**更新すること（更新漏れ＝偽の回帰警報の原因。2026-08-20 の u010 で実例）：")
    print("    README.md「品質の担保」／deploy/scripts/release.sh の既定値（REQUIRE_HIT5・REQUIRE_MRR）")
    print("    （他の文書は数値を写さず README へリンクしている）")
    print("  既知ミスと分かった上で追加した設問は、その旨をアンカー注記に残す。")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 12 質問→検証文パイプライン")
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("triage", help="捕捉済みクエリを一覧")
    pt.add_argument("--zero", action="store_true", help="0件応答（棄却候補）だけ表示")
    pt.add_argument("--org", help="返却団体で絞る（keidanren/gov/rengo/nissho/doyukai）")
    pt.add_argument("--contains", help="クエリ本文の部分一致で絞る")
    pt.add_argument("--limit", type=int, help="直近 N 件だけ表示")
    pt.set_defaults(func=cmd_triage)

    ps = sub.add_parser("scaffold", help="実質問を本番検索してゴールド雛形を作る")
    ps.add_argument("--query", help="検索クエリ（直指定）")
    ps.add_argument("--from-log", type=int, help="捕捉ログの #index からクエリを取る")
    ps.add_argument("--top-k", type=int, default=8, help="候補提示数（既定8）")
    ps.add_argument("--out", help="雛形 JSON の書き出し先")
    ps.set_defaults(func=cmd_scaffold)

    pa = sub.add_parser("append", help="ゲート検証→ユーザー由来へバイト不変追記")
    pa.add_argument("candidates", help="候補ゴールド JSON（eval_set と同スキーマ・id は u###）")
    pa.set_defaults(func=cmd_append)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
