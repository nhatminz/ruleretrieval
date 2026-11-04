from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from rule_retrieval.temporal.policy import parse_date


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceSpan(StrictModel):
    field: str = "markdown"
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str

    @model_validator(mode="after")
    def order(self):
        if self.end <= self.start or self.end-self.start != len(self.quote):
            raise ValueError("Invalid exact source span")
        return self


class Fact(StrictModel):
    fact_id: str
    text: str = Field(min_length=20)
    source_span: SourceSpan


class Event(StrictModel):
    event_id: str
    event_type: Literal["transaction","criminal_conduct","employment","filing_date","judgment_date","other"]
    date: str
    date_evidence: str
    fact_id: str

    @field_validator("date")
    @classmethod
    def valid_date(cls,v):
        if parse_date(v) != v:
            raise ValueError("date must be YYYY-MM-DD")
        return v


class LegalIssue(StrictModel):
    issue_id: str
    legal_question: str = Field(min_length=15)
    trigger_fact_ids: list[str] = Field(min_length=1)


class CaseInput(StrictModel):
    facts: list[Fact] = Field(min_length=1)
    events: list[Event]
    legal_issues: list[LegalIssue] = Field(min_length=1)
    date_uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def integrity(self):
        fids = {f.fact_id for f in self.facts}
        if len(fids)!=len(self.facts) or len({i.issue_id for i in self.legal_issues})!=len(self.legal_issues):
            raise ValueError("Duplicate fact or issue IDs")
        if any(not set(i.trigger_fact_ids)<=fids for i in self.legal_issues):
            raise ValueError("Unknown trigger fact ID")
        if any(e.fact_id not in fids for e in self.events):
            raise ValueError("Unknown event fact ID")
        if len({e.event_id for e in self.events})!=len(self.events):
            raise ValueError("Duplicate event IDs")
        return self


class PublicCase(StrictModel):
    case_id: str
    input: CaseInput
