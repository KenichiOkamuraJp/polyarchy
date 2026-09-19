"""
コーパス収録範囲（負のスコープ）と団体別収集鮮度の表出。

設計＝`recommendations/docs/コーパス収録範囲の明示.md`（採用 2026-08-20）。
背景：検索が有意なヒットを返すと、利用側 LLM が「DB にない＝存在しない/新規である」と
誤推論して追加調査を止める失敗モードが実利用で観察された。ツール説明文はツール選択時に
しか参照されず、この失敗はヒット取得後の判断段階で起きるため、介入点は**レスポンス本文**。

提供するもの（mcp_server が全検索レスポンスに載せる）：
- COVERAGE_NOTE            … 静的な負のスコープ宣言（全レスポンス共通の一文）
- COVERAGE_WARNING         … 低ヒット時のみ付ける強化警告（caveat fatigue 対策＝常時は出さない）
- COVERAGE_WARN_MIN_COUNT  … 警告の発火閾値（count < N で付与・既定 3）
- org_freshness()          … catalog.csv から団体別 {last_ingested, latest_doc_date} を計算

いずれも env で差し替え可（POLYARCHY_COVERAGE_NOTE / POLYARCHY_COVERAGE_WARNING /
POLYARCHY_COVERAGE_WARN_MIN_COUNT）＝コーパス拡張時に文言をコード変更なしで追随できる。

■ 鮮度の意味論（読み違えると不在判断を誤る）
last_ingested は「その団体の文書を最後に取り込んだ日」（catalog の retrieved_at の最大）で
あり「最後にサイトを確認した日」ではない。新着ゼロのクロールでは進まない＝実際より古く
見え得る（不在判断には安全側に働く）。クロール実行日そのものの記録は差分取込の定常化
（残タスク A2）と同時に導入する。latest_doc_date は収録済み文書の発行日の最大＝
last_ingested との差がそのまま「収集ラグの見かけ」になる。

■ 層の遮断
集計は layer=公開 の行のみ。非公開文書は（存在しても）収集日という間接情報すら出さない
（search_api の公開固定と同じフェイルクローズ方針）。
"""
import csv
import os
from pathlib import Path
from typing import Optional

from recommendations.core.config import DATA_DIR

# ── 静的フィールド（負のスコープ宣言）。事実の宣言を主・行動推奨を従に置く
# （ツール結果内の命令形はクライアント側で割り引かれ得るため、事実だけでも機能する文にする）。
COVERAGE_NOTE = os.getenv("POLYARCHY_COVERAGE_NOTE") or (
    "本コーパスは各団体（経団連・経済同友会・日本商工会議所・連合）の公式提言・意見・報告書と"
    "政府の主要方針文書（骨太方針・規制改革推進会議・財政制度等審議会）のみを収録（約3,700文書・1990年〜。"
    "経団連の2002年5月以前の発言主体は旧・経済団体連合会＝日経連との統合前）。"
    "審議会・有識者会議の議事録/配布資料、国会資料・附帯決議、パブリックコメントの結果公示、"
    "記者会見、報道、法令・政省令の本文は未収録。『検索にないこと』は『存在しないこと』を意味しない。"
    "制度の運用実態・最新動向・直近の公表物は必ずウェブ検索等で別途確認すること。"
    "各団体の収集時点は freshness（last_ingested）を参照＝それ以降の発信は未反映。"
)

# ── 低ヒット時の強化警告。全レスポンス同一音量にしない（すべてを強く警告するツールは、
# 警告しないツールと同じ効果に収束する）＝通常時は COVERAGE_NOTE のみ。
COVERAGE_WARNING = os.getenv("POLYARCHY_COVERAGE_WARNING") or (
    "ヒットが少ない。未収録領域・収集ラグの可能性が高い。この検索結果のみで"
    "『先行事例なし』『新規である』と結論しないこと。語を替える・orgs 絞りを外す再検索と、"
    "ウェブ検索での補完を強く推奨。"
)

COVERAGE_WARN_MIN_COUNT = int(os.getenv("POLYARCHY_COVERAGE_WARN_MIN_COUNT", "3"))


def _to_int_date(s: str) -> Optional[int]:
    """'2026-07-05' / '20260705' → 20260705。解釈できなければ None。"""
    if not s:
        return None
    digits = s.replace("-", "").strip()
    if len(digits) == 8 and digits.isdigit():
        return int(digits)
    return None


def org_freshness(catalog_path: Optional[Path] = None) -> dict[str, dict[str, int]]:
    """catalog.csv を走査し、団体別の収集鮮度を返す（サーバ起動時に 1 回呼ぶ）。

    返り値: {org: {"last_ingested": YYYYMMDD, "latest_doc_date": YYYYMMDD}}
      last_ingested   … retrieved_at の最大（最後に取り込みがあった日。上記の意味論に注意）
      latest_doc_date … date_int の最大（収録済み文書の発行日の最新）
    layer=公開 の行のみ集計。catalog が無い/空なら {}（fail-open＝検索自体は止めない）。
    """
    path = catalog_path or (DATA_DIR / "catalog.csv")
    out: dict[str, dict[str, int]] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("layer") or "公開") != "公開":
                    continue
                org = (row.get("org") or "").strip()
                if not org:
                    continue
                ra = _to_int_date(row.get("retrieved_at") or "")
                di = _to_int_date(row.get("date_int") or row.get("date") or "")
                cur = out.setdefault(org, {"last_ingested": 0, "latest_doc_date": 0})
                if ra and ra > cur["last_ingested"]:
                    cur["last_ingested"] = ra
                if di and di > cur["latest_doc_date"]:
                    cur["latest_doc_date"] = di
    except OSError:
        return {}
    return out
