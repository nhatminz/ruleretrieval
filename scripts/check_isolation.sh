#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"
if [[ ! -S "${RULE_RETRIEVAL_INFERENCE_UDS:-}" ]]; then echo 'Set RULE_RETRIEVAL_INFERENCE_UDS to a running inference socket.' >&2; exit 1; fi
PUBLIC="$RULE_RETRIEVAL_ROOT/data/benchmark/public_inputs"
DB="$RULE_RETRIEVAL_ROOT/data/database"
AGENT_OUTPUT="$RULE_RETRIEVAL_ROOT/outputs/agent_runs"
mkdir -p "$AGENT_OUTPUT"
bwrap --die-with-parent --unshare-net --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp \
  --tmpfs "$RULE_RETRIEVAL_ROOT" \
  --ro-bind "$RULE_RETRIEVAL_ROOT/.venv" "$RULE_RETRIEVAL_ROOT/.venv" \
  --ro-bind "$RULE_RETRIEVAL_ROOT/src" "$RULE_RETRIEVAL_ROOT/src" \
  --ro-bind "$RULE_RETRIEVAL_ROOT/configs" "$RULE_RETRIEVAL_ROOT/configs" \
  --ro-bind "$RULE_RETRIEVAL_ROOT/prompts" "$RULE_RETRIEVAL_ROOT/prompts" \
  --dir "$RULE_RETRIEVAL_ROOT/data" --dir "$RULE_RETRIEVAL_ROOT/data/benchmark" \
  --ro-bind "$PUBLIC" "$PUBLIC" --ro-bind "$DB" "$DB" \
  --dir "$RULE_RETRIEVAL_ROOT/outputs" --bind "$AGENT_OUTPUT" "$AGENT_OUTPUT" \
  --ro-bind "$RULE_RETRIEVAL_INFERENCE_UDS" "$RULE_RETRIEVAL_INFERENCE_UDS" \
  --chdir "$RULE_RETRIEVAL_ROOT" \
  -- "$PYTHON" - <<'PY'
import os,json,socket,httpx,sqlite3
from pathlib import Path
root=Path(os.environ['RULE_RETRIEVAL_ROOT'])
checks={k:not (root/p).exists() for k,p in {
    'raw_judgments_hidden':'data/raw/judgments','private_references_hidden':'data/benchmark/private_references',
    'offline_trajectories_hidden':'outputs/dataset_runs','processed_sources_hidden':'data/processed',
    'project_reports_hidden':'README.md','run_report_hidden':'RUN_REPORT.md'}.items()}
try:
    socket.create_connection(('1.1.1.1',443),timeout=1)
    checks['kernel_egress_denied']=False
except OSError as ex:
    checks['kernel_egress_denied']=ex.errno in {101,113}
with httpx.Client(transport=httpx.HTTPTransport(uds=os.environ['RULE_RETRIEVAL_INFERENCE_UDS']),trust_env=False) as c:
    checks['inference_uds_allowed']=c.get('http://localhost/health').json()['status']=='ok'
snapshots=sorted((root/'data/database/snapshots').glob('DB_*.sqlite'))
if not snapshots: raise RuntimeError('No frozen snapshot; run rule-retrieval freeze-database')
db=sqlite3.connect('file:'+str(snapshots[-1])+'?mode=ro',uri=True)
checks['database_readable']=db.execute('SELECT count(*) FROM provision_versions').fetchone()[0]>0
try:
    db.execute('CREATE TABLE isolation_must_not_exist(x)')
    checks['database_readonly']=False
except sqlite3.OperationalError as ex:
    checks['database_readonly']='readonly' in str(ex).lower()
print(json.dumps({'status':'PASS' if all(checks.values()) else 'FAIL','checks':checks},indent=2))
if not all(checks.values()): raise SystemExit(1)
PY
