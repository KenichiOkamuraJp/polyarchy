"""
Phase 10：MCP サーバ（retrieval-as-a-tool）。本番検索を MCP ツールとして公開する。

公開ツール：
- `search_policy_docs(query, orgs?, since?, until?, field?, top_k?, diversify?)`
    本番検索（ハイブリッド＋比較型マルチクエリ＋リランカー）でランク済チャンク＋メタを返す
    （top_k 既定 5・上限 20＝バッチ2 段2 で緩和）。
- `sweep_policy_docs(query, mode?, orgs?, per_org?, since?, until?, field?)`
    団体横断の多段検索（core/multistage.py の決定論実装＝バッチ2 段2 でツールに昇格）。
    網羅（coverage＝団体別 fan-out）と集約/最上級（aggregation＝全社走査→確定 or 棄却根拠）を
    1 コールで返す。mode=auto は設問の形から自動選択。
- `list_orgs()`  … コーパスの団体コードと鮮度（多段検索で「団体ごとに叩く」ための一覧）。

■ 公式コネクタ要件（docs/公式コネクタ要件.md §0 #5・#7）
全ツールに `title`＋`ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)`、
サーバに `instructions`（何を返し・何を返さないか）。説明文は「何をするか・いつ使うか」だけに保つ
（利用者 Claude への振る舞い指示・他ツールへの干渉・宣伝は書かない＝審査で却下される）。

■ 機密の物理遮断（最重要）
検索ツール（`search_policy_docs`・`sweep_policy_docs`）は **layer 引数を持たない**。検索は
`PolicySearchService` を通じて常に公開層固定で走り（sweep の多段も内部は同サービスのみを呼ぶ）、
境界フェイルクローズで非公開を落とす（`recommendations.core.search_api`）。
＝外部（MCP クライアント＝Claude）から機密を要求する術は存在しない。

■ stdio 安全
MCP は stdout を JSON-RPC 伝送路に使う。transformers/llama_index の英語 `print()`
（MockLLM 等）が混ざるとプロトコルが壊れるため、起動時に実 stdout をトランスポート
専用に確保し、ライブラリ print は stderr に逃がす（`polyarchy_common.logsetup.guard_stdout_for_stdio`）。
ログは全て日本語で stderr に出す。

起動：
    python -m recommendations.serving.mcp_server
    # あるいは MCP クライアント（Claude Code 等）の設定から stdio 起動（.mcp.json 参照）
"""
import json
from typing import Optional

# ── ノイズ抑制 & stdio 保護は「重い import より前」に行う（print 混入を根本で断つ）。
from polyarchy_common.logsetup import (
    configure_quiet_logging,
    get_logger,
    guard_stdout_for_stdio,
)
from polyarchy_common.mcp_http import serve_streamable_http
# Phase 12：実クエリの永続捕捉（config/log_setup のみ依存＝軽量・重い import を引かない）。
from recommendations.core.coverage import (
    COVERAGE_NOTE,
    COVERAGE_WARN_MIN_COUNT,
    COVERAGE_WARNING,
    org_freshness,
)
from recommendations.core.orgs import ORG_DESCRIPTIONS, ORG_DISPLAY_ORDER
from recommendations.core.query_capture import capture_query
from polyarchy_common.taxonomy import POLICY_TAGS

configure_quiet_logging()

SERVER_NAME = "polyarchy-policy-search"

# 団体コード（list_orgs の表示順。1 表は recommendations.core.orgs＝検索側の ORG_MENTIONS と同一集合）。
ORG_CODES = ORG_DISPLAY_ORDER
FIELD_VOCAB = "／".join(POLICY_TAGS)  # 分野の語彙（21 分類）。docstring と hint で同じ文字列を使う


def _field_matches(field: str) -> list[str]:
    """field（部分一致文字列）が統一 21 分類のどのタグに当たるかを返す（applied_filter／hint 用）。
    空＝21 分類に当たらない（発行元タグ field_tags のみで照合される＝ほぼ 0 件になる）。"""
    return [t for t in POLICY_TAGS if field in t]


def _field_parts(field: str, parts: list[str], payload_hint: dict) -> None:
    """applied_filter に分野の照合先を明示し、21 分類に当たらなければ hint に語彙を返す（B22）。"""
    matched = _field_matches(field)
    if matched:
        parts.append(f"分野~{field}→{'|'.join(matched)}")
    else:
        parts.append(f"分野~{field}（21 分類に該当なし＝発行元タグのみ照合）")
        payload_hint["hint"] = (f"field={field!r} は分野の語彙（21 分類）のどれにも部分一致しない。"
                                f"語彙＝{FIELD_VOCAB}。語彙の一部（例 '労働'・'財政'）で指定し直すと絞れる")

# MCP initialize で返すサーバ説明（公式コネクタ要件：何を返し・何を返さないか。振る舞い指示は書かない）。
SERVER_INSTRUCTIONS = (
    "Polyarchy 政策主張DB（recommendations）。経団連・経済同友会・日本商工会議所・連合・政府（骨太方針・"
    "規制改革推進会議・財政制度等審議会）の公表資料（提言・意見・答申・建議・方針・資料）約 3,700 文書"
    "／約 187,000 チャンクを、日本語特化のハイブリッド検索（BM25 語彙＋意味検索→融合→リランカー）で"
    "横断検索する読み取り専用サービス。"
    "ツールは 2 系統＝search_policy_docs（単一トピックの関連度検索・特定団体/文書の深掘り）と "
    "sweep_policy_docs（団体横断の網羅・集約/最上級＝団体ごとの走査と判定材料を 1 コールで返す）。"
    "返すもの＝関連度順のチャンク（本文抜粋）と出典メタ（団体・文書名・発行日・文書種別・分野タグ・"
    "ページ・原文 URL）。"
    "返さないもの＝文章の生成や要約（利用者側で行う）・非公開資料（検索層は公開に固定され layer 引数は"
    "存在しない）・文書全文（抜粋単位）・コーパス外の情報。該当が無ければ results は空で返る。"
    # rev.2（2026-09-07・LLM 側再試験の観測反映）：未収録類型の列挙は「呼ばずに知っている」口実に
    # 転用された（E4 実測）ため落とし、「返り値でのみ判別できる」側だけを残す。
    "経済団体・政府の主張や提言の有無・内容を扱うときは、Web 検索より先にまず search_policy_docs／"
    "sweep_policy_docs で収録を確認する。あるトピックが未収録かどうかは、この返り値（results の空・"
    "coverage_note）でのみ判別できる＝呼ばずに未収録と判断する根拠は存在しない。"
    "本 DB の返り値に含まれない本文は本 DB 由来ではない＝Web 等で補った内容の出典は results の"
    "出典メタと照合して判別できる。"
    "注意＝doc_type=資料 の文書（アンケート結果・政党への公開質問状の回答・政策比較 等）は団体自身の"
    "提言ではなく転載・調査物であり、発言主体は本文と title で判別する。"
    # rev.3（2026-09-08・B19 遡及拡充＝経団連 1990〜2009 を追加）：統合以前の発言主体の注記（案A）。
    "経団連の 2002-05-28（日経連との統合）以前の文書の発言主体は旧・経済団体連合会＝旧経団連である"
    "（労働分野は日経連管轄で、日経連自身の文書は未収録）。"
    "検索語・絞り込み条件・該当件数は品質改善のため 30 日間記録する（会話本文は取得しない。"
    "https://docs.polyarchy.net/privacy）。"
)


def build_server(service):
    """FastMCP サーバを組み立てる（ツールは渡された service を包むだけ）。"""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    # 公式コネクタ要件：全ツールに title＋readOnlyHint（読み取り専用・破壊なし・閉世界＝自前 DB のみ）。
    RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    log = get_logger("polyarchy.mcp")
    # log_level="WARNING": FastMCP は __init__ の configure_logging で root を INFO＋RichHandler に
    # 設定し、低レベルサーバが英語で "Processing request of type ..." を INFO 出力する。WARNING に
    # 上げて黙らせる（自前ロガーは propagate=False なので影響なし）。
    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS, log_level="WARNING")
    # FastMCP の configure_logging が levels をいじった後にも確実に効かせる（冪等）。
    configure_quiet_logging()

    # 収録範囲の明示（recommendations/docs/コーパス収録範囲の明示.md）：団体別鮮度は catalog から
    # 起動時に 1 回計算（コーパスは ingest まで不変＝リクエスト毎の再計算は不要）。
    freshness_all = org_freshness()
    log.info("収集鮮度: %s（警告閾値 count<%d）",
             " ".join(f"{o}={v['last_ingested']}" for o, v in freshness_all.items()) or "catalog なし",
             COVERAGE_WARN_MIN_COUNT)

    @mcp.tool(title="政策文書の横断検索（recommendations）", annotations=RO)
    def search_policy_docs(
        query: str,
        orgs: Optional[list[str]] = None,
        since: Optional[int] = None,
        until: Optional[int] = None,
        field: Optional[str] = None,
        top_k: int = 5,
        # dev では既定 on（実利用で opt-in が自発されない実測を受けての A/B。C3 で正式判断）
        diversify: bool = True,
    ) -> dict:
        """日本の政策主張文書コーパスを検索し、関連度順のチャンク（本文抜粋＋出典メタ）を返す。

        いつ使うか：経済団体・政府が「何をどう主張しているか」を、原文の抜粋と出典（団体・日付・
        原文 URL）つきで知りたいとき。団体間の比較・時系列の変化・特定制度への言及の有無の確認に向く。

        コーパス（全て公開ソースの自動収集・約 3,700 文書／約 187,000 チャンク）：
          経団連(keidanren)・政府(gov: 骨太方針／規制改革推進会議／財政制度等審議会)・連合(rengo)・
          日商(nissho)・経済同友会(doyukai) の提言・意見・答申・建議・方針・資料。1990 年〜現在
          （経団連の 2002-05 以前は旧・経済団体連合会＝日経連との統合前）。

        引数：
          query : 検索クエリ（日本語）。テーマ・制度名・数値・固有名詞に強い（BM25＋意味検索）。
          orgs  : 団体コード列で絞る（例 ["gov","rengo"]）。省略で全団体。
                  コード= keidanren / gov / rengo / nissho / doyukai（一覧は list_orgs）。
          since : この日付以降（YYYYMMDD の整数, 例 20240101）。日付なし文書は除外。
          until : この日付以前（YYYYMMDD の整数）。
          field : 分野の部分一致（例 "労働", "財政"）。照合先は全文書に付与済みの統一 21 分類＝
                  マクロ経済・経済財政運営／税制・財政／金融・資本市場／産業政策・成長戦略／イノベーション・科学技術・スタートアップ／デジタル・情報通信／エネルギー・環境／労働・雇用／人材・教育／社会保障（年金・医療・介護）／こども・子育て・人口／多様性・人権・ジェンダー／企業経営・ガバナンス・サステナビリティ／中小企業／通商・国際経済・国際協力／外交・安全保障・経済安全保障／地域経済・観光・地方創生／国土・住宅・交通・防災／行政改革・規制改革／政治・統治機構・憲法／農林水産・食料
                  （発行元タグ field_tags にも当たれば通す）。当たった分類は applied_filter に明示し、
                  語彙に当たらない指定は hint で語彙を返す。
          top_k : 返却件数（既定 5・上限 20。超過指定は 20 に制限し applied_filter に明示する）。
          diversify : 文書単位の重複抑制（既定 True）＝同一文書は最良チャンク 1 件だけ返し、残り枠を
                  別文書に充てる（同一文書の他のヒット数は same_doc_hits）。特定文書の深掘り・原文精読で
                  同一文書の複数チャンクが欲しいときは False（関連度順そのまま）。

        返り値（dict）：
          query, applied_filter（適用条件の要約。top_k の制限や重複抑制の適用もここに明示）,
          layer（"公開（固定）"）, count,
          coverage_note（コーパスの収録範囲＝未収録の情報類型の一覧）,
          freshness（団体別の収集鮮度 {org: {last_ingested, latest_doc_date}}。orgs 指定時は
            該当団体のみ。last_ingested=最終取込日・latest_doc_date=収録済み最新文書の発行日。
            last_ingested より後の発信はコーパスに未反映）,
          coverage_warning（低ヒット時のみ。未収録領域の可能性の警告）,
          results=[{rank, score, file_name, org, org_type, title, date, date_int,
                    doc_type, field_tags, policy_tags, page, layer, source_url, text, same_doc_hits}, ...]
          policy_tags＝統一 21 分類の分野タグ（最大 3・field の照合先）。field_tags＝発行元の分類語（団体固有）。
          page＝原典 PDF の通し番号（1 始まり）。PDF に印字されたページ番号とは一致しない
            場合がある（表紙・目次分ずれる等）＝原典照合はページ送りの位置で行う。
          該当が無ければ results は空（count=0）。空・低ヒットは「未収録・収集ラグ」の可能性を
          含み、「該当の主張が存在しない」ことの証明にはならない。

        返さないもの：非公開資料（検索層は公開に固定・layer 引数は存在しない）・文書全文（チャンク
        単位の抜粋）・生成文や要約。

        データの性格（解釈に要る事実）：
          - doc_type=資料 の文書（アンケート調査結果・公開質問状への各政党の回答・政党の政策比較 等）は
            団体自身の提言ではなく転載・調査物。発言主体は本文と title で判別できる。
          - 単一の検索は関連度で 1 団体に偏りやすい。「各団体はどう述べているか」（網羅）や
            「最初に／唯一 提言した団体はどこか」（集約・最上級）のような団体横断の設問は、
            sweep_policy_docs が 1 コールで団体別の走査と判定材料を返す。
        """
        # top_k は上限 TOP_K_MAX（既定20・バッチ2 段2 で 5→20 に緩和）へ明示クランプ。
        # 黙って切り詰めると呼び出し側が「それ以降は存在しない」と誤認し得るため、
        # 制限は applied_filter で必ず開示する。既定値は従来どおり 5（eval アンカーも top_k=5）。
        from recommendations.core.config import TOP_K_MAX
        requested_k = top_k
        top_k = max(1, min(top_k, TOP_K_MAX))
        log.info("検索要求: クエリ=%r 団体=%s 期間=%s..%s 分野=%s top_k=%d%s 重複抑制=%s 層=公開固定",
                 query, "・".join(orgs) if orgs else "全", since or "-", until or "-",
                 field or "-", top_k,
                 f"（要求{requested_k}を制限）" if requested_k != top_k else "",
                 "文書単位" if diversify else "なし")
        chunks = service.search(query, orgs=orgs, since=since, until=until,
                                field=field, top_k=top_k, diversify=diversify)
        results = [c.to_dict() for c in chunks]
        # 適用条件の要約（describe と同趣旨・層は必ず公開）。
        parts = []
        if orgs:
            parts.append("団体=" + "|".join(orgs))
        if since is not None:
            parts.append(f"以降={since}")
        if until is not None:
            parts.append(f"以前={until}")
        extra: dict = {}
        if field:
            _field_parts(field, parts, extra)
        if requested_k != top_k:
            parts.append(f"top_k={requested_k}→{top_k}に制限（上限{TOP_K_MAX}）")
        if diversify:
            parts.append("重複抑制=文書単位（同一文書は最良チャンク1件）")
        parts.append("層=公開（固定）")
        applied = ", ".join(parts)
        log.info("検索応答: %d 件返却（層=公開固定）%s", len(results),
                 " 団体内訳=" + "・".join(f"{c['org']}:{c['file_name']}" for c in results)
                 if results else "")
        # Phase 12【捕捉点】：実クエリを永続 JSONL に best-effort 追記（fail-open・stdout非汚染）。
        # 質問→検証文パイプライン（トリアージ→ゴールド構築→ゲート→ユーザー由来 eval）の原資。
        capture_query(
            query, orgs=orgs, since=since, until=until, field=field, top_k=top_k,
            result_count=len(results),
            top_files=[c["file_name"] for c in results],
            top_orgs=[c["org"] for c in results],
            source="mcp",
        )
        # 収録範囲の明示：coverage_note は全件・freshness は orgs 指定時に該当団体へ絞る。
        # 低ヒット（count < 閾値）時のみ coverage_warning を追加（常時は出さない＝caveat fatigue 対策）。
        freshness = ({o: freshness_all[o] for o in orgs if o in freshness_all}
                     if orgs else freshness_all)
        payload = {
            "query": query,
            "applied_filter": applied,
            "layer": "公開（固定・機密は返しません）",
            "count": len(results),
            "coverage_note": COVERAGE_NOTE,
            "freshness": freshness,
            "results": results,
        }
        if len(results) < COVERAGE_WARN_MIN_COUNT:
            payload["coverage_warning"] = COVERAGE_WARNING
        payload.update(extra)
        return payload

    # バッチ2 段2（2026-08-28）：多段検索（recommendations/core/multistage.py の決定論実装＝
    # multistage_eval でゲート済）を公開ツールに昇格。従来は説明文経由でクライアントの Claude が
    # 10〜30 回検索して実現していた網羅・集約を、サーバ側 1 コールで返す。
    from recommendations.core.multistage import ALL_ORGS, MultiStageSearcher
    ms = MultiStageSearcher(service)
    # sweep の応答予算：結果は per_org×走査団体数まで・上限 15 チャンク（無制限にはしない）。
    SWEEP_PER_ORG_MAX = 5
    SWEEP_RESULTS_MAX = 15

    @mcp.tool(title="政策文書の団体横断スイープ（多段検索・recommendations）", annotations=RO)
    def sweep_policy_docs(
        query: str,
        mode: str = "auto",
        orgs: Optional[list[str]] = None,
        per_org: int = 3,
        since: Optional[int] = None,
        until: Optional[int] = None,
        field: Optional[str] = None,
    ) -> dict:
        """複数団体を 1 団体ずつ走査し、団体ごとの代表チャンクと横断の判定材料を 1 コールで返す。

        いつ使うか：「各団体はどう述べているか」（網羅）や「最初に／唯一／最も早く提言したのは
        どこか」（集約・最上級）のように、複数団体の突き合わせが要る設問。単一トピックの関連度
        検索・特定団体/文書の深掘りは search_policy_docs。

        引数：
          query   : 検索クエリ（日本語）。
          mode    : "auto"（既定）＝設問の形から戦略を自動選択／"coverage"＝団体別 fan-out
                    （各団体の代表チャンクを均等合流＝関連度勝ちの 1 団体偏りを消す）／
                    "aggregation"＝全団体走査→最古言及日の突き合わせで団体を確定するか、
                    確定できない根拠を返す。設問型が分かっているときは明示指定が確実。
          orgs    : 走査する団体コード列（省略で全 5 団体）。コードは list_orgs。
          per_org : 団体あたりの取得数（既定 3・上限 5）。
          since/until : YYYYMMDD の整数（含む）。 field : 分野の部分一致（語彙・照合先は
                    search_policy_docs と同じ＝統一 21 分類。当たった分類は applied_filter に明示）。

        返り値（dict）：
          query, mode, strategy（実際に使った戦略。auto は targeted/baseline＝通常検索に倒れる
          こともあり、その場合も本ツールだけで完結する）, applied_filter, layer, count,
          decision/rationale（aggregation のみ・材料より前に置く。decision="resolve"＝団体を確定・
            "abstain"＝確定不可＝この判定が結論で、後続の日付や件数から順位を再構成しない。
            rationale は根拠の日本語文で、そのまま回答・棄却の根拠に使える。answer_org は
            resolve のときのみ付く＝abstain に確定団体は存在しない）,
          org_coverage（団体別の走査結果。covered=false はその団体の文書に該当が無かったことを
            示す。hits＝その団体からの取得件数〔≤per_org〕。形は戦略で異なる＝
            aggregation：[{org, covered, top_score, hits, earliest_date, earliest_file}...]／
            それ以外（coverage・auto の targeted/baseline 含む）：[{org, covered, top_score,
            hits}...]（earliest 系は付かない）。
            earliest_date＝取得した per_org 件〔関連度上位〕の中での最古日であり、その団体の
            コーパス内最古の言及日ではない（per_org を上げると早まり得る）＝「どこが最初か」の
            確定根拠には使えない参考値）,
          results（合流チャンク列・最大 per_org×走査団体数・上限 15）, coverage_note, freshness。
          覆いのない団体・低ヒットは「未収録・収集ラグ」の可能性を含み、「主張が存在しない」
          ことの証明にはならない。

        返さないもの：非公開資料（検索層は公開に固定・layer 引数は存在しない）・文書全文・
        生成文や要約。
        """
        if mode not in ("auto", "coverage", "aggregation"):
            raise ValueError("mode は auto / coverage / aggregation のいずれかを指定してください")
        if orgs:
            unknown = [o for o in orgs if o not in ORG_CODES]
            if unknown:
                raise ValueError(f"未知の団体コード {unknown}。有効なコード＝{list(ORG_CODES)}")
        requested_per_org = per_org
        per_org = max(1, min(per_org, SWEEP_PER_ORG_MAX))
        scope = tuple(orgs) if orgs else ALL_ORGS
        merged_cap = min(per_org * len(scope), SWEEP_RESULTS_MAX)

        log.info("スイープ要求: クエリ=%r mode=%s 団体=%s per_org=%d 期間=%s..%s 分野=%s 層=公開固定",
                 query, mode, "・".join(scope), per_org, since or "-", until or "-", field or "-")
        scan = None
        if mode == "coverage":
            strategy = "coverage"
            chunks = ms.coverage_search(query, top_k=merged_cap, per_org=per_org, orgs=scope,
                                        since=since, until=until, field=field)
            scanned = scope
        elif mode == "aggregation":
            strategy = "aggregation"
            scan = ms.aggregation_scan(query, per_org=per_org, orgs=scope,
                                       since=since, until=until, field=field)
            chunks = scan["chunks"]
            scanned = scope
        else:  # auto：classify_strategy（core/multistage.py）で采配
            st = ms.orchestrate(query, top_k=merged_cap, per_org=per_org,
                                since=since, until=until, field=field,
                                orgs_scope=tuple(orgs) if orgs else ())
            strategy, chunks, scan, scanned = st.strategy, st.chunks, st.scan, st.scanned_orgs

        results = [c.to_dict() for c in chunks]

        # 団体別の走査結果。aggregation は evidence（最古言及つき）、それ以外は結果から導出。
        if scan is not None:
            org_cov = [{
                "org": e.org, "covered": e.covered,
                "top_score": round(e.top_score, 4), "hits": e.hits,
                "earliest_date": e.earliest_date, "earliest_file": e.earliest_file,
            } for e in scan["evidence"]]
        else:
            cov_orgs = scanned or scope
            by_org: dict[str, list[dict]] = {}
            for c in results:
                by_org.setdefault(c["org"], []).append(c)
            org_cov = [{
                "org": o, "covered": o in by_org,
                "top_score": max((c["score"] for c in by_org.get(o, [])), default=0.0),
                "hits": len(by_org.get(o, [])),
            } for o in cov_orgs]

        parts = [f"mode={mode}→戦略={strategy}", "団体=" + "|".join(scope)]
        if since is not None:
            parts.append(f"以降={since}")
        if until is not None:
            parts.append(f"以前={until}")
        extra: dict = {}
        if field:
            _field_parts(field, parts, extra)
        if requested_per_org != per_org:
            parts.append(f"per_org={requested_per_org}→{per_org}に制限（上限{SWEEP_PER_ORG_MAX}）")
        else:
            parts.append(f"per_org={per_org}")
        if scan is not None:
            # M1：earliest_date の実態を応答内で開示（フィールド名が「最古」を名乗る罠の緩和）。
            parts.append("earliest_date=per_org 件中の最古（コーパス内最古の保証なし）")
        parts.append(f"結果上限={merged_cap}")
        parts.append("層=公開（固定）")
        applied = ", ".join(parts)
        log.info("スイープ応答: 戦略=%s %d 件返却（層=公開固定） 団体幅=%d%s",
                 strategy, len(results), len({c["org"] for c in results}),
                 f" 判定={scan['decision']}" if scan else "")

        # Phase 12【捕捉点】：sweep 1 コールにつき 1 行（内部の団体別サブ検索は記録しない＝
        # KPI の二重計上防止）。source で通常検索と区別できるようにする。
        capture_query(
            query, orgs=orgs, since=since, until=until, field=field, top_k=merged_cap,
            result_count=len(results),
            top_files=[c["file_name"] for c in results],
            top_orgs=[c["org"] for c in results],
            source="mcp_sweep",
        )

        freshness = {o: freshness_all[o] for o in scope if o in freshness_all}
        payload = {
            "query": query,
            "mode": mode,
            "strategy": strategy,
            "applied_filter": applied,
            "layer": "公開（固定・機密は返しません）",
            "count": len(results),
        }
        # M2：判定（decision/rationale）は材料（org_coverage/results）より前に置く＝後置だと
        # 読み飛ばされる実測（Haiku 4.5 が abstain を読んだうえで断定）。answer_org は resolve の
        # ときだけ付ける＝abstain に「確定団体」は存在しない（None のキーを出さない）。
        if scan is not None:
            payload["decision"] = scan["decision"]
            if scan["answer_org"]:
                payload["answer_org"] = scan["answer_org"]
            payload["rationale"] = scan["rationale"]
        payload.update({
            "org_coverage": org_cov,
            "coverage_note": COVERAGE_NOTE,
            "freshness": freshness,
            "results": results,
        })
        if len(results) < COVERAGE_WARN_MIN_COUNT:
            payload["coverage_warning"] = COVERAGE_WARNING
        payload.update(extra)
        return payload

    @mcp.tool(title="収録団体の一覧（recommendations）", annotations=RO)
    def list_orgs() -> dict:
        """コーパスに収録されている団体・政府のコードと名称を返す。

        いつ使うか：search_policy_docs / sweep_policy_docs の orgs 引数に渡すコードを確認する
        とき、および field 引数に渡す分野の語彙を確認するとき。引数なし。
        返り値＝{orgs:[{code, name, last_ingested, latest_doc_date}...], fields:[統一 21 分類の分野
        タグ], coverage_note, note}。last_ingested=最終取込日・latest_doc_date=収録済み最新文書の
        発行日（YYYYMMDD）。文書本文や件数は返さない。
        """
        log.info("団体一覧の要求")
        return {"orgs": [{"code": c, "name": ORG_DESCRIPTIONS[c],
                          **freshness_all.get(c, {})} for c in ORG_CODES],
                "fields": list(POLICY_TAGS),
                "coverage_note": COVERAGE_NOTE,
                "note": "層は常に公開固定。単発検索は search_policy_docs・団体横断は sweep_policy_docs。"}

    return mcp, log


def main() -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    from recommendations.core.search_api import PolicySearchService

    # ① 実 stdout をトランスポート専用に確保（以後のライブラリ print は stderr へ）。
    real_stdout = guard_stdout_for_stdio()
    log = get_logger("polyarchy.mcp")
    log.info("MCPサーバ起動中: 名称=%s（検索スタック読み込み・BM25索引構築に十数秒）", SERVER_NAME)

    # ② 本番検索スタックを 1 度だけ構築（層は公開固定）。
    service = PolicySearchService()

    # ③ FastMCP を組み、実 stdout でトランスポートを起動（JSON-RPC は実 stdout、ノイズは stderr）。
    mcp, log = build_server(service)
    log.info("MCPサーバ待受開始: 公開ツール=[search_policy_docs, sweep_policy_docs, list_orgs] "
             "層=公開固定（機密は物理遮断）")

    async def _serve() -> None:
        async with stdio_server(stdout=anyio.wrap_file(real_stdout)) as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream, write_stream,
                mcp._mcp_server.create_initialization_options(),
            )

    anyio.run(_serve)


def main_http(host: str = "127.0.0.1", port: int = 8765, path: str = "/mcp") -> None:
    """Streamable HTTP で待ち受ける（リモート公開用・stdio とは別モード・Phase 13+）。

    Cloudflare Tunnel 経由で `mcp.<ドメイン>` に出し、**Cloudflare Access の Managed OAuth**
    で認証する構成の"原点"。既定は **127.0.0.1 バインド**＝ローカル/トンネル経由のみ到達可
    （直接インターネットには晒さない）。ツールは stdio と同一＝**層は公開固定＋フェイルクローズ**
    のまま（機密は物理遮断）。HTTP は stdout をプロトコルに使わないので stdio 保護は不要。

    ※認証（Cf-Access-Jwt-Assertion 検証）は Cloudflare Access 設定後に追加する多層防御（Phase 2）。
      一次ゲートは Cloudflare Access のエッジ遮断（未認証は origin に届かない）。
    """
    from recommendations.core.search_api import PolicySearchService

    configure_quiet_logging()
    log = get_logger("polyarchy.mcp")
    log.info("MCPサーバ起動中(HTTP): 検索スタック読み込み（BM25索引構築に十数秒）")
    service = PolicySearchService()
    mcp, log = build_server(service)

    def _health() -> dict:
        # 生き死に判定（運用設計 §1.1）＝Qdrant へ live に count を取る（qdrant 死亡を検出）。
        from recommendations.core.qdrant_store import QdrantCorpusStore
        n = QdrantCorpusStore().count(service.collection_name)
        return {"ok": n > 0, "chunks": n, "collection": service.collection_name}

    serve_streamable_http(
        mcp, host=host, port=port, path=path, health_check=_health,
        tools_desc="公開ツール=[search_policy_docs, sweep_policy_docs, list_orgs] "
                   "層=公開固定（機密は物理遮断）")


if __name__ == "__main__":
    import argparse
    import os

    _p = argparse.ArgumentParser(
        description="Polyarchy MCP サーバ（既定=stdio・--http でリモート公開用の Streamable HTTP）")
    _p.add_argument("--http", action="store_true", help="Streamable HTTP で待ち受け（リモート公開用）")
    _p.add_argument("--host", default=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"))
    _p.add_argument("--port", type=int, default=int(os.environ.get("MCP_HTTP_PORT", "8765")))
    _p.add_argument("--path", default=os.environ.get("MCP_HTTP_PATH", "/mcp"))
    _a = _p.parse_args()
    if _a.http:
        main_http(_a.host, _a.port, _a.path)
    else:
        main()  # stdio（既定・.mcp.json / mcp_smoke はこちら＝不変）
