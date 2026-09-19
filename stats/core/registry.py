"""
統計系列レジストリ（発見層のメタデータ＋参照層の取得手段）。設計は `docs/参照粒度設計.md`。

- 系列メタデータは polyarchy_common.metadata_core の共通コア8欄を必ず持つ（corpus="stats"・layer="公開"）。
- 分野タグは polyarchy_common.taxonomy.POLICY_TAGS の語彙のみ。
- series_id は `<org>.<dataset>.<measure>[.<dims>].<freq>[.<region_level>]`（§1）。
- 値そのものは持たない（`core.values` の値ストア）。派生（仮想）エントリは `components` を持ち値を持たない。
- 既定レジストリは `stats/data/registry/series.jsonl`（1行=1系列）から読む。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from polyarchy_common.metadata_core import PUBLIC_LAYER, validate_payload
from polyarchy_common.taxonomy import is_valid_tag

from stats.core.paths import REGISTRY_PATH
from stats.core.periods import FREQS

CORPUS = "stats"

SECTORS = ("マクロ", "企業", "家計・労働", "物価・金融", "財政", "人口", "対外・国際")
KINDS = ("observation", "projection")
REGION_LEVELS = ("", "pref", "city", "cty")
ORG_NAMES = {
    "cao": "内閣府", "mof": "財務省", "boj": "日本銀行", "soumu": "総務省", "mhlw": "厚生労働省",
    "meti": "経済産業省", "nta": "国税庁", "ipss": "国立社会保障・人口問題研究所", "jpx": "日本取引所グループ",
    "imf": "IMF", "oecd": "OECD", "wb": "世界銀行", "keidanren": "経団連", "rengo": "連合",
    "bls": "米国労働統計局（BLS）", "eurostat": "欧州連合統計局（Eurostat）",
}
# dataset ごとの「分析の定石」（式・系列 ID・定義＝値は含まない）。目録（list_datasets）に載せて利用者 Claude に届ける。
from stats.core.dataset_notes import (CAO_GAP_NOTES as _CAO_GAP_NOTES, FCS_NOTES as _FCS_NOTES, FOF_NOTES as _FOF_NOTES,  # noqa: E402
                                       CPI2025_NOTES as _CPI25_NOTES, GAP_ANALYSIS_NOTES as _GAP_NOTES, MAIKIN_NOTES as _MAIKIN_NOTES, MITOSHI_NOTES as _MITOSHI_NOTES,
                                       ROUDOU_EMP_NOTES as _REMP_NOTES, SHOKUGYO_NOTES as _SHOKUGYO_NOTES,
                                       SDBS_ANALYSIS_NOTES as _SDBS_NOTES, SHAHO_NOTES as _SHAHO_NOTES,
                                       SNA_ACTIVITY_NOTES as _SNA_ACT_NOTES, SNA_SECTOR_BS_NOTES as _SEC_BS_NOTES,
                                       SNA_SECTOR_NOTES as _SEC_NOTES, STAN_ANALYSIS_NOTES as _STAN_NOTES)
from stats.core.hojin_vocab import HOJIN_ANALYSIS_NOTES as _HOJIN_NOTES  # noqa: E402
DATASET_ANALYSIS_NOTES = {"hojin": _HOJIN_NOTES, "sna_activity": _SNA_ACT_NOTES, "sdbs": _SDBS_NOTES, "stan": _STAN_NOTES,
                          "gap": _GAP_NOTES, "cao_gap": _CAO_GAP_NOTES, "sna_sector": _SEC_NOTES, "sna_sector_bs": _SEC_BS_NOTES,
                          "fof": _FOF_NOTES, "sna_fcs": _FCS_NOTES, "roudou_emp": _REMP_NOTES, "shaho": _SHAHO_NOTES,
                          "maikin": _MAIKIN_NOTES, "cpi2025": _CPI25_NOTES, "shokugyo": _SHOKUGYO_NOTES, "mitoshi": _MITOSHI_NOTES}

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9_\-]*){3,5}$")
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")


@dataclass(frozen=True)
class Series:
    """統計系列（＝1つの指標×調査主体×粒度×基準）。値は持たない。"""
    series_id: str
    title: str
    org: str
    source_url: str
    date: str = ""                 # 系列の最終更新日（取込の vintage）YYYY-MM-DD
    date_int: Optional[int] = None
    lang: str = "ja"
    policy_tags: tuple[str, ...] = ()
    unit: str = ""
    granularity: str = ""          # 表示用（年度・月次 …）
    region_level: str = ""         # "" 全国のみ／pref／city／cty
    accessor: dict = field(default_factory=dict)   # 取得手段の雛形（§6。period/region を除く）
    notes: str = ""
    # --- 拡張（§7） ---
    sector: str = ""
    kind: str = "observation"
    dataset: str = ""
    measure: str = ""
    dims: str = ""
    freq: str = ""
    basis: str = ""
    seasonal_adjustment: bool = False
    region_codes: tuple[str, ...] = ("JP",)
    first_period: str = ""
    last_period: str = ""
    stat_name: str = ""
    table_id: str = ""
    table_title: str = ""
    org_name: str = ""
    projection_by: str = ""
    edition: str = ""
    scenario: str = ""
    superseded_by: str = ""
    supersedes: str = ""
    components: tuple[str, ...] = ()
    citation_template: str = ""
    period_converter: str = ""     # 取得元の時間コード→正規表記の変換器名（core.periods.CONVERTERS）
    status: str = "registered"     # registered / planned（取込未実装）/ guide（値を持たず読み方だけ案内＝再配布条件により発見層のみ）
    license: dict = field(default_factory=dict)   # 再配布・商用利用条件（docs/再配布条件.md）：{"grade": "◎|○|△", "terms": 規約名, "url": 規約URL, "note": 条件}
    # --- 品質メタ（第 6 弾 段 3・データ拡充計画.md §4a）---
    # breaks：時系列の断層。各要素 {"period": 断層が入る最初の期（制度上の適用期）, "kind": classification|population|accounting|coverage,
    #   任意 "affects_from"/"affects_to": 影響が及びうる幅（早期適用〜移行完了。無ければ period の 1 期のみ）,
    #   "label": 何が起きたか, "treatment": separate_series|level_shift|exclude_ratio|note_only, "note": 使い方, "source": 典拠}
    #   kind＝断層の性質（利用側が機械判定するための語彙）：
    #   classification＝分類改定（別系列に分けるのが原則）／population＝母集団・標本推計の変更（水準補正で接続可）／
    #   accounting＝会計基準・企業形態の変更（売上と原価の対応が変わる＝**比率は期間を跨いで比較不可**・補正不能）／
    #   coverage＝表章範囲・区分の新設（注記のみ）。根拠は原典注記・制度文書（データからの推定は入れない）。
    breaks: tuple[dict, ...] = ()
    # usable_from：分類改定に由来する「連続利用できる最初の期」（空＝first_period と同じ）。会計断層は usable_from を動かさず breaks で伝える。
    usable_from: str = ""

    @property
    def is_derived(self) -> bool:
        return bool(self.components)

    @property
    def scope(self) -> str:
        """収録の範囲＝`jp`（日本の統計・全国／都道府県）か `intl`（国際比較＝国別 ISO3・IMF/OECD/世銀）。
        2026-08-23：国際比較は明示的に求められたときだけ要る、という利用側の実態に合わせ、発見層で分けられるようにした。"""
        return "intl" if self.region_level == "cty" else "jp"

    def payload(self) -> dict:
        """共通コア＋stats 拡張の payload（発見層に載せる形）。"""
        d = asdict(self)
        d["scope"] = self.scope
        d["policy_tags"] = list(self.policy_tags)
        d["region_codes"] = list(self.region_codes)
        d["components"] = list(self.components)
        d["breaks"] = [dict(b) for b in self.breaks]
        d.update({"corpus": CORPUS, "layer": PUBLIC_LAYER,
                  "org_name": self.org_name or ORG_NAMES.get(self.org, self.org)})
        return d

    def violations(self) -> list[str]:
        errs = validate_payload(self.payload())
        errs += [f"policy_tags: 語彙外 {t!r}" for t in self.policy_tags if not is_valid_tag(t)]
        if not self.series_id:
            errs.append("series_id: 空")
        elif not _ID_RE.match(self.series_id):
            errs.append(f"series_id: 命名規則違反 {self.series_id!r}")
        else:
            parts = self.series_id.split(".")
            if parts[0] != self.org:
                errs.append(f"series_id: 先頭 {parts[0]!r} が org {self.org!r} と不一致")
            if self.freq and self.freq not in parts:
                errs.append(f"series_id: freq {self.freq!r} が含まれない")
        if self.sector and self.sector not in SECTORS:
            errs.append(f"sector: 語彙外 {self.sector!r}")
        if self.kind not in KINDS:
            errs.append(f"kind: 語彙外 {self.kind!r}")
        if self.freq and self.freq not in FREQS:
            errs.append(f"freq: 語彙外 {self.freq!r}")
        if self.region_level not in REGION_LEVELS:
            errs.append(f"region_level: 語彙外 {self.region_level!r}")
        if self.kind == "projection" and not (self.projection_by and self.edition):
            errs.append("kind=projection には projection_by と edition が必須")
        if not self.is_derived and not self.accessor.get("type"):
            errs.append("accessor.type が必要（派生エントリ以外）")
        if self.license and self.license.get("grade") not in ("◎", "○", "△"):
            errs.append(f"license.grade は ◎/○/△ のいずれか（{self.license.get('grade')!r}）")
        for c in self.components:
            if not _ID_RE.match(c):
                errs.append(f"components: 命名規則違反 {c!r}")
        return errs


def _from_dict(d: dict) -> Series:
    d = dict(d)
    for k in ("policy_tags", "region_codes", "components", "breaks"):
        if k in d and isinstance(d[k], list):
            d[k] = tuple(d[k])
    d.pop("scope", None)  # payload の導出欄（region_level から決まる）＝保存形に混ざっていても無視
    known = {f for f in Series.__dataclass_fields__}
    extra = set(d) - known
    if extra:
        raise ValueError(f"未知の欄 {sorted(extra)}（series_id={d.get('series_id')}）")
    return Series(**d)


@dataclass
class Registry:
    series: dict[str, Series] = field(default_factory=dict)

    def register(self, s: Series) -> None:
        self._agg_pos = None
        errs = s.violations()
        if errs:
            raise ValueError(f"系列 {s.series_id!r} は共通契約違反: {errs}")
        if s.series_id in self.series:
            raise ValueError(f"系列 {s.series_id!r} は重複")
        self.series[s.series_id] = s

    def get(self, series_id: str) -> Optional[Series]:
        return self.series.get(series_id)

    # dims の語彙で「集計区分」を表す語＝並び順で上位に置く（業種・規模とも全体＝0 段、細分ほど後ろ）。
    AGGREGATE_TOKENS = frozenset({"", "allexfin", "allsize", "all", "total", "total_activities"})  # total_activities＝STAN・sna_activity の全活動（2026-08-23）

    @staticmethod
    def split_dims(dims: str) -> tuple[str, str]:
        """dims `<業種>-<規模>` を最初の `-` で分割（業種スラグは `_` 区切りで `-` を含まない）。1 トークンなら (dims, "")。"""
        a, _, b = (dims or "").partition("-")
        return a, b

    def find(self, query: str = "", *, tags: Optional[list[str]] = None,
             org: Optional[str] = None, sector: Optional[str] = None,
             kind: Optional[str] = None, freq: Optional[str] = None,
             dataset: Optional[str] = None, industry: Optional[str] = None,
             size: Optional[str] = None, scope: Optional[str] = None, limit: int = 20) -> list[Series]:
        """発見層の検索（部分一致・ファセット絞り込み）。search() の先頭 limit 件。"""
        return self.search(query, tags=tags, org=org, sector=sector, kind=kind, freq=freq,
                           dataset=dataset, industry=industry, size=size, scope=scope, limit=limit)[0]

    @staticmethod
    def tokenize(query: str) -> list[str]:
        """発見層のクエリを語に分ける＝空白（全角含む）・読点・中黒で区切り、さらに**英数字と和字の境界**で切る
        （第 9 弾 S-4・2026-08-29：「実質GDP」「GDPギャップ」等の融合複合語が単一トークンの部分一致に失敗し 0 件になっていた。
        分割後の AND は融合語の部分一致より常に弱い述語＝該当が減ることはない）。正規化はしない（決定的・原典の表記のまま）。"""
        import re
        raw = [t for t in re.split(r"[\s\u3000、,，・/／]+", (query or "").strip()) if t]
        out: list[str] = []
        for t in raw:
            parts = re.findall(r"[0-9A-Za-z._%-]+|[^0-9A-Za-z._%-]+", t)
            for p in (parts if len(parts) > 1 else [t]):
                if p not in out:
                    out.append(p)
        return out

    def token_hits(self, query: str, **facets) -> dict[str, int]:
        """語ごとの**単独**該当数（同じファセットで）。0 件応答の診断用＝「語を減らせば当たる 0」と「本当に無い 0」を利用者が区別できる。"""
        return {t: self.search(t, limit=0, **facets)[1] for t in self.tokenize(query)}

    def search(self, query: str = "", *, tags: Optional[list[str]] = None,
               org: Optional[str] = None, sector: Optional[str] = None,
               kind: Optional[str] = None, freq: Optional[str] = None,
               dataset: Optional[str] = None, industry: Optional[str] = None,
               size: Optional[str] = None, scope: Optional[str] = None, limit: int = 20) -> tuple[list[Series], int]:
        """発見層の検索＝(先頭 limit 件, 該当総数)。並びは決定的：
        ① dims の集計度（業種・規模とも全体＝先、細分ほど後。第 6 弾で系列が 1,000 超になっても全体系列が押し出されないため。
           同じ dataset に全体/細分の区別が無い dims は影響しない）
        ② query が表題・measure・系列IDに当たるもの → notes/表名だけに当たるもの
        ③ scope＝jp（日本の統計）が intl（国際比較・国別）より先（2026-08-23：国際比較は明示的に求められたときだけ要る。scope=intl で絞れば逆に国際だけ）
        ④ series_id。industry/size は dims `<業種>-<規模>` の各トークンへの完全一致フィルタ（法人企業統計等）。scope は jp|intl の完全一致フィルタ。"""
        tokens = [t.casefold() for t in self.tokenize(query)]  # 照合は大小無視（表示用の語は tokenize が正）
        hits: list[tuple[tuple, Series]] = []
        for s in self.series.values():
            if org and s.org != org:
                continue
            if dataset and s.dataset != dataset:
                continue
            if sector and s.sector != sector:
                continue
            if kind and s.kind != kind:
                continue
            if freq and s.freq != freq:
                continue
            if tags and not set(tags) & set(s.policy_tags):
                continue
            if scope and s.scope != scope:
                continue
            ind, sz = self.split_dims(s.dims)
            if industry and ind != industry:
                continue
            if size and sz != size:
                continue
            if tokens:
                # 空白区切りの語の AND（2026-08-22 S-1：クエリ丸ごとの部分一致だと複数語が常に 0 件＝偽の「ない」）。
                # 英字は大小を区別しない（casefold＝決定的。第 9 弾 S-5・2026-08-29：「GDP」が series_id の gdp_real に
                # 当たらず、表題が「実質国内総生産」の年次 SNA 実質GDPが「実質GDP」で引けなかった）。
                primary = " ".join([s.title, s.series_id, s.measure]).casefold()
                secondary = " ".join([s.notes, s.stat_name, s.table_title]).casefold()
                if all(t in primary for t in tokens):
                    strength = 0
                elif all(t in primary or t in secondary for t in tokens):
                    strength = 1
                else:
                    continue
            else:
                strength = 0
            # 旧基準（superseded 系列を含む dataset＝68SNA/93SNA 等）は同順位で最後尾群（2026-08-29 find_quality「純貸出」＝
            # sna2000 が ID 昇順で現行 sna2020 より前に並び top20 を占有していた）。dataset 単位で判定する＝
            # 後継の無い行（不突合 等）も一緒に下がり、将来の基準改定（新 dataset）でも自動で効く。
            hits.append(((self._specificity(s), strength, s.dataset in self._legacy_datasets(), s.scope == "intl", s.series_id), s))
        hits.sort(key=lambda x: x[0])
        return [s for _, s in hits[:limit]], len(hits)

    FAMILY_THRESHOLD = 3  # 同一 dataset+measure+freq の細分がこれを超えたら 1 枚のファミリーカードに畳む

    def coverage_of(self, org: str, dataset: str, measure: str, freq: str) -> Optional[dict]:
        """ある measure の**収録済み dims の組合せ**（S-3・2026-08-22）。unknown_series の診断用＝
        「measure 自体が無い」と「measure はあるが業種×規模の組合せが無い」を区別し、後者は収録済みの組合せを返す。無ければ None。"""
        members = [x for x in self.series.values() if x.org == org and x.dataset == dataset and x.measure == measure and x.freq == freq]
        if not members:
            return None
        from stats.core.hojin_vocab import INDUSTRY_ORDER, SIZE_ORDER
        ind_sizes: dict[str, set[str]] = {}
        for m in members:
            a, b = self.split_dims(m.dims)
            ind_sizes.setdefault(a, set()).add(b)
        by_sizes: dict[tuple[str, ...], list[str]] = {}
        for a in sorted(ind_sizes, key=lambda x: (INDUSTRY_ORDER.get(x, 10**6), x)):
            key = tuple(sorted(ind_sizes[a], key=lambda x: (SIZE_ORDER.get(x, 10**6), x)))
            by_sizes.setdefault(key, []).append(a)
        combos = [{"sizes": [k for k in key if k], "industries": v} for key, v in by_sizes.items()]
        return {"measure": measure, "series_count": len(members), "combinations": combos}

    def diagnose_unknown(self, series_id: str) -> Optional[dict]:
        """unknown_series の診断。ID が `<org>.<dataset>.<measure>.<dims>.<freq>` の形で measure が収録済みなら coverage を返す。"""
        parts = series_id.split(".")
        if len(parts) < 5:
            return None
        org, dataset, measure, freq = parts[0], parts[1], parts[2], parts[-1]
        cov = self.coverage_of(org, dataset, measure, freq)
        if cov is None:
            return None
        cov["requested_dims"] = ".".join(parts[3:-1])
        return cov

    # 畳み軸の宣言（第 9 弾・2026-08-29）：dataset ごとに増殖軸が違う＝hojin 等は dims（下の汎用畳み）・
    # qe2020 は measure（{item}×{variant} の直積）・cao_gap は freq（同一指標の q/a/fy）。
    VARIANT_DATASETS = ("qe2020",)
    FREQ_DATASETS = ("cao_gap",)
    FREQ_GRANULAR = {f: i for i, f in enumerate(("d", "m", "q", "fq", "h", "fy", "a", "irr"))}  # 細かい順（行に残す優先）

    def search_collapsed(self, query: str = "", *, limit: int = 20, **facets) -> tuple[list[Series], list[dict], int]:
        """発見層の応答形＝(series 行, families, 該当総数)。畳み軸は dataset ごとに宣言（増殖軸が違うため）：
        **dims 軸（既定・全 dataset）**：industry/size の指定が無いとき、同一 dataset+measure+freq の細分（dims に細分トークン）が
        FAMILY_THRESHOLD を超えたら 1 枚のファミリーカード（id_pattern＋dims 語彙）に畳む。全体系列（全産業・全規模）は行に残す。
        **variant 軸（VARIANT_DATASETS＝qe2020）**：measure＝{item}_{variant} の直積＝同一 variant の需要項目群を 1 枚に畳み、
        GDP（全体に相当）は行に残す。**freq 軸（FREQ_DATASETS＝cao_gap）**：同一指標の周期違いを畳み、最も細かい周期を行に残す。
        **畳まない**：industry/size 指定（dims 軸）・freq 指定（freq 軸）＝細分が欲しいという意思表示。
        limit は series 行に効く（families は件数が少ないので全部返す）。"""
        expand = bool(facets.get("industry") or facets.get("size"))
        hits, total = self.search(query, limit=10**9, **facets)
        if expand:
            return hits[:limit], [], total
        groups: dict[tuple[str, str, str], list[Series]] = {}
        for s in hits:
            if self._specificity(s) > 0:
                groups.setdefault((s.dataset, s.measure, s.freq), []).append(s)
        collapsed: set[str] = set()
        families: list[dict] = []
        for key, members in groups.items():
            if len(members) <= self.FAMILY_THRESHOLD:
                continue
            collapsed.update(m.series_id for m in members)
            families.append(self._family_card(members))
        # variant 軸（qe2020）：同一 (variant, dims, freq) の需要項目群を畳む。GDP は行に残す（全体系列に相当）。
        from stats.core.dim_vocab import split_measure_variant
        vgroups: dict[tuple[str, str, str, str], list[tuple[str, Series]]] = {}
        for s in hits:
            if s.dataset not in self.VARIANT_DATASETS or s.series_id in collapsed:
                continue
            item, variant = split_measure_variant(s.measure)
            if item is None:
                continue
            vgroups.setdefault((s.dataset, variant, s.dims, s.freq), []).append((item, s))
        for vkey, vmembers in vgroups.items():
            nonrep = [(i, s) for i, s in vmembers if i != "gdp"]
            if len(nonrep) < 2:
                continue
            collapsed.update(s.series_id for _i, s in nonrep)
            families.append(self._variant_card(vkey, vmembers, nonrep))
        # freq 軸（cao_gap）：同一 (measure, dims) の周期違いを畳む。最も細かい周期（四半期）を行に残す。
        if not facets.get("freq"):
            fgroups: dict[tuple[str, str, str], list[Series]] = {}
            for s in hits:
                if s.dataset not in self.FREQ_DATASETS or s.series_id in collapsed:
                    continue
                fgroups.setdefault((s.dataset, s.measure, s.dims), []).append(s)
            for fkey, fmembers in fgroups.items():
                if len(fmembers) < 2:
                    continue
                fmembers.sort(key=lambda s: self.FREQ_GRANULAR.get(s.freq, 10**6))
                keep, rest = fmembers[0], fmembers[1:]
                collapsed.update(s.series_id for s in rest)
                families.append(self._freq_card(keep, rest))
        rows = [s for s in hits if s.series_id not in collapsed]
        return rows[:limit], families, total

    def _variant_card(self, key: tuple[str, str, str, str], members: list[tuple[str, "Series"]],
                      nonrep: list[tuple[str, "Series"]]) -> dict:
        from stats.core.dim_vocab import qe_item_label, qe_item_order, qe_variant_label
        dataset, variant, _dims, _freq = key
        m0 = nonrep[0][1]
        items = [{"slug": i, "label": qe_item_label(i), "first_period": s.first_period}
                 for i, s in sorted(nonrep, key=lambda x: qe_item_order(x[0]))]
        pattern = m0.series_id.replace(f".{m0.measure}.", ".{item}_" + variant + ".", 1)
        kept_gdp = any(i == "gdp" for i, _s in members)
        example = pattern.replace("{item}", items[0]["slug"])
        return {
            "kind": "series_family", "axis": "variant", "dataset": dataset, "org": m0.org,
            "variant": variant, "label": qe_variant_label(variant), "freq": m0.freq, "unit": m0.unit,
            "stat_name": m0.stat_name, "id_pattern": pattern, "series_count": len(nonrep), "items": items,
            "applied_filter": (f"family collapse(variant): {qe_variant_label(variant)} の需要項目 {len(nonrep)} 系列 → 1 ファミリー"
                               + ("（GDP は series 行に残す）" if kept_gdp else "")),
            "usage_hint": ("値は id_pattern の {item} に items の slug を埋めた series_id で lookup_statistic を呼ぶ"
                           f"（例 {example}）。複数まとめては lookup_panel に series_ids を列挙。first_period より前は値なし（found=false）。"),
        }

    def _freq_card(self, keep: "Series", rest: list["Series"]) -> dict:
        # freqs は kept（series 行に残した周期）も含む全周期の一覧。series_count は**カードに畳んだ系列数（kept を除く）**＝
        # 応答の truncated 計算（series 行数＋畳み数＝total）と整合させるため（2026-08-29 利用側指摘でカウントの意味を明記）。
        freqs = [{"freq": s.freq, "series_id": s.series_id, "granularity": s.granularity, "first_period": s.first_period,
                  **({"kept_in_series": True} if s.series_id == keep.series_id else {})}
                 for s in [keep] + rest]
        pattern = keep.series_id.rsplit(".", 1)[0] + ".{freq}"
        return {
            "kind": "series_family", "axis": "freq", "dataset": keep.dataset, "org": keep.org,
            "measure": keep.measure, "unit": keep.unit, "stat_name": keep.stat_name,
            "id_pattern": pattern, "series_count": len(rest), "kept": keep.series_id, "freqs": freqs,
            "applied_filter": f"family collapse(freq): 同一指標の周期違い {len(freqs)} 系列 → 行 1（最も細かい {keep.freq}）＋畳み {len(rest)}",
            "usage_hint": ("series_count＝カードに畳んだ系列数（kept_in_series の行は series 側に残っているため含まない）。"
                           "各周期の値は freqs の series_id で lookup_statistic を呼ぶ。"
                           "特定の周期だけ一覧したいときは find_statistics に freq=a 等を指定する（畳まずに返す）。"),
        }

    def _family_card(self, members: list[Series]) -> dict:
        from stats.core.dim_vocab import dim_label, dim_order
        m0 = members[0]
        INDUSTRY_ORDER, SIZE_ORDER = dim_order("industry", m0.dataset), dim_order("size", m0.dataset)
        inds: dict[str, str] = {}
        sizes: set[str] = set()
        for m in members:
            a, b = self.split_dims(m.dims)
            if a not in inds or (m.first_period and m.first_period < inds[a]):
                inds[a] = m.first_period
            if b:
                sizes.add(b)
        ind_list = [{"slug": a, "label": dim_label("industry", a, m0.dataset), "first_period": inds[a]}
                    for a in sorted(inds, key=lambda x: (INDUSTRY_ORDER.get(x, 10**6), x))]
        size_list = [{"slug": b, "label": dim_label("size", b, m0.dataset)} for b in sorted(sizes, key=lambda x: (SIZE_ORDER.get(x, 10**6), x))]
        # 収録済みの組合せ（全組合せがあるとは限らない＝業種×全規模＋全産業×規模 など）を規模集合ごとにまとめて明示
        by_sizes: dict[tuple[str, ...], list[str]] = {}
        ind_sizes: dict[str, set[str]] = {}
        for m in members:
            a, b = self.split_dims(m.dims)
            ind_sizes.setdefault(a, set()).add(b)
        for a in sorted(ind_sizes, key=lambda x: (INDUSTRY_ORDER.get(x, 10**6), x)):
            key = tuple(sorted(ind_sizes[a], key=lambda x: (SIZE_ORDER.get(x, 10**6), x)))
            by_sizes.setdefault(key, []).append(a)
        combos = [{"sizes": list(k), "industries": v} for k, v in by_sizes.items()] if sizes else []
        pattern = m0.series_id.replace(f".{m0.dims}.", ".{industry}-{size}." if sizes else ".{industry}.", 1)
        return {
            "kind": "series_family", "dataset": m0.dataset, "org": m0.org, "measure": m0.measure, "freq": m0.freq,
            "unit": m0.unit, "stat_name": m0.stat_name, "id_pattern": pattern, "series_count": len(members),
            "dims": {"industry": ind_list, **({"size": size_list} if size_list else {})},
            **({"combinations": combos, "combinations_note": "収録済みの業種×規模の組合せ。ここに無い組合せは unknown_series"} if combos else {}),
            "applied_filter": f"family collapse: 細分 {len(members)} 系列 → 1 ファミリー（全体系列は series 行に残す）",
            "usage_hint": ("細分の値は id_pattern に dims の slug を埋めた series_id で lookup_statistic を呼ぶ"
                           "（例 " + pattern.replace("{industry}", ind_list[0]["slug"] if ind_list else "x").replace("{size}", size_list[0]["slug"] if size_list else "") + "）。"
                           "細分を系列一覧で見たいときは find_statistics に industry=<slug> または size=<slug> を指定する（畳まずに返す）。"
                           "first_period より前は値なし（found=false）。"
                           + (f"分析の定石（定義・分類の違い・接続しない軸）は list_datasets の {m0.dataset} の analysis_notes。" if m0.dataset in DATASET_ANALYSIS_NOTES else "")),
        }

    def _legacy_datasets(self) -> set:
        """superseded_by を持つ系列を 1 本でも含む dataset（＝旧基準）。並びの降格に使う。"""
        cache = getattr(self, "_legacy_ds", None)
        if cache is None:
            cache = {x.dataset for x in self.series.values() if x.superseded_by}
            self._legacy_ds = cache
        return cache

    def _specificity(self, s: Series) -> int:
        """dims の細分度＝業種・規模の各位置で「細分トークン」を持つ数。ただし**同じ dataset に集計トークンを持つ系列がある位置だけ**
        数える（qe2020 の `sa` のように語彙に全体/細分の区別が無い dims は 0＝並び順に影響しない）。"""
        cache = getattr(self, "_agg_pos", None)
        if cache is None:
            cache = {}
            for x in self.series.values():
                a, b = self.split_dims(x.dims)
                pos = cache.setdefault(x.dataset, [False, False])
                pos[0] |= a in self.AGGREGATE_TOKENS
                pos[1] |= b in self.AGGREGATE_TOKENS
            self._agg_pos = cache
        a, b = self.split_dims(s.dims)
        pos = cache.get(s.dataset, [False, False])
        return sum(1 for t, has_agg in ((a, pos[0]), (b, pos[1])) if has_agg and t not in self.AGGREGATE_TOKENS)

    def dataset_ids(self) -> frozenset:
        """登録済み dataset（series_id の 2 番目の要素）の集合＝find_statistics の dataset 引数の語彙（語彙外は入口で hint）。"""
        cache = getattr(self, "_dataset_ids", None)
        if cache is None:
            cache = frozenset(s.dataset for s in self.series.values() if s.dataset)
            self._dataset_ids = cache
        return cache

    def catalog(self) -> list[dict]:
        """目録＝dataset 単位の集約（発見層の入口）。値の範囲は values 側で足す。"""
        groups: dict[tuple[str, str], list[Series]] = {}
        for s in self.series.values():
            groups.setdefault((s.org, s.dataset), []).append(s)
        out = []
        for (org, ds), ss in groups.items():
            ss_sorted = sorted(ss, key=lambda x: x.series_id)
            freqs = sorted({x.freq for x in ss if x.freq})
            out.append({
                "dataset": ds, "org": org, "org_name": ss[0].org_name or ORG_NAMES.get(org, org),
                "stat_name": max((x.stat_name for x in ss), key=len, default=""),
                "sector": sorted({x.sector for x in ss if x.sector}),
                "series_count": len(ss),
                "with_status": {"registered": sum(1 for x in ss if x.status == "registered" and not x.is_derived),
                                "derived": sum(1 for x in ss if x.is_derived),
                                "planned": sum(1 for x in ss if x.status == "planned"),
                                "guide": sum(1 for x in ss if x.status == "guide")},
                "kind": sorted({x.kind for x in ss}),
                "freq": freqs, "granularity": sorted({x.granularity for x in ss if x.granularity}),
                "region_level": sorted({x.region_level or "national" for x in ss}),
                "scope": ss[0].scope,  # jp＝日本の統計／intl＝国際比較（国別）
                "units": sorted({x.unit for x in ss if x.unit}),  # 全件（S-2：目録は「無いことの根拠」に使われる＝黙って切らない）
                "basis": sorted({x.basis for x in ss if x.basis}),
                "policy_tags": sorted({t for x in ss for t in x.policy_tags}),
                "example_series": [x.series_id for x in ss_sorted[:4]],
                "license": next((x.license for x in ss if x.license), {}),
                **({"analysis_notes": DATASET_ANALYSIS_NOTES[ds]} if ds in DATASET_ANALYSIS_NOTES else {}),
                "measures": sorted({x.measure for x in ss if x.measure}),  # 全件（S-2・2026-08-22：[:40] で value_added 等 6 件が消えていた）
                "source_url": ss[0].source_url,
            })
        return sorted(out, key=lambda d: (d["sector"][0] if d["sector"] else "", d["org"], d["dataset"]))

    @classmethod
    def load(cls, path: Path = REGISTRY_PATH) -> "Registry":
        reg = cls()
        if not path.exists():
            return reg
        with path.open(encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    reg.register(_from_dict(json.loads(line)))
                except Exception as e:  # noqa: BLE001
                    raise ValueError(f"{path}:{ln}: {e}") from e
        return reg


def default_registry() -> Registry:
    """既定レジストリ＝ `stats/data/registry/series.jsonl`（無ければ空）。"""
    return Registry.load()
