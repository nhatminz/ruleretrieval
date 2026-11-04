#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
if [[ $# -lt 1 ]]; then echo 'Usage: run_experiments.sh DATASET_RUN [--limit 5]'; exit 1; fi
DATASET_RUN="$1"
shift
# Same public inputs, snapshot and model configuration; independent run IDs.
for BUDGET in 0 2 4; do
  "$PYTHON" -m rule_retrieval.cli run-agent --dataset-run "$DATASET_RUN" --search-budget "$BUDGET" "$@"
done
for TEMPORAL in none exact uncertainty_aware; do
  "$PYTHON" -m rule_retrieval.cli run-agent --dataset-run "$DATASET_RUN" --search-budget 4 --temporal "$TEMPORAL" "$@"
done
"$PYTHON" -m rule_retrieval.cli run-agent --dataset-run "$DATASET_RUN" --mode direct --search-budget 4 "$@"
