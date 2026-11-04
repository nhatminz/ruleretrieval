from pathlib import Path
import os
import yaml


def root() -> Path:
    return Path(os.environ.get("RULE_RETRIEVAL_ROOT", Path(__file__).resolve().parents[2])).resolve()


def path(value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root() / p


def config(name: str) -> dict:
    with (root() / "configs" / f"{name}.yaml").open() as f:
        return yaml.safe_load(f)


def database_url() -> str:
    url = os.environ.get("RULE_RETRIEVAL_DATABASE_URL", config("database")["url"])
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        return "sqlite:///" + str(path(url.removeprefix("sqlite:///")))
    return url


def hf_environment() -> None:
    # Override global caches: even metadata/tokenizer downloads must remain under data/.
    for key, value in {
        "HF_HOME": "data/hf_cache", "HF_HUB_CACHE": "data/hf_cache/hub",
        "HF_DATASETS_CACHE": "data/hf_cache/datasets", "HF_XET_CACHE": "data/hf_cache/xet",
        "TRANSFORMERS_CACHE": "data/hf_cache/transformers",
    }.items():
        os.environ[key] = str(path(value))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
