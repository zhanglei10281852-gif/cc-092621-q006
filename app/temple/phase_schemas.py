from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CheckItemInput(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)


class PhaseTemplateCreate(BaseModel):
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    craft_type: Literal["woodwork", "tiling", "painting", "sculpture", "stonework", "other"]
    check_items: list[CheckItemInput] = Field(min_length=1, max_length=100)
    required_roles: list[str] = Field(min_length=1, max_length=20)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def unique_values(self) -> "PhaseTemplateCreate":
        if len({item.code for item in self.check_items}) != len(self.check_items):
            raise ValueError("检查项编码不能重复")
        if len(set(self.required_roles)) != len(self.required_roles):
            raise ValueError("验收责任人角色不能重复")
        return self


class TemplateRetire(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)


class PhaseInput(BaseModel):
    template_id: int | None = Field(default=None, gt=0)
    code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str | None = Field(default=None, max_length=160)
    craft_type: Literal["woodwork", "tiling", "painting", "sculpture", "stonework", "other"] | None = None
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    check_items: list[CheckItemInput] | None = Field(default=None, max_length=100)
    required_roles: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def source_complete(self) -> "PhaseInput":
        if self.template_id is None:
            if not self.name or self.craft_type is None or not self.check_items or not self.required_roles:
                raise ValueError("未引用模板时，工序名称、类型、检查项与验收责任人为必填")
        return self


class CampaignPhasesCreate(BaseModel):
    phases: list[PhaseInput] = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=120)


class PhaseAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)


class PhaseSubmit(BaseModel):
    submission_key: str = Field(min_length=6, max_length=160)
    submitted_by: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=1000)


class DefectInput(BaseModel):
    defect_code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    description: str = Field(min_length=2, max_length=600)
    severity: Literal["minor", "major", "critical"]


class ApprovalRecord(BaseModel):
    approver_key: str = Field(min_length=1, max_length=120)
    approver_role: str = Field(default="", max_length=120)
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=1000)
    defects: list[DefectInput] = Field(default_factory=list, max_length=100)


class ApprovalWithdraw(BaseModel):
    approver_key: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)


class DefectRaise(BaseModel):
    defect_code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    description: str = Field(min_length=2, max_length=600)
    severity: Literal["minor", "major", "critical"]
    raised_by: str = Field(min_length=1, max_length=120)


class RectificationSubmit(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    note: str = Field(min_length=2, max_length=600)


class DefectReverify(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    result: Literal["passed", "failed"]
    note: str = Field(default="", max_length=600)
