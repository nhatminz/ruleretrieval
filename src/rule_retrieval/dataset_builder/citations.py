from __future__ import annotations
import re
import json
from rule_retrieval.config import root
from rule_retrieval.clients.openai import json_object
from rule_retrieval.dataset_builder.inputs import sections
from rule_retrieval.storage.files import stable_id


ROLES={"COURT_APPLIED","COURT_DISCUSSION","PARTY_CITED","PROSECUTOR_PROPOSED","REJECTED","AMBIGUOUS"}
LAW = re.compile(r"(?i)(?:Bộ\s*luật\s+(?:tố\s*tụng\s*dân\s*sự|tố\s*tụng\s*hình\s*sự|hình\s*sự|dân\s*sự|lao\s*động)|Luật\s+(?:hôn\s*nhân\s*và\s*gia\s*đình|thi\s*hành\s*án\s*dân\s*sự|đất\s*đai|thương\s*mại)|BLTTDS|BLTTHS|BLHS|BLDS|BLLĐ)(?:\s+năm\s+\d{4})?")
ALIASES={"blttds":"Bộ luật tố tụng dân sự","bltths":"Bộ luật tố tụng hình sự","blhs":"Bộ luật hình sự","blds":"Bộ luật dân sự","bllđ":"Bộ luật lao động"}


def article_match(raw:str,article:str):
    article=str(article).lstrip("0") or "0"
    direct=re.search(r"(?i)\bĐiều\s+0*"+re.escape(article)+r"(?![\da-zđ])",raw)
    if direct:
        return direct
    # A grouped source such as 'các điều 6, 7 và 9' explicitly supports each member.
    for group in re.finditer(r"(?i)\b(?:các\s+)?điều\s+\d+[a-zđ]?(?:\s*(?:,\s*(?:và\s+)?|và\s+)\d+[a-zđ]?)*",raw):
        numbers=[n.lstrip("0") or "0" for n in re.findall(r"\d+[a-zđ]?",group.group(),re.I)]
        if article in numbers:
            return group
    return None


def flexible_matches(raw: str, text: str):
    if not raw:
        return []
    pattern=r"\s+".join(re.escape(x) for x in raw.split())
    return list(re.finditer(pattern,text,re.I))


def occurrence_role(text: str, start: int, end: int) -> tuple[str,str,bool]:
    sec=sections(text)
    reasoning,decision=sec["reasoning_start"],sec["decision_start"]
    section="DECISION" if decision is not None and start>=decision else "REASONING" if reasoning is not None and start>=reasoning else "NARRATIVE"
    section_start=decision if section=="DECISION" else reasoning if section=="REASONING" else 0
    left=max(section_start,text.rfind("\n\n",0,start),text.rfind(";",0,start),start-220)
    right=min(len(text),end+240)
    if section=="NARRATIVE" and reasoning is not None:
        right=min(right,reasoning)
    if section=="REASONING" and decision is not None:
        right=min(right,decision)
    context=text[max(0,left):right]
    pre=text[max(section_start,start-160):start]
    if section=="NARRATIVE":
        return "PARTY_CITED",section,False
    if re.search(r"(?i)(?:không\s+áp\s+dụng|không\s+có\s+căn\s+cứ\s+áp\s+dụng|bác\s+đề\s+nghị)",pre):
        return "REJECTED",section,False
    if re.search(r"(?i)(?:viện kiểm sát|kiểm sát viên).{0,100}(?:đề nghị|cho rằng|áp dụng)",pre,re.S):
        return "PROSECUTOR_PROPOSED",section,False
    applied=bool(re.search(r"(?i)(?:căn\s+cứ|áp\s+dụng|theo\s+quy\s+định|quy\s+định\s+tại)",context))
    return "COURT_APPLIED" if applied else "COURT_DISCUSSION",section,applied


class CourtCitationLabelBuilder:
    def __init__(self,client,recorder,model_filter:bool=False):
        self.client,self.recorder=client,recorder
        self.model_filter=model_filter

    def extract(self,row:dict) -> list[dict]:
        text=row["markdown"]
        proposals=[]
        for c in row.get("citations",[]) or []:
            if c.get("dieu"):
                proposals.append({"article":c["dieu"],"clause":c.get("khoan"),"point":c.get("diem"),"raw":c.get("raw"),"name":c.get("target"),"year":None,"document_identifier":None})
        for c in row.get("citations_law",[]) or []:
            if not c.get("article"):
                continue
            name=" ".join(x for x in [c.get("law_type"),c.get("law_name")] if x)
            proposals.append({"article":c["article"],"clause":c.get("clause"),"point":c.get("point"),"raw":None,"name":name or None,"year":c.get("year"),"document_identifier":c.get("id")})
        citations=[]
        seen=set()
        for proposal in proposals:
            raw=proposal["raw"]
            matches=flexible_matches(raw,text) if raw else []
            if not matches:
                matches=list(re.finditer(r"(?i)\bĐiều\s+0*"+re.escape(str(proposal["article"]).lstrip("0") or "0")+r"(?![\da-zđ])",text))
            for m in matches:
                # Metadata may assign one clause to all articles in a grouped citation. Re-read each locator.
                am=article_match(m.group(),proposal["article"])
                if not am:
                    continue
                art_start=m.start()+am.start()
                prefix=text[max(0,art_start-90):art_start]
                prefix=re.split(r"(?i)Điều\s+\d+|[;.]",prefix)[-1]
                cm=re.search(r"(?i)khoản\s+(\d+)\s*$",prefix)
                pm=re.search(r"(?i)điểm\s+([a-zđ])\s+(?:khoản\s+\d+\s+)?$",prefix)
                clause=cm.group(1) if cm else None
                point=pm.group(1).lower() if pm else None
                grouped_points=re.search(r"(?i)điểm\s+([a-zđ](?:\s*(?:,|và)\s*[a-zđ])*)\s+(?:khoản\s+\d+\s+)?$",prefix)
                locator_status="EXACT_SOURCE_LOCATOR"
                if grouped_points:
                    labels=re.split(r"\s*(?:,|và)\s*",grouped_points.group(1).lower())
                    if len(labels)>1:
                        if proposal.get("point") in labels:
                            point=proposal["point"]
                        else:
                            locator_status="AMBIGUOUS_SOURCE_LOCATOR"
                tail=text[art_start:min(len(text),m.end()+180)]
                lm=LAW.search(tail)
                name=lm.group() if lm else proposal["name"]
                if name:
                    name=ALIASES.get(name.lower(),name)
                year_match=re.search(r"năm\s+(\d{4})",name or "",re.I)
                year=int(year_match.group(1)) if year_match else proposal["year"]
                # Exact source span is always verified against raw markdown, regardless of metadata spans.
                span_start=art_start
                if cm:
                    span_start=art_start-(len(prefix)-cm.start())
                span_end=max(m.end(),m.start()+am.end())
                if lm:
                    span_end=max(span_end,art_start+lm.end())
                key=(art_start,str(proposal["article"]).lstrip("0"),clause,point)
                if key in seen:
                    continue
                seen.add(key)
                role,section,applied=occurrence_role(text,span_start,span_end)
                context_start=max(0,span_start-180)
                context_end=min(len(text),span_end+220)
                citations.append({"citation_id":stable_id("C",row.get("doc_name"),key),"document_name":name,"document_identifier":proposal["document_identifier"],
                    "document_year":year,"article":str(proposal["article"]).lstrip("0") or "0","clause":clause,"point":point,
                    "citation_text":text[span_start:span_end],"court_source_span":{"field":"markdown","start":span_start,"end":span_end,"quote":text[span_start:span_end]},
                    "court_section":section,"citation_role":role,"confidence":0.7 if applied else 0.4,
                    "verification_status":"HEURISTIC_SILVER_NEEDS_REVIEW","context":text[context_start:context_end],"explicit_application_evidence":applied})
                citations[-1]["locator_verification_status"]=locator_status
        return citations

    def build(self,row:dict) -> list[dict]:
        citations=self.extract(row)
        eligible=[c for c in citations if c["court_section"]!="NARRATIVE"]
        if not eligible or not self.model_filter:
            return citations
        # Keep all occurrences in the private audit; only corroborated applied ones become labels.
        aliases={f"C{i+1}":c["citation_id"] for i,c in enumerate(eligible[:35])}
        messages=[{"role":"system","content":(root()/"prompts/court_citation_filter.md").read_text()},
                  {"role":"user","content":json.dumps([{"citation_id":f"C{i+1}","section":c["court_section"],"citation":c["citation_text"],"context":c["context"]} for i,c in enumerate(eligible[:35])],ensure_ascii=False)}]
        response=self.client.complete(messages)
        self.recorder("citation_roles",messages,response)
        obj=json_object(response["message"].get("content"))
        roles={aliases[alias]:role for alias,role in obj.items() if alias in aliases and isinstance(role,str) and role in ROLES}
        for c in eligible:
            role=roles.get(c["citation_id"],"AMBIGUOUS")
            if role=="COURT_APPLIED" and (not c["explicit_application_evidence"] or c["citation_role"] in {"REJECTED","PROSECUTOR_PROPOSED"}):
                role="AMBIGUOUS"
            c["citation_role"]=role
            c["verification_status"]="LLM_AND_SOURCE_CHECKED_SILVER_NEEDS_REVIEW"
        return citations
