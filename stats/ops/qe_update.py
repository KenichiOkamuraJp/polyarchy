"""
qe_update＝四半期別GDP速報（QE）の公表回（edition）更新（第 9 弾 段 3・2026-08-29）。

QE の accessor は公表回のディレクトリ・ファイル名（例 sokuhou/files/2026/qe262/tables/gaku-mk2621.csv）を
固定で刻む＝**再取込しても新しい期は入らない**（2026-08-28 ドッグフーディングで露見した構造）。
本コマンドが唯一の edition 更新経路：
  1. 発見＝年次索引 `sokuhou/files/{年}/toukei_{年}.html` を決定論でパース
     （命名規則：1 次速報＝qe{YY}{Q}・2 次＝qe{YY}{Q}_2・edition＝{YY}{Q}{次数}。当年と前年の索引を見る＝年明けの 10-12 月期対応）
  2. 適用（--apply）＝`data/registry/qe_edition.json` を書換 → レジストリ再生成 → qe2020 全系列を再取込
  3. 評価セットの照合＝QE は公表回ごとに過去値が**全面改定**される（実測：1994Q1 名目GDP まで動く）＝
     exact_match の QE 正例を独立 regex パーサ（csv モジュール不使用・年キャリーも独立実装＝取込経路と別実装）で
     新原本と照合し、更新案を提示。書換は --write-eval のみ（黙って自動更新しない＝参照粒度設計 §9。note に新旧を記録）
  4. ★取込後は HTTP(:8766) の上げ直しと箱への同期（bootstrap 再走行ではなく tar 再展開）を忘れない

公表カレンダー＝年 8 回（1 次：2 月・5 月・8 月・11 月の中旬／2 次：3 月・6 月・9 月・12 月の上旬）。
運用は手動キック（公表日翌日まで）。freshness／refresh からも呼ばれる（qe2020 の probe と自動適用）。

実行（リポジトリ root）：
    python -m stats.ops.qe_update                        # 発見と比較だけ（新しい公表回があれば exit 1）
    python -m stats.ops.qe_update --apply                # 適用＝json 書換＋レジストリ再生成＋再取込＋評価差分の提示
    python -m stats.ops.qe_update --apply --write-eval   # 評価セットの期待値も更新（note に新旧を記録）
終了コード：0=最新（作業なし）or 適用完了／1=新しい公表回あり（未適用）or 評価セットに未処理の差分／2=発見・照合失敗。
"""
import argparse
import json
import re
import sys
from typing import Optional

from polyarchy_common.logsetup import configure_quiet_logging, get_logger

from stats.core.paths import DATA_DIR, EVAL_DIR, REGISTRY_DIR
from stats.core.registry import default_registry
from stats.core.values import ValueStore
from stats.ingest._base import SourceError, today

configure_quiet_logging()
log = get_logger("polyarchy.stats.ops")

EDITION_PATH = REGISTRY_DIR / "qe_edition.json"
# 年次索引の release リンク：/files/{頁の年}/{qe ディレクトリ}/gdemenuja.html
RELEASE_RE = re.compile(r'href="[^"]*?/files/(\d{4})/(qe\d{2}\d(?:_2)?)/gdemenuja\.html"')
_QLAB = {"1-3": "Q1", "4-6": "Q2", "7-9": "Q3", "10-12": "Q4"}
_MONTHS = {1: "1-3", 2: "4-6", 3: "7-9", 4: "10-12"}


# ── 発見（決定論パース・純関数）───────────────────────────────────────────────

def edition_of_dir(d: str) -> str:
    """ディレクトリ名 → 公表回コード（qe262→2621・qe262_2→2622）。表記外はエラー（黙って選ばない）。"""
    m = re.fullmatch(r"qe(\d{2})(\d)(_2)?", d)
    if not m or not (1 <= int(m.group(2)) <= 4):
        raise SourceError(f"QE ディレクトリ名の表記外: {d!r}")
    return f"{m.group(1)}{m.group(2)}{'2' if m.group(3) else '1'}"


def label_of_dir(d: str) -> str:
    """ディレクトリ名 → 公表回の名称（qe262→「2026年4-6月期 1次速報」）。年はデータの年（20YY）。"""
    m = re.fullmatch(r"qe(\d{2})(\d)(_2)?", d)
    if not m:
        raise SourceError(f"QE ディレクトリ名の表記外: {d!r}")
    return f"20{m.group(1)}年{_MONTHS[int(m.group(2))]}月期 {'2次' if m.group(3) else '1次'}速報"


def parse_toukei(html: str) -> list[dict]:
    """年次索引 HTML から公表回を抽出（重複は除去・edition 昇順）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for m in RELEASE_RE.finditer(html):
        page_year, d = m.group(1), m.group(2)
        if d in seen:
            continue
        seen.add(d)
        out.append({"year": page_year, "dir": d, "edition": edition_of_dir(d), "label": label_of_dir(d)})
    return sorted(out, key=lambda r: r["edition"])


def discover(day: Optional[str] = None) -> Optional[dict]:
    """当年・前年の年次索引から最新の公表回を返す。両方取れなければ None（推測しない）。"""
    from stats.ingest.esri import fetch_file
    day = day or today()
    releases: list[dict] = []
    for y in (int(day[:4]), int(day[:4]) - 1):
        try:
            cp, _ = fetch_file(f"sokuhou/files/{y}/toukei_{y}.html", day)
            releases += parse_toukei(cp.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001 — 索引ページの取得失敗は片方なら許す（年初は当年ページが無い）
            log.warning("toukei_%s の取得・解析に失敗: %s", y, e)
    if not releases:
        return None
    return max(releases, key=lambda r: r["edition"])


# ── 現在値と適用 ─────────────────────────────────────────────────────────────

def current() -> dict:
    return json.loads(EDITION_PATH.read_text(encoding="utf-8"))


def apply_edition(rel: dict) -> None:
    EDITION_PATH.write_text(json.dumps({"edition": rel["edition"], "dir": rel["dir"], "year": rel["year"],
                                        "label": rel["label"], "applied_at": today()},
                                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_latest(day: Optional[str] = None) -> bool:
    """新しい公表回があれば qe_edition.json を更新してレジストリを再生成（refresh から呼ぶ）。True＝更新した。"""
    rel = discover(day)
    if rel is None or rel["edition"] == current()["edition"]:
        return False
    apply_edition(rel)
    from stats.ingest.seed_registry import main as seed_main
    seed_main()
    log.info("QE 公表回を更新: %s（%s）", rel["edition"], rel["label"])
    return True


# ── 評価セットの照合（独立 regex パーサ＝取込経路と別実装）─────────────────────

_SPLIT = re.compile(r',(?=(?:[^"]*"[^"]*")*[^"]*$)')


def _cell(x: str) -> str:
    return x.strip().strip('"').strip()


def csv_value(text: str, period: str, col_header: str, sub_header: str = "") -> Optional[str]:
    """QE CSV から (period, 列見出し) の値を独立に読む（csv モジュール不使用・年キャリー独立実装）。無ければ None。"""
    lines = text.splitlines()
    hdr = [_cell(x) for x in _SPLIT.split(lines[2])]
    sub = [_cell(x) for x in _SPLIT.split(lines[3])]
    cols = [i for i, h in enumerate(sub if sub_header else hdr) if h == (sub_header or col_header)]
    if len(cols) != 1:
        raise SourceError(f"列見出し {col_header!r}/{sub_header!r} が {len(cols)} 列（1 列に確定しない）")
    year = ""
    for l in lines[7:]:
        c = _SPLIT.split(l)
        lab = _cell(c[0]) if c else ""
        m = re.match(r"^(\d{4})/(.*)$", lab)
        rest = lab
        if m:
            year, rest = m.group(1), m.group(2)
        if not year:
            continue
        q = _QLAB.get(rest.replace(" ", "").rstrip("."))
        if q is None or f"{year}{q}" != period:
            continue
        v = _cell(c[cols[0]] if cols[0] < len(c) else "").replace(",", "")
        return v or None
    return None


def check_eval(write: bool) -> tuple[int, int, int]:
    """exact_match の QE 正例を新しい値ストア＋原本キャッシュと照合。
    返り値：(更新案の件数, 書換えた件数, 照合エラーの件数)。write=False では提示だけ。"""
    reg = default_registry()
    store = ValueStore()
    p = EVAL_DIR / "exact_match.jsonl"
    entries = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    n_prop = n_written = n_err = 0
    for e in entries:
        s = reg.get(e["series_id"])
        if s is None or s.dataset != "qe2020":
            continue
        r = store.lookup(e["series_id"], e["period"], e.get("region", "JP"))
        if r is None:
            print(f"  ✗ {e['id']}: 値ストアに {e['period']} が無い（再取込漏れ？）")
            n_err += 1
            continue
        cachep = DATA_DIR / str(r.accessor.get("cache", ""))
        try:
            ind = csv_value(cachep.read_text(encoding="shift_jis"), e["period"],
                            str(r.accessor.get("col_header", "")), str(r.accessor.get("sub_header", "")))
        except Exception as ex:  # noqa: BLE001
            print(f"  ✗ {e['id']}: 独立照合に失敗（{ex}）")
            n_err += 1
            continue
        if ind != r.value:
            print(f"  ✗ {e['id']}: 独立パーサ {ind!r} ≠ 値ストア {r.value!r}＝取込を疑う（評価は更新しない）")
            n_err += 1
            continue
        old_v, old_f = str(e["expected_value"]), str(e.get("source_locator", {}).get("file", ""))
        if old_v == r.value and old_f == str(r.accessor.get("file", "")):
            continue
        n_prop += 1
        print(f"  → {e['id']}: 期待値 {old_v} → {r.value}・file → {r.accessor.get('file')}（独立照合 一致）")
        if write:
            e["expected_value"] = r.value
            e.setdefault("source_locator", {})["file"] = r.accessor.get("file", "")
            e["checked_at"] = today()
            e["checked_by"] = "raw-csv-crosscheck（qe_update 独立パーサ）・要人手確認"
            e["note"] = (e.get("note", "") + f"／qe_update {today()}: 改定で {old_v}→{r.value}").strip("／")
            n_written += 1
    if write and n_written:
        p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8")
    return n_prop, n_written, n_err


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QE 公表回（edition）の発見・適用・再取込・評価差分の提示")
    ap.add_argument("--apply", action="store_true", help="qe_edition.json 書換＋レジストリ再生成＋qe2020 再取込＋評価差分の提示")
    ap.add_argument("--write-eval", action="store_true", help="--apply とともに：評価セットの期待値を独立照合つきで書換（note に新旧を記録）")
    a = ap.parse_args(argv)

    cur = current()
    rel = discover()
    if rel is None:
        print("発見失敗：年次索引（toukei_{年}.html）を取得・解析できない", file=sys.stderr)
        return 2
    fresh = rel["edition"] != cur["edition"]
    print(f"収録中: {cur['edition']}（{cur['label']}）／最新: {rel['edition']}（{rel['label']}）"
          f"＝{'新しい公表回あり' if fresh else '最新'}")
    if not a.apply:
        return 1 if fresh else 0

    if fresh:
        apply_edition(rel)
        print("── qe_edition.json を更新 → レジストリ再生成", file=sys.stderr)
        from stats.ingest.seed_registry import main as seed_main
        seed_main()
    print("── qe2020 全系列を再取込", file=sys.stderr)
    from stats.ops.refresh import build_plan, run_ingest
    if not run_ingest(build_plan([("cao", "qe2020")])):
        print("再取込に失敗あり＝上のログを確認", file=sys.stderr)
        return 2
    print("── 評価セット照合（QE は公表回ごとに過去値が全面改定される）", file=sys.stderr)
    n_prop, n_written, n_err = check_eval(a.write_eval)
    if n_err:
        return 2
    if n_prop and not a.write_eval:
        print(f"更新案 {n_prop} 件＝内容を確認のうえ --write-eval で書換（黙って自動更新しない）", file=sys.stderr)
    if n_written:
        print(f"評価セット {n_written} 件を更新（note に新旧を記録）", file=sys.stderr)
    print("★残りの手順：ゲート 3 本（test_core・mcp_smoke・exact_match）→ コミット → HTTP(:8766) 上げ直し → 箱へ tar 再展開",
          file=sys.stderr)
    return 1 if (n_prop and not a.write_eval) else 0


if __name__ == "__main__":
    raise SystemExit(main())
