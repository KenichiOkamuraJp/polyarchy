#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# 依存ロックの（再）生成（B17 (1)・運用設計 §2.6・RUNBOOK §7）。
#   出力＝deploy/requirements/lock-recommendations-stats.txt（箱の既定 extras）・lock-stats.txt（stats だけの箱）
#   箱（Ubuntu 22.04・x86_64・py3.12）と同じ条件で解決するため docker（linux/amd64・python:3.12-slim をダイジェスト固定）で
#   pip-compile --generate-hashes を回す。Mac 上で直接回すと Mac 向けの解決になる（torch +cpu が無い等）ので使わない。
#
#   bash deploy/scripts/lock_deps.sh                      # pyproject の変更を反映（既存の版は保つ＝pip-compile の既定）
#   bash deploy/scripts/lock_deps.sh --upgrade            # 全依存を最新へ（★更新は PR＝ゲート 9 本を通してから）
#   bash deploy/scripts/lock_deps.sh --upgrade-package X  # 1 つだけ上げる
#
# 生成後＝ローカルで同じロックから env を作ってゲート 9 本（RUNBOOK §7）→ release.sh で配布（箱の bootstrap ③ が --require-hashes で導入）。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="deploy/requirements"
# 生成環境も固定（イメージのダイジェスト・pip-tools の版）。上げるときはここを変えてコミット。
IMAGE="python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"   # python:3.12-slim（2026-09-04 取得）
PIP_TOOLS="pip-tools==7.6.1"
TORCH_INDEX="https://download.pytorch.org/whl/cpu"   # GPU 無しの箱＝CPU 版 torch（PyPI 版は CUDA 同梱で巨大）

command -v docker >/dev/null || { echo "docker が要る（qdrant-dev と同じ Docker Desktop）" >&2; exit 1; }
echo "[lock] docker linux/amd64 で pip-compile（$*）"
docker run --rm --platform linux/amd64 -v "$REPO_DIR:/repo" -w /tmp/proj "$IMAGE" bash -c '
  set -euo pipefail
  mkdir -p /tmp/proj && cp /repo/pyproject.toml /tmp/proj/    # egg-info 等をリポジトリに書かせない
  pip install -q --root-user-action=ignore "'"$PIP_TOOLS"'"
  common=(--generate-hashes --allow-unsafe --strip-extras --no-header "$@")
  pip-compile "${common[@]}" --extra recommendations --extra stats --extra-index-url '"$TORCH_INDEX"' \
    -o /repo/'"$OUT_DIR"'/lock-recommendations-stats.txt pyproject.toml /repo/'"$OUT_DIR"'/build.in
  pip-compile "${common[@]}" --extra stats \
    -o /repo/'"$OUT_DIR"'/lock-stats.txt pyproject.toml /repo/'"$OUT_DIR"'/build.in
' _ "$@"
# 出力のパス表記を安定させる（コンテナ内の /repo/… → リポジトリ相対。差分を読みやすくする）
sed -i '' 's#/repo/##g' "$REPO_DIR/$OUT_DIR"/lock-*.txt
echo "[lock] 生成: $OUT_DIR/lock-recommendations-stats.txt・lock-stats.txt"
grep -cE '^[a-zA-Z0-9_.-]+==' "$REPO_DIR/$OUT_DIR"/lock-*.txt
echo "[lock] 次＝RUNBOOK §7（ローカル env をロックから作り直し → ゲート 9 本 → release.sh）"
