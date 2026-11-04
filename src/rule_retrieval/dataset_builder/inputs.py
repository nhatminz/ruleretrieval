from __future__ import annotations
import json
import re
from rule_retrieval.config import root
from rule_retrieval.clients.openai import json_object
from rule_retrieval.dataset_builder.leakage import CITATION, OUTCOME, DISPOSITION, asserted_conviction, audit
from rule_retrieval.dataset_builder.schema import PublicCase
from rule_retrieval.temporal.policy import date_in_evidence,parse_date
from rule_retrieval.ingestion.statutes import normalize


def issue_terms(value: str) -> set[str]:
    stops={"nguoi","khong","nhu","nao","phai","duoc","cac","cua","cho","theo","phap","luat","toa","dinh","nhan","trong","viec","khi","hay","quyen","nghia","voi","mot","nhung","tien","tra","xem","xet","van","de"}
    proper={normalize(x) for x in re.findall(r"\b[A-ZĐÀ-Ỹ][A-Za-zĐđÀ-ỹ]*\b",value)[1:]}
    return {t for t in re.findall(r"[a-z0-9]+",normalize(value)) if len(t)>2 and t not in stops and t not in proper}


def locate_quote(text: str, quote: str, start: int = 0) -> tuple[int,int,str] | None:
    if not quote or len(quote)<15:
        return None
    pattern=r"\s+".join(re.escape(x) for x in quote.split())
    matches=list(re.finditer(pattern,text[start:]))
    if len(matches)!=1:
        return None
    match=matches[0]
    a,b=start+match.start(),start+match.end()
    return a,b,text[a:b]


REASONING = re.compile(r"(?mi)^\s*(?:#+\s*)?NHẬN\s*ĐỊNH(?:\s+CỦA)?\s+(?:T[ÒO][ÀA]\s*ÁN|CỦA\s+T[ÒO][ÀA]\s*ÁN)|(?mi:^\s*XÉT\s*THẤY\s*[:.]?)")
DECISION = re.compile(r"(?mi)^\s*(?:#+\s*)?QUYẾT\s*ĐỊNH\s*[:.]?\s*$")
NARRATIVE = re.compile(r"(?mi)^\s*(?:#+\s*)?(?:NỘI\s*DUNG\s*VỤ\s*ÁN|NỘI\s*DUNG|THEO\s*CÁC\s*TÀI\s*LIỆU).*$")


def sections(text: str) -> dict:
    r = REASONING.search(text)
    d = DECISION.search(text, r.end() if r else 0)
    n = NARRATIVE.search(text)
    return {"facts_start":n.end() if n and (not r or n.end()<r.start()) else 0,
            "facts_end":r.start() if r else (d.start() if d else len(text)),
            "reasoning_start":r.start() if r else None,"decision_start":d.start() if d else None}


def safe_facts(text: str) -> list[dict]:
    sec = sections(text)
    start,end = sec["facts_start"],sec["facts_end"]
    narrative = text[start:end]
    # Exclude quoted lower-court judgments and prosecutor submissions before reasoning.
    stop = re.search(r"(?i)(?:án sơ thẩm đã (?:xử|quyết định)|bản án sơ thẩm (?:đã|tuyên)|đại diện viện kiểm sát.{0,100}(?:đề nghị|phát biểu)|với nội dung trên.{0,30}án sơ thẩm)",narrative)
    if stop:
        end = start+stop.start()
    candidates=[]
    for m in re.finditer(r"[^\n.!?]+(?:\n(?!\s*\n)[^\n.!?]+)*[.!?]?",text[start:end]):
        raw = m.group(0)
        a = start+m.start()+len(raw)-len(raw.lstrip())
        quote = raw.strip()
        if not 60<=len(quote)<=1400 or CITATION.search(quote) or OUTCOME.search(quote) or DISPOSITION.search(quote) or asserted_conviction(quote):
            continue
        if re.search(r"(?i)(?:căn cứ|tòa án (?:quyết định|đã xử)|hội đồng xét xử|kiểm sát viên|nhân danh|thẩm phán|thư ký|## Page)",quote):
            continue
        candidates.append({"fact_id":f"S{len(candidates)+1}","text":quote,
                           "source_span":{"field":"markdown","start":a,"end":a+len(quote),"quote":quote}})
    return candidates[:50]


class CaseInputBuilder:
    def __init__(self, client, recorder):
        self.client,self.recorder = client,recorder

    def build(self, case_id: str, row: dict) -> tuple[dict,dict]:
        text=row["markdown"]
        facts=safe_facts(text)
        if not facts:
            raise ValueError("NO_LEAKAGE_SAFE_FACTS")
        sec=sections(text)
        if sec["reasoning_start"] is None:
            raise ValueError("NO_COURT_REASONING_SECTION")
        reasoning=[]
        rs=sec["reasoning_start"]
        re_end=sec["decision_start"] or len(text)
        for m in re.finditer(r"[^\n]+(?:\n(?!\s*\n|\s*\[\d+\])[^\n]+)*",text[rs:re_end]):
            raw=m.group().strip()
            if len(raw)<70 or raw.startswith("## Page"):
                continue
            a=rs+m.start()+len(m.group())-len(m.group().lstrip())
            raw=raw[:1800]
            reasoning.append({"support_id":f"R{len(reasoning)+1}","text":raw,"start":a,"end":a+len(raw)})
        if not reasoning:
            raise ValueError("NO_COURT_REASONING_EVIDENCE")
        # IDs bind output to source spans: the model cannot fabricate a quote or date.
        compact=[]
        for fact in facts:
            if sum(len(f["text"]) for f in compact)+len(fact["text"])>7000:
                break
            compact.append({"fact_id":fact["fact_id"],"text":fact["text"]})
        facts=[f for f in facts if f["fact_id"] in {x["fact_id"] for x in compact}]
        events=[]
        for fact in facts:
            for m in re.finditer(r"\b\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{4}\b",fact["text"]):
                value=parse_date(re.sub(r"\s+","",m.group()))
                if not value:
                    continue
                context=fact["text"][max(0,m.start()-80):m.end()+100]
                kind="other"
                if re.search(r"(?i)khởi kiện|nộp đơn",context): kind="filing_date"
                elif re.search(r"(?i)xét xử|tuyên án",context): kind="judgment_date"
                elif re.search(r"(?i)ký.{0,35}hợp đồng|hợp đồng.{0,35}ký",context,re.S): kind="transaction"
                events.append({"event_candidate_id":f"D{len(events)+1}","fact_id":fact["fact_id"],"event_type":kind,"date":value,"date_evidence":m.group()})
        prompt=(root()/"prompts/case_extraction.md").read_text()
        reasoning_visible=[]
        for item in reasoning:
            if sum(len(r["text"]) for r in reasoning_visible)+len(item["text"])>8000:
                break
            reasoning_visible.append(item)
        reasoning=reasoning_visible
        messages=[{"role":"system","content":prompt},{"role":"user","content":"Tình tiết:\n"+"\n".join(f["text"] for f in compact)+"\nNhận định:\n"+"\n".join(r["text"] for r in reasoning)}]
        for attempt in range(3):
            response=self.client.complete(messages)
            self.recorder(f"input_extraction_{attempt}",messages,response)
            try:
                obj=json_object(response["message"].get("content"))
                if "legal_question" in obj:
                    obj=self.source_bound_selection(obj["legal_question"],facts,reasoning,events)
                return self.validate(case_id,text,facts,obj,reasoning,events)
            except (ValueError,KeyError,TypeError) as ex:
                if attempt==2:
                    raise
                messages.extend([response["message"],{"role":"user","content":"Kết quả không hợp lệ: "+str(ex)+". Chỉ trả JSON với legal_question là một câu hỏi pháp lý cụ thể bằng tiếng Việt, dựa trên cả tình tiết và nhận định; không chứa trích dẫn luật hoặc kết luận của Tòa."}])
        raise ValueError("EXTRACTION_FAILED")

    @staticmethod
    def source_bound_selection(question,facts,reasoning,events):
        terms=issue_terms
        qt=terms(question)
        ranked_facts=sorted(facts,key=lambda f:(-len(qt&terms(f["text"])),f["source_span"]["start"]))
        ranked_reasoning=sorted(reasoning,key=lambda r:(-len(qt&terms(r["text"])),r["start"]))
        selected=[f["fact_id"] for f in ranked_facts[:3] if len(qt&terms(f["text"]))>=2]
        if not selected or not ranked_reasoning or len(qt&terms(ranked_reasoning[0]["text"]))<2:
            raise ValueError("QUESTION_NOT_SUPPORTED_BY_TRIGGER_FACTS_AND_COURT_CONTEXT")
        return {"fact_ids":selected,"legal_issues":[{"issue_id":"I1","legal_question":question,"trigger_fact_ids":selected,
                "court_support_ids":[ranked_reasoning[0]["support_id"]]}],"event_ids":[e["event_candidate_id"] for e in events if e["fact_id"] in selected],"date_uncertainties":[]}

    def validate(self,case_id,text,facts,obj,reasoning,events_available):
        selected={f["fact_id"]:f for f in facts if f["fact_id"] in obj.get("fact_ids",[])}
        if len(selected)!=len(set(obj.get("fact_ids",[]))) or not selected:
            raise ValueError("INVALID_FACT_SELECTION")
        legal_issues=[]
        supports={}
        reasoning_by_id={r["support_id"]:r for r in reasoning}
        for issue in obj.get("legal_issues",[]):
            question=issue.get("legal_question","")
            if not re.search(r"[À-ỹ]",question) or not question.rstrip().endswith("?") or len(question)<30:
                raise ValueError("WRITE_A_SPECIFIC_VIETNAMESE_LEGAL_QUESTION_ENDING_WITH_QUESTION_MARK")
            support_ids=issue.get("court_support_ids",[])
            if not support_ids or not set(support_ids)<=set(reasoning_by_id):
                raise ValueError("UNSUPPORTED_COURT_AWARE_ISSUE")
            terms=issue_terms
            qt=terms(question)
            triggers=" ".join(selected[x]["text"] for x in issue["trigger_fact_ids"] if x in selected)
            court=" ".join(reasoning_by_id[x]["text"] for x in support_ids)
            if len(qt&terms(triggers))<2 or len(qt&terms(court))<2:
                raise ValueError("QUESTION_NOT_SUPPORTED_BY_TRIGGER_FACTS_AND_COURT_CONTEXT")
            legal_issues.append({k:issue[k] for k in ["issue_id","legal_question","trigger_fact_ids"]})
            supports[issue["issue_id"]]=[{"start":reasoning_by_id[x]["start"],"end":reasoning_by_id[x]["end"],"quote":reasoning_by_id[x]["text"]} for x in support_ids]
        events=[]
        date_uncertainties=obj.get("date_uncertainties",[])
        proposed_events=[e for e in events_available if e["event_candidate_id"] in obj.get("event_ids",[])]
        if len(proposed_events)!=len(set(obj.get("event_ids",[]))):
            raise ValueError("UNKNOWN_EVENT_CANDIDATE_ID")
        for proposed in proposed_events:
            fact=selected.get(proposed.get("fact_id"))
            evidence=proposed.get("date_evidence","")
            if not fact or not evidence or evidence not in fact["text"] or not date_in_evidence(proposed.get("date",""),evidence):
                date_uncertainties.append("A proposed date failed source verification and was omitted.")
                continue
            events.append({"event_id":f"E{len(events)+1}",**{k:proposed[k] for k in ["fact_id","event_type","date","date_evidence"]}})
        if not events:
            date_uncertainties.append("No exact event date verified in selected facts; temporal selection remains uncertain.")
        public=PublicCase.model_validate({"case_id":case_id,"input":{"facts":list(selected.values()),"events":events,"legal_issues":legal_issues,"date_uncertainties":date_uncertainties}}).model_dump()
        result=audit(public)
        if result["status"]!="PASS":
            raise ValueError("LEAKAGE_RISK: "+json.dumps(result["errors"],ensure_ascii=False))
        return public,{"issue_support":supports,"leakage_audit":result,"sections":sections(text),
                       "evidence_selection":"Conservative lexical matching of the LLM court-aware question to source-only facts and reasoning; semantic review still required."}
