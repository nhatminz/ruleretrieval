from __future__ import annotations
from dataclasses import dataclass, asdict
import os
import json
import time
from pathlib import Path
import httpx
from rule_retrieval.config import config


@dataclass
class ModelConfig:
    base_url: str = "http://127.0.0.1:8000/v1"
    model_id: str = "qwen-local"
    timeout: float = 180
    temperature: float = 0
    max_tokens: int = 1800
    retries: int = 2
    native_tools: bool = False
    max_context_chars: int = 42000

    @classmethod
    def load(cls):
        d = config("models")
        for key, env in [("base_url","RULE_RETRIEVAL_BASE_URL"),("model_id","RULE_RETRIEVAL_MODEL_ID")]:
            if os.environ.get(env):
                d[key] = os.environ[env]
        return cls(**d)


class ChatClient:
    def __init__(self, cfg: ModelConfig | None = None):
        self.cfg = cfg or ModelConfig.load()
        self.uds = os.environ.get("RULE_RETRIEVAL_INFERENCE_UDS")
        transport = httpx.HTTPTransport(uds=self.uds) if self.uds else None
        self.http = httpx.Client(timeout=self.cfg.timeout, transport=transport, trust_env=False, follow_redirects=False)

    def complete(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = True) -> dict:
        payload = {"model":self.cfg.model_id,"messages":messages,"temperature":self.cfg.temperature,"max_tokens":self.cfg.max_tokens}
        if tools and self.cfg.native_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "required"
        elif json_mode:
            payload["response_format"] = {"type":"json_object"}
        attempts = []
        for attempt in range(self.cfg.retries+1):
            start = time.monotonic()
            try:
                headers = {}
                if os.environ.get("RULE_RETRIEVAL_API_KEY"):
                    headers["Authorization"] = "Bearer " + os.environ["RULE_RETRIEVAL_API_KEY"]
                response = self.http.post(self.cfg.base_url.rstrip("/")+"/chat/completions",json=payload,headers=headers)
                response.raise_for_status()
                data = response.json()
                if not data.get("choices"):
                    raise ValueError("Inference response has no choices")
                return {"message":data["choices"][0]["message"],"usage":data.get("usage",{}),
                        "latency":time.monotonic()-start,"attempts":attempts,
                        "finish_reason":data["choices"][0].get("finish_reason")}
            except (httpx.HTTPError, ValueError, KeyError) as ex:
                attempts.append({"attempt":attempt,"error":type(ex).__name__,"message":str(ex),"latency":time.monotonic()-start})
                if isinstance(ex,httpx.HTTPStatusError):
                    body=ex.response.text[:4000]
                    key=os.environ.get("RULE_RETRIEVAL_API_KEY")
                    if key:
                        body=body.replace(key,"[REDACTED]")
                    attempts[-1].update({"status_code":ex.response.status_code,"response_body":body})
                retryable = not isinstance(ex,httpx.HTTPStatusError) or ex.response.status_code in {408,429,500,502,503,504}
                if attempt == self.cfg.retries or not retryable:
                    raise ModelError(attempts) from ex
                time.sleep(min(2**attempt,8))
        raise ModelError(attempts)


class ModelError(RuntimeError):
    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__(str(attempts[-1]))


def json_object(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1])
    obj = json.loads(text)
    if not isinstance(obj,dict):
        raise ValueError("A single JSON object is required")
    return obj
