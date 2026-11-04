import json
import pytest
from rule_retrieval.dataset_builder.inputs import safe_facts,sections
from rule_retrieval.dataset_builder.citations import occurrence_role,CourtCitationLabelBuilder
from rule_retrieval.evaluation.metrics import scores,aggregate
from rule_retrieval.storage.files import atomic_json,read_json


def test_safe_facts_exclude_decision_and_reasoning():
    text="NỘI DUNG VỤ ÁN\nNgày 19/12/2016, ông A nhận khoản vay theo hợp đồng và chưa hoàn trả khi đến hạn.\n\nNHẬN ĐỊNH CỦA TOÀ ÁN\nTheo quy định tại Điều 466 Bộ luật dân sự, yêu cầu có căn cứ.\nQUYẾT ĐỊNH\nBuộc ông A phải thanh toán tiền."
    fs=safe_facts(text)
    assert fs
    assert all("466" not in f["text"] and "Buộc" not in f["text"] for f in fs)
    assert sections(text)["reasoning_start"] is not None
    assert all(text[f["source_span"]["start"]:f["source_span"]["end"]]==f["text"] for f in fs)


def test_citation_roles_do_not_count_party_or_rejected():
    text="NỘI DUNG VỤ ÁN\nNguyên đơn viện dẫn Điều 1 Luật A.\nNHẬN ĐỊNH CỦA TÒA ÁN\nKhông áp dụng Điều 2 Luật B.\nQUYẾT ĐỊNH\nCăn cứ Điều 3 Luật C."
    for article,role in [("1","PARTY_CITED"),("2","REJECTED"),("3","COURT_APPLIED")]:
        start=text.index("Điều "+article)
        assert occurrence_role(text,start,start+6)[0]==role


def test_grouped_citations_and_point_labels_are_source_verified():
    text="NHẬN ĐỊNH CỦA TÒA ÁN\nLý do giải quyết vụ án.\nQUYẾT ĐỊNH\nCăn cứ các điều 6, 7 và 9 Luật Thi hành án dân sự; áp dụng điểm b, s khoản 1 Điều 51 Bộ luật hình sự."
    cites=[{"dieu":n,"raw":"các điều 6, 7 và 9 Luật Thi hành án dân sự","target":"Luật Thi hành án dân sự"} for n in ["6","7","9"]]
    cites.extend({"dieu":"51","khoan":"1","diem":p,"raw":"điểm b, s khoản 1 Điều 51 Bộ luật hình sự","target":"Bộ luật hình sự"} for p in ["b","s"])
    result=CourtCitationLabelBuilder(None,None).extract({"doc_name":"test","markdown":text,"citations":cites})
    assert {c["article"] for c in result}=={"6","7","9","51"}
    assert {c["point"] for c in result if c["article"]=="51"}=={"b","s"}
    assert all(text[c['court_source_span']['start']:c['court_source_span']['end']]==c['court_source_span']['quote'] for c in result)


def test_metrics_duplicate_rank_empty_support():
    m=scores(["a","a","z","b"],{"a","b","c"},[1,3])
    assert m["tp"]==2 and m["fp"]==1 and m["fn"]==1
    assert m["precision"]==pytest.approx(2/3)
    assert m["recall@1"]==pytest.approx(1/3)
    assert m["recall@3"]==pytest.approx(2/3)
    assert m["mrr@3"]==1
    assert aggregate([])["micro"] is None
    assert aggregate([m])["micro"]["f1"]==pytest.approx(2/3)


def test_atomic_private_storage(tmp_path):
    p=tmp_path/"private"/"reference.json"
    atomic_json(p,{"references":[1]},private=True)
    atomic_json(p,{"references":[2]},private=True)
    assert read_json(p)=={"references":[2]}
    assert p.stat().st_mode&0o777==0o600
    assert not list(p.parent.glob('.tmp-*'))
