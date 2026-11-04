from pathlib import Path
from collections import Counter
from rule_retrieval.storage.files import read_json,atomic_json
from rule_retrieval.ingestion.download import rows
from rule_retrieval.dataset_builder.schema import PublicCase
from rule_retrieval.dataset_builder.inputs import sections
from rule_retrieval.dataset_builder.leakage import audit
from rule_retrieval.config import path


def audit_dataset(dataset_run:str,judgments:Path) -> dict:
    public=path(f"data/benchmark/public_inputs/{dataset_run}")
    private=path(f"data/benchmark/private_references/{dataset_run}")
    index=read_json(public/"index.json")
    oracles={cid:read_json(private/f"{cid}.json") for cid in index["case_ids"]}
    wanted={o["provenance"]["source_row"]:cid for cid,o in oracles.items()}
    errors=[]
    counts=Counter()
    for idx,row in enumerate(rows(judgments)):
        if idx not in wanted:
            continue
        cid=wanted[idx]
        oracle=oracles[cid]
        record=PublicCase.model_validate(read_json(public/f"{cid}.json")).model_dump()
        if record["case_id"]!=cid or oracle["case_id"]!=cid:
            errors.append({"case_id":cid,"reason":"CASE_ID_MISMATCH"})
        text=row["markdown"]
        sec=sections(text)
        counts["cases"]+=1
        result=audit(record,oracle["all_citation_occurrences"])
        if result["status"]!="PASS":
            errors.append({"case_id":cid,"reason":"LEAKAGE_RISK","details":result})
        for fact in record["input"]["facts"]:
            span=fact["source_span"]
            counts["facts"]+=1
            if text[span["start"]:span["end"]]!=fact["text"] or span["quote"]!=fact["text"] or (sec["reasoning_start"] is not None and span["end"]>sec["reasoning_start"]):
                errors.append({"case_id":cid,"reason":"FACT_SOURCE_MISMATCH","fact_id":fact["fact_id"]})
        for reference in oracle["references"]:
            counts["references"]+=1
            span=reference["court_source_span"]
            if text[span["start"]:span["end"]]!=span["quote"] or reference["citation_role"]!="COURT_APPLIED" or reference["court_section"]=="NARRATIVE" or not(reference.get("document_name") or reference.get("document_identifier")):
                errors.append({"case_id":cid,"reason":"REFERENCE_SOURCE_OR_ROLE_MISMATCH","citation_id":reference["citation_id"]})
        counts["events"]+=len(record["input"]["events"])
    if counts["cases"]!=len(index["case_ids"]):
        errors.append({"reason":"MISSING_SOURCE_ROWS"})
    result={"dataset_run_id":dataset_run,"status":"PASS" if not errors else "FAIL",**dict(counts),"errors":errors,
            "scope":"Mechanical source, role, schema and leakage checks; not expert legal/semantic validation."}
    atomic_json(path(f"outputs/dataset_runs/{dataset_run}/integrity_audit.json"),result,private=True)
    return result
