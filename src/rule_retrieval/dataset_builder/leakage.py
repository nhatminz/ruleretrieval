from __future__ import annotations
import re
from rule_retrieval.ingestion.statutes import normalize


CITATION = re.compile(r"\b(?:điều|khoản|article|clause)\s*\d+|\bđiểm\s+[a-zđ]\b|\b\d+/\d{4}/(?:NĐ|TT|QH|UBTVQH)",re.I)
OUTCOME = re.compile(r"(?:tuyên\s*(?:bố|phạt|xử)|quyết định\s*:|chấp nhận\s+(?:một phần\s+)?(?:yêu cầu|kháng cáo|khởi kiện)|bác\s+(?:yêu cầu|kháng cáo|đơn)|buộc\s+.{0,80}(?:phải|thanh toán|bồi thường)|hội đồng xét xử\s+(?:xét thấy|nhận thấy)|có (?:đủ |cơ sở |)căn cứ\s+(?:kết luận|xác định)|phạm tội\s*[“\"])" ,re.I)
FORBIDDEN_KEYS = {"references","ground_truth","court_citation","court_source_span","citation_role","provision_version_id","resolution_status","court_support_quote","label_quality","markdown","citations_law","parent_acts"}
DISPOSITION = re.compile(r"(?:^|\n)\s*(?:\d+[.)/-]\s*)?(?:Buộc\b|Tuyên\b|Công\s+nhận\b|Đình\s+chỉ\b|Xử\s+phạt\b|Không\s+chấp\s+nhận\b|Chấp\s+nhận\b)",re.I)


def asserted_conviction(value:str,is_question:bool=False) -> bool:
    for match in re.finditer(r"(?i)\bphạm\s+tội\b",value):
        before=value[max(0,match.start()-12):match.start()]
        if not (is_question and re.search(r"(?i)\bcó\s*$",before) and value.rstrip().endswith("?")):
            return True
    return bool(re.search(r"(?i)(?:đủ\s+cơ\s+sở|có\s+cơ\s+sở|có\s+căn\s+cứ)\s+(?:để\s+)?(?:kết\s+luận|xác\s+định)",value))


def audit(public: dict, court_citations: list[dict] | None = None) -> dict:
    errors = []
    def walk(value, location=""):
        if isinstance(value,dict):
            for k,v in value.items():
                if k in FORBIDDEN_KEYS:
                    errors.append({"path":location+"/"+k,"reason":"PRIVATE_FIELD"})
                # A source span's field value is a label, never the full source.
                walk(v,location+"/"+k)
        elif isinstance(value,list):
            for i,v in enumerate(value):
                walk(v,location+f"/{i}")
        elif isinstance(value,str):
            if CITATION.search(value):
                errors.append({"path":location,"reason":"STATUTORY_CITATION"})
            if OUTCOME.search(value) or DISPOSITION.search(value) or asserted_conviction(value,location.endswith("/legal_question")):
                errors.append({"path":location,"reason":"COURT_OUTCOME_OR_FINDING"})
            if re.search(r"\b(?:PV_[0-9a-f]{24}|COURT_APPLIED|EXACT_VERSION_MATCH)\b",value):
                errors.append({"path":location,"reason":"ANSWER_IDENTIFIER"})
    walk(public)
    # Long verbatim court reasoning/basis passages cannot be reintroduced as questions.
    for citation in court_citations or []:
        quote = citation.get("court_source_span",{}).get("quote","")
        if len(quote) >= 60:
            for fact in public.get("input",{}).get("facts",[]):
                if normalize(quote) in normalize(fact["text"]):
                    errors.append({"path":"/input/facts","reason":"REFERENCE_PASSAGE_OVERLAP"})
    return {"status":"LEAKAGE_RISK" if errors else "PASS","errors":errors,"audit_version":"leakage-v1"}
