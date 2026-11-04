from __future__ import annotations
from dataclasses import dataclass,asdict
from rule_retrieval.harness.protocol import Action,ARGUMENTS
from rule_retrieval.temporal.policy import temporal_check


class BudgetError(RuntimeError):
    pass


@dataclass
class Budget:
    max_search_calls:int=4
    max_total_tool_calls:int=10
    max_search_results:int=5
    max_final_rules_per_issue:int=10
    max_turns:int=14
    allow_abstain:bool=True
    external_web_access:bool=False
    temporal_mode:str="uncertainty_aware"
    backend:str="bm25"

    def __post_init__(self):
        if self.external_web_access:
            raise ValueError("External agent access is forbidden")
        for k in ["max_search_calls","max_total_tool_calls"]:
            if getattr(self,k)<0:
                raise ValueError(f"{k} must be nonnegative")
        if min(self.max_search_results,self.max_final_rules_per_issue,self.max_turns)<1:
            raise ValueError("Result, final-rule and turn bounds must be positive")


class Controller:
    def __init__(self,backend,public_case:dict,budget:Budget,state:dict|None=None):
        self.backend=backend
        self.case=public_case
        self.budget=budget
        state=state or {}
        self.total_calls=state.get("total_calls",0)
        self.search_calls=state.get("search_calls",0)
        self.violations=state.get("violations",[])
        self.seen=state.get("seen",{})
        self.invalid_submissions=state.get("invalid_submissions",0)
        self.ranked_searches=state.get("ranked_searches",[])
        self.final_only=state.get("final_only",False)
        self.before_execute=None

    def state(self):
        return {"total_calls":self.total_calls,"search_calls":self.search_calls,"violations":self.violations,
                "seen":self.seen,"invalid_submissions":self.invalid_submissions,"ranked_searches":self.ranked_searches,"final_only":self.final_only}

    def dispatch(self,raw:dict) -> dict:
        # Unknown names and malformed args still consume attempts, preventing an unbounded error loop.
        is_submit=raw.get("action")=="submit_rules"
        if not is_submit:
            if self.total_calls>=self.budget.max_total_tool_calls:
                self.violations.append("MAX_TOTAL_TOOL_CALLS")
                raise BudgetError("Total read-tool budget exhausted; submit now")
            self.total_calls+=1
        action=Action.model_validate(raw)
        args=ARGUMENTS[action.action].model_validate(action.arguments).model_dump()
        if is_submit:
            return self.submit(args)
        if self.final_only:
            self.violations.append("FINAL_ONLY_CONTEXT")
            raise BudgetError("Context budget is reserved for submit_rules; further read tools are disabled")
        if action.action=="search_provisions":
            if self.search_calls>=self.budget.max_search_calls:
                self.violations.append("MAX_SEARCH_CALLS")
                raise BudgetError("Search budget exhausted; submit now")
            if args["top_k"]>self.budget.max_search_results:
                self.violations.append("MAX_SEARCH_RESULTS")
                raise BudgetError("top_k exceeds controller limit")
            self.search_calls+=1
        # No dynamic attribute lookup or eval of model-provided tool names.
        funcs={"search_provisions":self.backend.search_provisions,"get_provision":self.backend.get_provision,
               "get_provision_history":self.backend.get_provision_history,"get_related_provisions":self.backend.get_related_provisions}
        if self.before_execute is not None:
            self.before_execute()
        result=funcs[action.action](**args)
        records=result if isinstance(result,list) else [result]
        visible=[]
        for record in records:
            v=dict(record)
            if action.action!="get_provision" and len(v.get("content",""))>2200:
                v["content"]=v["content"][:2200]
                v["content_truncated"]=True
                v["retrieval_note"]="Use get_provision to read the complete provision."
            self.seen[v["provision_version_id"]]=v
            visible.append(v)
        if action.action=="search_provisions":
            self.ranked_searches.append({"query":args["query"],"event_date":args["event_date"],"ids":[v["provision_version_id"] for v in visible]})
        return {"results":visible,"remaining_search_calls":self.budget.max_search_calls-self.search_calls,
                "remaining_tool_calls":self.budget.max_total_tool_calls-self.total_calls}

    def submit(self,args:dict) -> dict:
        predictions=args["predictions"]
        iids=[p["issue_id"] for p in predictions]
        expected={i["issue_id"] for i in self.case["input"]["legal_issues"]}
        if len(iids)!=len(set(iids)) or set(iids)!=expected:
            self.invalid_submissions+=1
            raise ValueError("Predictions must cover every given issue exactly once")
        events={e["event_id"]:e for e in self.case["input"]["events"]}
        for prediction in predictions:
            rules=prediction["rules"]
            if len(rules)>self.budget.max_final_rules_per_issue:
                self.violations.append("MAX_FINAL_RULES")
                raise BudgetError("Too many final rules per issue")
            if not rules and (not self.budget.allow_abstain or not prediction.get("abstain_reason")):
                raise ValueError("Empty rule list requires an allowed, explicit abstention")
            ids=[r["provision_version_id"] for r in rules]
            if len(ids)!=len(set(ids)):
                raise ValueError("Duplicate prediction IDs")
            for rule in rules:
                vid=rule["provision_version_id"]
                if vid not in self.seen:
                    self.invalid_submissions+=1
                    raise ValueError("UNGROUNDED_PROVISION_ID: ID was not returned by a tool in this case")
                retrieved=self.seen[vid]
                if rule["evidence"] not in retrieved["content"]:
                    self.invalid_submissions+=1
                    raise ValueError("UNGROUNDED_QUOTE: evidence must be an exact model-visible provision quote")
                eid=rule.get("event_id")
                if eid is not None and eid not in events:
                    raise ValueError("Unknown event_id")
                assessment=temporal_check(retrieved,events[eid]["date"] if eid else None)
                rule["model_temporal_status"]=rule["temporal_status"]
                rule["temporal_status"]=assessment
                rule["temporal_note"]="Recorded interval check only; legal applicability and exceptions require evidence."
                rule["grounded"]=True
        return {"submitted":True,"predictions":predictions}
