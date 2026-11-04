"""One local Transformers instance; OpenAI-compatible JSON and native-tool requests."""
from __future__ import annotations
import argparse
import json
import os
import time
import uuid
import threading
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from rule_retrieval.config import hf_environment


def create_app(model_path: str, model_id: str = "qwen-local", max_input_tokens: int = 24576):
    hf_environment()
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(model_path, local_files_only=True,
            torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
            device_map="auto", attn_implementation="sdpa").eval()
    lock = threading.Lock()
    torch.set_num_threads(min(os.cpu_count() or 4, 8))
    app = FastAPI(title="Local legal-rule benchmark inference")

    @app.get("/health")
    def health():
        return {"status":"ok","model":model_id,"device":str(model.device),"backend":"transformers","max_input_tokens":max_input_tokens,"torch_version":torch.__version__,"cuda_version":torch.version.cuda}

    @app.get("/v1/models")
    def models():
        return {"object":"list","data":[{"id":model_id,"object":"model","owned_by":"local"}]}

    @app.post("/v1/chat/completions")
    def complete(request: dict):
        if request.get("model") != model_id:
            raise HTTPException(404,"Unknown model ID")
        messages = request.get("messages",[])
        if request.get("tools"):
            raise HTTPException(400,"Use the JSON-action fallback: this server does not implement native function calling")
        maximum = int(request.get("max_tokens",1800))
        if maximum < 1 or maximum > 4096:
            raise HTTPException(400,"max_tokens must be between 1 and 4096")
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt")
        input_count = inputs.input_ids.shape[1]
        if input_count > max_input_tokens or input_count+maximum > model.config.max_position_embeddings:
            raise HTTPException(400,f"Context too long: {input_count} input tokens; no silent truncation")
        with lock, torch.inference_mode():
            inputs = inputs.to(model.device)
            temp = float(request.get("temperature",0))
            args = {"max_new_tokens":maximum,"do_sample":temp>0,"pad_token_id":tokenizer.eos_token_id,"temperature":None,"top_p":None,"top_k":None}
            if temp>0:
                args["temperature"] = temp
            generated = model.generate(**inputs,**args)
        tokens = generated[0,input_count:]
        content = tokenizer.decode(tokens,skip_special_tokens=True)
        return {"id":"chatcmpl-"+uuid.uuid4().hex,"object":"chat.completion","created":int(time.time()),"model":model_id,
                "choices":[{"index":0,"message":{"role":"assistant","content":content},"finish_reason":"length" if len(tokens)==maximum else "stop"}],
                "usage":{"prompt_tokens":input_count,"completion_tokens":len(tokens),"total_tokens":input_count+len(tokens)}}
    return app


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model-path",default=os.environ.get("RULE_RETRIEVAL_MODEL_PATH"), required=not bool(os.environ.get("RULE_RETRIEVAL_MODEL_PATH")))
    p.add_argument("--model-id",default="qwen-local")
    p.add_argument("--host",default="127.0.0.1")
    p.add_argument("--port",type=int,default=8000)
    p.add_argument("--uds")
    p.add_argument("--max-input-tokens",type=int,default=24576)
    a = p.parse_args()
    app = create_app(a.model_path,a.model_id,a.max_input_tokens)
    import uvicorn
    uvicorn.run(app,host=a.host,port=a.port,uds=a.uds)


if __name__ == "__main__":
    main()
