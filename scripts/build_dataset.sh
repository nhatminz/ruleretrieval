#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
exec "$PYTHON" -m rule_retrieval.cli build-dataset "$@"
