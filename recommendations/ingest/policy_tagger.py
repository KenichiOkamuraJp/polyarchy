"""分野タグ・文書性格の追い判定（新規収集文書用・Sonnet 5）。

catalog の policy_tags/doc_nature が空の行を LLM（claude-sonnet-5）で判定し、
catalog と Qdrant payload の両方へ反映する。判定基準は本モジュールのプロンプト
（2026-08-15 の全量付与と同一。当時の設計・校正メモは公開リポジトリに含めていない）。

使い方（収集→ingest の後に実行。本文は Qdrant から取るため ingest 後が正順）:
    python -m recommendations.ingest.policy_tagger --dry-run          # 未判定行の件数確認のみ
    python -m recommendations.ingest.policy_tagger                    # 未判定行を判定して反映
    python -m recommendations.ingest.policy_tagger --file <file_name> # 特定文書を再判定（カンマ区切り可）
    python -m recommendations.ingest.policy_tagger --sync-only        # LLM を使わず catalog の現値を payload へ同期
                                                   # （手動修正の反映用）

対象コレクションは recommendations.core.config.COLLECTION_NAME（env COLLECTION_NAME で上書き可・既定 policy_claims_v7）。
判定は挙動不変（payload 追記のみ・ベクトル不変）だが、実行後のゲート確認を推奨。
"""
import argparse
import csv
import json
import sys

import requests

from recommendations.core.config import COLLECTION_NAME  # .env 読込の副作用（ANTHROPIC_API_KEY）も兼ねる

from recommendations.ingest.collect import CATALOG, CATALOG_COLUMNS
from polyarchy_common.logsetup import configure_quiet_logging, get_logger
from recommendations.core.qdrant_store import QDRANT_URL
from polyarchy_common.taxonomy import POLICY_TAGS

log = get_logger("polyarchy.policy_tagger")

TARGET_COLLECTION = COLLECTION_NAME  # 既定は recommendations.core.config に一本化（env COLLECTION_NAME で上書き可）
MODEL = "claude-sonnet-5"

TAGS = list(POLICY_TAGS)  # 21分類の定義は polyarchy_common.taxonomy が唯一の場所（B6）
NATURES = ["主張・提言", "内部調査・会員アンケート", "外部・第三者資料", "活動記録"]

SYSTEM = f"""あなたは日本の経済団体・政府の政策文書を分類する専門家です。文書のタイトルと本文冒頭から、次の2軸を判定してください。

## 軸1: 政策分野タグ（policy_tags・重要度順に最大3個）
以下の21分類から選ぶ。文書が実質的に扱う政策分野のみ（言及だけの分野は付けない）。純粋な活動記録・調査集計で分野が特定できない場合は空配列も可。
{chr(10).join('- ' + t for t in TAGS)}

## 軸2: 文書性格（doc_nature・1つ）
- 主張・提言: 団体（発行体）としての意見・提言・要望・声明・パブリックコメント・共同声明。団体の公式な主張。団体が定めて公表するひな型・書式・ガイドライン・指針類もここに含める（団体の規範的推奨＝主張である）。
- 内部調査・会員アンケート: 団体が実施した会員企業・経営者向けアンケートや調査の**結果集計が主体**の文書（景気定点観測、実態調査、賞与妥結集計、福利厚生費調査など）。回答の集計であり、団体の主張とは区別する。企業事例・取組事例の紹介を含んでいても、文書全体が団体としての提言・推進文書であれば「主張・提言」とする。
- 外部・第三者資料: 第三者（政党・他団体・外部有識者）の見解の収録・転載（政党アンケート回答集など）。発行体自身の主張ではない。
- 活動記録: 視察団・ミッションの報告、会合・セミナー・国際会議の記録。主張を含むこともあるが文書の性格は活動レポート。

## 規則
- 海外ミッション・視察（中国ミッション、アフリカミッション、インド視察等）の分野は「通商・国際経済・国際協力」とする
- 「発行元タグ」は参考情報だが信頼性が低い。タイトルと本文を優先して判定する
- policy_tags は必ず3個以内。note には判定理由を30字以内で書く（日本語）"""

SCHEMA = {
    "type": "object",
    "properties": {
        "policy_tags": {"type": "array", "items": {"type": "string", "enum": TAGS}},
        "doc_nature": {"type": "string", "enum": NATURES},
        "confidence": {"type": "string", "enum": ["high", "mid", "low"]},
        "note": {"type": "string"},
    },
    "required": ["policy_tags", "doc_nature", "confidence", "note"],
    "additionalProperties": False,
}


def fetch_text(file_name: str, max_chars: int = 1200) -> str:
    """対象コレクションから当該文書の先頭ページのチャンクを取得して結合。"""
    try:
        pts = requests.post(
            f"{QDRANT_URL}/collections/{TARGET_COLLECTION}/points/scroll",
            json={
                "filter": {"must": [{"key": "file_name", "match": {"value": file_name}}]},
                "limit": 8, "with_payload": ["text", "source"], "with_vector": False,
            }, timeout=15,
        ).json()["result"]["points"]
    except Exception as e:  # noqa: BLE001
        log.warning("本文取得失敗 %s: %s", file_name, e)
        return ""

    def page(p):
        try:
            return int(p["payload"].get("source", 0))
        except (TypeError, ValueError):
            return 0

    return "\n".join(
        c["payload"].get("text", "") for c in sorted(pts, key=page)
    )[:max_chars]


def classify(client, row: dict, body: str) -> dict | None:
    user = (
        f"発行体: {row['org']}（{row.get('doc_type', '')}）\n"
        f"日付: {row.get('date', '')}\nタイトル: {row['title']}\n"
        f"発行元タグ（参考・信頼低）: {row.get('field_tags', '') or 'なし'}\n"
        f"本文冒頭（抜粋）:\n{body or '（本文取得なし・タイトルから判定）'}"
    )
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=1024, thinking={"type": "disabled"},
                system=[{"type": "text", "text": SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            )
            out = json.loads(next(b.text for b in resp.content if b.type == "text"))
            out["policy_tags"] = out.get("policy_tags", [])[:3]  # 上限3を機械的に強制
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("判定失敗（%d/3回目）%s: %s", attempt + 1, row["file_name"], e)
    return None


def set_payload(file_name: str, tags: list[str], nature: str) -> bool:
    r = requests.post(
        f"{QDRANT_URL}/collections/{TARGET_COLLECTION}/points/payload",
        json={"payload": {"policy_tags": tags, "doc_nature": nature},
              "filter": {"must": [{"key": "file_name", "match": {"value": file_name}}]},
              "wait": True}, timeout=30)
    return r.status_code == 200


def write_rows(rows: list[dict]) -> None:
    with CATALOG.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CATALOG_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    configure_quiet_logging()
    ap = argparse.ArgumentParser(description="分野タグ・文書性格の追い判定")
    ap.add_argument("--dry-run", action="store_true", help="未判定行の確認のみ")
    ap.add_argument("--file", help="特定 file_name を再判定（カンマ区切り可）")
    ap.add_argument("--sync-only", action="store_true",
                    help="LLM を使わず catalog の現値を payload へ同期")
    args = ap.parse_args()

    rows = list(csv.DictReader(CATALOG.open(encoding="utf-8")))
    for r in rows:  # 旧 catalog 互換（列欠落時）
        r.setdefault("policy_tags", "")
        r.setdefault("doc_nature", "")

    if args.file:
        names = {n.strip() for n in args.file.split(",")}
        targets = [r for r in rows if r["file_name"] in names]
        missing = names - {r["file_name"] for r in targets}
        if missing:
            log.error("catalog に存在しない file_name: %s", sorted(missing))
            return 1
    else:
        targets = [r for r in rows if not r["policy_tags"] and not r["doc_nature"]]

    if args.sync_only:
        n_ok = sum(
            set_payload(r["file_name"],
                        [t for t in r["policy_tags"].split("|") if t],
                        r["doc_nature"])
            for r in (targets if args.file else rows) if r["doc_nature"]
        )
        log.info("payload 同期完了: %d 件（コレクション=%s）", n_ok, TARGET_COLLECTION)
        return 0

    log.info("対象 %d 件（未判定 or 指定）・コレクション=%s・モデル=%s",
             len(targets), TARGET_COLLECTION, MODEL)
    if args.dry_run or not targets:
        for r in targets[:20]:
            log.info("  %s %s %s", r["org"], r.get("date", "")[:7], r["title"][:40])
        if len(targets) > 20:
            log.info("  …ほか %d 件", len(targets) - 20)
        return 0

    import anthropic
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY は .env（recommendations.core.config が読込済）

    n_ok = n_ng = 0
    for r in targets:
        body = fetch_text(r["file_name"])
        res = classify(client, r, body)
        if res is None:
            n_ng += 1
            continue
        r["policy_tags"] = "|".join(res["policy_tags"])
        r["doc_nature"] = res["doc_nature"]
        if not set_payload(r["file_name"], res["policy_tags"], res["doc_nature"]):
            log.warning("payload 反映失敗（catalog には記録）: %s", r["file_name"])
        n_ok += 1
        log.info("判定 %s → [%s] %s（%s）", r["file_name"],
                 r["policy_tags"] or "分野なし", r["doc_nature"], res.get("note", ""))

    write_rows(rows)
    log.info("完了: 判定 %d 件・失敗 %d 件・catalog 更新済。ゲート確認を推奨"
             "（運用手順 §5）", n_ok, n_ng)
    return 0 if n_ng == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
