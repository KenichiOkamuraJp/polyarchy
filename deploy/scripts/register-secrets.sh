#!/usr/bin/env bash
# §2 だけを単体で回す薄いラッパ。使い方: bash register-secrets.sh <staging|prod>
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
load_env "${1:-}"
do_register_secrets
