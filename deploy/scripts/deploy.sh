#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# 一連の operator 手順を1コマンドで（env ファイル駆動＝staging も prod も同じスクリプト）。
#
#   bash deploy.sh <staging|prod> [phase]
#     phase = secrets | upload | apply | all（既定 all）
#       secrets … §2 SSM 秘密登録
#       upload  … §3 バケット先行作成 → コード/データを S3 へ（約 7GB・時間の山）
#       apply   … §4 本適用（EC2/VPC/IAM…・プラン確認あり）
#       all     … secrets → upload →（確認）→ apply
#
# prod 昇格は「env/prod.env を用意して bash deploy.sh prod all」だけ（仕様§3.1 の転写）。
# ═══════════════════════════════════════════════════════════════════════════
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# ── 凍結ガード（開発環境方針.md §2）：deploy/FREEZE が存在する間は全 phase を中止する。
#    PoC 試行中の誤デプロイ防止（人手・AIセッションの双方）。解除＝FREEZE ファイルを削除。
FREEZE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/FREEZE"
if [[ -e "${FREEZE_FILE}" ]]; then
  die "凍結中のため中止します（${FREEZE_FILE} が存在）。試行終了後に削除してから再実行してください。"
fi

ENV_NAME="${1:-}"
PHASE="${2:-all}"
[[ -n "$ENV_NAME" ]] || die "使い方: bash deploy.sh <staging|prod> [secrets|upload|apply|all]"

load_env "$ENV_NAME"

case "$PHASE" in
  secrets) do_register_secrets ;;
  upload)  do_provision_bucket; do_upload ;;
  apply)   do_apply; print_next_steps ;;
  all)
    do_register_secrets
    do_provision_bucket
    do_upload
    confirm "▲ 本適用に進みますか？（EC2/VPC/IAM を作成。プランは次に表示）" || die "中断しました（apply は未実行）"
    do_apply
    print_next_steps
    ;;
  *) die "未知の phase: ${PHASE}（secrets|upload|apply|all）" ;;
esac

log "phase=${PHASE} 完了（env=${ENV_NAME}）"
