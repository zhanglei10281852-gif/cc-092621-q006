from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.workmanship import WorkmanshipAcceptanceService
from app.temple.workmanship_schemas import (
    RectificationReview,
    RectificationSubmit,
    SignatureRevoke,
    TargetPauseAction,
    WorkmanshipDefectReport,
    WorkmanshipSignatureCreate,
    WorkmanshipStageSubmit,
    WorkmanshipTemplateCreate,
)

router = APIRouter(prefix="/api/temple/operations", tags=["古建修缮工序验收"])


def service() -> WorkmanshipAcceptanceService:
    return WorkmanshipAcceptanceService()


@router.post("/restoration_campaigns/{restoration_campaign_id}/workmanship_templates", status_code=201)
def create_workmanship_template(restoration_campaign_id: int, payload: WorkmanshipTemplateCreate):
    data = payload.model_dump()
    data["restoration_campaign_id"] = restoration_campaign_id
    return service().create_workmanship_template(data)


@router.get("/restoration_campaigns/{restoration_campaign_id}/workmanship_templates")
def list_workmanship_templates(restoration_campaign_id: int):
    return {"items": service().list_workmanship_templates(restoration_campaign_id)}


@router.post("/restoration_campaigns/{restoration_campaign_id}/materialize")
def materialize_campaign(restoration_campaign_id: int):
    service().materialize_campaign(restoration_campaign_id)
    return {"restoration_campaign_id": restoration_campaign_id, "materialized": True}


@router.post("/restoration_targets/{restoration_target_id}/stages/{stage_code}/submit", status_code=201)
def submit_site_work(restoration_target_id: int, stage_code: str, payload: WorkmanshipStageSubmit):
    return service().submit_site_work(restoration_target_id, stage_code, payload.model_dump())


@router.get("/restoration_targets/{restoration_target_id}/workmanship")
def target_workmanship_detail(restoration_target_id: int):
    return service().target_workmanship_detail(restoration_target_id)


@router.post("/restoration_targets/{restoration_target_id}/pause")
def pause_target(restoration_target_id: int, payload: TargetPauseAction):
    return service().pause_target(restoration_target_id, payload.actor, payload.reason)


@router.post("/restoration_targets/{restoration_target_id}/resume")
def resume_target(restoration_target_id: int, payload: TargetPauseAction):
    return service().resume_target(restoration_target_id, payload.actor, payload.reason)


@router.post("/restoration_targets/{restoration_target_id}/defects", status_code=201)
def report_defect(restoration_target_id: int, payload: WorkmanshipDefectReport, stage_code: str | None = Query(default=None)):
    return service().report_defect(restoration_target_id, payload.model_dump(), stage_code=stage_code)


@router.get("/restoration_targets/{restoration_target_id}/defects")
def list_defects(restoration_target_id: int, open_only: bool = Query(default=False)):
    return {"items": service().list_defects(restoration_target_id, open_only=open_only)}


@router.get("/acceptance_rounds/{acceptance_round_id}")
def acceptance_round_detail(acceptance_round_id: int):
    return service().acceptance_round_detail(acceptance_round_id)


@router.post("/acceptance_rounds/{acceptance_round_id}/sign")
def sign_acceptance(acceptance_round_id: int, payload: WorkmanshipSignatureCreate):
    return service().sign_acceptance(acceptance_round_id, payload.model_dump())


@router.post("/acceptance_rounds/{acceptance_round_id}/revoke")
def revoke_signature(acceptance_round_id: int, payload: SignatureRevoke):
    return service().revoke_signature(acceptance_round_id, payload.model_dump())


@router.get("/defects/{defect_id}")
def defect_detail(defect_id: int):
    return service().defect_detail(defect_id)


@router.post("/defects/{defect_id}/rectifications/start")
def start_rectification(defect_id: int, actor: str = Query(min_length=1)):
    return service().start_rectification(defect_id, actor)


@router.post("/defects/{defect_id}/rectifications", status_code=201)
def submit_rectification(defect_id: int, payload: RectificationSubmit):
    return service().submit_rectification(defect_id, payload.model_dump())


@router.post("/rectifications/{rectification_id}/review")
def review_rectification(rectification_id: int, payload: RectificationReview):
    return service().review_rectification(rectification_id, payload.model_dump())
