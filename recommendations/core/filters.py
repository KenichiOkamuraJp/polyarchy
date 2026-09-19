"""
Phase 6：構造化フィルタ検索とデータ層分離のためのフィルタ定義。

本番検索（`recommendations/core/hybrid.py` の HybridRetriever）は「団体・日付・分野・層」でチャンクを絞り込める。
フィルタは 2 経路に分けて適用する（バッチ2 段4〔2026-08-28〕で Chroma 全廃＝push-down は
`core/qdrant_store.qdrant_filter` が本 SearchFilter を翻訳する）：

- **ベクトル・BM25 sparse 側**: org=$in / layer=$in / date_int range を Qdrant の filter に push。
  **重要**: ベクトル検索は「フィルタ後の母集合の中で」近傍を返す必要があるため（後段 Python
  フィルタだけだと top-KV が全部圏外になり recall が消える）、必ず push-down する。
- **分野（field）**: doc 単位・部分一致のため push できない＝候補を広めに取り
  Python 述語 `matches()` で融合前に後段フィルタする（FILTER_WIDEN で recall を守る）。
  照合先は **統一 21 分類 `policy_tags`**（全文書に付与済み・`polyarchy_common.taxonomy`）を主とし、
  発行元タグ `field_tags`（団体固有・gov は 0 件、日商は 2 割しか付与なし）は後方互換の副とする
  （B22・2026-09-15＝実利用ログで gov×field が常に 0 件になる不具合の修正）。

**データ層分離**: 既定は公開層のみ（`DEFAULT_LAYERS`）。機密層は明示的に層を
広げない限り検索経路に出てこない＝「意志ではなく仕組みで分離」（ロードマップ §2.4）。
"""
from dataclasses import dataclass, field

from recommendations.core.config import DEFAULT_LAYERS


@dataclass(frozen=True)
class SearchFilter:
    """本番検索のフィルタ条件。全て AND で結合する。

    - orgs:       団体コード（"keidanren"/"gov"/"rengo"/"nissho"/…）。空=団体で絞らない。
    - date_from:  YYYYMMDD の int（含む・以降）。日付が空の文書は date_int を持たず除外される。
    - date_to:    YYYYMMDD の int（含む・以前）。
    - field_tag:  分野の部分一致文字列（例 "労働"）。統一 21 分類 policy_tags への部分一致を主、
                  発行元タグ field_tags への部分一致を副（どちらかに当たれば通す）。
    - layers:     層（"公開"/"機密"）。既定は公開のみ（データ層分離）。
    """

    orgs: tuple[str, ...] = ()
    date_from: int | None = None
    date_to: int | None = None
    field_tag: str | None = None
    layers: tuple[str, ...] = DEFAULT_LAYERS

    def is_active(self) -> bool:
        """既定（公開層のみ・他条件なし）以外の絞り込みが指定されているか。"""
        return bool(
            self.orgs
            or self.date_from is not None
            or self.date_to is not None
            or self.field_tag
            or tuple(self.layers) != tuple(DEFAULT_LAYERS)
        )

    def needs_python_postfilter(self) -> bool:
        """push-down で表現できない条件（分野の部分一致）があるか。

        これが True のときはベクトル/BM25 候補を広げて `matches()` で後段フィルタする。
        """
        return bool(self.field_tag)

    def matches(self, meta: dict) -> bool:
        """チャンクのメタデータが全条件を満たすか（分野の部分一致を含む完全な述語）。

        BM25 候補の後段フィルタ、およびベクトル候補の分野絞り込みに使う。
        push-down（qdrant_filter）と同じ意味論（org/layer/date）＋ 分野の部分一致
        （`field_tag_matches`＝policy_tags 主・field_tags 副）。
        """
        if self.orgs and meta.get("org") not in self.orgs:
            return False
        if self.layers and meta.get("layer") not in self.layers:
            return False
        di = meta.get("date_int")
        if self.date_from is not None or self.date_to is not None:
            if not isinstance(di, int):  # 日付なし文書は範囲指定時に除外
                return False
            if self.date_from is not None and di < self.date_from:
                return False
            if self.date_to is not None and di > self.date_to:
                return False
        if self.field_tag and not field_tag_matches(self.field_tag, meta):
            return False
        return True

    def describe(self) -> str:
        """検索構成の表示用（query.py の出典タグ等）。"""
        parts = []
        if self.orgs:
            parts.append("org=" + "|".join(self.orgs))
        if self.date_from is not None:
            parts.append(f"since={self.date_from}")
        if self.date_to is not None:
            parts.append(f"until={self.date_to}")
        if self.field_tag:
            parts.append(f"field~{self.field_tag}")
        parts.append("layer=" + "|".join(self.layers))
        return ", ".join(parts)


def policy_tags_of(meta: dict) -> list[str]:
    """メタデータの統一 21 分類タグを list で返す（Qdrant payload は list・catalog 由来は "|" 連結）。"""
    pt = meta.get("policy_tags") or ()
    if isinstance(pt, str):
        pt = pt.split("|")
    return [t for t in pt if t]


def field_tag_matches(needle: str, meta: dict) -> bool:
    """分野絞りの述語：policy_tags（統一 21 分類・全文書に付与）への部分一致を主、
    field_tags（発行元タグ・団体固有で充足率が不均一）への部分一致を副とし、どちらかで通す。

    旧実装は field_tags のみを見ていたため、発行元タグを持たない gov（0/28 文書）は field 指定で
    必ず 0 件になっていた（実利用ログ 2026-09-05〜06 で 2 件・B22）。
    """
    if any(needle in t for t in policy_tags_of(meta)):
        return True
    return needle in (meta.get("field_tags") or "")


def default_filter() -> SearchFilter:
    """本番の既定フィルタ（公開層のみ）。呼び出し側が None を渡したときに使う。"""
    return SearchFilter()
