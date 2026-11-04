"""Download exactly one explicitly selected documents shard per source."""
from pathlib import Path
import fnmatch
import time
from rule_retrieval.config import config, path, hf_environment
from rule_retrieval.storage.files import atomic_json, sha256, now, read_json


def download(kind: str, filename: str | None = None, revision: str | None = None) -> Path:
    hf_environment()
    from huggingface_hub import HfApi, hf_hub_download
    cfg = config("datasets")[kind]
    repo = cfg["repo_id"]
    filename = filename or cfg["filename"]
    info = HfApi().dataset_info(repo, revision=revision or cfg["revision"], files_metadata=True)
    entries = [c for c in (info.card_data.to_dict().get("configs", []) if info.card_data else [])
               if c.get("config_name") == "documents"]
    patterns = [d["path"] for c in entries for d in c.get("data_files", [])]
    if not patterns or not any(fnmatch.fnmatch(filename, p) for p in patterns):
        raise ValueError("Selected file is not in the repository's documents config")
    file_info = next((s for s in info.siblings if s.rfilename == filename), None)
    if file_info is None or not filename.endswith(".parquet"):
        raise ValueError(f"Missing documents parquet: {filename}")
    destination = path(f"data/raw/{kind}")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / filename
    manifest_path = destination / (filename + ".manifest.json")
    expected = file_info.lfs.sha256 if file_info.lfs else None
    if target.exists() and manifest_path.exists():
        old = read_json(manifest_path)
        digest = sha256(target)
        if old["revision"] == info.sha and digest == old["sha256"] and (not expected or digest == expected):
            return target
    for attempt in range(config("datasets").get("download_retries", 4)):
        try:
            downloaded = Path(hf_hub_download(repo, filename, repo_type="dataset", revision=info.sha,
                                            local_dir=destination, cache_dir=path("data/hf_cache/hub")))
            digest = sha256(downloaded)
            if expected and digest != expected:
                downloaded.unlink()
                raise ValueError("Downloaded file SHA256 mismatch")
            if file_info.size and downloaded.stat().st_size != file_info.size:
                raise ValueError("Downloaded file size mismatch")
            atomic_json(manifest_path, {"dataset_repo": repo, "config": "documents", "revision": info.sha,
                        "filename": filename, "sha256": digest, "expected_sha256": expected,
                        "size_bytes": downloaded.stat().st_size, "download_time": now()})
            return downloaded
        except Exception:
            if attempt + 1 == config("datasets").get("download_retries", 4):
                raise
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("download exhausted")


def rows(file: Path, batch_size: int = 32, limit: int | None = None):
    import pyarrow.parquet as pq
    count = 0
    for batch in pq.ParquetFile(file).iter_batches(batch_size=batch_size):
        for row in batch.to_pylist():
            if limit is not None and count >= limit:
                return
            yield row
            count += 1


def inspect(file: Path) -> dict:
    import pyarrow.parquet as pq
    p = pq.ParquetFile(file)
    first = next(rows(file, 1, 1), {})
    result = {"file": str(file), "rows": p.metadata.num_rows, "row_groups": p.metadata.num_row_groups,
              "schema": str(p.schema_arrow), "first_record": first}
    atomic_json(file.with_suffix(".schema.json"), result)
    return result
