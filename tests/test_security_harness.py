import copy
import json
from dataclasses import replace
import pytest
from pydantic import ValidationError
from rule_retrieval.harness.controller import Controller,Budget,BudgetError
from rule_retrieval.harness.runner import load_public,run_case
from rule_retrieval.dataset_builder.leakage import audit
from rule_retrieval.dataset_builder.schema import PublicCase
from rule_retrieval.clients.openai import ModelConfig
from rule_retrieval.storage.files import atomic_json,stable_id,read_json


def test_reference_label_isolation(tmp_path,sample_case,statute_db):
    secret="PRIVATE_ORACLE_SENTINEL_39851"
    atomic_json(tmp_path/"private_references.json",{"references":[secret]})
    public=tmp_path/"public.json"
    atomic_json(public,sample_case)
    case=load_public(public)
    controller=Controller(statute_db["backend"],case,Budget())
    result=controller.dispatch({"action":"search_provisions","arguments":{"query":secret}})
    assert secret not in json.dumps(result)
    assert "raw_metadata" not in json.dumps(controller.dispatch({"action":"get_provision","arguments":{"provision_version_id":statute_db["old"]}}))
    for extra in [{"references":[secret]},{"markdown":secret}]:
        atomic_json(public,{**sample_case,**extra})
        with pytest.raises(ValidationError): load_public(public)


@pytest.mark.parametrize("name",["shell","http_get","read_file","web_search","sql","__class__","../private_references"])
def test_tool_whitelist(name,sample_case,statute_db):
    c=Controller(statute_db["backend"],sample_case,Budget())
    with pytest.raises(ValidationError): c.dispatch({"action":name,"arguments":{}})
    assert c.total_calls==1


def test_budget_enforcement(sample_case,statute_db):
    c=Controller(statute_db["backend"],sample_case,Budget(max_search_calls=1,max_total_tool_calls=3))
    action={"action":"search_provisions","arguments":{"query":"khoản vay","top_k":2}}
    c.dispatch(action)
    with pytest.raises(BudgetError): c.dispatch(action)
    assert c.search_calls==1
    with pytest.raises(BudgetError): c.dispatch({"action":"search_provisions","arguments":{"query":"vay","top_k":6}})
    with pytest.raises(BudgetError): c.dispatch({"action":"get_provision","arguments":{"provision_version_id":statute_db["old"]}})
    assert c.total_calls==3
    assert c.dispatch({"action":"submit_rules","arguments":{"predictions":[{"issue_id":"I1","rules":[],"abstain_reason":"Budget exhausted"}]}})["submitted"]
    zero=Controller(statute_db["backend"],sample_case,Budget(max_search_calls=0))
    with pytest.raises(BudgetError): zero.dispatch(action)
    assert zero.search_calls==0


def test_citation_ids_quotes_and_temporal_validation(sample_case,statute_db):
    c=Controller(statute_db["backend"],sample_case,Budget())
    rule={"provision_version_id":statute_db["old"],"explanation":"Relevant rule","evidence":"Bên vay phải trả tiền khi đến hạn.","event_id":"E1","temporal_status":"VERIFIED_IN_INTERVAL"}
    def submit(r):
        return c.dispatch({"action":"submit_rules","arguments":{"predictions":[{"issue_id":"I1","rules":[r]}]}})
    with pytest.raises(ValueError,match="UNGROUNDED_PROVISION_ID"): submit(rule)
    c.dispatch({"action":"get_provision","arguments":{"provision_version_id":statute_db["old"]}})
    with pytest.raises(ValueError,match="UNGROUNDED_QUOTE"): submit({**rule,"evidence":"invented evidence"})
    assert submit(rule)["predictions"][0]["rules"][0]["temporal_status"]=="VERIFIED_IN_INTERVAL"
    c.dispatch({"action":"get_provision","arguments":{"provision_version_id":statute_db["unknown"]}})
    assert submit({**rule,"provision_version_id":statute_db["unknown"]})["predictions"][0]["rules"][0]["temporal_status"]=="TEMPORAL_UNCERTAIN"


def test_leakage_and_dataset_integrity(sample_case):
    assert audit(sample_case)["status"]=="PASS"
    for text in ["Áp dụng khoản 2 Điều 51 Bộ luật hình sự.","Chấp nhận yêu cầu khởi kiện của nguyên đơn.","Buộc các bên tháo dỡ công trình.","Tuyên bị cáo có tội.","Bị cáo phạm tội trộm cắp, có phải bồi thường không?","PV_"+"a"*24]:
        bad=copy.deepcopy(sample_case)
        bad["input"]["legal_issues"][0]["legal_question"]=text
        assert audit(bad)["status"]=="LEAKAGE_RISK"
    bad=copy.deepcopy(sample_case)
    bad["input"]["legal_issues"][0]["trigger_fact_ids"]=["F999"]
    with pytest.raises(ValidationError): PublicCase.model_validate(bad)
    bad=copy.deepcopy(sample_case)
    bad["input"]["events"][0]["date"]="2016-02-31"
    with pytest.raises(ValidationError): PublicCase.model_validate(bad)


def test_incremental_checkpoint_resume_without_model(tmp_path,sample_case,statute_db):
    class ScriptedClient:
        cfg=ModelConfig()
        def __init__(self): self.calls=0
        def complete(self,messages,tools=None):
            self.calls+=1
            action={"action":"submit_rules","arguments":{"predictions":[{"issue_id":"I1","rules":[],"abstain_reason":"No verified evidence"}]}}
            return {"message":{"role":"assistant","content":json.dumps(action)},"usage":{"total_tokens":20},"latency":0.01}
    client=ScriptedClient()
    result=run_case(sample_case,statute_db["backend"],client,Budget(),tmp_path)
    assert result["status"]=="COMPLETED"
    assert (tmp_path/"checkpoints"/"CASE_test.json").exists()
    assert len((tmp_path/"trajectories"/"CASE_test.jsonl").read_text().splitlines())>=5
    again=run_case(sample_case,statute_db["backend"],client,Budget(),tmp_path)
    assert again==result and client.calls==1


def test_controller_checkpoint_restores_budget(sample_case,statute_db):
    c=Controller(statute_db["backend"],sample_case,Budget(max_search_calls=1))
    action={"action":"search_provisions","arguments":{"query":"vay"}}
    c.dispatch(action)
    restored=Controller(statute_db["backend"],sample_case,Budget(max_search_calls=1),json.loads(json.dumps(c.state())))
    with pytest.raises(BudgetError): restored.dispatch(action)


def test_interrupted_tool_is_charged_before_resume(tmp_path,sample_case,statute_db):
    class Client:
        cfg=ModelConfig()
        def __init__(self): self.calls=0
        def complete(self,messages,tools=None):
            self.calls+=1
            action={"action":"search_provisions","arguments":{"query":"khoản vay"}} if self.calls==1 else {"action":"submit_rules","arguments":{"predictions":[{"issue_id":"I1","rules":[],"abstain_reason":"Previous tool interrupted; search budget exhausted"}]}}
            return {"message":{"role":"assistant","content":json.dumps(action)},"usage":{},"latency":0}
    class InterruptedBackend:
        def search_provisions(self,**kwargs): raise KeyboardInterrupt()
        def get_provision(self,**kwargs): raise AssertionError()
        def get_provision_history(self,**kwargs): raise AssertionError()
        def get_related_provisions(self,**kwargs): raise AssertionError()
    client=Client()
    with pytest.raises(KeyboardInterrupt): run_case(sample_case,InterruptedBackend(),client,Budget(max_search_calls=1),tmp_path)
    cp=read_json(tmp_path/"checkpoints"/"CASE_test.json")
    assert cp["controller"]["search_calls"]==1
    assert cp["recovery_note"]=="Budget reserved before read-tool execution"
    result=run_case(sample_case,statute_db["backend"],client,Budget(max_search_calls=1),tmp_path)
    assert result["status"]=="COMPLETED" and result["controller"]["search_calls"]==1


def test_context_compaction_preserves_observed_data_and_requires_model_submission(tmp_path,sample_case,statute_db):
    class Client:
        cfg=ModelConfig(max_context_chars=18000)
        def complete(self,messages,tools=None):
            final=any("FINAL_ONLY_CONTEXT" in m.get("content","") for m in messages)
            action={"action":"submit_rules","arguments":{"predictions":[{"issue_id":"I1","rules":[],"abstain_reason":"Insufficient retrieved evidence"}]}} if final else {"action":"search_provisions","arguments":{"query":"khoản vay","top_k":5}}
            return {"message":{"role":"assistant","content":json.dumps(action)},"usage":{},"latency":0}
    result=run_case(sample_case,statute_db["backend"],Client(),Budget(),tmp_path)
    events=[json.loads(line) for line in (tmp_path/"trajectories"/"CASE_test.jsonl").read_text().splitlines()]
    assert result["status"]=="COMPLETED"
    assert result["controller"]["final_only"]
    assert any(e["event"]=="context_compaction" for e in events)
