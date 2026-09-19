"""
Phase 10：多段検索オーケストレータ（retrieval-as-a-tool の"使い方"を決定論で再現）。

Claude が MCP ツール `search_policy_docs` を**団体ごとに複数回叩く**ときの戦略を、
LLM を介さずに（＝クレジット0で・再現可能に）実装する。新規の検索ロジックは持たず、
`PolicySearchService`（＝MCP と同じ本番検索・層は公開固定）だけを呼ぶ。

対象は Phase 8 実運用で判明した失敗2クラス（§25.2）：
  知見1（網羅で1団体に偏る）→ **team fan-out**：全団体を1つずつ org 指定で叩き、
    各団体の代表チャンクを均等合流する（単一プールの関連度勝ちによる偏りを消す）。
  知見2（集約/最上級は単発QA不可）→ **full scan → argmin/決定**：全団体を叩いて各社の
    最古の言及日（date_int）を突き合わせ、確定できれば団体を、できなければ健全に棄却する。

一発QA（＝`service.search` 単発）との差を `multistage_eval.py` が Phase 9 メトリクスで測る。
"""
from dataclasses import dataclass
from typing import Optional

from recommendations.core.orgs import ORG_CODES, ORG_MENTIONS
from recommendations.core.search_api import Chunk, PolicySearchService

# 多段検索で走査する団体コード（全団体・走査順）。1 表は recommendations.core.orgs。
ALL_ORGS: tuple[str, ...] = ORG_CODES

# 「各団体」「それぞれ」等＝横断・網羅の意図。名指し検出より優先し、fan-out に倒す。
_COVERAGE_MARKERS = ("各団体", "各社", "それぞれ", "各政党", "諸団体")

# 集約/最上級の意図（_decide と共有＝単一の真実源）。「唯一/だけ/のみ」＝一意性、
# 「最初/最も早く/…」＝argmin。どちらも全社走査→argmin か健全な棄却に倒す（知見2）。
UNIQUE_MARKERS = ("唯一", "だけ", "のみ")
ARGMIN_MARKERS = ("最初", "最も早", "先駆", "初めて", "最大", "最も強")
AGGREGATION_MARKERS = UNIQUE_MARKERS + ARGMIN_MARKERS


def detect_named_orgs(query: str) -> tuple[str, ...]:
    """クエリが明示的に名指しした団体コード列を返す（出現順）。網羅マーカーがあれば空。

    multiquery.detect_comparative_orgs と同じ「長い言及語から順にマスク」で包含誤検出を防ぐ
    （例「日本経済団体連合会」中の「連合」を rengo と誤検出しない）。比較マーカーは不要＝
    単一団体を名指しした設問（"経団連の資料は…"）でも拾う。網羅設問（"各団体は…"）は fan-out
    を優先したいので空を返す。
    """
    if any(m in query for m in _COVERAGE_MARKERS):
        return ()
    pairs = [(org, kw) for org, kws in ORG_MENTIONS.items() for kw in kws]
    pairs.sort(key=lambda p: len(p[1]), reverse=True)
    masked = query
    first_pos: dict[str, int] = {}
    for org, kw in pairs:
        pos = masked.find(kw)
        if pos < 0:
            continue
        masked = masked.replace(kw, "\x00" * len(kw))
        if org not in first_pos or pos < first_pos[org]:
            first_pos[org] = pos
    return tuple(sorted(first_pos, key=first_pos.get))


def classify_strategy(query: str) -> str:
    """設問の形から多段の戦略を選ぶ（オーケストレータの采配を決定論で再現）。

    返り値：
      "targeted"    … 単一団体を名指し（"経団連は…" / "日商が最初に…"）→ その団体内で答える。
                      ※最上級語があっても単一名指しなら targeted（eval 217-219 の答え可能型がこれ）。
      "aggregation" … 集約/最上級（"最初に/唯一/最も"）で名指しが単一でない→全社走査→argmin/棄却。
      "coverage"    … 横断・網羅（"各団体" 等）or 複数団体名指し→団体別 fan-out で偏りを消す。
      "baseline"    … 上記いずれでもない単一トピック→通常の関連度検索（1団体に絞らない）。

    smart_search との違い：baseline（無印の単一質問）を fan-out ではなく通常検索に倒す点。
    横断意図の無い焦点質問で「最良ソース勝ち」を保つ（＝現行 app.py と同じ体験）。
    """
    named = detect_named_orgs(query)  # 網羅マーカーがあれば () を返す
    if len(named) == 1:
        return "targeted"
    if any(m in query for m in AGGREGATION_MARKERS):
        return "aggregation"
    if any(m in query for m in _COVERAGE_MARKERS) or len(named) >= 2:
        return "coverage"
    return "baseline"


@dataclass
class StageResult:
    """orchestrate() の統一返り値（app.py / 将来の生成 eval が使う）。

    strategy   : classify_strategy の結果（targeted/aggregation/coverage/baseline）。
    chunks     : 生成・出典表示に使う代表チャンク列（層は公開固定・PolicySearchService 由来）。
    scan       : aggregation のときだけ aggregation_scan の dict（decision/rationale/evidence 等）。
    named_orgs : 名指しされた団体コード（targeted/複数名指し coverage の内訳表示用）。
    scanned_orgs: 実際に叩いた団体コード（fan-out/scan の走査範囲。baseline/targeted は名指しのみ）。
    """

    strategy: str
    chunks: list[Chunk]
    scan: Optional[dict] = None
    named_orgs: tuple[str, ...] = ()
    scanned_orgs: tuple[str, ...] = ()


@dataclass
class OrgEvidence:
    """1団体分の集約エビデンス（最古の言及＋代表チャンク）。"""

    org: str
    covered: bool
    top_score: float
    earliest_date_int: Optional[int]
    earliest_date: str
    earliest_file: str
    top_file: str
    # 取得件数（score_floor 通過後・≤per_org）。coverage 側の org_coverage と形を揃える（2026-09-07）。
    hits: int = 0


class MultiStageSearcher:
    """`PolicySearchService` を団体ごとに叩く多段検索。"""

    def __init__(self, service: PolicySearchService, score_floor: float = 0.0):
        self.svc = service
        # 「その団体がトピックに言及しているとみなす」最低スコア。0=返れば言及ありと扱う。
        self.score_floor = score_floor
        self.log = service.log

    # ── 戦略選択：名指しなら targeted、横断なら fan-out ───────────────────
    def smart_search(self, query: str, top_k: int = 5, per_org: int = 3,
                     since: Optional[int] = None, until: Optional[int] = None,
                     field: Optional[str] = None) -> list[Chunk]:
        """設問の形で戦略を選ぶ（オーケストレータの采配を決定論で再現）。

        - 単一団体を名指し（"経団連の資料は…"）→ その団体だけを targeted 検索。
        - 複数団体名指し → その団体群に限定して fan-out。
        - 名指しなし／"各団体"横断 → 全団体 fan-out（網羅）。

        since/until/field は任意のセッションフィルタ（app.py のサイドバー等）。既定 None＝
        従来挙動（フィルタなし）。層は PolicySearchService が常に公開固定＋フェイルクローズ。
        """
        named = detect_named_orgs(query)
        if len(named) == 1:
            hits = self.svc.search(query, orgs=list(named), top_k=top_k,
                                   since=since, until=until, field=field)
            self.log.info("多段（targeted）: クエリ=%r 名指し=%s → %d件", query, named[0], len(hits))
            return hits
        orgs = named if named else ALL_ORGS
        return self.coverage_search(query, top_k=top_k, per_org=per_org, orgs=orgs,
                                    since=since, until=until, field=field)

    # ── UI/生成の単一入口：戦略選択→統一結果 ─────────────────────────────
    def orchestrate(self, query: str, top_k: int = 5, per_org: int = 3,
                    since: Optional[int] = None, until: Optional[int] = None,
                    field: Optional[str] = None,
                    orgs_scope: tuple[str, ...] = ()) -> StageResult:
        """classify_strategy で戦略を選び、統一結果 StageResult を返す（app.py の入口）。

        検索は全て PolicySearchService（層=公開固定＋フェイルクローズ）経由＝機密は
        物理的に出ない。生成はここでは行わず、呼び出し側が chunks/scan から回答を組む
        （aggregation は決定論の scan["rationale"] が回答、それ以外は chunks を生成に渡す）。

        orgs_scope: サイドバー等で団体を明示限定したときの走査範囲（既定 () ＝クエリ形で
        自動選択）。指定時は 1団体→targeted・集約語あり→スコープ内走査・それ以外→スコープ内
        fan-out に倒す（クエリ由来の名指し検出より優先＝ユーザーの明示指定を尊重）。
        """
        scope = tuple(orgs_scope or ())
        if scope:
            if len(scope) == 1:
                chunks = self.svc.search(query, orgs=list(scope), top_k=top_k,
                                         since=since, until=until, field=field)
                return StageResult("targeted", chunks, named_orgs=scope, scanned_orgs=scope)
            if any(m in query for m in AGGREGATION_MARKERS):
                scan = self.aggregation_scan(query, orgs=scope,
                                             since=since, until=until, field=field)
                return StageResult("aggregation", scan["chunks"], scan=scan, scanned_orgs=scope)
            chunks = self.coverage_search(query, top_k=top_k, per_org=per_org, orgs=scope,
                                          since=since, until=until, field=field)
            return StageResult("coverage", chunks, scanned_orgs=scope)

        strategy = classify_strategy(query)
        named = detect_named_orgs(query)
        if strategy == "aggregation":
            scan = self.aggregation_scan(query, orgs=ALL_ORGS,
                                         since=since, until=until, field=field)
            return StageResult("aggregation", scan["chunks"], scan=scan,
                               named_orgs=named, scanned_orgs=ALL_ORGS)
        if strategy == "coverage":
            orgs = named if named else ALL_ORGS
            chunks = self.coverage_search(query, top_k=top_k, per_org=per_org, orgs=orgs,
                                          since=since, until=until, field=field)
            return StageResult("coverage", chunks, named_orgs=named, scanned_orgs=orgs)
        if strategy == "targeted":
            chunks = self.svc.search(query, orgs=list(named), top_k=top_k,
                                     since=since, until=until, field=field)
            self.log.info("多段（targeted）: クエリ=%r 名指し=%s → %d件",
                          query, named[0], len(chunks))
            return StageResult("targeted", chunks, named_orgs=named, scanned_orgs=named)
        # baseline：横断意図の無い単一トピック＝通常の関連度検索（1団体に絞らない）。
        chunks = self.svc.search(query, top_k=top_k, since=since, until=until, field=field)
        self.log.info("多段（baseline）: クエリ=%r → %d件（通常検索）", query, len(chunks))
        return StageResult("baseline", chunks, scanned_orgs=())

    # ── 知見1：網羅（team fan-out）────────────────────────────────────────
    def coverage_search(self, query: str, top_k: int = 5, per_org: int = 3,
                        orgs: tuple[str, ...] = ALL_ORGS,
                        since: Optional[int] = None, until: Optional[int] = None,
                        field: Optional[str] = None) -> list[Chunk]:
        """全団体を1つずつ叩き、各団体の代表チャンクを関連度順にラウンドロビン合流する。

        gold ラベルは見ない（全団体を等しく叩く）。単一検索が関連度で1団体に偏るのに対し、
        団体別プールから均等に取ることで横断の網羅を担保する（知見1の直接的対処）。
        since/until/field は任意のフィルタ（既定 None＝従来挙動）。
        """
        per_org_hits: dict[str, list[Chunk]] = {}
        for org in orgs:
            hits = self.svc.search(query, orgs=[org], top_k=per_org,
                                   since=since, until=until, field=field)
            hits = [h for h in hits if h.score >= self.score_floor]
            if hits:
                per_org_hits[org] = hits
        # 各団体の最良スコア順に団体を並べ、ラウンドロビンで1件ずつ取る（breadth-first）。
        order = sorted(per_org_hits, key=lambda o: per_org_hits[o][0].score, reverse=True)
        merged: list[Chunk] = []
        depth = max((len(v) for v in per_org_hits.values()), default=0)
        for i in range(depth):
            for org in order:
                hits = per_org_hits[org]
                if i < len(hits):
                    merged.append(hits[i])
                    if len(merged) >= top_k:
                        break
            if len(merged) >= top_k:
                break
        # rank を振り直して返す（スコアは各検索のリランカー値のまま）。
        for i, c in enumerate(merged[:top_k], 1):
            c.rank = i
        self.log.info("網羅多段検索: クエリ=%r 団体別ヒット=%s → 合流%d件（団体幅=%d）",
                      query, {o: len(v) for o, v in per_org_hits.items()},
                      len(merged[:top_k]), len({c.org for c in merged[:top_k]}))
        return merged[:top_k]

    # ── 知見2：集約/最上級（full scan → argmin/決定）──────────────────────
    def aggregation_scan(self, query: str, per_org: int = 5,
                         orgs: tuple[str, ...] = ALL_ORGS,
                         since: Optional[int] = None, until: Optional[int] = None,
                         field: Optional[str] = None) -> dict:
        """全団体を叩いて各社の最古の言及日を集め、argmin か健全な棄却かを決める。

        返り値：
          evidence   : OrgEvidence 列（団体別の最古言及・代表チャンク）。
          covering   : トピックに言及した団体コード列。
          decision   : "resolve"（団体を1つに確定できる）/ "abstain"（確定不可＝棄却が正しい）。
          answer_org : resolve のときの団体コード（唯一言及 or 明確な最古）。
          rationale  : 日本語の根拠（生成側 LLM がそのまま棄却/回答に使える）。
          chunks     : 言及団体の代表チャンク（スコア降順・出典表示用。ワンパスで収集）。
        since/until/field は任意のフィルタ（既定 None＝従来挙動）。
        """
        evid: list[OrgEvidence] = []
        rep: list[Chunk] = []  # 言及団体の代表（最上位）チャンク＝出典・生成の材料
        for org in orgs:
            hits = self.svc.search(query, orgs=[org], top_k=per_org,
                                   since=since, until=until, field=field)
            hits = [h for h in hits if h.score >= self.score_floor]
            if not hits:
                evid.append(OrgEvidence(org, False, 0.0, None, "", "", ""))
                continue
            dated = [h for h in hits if h.date_int is not None]
            earliest = min(dated, key=lambda h: h.date_int) if dated else hits[0]
            evid.append(OrgEvidence(
                org=org, covered=True, top_score=hits[0].score,
                earliest_date_int=earliest.date_int, earliest_date=earliest.date,
                earliest_file=earliest.file_name, top_file=hits[0].file_name,
                hits=len(hits)))
            rep.append(hits[0])

        covering = [e.org for e in evid if e.covered]
        decision, answer_org, rationale = self._decide(query, evid, covering)
        # 代表チャンクをスコア降順に並べ rank を振り直す（出典パネル用）。
        rep.sort(key=lambda c: c.score, reverse=True)
        for i, c in enumerate(rep, 1):
            c.rank = i
        self.log.info("集約多段検索: クエリ=%r 言及団体=%s 判定=%s%s",
                      query, "・".join(covering) or "なし", decision,
                      f"（{answer_org}）" if answer_org else "")
        return {
            "evidence": evid,
            "covering": covering,
            "decision": decision,
            "answer_org": answer_org,
            "rationale": rationale,
            "chunks": rep,
        }

    def _decide(self, query, evid, covering):
        """集約の判定ロジック（唯一 → resolve、最古で断定不可 → abstain）。"""
        is_unique_q = any(m in query for m in UNIQUE_MARKERS)
        is_argmin_q = any(m in query for m in ARGMIN_MARKERS)

        if not covering:
            return "abstain", None, "どの団体の文書にも該当トピックの言及が見つからないため、確定できません。"

        if is_unique_q:
            if len(covering) == 1:
                return "resolve", covering[0], (
                    f"該当トピックに言及しているのは {covering[0]} のみで、他団体の文書には"
                    "見当たらないため、この団体と判断できます。")
            return "abstain", None, (
                f"「唯一」を問うが、{('・'.join(covering))} の複数団体が言及しており、"
                "唯一の団体を文書からは確定できません。")

        if is_argmin_q:
            # 「最初/最も早く」＝歴史的初出。現行コーパスは各団体の近年の提言集であり、
            # どの団体が"初めて"かは単一文書からは確定できない。複数団体が言及していれば
            # 断定は不可＝健全な棄却が正しい（知見2）。全社の最古日は根拠として提示する。
            dated = [e for e in evid if e.covered and e.earliest_date_int is not None]
            table = "／".join(f"{e.org}:{e.earliest_date}" for e in sorted(
                dated, key=lambda e: e.earliest_date_int)) if dated else "日付特定不可"
            if len(covering) >= 2:
                return "abstain", None, (
                    f"{('・'.join(covering))} の複数団体が言及しており（各社の最古言及"
                    f"〔検索上位 per_org 件内での最古＝コーパス内最古の保証なし〕＝{table}）、"
                    "コーパスは各団体の近年の提言であって歴史的初出を保証しないため、"
                    "どこが最初かは文書からは確定できません。この日付一覧から順位を再構成することも"
                    "できません（取得件数に依存して変わる参考値）。")
            return "abstain", None, (
                "言及団体が限られ、かつ歴史的な初出を単一文書からは確定できないため、"
                f"最初の団体は文書からは確定できません（言及〔検索上位 per_org 件内での最古〕＝{table}）。")

        # 集約語が判定できない場合も、複数団体言及なら安全側に棄却。
        if len(covering) == 1:
            return "resolve", covering[0], (
                f"言及は {covering[0]} に限られるため、この団体と判断できます。")
        return "abstain", None, (
            f"{('・'.join(covering))} の複数団体が該当し、単一の団体には絞れないため確定できません。")
