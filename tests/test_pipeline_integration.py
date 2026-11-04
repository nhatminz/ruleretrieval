import json
import os
import subprocess
import sys
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from rule_retrieval.database.engine import engine,snapshot
from rule_retrieval.database.models import ProvisionVersion,IngestionRun
from rule_retrieval.ingestion.statutes import ingest
from rule_retrieval.storage.files import atomic_json,read_json
from rule_retrieval.evaluation.evaluator import evaluate
from rule_retrieval.dataset_builder.inputs import CaseInputBuilder
from rule_retrieval.database.snapshots import freeze_sqlite
from rule_retrieval.retrieval.backend import BM25Backend


def test_parquet_ingestion_missing_metadata_and_resume(tmp_path,monkeypatch):
    monkeypatch.setenv("RULE_RETRIEVAL_ROOT",str(tmp_path))
    parquet=tmp_path/"real_schema_fixture.parquet"
    pq.write_table(pa.Table.from_pylist([
        {"id":"1","title":"Luật thử nghiệm","vn_text":"Điều 1. Nội dung\n1. Nghĩa vụ thực hiện.\n","issue_date":"01/01/2015","effective_date":None},
        {"id":"2","title":"Thiếu toàn văn","vn_text":None,"issue_date":None,"effective_date":"invalid"},
    ]),parquet)
    e=engine("sqlite:///"+str(tmp_path/"test.sqlite"))
    stats=ingest(parquet,e)
    assert stats["processed_rows"]==2 and stats["legal_documents"]==1
    assert stats["provisions"]==2
    with Session(e) as s:
        versions=s.scalars(select(ProvisionVersion)).all()
        assert all(v.effective_from is None and v.temporal_status=="UNKNOWN" for v in versions)
        assert len(s.get(IngestionRun,stats["run_id"]).errors)==1
    second=ingest(parquet,e,resume=stats["run_id"])
    assert second["provisions"]==2
    assert second["snapshot_id"]==stats["snapshot_id"]
    assert read_json(tmp_path/"outputs"/"ingestion_runs"/stats["run_id"]/"checkpoint.json")["next_row"]==2


def test_network_guard_does_not_allow_arbitrary_egress():
    code="""
import socket
from rule_retrieval.harness.isolation import deny_egress_except_inference
deny_egress_except_inference('http://127.0.0.1:8000/v1')
try:
    socket.socket().connect(('192.0.2.1',443))
except PermissionError:
    print('DENIED')
else:
    raise AssertionError('Unexpected egress')
"""
    result=subprocess.run([sys.executable,"-c",code],capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and "DENIED" in result.stdout


def test_questions_cannot_be_placeholder_or_detached_from_facts(sample_case):
    facts=sample_case["input"]["facts"]
    reasoning=[{"support_id":"R1","text":"Bên vay đã nhận tiền theo hợp đồng và phải trả khoản vay khi đến hạn.","start":0,"end":70}]
    with pytest.raises(ValueError):
        CaseInputBuilder.source_bound_selection("A Vietnamese legal question, without its answer",facts,reasoning,[])
    with pytest.raises(ValueError):
        CaseInputBuilder.source_bound_selection("Người sử dụng lao động có phải trả tiền nghỉ phép hằng năm không?",facts,reasoning,[])


def test_evaluation_separates_provision_match_from_wrong_version(tmp_path,statute_db):
    run_dir=tmp_path/"agent"
    private_dir=tmp_path/"DATASET_test"
    snap=snapshot(statute_db["engine"])
    stats={"db_snapshot_id":snap,"candidate_cases":1,"corpus_supported_cases":1,"provision_supported_cases":1,"exact_case_coverage":1,
        "references_aligned":1,"resolution_statuses":{"EXACT_VERSION_MATCH":1},"unresolved_alignments":0,
        "citation_roles":{"COURT_APPLIED":1},"leakage_audit_failure_rate":0,"coverage_selection_bias":"test fixture"}
    atomic_json(private_dir/"index.json",{"stats":stats})
    atomic_json(private_dir/"CASE_test.json",{"references":[{"issue_id":"I1","provision_id":statute_db["provision_id"],"provision_version_id":statute_db["old"],"resolution_status":"EXACT_VERSION_MATCH","citation_role":"COURT_APPLIED"}],"exact_corpus_supported":True})
    atomic_json(run_dir/"manifest.json",{"run_id":"test","identity":{"dataset_run_id":"DATASET_test","case_ids":["CASE_test"],"db_snapshot_id":snap}})
    rule={"provision_version_id":statute_db["new"],"evidence":"Bên vay phải trả tiền khi đến hạn.","temporal_status":"VERIFIED_IN_INTERVAL"}
    result={"case_id":"CASE_test","status":"COMPLETED","predictions":[{"issue_id":"I1","rules":[rule]}],"controller":{"total_calls":1,"search_calls":1,"violations":[],"invalid_submissions":0,
        "seen":{statute_db["new"]:{"content":statute_db["content"]}},"ranked_searches":[{"ids":[statute_db["new"]]}]},"usage":{"total_tokens":100},"latency_seconds":1}
    atomic_json(run_dir/"cases"/"CASE_test.json",result)
    metrics=evaluate(run_dir,private_dir,statute_db["engine"])
    assert metrics["all_evaluated_candidates"]["micro"]["f1"]==1
    assert metrics["temporal"]["wrong_version_rate"]==1
    assert metrics["temporal"]["correct_provision_version_rate"]==0
    assert metrics["harness"]["citation_grounding_rate"]==1
    assert (run_dir/"evaluation"/"per_case_results.jsonl").exists()
    # One issue can legitimately require two historical versions at different events.
    oracle=read_json(private_dir/"CASE_test.json")
    oracle["references"].append({**oracle["references"][0],"provision_version_id":statute_db["new"]})
    atomic_json(private_dir/"CASE_test.json",oracle)
    stats["references_aligned"]=2
    stats["resolution_statuses"]={"EXACT_VERSION_MATCH":2}
    atomic_json(private_dir/"index.json",{"stats":stats})
    multiple=evaluate(run_dir,private_dir,statute_db["engine"])
    assert multiple["temporal"]["wrong_version_rate"]==0
    assert multiple["temporal"]["correct_provision_version_rate"]==1
    assert multiple["temporal"]["exact_reference_rules"]==2


def test_frozen_database_is_readable_without_wal_and_unchanged_by_source_edits(tmp_path,statute_db):
    from sqlalchemy import text
    sid=snapshot(statute_db["engine"])
    frozen=freeze_sqlite(statute_db["engine"],sid,tmp_path/"snapshots")
    assert not frozen.with_name(frozen.name+"-wal").exists()
    assert frozen.stat().st_mode&0o222==0
    b=BM25Backend("sqlite:///"+str(frozen))
    assert b.get_provision(statute_db["old"])["content"]==statute_db["content"]
    with statute_db["engine"].begin() as c:
        c.execute(text("UPDATE provision_versions SET content='changed' WHERE provision_version_id=:id"),{"id":statute_db["old"]})
    assert snapshot(statute_db["engine"])!=sid
    assert snapshot(b.engine)==sid
    assert b.get_provision(statute_db["old"])["content"]==statute_db["content"]
