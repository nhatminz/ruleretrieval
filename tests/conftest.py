from pathlib import Path
import pytest
from sqlalchemy.orm import Session
from rule_retrieval.database.engine import engine,initialize
from rule_retrieval.database.models import LegalDocument,Provision,ProvisionVersion
from rule_retrieval.storage.files import stable_id
from rule_retrieval.retrieval.backend import BM25Backend


@pytest.fixture
def sample_case():
    text="Ngày 19/12/2016, bên vay nhận tiền theo hợp đồng và chưa hoàn trả số tiền đến hạn."
    return {"case_id":"CASE_test","input":{"facts":[{"fact_id":"F1","text":text,"source_span":{"field":"markdown","start":0,"end":len(text),"quote":text}}],
            "events":[{"event_id":"E1","event_type":"transaction","date":"2016-12-19","date_evidence":"19/12/2016","fact_id":"F1"}],
            "legal_issues":[{"issue_id":"I1","legal_question":"Bên vay có nghĩa vụ hoàn trả khoản vay khi đến hạn như thế nào?","trigger_fact_ids":["F1"]}],"date_uncertainties":[]}}


@pytest.fixture
def statute_db(tmp_path):
    url="sqlite:///"+str(tmp_path/"statutes.sqlite")
    e=engine(url)
    initialize(e)
    did=stable_id("D","test-law")
    pid=stable_id("P",did,"1")
    old=stable_id("PV",pid,"old")
    new=stable_id("PV",pid,"new")
    unknown=stable_id("PV",pid,"unknown")
    content="Điều 1. Nghĩa vụ hoàn trả khoản vay. Bên vay phải trả tiền khi đến hạn."
    with Session(e) as s:
        s.add(LegalDocument(document_id=did,title="Bộ luật dân sự 2015",document_number="91/2015/QH13",document_type="Bộ luật",issue_date="2015-11-24",source_dataset_id="test",raw_metadata={},raw_text=content))
        s.flush()
        s.add(Provision(provision_id=pid,document_id=did,article_number="1",heading="Nghĩa vụ hoàn trả khoản vay"))
        s.flush()
        for vid,start,end,status,verification in [(old,"2010-01-01","2017-01-01","VERIFIED_INTERVAL","VERIFIED"),
                (new,"2017-01-01",None,"VERIFIED_INTERVAL","VERIFIED"),(unknown,None,None,"UNKNOWN","PARSED_UNVERIFIED")]:
            s.add(ProvisionVersion(provision_version_id=vid,provision_id=pid,content=content,effective_from=start,effective_to=end,
                                  temporal_status=status,verification_status=verification,source_span={"field":"vn_text","start":0,"end":len(content)}))
            s.connection().exec_driver_sql("INSERT INTO provision_fts(provision_version_id,title,heading,content) VALUES (?,?,?,?)",(vid,"Bộ luật dân sự 2015","Nghĩa vụ hoàn trả khoản vay",content))
        s.commit()
    return {"engine":e,"url":url,"backend":BM25Backend(url),"document_id":did,"provision_id":pid,"old":old,"new":new,"unknown":unknown,"content":content}
