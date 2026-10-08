#!/bin/bash
# リリースの自動適用（15 分毎・polyarchy-dataapply.timer から。root で実行＝systemctl を使うため）。
# トリガ＝S3 release/data.json（release.sh がゲート全 PASS 時にのみ書く）。前回適用と同じなら何もしない。
#
# 工程（2026-09-03 改訂＝コード自動反映＋自動切り戻し・残タスク B15 論点1）:
#   ① 退避   ＝現行のデータ（qdrant・catalog/bm25/eval・stats registry/values・companies store・deliberations の束と専用 qdrant）を $RB_DIR へ rsync
#              （稼働中に 1 回目→停止後に差分の 2 回目＝停止時間を伸ばさず整合コピー）。現行の code tar も prev として保持。
#   ② 反映   ＝サービス停止 → code tar 再展開（★bootstrap はコードを更新しないため apply が行う＝旧地雷の解消）
#              → qdrant ミラー同期（--delete）→ bootstrap 再走行（データ差分 sync・pip 再解決・ユニット設置）→ 再起動
#              → ★入口：bootstrap が /etc/cloudflared/config.yml を書き換えていた（ingress の追加・削除）ときだけ cloudflared も再起動
#                （数秒の断。通常のデータ更新では config が変わらない＝入口を揺らさない。2026-09-22 staging 実測＝再起動しないと
#                 新ホストは 25 分以上 404 のまま・切り戻し経路でも同じ判定）
#   ③ 検証   ＝qdrant 応答待ち → 箱上 smoke（recommendations＋stats＋companies〔有効時〕＋deliberations〔有効時〕）
#              → 稼働中の各サービスの /healthz が 200（配った形＝unit・env・HTTP の待受・認証の装着を通るのはこちらだけ）
#   ④ 判定   ＝PASS：マーク更新（status=APPLIED）・メトリクス dataapply=1
#              FAIL：**自動切り戻し**＝停止 → ①の退避を書き戻し（qdrant はミラー）→ prev tar 再展開＋pip → 再起動 → smoke 再実行
#                    → マーク status=ROLLED_BACK（このマニフェストは再試行しない＝15 分毎の再適用ループを防ぐ）
#                    → メトリクス dataapply=0（→ アラーム dataapply-rollback → SNS メール）→ exit 1
# 訓練＝/etc/polyarchy/apply_drill_fail が存在すると smoke を FAIL 扱いにする（切り戻し経路の演習用・演習後に消す）。
# 手動＝`polyarchy-data-apply --force` は同じマニフェストでも再適用する（演習・復旧の再実行用。通常運用では使わない）。
# 記録＝本スクリプトの全出力が journal→rsyslog→CloudWatch Logs（polyarchy/dataapply）に残る＝人手の箱操作ゼロでも監査可能。
set -euo pipefail
source /etc/polyarchy/deploy.env
REPO_DIR="${REPO_DIR:-$INSTALL_DIR/polyarchy}"
APP_DIR="${APP_DIR:-$REPO_DIR/recommendations}"
CODE_S3_KEY="${CODE_S3_KEY:-code/polyarchy.tar.gz}"
PIP_EXTRAS="${PIP_EXTRAS:-recommendations,stats}"
MARK_DIR="$REPO_DIR/ops/dashboard"
MARK="$MARK_DIR/applied_data_release.json"
RB_DIR="$INSTALL_DIR/rollback"      # データ退避先（前回 PASS 時点の整合コピー・root 所有）
REL_DIR="$INSTALL_DIR/releases"     # code tar の保管（current=稼働中・prev=切り戻し先・next=適用中）
DRILL_FLAG=/etc/polyarchy/apply_drill_fail
PY=/opt/miniconda/envs/polyarchy/bin/python
PIP=/opt/miniconda/envs/polyarchy/bin/pip
FORCE=0; [[ "${1:-}" == "--force" ]] && FORCE=1
TMP="$(mktemp)"
mkdir -p "$MARK_DIR" "$RB_DIR" "$REL_DIR"

log()  { echo "[dataapply] $*"; }
warn() { echo "[dataapply] ⚠ $*" >&2; }
metric() { # $1=1(PASS)|0(ROLLED_BACK)。失敗しても本体は止めない（best-effort）
  aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace polyarchy \
    --metric-data "MetricName=dataapply,Dimensions=[{Name=service,Value=box}],Value=$1,Unit=None" 2>/dev/null \
    || warn "メトリクス送信失敗（dataapply=$1）"
}

# ── 0. 新リリースの検知 ─────────────────────────────────────────────────────
if ! aws s3 cp "s3://$S3_BUCKET/release/data.json" "$TMP" --region "$AWS_REGION" --quiet 2>/dev/null; then
  log "release/data.json なし＝リリース未実施（何もしない）"
  exit 0
fi
NEW_AT="$(python3 -c "import json; print(json.load(open('$TMP')).get('released_at',''))")"
NEW_VER="$(python3 -c "import json; print(json.load(open('$TMP')).get('code_version',''))")"
CUR_AT="$( [ -f "$MARK" ] && python3 -c "import json;print(json.load(open('$MARK')).get('released_at',''))" || echo '')"
if [[ -z "$NEW_AT" ]]; then warn "マニフェスト不正（released_at なし）＝適用しない"; exit 1; fi
if [[ "$NEW_AT" == "$CUR_AT" && "$FORCE" == 0 ]]; then
  exit 0   # 適用済み（PASS でも ROLLED_BACK でも同じマニフェストは再試行しない）＝静かに終了
fi
# 現在サービング中の版（切り戻し先の表示用）＝前回マークの serving（無ければ前回マーク自身）
SERVING_JSON="$( [ -f "$MARK" ] && python3 -c "
import json; m=json.load(open('$MARK')); s=m.get('serving') or {k:m.get(k) for k in ('released_at','code_version')}
print(json.dumps(s, ensure_ascii=False))" || echo '{}')"
log "新リリース検知: ${NEW_AT}（code ${NEW_VER}・適用中: ${CUR_AT:-なし}$( [[ "$FORCE" == 1 ]] && echo "・--force" )）＝適用開始"

# ── 共通部品 ────────────────────────────────────────────────────────────────
command -v rsync >/dev/null || { export DEBIAN_FRONTEND=noninteractive; apt-get install -y -q rsync >/dev/null; }
# 退避対象＝apply が書き換えるもの。除外＝箱で生まれる追記物（query_log・cache）と巨大な追記専用物（pdfs）
REC_EXCL=(--exclude=pdfs/ --exclude=query_log/ --exclude=cache/ --exclude=qdrant_snapshots/)
STATS_EXCL=(--exclude=query_log/ --exclude=cache/ --exclude=values_archive/)
COMPANIES_EXCL=(--exclude=query_log/ --exclude=cache/ --exclude=verify/ --exclude=logs/)
COMPANIES_ON=0; [[ "${ENABLE_COMPANIES_APP:-false}" == "true" ]] && COMPANIES_ON=1  # companies は opt-in（deploy.env）
# deliberations（審議会議事録DB）も opt-in。データ＝束（bundle/）＋専用 Qdrant のストレージ（qdrant/）＋復元の印＝丸ごと退避・書き戻す
# （政策主張DB の qdrant ミラーとは別＝qdrant-deliberations.service・:6340）。捕捉ログとスナップショットの作業場は除外。
DELIB_EXCL=(--exclude=query_log/ --exclude=qdrant_snapshots/ --exclude=cache/)
DELIB_ON=0; [[ "${ENABLE_DELIBERATIONS_APP:-false}" == "true" ]] && DELIB_ON=1
snapshot_data() { # 現行データ → ${RB_DIR}（--delete＝前回退避の残骸を残さない）。戻り値＝rsync の合否
  rsync -a --delete "${REC_EXCL[@]}"   "$APP_DIR/data/"        "$RB_DIR/recommendations_data/" || return 1
  if [[ -d "$REPO_DIR/stats/data" ]]; then
    rsync -a --delete "${STATS_EXCL[@]}" "$REPO_DIR/stats/data/" "$RB_DIR/stats_data/" || return 1
  fi
  if [[ "$COMPANIES_ON" == 1 && -d "$REPO_DIR/companies/data" ]]; then
    rsync -a --delete "${COMPANIES_EXCL[@]}" "$REPO_DIR/companies/data/" "$RB_DIR/companies_data/" || return 1
  fi
  if [[ "$DELIB_ON" == 1 && -d "$REPO_DIR/deliberations/data" ]]; then
    rsync -a --delete "${DELIB_EXCL[@]}" "$REPO_DIR/deliberations/data/" "$RB_DIR/deliberations_data/" || return 1
  fi
  return 0
}
restore_data() { # $RB_DIR → 現行（除外パターンは受け側でも保護される＝--delete が捕捉ログ等に及ばない）
  rsync -a --delete "${REC_EXCL[@]}"   "$RB_DIR/recommendations_data/" "$APP_DIR/data/" || warn "recommendations データの書き戻しで rsync エラー"
  if [[ -d "$RB_DIR/stats_data" ]]; then
    rsync -a --delete "${STATS_EXCL[@]}" "$RB_DIR/stats_data/" "$REPO_DIR/stats/data/" || warn "stats データの書き戻しで rsync エラー"
  fi
  if [[ "$COMPANIES_ON" == 1 && -d "$RB_DIR/companies_data" ]]; then
    rsync -a --delete "${COMPANIES_EXCL[@]}" "$RB_DIR/companies_data/" "$REPO_DIR/companies/data/" || warn "companies データの書き戻しで rsync エラー"
    chown -R polyarchy:polyarchy "$REPO_DIR/companies/data"
  fi
  if [[ "$DELIB_ON" == 1 && -d "$RB_DIR/deliberations_data" ]]; then
    rsync -a --delete "${DELIB_EXCL[@]}" "$RB_DIR/deliberations_data/" "$REPO_DIR/deliberations/data/" || warn "deliberations データの書き戻しで rsync エラー"
    chown -R polyarchy:polyarchy "$REPO_DIR/deliberations/data"
  fi
  chown -R polyarchy:polyarchy "$APP_DIR/data" "$REPO_DIR/stats/data"
}
extract_code() { # $1=tar → ${INSTALL_DIR}（tar 内トップは polyarchy/＝REPO_DIR）。data/ は tar に含まれない（upload_to_s3.sh）
  tar -xzf "$1" -C "$INSTALL_DIR" && chown -R polyarchy:polyarchy "$REPO_DIR"
}
pip_resolve() { # 切り戻し時の依存再解決（bootstrap ③ と同じ＝ロックから --require-hashes・本体は --no-deps・出力は要点のみ）
  local lock="$REPO_DIR/deploy/requirements/lock-${PIP_EXTRAS//,/-}.txt"
  if [[ -f "$lock" ]]; then
    { sudo -u polyarchy env HF_HOME="$HF_HOME" "$PIP" install --no-input -q --require-hashes -r "$lock" \
      && sudo -u polyarchy env HF_HOME="$HF_HOME" "$PIP" install --no-input -q --no-deps --no-build-isolation -e "$REPO_DIR"; } \
      || warn "pip 再解決（ロック）に失敗（コードは旧版に戻っている・依存差分があれば bootstrap 再走行）"
  else
    # ロック導入（2026-09-04）より前の tar へ戻る場合だけの互換経路（ロック無し＝下限指定の解決）。
    warn "旧版 tar にロックが無い＝pyproject の下限指定で再解決（ロック導入前の版への切り戻し）"
    sudo -u polyarchy env HF_HOME="$HF_HOME" "$PIP" install --no-input -q -e "$REPO_DIR[$PIP_EXTRAS]" \
      || warn "pip 再解決に失敗（コードは旧版に戻っている・依存差分があれば bootstrap 再走行）"
  fi
}
svc_stop()  {
  systemctl stop polyarchy-mcp polyarchy-stats qdrant
  [[ "$COMPANIES_ON" == 1 ]] && systemctl stop polyarchy-companies
  [[ "$DELIB_ON" == 1 ]] && systemctl stop polyarchy-deliberations qdrant-deliberations
  return 0
}
# 入口（cloudflared）＝config.yml が「いま動いているプロセスが読んだ内容」から変わったときだけ再起動する。
#   bootstrap ⑦ は再走行のたびに config.yml を生成する（ingress は deploy.env の ENABLE_*_APP／TUNNEL_HOST_* から）が、
#   cloudflared は起動時にしか config を読まない＝ingress を足しても再起動しなければ新ホストは 404（トンネルの catch-all）のまま。
#   毎回 restart しない理由＝通常のデータ更新で入口を揺らさない（stats/recommendations に数秒の断が出る）。
#   cloudflared が動いていない箱（初回カットオーバー前＝入口は手動 start・bootstrap ⑩）では何もしない＝安全順序を崩さない。
CF_CFG=/etc/cloudflared/config.yml
cf_cfg_hash() { [[ -f "$CF_CFG" ]] && sha256sum "$CF_CFG" | cut -d' ' -f1 || echo none; }
CF_CFG_LIVE="$(cf_cfg_hash)"   # 適用前＝稼働中の cloudflared が読んでいる config（切り戻し経路でも同じ基準で比べる）
reload_ingress() {
  local now; now="$(cf_cfg_hash)"
  [[ "$now" == "$CF_CFG_LIVE" ]] && return 0
  if systemctl is-active --quiet cloudflared; then
    log "入口: $CF_CFG が変わった（ingress の追加・削除）＝cloudflared を再起動（数秒の断）"
    systemctl restart cloudflared || warn "cloudflared の再起動に失敗（origin は健全・入口だけが古い＝RUNBOOK §1「公開 URL が落ちている」）"
  else
    log "入口: $CF_CFG が変わったが cloudflared は停止中＝起動しない（初回カットオーバーは手動 start＝bootstrap ⑩）"
  fi
  CF_CFG_LIVE="$now"
}
svc_start() { # qdrant → 応答待ち（最長 300 秒）→ アプリ → 入口（config が変わったときだけ）。戻り値＝qdrant が応答したか
  systemctl restart qdrant
  for _ in $(seq 1 60); do
    curl -fsS -m 3 http://127.0.0.1:6333/collections >/dev/null 2>&1 && break
    sleep 5
  done
  local ok=0
  curl -fsS -m 3 http://127.0.0.1:6333/collections >/dev/null 2>&1 && ok=1
  systemctl restart polyarchy-mcp polyarchy-stats
  [[ "$COMPANIES_ON" == 1 ]] && { systemctl restart polyarchy-companies || ok=0; }
  if [[ "$DELIB_ON" == 1 ]]; then
    systemctl restart qdrant-deliberations || ok=0
    for _ in $(seq 1 24); do curl -fsS -m 3 http://127.0.0.1:6340/collections >/dev/null 2>&1 && break; sleep 5; done
    systemctl restart polyarchy-deliberations || ok=0
  fi
  reload_ingress
  [[ "$ok" == 1 ]]
}
# 稼働中のサービス（systemd の unit・サービスごとの env・HTTP の待受・install_auth）は stdio の smoke を通らない＝
#   /healthz（health_metric.sh と同じポート）が 200 を返すまで待つ。Type=simple の restart は起動直後に落ちても
#   成功を返すので、ここで初めて「配った形で起動した」と言える（2026-10-09）。mcp はモデル読込で遅い＝最長 300 秒。
wait_healthz() { # $1=名前 $2=port → 0/1
  for _ in $(seq 1 60); do
    curl -fsS -m 5 "http://127.0.0.1:$2/healthz" >/dev/null 2>&1 && return 0
    sleep 5
  done
  warn "$1 の /healthz（:$2）が 300 秒以内に 200 を返さない（unit・env・認証の配線を journalctl -u で確認）"
  return 1
}
smoke() { # 箱上 smoke（recommendations＋stats＋companies〔有効時〕＋deliberations〔有効時〕）＋稼働中の /healthz。戻り値＝合否。訓練フラグがあれば FAIL 扱い
  local ok=1
  sudo -u polyarchy bash -lc "cd $REPO_DIR; PYTHONPATH=$REPO_DIR HF_HOME=$HF_HOME COLLECTION_NAME=$COLLECTION_NAME VECTOR_BACKEND=${VECTOR_BACKEND:-qdrant} $PY -m recommendations.eval.mcp_smoke" || ok=0
  sudo -u polyarchy bash -lc "cd $REPO_DIR; PYTHONPATH=$REPO_DIR $PY -m stats.eval.mcp_smoke" || ok=0
  if [[ "$COMPANIES_ON" == 1 ]]; then
    sudo -u polyarchy bash -lc "cd $REPO_DIR; PYTHONPATH=$REPO_DIR $PY -m companies.eval.mcp_smoke" || ok=0
  fi
  if [[ "$DELIB_ON" == 1 ]]; then
    sudo -u polyarchy bash -lc "cd $REPO_DIR; PYTHONPATH=$REPO_DIR HF_HOME=$HF_HOME DELIB_QDRANT_URL=http://127.0.0.1:6340 $PY -m deliberations.eval.mcp_smoke" || ok=0
  fi
  wait_healthz mcp 8765 || ok=0
  if [[ "${ENABLE_STATS_APP:-false}" == "true" ]]; then wait_healthz stats 8766 || ok=0; fi
  if [[ "$COMPANIES_ON" == 1 ]]; then wait_healthz companies 8767 || ok=0; fi
  if [[ "$DELIB_ON" == 1 ]]; then wait_healthz deliberations 8768 || ok=0; fi
  if [[ -e "$DRILL_FLAG" ]]; then warn "訓練フラグ $DRILL_FLAG あり＝smoke を FAIL 扱いにする（演習）"; ok=0; fi
  [[ "$ok" == 1 ]]
}
write_mark() { # $1=status(APPLIED|ROLLED_BACK) $2=smoke $3=rollback_smoke("" なら省略)
  python3 - "$TMP" "$MARK" "$1" "$2" "$3" "$SERVING_JSON" "$NEW_VER" <<'PYEOF'
import json, sys, datetime
m = json.load(open(sys.argv[1]))
status, smoke, rb_smoke, serving, new_ver = sys.argv[3], sys.argv[4], sys.argv[5], json.loads(sys.argv[6]), sys.argv[7]
import zoneinfo
m["applied_at"] = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")  # 箱は UTC＝JST で刻む（released_at と揃える）
m["status"] = status
m["smoke"] = smoke
if status == "APPLIED":
    m["serving"] = {"released_at": m.get("released_at"), "code_version": new_ver}
else:
    m["serving"] = serving or {}
    m["rollback_smoke"] = rb_smoke
json.dump(m, open(sys.argv[2], "w"), ensure_ascii=False, indent=1)
PYEOF
  chown polyarchy:polyarchy "$MARK"
}

# ── ① 退避（1 回目＝稼働中・大物の転送を先に済ませる）────────────────────────
log "① 退避（rsync → ${RB_DIR}）※この間サービスは稼働中。②以降の停止が 3 分を超えると health アラームが ALARM→OK と 1 往復する（想定内・RUNBOOK §5）"
# 中止でも dataapply=0 を送る（同じアラームで気づく＝RUNBOOK §5）。マークは書かない＝ダッシュボードは稼働中の版のまま（事実どおり）。
fail_early() { warn "①退避段で失敗（$1）＝サービスは無停止のまま中止（切り戻しではない）。原因（ディスク・S3 権限）を直せば次の 15 分で再試行"; metric 0; exit 1; }
snapshot_data || fail_early "rsync"
if [[ -f "$REL_DIR/current.tar.gz" ]]; then cp -f "$REL_DIR/current.tar.gz" "$REL_DIR/prev.tar.gz" || fail_early "prev tar"; fi
aws s3 cp "s3://$S3_BUCKET/$CODE_S3_KEY" "$REL_DIR/next.tar.gz" --region "$AWS_REGION" --quiet || fail_early "code tar 取得"

# ── ② 反映（ここから先で失敗したら切り戻し）────────────────────────────────
apply_new() { # 各段は失敗したら即 return 1（set +e の下で呼ぶため明示）
  svc_stop || return 1
  snapshot_data || return 1   # 2 回目＝停止後の差分（整合コピーの確定）
  log "② コード反映（tar 再展開 ${NEW_VER}）→ qdrant ミラー同期 → bootstrap"
  extract_code "$REL_DIR/next.tar.gz" || { warn "tar 再展開に失敗"; return 1; }
  # ★Qdrant ストレージは新旧セグメント混在を許せない＝ミラー同期（--delete）を apply が自前で行う
  #   （bootstrap ⑥ の --delete なし同期は捕捉ログ温存のための仕様＝qdrant には適用不可。2026-09-03 実測の恒久対策）
  #   --exact-timestamps＝大きさが同じで中身の違うファイル（固定長のページ等・箱の qdrant が実行中に触ったもの）も取り直す（B28）
  aws s3 sync "s3://$S3_BUCKET/$DATA_S3_PREFIX/qdrant/" "$APP_DIR/data/qdrant/" --delete --exact-timestamps --region "$AWS_REGION" --only-show-errors \
    || { warn "qdrant 同期に失敗"; return 1; }
  chown -R polyarchy:polyarchy "$APP_DIR/data"
  bash "$REPO_DIR/deploy/bootstrap/bootstrap.sh" || { warn "bootstrap 再走行に失敗"; return 1; }
  svc_start || { warn "qdrant が起動しない"; return 1; }
  log "③ 箱上 smoke（recommendations＋stats＋companies〔有効時〕＋deliberations〔有効時〕）＋稼働中の /healthz"
  smoke
}
set +e
apply_new; APPLY_RC=$?
set -e

if [[ "$APPLY_RC" == 0 ]]; then
  mv -f "$REL_DIR/next.tar.gz" "$REL_DIR/current.tar.gz"
  write_mark APPLIED PASS ""
  metric 1
  # 完了の行は更新チェックより先に出す＝更新チェック（上流への取得を含み、終わるまで戻らない）を待たずに CW Logs で適用の完了を確定できる
  log "✅ APPLIED: ${NEW_AT}（code ${NEW_VER}・smoke PASS）→ 更新チェックとダッシュボード更新を開始"
  # ダッシュボードを先に再生成して S3 へ（polyarchy ユーザで＝ファイル所有を崩さない）＝版一致を適用直後に確かめられる。
  # 更新チェック（上流への取得で長くかかる）の後ろに置くと、S3 のダッシュボードが次の毎時 05 分まで旧版のままだった（2026-09-26・27 staging）
  systemctl start polyarchy-dashboard.service && log "ダッシュボード更新済（S3 ops/dashboard/）" \
    || warn "ダッシュボードの再生成に失敗（次の毎時 05 分の生成を待つ）"
  # 更新チェックは待たずに裏で回す（終われば自分でダッシュボードを再生成する）＝「新着 N 件」が取込後も翌朝まで残らない
  systemctl start --no-block polyarchy-updatecheck.service || true
  exit 0
fi

# ── ④ 自動切り戻し（smoke FAIL／qdrant 起動不能／反映途中の失敗）────────────
warn "適用失敗＝自動切り戻し開始（戻し先: ${SERVING_JSON}）"
set +e
svc_stop
restore_data
if [[ -f "$REL_DIR/prev.tar.gz" ]]; then
  extract_code "$REL_DIR/prev.tar.gz"; pip_resolve
  cp -f "$REL_DIR/prev.tar.gz" "$REL_DIR/current.tar.gz"
else
  warn "prev tar なし＝コードは戻せない（初回適用）。データのみ戻す"
fi
rm -f "$REL_DIR/next.tar.gz"
RB_SMOKE=FAILED
if svc_start; then
  # 演習フラグは切り戻し後の検証には効かせない（戻した側の健全性をそのまま測る）
  if [[ -e "$DRILL_FLAG" ]]; then mv "$DRILL_FLAG" "$DRILL_FLAG.used"; fi
  smoke && RB_SMOKE=PASS
  [[ -e "$DRILL_FLAG.used" ]] && mv "$DRILL_FLAG.used" "$DRILL_FLAG"
else
  warn "切り戻し後も qdrant が起動しない＝RUNBOOK §3（S3 からの復元）へ"
fi
set -e
write_mark ROLLED_BACK FAILED "$RB_SMOKE"
metric 0
/usr/local/bin/polyarchy-dashboard || true
warn "⏪ 切り戻し完了: リリース $NEW_AT は不採用（旧版でサービング中・切り戻し後 smoke ${RB_SMOKE}）。"
warn "   次＝ローカルで原因を直して再 release（マニフェストが新しくなれば自動で再適用）。RUNBOOK §5「切り戻し後」"
exit 1   # unit failure として journal に残す（メトリクス dataapply=0 → アラーム → メール）
