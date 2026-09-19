"""
文書ローダ（カタログ join・層分類・status フィルタ）＝取込の純関数部。

バッチ2 段4（2026-08-28・Chroma 全廃）：Chroma への格納 CLI（build_index/main）を削除し、
本モジュールは load_catalog / classify_layer / load_documents の共有部のみになった。
取込の本線は `recommendations.ingest.qdrant_ingest`（doc 単位増分・v7 経路）＝そちらが本モジュールを使う。
"""
import csv
import sys
from pathlib import Path

from llama_index.core import SimpleDirectoryReader
from llama_index.readers.file import PyMuPDFReader

from recommendations.core.config import DATA_DIR, PDF_DIR

# カタログ（PDF↔メタデータ対応表）。Phase4_収集_メタデータ設計.md §2.2 準拠。
CATALOG = DATA_DIR / "catalog.csv"
# 生成(LLM)時に文脈として見せるメタデータ。それ以外はLLMにもembeddingにも入れない。
LLM_VISIBLE_METADATA = {"title", "org", "date", "source_url"}
# 投入を許すステータス（needs_ocr / excluded / html_thin は落とす）。uncataloged=カタログ
# 未登録（後方互換で通す）。html_text=HTML本文をテキスト抽出した .txt（判断B の主機構）。
INGEST_OK_STATUS = {"ok", "uncataloged", "html_text"}


def load_catalog():
    """data/catalog.csv を file_name -> 行dict で読み込む（無ければ空）。"""
    if not CATALOG.exists():
        return {}
    with CATALOG.open(encoding="utf-8") as f:
        return {r["file_name"]: r for r in csv.DictReader(f)}


def _has_provenance(meta: dict) -> bool:
    """自動収集の証跡（source_url かつ retrieved_at）を持つか。collect.py 由来なら真。"""
    return bool((meta.get("source_url") or "").strip()
                and (meta.get("retrieved_at") or "").strip())


def classify_layer(meta: dict) -> str:
    """Phase 10：provenance ベースの層分類（§25.4・フェイルクローズ）。

    ルール（採用）：
      - 明示的に layer="機密" の指定は尊重する（誤って公開化しない）。
      - 自動収集の証跡（source_url かつ retrieved_at＝collect.py 由来）があれば **公開**。
      - 証跡が無い（手動で足した／カタログ未登録）ものは **機密**（既定・フェイルクローズ）。

    誤分類しても"隠す側"に倒れる。自動収集は公開ソース限定なので、この規則で機密漏れは起きない。
    prod=policy_docs_v4 は全 540 行が provenance 付き→全て公開（本規則で再現・不変）。
    本規則は将来の取り込みにのみ効き、既存 v4 は再構築しない。
    """
    if (meta.get("layer") or "").strip() == "機密":
        return "機密"
    return "公開" if _has_provenance(meta) else "機密"


def load_documents(input_files=None):
    """PDF_DIR 配下のPDFをページ単位のドキュメントとして読み込み、カタログのメタデータを付与する。

    - `file_metadata` フックで各PDFにカタログ行を join（ノードは親docのメタを継承 → チャンク単位フィルタ可）。
    - メタデータは埋め込みから除外（Phase 3 ベンチと同条件＝生本文のみ）。LLM可視は §LLM_VISIBLE のみ。
    - layer=='機密' の混入を検知したら中断（データ層分離の仕組み的担保）。
    - status が INGEST_OK_STATUS 以外（needs_ocr/excluded）の文書は投入対象から除外。
    - input_files を渡すとそのファイルだけを読む（qdrant_ingest の doc 単位増分用。
      既定 None は従来どおり PDF_DIR 全体＝既定経路の挙動不変）。
    """
    if input_files is None and not (PDF_DIR.exists() and
                                    (any(PDF_DIR.rglob("*.pdf")) or any(PDF_DIR.rglob("*.txt")))):
        sys.exit(f"ERROR: {PDF_DIR} にPDF/テキスト文書が見つかりません")

    catalog = load_catalog()

    def file_metadata(path: str) -> dict:
        row = catalog.get(Path(path).name)
        if row is None:
            # カタログ未登録＝provenance 無し＝手動足し扱い → 機密（フェイルクローズ・§25.4）。
            # 下の層アサーションで検知され、公開コレクションへの取り込みは中断される。
            return {"file_name": Path(path).name, "org": "unknown",
                    "layer": "機密", "status": "uncataloged"}
        meta = {k: v for k, v in row.items() if v != ""}
        # 層は catalog の値をそのまま使わず provenance で機械判定（§25.4）。全 540 行は
        # 証跡付き→公開（既存 v4 と一致）。証跡なき手動追加は機密に倒れる。
        meta["layer"] = classify_layer(meta)
        return meta

    print("文書を読み込み中...")
    # .pdf は PyMuPDF、.txt は既定リーダ（HTML本文抽出済みテキスト＝判断B の主機構）。
    source = (dict(input_dir=str(PDF_DIR), required_exts=[".pdf", ".txt"], recursive=True)
              if input_files is None else dict(input_files=[str(p) for p in input_files]))
    documents = SimpleDirectoryReader(
        **source,
        file_extractor={".pdf": PyMuPDFReader()},
        file_metadata=file_metadata,
    ).load_data()

    # 埋め込み・LLM 可視範囲の制御（§2.3）。
    for d in documents:
        keys = list(d.metadata.keys())
        d.excluded_embed_metadata_keys = keys
        d.excluded_llm_metadata_keys = [k for k in keys if k not in LLM_VISIBLE_METADATA]

    # 層アサーション（§25.4 フェイルクローズ）: 公開コレクションへの取り込みは公開層のみ許す。
    # provenance 無し（手動足し／カタログ未登録）は classify_layer で機密に倒れ、ここで中断する
    # ＝証跡のない文書が公開コレクションへ紛れ込むことを仕組みで防ぐ。機密文書を扱う場合は
    # 別コレクション／別経路で（本番の公開コレクションには入れない）。
    leaked = sorted({d.metadata.get("file_name") for d in documents
                     if d.metadata.get("layer") == "機密"})
    if leaked:
        sys.exit("ERROR: 機密層（provenance 無し）の文書が公開コレクションの投入対象に含まれています。"
                 f"カタログに source_url/retrieved_at を付与するか投入対象から外してください: {leaked}")

    # status フィルタ。
    before = len(documents)
    documents = [d for d in documents
                 if d.metadata.get("status", "ok") in INGEST_OK_STATUS]
    dropped = before - len(documents)

    uncataloged = sorted({d.metadata.get("file_name") for d in documents
                          if d.metadata.get("status") == "uncataloged"})
    if uncataloged:
        print(f"  ! カタログ未登録（uncataloged）: {len(uncataloged)}ファイル {uncataloged[:5]}"
              f"{' …' if len(uncataloged) > 5 else ''}")
    if dropped:
        print(f"  ! status除外（needs_ocr/excluded）で {dropped}ページ分を投入対象外に")
    print(f"  → {len(documents)}件のドキュメント（ページ）を読み込み")
    return documents
