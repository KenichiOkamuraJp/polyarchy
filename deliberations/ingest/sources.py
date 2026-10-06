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
)

BY_ORG = {s.org: s for s in SOURCES}

# 取得の作法（開発計画 §7）：既定は curl の UA・1 リクエストごとに 1 秒以上あける。
# 例外＝cas.go.jp は curl の UA に 404 を返す（UA の文字列で弾く型）＝ブラウザの UA。
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BROWSER_UA_HOSTS = ("www.cas.go.jp",)
REQUEST_INTERVAL = 1.2  # 秒
