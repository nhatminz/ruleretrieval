from typing import Literal
from pydantic import Field, model_validator
from rule_retrieval.dataset_builder.schema import StrictModel


class SearchArgs(StrictModel):
    query: str = Field(min_length=1,max_length=2000)
    event_date: str | None = None
    legal_domain: str | None = Field(default=None,max_length=150)
    top_k: int = Field(default=5,ge=1,le=50)


class VersionArgs(StrictModel):
    provision_version_id: str = Field(pattern=r"^PV_[0-9a-f]{24}$")


class HistoryArgs(StrictModel):
    provision_id: str = Field(pattern=r"^P_[0-9a-f]{24}$")


class RulePrediction(StrictModel):
    provision_version_id: str = Field(pattern=r"^PV_[0-9a-f]{24}$")
    explanation: str = Field(min_length=1,max_length=2000)
    evidence: str = Field(min_length=5,max_length=3000)
    event_id: str | None = None
    temporal_status: Literal["TEMPORAL_UNCERTAIN","VERIFIED_IN_INTERVAL","OUTSIDE_RECORDED_INTERVAL"] = "TEMPORAL_UNCERTAIN"


class IssuePrediction(StrictModel):
    issue_id: str
    rules: list[RulePrediction]
    abstain_reason: str | None = None


class SubmitArgs(StrictModel):
    predictions: list[IssuePrediction]


class Action(StrictModel):
    action: Literal["search_provisions","get_provision","get_provision_history","get_related_provisions","submit_rules"]
    arguments: dict


ARGUMENTS={"search_provisions":SearchArgs,"get_provision":VersionArgs,"get_provision_history":HistoryArgs,
           "get_related_provisions":VersionArgs,"submit_rules":SubmitArgs}


def declarations() -> list[dict]:
    return [{"type":"function","function":{"name":name,"description":{
        "search_provisions":"Search only the internal statute corpus; UNKNOWN temporal candidates remain visible.",
        "get_provision":"Read an internal provision version by ID.","get_provision_history":"Read recorded version history; missing history is not proof of historical validity.",
        "get_related_provisions":"Read neighboring provisions and source-evidenced legal relations.","submit_rules":"Submit rules for every provided issue, with retrieval evidence or an explicit abstention."
    }[name],"parameters":schema.model_json_schema()}} for name,schema in ARGUMENTS.items()]
