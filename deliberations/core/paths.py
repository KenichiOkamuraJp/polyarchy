"""置き場（原文・中間物・評価）。原文と中間物は git 外（S3 が原本）。"""
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PKG_DIR / "data"
RAW_DIR = DATA_DIR / "raw"            # 原文（PDF・HTML）＝<org>/<回 2 桁>/<ファイル名>
CACHE_DIR = DATA_DIR / "cache"        # 抽出・解析の中間物
MANIFEST = RAW_DIR / "manifest.jsonl"  # 目録（1 行 1 件：議事次第ページ・資料・記録・非公開の名前だけの資料）
UNITS = CACHE_DIR / "units.jsonl"      # 検索の単位（スライド 1 ページ・1 発言 など）＝M4 で埋め込む
DOCUMENTS = CACHE_DIR / "documents.jsonl"  # 文書ごとの解析結果（提出者・記録の型・抽出の質）
EVAL_DIR = PKG_DIR / "eval"
