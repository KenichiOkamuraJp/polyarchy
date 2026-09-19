"""
分野タグ 21 分類（連邦の共通ファセット）。

政策主張DB（recommendations）の全文書に付与済みの `policy_tags` と同じ語彙を、統計参照（stats）・
審議会（deliberations）等の兄弟サービスもファセットとして使う＝**コーパス横断の突き合わせ軸**。
語彙の追加・改名はここだけで行い、各サービスは import して参照する（文字列の複製を作らない）。
参照は `TAGS[<英字キー>]`（名前付き定数・2026-08-19 追加）か `POLICY_TAGS`（語彙タプル・同順）。

※ 文書性格（doc_nature：主張・提言／内部調査／外部資料／活動記録）は主張文書に固有の軸のため
   recommendations 側に置く（共通ではない）。
"""

# 名前付き定数表（英字キー → 表示語）。順序＝POLICY_TAGS の順序（変えない：recommendations のフィルタ/評価・stats のレジストリが依存）。
# 各サービスは `TAGS["tax_fiscal"]` のように**キーで**参照する（日本語の文字列を複製しない）。存在しないキーは KeyError で即検知。
TAGS: dict[str, str] = {
    "macro":        "マクロ経済・経済財政運営",
    "tax_fiscal":   "税制・財政",
    "finance":      "金融・資本市場",
    "industry":     "産業政策・成長戦略",
    "innovation":   "イノベーション・科学技術・スタートアップ",
    "digital":      "デジタル・情報通信",
    "energy_env":   "エネルギー・環境",
    "labor":        "労働・雇用",
    "education":    "人材・教育",
    "social_sec":   "社会保障（年金・医療・介護）",
    "children_pop": "こども・子育て・人口",
    "diversity":    "多様性・人権・ジェンダー",
    "governance":   "企業経営・ガバナンス・サステナビリティ",
    "sme":          "中小企業",
    "trade":        "通商・国際経済・国際協力",
    "security":     "外交・安全保障・経済安全保障",
    "regional":     "地域経済・観光・地方創生",
    "infra":        "国土・住宅・交通・防災",
    "admin_reform": "行政改革・規制改革",
    "politics":     "政治・統治機構・憲法",
    "agri_food":    "農林水産・食料",
}

# 後方互換の語彙タプル（TAGS の値・同順）。既存コードはこれを参照し続けてよい。
POLICY_TAGS: tuple[str, ...] = tuple(TAGS.values())

assert len(POLICY_TAGS) == 21 and len(set(POLICY_TAGS)) == 21 and len(TAGS) == 21


def is_valid_tag(tag: str) -> bool:
    return tag in POLICY_TAGS
