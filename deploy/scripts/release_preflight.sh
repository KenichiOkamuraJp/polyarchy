#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# release.sh の「配る前の確認」＝ゲートが測るもの（手元の Qdrant・Python・env）と配るもの（このフォルダのデータ・
# HEAD のコード）が同じかを機械で確かめる（2026-10-09＝以前は RUNBOOK §5 の確認を人が打っていた）。
#
#   release.sh が source して `preflight` を呼ぶ（COLLECTION_NAME の export を release.sh のシェルに効かせるため
#   別プロセスにしない）。単体で先に回す（読み取りだけ・何も配らない＝RUNBOOK §5）＝リポジトリ root で
#   `bash -c 'source deploy/env/<env>.env; REPO_DIR=$PWD; source deploy/scripts/release_preflight.sh; preflight'`
#   ＝env ファイルを読ませて release.sh と同じ変数で走らせる。前提の変数（REPO_DIR・AWS_PROFILE・ENABLE_DELIBERATIONS_APP・
#   RECOMMENDATIONS_COLLECTION_NAME・DELIB_QDRANT_URL〔任意〕）を手で渡すと、ENABLE_DELIBERATIONS_APP の書き忘れで
#   qdrant-delib の確認が黙って抜ける。
#
# 確かめること（1 つでも外れたら非 0＝配らない）:
#   a. qdrant-dev（と審議会DB 有効時の qdrant-delib）のマウント元＝このフォルダの data/qdrant（測る索引＝配る索引）
#   b. 配布に使う AWS のプロファイルが通る（sts。ログインからの経過時間は人の判断＝表示だけ）
#   c. COLLECTION_NAME＝env ファイルの RECOMMENDATIONS_COLLECTION_NAME（シェルに別の値があれば中止）・
#      DELIB_COLLECTION は未設定か deliberations_v1（配布・箱の unit はこの名前で決め打ち）
#   d. 検索の調整値（recommendations・deliberations の config が os.getenv で読む名前）がシェルに export されていない
#   e. ゲートを回す python の依存＝箱のロック（check_lock.py）
#   f. 政策主張DB の BM25 語彙の n_docs＝qdrant-dev の点数（語彙と索引が同じ時点）
#   g. コードの範囲（*/data/ 以外）に未コミットの差分が無い（tar は HEAD から作る＝測ったコードと配るコードを揃える）
# ═══════════════════════════════════════════════════════════════════════════

PF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # このスクリプトの置き場（check_lock.py を並べて置く）
pf_fail() { echo "❌ 配る前の確認で中止: $*" >&2; return 1; }

pf_mount() { # $1=コンテナ $2=期待するマウント元（このフォルダの data/qdrant）
  local src
  src="$(docker inspect "$1" --format '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)" \
    || { pf_fail "$1 の情報を取れない（docker start $1）"; return 1; }
  [[ -n "$src" ]] || { pf_fail "$1 に /qdrant/storage のマウントが無い"; return 1; }
  [[ -d "$2" ]] || { pf_fail "配る索引の置き場 ${2} が無い"; return 1; }
  if [[ "$(cd "$src" 2>/dev/null && pwd -P)" != "$(cd "$2" 2>/dev/null && pwd -P)" ]]; then
    pf_fail "$1 のマウント元が ${src}（配るのは ${2}）＝ゲートが測る索引と配る索引が違う。配布用のフォルダの data/qdrant をマウントした $1 でゲートを回す（RUNBOOK §5）"
    return 1
  fi
  echo "  ✓ $1 のマウント元＝${2}"
}

pf_tuning() { # 検索の調整値の混入（名前は config から拾う＝一覧を手で写さない）
  local n v bad=()
  # 走査の対象＝検索の構成を env から読むファイル（各コーパスの config・Qdrant の接続先を読む 2 つ）
  for n in $(grep -ohE 'os\.(getenv|environ\.get)\("[A-Z0-9_]+"' \
               "$REPO_DIR/recommendations/core/config.py" "$REPO_DIR/deliberations/core/config.py" \
               "$REPO_DIR/recommendations/core/qdrant_store.py" "$REPO_DIR"/polyarchy_retrieval/*.py \
             | sed -E 's/.*\("//; s/"$//' | sort -u); do
    v="$(printenv "$n" || true)"
    [[ -z "$v" ]] && continue
    case "$n" in
      ANTHROPIC_API_KEY|OPENAI_API_KEY|INGEST_NUM_WORKERS|EMBED_TIMEOUT) ;;   # 鍵・取込だけが読む値＝検索の結果を変えない
      COLLECTION_NAME|DELIB_COLLECTION) ;;                                    # c で検査する
      DELIB_QDRANT_URL) ;;                                                    # release.sh 自身がゲートと束の書き出しに使う
      QDRANT_URL) [[ "$v" == "http://localhost:6333" ]] || bad+=("QDRANT_URL=${v}（ゲートは :6333 の qdrant-dev を測る）") ;;
      VECTOR_BACKEND) [[ "$v" == "qdrant" ]] || bad+=("VECTOR_BACKEND=${v}") ;;
      *) bad+=("${n}=${v}") ;;
    esac
  done
  if (( ${#bad[@]} )); then
    pf_fail "検索の調整値がシェルに export されている＝ゲートが箱と違う構成を測る（unset してから再実行）: ${bad[*]}"
    return 1
  fi
  echo "  ✓ 検索の調整値の混入なし"
}

pf_vocab() { # 政策主張DB の語彙と索引が同じ時点か
  local out
  out="$(python - "$REPO_DIR/recommendations/data/bm25/${COLLECTION_NAME}_vocab.json.gz" "$COLLECTION_NAME" 2>&1 <<'PYEOF'
import gzip, json, sys, urllib.request
path, coll = sys.argv[1], sys.argv[2]
try:
    n_docs = json.load(gzip.open(path, "rt", encoding="utf-8"))["params"]["n_docs"]
except FileNotFoundError:
    sys.exit(f"語彙ファイルが無い: {path}")
req = urllib.request.Request(f"http://localhost:6333/collections/{coll}/points/count", method="POST",
                             data=b'{"exact": true}', headers={"Content-Type": "application/json"})
try:
    points = json.load(urllib.request.urlopen(req, timeout=30))["result"]["count"]
except Exception as e:  # noqa: BLE001
    sys.exit(f"qdrant-dev（:6333）の {coll} の点数を取れない（docker start qdrant-dev・コレクション名を確かめる）: {e}")
if points != n_docs:
    sys.exit(f"BM25 語彙の n_docs {n_docs} と qdrant-dev の {coll} の点数 {points} が違う＝語彙が索引と同じ時点でない")
print(points)
PYEOF
)" || { pf_fail "$out"; return 1; }
  echo "  ✓ BM25 語彙と索引が一致（${COLLECTION_NAME}・${out} 点）"
}

preflight() {
  echo "── ⓪ 配る前の確認（測るもの＝配るもの）"
  : "${REPO_DIR:?}" "${AWS_PROFILE:?}"
  # a. マウント元
  pf_mount qdrant-dev "$REPO_DIR/recommendations/data/qdrant" || return 1
  if [[ "${ENABLE_DELIBERATIONS_APP:-false}" == "true" ]]; then
    pf_mount qdrant-delib "$REPO_DIR/deliberations/data/qdrant" || return 1
  fi
  # b. 配布に使うプロファイル（読み取りだけ）
  aws sts get-caller-identity --profile "$AWS_PROFILE" >/dev/null 2>&1 \
    || { pf_fail "AWS プロファイル ${AWS_PROFILE} が通らない（aws sso login --profile ${AWS_PROFILE}）"; return 1; }
  echo "  ✓ AWS プロファイル ${AWS_PROFILE} が通る（★ログインからの経過時間は人が見る：転送の途中で切れると箱は動かないが S3 は半端＝RUNBOOK §5）"
  # c. コレクション名＝測るもの・配るもの・箱の env を 1 つの源に
  local want="${RECOMMENDATIONS_COLLECTION_NAME:-policy_claims_v7}"
  if [[ -n "${COLLECTION_NAME:-}" && "$COLLECTION_NAME" != "$want" ]]; then
    pf_fail "シェルの COLLECTION_NAME=${COLLECTION_NAME} が env ファイルの RECOMMENDATIONS_COLLECTION_NAME=${want} と違う（unset して再実行）"
    return 1
  fi
  export COLLECTION_NAME="$want"
  if [[ -n "${DELIB_COLLECTION:-}" && "$DELIB_COLLECTION" != "deliberations_v1" ]]; then
    pf_fail "DELIB_COLLECTION=${DELIB_COLLECTION}（配布と箱は deliberations_v1 決め打ち＝開発用の名前のまま release しない）"
    return 1
  fi
  echo "  ✓ コレクション名 ${COLLECTION_NAME}（env ファイル由来）"
  # d. 調整値
  pf_tuning || return 1
  # e. ロック
  python "$PF_DIR/check_lock.py" "$REPO_DIR/deploy/requirements/lock-recommendations-stats.txt" || { pf_fail "ゲートを回す python の依存が箱のロックと違う"; return 1; }
  # f. 語彙と索引
  pf_vocab || return 1
  # g. コードの範囲の未コミット（data/ 配下の差分＝update.sh が書き換える catalog.csv 等は止めない）
  local dirty
  dirty="$(git -C "$REPO_DIR" status --porcelain -- . ':(exclude)*/data/*' ':(exclude)VERSION')"
  if [[ -n "$dirty" ]]; then
    pf_fail "コードの範囲に未コミットの差分がある（tar は HEAD から作る＝測ったコードと配るコードが食い違う）:
$dirty"
    return 1
  fi
  echo "  ✓ コードの範囲に未コミットの差分なし（tar＝HEAD）"
}
