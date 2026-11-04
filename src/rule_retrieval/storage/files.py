from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import tempfile
import uuid


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, *values) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str).encode()
    return prefix + "_" + hashlib.sha256(raw).hexdigest()[:24]


def run_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"


def sha256(file: Path) -> str:
    h = hashlib.sha256()
    with file.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(file: Path, obj, private: bool = False, durable: bool = False) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=file.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            if durable:
                os.fsync(f.fileno())
        os.chmod(name, 0o600 if private else 0o644)
        os.replace(name, file)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def append_jsonl(file: Path, obj, durable: bool = False) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        f.flush()
        if durable:
            os.fsync(f.fileno())


def read_json(file: Path):
    return json.loads(file.read_text())


def code_version() -> str:
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    except (OSError, subprocess.CalledProcessError):
        from rule_retrieval.config import root
        h = hashlib.sha256()
        for f in sorted((root() / "src").rglob("*.py")):
            h.update(str(f.relative_to(root())).encode())
            h.update(f.read_bytes())
        return "source-sha256:" + h.hexdigest()
