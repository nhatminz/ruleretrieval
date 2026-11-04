from __future__ import annotations
from collections import Counter, defaultdict, deque
from dataclasses import asdict
from itertools import zip_longest
from pathlib import Path
import hashlib
import json
import os
from rule_retrieval.config import path, root, config
from rule_retrieval.storage.files import atomic_json, append_jsonl, read_json, run_id, stable_id, sha256, now, code_version
from rule_retrieval.ingestion.download import rows
from rule_retrieval.database.engine import snapshot
from rule_retrieval.dataset_builder.inputs import CaseInputBuilder, sections, safe_facts, issue_terms
from rule_retrieval.dataset_builder.citations import CourtCitationLabelBuilder
from rule_retrieval.dataset_builder.alignment import IssueRuleAligner
from rule_retrieval.dataset_builder.resolution import CitationResolver, relevant_date
from rule_retrieval.dataset_builder.leakage import audit
from rule_retrieval.ingestion.statutes import normalize


def metadata_year(value) -> str:
    try:
        year=int(value)
        if float(value)!=year or not 1<=year<=9999:
            return "Unknown"
        return str(year)
    except (TypeError,ValueError,OverflowError):
        return "Unknown"


def select_pool(file:Path,scan_limit:int=100000,per_stratum:int=5) -> tuple[list[dict],dict]:
    # Bounded, deterministic hash reservoir per observed domain/year, not head-only selection.
    buckets=defaultdict(list)
    counts=Counter()
    scanned=0
    for index,row in enumerate(rows(file, batch_size=32, limit=scan_limit)):
        scanned+=1
        key=f"{row.get('category') or 'Unknown'}|{metadata_year(row.get('year'))}"
        counts[key]+=1
        text=row.get("markdown") or ""
        if not 3500<=len(text)<=26000 or not row.get("citations_law") or sections(text)["reasoning_start"] is None:
            continue
        identifier=row.get("web_url") or row.get("doc_name") or str(index)
        score=hashlib.sha256(str(identifier).encode()).hexdigest()
        bucket=buckets[key]
        if len(bucket)>=per_stratum and score>=bucket[-1][0]:
            continue
        if not safe_facts(text):
            continue
        bucket.append((score,index,row))
        bucket.sort(key=lambda x:x[0])
        del bucket[per_stratum:]
    ordered=[]
    # Round-robin across domains then year strata; preserve diversity even when stopping early.
    domains=defaultdict(list)
    for key in sorted(buckets):
        domains[key.split('|')[0]].append((key,buckets[key]))
    strata=[]
    for group in zip_longest(*domains.values()):
        strata.extend(x for x in group if x)
    for group in zip_longest(*(bucket for _,bucket in strata)):
        for item in group:
            if item:
                ordered.append({"source_row":item[1],"row":item[2]})
    return ordered,{"scanned_rows":scanned,"source_strata":dict(counts),"pool_size":len(ordered),
                    "policy":"deterministic hash reservoir per domain/year, round-robin domains and years",
                    "per_stratum_reservoir":per_stratum,"length_filter":[3500,26000]}


def build_dataset(file:Path,e,client,target:int=30,max_attempts:int=150,scan_limit:int=100000,resume:str|None=None,prepare_only:bool=False) -> dict:
    rid=resume or run_id("dataset")
    out=path(f"outputs/dataset_runs/{rid}")
    private=path(f"data/benchmark/private_references/{rid}")
    public=path(f"data/benchmark/public_inputs/{rid}")
    for directory in [out,private,public]:
        directory.mkdir(parents=True,exist_ok=True)
    os.chmod(private,0o700)
    source=read_json(file.with_name(file.name+".manifest.json"))
    prompts={p.name:sha256(p) for p in (root()/"prompts").glob("*.md") if p.name!="rule_agent.md"}
    builder_policy=config("datasets").get("builder",{})
    role_mode=builder_policy.get("citation_role_mode","source_evidence")
    if role_mode not in {"source_evidence","llm_corroborated"}:
        raise ValueError("Unknown citation_role_mode")
    identity={"source":source,"db_snapshot_id":None if prepare_only else snapshot(e),"model":asdict(client.cfg),"prompt_hashes":prompts,"builder_policy":builder_policy}
    if (out/"manifest.json").exists():
        manifest=read_json(out/"manifest.json")
        if manifest["identity"]!=identity:
            prior=manifest["identity"]
            if not prepare_only and prior.get("db_snapshot_id") is None and {**prior,"db_snapshot_id":identity["db_snapshot_id"]}==identity and not (public/"index.json").exists():
                manifest["identity"]=identity
                manifest["database_frozen_at"]=now()
                atomic_json(out/"manifest.json",manifest,private=True)
            else:
                raise ValueError("Dataset resume identity changed (source/database/model/prompts)")
        versions=manifest.get("code_version_history",[{"version":manifest["code_version"],"timestamp":manifest["started_at"]}])
        current=code_version()
        if versions[-1]["version"]!=current:
            versions.append({"version":current,"timestamp":now()})
        manifest["code_version_history"]=versions
        manifest["last_invocation_config"]={"target":target,"max_attempts":max_attempts,"scan_limit":scan_limit,"prepare_only":prepare_only}
        atomic_json(out/"manifest.json",manifest,private=True)
    else:
        atomic_json(out/"manifest.json",{"run_id":rid,"identity":identity,"started_at":now(),"code_version":code_version(),
                    "target":target,"generation_config":{"target":target,"max_attempts":max_attempts,"scan_limit":scan_limit,"prepare_only":prepare_only},
                    "label_quality":"SILVER","expert_verified":False,"public_dir":str(public),"private_dir":str(private)},private=True)
    if (out/"selection.json").exists():
        selection=read_json(out/"selection.json")
        wanted=set(selection["source_rows"])
        data={i:r for i,r in enumerate(rows(file,limit=scan_limit)) if i in wanted}
        pool=[{"source_row":i,"row":data[i]} for i in selection["source_rows"]]
        selection_stats=selection["stats"]
    else:
        pool,selection_stats=select_pool(file,scan_limit)
        atomic_json(out/"selection.json",{"source_rows":[p["source_row"] for p in pool],"stats":selection_stats},private=True)
    resolver=None if prepare_only else CitationResolver(e)
    accepted=[]
    supported=[]
    attempted=0
    for item in pool:
        row=item["row"]
        cid=stable_id("CASE",source["dataset_repo"],row.get("web_url") or row.get("doc_name"))
        case_dir=out/"stages"/cid
        case_dir.mkdir(parents=True,exist_ok=True)
        if len(accepted)>=target or attempted>=max_attempts:
            break
        attempted+=1

        def recorder(stage,messages,response):
            atomic_json(case_dir/f"{stage}_model.json",{"messages":messages,"response":response},private=True)

        try:
            if (case_dir/"accepted.json").exists():
                decision=read_json(case_dir/"accepted.json")
                accepted.append(cid)
                if decision["exact_supported"]:
                    supported.append(cid)
                continue
            if (case_dir/"rejected.json").exists():
                continue
            if (case_dir/"input.json").exists():
                draft=read_json(case_dir/"input.json")
                inp,provenance=draft["public"],draft["provenance"]
            else:
                inp,provenance=CaseInputBuilder(client,recorder).build(cid,row)
                atomic_json(case_dir/"input.json",{"public":inp,"provenance":provenance},private=True)
            if (case_dir/"citations.json").exists():
                citations=read_json(case_dir/"citations.json")
            else:
                citations=CourtCitationLabelBuilder(client,recorder,model_filter=role_mode=="llm_corroborated").build(row)
                atomic_json(case_dir/"citations.json",citations,private=True)
            if (case_dir/"alignments.json").exists():
                aligned=read_json(case_dir/"alignments.json")
                references,unresolved=aligned["references"],aligned["unresolved"]
            else:
                references,unresolved=IssueRuleAligner(client,recorder).align(inp,citations,provenance["issue_support"])
                atomic_json(case_dir/"alignments.json",{"references":references,"unresolved":unresolved},private=True)
            if not prepare_only and role_mode=="source_evidence":
                # Recheck cached labels against the current source parser at finalization.
                current_citations=CourtCitationLabelBuilder(None,None).extract(row)
                current_by_id={c["citation_id"]:c for c in current_citations}
                for ref in references:
                    current=current_by_id.get(ref["citation_id"])
                    if current is None or current.get("locator_verification_status")=="AMBIGUOUS_SOURCE_LOCATOR" or current["citation_role"]!="COURT_APPLIED":
                        raise ValueError("SOURCE_LOCATOR_OR_ROLE_NEEDS_REVIEW")
                    if any(current.get(k)!=ref.get(k) for k in ["article","clause","point","document_name"]):
                        raise ValueError("CACHED_LABEL_SOURCE_CHANGED_NEEDS_REVIEW")
                    ref["locator_verification_status"]=current["locator_verification_status"]
                citations=current_citations
                aligned_ids={r["citation_id"] for r in references}
                unresolved=[{"citation_id":c["citation_id"],"status":"UNRESOLVED_ALIGNMENT"} for c in citations if c["citation_role"]=="COURT_APPLIED" and c["citation_id"] not in aligned_ids]
            # Every evaluated issue must have a court-applied aligned reference, including missing-corpus labels.
            covered_issues={r["issue_id"] for r in references}
            for issue in inp["input"]["legal_issues"]:
                qterms=issue_terms(issue["legal_question"])
                facts_text=" ".join(f["text"] for f in inp["input"]["facts"] if f["fact_id"] in issue["trigger_fact_ids"])
                support=provenance["issue_support"][issue["issue_id"]]
                support_text=" ".join(x["quote"] for x in support) if isinstance(support,list) else support["quote"]
                if len(qterms&issue_terms(facts_text))<2 or len(qterms&issue_terms(support_text))<2:
                    raise ValueError("INPUT_SEMANTIC_SUPPORT_NEEDS_REVIEW")
            if any(not (r.get("document_name") or r.get("document_identifier")) or ("to tung" in normalize(r.get("document_name") or "") and r.get("rule_group")!="procedural") for r in references):
                raise ValueError("LABEL_FUNCTION_OR_DOCUMENT_ID_NEEDS_REVIEW")
            if not references or covered_issues!={i["issue_id"] for i in inp["input"]["legal_issues"]}:
                raise ValueError("UNRESOLVED_ALIGNMENT: at least one issue lacks evidenced court-applied rules")
            leakage=audit(inp,citations)
            if leakage["status"]!="PASS":
                raise ValueError("LEAKAGE_RISK: "+json.dumps(leakage["errors"],ensure_ascii=False))
            if prepare_only:
                atomic_json(case_dir/"prepared.json",{"case_id":cid,"status":"OFFLINE_LABELS_PREPARED_NOT_EXPORTED"},private=True)
                accepted.append(cid)
                print(f"prepared {len(accepted)}/{target}: {cid}; aligned references={len(references)}",flush=True)
                continue
            for ref in references:
                ref.update(resolver.resolve(ref,relevant_date(inp,ref)))
                ref["court_citation"]=ref["citation_text"]
            atomic_json(case_dir/"alignments.json",{"references":references,"unresolved":unresolved},private=True)
            exact=all(r["resolution_status"]=="EXACT_VERSION_MATCH" for r in references)
            provision_supported=all(r["resolution_status"] in {"EXACT_VERSION_MATCH","PROVISION_MATCH_VERSION_UNKNOWN"} for r in references)
            oracle={"case_id":cid,"references":references,"all_citation_occurrences":citations,"unresolved_alignments":unresolved,
                    "label_quality":"SILVER","label_source":"COURT_APPLIED","expert_verified":False,
                    "verification_status":"NEEDS_REVIEW_SMALL_MODEL","provenance":{**provenance,"source":source,"source_row":item["source_row"],
                    "source_judgment_id":row.get("doc_name"),"domain":row.get("category"),"year":row.get("year")},
                    "exact_corpus_supported":exact,"provision_corpus_supported":provision_supported,"leakage_audit":leakage}
            atomic_json(private/f"{cid}.json",oracle,private=True)
            atomic_json(public/f"{cid}.json",inp)
            atomic_json(case_dir/"accepted.json",{"case_id":cid,"exact_supported":exact,"provision_supported":provision_supported},private=True)
            accepted.append(cid)
            if exact:
                supported.append(cid)
            print(f"accepted {len(accepted)}/{target}: {cid}; references={len(references)} exact_supported={exact}",flush=True)
        except Exception as ex:
            reason={"case_id":cid,"source_row":item["source_row"],"error":type(ex).__name__,"message":str(ex)}
            atomic_json(case_dir/"rejected.json",reason,private=True)
            append_jsonl(out/"failures.jsonl",reason)
            print(f"review/excluded {cid}: {str(ex)[:160]}",flush=True)
        finally:
            atomic_json(out/"checkpoint.json",{"attempted":attempted,"accepted_case_ids":accepted,"exact_supported_case_ids":supported})
    if prepare_only:
        stats={"run_id":rid,"prepared_cases":len(accepted),"attempted_cases":attempted,"status":"AWAITING_DATABASE_FINALIZATION","selection":selection_stats}
        atomic_json(out/"preparation_stats.json",stats,private=True)
        return stats
    all_refs=[]
    role_counts=Counter()
    resolution=Counter()
    strata=Counter()
    unresolved_count=0
    aligned_citations=0
    for cid in accepted:
        record=read_json(private/f"{cid}.json")
        all_refs.extend(record["references"])
        role_counts.update(c["citation_role"] for c in record["all_citation_occurrences"])
        resolution.update(r["resolution_status"] for r in record["references"])
        strata[f"{record['provenance']['domain']}|{record['provenance']['year']}"]+=1
        unresolved_count+=len(record["unresolved_alignments"])
        aligned_citations+=len({r["citation_id"] for r in record["references"]})
    failures=[read_json(f) for f in (out/"stages").glob("*/rejected.json")]
    failure_counts=Counter(r["message"].split(":")[0] for r in failures)
    stats={"run_id":rid,"target":target,"candidate_cases":len(accepted),"corpus_supported_cases":len(supported),
           "provision_supported_cases":sum(read_json(private/f"{cid}.json")["provision_corpus_supported"] for cid in accepted),
           "exact_case_coverage":len(supported)/len(accepted) if accepted else 0,"references_aligned":len(all_refs),
           "aligned_citation_count":aligned_citations,"extracted_citation_occurrences":sum(role_counts.values()),
           "resolution_statuses":dict(resolution),"citation_roles":dict(role_counts),"unresolved_alignments":unresolved_count,
           "attempted_cases":attempted,"exclusions":dict(failure_counts),"selected_strata":dict(strata),"selection":selection_stats,
           "leakage_audit_failure_rate":sum("LEAKAGE" in r["message"] for r in failures)/attempted if attempted else 0,
           "db_snapshot_id":identity["db_snapshot_id"],"label_quality":"SILVER","needs_expert_review":True,
           "coverage_selection_bias":"Candidates stratified by domain/year and document length; supported-only subset additionally favors laws present and temporally verified in this single statute shard. No additional shards downloaded."}
    atomic_json(out/"stats.json",stats)
    atomic_json(public/"index.json",{"dataset_run_id":rid,"case_ids":accepted,"db_snapshot_id":identity["db_snapshot_id"],
                "source_provenance":{k:source[k] for k in ["dataset_repo","revision","filename","sha256"]}})
    atomic_json(private/"index.json",{"all_candidate_cases":accepted,"corpus_supported_cases":supported,"stats":stats},private=True)
    return stats
