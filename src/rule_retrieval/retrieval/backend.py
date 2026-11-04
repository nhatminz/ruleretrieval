from __future__ import annotations
from typing import Protocol
from pathlib import Path
import re
from sqlalchemy import create_engine, text, event
from rule_retrieval.config import database_url
from rule_retrieval.temporal.policy import parse_date, temporal_check


class RetrievalBackend(Protocol):
    def search_provisions(self, query: str, event_date: str | None = None, legal_domain: str | None = None, top_k: int = 5) -> list[dict]: ...
    def get_provision(self, provision_version_id: str) -> dict: ...
    def get_provision_history(self, provision_id: str) -> list[dict]: ...
    def get_related_provisions(self, provision_version_id: str) -> list[dict]: ...


PROJECTION = """SELECT v.provision_version_id, p.provision_id, p.document_id,
 d.title AS document_title, d.document_number, d.legal_domain, d.source_dataset_id AS source_id,
 d.source_url, p.article_number, p.clause_number, p.point_label, p.heading,
 p.parent_provision_id, v.content, v.effective_from, v.effective_to,
 v.temporal_status, v.verification_status, v.source_span
 FROM provision_versions v JOIN provisions p ON p.provision_id=v.provision_id
 JOIN legal_documents d ON d.document_id=p.document_id """


class BM25Backend:
    """A read-only, parameterized facade; never returns raw document metadata."""
    def __init__(self, url: str | None = None, temporal_mode: str = "uncertainty_aware"):
        url = url or database_url()
        if temporal_mode not in {"none", "exact", "uncertainty_aware"}:
            raise ValueError("Unknown temporal mode")
        self.temporal_mode = temporal_mode
        if url.startswith("sqlite:///"):
            db = Path(url.removeprefix("sqlite:///"))
            if not db.is_file():
                raise FileNotFoundError(db)
            url = "sqlite:///file:" + str(db.resolve()) + "?mode=ro&uri=true"
        self.engine = create_engine(url)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def readonly(connection, _):
                connection.execute("PRAGMA query_only=ON")
        elif self.engine.dialect.name == "postgresql":
            @event.listens_for(self.engine, "begin")
            def readonly_pg(connection):
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    def _query(self, sql: str, params: dict) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(text(sql), params).mappings()]

    def get_provision(self, provision_version_id: str) -> dict:
        if not re.fullmatch(r"PV_[0-9a-f]{24}", provision_version_id):
            raise ValueError("INVALID_PROVISION_ID")
        result = self._query(PROJECTION + " WHERE v.provision_version_id=:id", {"id":provision_version_id})
        if not result:
            raise KeyError("PROVISION_NOT_FOUND")
        return self._visible(result[0])

    def _visible(self, item: dict, event_date: str | None = None) -> dict:
        if isinstance(item.get("source_span"),str):
            import json
            item["source_span"]=json.loads(item["source_span"])
        item["temporal_assessment"] = temporal_check(item, event_date)
        if self.temporal_mode == "none":
            for k in ["effective_from", "effective_to", "temporal_status", "temporal_assessment"]:
                item.pop(k, None)
        return item

    def search_provisions(self, query: str, event_date: str | None = None, legal_domain: str | None = None, top_k: int = 5) -> list[dict]:
        if not 1 <= top_k <= 50 or len(query) > 2000:
            raise ValueError("Invalid search size")
        if event_date is not None and parse_date(event_date) != event_date:
            raise ValueError("event_date must be an ISO calendar date")
        # Quotes, operators and SQL are treated as words, never executed.
        stop = {"là", "và", "của", "có", "được", "không", "cho", "các", "việc", "theo", "nào", "phải", "về"}
        tokens = list(dict.fromkeys(t.lower() for t in re.findall(r"[^\W_]+",query, re.UNICODE) if t.lower() not in stop))[:36]
        if not tokens:
            return []
        params = {"n": max(100, top_k*20)}
        filters = ""
        if self.temporal_mode == "exact":
            if event_date is None:
                return []
            filters += " AND v.temporal_status='VERIFIED_INTERVAL' AND v.verification_status='VERIFIED' AND v.effective_from<=:date AND (v.effective_to IS NULL OR v.effective_to>:date)"
            params["date"] = event_date
        if legal_domain:
            filters += " AND lower(d.legal_domain) LIKE lower(:domain)"
            params["domain"] = "%" + legal_domain[:150] + "%"
        if self.engine.dialect.name == "sqlite":
            params["q"] = " OR ".join('"'+t+'"' for t in tokens)
            sql = PROJECTION.replace("SELECT v.provision_version_id,", "SELECT bm25(provision_fts,0,2,3,1) AS score, v.provision_version_id,")
            sql += " JOIN provision_fts ON provision_fts.provision_version_id=v.provision_version_id WHERE provision_fts MATCH :q" + filters + " ORDER BY score, v.provision_version_id LIMIT :n"
        else:
            params["q"] = " | ".join(tokens)
            vector = "to_tsvector('simple', coalesce(p.heading,'') || ' ' || d.title || ' ' || v.content)"
            sql = PROJECTION.replace("SELECT v.provision_version_id,", f"SELECT -ts_rank_cd({vector}, to_tsquery('simple', :q)) AS score, v.provision_version_id,")
            sql += f" WHERE {vector} @@ to_tsquery('simple', :q)" + filters + " ORDER BY score, v.provision_version_id LIMIT :n"
        result = self._query(sql, params)
        selected = []
        for item in result:
            assessment = temporal_check(item, event_date)
            if self.temporal_mode == "exact" and assessment != "VERIFIED_IN_INTERVAL":
                continue
            if self.temporal_mode == "uncertainty_aware" and assessment == "OUTSIDE_RECORDED_INTERVAL":
                # Keep it visible as a potential transition/retroactivity candidate, with an explicit flag.
                item["requires_temporal_exception_evidence"] = True
            selected.append(self._visible(item, event_date))
        return selected[:top_k]

    def get_provision_history(self, provision_id: str) -> list[dict]:
        if not re.fullmatch(r"P_[0-9a-f]{24}", provision_id):
            raise ValueError("INVALID_PROVISION_ID")
        return [self._visible(r) for r in self._query(PROJECTION+" WHERE p.provision_id=:id ORDER BY v.effective_from,v.provision_version_id",{"id":provision_id})]

    def get_related_provisions(self, provision_version_id: str) -> list[dict]:
        original = self.get_provision(provision_version_id)
        relations = self._query("SELECT source_id,target_id,relation_type,evidence,verification_status FROM legal_relations WHERE source_id=:id OR target_id=:id", {"id":original["document_id"]})
        # Return actual provision records, together with evidenced document relations.
        siblings = self._query(PROJECTION + " WHERE p.document_id=:d AND p.article_number=:a AND v.provision_version_id<>:v ORDER BY p.clause_number,p.point_label LIMIT 20",
                               {"d":original["document_id"],"a":original["article_number"],"v":provision_version_id})
        return [{**self._visible(r), "legal_relations":relations} for r in siblings]


def backend(name: str = "bm25", **kwargs) -> RetrievalBackend:
    if name != "bm25":
        raise NotImplementedError(f"{name} is optional and not installed; no silent backend substitution")
    return BM25Backend(**kwargs)
