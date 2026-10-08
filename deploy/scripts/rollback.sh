#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# ロールバック/現状復帰スクリプト。「仮に何か間違っても現状復帰できる」ための安全網。
#
#   bash rollback.sh status [staging|prod]     現状把握（EC2 のサービス・公開 URL の healthz）
#   bash rollback.sh snapshot <staging|prod>   EBS スナップショット（破棄・変更の前の保険）
#   bash rollback.sh destroy  <staging|prod>   ★terraform destroy（事前スナップ＋二重確認）
#
# 破壊的操作（destroy）は必ず確認プロンプトを通す。
# 作業用 PC へ入口を戻す経路（旧 entry-to-mac／entry-to-ec2）は撤去した＝PC で cloudflared tunnel run しない・
# 認証の env なしで公開しない（ルート CLAUDE.md §3・§5）。箱の障害は RUNBOOK_OPS の手順で箱側を直す。
# ═══════════════════════════════════════════════════════════════════════════
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# 公開ホスト（healthz の外形確認用）＝対象環境の env ファイル（deploy/env/<env>.env）の TUNNEL_HOST_<SVC> から組む
# （load_env の後に hosts_from_env）。2026-10-09 までは staging のホストを既定に固定しており、
# `status prod` が staging の /healthz を prod の結果として表示した＝env から組めないものは確かめない（表示で明示）。
hosts_from_env() {
  MCP_HOST="${TUNNEL_HOST_MCP:-}"
  STATS_HOST="${TUNNEL_HOST_STATS:-}"
  COMPANIES_HOST="${TUNNEL_HOST_COMPANIES:-}"
  DELIBERATIONS_HOST="${TUNNEL_HOST_DELIBERATIONS:-}"
}

# ── SSM でコマンド（★ダブルクオートを含めない単一文字列・; 連結可）を実行し stdout を返す ──
ssm_run() { # $1=instance-id  $2=command
  local iid="$1" cmd="$2" cid st
  cid=$(aws ssm send-command --instance-ids "$iid" --document-name AWS-RunShellScript \
        --parameters "commands=[\"$cmd\"]" --query Command.CommandId --output text) || return 1
  local waited=0
  while :; do
    st=$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$iid" --query Status --output text 2>/dev/null || echo Pending)
    case "$st" in Success|Failed|Cancelled|TimedOut) break;; esac
    sleep 2; waited=$((waited+2))
    if (( waited >= ${SSM_WAIT_MAX:-600} )); then echo "[rollback] SSM コマンド待ちがタイムアウト（${waited}s）" >&2; return 1; fi
  done
  aws ssm get-command-invocation --command-id "$cid" --instance-id "$iid" --query 'StandardOutputContent' --output text 2>/dev/null
}

verify_public() { # /healthz は認証不要（WAF 除外）＝origin 生存の外形確認（ホストは対象環境の env から＝hosts_from_env）
  local pair name var host code
  for pair in recommendations:MCP_HOST stats:STATS_HOST companies:COMPANIES_HOST deliberations:DELIBERATIONS_HOST; do
    name="${pair%%:*}"; var="${pair#*:}"; host="${!var:-}"
    if [[ -z "$host" ]]; then
      log "公開URL: ${name}＝env に TUNNEL_HOST が無い（公開していない・または env の不備）＝確かめない"
      continue
    fi
    code=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' "https://$host/healthz" 2>/dev/null || echo ERR)
    log "公開URL: ${name}($host)/healthz=${code}（200=疎通OK）"
  done
}

# ── サブコマンド ───────────────────────────────────────────────────────────────
cmd_status() {
  local name="${1:-staging}"
  load_env "$name"
  hosts_from_env
  local iid; iid=$(instance_id)
  echo "── EC2($name) iid=${iid:-なし} ──"
  if [[ -n "$iid" ]]; then
    echo -n "  instance state: "; aws ec2 describe-instances --instance-ids "$iid" --query 'Reservations[].Instances[].State.Name' --output text
    ssm_run "$iid" 'for s in qdrant polyarchy-mcp polyarchy-stats polyarchy-companies qdrant-deliberations polyarchy-deliberations cloudflared; do echo $s=$(systemctl is-active $s); done' 2>/dev/null | sed 's/^/  service /' || echo "  (SSM 取得失敗)"
  fi
  verify_public
}

cmd_snapshot() {
  local name="${1:-}"; [[ -n "$name" ]] || die "使い方: rollback.sh snapshot <staging|prod>"
  load_env "$name"
  local iid; iid=$(instance_id); [[ -n "$iid" ]] || die "$name の EC2 が見つからない"
  local vol; vol=$(root_volume_id "$iid"); [[ -n "$vol" ]] || die "ルートEBSが特定できない（iid=${iid}）"
  log "EBS スナップショット作成: vol=${vol}（iid=${iid}・env=${name}）"
  local sid
  sid=$(aws ec2 create-snapshot --volume-id "$vol" \
        --description "polyarchy-$name rollback snapshot" \
        --tag-specifications "ResourceType=snapshot,Tags=[{Key=Project,Value=$PROJECT},{Key=Environment,Value=$name},{Key=Purpose,Value=rollback}]" \
        --query SnapshotId --output text)
  log "スナップショット発行: ${sid}（完了まで数分・aws ec2 describe-snapshots で確認）"
  echo "$sid"
}

cmd_destroy() {
  local name="${1:-}"; [[ -n "$name" ]] || die "使い方: rollback.sh destroy <staging|prod>"
  load_env "$name"
  warn "★★ terraform destroy（${name}）＝EC2/VPC/IAM 等を削除します。★★"
  warn "  S3 バケット（データ/燃料）は中身が空でないと destroy が失敗＝実質保護されます（消したい時は手動で空に）。"
  log "破棄前に EBS スナップショットを取ります（保険）。"
  cmd_snapshot "$name" || warn "スナップショット失敗（インスタンス未作成かも）。続行判断は慎重に。"
  echo ""
  ( cd "$TF_DIR" && terraform init -input=false >/dev/null && terraform plan -destroy "${TF_ARGS[@]}" | tail -40 )
  echo ""
  warn "上記が削除内容です。取り消せません。"
  local typed
  read -r -p "本当に破棄するなら環境名『${name}』を入力: " typed
  [[ "$typed" == "$name" ]] || die "入力不一致＝中断（何も削除していません）"
  ( cd "$TF_DIR" && terraform destroy "${TF_ARGS[@]}" )
  log "destroy 完了（S3 バケットが残っていれば中身ありのため＝データは保全）。"
}

# ── ディスパッチ ──────────────────────────────────────────────────────────────
case "${1:-}" in
  status)        cmd_status "${2:-staging}" ;;
  snapshot)      cmd_snapshot "${2:-}" ;;
  destroy)       cmd_destroy "${2:-}" ;;
  *) die "使い方: rollback.sh {status [env]|snapshot <env>|destroy <env>}" ;;
esac
