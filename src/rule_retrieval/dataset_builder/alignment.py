import json
import re
from rule_retrieval.config import root
from rule_retrieval.clients.openai import json_object
from rule_retrieval.dataset_builder.inputs import locate_quote, issue_terms
from rule_retrieval.ingestion.statutes import normalize


class IssueRuleAligner:
    def __init__(self,client,recorder):
        self.client,self.recorder=client,recorder

    def align(self,public:dict,citations:list[dict],issue_support:dict) -> tuple[list[dict],list[dict]]:
        applied=[c for c in citations if c["citation_role"]=="COURT_APPLIED"]
        if not applied:
            return [],[]
        result,unresolved=[],[]
        matched=set()
        terms=issue_terms
        for issue in public["input"]["legal_issues"]:
            # Rank/filter only offline alignment proposals, using the question and court context.
            # This never uses retrieval-agent predictions or adds a citation absent from the judgment.
            qt=terms(issue["legal_question"])
            candidates=sorted([c for c in applied if c.get("locator_verification_status")!="AMBIGUOUS_SOURCE_LOCATOR" and len(qt&terms(c["context"]))>=2],
                              key=lambda c:(-len(qt&terms(c["context"])),c["court_source_span"]["start"]))[:20]
            if not candidates:
                continue
            aliases={f"C{i+1}":c["citation_id"] for i,c in enumerate(candidates)}
            payload={"legal_question":issue["legal_question"],"court_issue_context":issue_support[issue["issue_id"]],
                     "citations":[{"citation_id":f"C{i+1}","citation":c["citation_text"],"context":c["context"]} for i,c in enumerate(candidates)]}
            messages=[{"role":"system","content":(root()/"prompts/issue_rule_alignment.md").read_text()},
                      {"role":"user","content":json.dumps(payload,ensure_ascii=False)}]
            response=self.client.complete(messages)
            self.recorder("issue_alignment_"+issue["issue_id"],messages,response)
            alignments=json_object(response["message"].get("content")).get("matches",[])
            by_id={aliases[a["citation_id"]]:a for a in alignments if a.get("citation_id") in aliases}
            for c in candidates:
                a=by_id.get(c["citation_id"])
                if not a:
                    continue
                group=a.get("rule_group")
                if not c.get("document_name") and not c.get("document_identifier"):
                    continue
                if "to tung" in normalize(c.get("document_name") or "") and group!="procedural":
                    # Observable rule-function conflict, rather than silently blessing small-model labels.
                    continue
                if group not in {"substantive","procedural","sentencing","mitigation","other"} or len(terms(issue["legal_question"])&terms(c["context"]))<2:
                    continue
                result.append({**c,"issue_id":issue["issue_id"],"alignment_status":"ALIGNED_SILVER_NEEDS_REVIEW",
                    "alignment_evidence":c["context"],"alignment_evidence_status":"LLM_SELECTION_WITH_SOURCE_CONTEXT_LEXICAL_CHECK","rule_group":group})
                matched.add(c["citation_id"])
        for c in applied:
            if c["citation_id"] not in matched:
                unresolved.append({"citation_id":c["citation_id"],"status":"UNRESOLVED_ALIGNMENT"})
        return result,unresolved
