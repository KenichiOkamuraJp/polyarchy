#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# アプリコード（tar）とデータを S3 へ投入する（手元＝Mac で実行。要 AWS CLI＋プロファイル）。
# EC2 の user_data/bootstrap がここから取得する（ロードマップ§2.2 ④「コード＋データは S3 から」）。
#
# 使い方:
#   BUCKET=polyarchy-staging-123456789012 PROFILE=polyarchy-staging \
#     bash deploy/scripts/upload_to_s3.sh
#   （BUCKET は `terraform output -raw s3_bucket` で取得可）
#
# ★秘密は運ばない：tar は git の追跡ファイルだけ（.env は追跡外）。データにも秘密は無い（公開データ＋捕捉ログ）。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

: "${BUCKET:?BUCKET=<terraform output s3_bucket> を指定してください}"
REGION="${REGION:-ap-northeast-1}"
PROFILE="${PROFILE:-}"
DATA_PREFIX="${DATA_PREFIX:-data}"
CODE_KEY="${CODE_KEY:-code/polyarchy.tar.gz}"

# このスクリプトは deploy/scripts/ にある → リポジトリ root はその2つ上（recommendations/ deploy/ docs/ … を含む）。
# tar のトップ名は箱側の展開先（INSTALL_DIR/polyarchy）に合わせて固定で polyarchy/ とする。
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"  # リポジトリ root
APP_BASENAME="polyarchy"                                        # tar 内トップ名（箱の展開先名）

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")

# ★code tar は git の追跡ファイルだけから作る（git archive HEAD＝コミットした版そのもの・2026-10-09）。
#   旧方式（作業フォルダを除外リストで固める）は、.gitignore を読まないため私的なもの・巨大なもの・箱の状態の
#   置き場（社内ノート・.terraform・env の控え・ops/dashboard 等）を事故のたびに除外へ足していた＝追跡していない
#   ものは原理的に入らない形にする。未コミットのコードは配らない（release.sh の配る前の確認が、コードの範囲の
#   未コミットの差分で止める＝測ったコードと配るコードを揃える）。
#   除外＝*/data（データは S3 経由）・env のひな型（箱では使わない＝手元の deploy.sh が読むだけ）。
#   .gitattributes の export-ignore は使わない（公開リポの GitHub のソース zip からも消えてしまう）。
VDIR="$(mktemp -d -t polyarchy-version.XXXXXX)"
# コード版の刻印（箱には .git が無いため tar に VERSION を同梱＝ops_dashboard が表示する）。tar＝HEAD なので --dirty は付けない
git -C "$REPO_DIR" describe --tags --always 2>/dev/null > "$VDIR/VERSION" || echo unknown > "$VDIR/VERSION"
date -Iseconds >> "$VDIR/VERSION"
echo "[upload] コードを tar 化（git archive HEAD＝追跡ファイルだけ・*/data と env のひな型を除く）"
TARBALL="$(mktemp -t polyarchy.XXXXXX)"   # 形式は --format で指定（拡張子に依らない）
git -C "$REPO_DIR" archive --format=tar.gz --prefix="$APP_BASENAME/" --add-file="$VDIR/VERSION" -o "$TARBALL" HEAD -- . \
  ':(exclude,glob)**/data/**' ':(exclude,glob)**/*.env.example'
rm -rf "$VDIR"
# 未コミットのコードは tar に入らない（HEAD を配る）＝release.sh は配る前の確認で止まるが、deploy.sh <env> upload 等の直接の呼び出しは止まらない＝告げる
if [[ -n "$(git -C "$REPO_DIR" status --porcelain -- . ':(exclude)*/data/*' ':(exclude)VERSION')" ]]; then
  echo "[upload] ★コードの範囲に未コミットの差分がある＝tar には入らない（HEAD を配る）。配るならコミットしてから" >&2
fi

echo "[upload] tar → s3://$BUCKET/$CODE_KEY"
"${AWS[@]}" s3 cp "$TARBALL" "s3://$BUCKET/$CODE_KEY" --region "$REGION"
rm -f "$TARBALL"

# ★qdrant ストレージ（data/qdrant）は「サーバ停止中」にしか整合コピーできない（WAL・セグメントが
#   書き換わる）。ローカル qdrant-dev（docker）が :6333 で応答している間は sync を中止する（fail-closed）。
if curl -fsS -m 2 http://localhost:6333/collections >/dev/null 2>&1; then
  echo "[upload] ERROR: ローカル Qdrant(:6333) が稼働中。docker stop qdrant-dev してから再実行（転送後 docker start qdrant-dev）" >&2
  exit 1
fi

echo "[upload] データ同期 → s3://$BUCKET/$DATA_PREFIX/ （Qdrant/BM25語彙/PDF/eval/catalog）"
# 大物＝pdfs(2.0G)/qdrant(2.0G)。バックアップ .bak と eval/results は除外して転送を軽く。
# ローカル data/chroma はバッチ2 段4（2026-08-28）で退避済＝S3 の data/chroma/（v5 保管）は
# --delete を使わないため残る（消さない＝v5 データの最終保管場所）。
# ★query_log は運ばない：S3 の data/query_log/ は箱の fuelsync が書く保護コピーの置き場＝手元の捕捉分で
#   上書き・混入させない。箱は自前の捕捉を data/query_log/ に書く（親 dir は書込時に自動作成・polyarchy_common.capture）。
# ★qdrant ストレージは S3 側も「ミラー」（--delete）＝ローカルで消えた旧 WAL セグメントを S3 に残さない。
#   追記型で残すと新旧世代が混在し、箱側で "missing wal segments" の起動不能になる（2026-09-03 実測）。
#   qdrant ディレクトリに捕捉ログ等の温存対象は無い＝--delete して安全。
"${AWS[@]}" s3 sync "$REPO_DIR/recommendations/data/qdrant/" "s3://$BUCKET/$DATA_PREFIX/qdrant/" --region "$REGION" --delete
"${AWS[@]}" s3 sync "$REPO_DIR/recommendations/data/" "s3://$BUCKET/$DATA_PREFIX/" --region "$REGION" \
    --exclude "qdrant/*" \
  --exclude "*.bak" --exclude "*.bak[0-9]" \
  --exclude "*.phase11*.bak" \
  --exclude "eval/results/*" --exclude "stats/*" --exclude "companies/*" --exclude "deliberations/*" --exclude "query_log/*" \
  --exclude "cache/*"  # 箱で作る（新着チェックの結果 等）＝手元の古い値を S3 に載せると bootstrap の同期が箱の最新を上書きする（2026-10-02）

# stats（統計参照DB）のデータ＝S3 `data/stats/`（共通契約 §4）。registry（git 追跡・2MB）＋values（88MB）＋eval を運ぶ。
# cache/（原本 1.2GB・取込時のみ使用）・values_archive/・query_log/（箱で生成する燃料）は運ばない。
echo "[upload] stats データ同期 → s3://$BUCKET/$DATA_PREFIX/stats/ （registry/values/eval）"
"${AWS[@]}" s3 sync "$REPO_DIR/stats/data/" "s3://$BUCKET/$DATA_PREFIX/stats/" --region "$REGION" \
  --exclude "cache/*" --exclude "values_archive/*" --exclude "query_log/*" --exclude "*.bak"

# companies（企業情報DB）のデータ＝S3 `data/companies/`。値の置き場 store/（約 3.9GB＝経営指標 facts/ 約 1.8GB＋セグメント別 segments/ 約 1.1GB＋横断検索の索引 screen_index/ 約 0.9GB＋地域別 約 90MB・2026-09-30＝第 1d 便の遡りの後）＋評価問 eval/ を運ぶ（tar は */data を除外するため
# git 追跡の eval/ も S3 経由）。cache/（原本 約 27GB・取込時のみ使用＝退避先は `archive/companies/edinet/`・RUNBOOK_OPS §3）・verify/・logs/・query_log/（箱で生成する燃料）は運ばない。
# 値の置き場が無い環境（companies を使わない導入団体）では何もしない＝S3 側を空にしない（--delete も付けない）。
if [[ -f "$REPO_DIR/companies/data/store/companies.json" ]]; then
  echo "[upload] companies データ同期 → s3://$BUCKET/$DATA_PREFIX/companies/ （store/eval）"
  "${AWS[@]}" s3 sync "$REPO_DIR/companies/data/" "s3://$BUCKET/$DATA_PREFIX/companies/" --region "$REGION" \
    --exclude "cache/*" --exclude "verify/*" --exclude "logs/*" --exclude "query_log/*" --exclude "*.bak"
fi

# deliberations（審議会議事録DB）のデータ＝S3 `data/deliberations/bundle/`（Qdrant のスナップショット＋語彙・目録・解析結果＝
# deliberations.ops.bundle export が release.sh の中で作る）。束が無い環境では何もしない。--delete は bundle/ の中だけ
# （親の data/deliberations/ には箱の捕捉ログ query_log/ が fuelsync で入る＝消さない）。
if [[ -f "$REPO_DIR/deliberations/data/bundle/deliberations_v1/bundle.json" ]]; then
  echo "[upload] deliberations の束 → s3://$BUCKET/$DATA_PREFIX/deliberations/bundle/"
  "${AWS[@]}" s3 sync "$REPO_DIR/deliberations/data/bundle/" "s3://$BUCKET/$DATA_PREFIX/deliberations/bundle/" --region "$REGION" \
    --delete --exclude ".restored_*"
fi

# ★ここで「bootstrap 再走行で取り込まれる」と案内しない：bootstrap の再走行はコードを更新しない（RUNBOOK §5）。
#   稼働中の箱への反映は release.sh が最後に書くマニフェスト→自動適用（tar 再展開）だけが正規経路。
echo "[upload] 完了（S3 へ置いただけ＝箱への反映はこのスクリプトの仕事ではない）。"
echo "[upload]   release.sh 経由＝続けてマニフェストが書かれ、箱が 15 分以内に自動適用する（人手の箱操作は不要）。"
echo "[upload]   新規の箱＝EC2 初回起動（user_data）が S3 から取得する。"
echo "[upload]   単体実行＝稼働中の箱には反映されない（bootstrap の再走行はコードを更新しない）。反映は release.sh で。"
# 期待値は recommendations/eval/phase12_pipeline.py の CANONICAL_SHA256 だけに置く（ここに値を写さない）。表示のみ＝配布は止めない。
EVAL_EXPECT=$(sed -n 's/^CANONICAL_SHA256 = "\([0-9a-f]*\)".*/\1/p' "$REPO_DIR/recommendations/eval/phase12_pipeline.py")
EVAL_ACTUAL=$(shasum -a 256 "$REPO_DIR/recommendations/data/eval/eval_set.json" 2>/dev/null | cut -d' ' -f1)
if [[ -z "$EVAL_ACTUAL" ]]; then
  echo "[upload] 正典 eval の sha256：shasum 不可の環境（期待＝phase12_pipeline.py の CANONICAL_SHA256 ${EVAL_EXPECT:0:8}…）"
elif [[ "$EVAL_ACTUAL" == "$EVAL_EXPECT" ]]; then
  echo "[upload] 正典 eval の sha256 ${EVAL_ACTUAL:0:8}…＝CANONICAL_SHA256 と一致"
else
  echo "[upload] ★正典 eval の sha256 ${EVAL_ACTUAL:0:8}… が CANONICAL_SHA256 ${EVAL_EXPECT:0:8}… と不一致"
  echo "[upload]   正典に評価問を足したなら phase12_pipeline.py の CANONICAL_SHA256 を更新する（append が中止する）。"
fi
