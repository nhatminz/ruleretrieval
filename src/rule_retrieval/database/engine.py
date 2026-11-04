from pathlib import Path
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import Session
from rule_retrieval.config import database_url
from rule_retrieval.database.models import Base, LegalDocument, Provision, ProvisionVersion, DatabaseMeta
from rule_retrieval.storage.files import stable_id
import hashlib
import json


def engine(url: str | None = None):
    url = url or database_url()
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    e = create_engine(url)
    if e.dialect.name == "sqlite":
        @event.listens_for(e, "connect")
        def sqlite_setup(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA wal_autocheckpoint=10000")
            connection.execute("PRAGMA cache_size=-131072")
            connection.execute("PRAGMA temp_store=MEMORY")
    return e


def initialize(e):
    Base.metadata.create_all(e)
    if e.dialect.name == "sqlite":
        with e.begin() as c:
            c.exec_driver_sql("PRAGMA journal_mode=WAL")
            c.exec_driver_sql("CREATE VIRTUAL TABLE IF NOT EXISTS provision_fts USING fts5(provision_version_id UNINDEXED, title, heading, content, tokenize='unicode61 remove_diacritics 2')")


def counts(e) -> dict:
    with Session(e) as s:
        return {m.__tablename__: s.scalar(select(func.count()).select_from(m))
                for m in [LegalDocument, Provision, ProvisionVersion]}


def snapshot(e) -> str:
    # Content, provenance and temporal metadata all affect reproducibility.
    with Session(e) as s:
        pairs = s.execute(select(ProvisionVersion.provision_version_id, ProvisionVersion.effective_from,
                          ProvisionVersion.effective_to, ProvisionVersion.temporal_status,
                          ProvisionVersion.verification_status).order_by(ProvisionVersion.provision_version_id)).all()
        sources = s.get(DatabaseMeta, "sources")
        h=hashlib.sha256()
        h.update(json.dumps([tuple(r) for r in pairs],ensure_ascii=False,default=str).encode())
        h.update(json.dumps(sources.value if sources else [],sort_keys=True,default=str).encode())
        # Include all agent-visible data; manual edits cannot keep a stale snapshot identity.
        from rule_retrieval.database.models import LegalRelation
        for model in [LegalDocument,Provision,ProvisionVersion,LegalRelation]:
            cols=[c for c in model.__table__.columns if c.name not in {"raw_text","raw_metadata"}]
            pk=list(model.__table__.primary_key.columns)[0]
            for row in s.execute(select(*cols).order_by(pk)).yield_per(64):
                h.update(json.dumps(tuple(row),ensure_ascii=False,sort_keys=True,default=str).encode())
        return "DB_"+h.hexdigest()
