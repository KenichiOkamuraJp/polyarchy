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
# ★秘密は運ばない：tar から .env を除外。データにも秘密は無い（公開データ＋捕捉ログ）。
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
STAGE_DIR="$(mktemp -d -t polyarchy-stage.XXXXXX)"
ln -s "$REPO_DIR" "$STAGE_DIR/$APP_BASENAME"
PARENT_DIR="$STAGE_DIR"

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")

# コード版の刻印（箱には .git が無いため tar に VERSION を同梱＝ops_dashboard が表示する）
git -C "$REPO_DIR" describe --tags --always --dirty 2>/dev/null > "$REPO_DIR/VERSION" || echo unknown > "$REPO_DIR/VERSION"
date -Iseconds >> "$REPO_DIR/VERSION"
echo "[upload] コードを tar 化（.env/data/__pycache__/scratchpad/社内ノート/egg-info を除外）"
TARBALL="$(mktemp -t polyarchy.XXXXXX.tar.gz)"
# --no-xattrs/--no-mac-metadata＝macOS の拡張属性（com.apple.provenance 等）を tar に入れない（箱の GNU tar が警告を大量に出す・2026-09-03）
tar --no-xattrs --no-mac-metadata -czf "$TARBALL" -C "$PARENT_DIR" \
  --exclude='*.env' \
  --exclude="$APP_BASENAME/*/data" \
  --exclude="$APP_BASENAME/*/scratchpad" \
  --exclude="$APP_BASENAME/*/notebooks" \
  --exclude="$APP_BASENAME/.git" \
  --exclude="$APP_BASENAME/.claude" \
  --exclude='*/社内ノート' \
  --exclude='*/ops/dashboard' \
  --exclude='*/ops/usage' \
  --exclude='*/ops/triage' \
  --exclude='*/work_history' \
  --exclude='*/__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.egg-info' \
  --exclude='.DS_Store' \
  --exclude='.git' \
  --exclude='*/.terraform' \
  --exclude='*.tfstate' \
  --exclude='*.tfstate.*' \
  --exclude='.terraform.lock.hcl' \
  -h "$APP_BASENAME"
rm -rf "$STAGE_DIR"
# ↑ `terraform init` が deploy/terraform/.terraform に AWS プロバイダ(~648MB)を落とすため、これを
#   除外しないと code tar に混入して肥大化する（tar は .gitignore を読まない）。state/lock も箱には不要。
#   `*/.terraform` はどの階層の .terraform ディレクトリも除外（将来 init 位置が変わっても安全）。
# ★`*/ops/usage` は箱が恒久蓄積する weekly.jsonl の置き場（手元版で上書きすると系列が途切れる）・`*/ops/triage` は手元の草稿。
# ★`*/ops/dashboard` は箱の適用マーク（applied_data_release.json）の置き場＝手元の生成物で上書きしない（apply が tar を再展開するため）。
# ★`*/社内ノート` は git 管理外の社内向け開発ノート（引き継ぎ/ロードマップ/設計メモ）。
#   .gitignore の「社内ノート/」と対で維持する＝tar は .gitignore を読まないため、ここに書かないと
#   `deploy.sh prod all` で導入団体 AWS へ社内ノートが転写される。新規ノートは社内ノート/ 配下に
#   置けば自動で除外される（どの階層でも一致）。

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
# ★query_log は運ばない：S3 の data/query_log/ は空が正（捕捉ログは 30 日で消す約束）＝ローカル捕捉分で
#   復活させない。箱は自前の捕捉を data/query_log/ に書く（親 dir は書込時に自動作成・polyarchy_common.capture）。
# ★qdrant ストレージは S3 側も「ミラー」（--delete）＝ローカルで消えた旧 WAL セグメントを S3 に残さない。
#   追記型で残すと新旧世代が混在し、箱側で "missing wal segments" の起動不能になる（2026-09-03 実測）。
#   qdrant ディレクトリに捕捉ログ等の温存対象は無い＝--delete して安全。
"${AWS[@]}" s3 sync "$REPO_DIR/recommendations/data/qdrant/" "s3://$BUCKET/$DATA_PREFIX/qdrant/" --region "$REGION" --delete
"${AWS[@]}" s3 sync "$REPO_DIR/recommendations/data/" "s3://$BUCKET/$DATA_PREFIX/" --region "$REGION" \
    --exclude "qdrant/*" \
  --exclude "*.bak" --exclude "*.bak[0-9]" \
  --exclude "*.phase11*.bak" \
  --exclude "eval/results/*" --exclude "stats/*" --exclude "companies/*" --exclude "query_log/*"

# stats（統計参照DB）のデータ＝S3 `data/stats/`（共通契約 §4）。registry（git 追跡・2MB）＋values（88MB）＋eval を運ぶ。
# cache/（原本 1.2GB・取込時のみ使用）・values_archive/・query_log/（箱で生成する燃料）は運ばない。
echo "[upload] stats データ同期 → s3://$BUCKET/$DATA_PREFIX/stats/ （registry/values/eval）"
"${AWS[@]}" s3 sync "$REPO_DIR/stats/data/" "s3://$BUCKET/$DATA_PREFIX/stats/" --region "$REGION" \
  --exclude "cache/*" --exclude "values_archive/*" --exclude "query_log/*" --exclude "*.bak"

# companies（企業情報DB）のデータ＝S3 `data/companies/`。値の置き場 store/（約 200MB）＋評価問 eval/ を運ぶ（tar は */data を除外するため
# git 追跡の eval/ も S3 経由）。cache/（原本 zip 約 3GB・取込時のみ使用）・verify/・logs/・query_log/（箱で生成する燃料）は運ばない。
# 値の置き場が無い環境（companies を使わない導入団体）では何もしない＝S3 側を空にしない（--delete も付けない）。
if [[ -f "$REPO_DIR/companies/data/store/companies.json" ]]; then
  echo "[upload] companies データ同期 → s3://$BUCKET/$DATA_PREFIX/companies/ （store/eval）"
  "${AWS[@]}" s3 sync "$REPO_DIR/companies/data/" "s3://$BUCKET/$DATA_PREFIX/companies/" --region "$REGION" \
    --exclude "cache/*" --exclude "verify/*" --exclude "logs/*" --exclude "query_log/*" --exclude "*.bak"
fi

# ★ここで「bootstrap 再走行で取り込まれる」と案内しない：bootstrap の再走行はコードを更新しない（RUNBOOK §5）。
#   稼働中の箱への反映は release.sh が最後に書くマニフェスト→自動適用（tar 再展開）だけが正規経路。
echo "[upload] 完了（S3 へ置いただけ＝箱への反映はこのスクリプトの仕事ではない）。"
echo "[upload]   release.sh 経由＝続けてマニフェストが書かれ、箱が 15 分以内に自動適用する（人手の箱操作は不要）。"
echo "[upload]   新規の箱＝EC2 初回起動（user_data）が S3 から取得する。"
echo "[upload]   単体実行＝稼働中の箱には反映されない（bootstrap の再走行はコードを更新しない）。反映は release.sh で。"
echo "[upload] 正典 eval の sha256（バイト不変の確認用・期待 4a51f5f5…）:"
shasum -a 256 "$REPO_DIR/recommendations/data/eval/eval_set.json" 2>/dev/null || \
  echo "  （shasum 不可の環境。recommendations/eval/phase12_pipeline.py の CANONICAL_SHA256 を参照）"
