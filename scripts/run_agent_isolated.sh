#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
if ! command -v bwrap >/dev/null; then echo 'Bubblewrap missing; use run_agent.sh for the inference-origin guard only.' >&2; exit 1; fi
if [[ ! -S "${RULE_RETRIEVAL_INFERENCE_UDS:-}" ]]; then
  echo 'Start inference with --uds and set RULE_RETRIEVAL_INFERENCE_UDS to that absolute socket path.' >&2
  exit 1
fi
# Replace all project data with an allowlist: no original judgments, private references or builder traces.
PUBLIC="$RULE_RETRIEVAL_ROOT/data/benchmark/public_inputs"
DB="$RULE_RETRIEVAL_ROOT/data/database"
AGENT_OUTPUT="$RULE_RETRIEVAL_ROOT/outputs/agent_runs"
mkdir -p "$AGENT_OUTPUT"
exec bwrap --die-with-parent --unshare-net --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp \
  --tmpfs "$RULE_RETRIEVAL_ROOT" \
  --ro-bind "$RULE_RETRIEVAL_ROOT/.venv" "$RULE_RETRIEVAL_ROOT/.venv" \
  --ro-bind "$RULE_RETRIEVAL_ROOT/src" "$RULE_RETRIEVAL_ROOT/src" \
  --ro-bind "$RULE_RETRIEVAL_ROOT/configs" "$RULE_RETRIEVAL_ROOT/configs" \
  --ro-bind "$RULE_RETRIEVAL_ROOT/prompts" "$RULE_RETRIEVAL_ROOT/prompts" \
  --dir "$RULE_RETRIEVAL_ROOT/data" \
  --dir "$RULE_RETRIEVAL_ROOT/data/benchmark" \
  --ro-bind "$PUBLIC" "$PUBLIC" --ro-bind "$DB" "$DB" \
  --dir "$RULE_RETRIEVAL_ROOT/outputs" \
  --bind "$AGENT_OUTPUT" "$AGENT_OUTPUT" \
  --ro-bind "$RULE_RETRIEVAL_INFERENCE_UDS" "$RULE_RETRIEVAL_INFERENCE_UDS" \
  --setenv RULE_RETRIEVAL_OS_ISOLATED 1 \
  --chdir "$RULE_RETRIEVAL_ROOT" \
  -- "$PYTHON" -m rule_retrieval.cli run-agent "$@"
