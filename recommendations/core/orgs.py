"""
団体（発言主体）の 1 表＝コード・言及語・表示名・正式名・issuer。

リポジトリ内に 8 箇所あった団体リスト（multiquery.ORG_MENTIONS／multistage.ALL_ORGS／
mcp_server.ORG_CODES＋表示名／app・chat_app.ORG_LABELS／eval 各所の ORG_PREFIXES／
qdrant_ingest.ISSUER_OF_ORG）をここに集約した（所見 2026-08-19 段 3・値は既存のものを優先して転記）。
団体を増やすときはこの表だけを直す（ファイル名プレフィックス・catalog の org 列もこのコードに従う）。

順序について（挙動不変のため 2 本を区別する）：
  ORG_CODES（＝走査順）       = multiquery.ORG_MENTIONS の定義順（keidanren, rengo, nissho, doyukai, gov）。
                                多段検索の fan-out / 全団体走査はこの順で回る。
  ORG_DISPLAY_ORDER（＝表示順）= mcp list_orgs・eval の団体別集計表の行順（keidanren, gov, rengo, nissho, doyukai）。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Org:
    code: str            # 内部コード＝ファイル名プレフィックス・catalog.org・検索 orgs 引数
    label: str           # 短い表示名（UI 用）
    formal_name: str     # 正式名称
    description: str     # MCP list_orgs で返す説明つき名称
    mentions: tuple[str, ...]  # 設問文中の言及語（比較型マルチクエリ・名指し検出）
    issuer: str          # 発行主体（Qdrant payload の issuer）。gov は会議体別＝空（qdrant_ingest 側で決める）


ORGS: tuple[Org, ...] = (
    Org("keidanren", "経団連", "日本経済団体連合会",
        "日本経済団体連合会（経団連。2002-05-28 の日経連との統合以前の収録文書は旧・経済団体連合会）",
        ("日本経済団体連合会", "経団連"), "経団連"),
    Org("rengo", "連合", "日本労働組合総連合会", "日本労働組合総連合会（連合）",
        ("日本労働組合総連合会", "連合"), "連合"),
    Org("nissho", "日商", "日本商工会議所", "日本商工会議所（日商）",
        ("日本商工会議所", "日商"), "日本商工会議所"),
    Org("doyukai", "経済同友会", "経済同友会", "経済同友会（同友会）",
        ("経済同友会", "同友会"), "経済同友会"),
    Org("gov", "政府", "政府", "政府（骨太方針／規制改革／財政審 等の公的文書）",
        ("政府", "骨太", "規制改革推進会議", "財政制度等審議会", "財政審", "経済財政諮問会議"), ""),
)

ORG_BY_CODE: dict[str, Org] = {o.code: o for o in ORGS}

# 走査順（multiquery/multistage の従来順）。
ORG_CODES: tuple[str, ...] = tuple(o.code for o in ORGS)
# 表示順（mcp list_orgs・eval 団体別表の従来順）。
ORG_DISPLAY_ORDER: tuple[str, ...] = ("keidanren", "gov", "rengo", "nissho", "doyukai")
assert set(ORG_DISPLAY_ORDER) == set(ORG_CODES)

# 旧名の互換ビュー（呼び出し側はこれらを import して使う）。
ORG_MENTIONS: dict[str, tuple[str, ...]] = {o.code: o.mentions for o in ORGS}
ORG_LABELS: dict[str, str] = {o.code: o.label for o in ORGS}
ORG_DESCRIPTIONS: dict[str, str] = {o.code: o.description for o in ORGS}
ISSUER_OF_ORG: dict[str, str] = {o.code: o.issuer for o in ORGS if o.issuer}


def org_of_filename(fn: str) -> str:
    """ファイル名から団体コードを推定する（例 keidanren_2026_003.txt → keidanren）。不明は "?"。"""
    for code in ORG_CODES:
        if fn.startswith(code + "_"):
            return code
    return "?"
