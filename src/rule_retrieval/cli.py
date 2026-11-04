from __future__ import annotations
from pathlib import Path
import json
import os
import typer
from rule_retrieval.config import config,path,hf_environment

app=typer.Typer(no_args_is_help=True,pretty_exceptions_enable=False)


def show(obj):
    typer.echo(json.dumps(obj,ensure_ascii=False,indent=2,default=str))


@app.command("download")
def download_command(kind:str=typer.Option("both"),shard:str|None=typer.Option(None),revision:str|None=typer.Option(None)):
    """Inspect HF repository and download one documents shard per selected source."""
    from rule_retrieval.ingestion.download import download,inspect
    if kind not in {"both","judgments","statutes"} or (kind=="both" and shard):
        raise typer.BadParameter("Use --kind judgments|statutes when specifying --shard")
    for source in ["judgments","statutes"] if kind=="both" else [kind]:
        file=download(source,shard,revision)
        result=inspect(file)
        show({"kind":source,"file":str(file),"size_bytes":file.stat().st_size,"rows":result["rows"],"schema":result["schema"]})


@app.command("inspect")
def inspect_command(file:Path):
    from rule_retrieval.ingestion.download import inspect
    show(inspect(path(file)))


@app.command("build-database")
def build_database(file:Path|None=typer.Option(None),limit:int|None=typer.Option(None),resume:str|None=typer.Option(None)):
    from rule_retrieval.database.engine import engine
    from rule_retrieval.ingestion.statutes import ingest
    file=path(file or f"data/raw/statutes/{config('datasets')['statutes']['filename']}")
    show(ingest(file,engine(),limit,resume))


@app.command("search")
def search(query:str,event_date:str|None=typer.Option(None),top_k:int=typer.Option(5)):
    from rule_retrieval.retrieval.backend import BM25Backend
    show(BM25Backend().search_provisions(query,event_date,None,top_k))


@app.command("build-dataset")
def build_dataset(file:Path|None=typer.Option(None),target:int=typer.Option(30),max_attempts:int=typer.Option(150),scan_limit:int=typer.Option(100000),resume:str|None=typer.Option(None),prepare_only:bool=typer.Option(False)):
    from rule_retrieval.clients.openai import ChatClient
    from rule_retrieval.database.engine import engine
    from rule_retrieval.dataset_builder.pipeline import build_dataset as build
    file=path(file or f"data/raw/judgments/{config('datasets')['judgments']['filename']}")
    show(build(file,engine(),ChatClient(),target,max_attempts,scan_limit,resume,prepare_only))


@app.command("run-agent")
def run(dataset_run:str=typer.Option(...),limit:int|None=typer.Option(5),resume:str|None=typer.Option(None),search_budget:int|None=typer.Option(None),temporal:str|None=typer.Option(None),mode:str=typer.Option("agent"),backend_name:str|None=typer.Option(None,"--backend"),case_id:list[str]|None=typer.Option(None,"--case-id")):
    from rule_retrieval.clients.openai import ChatClient
    from rule_retrieval.harness.controller import Budget
    from rule_retrieval.harness.runner import run_agent
    from rule_retrieval.harness.isolation import deny_egress_except_inference
    from rule_retrieval.retrieval.backend import backend
    from rule_retrieval.database.snapshots import runtime_url
    from rule_retrieval.storage.files import read_json
    if mode not in {"agent","direct"}:
        raise typer.BadParameter("--mode agent|direct")
    if "/" in dataset_run or ".." in dataset_run:
        raise typer.BadParameter("Dataset run must be a run ID, not a filesystem path")
    cfg=config("harness")["agent"]
    if search_budget is not None:
        cfg["max_search_calls"]=search_budget
    if temporal is not None:
        cfg["temporal_mode"]=temporal
    if backend_name is not None:
        cfg["backend"]=backend_name
    budget=Budget(**cfg)
    client=ChatClient()
    public_dir=path(f"data/benchmark/public_inputs/{dataset_run}")
    index=read_json(public_dir/"index.json")
    retriever=backend(budget.backend,url=runtime_url(index["db_snapshot_id"]),temporal_mode=budget.temporal_mode)
    deny_egress_except_inference(client.cfg.base_url,client.uds)
    show(run_agent(public_dir,retriever,client,budget,limit,resume,mode,case_id))


@app.command("evaluate")
def evaluate_command(run_id:str,dataset_run:str=typer.Option(...)):
    from rule_retrieval.evaluation.evaluator import evaluate
    from rule_retrieval.database.engine import engine
    show(evaluate(path(f"outputs/agent_runs/{run_id}"),path(f"data/benchmark/private_references/{dataset_run}"),engine()))


@app.command("audit-dataset")
def audit_dataset_command(dataset_run:str,judgments:Path|None=typer.Option(None)):
    from rule_retrieval.dataset_builder.audit import audit_dataset
    file=path(judgments or f"data/raw/judgments/{config('datasets')['judgments']['filename']}")
    result=audit_dataset(dataset_run,file)
    show(result)
    if result["status"]!="PASS":
        raise typer.Exit(1)


@app.command("database-stats")
def database_stats():
    from rule_retrieval.database.engine import engine,counts,snapshot
    from sqlalchemy import text
    e=engine()
    with e.connect() as c:
        temporal=dict(c.execute(text("SELECT temporal_status,count(*) FROM provision_versions GROUP BY temporal_status")).all())
    show({**counts(e),"snapshot_id":snapshot(e),"temporal_statuses":temporal})


@app.command("freeze-database")
def freeze_database_command():
    from rule_retrieval.database.engine import engine
    from rule_retrieval.database.snapshots import freeze_sqlite
    file=freeze_sqlite(engine())
    show({"frozen_snapshot_file":str(file)})


if __name__=="__main__":
    app()
