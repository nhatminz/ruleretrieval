from __future__ import annotations
import re
from sqlalchemy import select
from sqlalchemy.orm import Session
from rule_retrieval.database.models import LegalDocument,Provision,ProvisionVersion
from rule_retrieval.ingestion.statutes import normalize
from rule_retrieval.temporal.policy import temporal_check


def canonical(name: str) -> str:
    name=normalize(name)
    name=re.sub(r"\bnam\s+\d{4}\b","",name)
    return " ".join(name.split())


class CitationResolver:
    def __init__(self,e):
        self.engine=e
        with Session(e) as s:
            # Keep only the small document identity index, not the full statute texts.
            self.documents=[{"id":d.document_id,"title":normalize(d.title),"number":normalize(d.document_number or ""),
                             "year":d.issue_date[:4] if d.issue_date else None,"type":normalize(d.document_type or "")}
                            for d in s.execute(select(LegalDocument.document_id,LegalDocument.title,LegalDocument.document_number,
                                                       LegalDocument.issue_date,LegalDocument.document_type))]

    def resolve(self,label:dict,event_date:str|None=None) -> dict:
        name=canonical(label.get("document_name") or "")
        identifier=normalize(label.get("document_identifier") or "")
        year=str(label["document_year"]) if label.get("document_year") else None
        docs=[]
        for d in self.documents:
            number_match=bool(identifier and d["number"]==identifier)
            # Name matches must start at the document title: a decree *mentioning* a code is not that code.
            name_match=bool(name and (d["title"]==name or d["title"].startswith(name+" ")))
            if not (number_match or name_match):
                continue
            if year and d["year"]!=year and not re.search(r"\b"+year+r"\b",d["title"]):
                continue
            # Amendment instruments are not substitute versions of the code they amend.
            if not identifier and re.search(r"\b(sua doi|bo sung|hop nhat)\b",d["title"]) and not re.search(r"\b(sua doi|bo sung|hop nhat)\b",name):
                continue
            docs.append(d["id"])
        result={"resolution_status":"NOT_IN_CORPUS","provision_version_id":None,"provision_id":None,"document_matches":docs,
                "resolution_event_date":event_date,"temporal_assessment":"TEMPORAL_UNCERTAIN"}
        if not docs:
            return result
        if len(docs)>1:
            result["resolution_status"]="AMBIGUOUS_MATCH"
            return result
        with Session(self.engine) as s:
            ps=list(s.scalars(select(Provision).where(Provision.document_id==docs[0],Provision.article_number==str(label["article"]),
                      Provision.clause_number==label.get("clause"),Provision.point_label==label.get("point"))))
            if not ps:
                result["resolution_status"]="DOCUMENT_ONLY_MATCH"
                return result
            if len(ps)>1:
                result["resolution_status"]="AMBIGUOUS_MATCH"
                return result
            vs=list(s.scalars(select(ProvisionVersion).where(ProvisionVersion.provision_id==ps[0].provision_id)))
            verified=[v for v in vs if temporal_check({"effective_from":v.effective_from,"effective_to":v.effective_to,
                    "temporal_status":v.temporal_status,"verification_status":v.verification_status},event_date)=="VERIFIED_IN_INTERVAL"]
            version=verified[0] if len(verified)==1 else vs[0] if len(vs)==1 else None
            if version is None or len(verified)>1:
                result["resolution_status"]="AMBIGUOUS_MATCH"
                return result
            exact=version in verified
            result.update({"resolution_status":"EXACT_VERSION_MATCH" if exact else "PROVISION_MATCH_VERSION_UNKNOWN",
                "provision_version_id":version.provision_version_id,"provision_id":version.provision_id,
                "temporal_assessment":"VERIFIED_IN_INTERVAL" if exact else "TEMPORAL_UNCERTAIN"})
        return result


def relevant_date(public:dict,label:dict) -> str|None:
    issue=next(i for i in public["input"]["legal_issues"] if i["issue_id"]==label["issue_id"])
    types={"filing_date","judgment_date"} if label.get("rule_group")=="procedural" else {"transaction","criminal_conduct","employment"}
    dates={e["date"] for e in public["input"]["events"] if e["event_type"] in types and e["fact_id"] in issue["trigger_fact_ids"]}
    return next(iter(dates)) if len(dates)==1 else None
