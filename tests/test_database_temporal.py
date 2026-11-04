import pytest
from sqlalchemy import text
from rule_retrieval.temporal.policy import parse_date,date_in_evidence,temporal_check
from rule_retrieval.dataset_builder.resolution import CitationResolver
from rule_retrieval.retrieval.backend import BM25Backend
from rule_retrieval.ingestion.statutes import parse_provisions


def test_hierarchy_and_exact_spans():
    text="Chương I. CHUNG\nĐiều 1. Quyền\n1. Nội dung một.\na) Một điểm.\nb) Điểm hai.\n2. Nội dung hai.\nĐiều 2. Tiếp theo\nNội dung."
    ps=parse_provisions(text,"D_test")
    assert len(ps)==6
    for p in ps:
        assert text[p["start"]:p["end"]]==p["content"]
    assert ps[2]["point_label"]=="a"
    assert ps[2]["parent_provision_id"]==ps[1]["provision_id"]
    assert [p["provision_id"] for p in ps]==[p["provision_id"] for p in parse_provisions(text,"D_test")]


def test_temporal_boundary_and_unknown():
    item={"effective_from":"2010-01-01","effective_to":"2017-01-01","temporal_status":"VERIFIED_INTERVAL","verification_status":"VERIFIED"}
    assert temporal_check(item,"2016-12-31")=="VERIFIED_IN_INTERVAL"
    assert temporal_check(item,"2017-01-01")=="OUTSIDE_RECORDED_INTERVAL"
    assert temporal_check({**item,"verification_status":"PARSED_UNVERIFIED"},"2016-12-31")=="TEMPORAL_UNCERTAIN"
    assert temporal_check({"temporal_status":"UNKNOWN"},"2016-12-31")=="TEMPORAL_UNCERTAIN"
    assert temporal_check({**item,"effective_from":"bad-date"},"2016-12-31")=="TEMPORAL_UNCERTAIN"
    assert temporal_check({**item,"effective_from":"2020-01-01"},"2016-12-31")=="TEMPORAL_UNCERTAIN"
    assert parse_date("31/02/2020") is None
    assert parse_date("2015") is None
    assert not date_in_evidence("2016-12-19","Ngày xét xử 20/12/2016")
    assert date_in_evidence("2016-12-19","ngày 19 tháng 12 năm 2016")


def test_resolution_not_article_only(statute_db):
    resolver=CitationResolver(statute_db["engine"])
    ref={"document_name":"Bộ luật dân sự","document_year":2015,"article":"1","clause":None,"point":None}
    match=resolver.resolve(ref,"2016-12-19")
    assert match["resolution_status"]=="EXACT_VERSION_MATCH"
    assert match["provision_version_id"]==statute_db["old"]
    assert resolver.resolve({**ref,"document_name":"Bộ luật hình sự"},"2016-12-19")["resolution_status"]=="NOT_IN_CORPUS"
    assert resolver.resolve({**ref,"document_year":1999},"2016-12-19")["resolution_status"]=="NOT_IN_CORPUS"
    assert resolver.resolve({**ref,"article":"2"},"2016-12-19")["resolution_status"]=="DOCUMENT_ONLY_MATCH"
    assert resolver.resolve(ref,None)["resolution_status"]=="AMBIGUOUS_MATCH"


def test_readonly_and_injection(statute_db):
    b=statute_db["backend"]
    assert b.search_provisions("khoản vay",None,None,3)
    with b.engine.connect() as c,pytest.raises(Exception):
        c.execute(text("DELETE FROM provisions"))
    with pytest.raises(ValueError):
        b.get_provision("' OR 1=1 --")
    assert b.search_provisions('"; DROP TABLE legal_documents; --') is not None
    assert len(b.get_provision_history(statute_db["provision_id"]))==3


def test_unknown_not_hard_filtered(statute_db):
    b=BM25Backend(statute_db["url"],"uncertainty_aware")
    results=b.search_provisions("khoản vay","2016-12-19",None,5)
    assert any(r["provision_version_id"]==statute_db["unknown"] for r in results)
    exact=BM25Backend(statute_db["url"],"exact").search_provisions("khoản vay","2016-12-19",None,5)
    assert [r["provision_version_id"] for r in exact]==[statute_db["old"]]
    none=BM25Backend(statute_db["url"],"none").get_provision(statute_db["old"])
    assert "effective_from" not in none
