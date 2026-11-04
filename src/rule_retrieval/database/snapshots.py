"""Materialize an immutable SQLite file, independent of WAL/SHM sidecars."""
from pathlib import Path
import os
import re
import sqlite3
import uuid
from rule_retrieval.config import path,database_url,config
from rule_retrieval.database.engine import snapshot,engine
from rule_retrieval.storage.files import atomic_json,now


def freeze_sqlite(e,snapshot_id:str|None=None,destination_dir:Path|None=None) -> Path:
    if e.dialect.name!="sqlite":
        raise ValueError("SQLite file snapshots require a SQLite database")
    sid=snapshot_id or snapshot(e)
    if not re.fullmatch(r"DB_[0-9a-f]{64}",sid):
        raise ValueError("Invalid database snapshot ID")
    directory=destination_dir or path("data/database/snapshots")
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/f"{sid}.sqlite"
    if target.exists():
        frozen=engine("sqlite:///"+str(target))
        try:
            if snapshot(frozen)!=sid:
                raise ValueError("Existing frozen snapshot failed content verification")
        finally:
            frozen.dispose()
        return target
    temp=directory/f".snapshot-{uuid.uuid4().hex}.sqlite"
    try:
        source=sqlite3.connect("file:"+str(Path(e.url.database).resolve())+"?mode=ro",uri=True)
        destination=sqlite3.connect(temp)
        try:
            source.backup(destination,pages=4096)
            destination.execute("PRAGMA journal_mode=DELETE")
            destination.commit()
        finally:
            destination.close()
            source.close()
        copied=engine("sqlite:///"+str(temp))
        try:
            if snapshot(copied)!=sid:
                raise ValueError("Database changed during snapshot creation; copy rejected")
        finally:
            copied.dispose()
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.chmod(temp,0o444)
        os.replace(temp,target)
        atomic_json(target.with_suffix(".manifest.json"),{"snapshot_id":sid,"file":target.name,"created_at":now(),"journal_mode":"DELETE","readonly":True})
        return target
    finally:
        if temp.exists():
            temp.unlink()


def runtime_url(snapshot_id:str) -> str:
    if not re.fullmatch(r"DB_[0-9a-f]{64}",snapshot_id):
        raise ValueError("Invalid database snapshot ID")
    url=database_url()
    # Explicit alternate database URLs remain authoritative.
    if not os.environ.get("RULE_RETRIEVAL_DATABASE_URL") and config("database")["url"]=="sqlite:///data/database/statutes.sqlite":
        frozen=path("data/database/snapshots")/f"{snapshot_id}.sqlite"
        if frozen.exists():
            return "sqlite:///"+str(frozen)
        if os.environ.get("RULE_RETRIEVAL_OS_ISOLATED"):
            raise FileNotFoundError("Freeze the completed database first: rule-retrieval freeze-database")
    return url
