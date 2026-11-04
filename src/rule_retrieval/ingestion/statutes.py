from __future__ import annotations
from pathlib import Path
import re
import unicodedata
from sqlalchemy import select, insert
from sqlalchemy.orm import Session
from rule_retrieval.database.models import LegalDocument, Provision, ProvisionVersion, IngestionRun, LegalRelation, DatabaseMeta
from rule_retrieval.database.engine import initialize, counts, snapshot
from rule_retrieval.ingestion.download import rows
from rule_retrieval.temporal.policy import parse_date
from rule_retrieval.storage.files import stable_id, run_id, now, atomic_json, append_jsonl, read_json, sha256, code_version
from rule_retrieval.config import path


def normalize(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    return " ".join("".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)).lower().split())


ARTICLE = re.compile(r"(?mi)^[ \t]*(?:\*\*|#+\s*)?Điều\s+(\d+[a-zđ]?)\s*[.:]\s*([^\n]*)")
CLAUSE = re.compile(r"(?m)^[ \t]*(\d{1,3})[.)]\s+([^\n]*)")
POINT = re.compile(r"(?m)^[ \t]*([a-zđ])[)]\s+([^\n]*)")
STRUCTURE = re.compile(r"(?mi)^[ \t]*(Chương|Mục|Tiểu mục)\s+([IVXLCDM\d]+)[.\s]*([^\n]*)")


def parse_provisions(text: str, document_id: str) -> list[dict]:
    articles = list(ARTICLE.finditer(text))
    result = []
    seen = set()

    def add(article, clause, point, start, end, heading, parent, structure):
        # Repeated articles (annexes, quoted amendments) stay distinguishable and are unverified.
        key = (article, clause, point)
        occurrence = 1 + sum(1 for p in result if (p['article_number'], p['clause_number'], p['point_label']) == key)
        pid = stable_id("P", document_id, article, clause, point, occurrence)
        result.append({"provision_id": pid, "document_id": document_id, "article_number": article,
                       "clause_number": clause, "point_label": point, "heading": heading,
                       "parent_provision_id": parent, "structure_path": structure,
                       "content": text[start:end], "start": start, "end": end,
                       "duplicate_locator": occurrence > 1})
        seen.add(key)
        return pid

    for i, m in enumerate(articles):
        end = articles[i+1].start() if i+1 < len(articles) else len(text)
        # Do not attach annexes/signatures or the next chapter to the preceding article.
        tail = re.search(r"(?mi)^\s*(?:PHỤ LỤC|Nơi nhận:|Chương\s+[IVXLCDM\d]+)", text[m.end():end])
        if tail:
            end = m.end() + tail.start()
        structure = [{"level": s.group(1), "number": s.group(2), "heading": s.group(3)}
                     for s in STRUCTURE.finditer(text[:m.start()])][-3:]
        a = m.group(1).lower()
        aid = add(a, None, None, m.start(), end, m.group(2), None, structure)
        clauses = list(CLAUSE.finditer(text, m.end(), end))
        for j, c in enumerate(clauses):
            ce = clauses[j+1].start() if j+1 < len(clauses) else end
            cid = add(a, c.group(1), None, c.start(), ce, c.group(2), aid, structure)
            points = list(POINT.finditer(text, c.end(), ce))
            for k, p in enumerate(points):
                pe = points[k+1].start() if k+1 < len(points) else ce
                add(a, c.group(1), p.group(1), p.start(), pe, p.group(2), cid, structure)
    return result


def document_number(row: dict) -> str | None:
    if row.get("doc_number"):
        return str(row["doc_number"]).strip()
    m = re.search(r"(?mi)^\s*Số\s*:\s*([^\n]+)", row.get("vn_text") or "")
    return m.group(1).strip() if m else None


def ingest(file: Path, e, limit: int | None = None, resume: str | None = None) -> dict:
    initialize(e)
    rid = resume or run_id("ingest")
    out = path(f"outputs/ingestion_runs/{rid}")
    out.mkdir(parents=True, exist_ok=True)
    manifest = read_json(file.with_name(file.name+".manifest.json")) if file.with_name(file.name+".manifest.json").exists() else {"sha256": sha256(file)}
    if (out/"manifest.json").exists():
        if read_json(out/"manifest.json")["source"] != manifest:
            raise ValueError("Resume source changed")
    else:
        atomic_json(out/"manifest.json", {"run_id": rid, "source": manifest, "code_version": code_version(), "started_at": now()})
    start_at = read_json(out/"checkpoint.json").get("next_row", 0) if (out/"checkpoint.json").exists() else 0
    processed = start_at
    with Session(e) as s:
        if not s.get(IngestionRun, rid):
            s.add(IngestionRun(run_id=rid, source_shard=str(file), processed_count=processed, errors=[], timestamp=now()))
            s.commit()
    for idx, row in enumerate(rows(file, limit=limit)):
        if idx < start_at:
            continue
        try:
            text = row.get("vn_text") or ""
            if not text.strip():
                raise ValueError("MISSING_VIETNAMESE_TEXT")
            did = stable_id("D", manifest.get("dataset_repo", "statutes"), str(row["id"]))
            provisions = parse_provisions(text, did)
            with Session(e) as s:
                existing = s.get(LegalDocument, did)
                if existing and existing.raw_text != text:
                    raise ValueError("Source document changed; use a new database snapshot")
                if not existing:
                    metadata = {k:v for k,v in row.items() if k not in {"vn_text", "en_text"}}
                    metadata["provenance"] = manifest
                    s.add(LegalDocument(document_id=did, title=row.get("title") or str(row["id"]),
                        document_number=document_number(row), document_type=row.get("doc_type"), issuer=row.get("issuing_body") or row.get("issuer"),
                        issue_date=parse_date(row.get("issue_date")), source_url=row.get("url"), source_dataset_id=str(row["id"]),
                        legal_domain=row.get("legal_area"), raw_metadata=metadata, raw_text=text))
                    s.flush()
                    provision_rows, version_rows, fts_rows = [], [], []
                    for p in provisions:
                        data = {k:v for k,v in p.items() if k not in {"content", "start", "end", "duplicate_locator"}}
                        provision_rows.append(data)
                        effective = parse_date(row.get("effective_date"))
                        vid = stable_id("PV", p["provision_id"], p["content"], effective, manifest.get("sha256"))
                        version_rows.append(dict(provision_version_id=vid, provision_id=p["provision_id"], content=p["content"],
                            effective_from=effective, effective_to=None, temporal_status="DOCUMENT_DATE_ONLY" if effective else "UNKNOWN",
                            source_span={"field": "vn_text", "start": p["start"], "end": p["end"], "source_id": str(row["id"]),
                                         "duplicate_locator": p["duplicate_locator"]}, verification_status="PARSED_UNVERIFIED"))
                        fts_rows.append((vid, row.get("title") or "", p["heading"], p["content"]))
                    if provision_rows:
                        s.connection().execute(insert(Provision), provision_rows)
                        s.connection().execute(insert(ProvisionVersion), version_rows)
                        if e.dialect.name == "sqlite":
                            s.connection().exec_driver_sql("INSERT INTO provision_fts(provision_version_id,title,heading,content) VALUES (?,?,?,?)", fts_rows)
                run = s.get(IngestionRun, rid)
                run.processed_count = idx+1
                s.commit()
            if not provisions:
                append_jsonl(out/"unparsed.jsonl", {"row":idx,"source_id":row["id"],"document_id":did,"reason":"NO_ARTICLE_STRUCTURE"}, durable=False)
        except Exception as ex:
            err = {"row":idx,"source_id":row.get("id"),"error":type(ex).__name__,"message":str(ex)}
            append_jsonl(out/"errors.jsonl", err, durable=False)
            with Session(e) as s:
                run = s.get(IngestionRun, rid)
                run.errors = [*run.errors, err]
                s.commit()
        processed = idx+1
        # SQLite is committed first. Atomic replace survives process interruption;
        # fsync of a separate JSON file for every document needlessly serializes HDD I/O.
        atomic_json(out/"checkpoint.json", {"next_row":processed}, durable=False)
        if processed % 500 == 0:
            print(f"ingested {processed} source records", flush=True)
    with Session(e) as s:
        meta = s.get(DatabaseMeta, "sources")
        sources = meta.value if meta else []
        if manifest not in sources:
            sources = [*sources, manifest]
        s.merge(DatabaseMeta(key="sources",value=sources))
        s.commit()
    relations = link_document_references(e)
    stats = {"run_id":rid,"processed_rows":processed, **counts(e), "new_reference_relations":relations, "snapshot_id":snapshot(e)}
    if e.dialect.name=="sqlite":
        from rule_retrieval.database.snapshots import freeze_sqlite
        stats["frozen_snapshot_file"]=str(freeze_sqlite(e,stats["snapshot_id"]))
    atomic_json(out/"stats.json",stats)
    return stats


def link_document_references(e) -> int:
    # Only REFERS_TO with an actual source citation. Never infer amendment/repeal history.
    added = 0
    with Session(e) as s:
        numbers = {}
        for did,number in s.execute(select(LegalDocument.document_id,LegalDocument.document_number)):
            if number:
                numbers.setdefault(normalize(number), []).append(did)
        existing=set(s.scalars(select(LegalRelation.relation_id)))
        docs=s.execute(select(LegalDocument.document_id,LegalDocument.raw_metadata,LegalDocument.raw_text)).yield_per(16)
        for did,metadata,raw_text in docs:
            for cite in metadata.get("citations", []):
                raw = cite.get("raw") or ""
                for number in re.findall(r"\d+[\w-]*/\d{4}/[\wĐđ-]+|\d+/[\wĐđ-]+", raw):
                    targets = numbers.get(normalize(number), [])
                    if len(targets) != 1 or targets[0] == did:
                        continue
                    offset = raw_text.find(raw)
                    if offset < 0:
                        continue
                    relid = stable_id("R", did,targets[0],"REFERS_TO")
                    if relid not in existing:
                        s.add(LegalRelation(relation_id=relid, source_id=did, target_id=targets[0], relation_type="REFERS_TO",
                            evidence={"quote":raw,"start":offset,"end":offset+len(raw)},verification_status="SOURCE_EVIDENCED"))
                        added += 1
                        existing.add(relid)
        s.commit()
    return added
