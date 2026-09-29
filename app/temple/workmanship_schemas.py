from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Severity = Literal["minor", "major", "critical"]


class WorkmanshipCheckItem(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=160)
    required: bool = True


class WorkmanshipSignatory(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    role_name: str = Field(default="", max_length=120)


class WorkmanshipTemplateCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    sequence_no: int = Field(ge=0, le=10_000)
    checks: list[WorkmanshipCheckItem] = Field(min_length=1, max_length=200)
    signatories: list[WorkmanshipSignatory] = Field(min_length=1, max_length=50)
    depends_on: list[str] = Field(default_factory=list, max_length=200)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_unique(self) -> "WorkmanshipTemplateCreate":
        check_codes = [item.code for item in self.checks]
        signatory_codes = [item.code for item in self.signatories]
        if len(check_codes) != len(set(check_codes)):
            raise ValueError("检查项编码不能重复")
        if len(signatory_codes) != len(set(signatory_codes)):
            raise ValueError("责任人编码不能重复")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("依赖工序不能重复")
        if self.code in self.depends_on:
            raise ValueError("工序不能依赖自身")
        return self


class WorkmanshipStageSubmit(BaseModel):
    submitter: str = Field(min_length=1, max_length=120)
    submission_key: str = Field(min_length=6, max_length=160)
    site_notes: str = Field(default="", max_length=2000)
    check_results: list[dict] = Field(default_factory=list, max_length=500)


class WorkmanshipSignatureCreate(BaseModel):
    signatory_code: str = Field(min_length=1, max_length=64)
    signatory_name: str = Field(default="", max_length=120)
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=1000)


class SignatureRevoke(BaseModel):
    signatory_code: str = Field(min_length=1, max_length=64)
    revoked_by: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)


class WorkmanshipDefectCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    detail: str = Field(default="", max_length=2000)
    severity: Severity
    reporter: str = Field(min_length=1, max_length=120)


class WorkmanshipDefectReport(WorkmanshipDefectCreate):
    """缺陷登记可由现场或签署环节发起。"""


class RectificationSubmit(BaseModel):
    rectification_key: str = Field(min_length=6, max_length=160)
    action: str = Field(min_length=2, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=200)
    rectified_by: str = Field(min_length=1, max_length=120)


class RectificationReview(BaseModel):
    accepted: bool
    comment: str = Field(default="", max_length=1000)
    reviewer: str = Field(min_length=1, max_length=120)


class TargetPauseAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)
