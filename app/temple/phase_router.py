from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.phase_schemas import (
    ApprovalRecord,
    ApprovalWithdraw,
    CampaignPhasesCreate,
    DefectRaise,
    DefectReverify,
    PhaseAction,
    PhaseSubmit,
    PhaseTemplateCreate,
    RectificationSubmit,
    TemplateRetire,
)
from app.temple.phase_workflow import PhaseWorkflowService

router = APIRouter(prefix="/api/temple/operations", tags=["修缮工序验收闭环"])


def service() -> PhaseWorkflowService:
    return PhaseWorkflowService()


@router.post("/phase_templates", status_code=201)
def create_phase_template(payload: PhaseTemplateCreate):
    return service().create_phase_template(payload.model_dump())


@router.get("/phase_templates")
def list_phase_templates(state: str | None = Query(default=None, pattern="^(active|retired)$")):
    return {"items": service().list_phase_templates(state)}


@router.post("/phase_templates/{template_id}/retire")
def retire_phase_template(template_id: int, payload: TemplateRetire):
    return service().retire_phase_template(template_id, payload.actor, payload.reason)


@router.post("/restoration_campaigns/{restoration_campaign_id}/phases", status_code=201)
def add_campaign_phases(restoration_campaign_id: int, payload: CampaignPhasesCreate):
    return service().add_campaign_phases(restoration_campaign_id, payload.model_dump())


@router.get("/restoration_campaigns/{restoration_campaign_id}/workflow")
def workflow_detail(restoration_campaign_id: int):
    return service().workflow_detail(restoration_campaign_id)


@router.post("/phases/{phase_id}/open")
def open_phase(phase_id: int, payload: PhaseAction):
    return service().open_phase(phase_id, payload.actor, payload.reason)


@router.post("/phases/{phase_id}/submit")
def submit_phase(phase_id: int, payload: PhaseSubmit):
    return service().submit_phase(phase_id, payload.model_dump())


@router.post("/submissions/{submission_id}/approvals")
def record_approval(submission_id: int, payload: ApprovalRecord):
    return service().record_approval(submission_id, payload.model_dump())


@router.post("/submissions/{submission_id}/withdraw")
def withdraw_approval(submission_id: int, payload: ApprovalWithdraw):
    return service().withdraw_approval(submission_id, payload.model_dump())


@router.post("/submissions/{submission_id}/defects", status_code=201)
def raise_defect(submission_id: int, payload: DefectRaise):
    return service().raise_defect(submission_id, payload.model_dump())


@router.post("/defects/{defect_id}/rectification")
def submit_rectification(defect_id: int, payload: RectificationSubmit):
    return service().submit_rectification(defect_id, payload.model_dump())


@router.post("/defects/{defect_id}/reverify")
def reverify_defect(defect_id: int, payload: DefectReverify):
    return service().reverify_defect(defect_id, payload.model_dump())
