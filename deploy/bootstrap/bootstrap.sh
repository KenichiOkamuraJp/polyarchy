#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Polyarchy RAG ブートストラップ（Ubuntu 22.04 / x86）。
# user_data から呼ばれるが、SSM で入って手動で再走行しても良い（冪等に作ってある）。
# 手順（ロードマップ§2.2）：
#   ①OS準備 → ②Miniconda → ③env polyarchy(py3.12)+依存はロック（--require-hashes）＋本体 -e . --no-deps → ④fugashi辞書検証
#   → ⑤HFモデル事前DL(EBS固定) → ⑥S3からデータ取得 → ⑦cloudflared導入+config
#   → ⑧MCP 秘密パス＋Access 設定（SSM→env）→ ⑨web 秘密（staging のみ）→ ⑩systemd 設置（mcp/(web)/(stats)/fuelsync/logprune を enable。cloudflared はカットオーバー時に手動 start）
# 秘密は .env で運ばず SSM Parameter Store から取る（仕様§3.3/§4.1）。ログは日本語。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
exec > >(tee -a /var/log/polyarchy-bootstrap.log) 2>&1
echo "════════ [bootstrap] 開始 $(date -Is) ════════"

# ── 設定の読み込み（Terraform user_data が書いた確定値）───────────────────────
DEPLOY_ENV=/etc/polyarchy/deploy.env
if [[ ! -f "$DEPLOY_ENV" ]]; then
  echo "[bootstrap] $DEPLOY_ENV が無い。単独実行なら先に作成すること（terraform/user_data.sh.tftpl 参照）" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$DEPLOY_ENV"

: "${AWS_REGION:?}" "${S3_BUCKET:?}" "${PROJECT:?}" "${ENVIRONMENT:?}"
: "${APP_DIR:?}" "${INSTALL_DIR:?}" "${HF_HOME:?}" "${COLLECTION_NAME:?}"
# REPO_DIR＝tar 展開先のリポジトリ root（deploy/ はここ）。旧 deploy.env（APP_DIR のみ）との互換で既定を補う。
REPO_DIR="${REPO_DIR:-$INSTALL_DIR/polyarchy}"
: "${DATA_S3_PREFIX:?}" "${ENABLE_WEB_APP:?}"
# stats（統計参照DB・別プロセス :8766）は opt-in。deploy.env に ENABLE_STATS_APP=true で設置（B6 案 B・C2 以降）。
ENABLE_STATS_APP="${ENABLE_STATS_APP:-false}"

SVC_USER=polyarchy
CONDA_DIR=/opt/miniconda
CONDA_ENV=polyarchy
PY_VER=3.12
ENV_PY="$CONDA_DIR/envs/$CONDA_ENV/bin/python"
ENV_PIP="$CONDA_DIR/envs/$CONDA_ENV/bin/pip"
SSM_PREFIX="/$PROJECT/$ENVIRONMENT"

ssm_get() { # $1=パラメータ名(相対) → 値を stdout（無ければ空・失敗させない）
  aws ssm get-parameter --region "$AWS_REGION" --with-decryption \
    --name "$SSM_PREFIX/$1" --query 'Parameter.Value' --output text 2>/dev/null || true
}

# ── ① OS 準備 ────────────────────────────────────────────────────────────────
echo "[bootstrap] ① OS パッケージ"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
# build-essential＝fugashi 等のCビルド保険／git・curl・bzip2＝取得系。
apt-get install -y build-essential git curl wget bzip2 ca-certificates jq rsync   # rsync＝apply の退避/切り戻し

# サービス用ユーザ（ログインシェル無し・home は INSTALL_DIR）。
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi
mkdir -p "$INSTALL_DIR" "$HF_HOME" /etc/polyarchy
# コードは root（user_data）が展開済み。以後の書込み先を含め所有をサービスユーザへ。
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR"

# ── ② Miniconda（システム共通・/opt/miniconda）─────────────────────────────
if [[ ! -x "$CONDA_DIR/bin/conda" ]]; then
  echo "[bootstrap] ② Miniconda 導入"
  curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -o /tmp/miniconda.sh
  bash /tmp/miniconda.sh -b -p "$CONDA_DIR"
  chown -R "$SVC_USER:$SVC_USER" "$CONDA_DIR"
fi

# ── ③ env polyarchy（py3.12）＋依存 ─────────────────────────────────────────
if [[ ! -x "$ENV_PY" ]]; then
  echo "[bootstrap] ③ conda env $CONDA_ENV(py$PY_VER) 作成"
  # 近年の conda は defaults チャンネルの Terms of Service 明示同意が必須で、非対話だと
  # CondaToSNonInteractiveError で create が落ちる。①ToS を受諾（古い conda に tos サブコマンドが
  # 無ければ || true で無視）②create は conda-forge 固定で defaults を回避＝二重の保険。
  sudo -u "$SVC_USER" "$CONDA_DIR/bin/conda" tos accept --override-channels \
    --channel https://repo.anaconda.com/pkgs/main || true
  sudo -u "$SVC_USER" "$CONDA_DIR/bin/conda" tos accept --override-channels \
    --channel https://repo.anaconda.com/pkgs/r || true
  sudo -u "$SVC_USER" "$CONDA_DIR/bin/conda" create -y -n "$CONDA_ENV" \
    -c conda-forge --override-channels "python=$PY_VER"
fi

# 依存は pyproject の 3 層（共通／recommendations／stats）。箱は recommendations が主役なので既定 extras は recommendations(+stats)。
# stats だけの箱なら PIP_EXTRAS=stats（torch を引かない＝bootstrap が大幅に軽い）。
# ★2026-09-04（B17 (1)・運用設計 §2.6）：依存は**ロックファイル（sha256 付き）からのみ**入れる＝
#   deploy/requirements/lock-<extras>.txt（pip-compile --generate-hashes・箱と同じ Linux x86_64／py3.12 で生成。
#   再生成の手順＝RUNBOOK §7）。--require-hashes＝ロックに無い版・ハッシュ不一致の配布物は入らない（汚染パッケージの混入を塞ぐ）。
#   torch は CPU 版（+cpu・download.pytorch.org）をロックが指す＝以前の「torch を先に別 index で入れる」段は不要になった。
#   本体は -e . を --no-deps（依存解決はロックが唯一の真実）・--no-build-isolation（ビルド時依存 setuptools/wheel もロックに含めて固定）。
PIP_EXTRAS="${PIP_EXTRAS:-recommendations,stats}"
LOCK_FILE="$REPO_DIR/deploy/requirements/lock-${PIP_EXTRAS//,/-}.txt"
if [[ ! -f "$LOCK_FILE" ]]; then
  echo "[bootstrap] ✗ ロックファイルが無い: $LOCK_FILE（PIP_EXTRAS=${PIP_EXTRAS} に対応するロックを RUNBOOK §7 の手順で生成して tar に含めること）" >&2
  exit 1
fi
echo "[bootstrap] ③ 依存インストール（extras=${PIP_EXTRAS}・ロック $(basename "$LOCK_FILE")・--require-hashes）"
sudo -u "$SVC_USER" env HF_HOME="$HF_HOME" "$ENV_PIP" install --no-input --require-hashes -r "$LOCK_FILE"
sudo -u "$SVC_USER" env HF_HOME="$HF_HOME" "$ENV_PIP" install --no-input --no-deps --no-build-isolation -e "$REPO_DIR"

# ── ④ fugashi 辞書の検証（Mac の暗黙依存に注意・ロードマップ§6）───────────────
if [[ ",$PIP_EXTRAS," == *",recommendations,"* ]]; then
echo "[bootstrap] ④ fugashi(unidic-lite) 動作確認"
sudo -u "$SVC_USER" env HF_HOME="$HF_HOME" "$ENV_PY" - <<'PY'
import fugashi
tagger = fugashi.Tagger()  # unidic-lite が無いとここで失敗する
print("[bootstrap] fugashi OK:", [w.surface for w in tagger("日本語形態素の動作確認")])
PY
fi

# ── ⑤ HFモデル事前DL（EBS 固定・初回起動の外部依存を消す）─────────────────────
if [[ ",$PIP_EXTRAS," == *",recommendations,"* ]]; then
echo "[bootstrap] ⑤ HFモデル事前DL → $HF_HOME"
sudo -u "$SVC_USER" env HF_HOME="$HF_HOME" "$ENV_PY" \
  "$REPO_DIR/deploy/bootstrap/prefetch_models.py"
fi

# ── ⑥ データを S3 から取得（Chroma/PDF/eval/catalog/query_log）─────────────────
echo "[bootstrap] ⑥ S3 からデータ同期 s3://$S3_BUCKET/$DATA_S3_PREFIX/"
mkdir -p "$APP_DIR/data"
# ★sync は root で実行する。sudo -u polyarchy だと data/ が root 所有のときサブディレクトリを
#   作れず Permission denied になる（root は任意のパスを作成可・IMDS の instance role 資格は
#   どのユーザからでも使える）。取得後に所有権をまとめて polyarchy へ渡す。
# --delete は付けない（箱側で溜まった捕捉ログ＝燃料を消さない）。
aws s3 sync "s3://$S3_BUCKET/$DATA_S3_PREFIX/" "$APP_DIR/data/" --region "$AWS_REGION" --exclude "stats/*"
# .streamlit/config.toml は code tar に同梱済（fileWatcherType=none・§32.3）。
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR/data"
# stats（統計参照DB）のデータ＝S3 `data/stats/` → `stats/data/`（共通契約 §4）。tar は */data を除外するので
# registry（git 追跡）も S3 経由で届ける。query_log は箱で生成（--delete なし・上の recommendations と同じ思想）。
if [[ "$ENABLE_STATS_APP" == "true" ]]; then
  echo "[bootstrap] ⑥ stats データ同期 s3://$S3_BUCKET/$DATA_S3_PREFIX/stats/ → $REPO_DIR/stats/data/"
  mkdir -p "$REPO_DIR/stats/data"
  aws s3 sync "s3://$S3_BUCKET/$DATA_S3_PREFIX/stats/" "$REPO_DIR/stats/data/" --region "$AWS_REGION" --exclude "query_log/*"
  chown -R "$SVC_USER:$SVC_USER" "$REPO_DIR/stats/data"
fi

# ── ⑥b Qdrant 導入（VECTOR_BACKEND=qdrant のときだけ・v7 本線）──
# サーバは systemd 常駐（qdrant.service・⑩で設置）。docker は箱に入れない（依存を増やさない）。
# ★版はローカル qdrant-dev と同じに固定＝ストレージ形式の互換を保証（data/qdrant は
#   S3 経由の「ディレクトリ丸ごと配布」なので、読む側の版が新しすぎ/古すぎると起動しない）。
VECTOR_BACKEND="${VECTOR_BACKEND:-qdrant}"   # qdrant のみ有効（Chroma 経路は 2026-08-28 に全廃。user_data が常に明示する）
QDRANT_VERSION=1.19.0
QDRANT_SHA256=9ec667456443463eee390e43cd36988af6b730c6db807b4e39f57c303d0264a3
if [[ "$VECTOR_BACKEND" == "qdrant" ]]; then
  if ! { command -v qdrant >/dev/null 2>&1 && qdrant --version 2>/dev/null | grep -qF "$QDRANT_VERSION"; }; then
    echo "[bootstrap] ⑥b Qdrant v${QDRANT_VERSION} 導入（GitHub リリース・sha256 検証）"
    # ★musl（静的リンク）ビルドを使う：linux-gnu ビルドは GLIBC 2.38 要求で Ubuntu 22.04（glibc 2.35）では起動しない（2026-08-28 実測）。
    curl -fsSL -o /tmp/qdrant.tar.gz \
      "https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VERSION}/qdrant-x86_64-unknown-linux-musl.tar.gz"
    echo "${QDRANT_SHA256}  /tmp/qdrant.tar.gz" | sha256sum -c -
    tar -xzf /tmp/qdrant.tar.gz -C /tmp qdrant
    install -m 755 /tmp/qdrant /usr/local/bin/qdrant
    rm -f /tmp/qdrant.tar.gz /tmp/qdrant
  fi
  # データが未配布なら早めに警告（qdrant は空ストレージでも起動するが検索は該当コレクション無しで失敗する）。
  if [[ ! -d "$APP_DIR/data/qdrant/collections" ]]; then
    echo "[bootstrap] ⚠ $APP_DIR/data/qdrant/collections が無い＝S3 data/qdrant/ 未配布？（upload_to_s3.sh を確認）" >&2
  fi
fi

# ── ⑦ cloudflared 導入＋config（資格情報は SSM から・DNS は UUID 向きで不変）────
echo "[bootstrap] ⑦ cloudflared"
if ! command -v cloudflared >/dev/null 2>&1; then
  mkdir -p /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared jammy main" \
    > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -y
  apt-get install -y cloudflared
fi

mkdir -p /etc/cloudflared
TUNNEL_ID="$(ssm_get cloudflared_tunnel_id)"
TUNNEL_CRED="$(ssm_get cloudflared_credentials)"  # tunnel の <UUID>.json の中身（SecureString）
if [[ -n "$TUNNEL_ID" && -n "$TUNNEL_CRED" ]]; then
  printf '%s' "$TUNNEL_CRED" > "/etc/cloudflared/$TUNNEL_ID.json"  # echo だと -e 等の解釈事故を避ける
  chmod 600 "/etc/cloudflared/$TUNNEL_ID.json"
  # ingress：enable_web_app に応じて Web(8502) を出すか出さないか（prod は MCP のみ）。
  {
    echo "tunnel: $TUNNEL_ID"
    echo "credentials-file: /etc/cloudflared/$TUNNEL_ID.json"
    echo "ingress:"
    echo "  - hostname: $TUNNEL_HOST_MCP"
    echo "    service: http://localhost:8765"
    if [[ "$ENABLE_WEB_APP" == "true" && -n "${TUNNEL_HOST_WEB:-}" ]]; then
      echo "  - hostname: $TUNNEL_HOST_WEB"
      echo "    service: http://localhost:8502"
    fi
    if [[ "$ENABLE_STATS_APP" == "true" && -n "${TUNNEL_HOST_STATS:-}" ]]; then
      echo "  - hostname: $TUNNEL_HOST_STATS"
      echo "    service: http://localhost:8766"
    fi
    echo "  - service: http_status:404"
  } > /etc/cloudflared/config.yml
  echo "[bootstrap] cloudflared config を生成（tunnel=${TUNNEL_ID}）"
else
  echo "[bootstrap] ⚠ SSM に cloudflared_tunnel_id/credentials 未登録。config は後で（README §2/§7）。" >&2
fi

# ── ⑧ MCP の秘密パス（両環境）＝SSM → EnvironmentFile ─────────────────────────
# 公開URLに埋める「合言葉」。Claude のカスタムコネクタは Anthropic 経由で来るため送信元IPで
# 組織を絞れない（deploy/PROD_MIGRATION.md §2.3 の★）。その補完として推測不可能なパスを使う。
MCP_PATH="$(ssm_get mcp_http_path)"
# Cloudflare Access Managed OAuth（案 A＝不採用・資産温存。現行は案 B＝docs/個人認証_案B設計.md）。
#   SSM access_team_domain（例 xxxx.cloudflareaccess.com）＋ access_aud_<service>（Access アプリの AUD）が両方あると、
#   そのサービスの env に MCP_ACCESS_TEAM_DOMAIN/MCP_ACCESS_AUD を書き、origin 側で Cf-Access-Jwt-Assertion を検証
#   （polyarchy_common.mcp_http が自動装着・利用者 ID を捕捉ログに user_hash で残す）。無ければ従来どおり素の HTTP。
ACCESS_TEAM="$(ssm_get access_team_domain)"
# 外部 IdP（個人認証・案 B＝docs/個人認証_案B設計.md）。SSM auth_issuer＋auth_aud_<service> が両方あると、
# そのサービスの env に MCP_AUTH_ISSUER/MCP_AUTH_AUD/MCP_AUTH_RESOURCE_URL を書き、origin 側で
# Bearer JWT 検証＋PRM 配信（polyarchy_common.mcp_http が自動装着）。resource URL は公開ホスト＋秘密パス。
AUTH_ISSUER_SSM="$(ssm_get auth_issuer)"
write_service_env() { # $1=env ファイル $2=秘密パス $3=Access AUD $4=IdP AUD（案 B）$5=公開ホスト → 内容があれば 600 で書く・無ければ削除
  local f="$1" p="$2" aud="$3" idp_aud="${4:-}" pub_host="${5:-}"
  if [[ -z "$p" && -z "$aud" && -z "$idp_aud" ]]; then rm -f "$f"; return 0; fi
  umask 077
  : > "$f"
  [[ -n "$p" ]] && printf 'MCP_HTTP_PATH=%s\n' "$p" >> "$f"
  if [[ -n "$aud" && -n "$ACCESS_TEAM" ]]; then
    printf 'MCP_ACCESS_TEAM_DOMAIN=%s\nMCP_ACCESS_AUD=%s\n' "$ACCESS_TEAM" "$aud" >> "$f"
  fi
  if [[ -n "$idp_aud" && -n "$AUTH_ISSUER_SSM" && -n "$pub_host" ]]; then
    printf 'MCP_AUTH_ISSUER=%s\nMCP_AUTH_AUD=%s\nMCP_AUTH_RESOURCE_URL=https://%s%s\n' \
      "$AUTH_ISSUER_SSM" "$idp_aud" "$pub_host" "${p:-/mcp}" >> "$f"
  fi
  chown "$SVC_USER:$SVC_USER" "$f"; chmod 600 "$f"; umask 022
}
MCP_AUD="$(ssm_get access_aud_mcp)"
MCP_IDP_AUD="$(ssm_get auth_aud_mcp)"
write_service_env /etc/polyarchy/mcp.env "$MCP_PATH" "$MCP_AUD" "$MCP_IDP_AUD" "${TUNNEL_HOST_MCP:-}"
if [[ -n "$MCP_PATH" ]]; then
  echo "[bootstrap] MCP 秘密パスを SSM から設定（値はログに出さない）"
else
  echo "[bootstrap] ⚠ SSM に mcp_http_path 未登録＝既定パス /mcp で起動（推測可能）" >&2
fi
if [[ -n "$MCP_AUD" && -n "$ACCESS_TEAM" ]]; then
  echo "[bootstrap] recommendations(mcp): Access JWT 検証を有効化（team=${ACCESS_TEAM}）"
fi
if [[ -n "$MCP_IDP_AUD" && -n "$AUTH_ISSUER_SSM" ]]; then
  echo "[bootstrap] recommendations(mcp): IdP Bearer 検証を有効化（issuer=${AUTH_ISSUER_SSM}・案 B）"
fi

# ── ⑨ 秘密（staging web のみ Anthropic）＝SSM → EnvironmentFile ─────────────────
# prod は enable_web_app=false＝この経路を通らず「ランタイム秘密ゼロ」（仕様§2.3）。
if [[ "$ENABLE_WEB_APP" == "true" ]]; then
  ANTHROPIC_KEY="$(ssm_get anthropic_api_key)"
  if [[ -n "$ANTHROPIC_KEY" ]]; then
    umask 077
    printf 'ANTHROPIC_API_KEY=%s\n' "$ANTHROPIC_KEY" > /etc/polyarchy/web.env
    chown "$SVC_USER:$SVC_USER" /etc/polyarchy/web.env
    chmod 600 /etc/polyarchy/web.env
    echo "[bootstrap] web.env を SSM から生成（600・${SVC_USER}）"
  else
    echo "[bootstrap] ⚠ enable_web_app=true だが SSM anthropic_api_key 未登録＝web は生成不可" >&2
  fi
fi

# ── ⑩ systemd ユニット設置 ──────────────────────────────────────────────────
echo "[bootstrap] ⑩ systemd ユニット設置"
UNIT_SRC="$REPO_DIR/deploy/systemd"
install -m 644 "$UNIT_SRC/polyarchy-mcp.service" /etc/systemd/system/polyarchy-mcp.service
install -m 644 "$UNIT_SRC/polyarchy-fuelsync.service" /etc/systemd/system/polyarchy-fuelsync.service
install -m 644 "$UNIT_SRC/polyarchy-fuelsync.timer" /etc/systemd/system/polyarchy-fuelsync.timer
# 捕捉ログの保持期間（既定30日）を守る削除ジョブ（プライバシーポリシーの約束）。
install -m 644 "$UNIT_SRC/polyarchy-logprune.service" /etc/systemd/system/polyarchy-logprune.service
install -m 644 "$UNIT_SRC/polyarchy-logprune.timer" /etc/systemd/system/polyarchy-logprune.timer
install -m 644 "$UNIT_SRC/cloudflared.service" /etc/systemd/system/cloudflared.service
if [[ "$VECTOR_BACKEND" == "qdrant" ]]; then
  install -m 644 "$UNIT_SRC/qdrant.service" /etc/systemd/system/qdrant.service
else
  rm -f /etc/systemd/system/qdrant.service  # chroma の箱には置かない
fi
if [[ "$ENABLE_WEB_APP" == "true" ]]; then
  install -m 644 "$UNIT_SRC/polyarchy-web.service" /etc/systemd/system/polyarchy-web.service
else
  rm -f /etc/systemd/system/polyarchy-web.service  # prod は web ユニットを置かない
fi
if [[ "$ENABLE_STATS_APP" == "true" ]]; then
  # stats の秘密パス（SSM stats_http_path・無ければ既定 /mcp）＋ Access Managed OAuth（SSM access_aud_stats）。
  # recommendations の mcp.env とは別ファイル（write_service_env は上の⑧で定義）。
  STATS_PATH="$(ssm_get stats_http_path)"
  STATS_AUD="$(ssm_get access_aud_stats)"
  STATS_IDP_AUD="$(ssm_get auth_aud_stats)"
  write_service_env /etc/polyarchy/stats.env "$STATS_PATH" "$STATS_AUD" "$STATS_IDP_AUD" "${TUNNEL_HOST_STATS:-}"
  if [[ -n "$STATS_AUD" && -n "$ACCESS_TEAM" ]]; then
    echo "[bootstrap] stats: Access JWT 検証を有効化（team=${ACCESS_TEAM}）"
  fi
  if [[ -n "$STATS_IDP_AUD" && -n "$AUTH_ISSUER_SSM" ]]; then
    echo "[bootstrap] stats: IdP Bearer 検証を有効化（issuer=${AUTH_ISSUER_SSM}・案 B）"
  fi
  # e-Stat appId（SSM estat_app_id・SecureString）→ /etc/polyarchy/estat.env（更新チェックの freshness が読む。値はログに出さない）。
  # 環境ごとに取得する（導入団体 prod は導入団体が取得した appId）。無ければファイルを消す＝e-Stat 系は検知対象外。
  ESTAT_ID="$(ssm_get estat_app_id)"
  if [[ -n "$ESTAT_ID" ]]; then
    umask 077; printf 'ESTAT_APP_ID=%s\n' "$ESTAT_ID" > /etc/polyarchy/estat.env
    chown "$SVC_USER:$SVC_USER" /etc/polyarchy/estat.env; chmod 600 /etc/polyarchy/estat.env; umask 022
    echo "[bootstrap] stats: e-Stat appId を SSM から設定（estat.env）"
  else
    rm -f /etc/polyarchy/estat.env
    echo "[bootstrap] ⚠ SSM に estat_app_id 未登録＝更新チェックの e-Stat 系は検知しない" >&2
  fi
  mkdir -p "$REPO_DIR/stats/data/query_log" "$REPO_DIR/stats/data/cache"; chown -R "$SVC_USER:$SVC_USER" "$REPO_DIR/stats/data"
  install -m 644 "$UNIT_SRC/polyarchy-stats.service" /etc/systemd/system/polyarchy-stats.service
else
  rm -f /etc/systemd/system/polyarchy-stats.service
fi
# ユニットは固定パス＋EnvironmentFile=/etc/polyarchy/deploy.env で自己完結（deploy.env は
# user_data が生成済＝APP_DIR/HF_HOME/COLLECTION_NAME/S3_BUCKET 等）。drop-in は不要。

systemctl daemon-reload
# アプリ面は起動（origin を先に健全化）。cloudflared はアプリ面の健全を確認した後に手動 start＝
# 二重 origin（Mac と EC2 が同一トンネルに同時接続）を避ける安全順序（README §7）。
# enable + restart（enable --now は既存起動プロセスを再起動しない＝再走行でユニット更新が反映されない）。
# Qdrant はアプリより先に上げる（mcp/web は localhost:6333 に接続する。ユニット側も After=qdrant.service）。
if [[ "$VECTOR_BACKEND" == "qdrant" ]]; then
  systemctl enable qdrant.service
  systemctl restart qdrant.service
fi
systemctl enable polyarchy-mcp.service
systemctl restart polyarchy-mcp.service
systemctl enable --now polyarchy-fuelsync.timer
systemctl enable --now polyarchy-logprune.timer
if [[ "$ENABLE_WEB_APP" == "true" ]]; then
  systemctl enable polyarchy-web.service
  systemctl restart polyarchy-web.service || echo "[bootstrap] ⚠ web 起動失敗（web.env 未登録の可能性）" >&2
fi
if [[ "$ENABLE_STATS_APP" == "true" ]]; then
  systemctl enable polyarchy-stats.service
  systemctl restart polyarchy-stats.service || echo "[bootstrap] ⚠ stats 起動失敗" >&2
fi

# ── ⑪ 運用（運用設計 §1.1/§1.2/§4.2 段2＝A1 と同時・2026-08-28）────────
# メトリクス（health×2・disk）は health スクリプトが送る（CW agent の metrics は使わない＝アラーム定義が単純）。
# ログは SyslogIdentifier→rsyslog でサービス別ファイル→ CW agent が CloudWatch Logs（保持 30 日）へ。
echo "[bootstrap] ⑪ 運用（CW agent・ログ転送・health timer・週次レポート）"
if ! dpkg -s amazon-cloudwatch-agent >/dev/null 2>&1; then
  curl -fsSL -o /tmp/cwagent.deb https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
  dpkg -i /tmp/cwagent.deb
  rm -f /tmp/cwagent.deb
fi
install -m 644 "$REPO_DIR/deploy/bootstrap/cloudwatch-agent.json" /opt/aws/amazon-cloudwatch-agent/etc/polyarchy-cwagent.json
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/polyarchy-cwagent.json -s >/dev/null

mkdir -p /var/log/polyarchy
chown syslog:adm /var/log/polyarchy
install -m 644 "$REPO_DIR/deploy/bootstrap/rsyslog-polyarchy.conf" /etc/rsyslog.d/30-polyarchy.conf
install -m 644 "$REPO_DIR/deploy/bootstrap/logrotate-polyarchy" /etc/logrotate.d/polyarchy
systemctl restart rsyslog

install -m 755 "$REPO_DIR/deploy/bootstrap/health_metric.sh" /usr/local/bin/polyarchy-health-metric
install -m 755 "$REPO_DIR/deploy/bootstrap/usage_report_weekly.sh" /usr/local/bin/polyarchy-usage-report
install -m 755 "$REPO_DIR/deploy/bootstrap/ops_dashboard.sh" /usr/local/bin/polyarchy-dashboard
install -m 755 "$REPO_DIR/deploy/bootstrap/update_check.sh" /usr/local/bin/polyarchy-update-check
install -m 755 "$REPO_DIR/deploy/bootstrap/apply_data_update.sh" /usr/local/bin/polyarchy-data-apply
install -m 644 "$UNIT_SRC/polyarchy-health.service" /etc/systemd/system/polyarchy-health.service
install -m 644 "$UNIT_SRC/polyarchy-health.timer" /etc/systemd/system/polyarchy-health.timer
install -m 644 "$UNIT_SRC/polyarchy-usagereport.service" /etc/systemd/system/polyarchy-usagereport.service
install -m 644 "$UNIT_SRC/polyarchy-usagereport.timer" /etc/systemd/system/polyarchy-usagereport.timer
install -m 644 "$UNIT_SRC/polyarchy-dashboard.service" /etc/systemd/system/polyarchy-dashboard.service
install -m 644 "$UNIT_SRC/polyarchy-dashboard.timer" /etc/systemd/system/polyarchy-dashboard.timer
install -m 644 "$UNIT_SRC/polyarchy-updatecheck.service" /etc/systemd/system/polyarchy-updatecheck.service
install -m 644 "$UNIT_SRC/polyarchy-updatecheck.timer" /etc/systemd/system/polyarchy-updatecheck.timer
install -m 644 "$UNIT_SRC/polyarchy-dataapply.service" /etc/systemd/system/polyarchy-dataapply.service
install -m 644 "$UNIT_SRC/polyarchy-dataapply.timer" /etc/systemd/system/polyarchy-dataapply.timer
systemctl daemon-reload
systemctl enable --now polyarchy-health.timer
systemctl enable --now polyarchy-usagereport.timer
systemctl enable --now polyarchy-dashboard.timer
systemctl enable --now polyarchy-updatecheck.timer
systemctl enable --now polyarchy-dataapply.timer

echo "════════ [bootstrap] 完了 $(date -Is) ════════"
echo "[bootstrap] 次＝回帰ゲート4種を PASS（README §6）→ Tunnel カットオーバー（README §7）"
