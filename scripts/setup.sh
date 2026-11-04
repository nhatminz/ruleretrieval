#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${RULE_RETRIEVAL_ROOT:-$PROJECT_DIR}"
export UV_CACHE_DIR="$PWD/data/tool_cache/uv"
export UV_HTTP_TIMEOUT=300
if ! command -v uv >/dev/null; then echo 'Install uv or create a Python >=3.11 venv manually; see README.' >&2; exit 1; fi
if [[ ! -x .venv/bin/python ]]; then uv venv --python 3.12 .venv; fi
uv pip install --python .venv/bin/python -e '.[test]'
if [[ "${1:-}" == '--model' ]]; then
  uv pip install --python .venv/bin/python 'torch==2.5.1' --index-url https://download.pytorch.org/whl/cu121
  uv pip install --python .venv/bin/python 'torch==2.5.1' 'transformers>=4.48,<4.52' 'accelerate>=1,<2'
fi
