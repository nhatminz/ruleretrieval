from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
import json
import time
import copy
from rule_retrieval.config import path,root
from rule_retrieval.storage.files import atomic_json,append_jsonl,read_json,run_id,sha256,now,code_version
from rule_retrieval.database.engine import snapshot
from rule_retrieval.dataset_builder.schema import PublicCase
from rule_retrieval.dataset_builder.leakage import audit
from rule_retrieval.harness.protocol import declarations
from rule_retrieval.harness.controller import Controller,Budget
from rule_retrieval.clients.openai import json_object,ModelError


def load_public(file:Path) -> dict:
    # Reject extra fields rather than stripping a mistakenly supplied private payload.
    case=PublicCase.model_validate(read_json(file)).model_dump()
    if audit(case)["status"]!="PASS":
        raise ValueError("Runtime input failed leakage audit")
    return case


def action_from_response(response:dict,native:bool) -> tuple[dict,str|None]:
    message=response["message"]
    if native:
        calls=message.get("tool_calls") or []
        if len(calls)!=1:
            raise ValueError("Exactly one native tool action per turn is required")
        call=calls[0]
        return {"action":call["function"]["name"],"arguments":json_object(call["function"]["arguments"])},call["id"]
    return json_object(message.get("content")),None


def run_case(case:dict,retriever,client,budget:Budget,out:Path) -> dict:
    cid=case["case_id"]
    final=out/"cases"/f"{cid}.json"
    if final.exists():
        return read_json(final)
    checkpoint=out/"checkpoints"/f"{cid}.json"
    trajectory=out/"trajectories"/f"{cid}.jsonl"
    if checkpoint.exists():
        state=read_json(checkpoint)
        messages=state["messages"]
        controller=Controller(retriever,case,budget,state["controller"])
        turn=state["next_turn"]
        usage=state["usage"]
        accumulated_latency=state["latency"]
    else:
        prompt=(root()/"prompts/rule_agent.md").read_text()
        example={"action":"search_provisions","arguments":{"query":case["input"]["legal_issues"][0]["legal_question"],"event_date":None,"legal_domain":None,"top_k":budget.max_search_results}}
        prompt=prompt.replace("{{SEARCH_ACTION_EXAMPLE}}",json.dumps(example,ensure_ascii=False))
        prompt+="\nController budgets: "+json.dumps(asdict(budget))
        messages=[{"role":"system","content":prompt},{"role":"user","content":json.dumps(case,ensure_ascii=False)}]
        controller=Controller(retriever,case,budget)
        turn,usage,accumulated_latency=0,{},0.0
        append_jsonl(trajectory,{"event":"case_start","case_id":cid,"timestamp":now(),"tool_declarations":declarations()})
    predictions=[]
    status="TURN_LIMIT"
    start=time.monotonic()
    for current in range(turn,budget.max_turns):
        atomic_json(checkpoint,{"next_turn":current,"messages":messages,"controller":controller.state(),"usage":usage,
                    "latency":accumulated_latency+time.monotonic()-start})
        if sum(len(json.dumps(m,ensure_ascii=False)) for m in messages)>client.cfg.max_context_chars:
            if controller.final_only:
                status="CONTEXT_LIMIT"
                append_jsonl(trajectory,{"event":"context_limit","turn":current,"messages":messages})
                break
            old_size=sum(len(json.dumps(m,ensure_ascii=False)) for m in messages)
            summaries=[]
            for record in list(controller.seen.values())[-20:]:
                item={k:record.get(k) for k in ["provision_version_id","document_title","article_number","clause_number","point_label","effective_from","effective_to","temporal_status","verification_status","source_id"] if k in record}
                item["content"]=record["content"][:400]
                item["content_truncated"]=len(record["content"])>400
                summaries.append(item)
            # This is deterministic copying of observed statute data, never a reconstruction of reasoning.
            messages=[messages[0],{"role":"user","content":json.dumps(case,ensure_ascii=False)},
                      {"role":"user","content":"FINAL_ONLY_CONTEXT: The context budget is nearly exhausted. Further read tools are disabled. Submit rules now using exact evidence from the previously retrieved records below, or submit an explicit abstention for each issue. These excerpts are copied from tool outputs; unknown temporal metadata remains uncertain.\n"+json.dumps({"retrieved_excerpts":summaries,"remaining_search_calls":budget.max_search_calls-controller.search_calls},ensure_ascii=False)}]
            controller.final_only=True
            append_jsonl(trajectory,{"event":"context_compaction","turn":current,"before_chars":old_size,"after_chars":sum(len(json.dumps(m,ensure_ascii=False)) for m in messages),"kept_provision_ids":[r["provision_version_id"] for r in summaries],"final_only":True})
            if sum(len(json.dumps(m,ensure_ascii=False)) for m in messages)>client.cfg.max_context_chars:
                status="CONTEXT_LIMIT"
                break
            atomic_json(checkpoint,{"next_turn":current,"messages":messages,"controller":controller.state(),"usage":usage,"latency":accumulated_latency+time.monotonic()-start})
        append_jsonl(trajectory,{"event":"model_request","turn":current,"messages":copy.deepcopy(messages),"timestamp":now()})
        try:
            response=client.complete(messages,tools=declarations())
            for k,v in response.get("usage",{}).items():
                if isinstance(v,(int,float)):
                    usage[k]=usage.get(k,0)+v
            append_jsonl(trajectory,{"event":"model_response","turn":current,**response})
        except ModelError as ex:
            status="MODEL_ERROR"
            append_jsonl(trajectory,{"event":"model_error","turn":current,"attempts":ex.attempts})
            break
        call_id=None
        try:
            action,call_id=action_from_response(response,client.cfg.native_tools)
            messages.append(response["message"])
            append_jsonl(trajectory,{"event":"tool_call","turn":current,"action":action,"tool_call_id":call_id})
            def reserve_budget():
                recovery={"error":"INTERRUPTED_TOOL_RESULT","message":"A charged read-tool attempt has no checkpointed response. No success or results can be assumed; submit or use the remaining budget."}
                result_message={"role":"tool","tool_call_id":call_id,"content":json.dumps(recovery)} if call_id else {"role":"user","content":"Controller result: "+json.dumps(recovery)}
                atomic_json(checkpoint,{"next_turn":current+1,"messages":[*messages,result_message],"controller":controller.state(),"usage":usage,
                            "latency":accumulated_latency+time.monotonic()-start,"recovery_note":"Budget reserved before read-tool execution"},durable=True)
                append_jsonl(trajectory,{"event":"tool_budget_reserved","turn":current,"total_calls":controller.total_calls,"search_calls":controller.search_calls})
            controller.before_execute=reserve_budget
            result=controller.dispatch(action)
            append_jsonl(trajectory,{"event":"tool_response","turn":current,"action_name":action.get("action"),"response":result})
            if result.get("submitted"):
                predictions=result["predictions"]
                status="COMPLETED"
                break
        except Exception as ex:
            result={"error":type(ex).__name__,"message":str(ex),"remaining_search_calls":max(0,budget.max_search_calls-controller.search_calls),
                    "remaining_tool_calls":max(0,budget.max_total_tool_calls-controller.total_calls)}
            result["allowed_provision_version_ids"]=list(controller.seen)
            result["recovery_instruction"]="Use only IDs returned by this case's tools, with an exact visible quote. If you cannot ground a rule, submit an explicit abstention; do not repeat invalid IDs."
            result["valid_abstention_action"]={"action":"submit_rules","arguments":{"predictions":[{"issue_id":i["issue_id"],"rules":[],"abstain_reason":"No rule grounded in the internal retrieved evidence"} for i in case["input"]["legal_issues"]]}}
            append_jsonl(trajectory,{"event":"action_error","turn":current,"response":result})
            if not messages or messages[-1].get("role")!="assistant":
                messages.append(response["message"])
        if call_id:
            messages.append({"role":"tool","tool_call_id":call_id,"content":json.dumps(result,ensure_ascii=False)})
        else:
            messages.append({"role":"user","content":"Controller result: "+json.dumps(result,ensure_ascii=False)})
        atomic_json(checkpoint,{"next_turn":current+1,"messages":messages,"controller":controller.state(),"usage":usage,
                    "latency":accumulated_latency+time.monotonic()-start})
    elapsed=accumulated_latency+time.monotonic()-start
    result={"case_id":cid,"status":status,"predictions":predictions,"controller":controller.state(),"usage":usage,"latency_seconds":elapsed}
    atomic_json(final,result)
    append_jsonl(trajectory,{"event":"case_end","status":status,"predictions":predictions,"latency_seconds":elapsed,"usage":usage,"controller_counts":{"tool_calls":controller.total_calls,"search_calls":controller.search_calls}})
    if status!="COMPLETED":
        append_jsonl(out/"failures.jsonl",{"case_id":cid,"status":status})
    return result


def direct_case(case,retriever,budget:Budget,out:Path) -> dict:
    file=out/"cases"/f"{case['case_id']}.json"
    if file.exists():
        return read_json(file)
    start=time.monotonic()
    controller=Controller(retriever,case,budget)
    predictions=[]
    trajectory=out/"trajectories"/f"{case['case_id']}.jsonl"
    for issue in case["input"]["legal_issues"]:
        facts=" ".join(f["text"] for f in case["input"]["facts"] if f["fact_id"] in issue["trigger_fact_ids"])
        args={"query":(issue["legal_question"]+" "+facts)[:1200],"event_date":None,"legal_domain":None,"top_k":budget.max_search_results}
        action={"action":"search_provisions","arguments":args}
        try:
            result=controller.dispatch(action)
            append_jsonl(trajectory,{"event":"tool_call","action":action})
            append_jsonl(trajectory,{"event":"tool_response","action_name":"search_provisions","response":result,"issue_id":issue["issue_id"]})
            rules=[{"provision_version_id":r["provision_version_id"],"explanation":"Direct BM25 rank","evidence":r["content"][:160],"event_id":None,"temporal_status":"TEMPORAL_UNCERTAIN"} for r in result["results"]]
            predictions.append({"issue_id":issue["issue_id"],"rules":rules,"abstain_reason":None if rules else "No internal search results"})
        except Exception as ex:
            append_jsonl(trajectory,{"event":"action_error","message":str(ex),"action":action})
            predictions.append({"issue_id":issue["issue_id"],"rules":[],"abstain_reason":str(ex)})
    final=controller.dispatch({"action":"submit_rules","arguments":{"predictions":predictions}})
    result={"case_id":case["case_id"],"status":"COMPLETED","predictions":final["predictions"],"controller":controller.state(),"usage":{},"latency_seconds":time.monotonic()-start}
    atomic_json(file,result)
    return result


def run_agent(public_dir:Path,retriever,client,budget:Budget,limit:int|None=5,resume:str|None=None,mode:str="agent",case_ids:list[str]|None=None) -> dict:
    rid=resume or run_id("agent" if mode=="agent" else "bm25")
    out=path(f"outputs/agent_runs/{rid}")
    out.mkdir(parents=True,exist_ok=True)
    index=read_json(public_dir/"index.json")
    chosen=case_ids if case_ids is not None else index["case_ids"]
    if len(set(chosen))!=len(chosen) or not set(chosen)<=set(index["case_ids"]):
        raise ValueError("Explicit case IDs must be unique members of the public index")
    ids=chosen[:limit] if limit is not None else chosen
    from rule_retrieval.database.engine import engine
    current_snapshot=snapshot(retriever.engine)
    if current_snapshot!=index["db_snapshot_id"]:
        raise ValueError("Input dataset and runtime database snapshots differ")
    identity={"db_snapshot_id":current_snapshot,"dataset_run_id":index["dataset_run_id"],"case_ids":ids,
              "public_input_hashes":{cid:sha256(public_dir/f"{cid}.json") for cid in ids},"budget":asdict(budget),
              "model":asdict(client.cfg),"prompt_hash":sha256(root()/"prompts/rule_agent.md"),"mode":mode}
    if (out/"manifest.json").exists():
        if read_json(out/"manifest.json")["identity"]!=identity:
            raise ValueError("Resume identity changed (cases, public inputs, DB, model, prompt or budgets)")
    else:
        from sqlalchemy.orm import Session
        from rule_retrieval.database.models import DatabaseMeta
        with Session(retriever.engine) as session:
            source_meta=session.get(DatabaseMeta,"sources")
            database_sources=source_meta.value if source_meta else []
        atomic_json(out/"manifest.json",{"run_id":rid,"identity":identity,"code_version":code_version(),"started_at":now(),
                    "case_selection_mode":"explicit_ids" if case_ids is not None else "index_prefix",
                    "dataset_source_provenance":index.get("source_provenance",{}),"database_sources":database_sources,
                    "prompt_version":"rule-agent-v2","tool_declarations":declarations(),"network_isolation":"os-namespace" if __import__('os').environ.get('RULE_RETRIEVAL_OS_ISOLATED') else "inference-origin audit guard; no internet tools",
                    "hidden_reasoning":"Only server-provided response fields are logged; none reconstructed."})
    results=[]
    for cid in ids:
        case=load_public(public_dir/f"{cid}.json")
        if case["case_id"]!=cid:
            raise ValueError("Public case_id does not match its index entry and filename")
        result=run_case(case,retriever,client,budget,out) if mode=="agent" else direct_case(case,retriever,budget,out)
        results.append(result)
        # Per-case atomic files are canonical; rebuild aggregate to prevent resume duplicates.
        aggregate=out/"predictions.jsonl"
        tmp=aggregate.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in results))
        tmp.replace(aggregate)
        atomic_json(out/"checkpoint.json",{"completed_case_ids":[r["case_id"] for r in results],"case_count":len(results)})
        print(f"{cid}: {result['status']}; searches={result['controller']['search_calls']}",flush=True)
    stats={"run_id":rid,"case_count":len(results),"completed":sum(r["status"]=="COMPLETED" for r in results),
           "failed":sum(r["status"]!="COMPLETED" for r in results),"search_calls":sum(r["controller"]["search_calls"] for r in results),
           "tool_calls":sum(r["controller"]["total_calls"] for r in results)}
    atomic_json(out/"stats.json",stats)
    return stats
