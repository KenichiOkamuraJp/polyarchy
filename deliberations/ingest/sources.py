"""
取得元（会議体）の定義。会議体を足すときはここに 1 件足し、評価問を先に足す（開発計画 §9 の M7）。

org＝会議体のコード（`org` 欄・フォルダ名）。index＝回の一覧ページ（回のページと、回のページには
載らない記録〔議事録・議事要旨・議事概要〕の両方をここから見つける）。session_href＝一覧の中の
回のページの href（グループ 1＝回次）。record_href＝一覧の中の記録の href（グループ 1＝回次）。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    org: str
    name: str          # 会議体の正式名（返り値に出す）
    ministry: str      # 所管（移管があれば回ごとに上書きする）
    index: str
    session_href: str
    record_href: str
    batch: int         # 便（開発計画 §3.2）
    secretariat: tuple[str, ...] = ()  # その会議の事務局（記録の出席者欄の【事務局】で確かめた名前だけを書く）
    # ヒアリングの会議（出席者が委員等・事務局・関係者〔府省と外部の団体〕に分かれる）。委員・事務局・府省の型に
    # 当たらない提出者・発言者（「団体名（氏名 役職）」・「…提出資料」）を外部（ヒアリング）と読む（attribution.py）
    hearing: bool = False
    # 記録の書式。""＝第 1 便の型（○・【】・箇条）。"paren"＝デジタル庁の議事要旨（「◼ 議事」の後に
    # 「(生田目構成員)本文」「(事務局)本文」が箇条の記号の行〔⚫・➢・o〕と交互に並ぶ）＝records.py
    record_style: str = ""
    # 同じ案が回をまたいで何度も配られる会議体（日本成長戦略＝ロードマップ案 324 ページが 3 回）。先に配られた回と
    # 同じ文面のページは検索の単位にしない（文書は目録・list_meeting に残る・直されたページは残る＝build.py）
    dedupe_pages: bool = False
    # ── 第 4 便（2026-11-14）で足した設定。どれも既定のままなら第 1〜3 便の会議体の動きは変わらない ──
    # 一覧が年度ごとのページに分かれる会議体の 2 枚目以降（CSTI）。手元には _index2.html… で置く
    more_index: tuple[str, ...] = ()
    # 一覧の記録のリンクの文字が「PDF形式：…KB」だけの会議体＝record_href に合うリンクを記録とし、名前は一覧の表の
    # 列の見出しの語（「議事要旨」「議事概要」）にする（原典に書かれた語だけを使う）
    record_label: str = ""
    # これより前の回は取らない（範囲＝2024 年以降。session_is_date の会議体は開催日 YYYYMMDD）
    min_session: int = 0
    # 回の番号が無い会議体（CSTI の有識者議員懇談会＝回の URL が開催日）。session_no は開催日 YYYYMMDD の整数
    # （原典に無い通し番号を作らない）。記録の href の 6 桁（YYMMDD）は 20YYMMDD に読む
    session_is_date: bool = False
    # 回のページにあっても目録に入れない資料（正規表現・資料名と番号に当てる）。CSTI 本会議は前の回の「議事録（案）」が
    # 後の回の参考資料に載る＝回を取り違える（確定版は一覧の記録にある）
    skip_material: str = ""
    # その会議体で構成員（政府外）と読む肩書の末尾（CSTI の有識者議員「○宮園議員」・産構審の「○染谷委員長」）。既定の型に
    # 足す。「委員長」「議員」を既定に入れない＝「国家公安委員会委員長」「衆議院議員」のような別の意味がある
    member_titles: tuple[str, ...] = ()
    # 表紙の提出者が、人・研究機関・府省の局課の行だけで書かれる会議体（ムーンショットの PD・大学・国研・府省の課）
    # ＝attribution.by_cover_people が表紙の上の方を読む
    cover_people: bool = False
    # 記録の出席者欄に事務局の節（「(事務局)」「〈事務局〉」）がある会議体＝官職だけの発言者（「○嶋崎参事官」）を、その節に
    # 名前が書かれていれば事務局と読む（書かれていなければ従来どおり府省・会議体）
    attendee_sections: bool = False
    # 表紙に書かれる会議の略称（「CSTI本会議」「戦略推進会議」）＝会議の名であって提出者ではない
    meeting_aliases: tuple[str, ...] = ()
    # 記録の見出し・発言者の行で、名前つきの発言者と読む肩書の末尾（区分は決めない＝attribution の規則のまま）。CSTI 本会議に
    # 招いた説明者「【浅川館長】」「【橋本理事長】」。既定に入れない（ほかの会議体の記録の切り方を変えない）
    named_titles: tuple[str, ...] = ()


SOURCES: tuple[Source, ...] = (
    Source("dgk", "デジタル行財政改革会議", "内閣官房",
           "https://www.cas.go.jp/jp/seisaku/digital_gyozaikaikaku/index.html",
           r"/digital_gyozaikaikaku/kaigi(\d+)/gijishidai\1\.html$",
           r"/digital_gyozaikaikaku/pdf/kaigi(\d+)_gijiroku\.pdf$", 1),
    Source("ai_hq", "人工知能戦略本部", "内閣府",
           "https://www8.cao.go.jp/cstp/ai/ai_hq/kaisai.html",
           r"/ai_hq/(\d+)kai/\1kai\.html$", r"/ai_hq/(\d+)kai/[^/]+\.pdf$", 1,
           ("内閣府科学技術・イノベーション推進事務局",)),  # 議事概要の出席者欄（第 1・3・5 回）
    Source("ai_senmon", "人工知能戦略専門調査会", "内閣府",
           "https://www8.cao.go.jp/cstp/ai/ai_expert_panel/ai_expert_panel.html",
           r"/ai_expert_panel/(\d+)kai/\1kai\.html$", r"/ai_expert_panel/(\d+)kai/[^/]+\.pdf$", 1,
           ("内閣府科学技術・イノベーション推進事務局",)),  # 議事概要の【事務局】（第 1〜4・6 回）
    Source("ai_senryaku", "AI戦略会議", "内閣府",
           "https://www8.cao.go.jp/cstp/ai/ai_senryaku/ai_senryaku.html",
           r"/ai_senryaku/(\d+)kai/\1kai\.html$", r"/ai_senryaku/(\d+)kai/[^/]+\.pdf$", 1),
    # 規制改革推進会議の WG は会議の一覧（全 WG・全期）にだけ記録が載る＝一覧はそのページ。回のフォルダは開催日（YYMMDD）
    Source("kisei_ai_wg", "規制改革推進会議 デジタル・AIワーキング・グループ", "内閣府",
           "https://www8.cao.go.jp/kisei-kaikaku/kisei/meeting/meeting.html",
           r"/wg/2501_06ai/\d{6}/ai(\d+)_agenda\.html$", r"/wg/2501_06ai/\d{6}/ai(\d+)_minutes\.pdf$", 2,
           ("内閣府規制改革推進室",), hearing=True),  # 議事録の出席者欄の（事務局）（第 1〜10 回）
    # デジタル庁の会議は回の URL に回次が無い（uuid）＝session_href に括弧の組が無いときは、リンクの文字列
    # 「第N回」から回次を読む（collect.py）。記録は回のページにある（一覧には無い）
    Source("da_ai_board", "先進的AI利活用アドバイザリーボード", "デジタル庁",
           "https://www.digital.go.jp/councils/ai-advisory-board",
           r"/councils/ai-advisory-board/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", r"(?!)", 3,
           ("デジタル庁",), record_style="paren"),  # 議事要旨の出席者欄の「(2) デジタル庁」＝開催する庁
    # 日本成長戦略本部（閣僚）と日本成長戦略会議（首相が議長・有識者と閣僚）は同じ一覧ページ。記録は一覧にだけ載る。
    # 会議は同じ回に議事録（逐語）と議事要旨の両方がある＝議事録のある回の議事要旨は検索の単位にしない（collect.py の superseded）
    Source("seicho", "日本成長戦略会議", "内閣官房",
           "https://www.cas.go.jp/jp/seisaku/nipponseichosenryaku/index.html",
           r"/nipponseichosenryaku/kaigi/dai(\d+)/gijis(?:h)?idai\.html$",
           r"/nipponseichosenryaku/kaigi/dai(\d+)/[^/]+\.pdf$", 3, dedupe_pages=True),
    Source("seicho_honbu", "日本成長戦略本部", "内閣官房",
           "https://www.cas.go.jp/jp/seisaku/nipponseichosenryaku/index.html",
           r"/nipponseichosenryaku/honbu/dai(\d+)/gijis(?:h)?idai\.html$",
           r"/nipponseichosenryaku/honbu/dai(\d+)/[^/]+\.pdf$", 3, dedupe_pages=True),
    # ── 第 4 便（2026-11-14・科学技術・イノベーションと防衛産業の会議体・2024 年以降＝deliberations/docs/第4便_計画.md）──
    # CSTI 本会議：一覧は年度ごと（giji_r5〜r8）。回のページの綴りが揺れる（haihui085/haihu-085・haihu-086/haihu-086）。
    # 記録は一覧の「議事要旨」の列（リンクの文字は「PDF形式：…」だけ）。第 71 回（2024-02-20）から
    Source("csti", "総合科学技術・イノベーション会議", "内閣府",
           "https://www8.cao.go.jp/cstp/giji/giji_r8.html",
           r"/siryo/haihui?-?(\d{3})/haihu-\d{3}\.html$", r"/giji/giji-si(\d{3})\.pdf$", 4,
           ("内閣府科学技術・イノベーション推進事務局",),
           more_index=("https://www8.cao.go.jp/cstp/giji/giji_r7.html", "https://www8.cao.go.jp/cstp/giji/giji_r6.html",
                       "https://www8.cao.go.jp/cstp/giji/giji_r5.html"),
           record_label="議事要旨", min_session=71, skip_material=r"議事録\s*[（(]\s*案\s*[）)]",
           record_style="heading", member_titles=("議員",), named_titles=("館長", "理事長", "会長"),
           meeting_aliases=("CSTI本会議", "総合科学技術・イノベーション会議本会議")),
    # CSTI 有識者議員懇談会（木曜会合）：一覧は年度ごと。回の URL は開催日（回次が無い）。同じ一覧の「…_seisaku.html」は
    # 日本学術会議の在り方に関する政策討議（別の会合）＝入れない。記録は一覧の「議事概要」の列。記録の href は
    # 「YYYYMMDD/giji_YYYYMMDD.pdf」「YYYYMMDD/giji_YYMMDD.pdf」「giji_YYMMDD.pdf」（フォルダなし）の 3 つの型
    Source("csti_yusiki", "総合科学技術・イノベーション会議 有識者議員懇談会", "内閣府",
           "https://www8.cao.go.jp/cstp/gaiyo/yusikisha/index2026.html",
           r"/gaiyo/yusikisha/(\d{8})\.html$", r"/gaiyo/yusikisha/(?:(\d{8})/giji(?:_\d{6,8})?|giji_(\d{6}))\.pdf$", 4,
           ("内閣府科学技術・イノベーション推進事務局",),
           more_index=("https://www8.cao.go.jp/cstp/gaiyo/yusikisha/index2025.html",
                       "https://www8.cao.go.jp/cstp/gaiyo/yusikisha/index2024.html",
                       "https://www8.cao.go.jp/cstp/gaiyo/yusikisha/index2023.html"),
           record_label="議事概要", min_session=20240101, session_is_date=True,
           member_titles=("議員",), attendee_sections=True),
    # ムーンショット型研究開発制度に係る戦略推進会議：一覧（news.html）に全回。記録は回のページ
    Source("moonshot", "ムーンショット型研究開発制度に係る戦略推進会議", "内閣府",
           "https://www8.cao.go.jp/cstp/moonshot/news.html",
           r"/senryakusuishin/(\d+)(?:st|nd|rd|th)/\1(?:st|nd|rd|th)\.html$", r"(?!)", 4,
           ("内閣府科学技術・イノベーション推進事務局",), min_session=12, attendee_sections=True, cover_people=True, record_style="split_mark",
           meeting_aliases=("戦略推進会議", "ムーンショット型研究開発制度に係る戦略推進会議（懇談会）")),
    # 経済産業省（www.meti.go.jp＝ブラウザの名乗り・SLOW_HOSTS）。記録は回のページ。自動の収集は 1 回に数本まで＝取り切れない分は
    # 運営者がブラウザで保存したファイル（data/raw/<org>/<回 2 桁>/_page.html と PDF）から --offline で目録を作る（第 4 便の計画 §3）
    # イノベーション小委員会：2024-02-09 の第 1 回から番号を振り直している（前身の研究開発・イノベーション小委員会は別の一覧）
    Source("meti_inno", "産業構造審議会 イノベーション小委員会", "経済産業省",
           "https://www.meti.go.jp/shingikai/sankoshin/sangyo_gijutsu/innovation/index.html",
           r"/sangyo_gijutsu/innovation/(\d{3})\.html$", r"(?!)", 4,
           member_titles=("委員長",), record_style="anon_marks", cover_people=True),
    Source("meti_defense", "防衛産業ワーキンググループ", "経済産業省",
           "https://www.meti.go.jp/shingikai/mono_info_service/defense_industry_wg/index.html",
           r"/defense_industry_wg/(\d{3})\.html$", r"(?!)", 4, record_style="anon_marks", cover_people=True),
)

BY_ORG = {s.org: s for s in SOURCES}

# 取得の作法（開発計画 §7）：既定は curl の UA・1 リクエストごとに 1 秒以上あける。
# 例外＝cas.go.jp は curl の UA に 404 を返す（UA の文字列で弾く型）＝ブラウザの UA。
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BROWSER_UA_HOSTS = ("www.cas.go.jp", "www.meti.go.jp")
REQUEST_INTERVAL = 1.2  # 秒
# 既定より慎重に取るホスト（2026-11-14）。経済産業省は UA の文字列で弾き（curl の名乗りに 403）、続けて取ると AWS WAF の
# JS チャレンジ（HTTP 202・x-amzn-waf-action）を返す＝前の要求から interval 秒あけ、1 回の実行で budget 本まで。チャレンジは
# 解かない＝出たらそのホストへはその実行の間は送らない（ほかのホストの収集は続ける）。取らなかった分は収集の最後に一覧で示す
# （data/raw/_collect_report.json）＝時間をおいて再実行するか、運営者がブラウザで保存して --offline で目録を作る。
# 2026-11-14 の実測＝120 秒おきで 8 本（一覧 2・報告のページ 3・PDF 3）はチャレンジなし。
SLOW_HOSTS = {"www.meti.go.jp": {"interval": 120.0, "budget": 6}}
