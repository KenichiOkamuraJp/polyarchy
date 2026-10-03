"""M0 の確認シート（確認シート.md）を、同じフォルダの JSON 2 本から作り直す。

python deliberations/eval/make_sheet.py   # JSON を直したら作り直す（シートを手で直さない）
"""
import json
from pathlib import Path

D = Path(__file__).resolve().parent
A=json.load(open(D / "anchor_questions.json", encoding="utf-8")); L=json.load(open(D / "attribution_labels.json", encoding="utf-8"))
e=lambda s:(s or "").replace("|","｜").replace("\n"," ")
out=["# M0 確認シート（第 1 便の評価問・帰属ラベルの下書き）","",
"> **状態：記録（下書きの確認用）。** 作成 2026-10-03。正本は同じフォルダの `anchor_questions.json`・`attribution_labels.json`。本表はそこから機械で作った一覧（手で直すときは JSON 側を直して作り直す）。",
"> 根拠の引用は PyMuPDF `get_text()` の抽出文字列そのまま（行の途中で切れている＝原文の改行位置）。パスは `deliberations/data/raw/` から。",
"> 確認のしかた＝各行の「確認」欄に ✓／✗／保留を書く。区分は会議での役割（開発計画 §4.1・2026-10-03 改定）＝政務／事務局／府省・会議体／構成員（政府外）／外部（ヒアリング）／不明。","",
f"## 1. 検索のアンカー問（{len(A['questions'])} 問）","",
"型の内訳："+"・".join(f"{k}＝{v}" for k,v in A['meta']['型の内訳'].items())+"／会議体の内訳："+"・".join(f"{k}＝{v}" for k,v in A['meta']['会議体の内訳'].items()),"",
"| ID | 型 | 問 | 正解（ファイル・ページ・資料番号） | 発言者／提出者（区分） | 根拠の引用 | 要確認 | 確認 |","|---|---|---|---|---|---|---|---|"]
for q in A["questions"]:
  x=q["expected"]; ev=q["evidence"]
  if q["type"].startswith("D"):
    ans="初出 "+x["first_date"]+f" {x['org']} 第{x['session_no']}回："+"／".join(f"`{a['path']}`（{a['material_no'] or '番号なし'}）" for a in x["acceptable_paths"])
    who="—"
  else:
    ans=f"`{x['path']}` p{x['page']}（{x['material_no'] or x.get('doc_kind','')}）"
    who=f"{x.get('speaker') or x.get('presenter')}（{x.get('speaker_role') or x.get('presenter_type')}）"
  quote="<br>".join(f"p{v['page']}「{e(' / '.join(v['quote']))}」" for v in ev)
  out.append(f"| {q['id']} | {q['type'][0]} | {e(q['question'])} | {ans} | {e(who)} | {quote} | {'**要確認**：'+e(q['要確認']) if q['要確認'] else ''} | |")
out+=["","型の記号＝A 政務・回次の特定／B 名前つき要約・逐語（発言者の特定）／C 匿名の要約（発言者は不明で返るべき）／D 決定文書との往復（第 1 便の中での初出の回と資料）。D の正解は初出の回にその語を含む全ファイル（どれを返しても可）＝発言者は正解にしない（同じ回で触れた人は acceptable_speakers）。","",
f"## 2. 帰属ラベル：資料（{len(L['materials'])} 件・要確認 {L['meta']['資料_要確認']}）","",
"| ID | ファイル | 一覧の資料名 | presenter_type | presenter | 当たった規則 | 表紙の根拠の行 | 要確認 | 確認 |","|---|---|---|---|---|---|---|---|---|"]
for m in L["materials"]:
  out.append(f"| {m['id']} | `{m['path']}` {m['material_no']} | {e(m['material_name_in_list'])} | {m['presenter_type']} | {e(m['presenter'])} | {e(m['rule'])} | {e(m.get('cover_evidence') or '')} | {'**要確認**：'+e(m['要確認']) if m['要確認'] else ''}{('（'+e(m['note'])+'）') if m['note'] else ''} | |")
out+=["",f"## 3. 帰属ラベル：発言（名前あり {len(L['utterances_named'])} 件・要確認 {L['meta']['発言_要確認']}）","",
"| ID | ファイル・ページ | speaker | speaker_role | 当たった規則（見出し） | 引用 | 要確認・注 | 確認 |","|---|---|---|---|---|---|---|---|"]
for u in L["utterances_named"]:
  out.append(f"| {u['id']} | `{u['path']}` p{u['page']}{'–'+str(u['quote_pages'][-1]) if u.get('quote_pages') else ''} | {e(u['speaker'])} | {u['speaker_role']} | {e(u['rule'])}：{e(u['heading_in_text'])} | {e(' / '.join(u['quote']))} | {'**要確認**：'+e(u['要確認']) if u['要確認'] else ''}{('（'+e(u['note'])+'）') if u['note'] else ''} | |")
out+=["",f"## 4. 帰属ラベル：匿名の要約（{len(L['utterances_anonymous'])} 件・speaker＝不明）","",
"| ID | ファイル・ページ | 当たった規則 | 直前の見出し | 引用 | 確認 |","|---|---|---|---|---|---|"]
for u in L["utterances_anonymous"]:
  out.append(f"| {u['id']} | `{u['path']}` p{u['page']} | {e(u['rule'])} | {e(u['heading_in_text'])} | {e(' / '.join(u['quote']))} | |")
open(D / "確認シート.md", "w", encoding="utf-8").write("\n".join(out)+"\n")
print(len(out))
