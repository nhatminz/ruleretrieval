#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export RULE_RETRIEVAL_ROOT="${RULE_RETRIEVAL_ROOT:-$PROJECT_DIR}"
cd "$RULE_RETRIEVAL_ROOT"
export UV_CACHE_DIR="$RULE_RETRIEVAL_ROOT/data/tool_cache/uv"
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-300}"
export HF_HOME="$RULE_RETRIEVAL_ROOT/data/hf_cache"
export HF_HUB_CACHE="$HF_HOME/hub"
export HF_XET_CACHE="$HF_HOME/xet"
export HF_DATASETS_CACHE="$HF_HOME/datasets"
export HF_HUB_DISABLE_XET=1
PYTHON="${RULE_RETRIEVAL_PYTHON:-$RULE_RETRIEVAL_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo 'Project Python missing. Run scripts/setup.sh first.' >&2
  exit 1
fi
