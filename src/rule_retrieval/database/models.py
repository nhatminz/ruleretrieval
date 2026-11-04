from sqlalchemy import Column, String, Text, Integer, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class LegalDocument(Base):
    __tablename__ = "legal_documents"
    document_id = Column(String, primary_key=True)
    title = Column(Text, nullable=False)
    document_number = Column(String, index=True)
    document_type = Column(String)
    issuer = Column(String)
    issue_date = Column(String)
    source_url = Column(Text)
    source_dataset_id = Column(String, index=True)
    legal_domain = Column(String)
    raw_metadata = Column(JSON, nullable=False)
    raw_text = Column(Text, nullable=False)


class Provision(Base):
    __tablename__ = "provisions"
    provision_id = Column(String, primary_key=True)
    document_id = Column(String, ForeignKey("legal_documents.document_id"), nullable=False, index=True)
    article_number = Column(String, index=True)
    clause_number = Column(String)
    point_label = Column(String)
    heading = Column(Text)
    parent_provision_id = Column(String, ForeignKey("provisions.provision_id"))
    structure_path = Column(JSON)
    __table_args__ = (UniqueConstraint("document_id", "article_number", "clause_number", "point_label", "provision_id"),)


class ProvisionVersion(Base):
    __tablename__ = "provision_versions"
    provision_version_id = Column(String, primary_key=True)
    provision_id = Column(String, ForeignKey("provisions.provision_id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    effective_from = Column(String)
    effective_to = Column(String)
    temporal_status = Column(String, nullable=False, default="UNKNOWN")
    source_span = Column(JSON, nullable=False)
    verification_status = Column(String, nullable=False)


class LegalRelation(Base):
    __tablename__ = "legal_relations"
    relation_id = Column(String, primary_key=True)
    source_id = Column(String, nullable=False, index=True)
    target_id = Column(String, nullable=False, index=True)
    relation_type = Column(String, nullable=False)
    evidence = Column(JSON, nullable=False)
    verification_status = Column(String, nullable=False)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    run_id = Column(String, primary_key=True)
    source_shard = Column(String, nullable=False)
    processed_count = Column(Integer, default=0)
    errors = Column(JSON, default=list)
    timestamp = Column(String, nullable=False)


class DatabaseMeta(Base):
    __tablename__ = "database_metadata"
    key = Column(String, primary_key=True)
    value = Column(JSON, nullable=False)
