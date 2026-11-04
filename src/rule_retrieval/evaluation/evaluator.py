"""Private labels are opened only here, after prediction runs have finished."""
from __future__ import annotations
from pathlib import Path
from collections import Counter
from sqlalchemy import select
from sqlalchemy.orm import Session
from rule_retrieval.config import config
from rule_retrieval.database.models import ProvisionVersion
from rule_retrieval.database.engine import snapshot
from rule_retrieval.storage.files import read_json,atomic_json,append_jsonl,stable_id,code_version
from rule_retrieval.evaluation.metrics import scores,aggregate,ratio


def label_key(ref:dict) -> str:
    if ref.get("provision_id"):
        return ref["provision_id"]
    return stable_id("UNAVAILABLE",ref.get("document_identifier"),ref.get("document_name"),ref.get("document_year"),ref.get("article"),ref.get("clause"),ref.get("point"))


def evaluate(run_dir:Path,private_dir:Path,e) -> dict:
    manifest=read_json(run_dir/"manifest.json")
    index=read_json(private_dir/"index.json")
    identity=manifest["identity"]
    if identity["dataset_run_id"]!=private_dir.name:
        raise ValueError("Private reference dataset does not match prediction run")
    if identity["db_snapshot_id"]!=snapshot(e) or identity["db_snapshot_id"]!=index["stats"]["db_snapshot_id"]:
        raise ValueError("Evaluation database snapshot changed")
    ks=config("evaluation")["ks"]
    with Session(e) as s:
        mapping=dict(s.execute(select(ProvisionVersion.provision_version_id,ProvisionVersion.provision_id)).all())
    all_issues=[]
    supported_issues=[]
    candidate_rank_issues=[]
    case_results=[]
    temporal=Counter()
    harness=Counter()
    missing=Counter()
    for cid in identity["case_ids"]:
        result_path=run_dir/"cases"/f"{cid}.json"
        if not result_path.exists():
            raise ValueError(f"Agent has not completed/failed case {cid}; evaluation must run afterward")
        pred=read_json(result_path)
        oracle=read_json(private_dir/f"{cid}.json")
        refs=oracle["references"]
        if any(r["citation_role"]!="COURT_APPLIED" for r in refs):
            raise ValueError("Non-applied citation in primary references")
        missing.update(r["resolution_status"] for r in refs)
        by_issue={}
        for r in refs:
            by_issue.setdefault(r["issue_id"],[]).append(r)
        pp={p["issue_id"]:p for p in pred["predictions"]}
        per_case=[]
        state=pred["controller"]
        global_rank=list(dict.fromkeys(x for search in state.get("ranked_searches",[]) for x in search["ids"]))
        for iid,labels in by_issue.items():
            expected={label_key(r) for r in labels}
            rules=pp.get(iid,{}).get("rules",[])
            ids=[r["provision_version_id"] for r in rules]
            keys=[mapping.get(x,"INVALID:"+x) for x in ids]
            metric=scores(keys,expected,ks)
            all_issues.append(metric)
            if oracle["exact_corpus_supported"]:
                supported_issues.append(metric)
            rank=scores([mapping.get(x,"INVALID:"+x) for x in global_rank],expected,ks)
            candidate_rank_issues.append(rank)
            per_case.append({"issue_id":iid,"metrics":metric,"reference_count":len(expected),"predicted_count":len(ids)})
            exact={}
            for label in labels:
                if label["resolution_status"]=="EXACT_VERSION_MATCH":
                    exact.setdefault(label["provision_id"],set()).add(label["provision_version_id"])
            for rule in rules:
                vid=rule["provision_version_id"]
                harness["predicted_rules"]+=1
                if vid not in mapping:
                    harness["invalid_ids"]+=1
                seen=state.get("seen",{}).get(vid)
                if seen and rule.get("evidence") and rule["evidence"] in seen.get("content",""):
                    harness["grounded_rules"]+=1
                if rule.get("temporal_status")=="TEMPORAL_UNCERTAIN":
                    temporal["uncertain_predictions"]+=1
                pid=mapping.get(vid)
                if pid in exact:
                    temporal["version_comparable_predictions"]+=1
                    temporal["correct_version" if vid in exact[pid] else "wrong_version"]+=1
            temporal["exact_reference_rules"]+=sum(len(versions) for versions in exact.values())
        harness["cases"]+=1
        harness["completed_cases"]+=pred["status"]=="COMPLETED"
        harness["tool_calls"]+=state["total_calls"]
        harness["search_calls"]+=state["search_calls"]
        harness["budget_compliant_cases"]+=not state["violations"]
        limits=identity.get("budget",{})
        harness["budget_enforced_cases"]+=(state["total_calls"]<=limits.get("max_total_tool_calls",10) and state["search_calls"]<=limits.get("max_search_calls",4))
        harness["budget_violations"]+=len(state["violations"])
        harness["invalid_submission_attempts"]+=state.get("invalid_submissions",0)
        harness["latency_seconds"]+=pred["latency_seconds"]
        harness["total_tokens"]+=pred.get("usage",{}).get("total_tokens",0)
        case_results.append({"case_id":cid,"status":pred["status"],"exact_corpus_supported":oracle["exact_corpus_supported"],"issues":per_case})
    stats=index["stats"]
    resolved=sum(stats["resolution_statuses"].get(k,0) for k in ["EXACT_VERSION_MATCH","PROVISION_MATCH_VERSION_UNKNOWN"])
    metrics={"run_id":manifest["run_id"],"evaluation_code_version":code_version(),"label_quality":"COURT_APPLIED_SILVER_NOT_EXPERT_GOLD",
        "matching_unit":"Provision IDs for rule retrieval; historical version accuracy is scored separately ONLY against EXACT_VERSION_MATCH labels. Unavailable/ambiguous labels remain in all-candidate denominators.",
        "exact_supported_only":aggregate(supported_issues),"all_evaluated_candidates":aggregate(all_issues),
        "evaluated_cases":len(case_results),"dataset_total_candidates":stats["candidate_cases"],
        "missing_corpus_breakdown_evaluated_references":dict(missing),
        "retrieval_candidate_ranking":{"attribution":"case-global search candidates merged by first appearance; not issue-specific search relevance", "metrics":aggregate(candidate_rank_issues)},
        "temporal":{**dict(temporal),"correct_provision_version_rate":ratio(temporal["correct_version"],temporal["version_comparable_predictions"]),
                    "wrong_version_rate":ratio(temporal["wrong_version"],temporal["version_comparable_predictions"]),
                    "temporal_uncertainty_rate":ratio(temporal["uncertain_predictions"],harness["predicted_rules"]),
                    "denominator_note":"Version rates are undefined (null) if no predicted provision has an exact-version reference."},
        "harness":{**dict(harness),"budget_compliance_rate":ratio(harness["budget_compliant_cases"],harness["cases"]),
                   "budget_enforcement_rate":ratio(harness["budget_enforced_cases"],harness["cases"]),
                   "budget_metric_note":"Compliance counts cases with no forbidden attempts; enforcement checks that charged tool/search counts never exceed controller limits. Blocked attempts remain violations.",
                   "citation_grounding_rate":ratio(harness["grounded_rules"],harness["predicted_rules"]),"invalid_provision_id_rate":ratio(harness["invalid_ids"],harness["predicted_rules"]),
                   "latency_per_case":ratio(harness["latency_seconds"],harness["cases"]),"tokens_per_case":ratio(harness["total_tokens"],harness["cases"])},
        "data_quality":{"candidate_cases":stats["candidate_cases"],"corpus_supported_cases":stats["corpus_supported_cases"],"provision_supported_cases":stats["provision_supported_cases"],
                        "exact_case_coverage":stats["exact_case_coverage"],"citation_resolution_rate":ratio(resolved,stats["references_aligned"]),
                        "alignment_coverage":ratio(stats.get("aligned_citation_count",stats["references_aligned"]),stats.get("aligned_citation_count",stats["references_aligned"])+stats["unresolved_alignments"]),
                        "unresolved_alignments":stats["unresolved_alignments"],"citation_roles":stats["citation_roles"],
                        "resolution_statuses":stats["resolution_statuses"],"leakage_audit_failure_rate":stats["leakage_audit_failure_rate"],"selection_bias":stats["coverage_selection_bias"]}}
    destination=run_dir/"evaluation"
    destination.mkdir(exist_ok=True)
    for case in case_results:
        atomic_json(destination/"cases"/f"{case['case_id']}.json",case,private=True)
    tmp=destination/"per_case_results.tmp"
    import json
    tmp.write_text("".join(json.dumps(c,ensure_ascii=False)+"\n" for c in case_results))
    tmp.replace(destination/"per_case_results.jsonl")
    atomic_json(destination/"metrics.json",metrics,private=True)
    return metrics
