#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
if [[ -z "${RULE_RETRIEVAL_MODEL_PATH:-}" && "$*" != *'--model-path'* ]]; then
  echo 'Set RULE_RETRIEVAL_MODEL_PATH or pass --model-path /path/to/checkpoint.' >&2
  exit 1
fi
if [[ "${RULE_RETRIEVAL_SERVER_BACKEND:-transformers}" == 'vllm' ]]; then
  if ! "$PYTHON" -c 'import vllm' >/dev/null 2>&1; then echo 'vLLM is not installed in project Python; use Transformers or a separate compatible vLLM environment.' >&2; exit 1; fi
  exec "$PYTHON" -m vllm.entrypoints.openai.api_server --model "$RULE_RETRIEVAL_MODEL_PATH" --served-model-name "${RULE_RETRIEVAL_MODEL_ID:-qwen-local}" --host 127.0.0.1 "$@"
fi
exec "$PYTHON" -m rule_retrieval.clients.server "$@"
