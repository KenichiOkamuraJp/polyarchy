"""
メタデータ共通コアの**定義**と機械検証（長期開発計画 §6-2・全サービス共通）。

全コーパス共通の「芯」と、コーパス固有の「拡張」を分離する：

- **共通コア**（全コーパスの全チャンクが必ず持つ・本モジュールが唯一の定義）：
    corpus / org（発行体）/ title / date / date_int / source_url / layer / lang
- **拡張**（コーパス固有・自由）：org_type・doc_type・policy_tags・file_name・page 等。
  拡張はコーパスごとに異なってよく、共通コアの検証対象にしない。

設計原則：
- **layer は全コーパスに対する単一の不変条件**（公開データのみ提供する制約の物理的な担い手）。
  値は {公開, 機密} のみ許す。各サービスの検索側「公開固定」と対。
- **corpus はレジストリのキーと一致**させる。コレクション内では冗長だが、チャンクが境界の
  外へ出た瞬間（検索結果・エクスポート・コーパス横断）に出自を自己記述するために刻む。
- 検証は「型と存在」まで（値の正しさ＝収集検証の仕事。ここは器の整合の機械的担保）。

コレクション全点の監査 CLI はサービス側（例：`recommendations.ingest.metadata_audit`）が本モジュールの
`validate_payload` を使って実装する。
"""

# 共通コア: フィールド名 → (許容型, 必須か, 説明)。int の date_int のみ None 許容
# （日付なし文書。範囲フィルタから自然に除外される）。
COMMON_CORE: dict[str, tuple[tuple, bool, str]] = {
    "corpus":     ((str,), True,  "コーパス名（各サービスのレジストリのキーと一致）"),
    "org":        ((str,), True,  "発行体コード（keidanren/gov/…。境界ではなくファセット）"),
    "title":      ((str,), True,  "文書タイトル"),
    "date":       ((str,), True,  "発行日 YYYY-MM-DD（不明は空文字）"),
    "date_int":   ((int, type(None)), False, "発行日 YYYYMMDD の int（範囲フィルタ用・日付なしは欠落可）"),
    "source_url": ((str,), True,  "出典URL（信頼成分①の担い手）"),
    "layer":      ((str,), True,  "公開層。全コーパス共通の不変条件（値は 公開|機密 のみ）"),
    "lang":       ((str,), True,  "言語コード（現行コーパスは ja）"),
}
ALLOWED_LAYERS = ("公開", "機密")
PUBLIC_LAYER = "公開"


def validate_payload(payload: dict) -> list[str]:
    """共通コア違反のリストを返す（空＝適合）。拡張フィールドは検査しない。"""
    errs: list[str] = []
    for field, (types, required, _desc) in COMMON_CORE.items():
        if field not in payload:
            if required:
                errs.append(f"{field}: 欠落")
            continue
        v = payload[field]
        if not isinstance(v, types):
            errs.append(f"{field}: 型不正 {type(v).__name__}")
            continue
        if required and isinstance(v, str) and v == "" and field not in ("date",):
            errs.append(f"{field}: 空文字")
    if payload.get("layer") not in ALLOWED_LAYERS:
        errs.append(f"layer: 不正値 {payload.get('layer')!r}")
    return errs
