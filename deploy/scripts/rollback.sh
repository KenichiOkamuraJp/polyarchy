#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# ロールバック/現状復帰スクリプト。「仮に何か間違っても現状復帰できる」ための安全網。
#
#   bash rollback.sh status [staging|prod]     現状把握（Mac/EC2 の入口・サービス）
#   bash rollback.sh entry-to-mac              ★緊急復帰：入口を EC2→Mac へ戻す
#   bash rollback.sh entry-to-ec2              入口を Mac→EC2 へ（再カットオーバー）
#   bash rollback.sh snapshot <staging|prod>   EBS スナップショット（破棄・変更の前の保険）
#   bash rollback.sh destroy  <staging|prod>   ★terraform destroy（事前スナップ＋二重確認）
#
# 破壊的操作（destroy）は必ず確認プロンプトを通す。entry-* は瞬断を伴う。
# ★entry-to-mac / entry-to-ec2 は初期移行期（作業用 PC が元ライブだった時期）の名残＝通常運用では使わない。
#   同じトンネルを 2 箇所で run すると本番トラフィックが分流する（ルート CLAUDE.md §5）。
#   使うなら必ず片側の cloudflared を止めてから（本スクリプトはその順で実行する）。
# ═══════════════════════════════════════════════════════════════════════════
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# ── Mac 側（元ライブ）の既定値。必要なら環境変数で上書き可 ─────────────────────
MAC_APP_DIR="${MAC_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"  # リポジトリ root（recommendations.serving.* を -m で起動。旧 rag-phase1 は Chroma 世代＝廃止）
MAC_CONDA_BIN="${MAC_CONDA_BIN:-$HOME/miniconda3/envs/polyarchy/bin}"
MAC_CLOUDFLARED="${MAC_CLOUDFLARED:-/opt/homebrew/bin/cloudflared}"
MAC_TUNNEL="${MAC_TUNNEL:-}"   # entry-to-mac / entry-to-ec2 のときだけ必須（mac_start/mac_stop で検査）
MAC_LOG_DIR="${MAC_LOG_DIR:-/tmp/polyarchy-mac}"
# web(chat_app) は 2026-09-02 廃止＝Mac 復帰は MCP のみ。ホストは 2026-09-02 ドメイン移行後の現行。
MCP_HOST="${MCP_HOST:-recommendations.polyarchy.net}"
STATS_HOST="${STATS_HOST:-stats.polyarchy.net}"
COMPANIES_HOST="${COMPANIES_HOST:-}"   # companies を有効にした環境だけ指定（例 companies.polyarchy.net）

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

# ── EC2 の cloudflared を止める/起動する（SSM 経由）─────────────────────────────
ec2_cloudflared() { # $1=start|stop  $2=instance-id
  case "$1" in
    stop)  ssm_run "$2" 'systemctl disable --now cloudflared 2>&1 | tail -2; systemctl is-active cloudflared || echo stopped' ;;
    start) ssm_run "$2" 'systemctl enable --now cloudflared 2>&1 | tail -2; sleep 6; systemctl is-active cloudflared' ;;
    *) die "ec2_cloudflared: start|stop" ;;
  esac
}

# ── Mac 側の app + cloudflared を起動/停止 ──────────────────────────────────────
mac_stop() {
  [[ -n "$MAC_TUNNEL" ]] || die "MAC_TUNNEL（Mac 側で run するトンネル名）を環境変数で指定"
  log "Mac: cloudflared / mcp を停止"
  pkill -f "cloudflared tunnel run $MAC_TUNNEL" 2>/dev/null || true
  pkill -f "recommendations.serving.mcp_server --http" 2>/dev/null || true
  sleep 1 || true
  pgrep -lf "cloudflared|recommendations.serving.mcp_server" || log "Mac: 全停止確認"
}

mac_start() {
  [[ -n "$MAC_TUNNEL" ]] || die "MAC_TUNNEL（Mac 側で run するトンネル名）を環境変数で指定"
  [[ -d "$MAC_APP_DIR" ]] || die "MAC_APP_DIR が無い: ${MAC_APP_DIR}（環境変数で指定可）"
  [[ -x "$MAC_CONDA_BIN/python" ]] || die "conda python が無い: $MAC_CONDA_BIN/python"
  [[ -x "$MAC_CLOUDFLARED" ]] || die "cloudflared が無い: $MAC_CLOUDFLARED"
  mkdir -p "$MAC_LOG_DIR"
  log "Mac: MCP(:8765) 起動 → $MAC_LOG_DIR/mcp.log"
  ( cd "$MAC_APP_DIR" && PYTHONPATH="$MAC_APP_DIR" nohup "$MAC_CONDA_BIN/python" -m recommendations.serving.mcp_server --http --port 8765 \
      >"$MAC_LOG_DIR/mcp.log" 2>&1 & )
  log "Mac: cloudflared($MAC_TUNNEL) 起動 → $MAC_LOG_DIR/cloudflared.log"
  nohup "$MAC_CLOUDFLARED" tunnel run "$MAC_TUNNEL" >"$MAC_LOG_DIR/cloudflared.log" 2>&1 &
  log "（スリープ防止が要るなら別途 caffeinate -s を。ログは $MAC_LOG_DIR/）"
}

verify_public() { # /healthz は認証不要（WAF 除外）＝origin 生存の外形確認
  local m s
  m=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' "https://$MCP_HOST/healthz" 2>/dev/null || echo ERR)
  s=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' "https://$STATS_HOST/healthz" 2>/dev/null || echo ERR)
  log "公開URL: recommendations($MCP_HOST)/healthz=$m  stats($STATS_HOST)/healthz=$s  （200=疎通OK）"
  if [[ -n "$COMPANIES_HOST" ]]; then
    c=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' "https://$COMPANIES_HOST/healthz" 2>/dev/null || echo ERR)
    log "公開URL: companies($COMPANIES_HOST)/healthz=$c"
  fi
}

# ── サブコマンド ───────────────────────────────────────────────────────────────
cmd_status() {
  local name="${1:-staging}"
  echo "── Mac 側プロセス ──"
  pgrep -lf "cloudflared tunnel run|recommendations.serving.mcp_server" || echo "  （cloudflared/mcp なし＝Mac は配信していない）"
  load_env "$name"
  local iid; iid=$(instance_id)
  echo "── EC2($name) iid=${iid:-なし} ──"
  if [[ -n "$iid" ]]; then
    echo -n "  instance state: "; aws ec2 describe-instances --instance-ids "$iid" --query 'Reservations[].Instances[].State.Name' --output text
    ssm_run "$iid" 'for s in qdrant polyarchy-mcp polyarchy-stats polyarchy-companies cloudflared; do echo $s=$(systemctl is-active $s); done' 2>/dev/null | sed 's/^/  service /' || echo "  (SSM 取得失敗)"
  fi
  verify_public
}

cmd_entry_to_mac() {
  warn "★入口を EC2→Mac へ戻します（現状復帰）。数秒〜1分の瞬断が出ます。"
  confirm "続行しますか？" || die "中断"
  load_env staging
  local iid; iid=$(instance_id)
  [[ -n "$iid" ]] || die "staging の EC2 が見つからない"
  log "① EC2($iid) の cloudflared を停止（二重 origin 回避のため先に）"
  ec2_cloudflared stop "$iid"
  log "② Mac 側 app+cloudflared を起動"
  mac_start
  log "③ 反映待ち…検証"
  sleep 8 || true
  verify_public
  log "完了。recommendations/healthz=200 なら Mac が配信に復帰（stats は Mac では配信しない＝要 EC2 復旧）。ダメなら $MAC_LOG_DIR/*.log を確認。"
}

cmd_entry_to_ec2() {
  warn "★入口を Mac→EC2 へ（再カットオーバー）。数秒〜1分の瞬断が出ます。"
  confirm "続行しますか？" || die "中断"
  load_env staging
  local iid; iid=$(instance_id)
  [[ -n "$iid" ]] || die "staging の EC2 が見つからない"
  log "① Mac の cloudflared/app を停止"
  mac_stop
  log "② EC2($iid) の cloudflared を起動"
  ec2_cloudflared start "$iid"
  log "③ 反映待ち…検証"
  sleep 8 || true
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
  entry-to-mac)  cmd_entry_to_mac ;;
  entry-to-ec2)  cmd_entry_to_ec2 ;;
  snapshot)      cmd_snapshot "${2:-}" ;;
  destroy)       cmd_destroy "${2:-}" ;;
  *) die "使い方: rollback.sh {status [env]|entry-to-mac|entry-to-ec2|snapshot <env>|destroy <env>}" ;;
esac
