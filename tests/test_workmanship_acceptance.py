from __future__ import annotations

import threading

from app.database import get_connection
from app.temple.rules import DEFAULT_RULES
from app.temple.workmanship import WorkmanshipAcceptanceService

WOOD_TEMPLATE = {
    "code": "timber-frame",
    "name": "木构架工序",
    "sequence_no": 10,
    "checks": [
        {"code": "column", "name": "柱础归安", "required": True},
        {"code": "beam", "name": "梁架榫卯", "required": True},
    ],
    "signatories": [
        {"code": "heritage-officer", "name": "文保员", "role_name": "文物保护"},
        {"code": "master-carpenter", "name": "木作匠师", "role_name": "木作"},
    ],
    "depends_on": [],
}
TILE_TEMPLATE = {
    "code": "tile-roof",
    "name": "瓦作工序",
    "sequence_no": 20,
    "checks": [{"code": "tile", "name": "瓦面搭接", "required": True}],
    "signatories": [
        {"code": "heritage-officer", "name": "文保员", "role_name": "文物保护"},
        {"code": "tile-master", "name": "瓦作匠师", "role_name": "瓦作"},
    ],
    "depends_on": ["timber-frame"],
}


def prepare_campaign(client, *, templates=(WOOD_TEMPLATE, TILE_TEMPLATE)):
    client.post(
        "/api/temple/temples",
        json={"code": "baoguo-temple", "name": "报国寺", "temple_type": "heritage", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 100, "ventilation_capacity": 1000},
    )
    client.post(
        "/api/temple/temples/baoguo-temple/halls",
        json={"code": "main", "name": "大雄宝殿", "visit_order": 1, "expected_visit_seconds": 600, "ventilation_capacity": 400},
    )
    safety_policy = client.post("/api/temple/temples/baoguo-temple/policies", json={"rules": DEFAULT_RULES, "actor": "tests"}).json()
    client.post(f"/api/temple/policies/{safety_policy['id']}/publish", json={"actor": "tests", "effective_from": "2020-01-01T00:00:00Z"})
    campaign = client.post(
        "/api/temple/operations/restoration_campaigns",
        json={
            "temple_code": "baoguo-temple",
            "safety_policy_id": safety_policy["id"],
            "code": "main-hall-restoration",
            "name": "大雄宝殿修缮",
            "strategy": "halls",
            "hall_codes": ["main"],
            "actor": "operator",
        },
    ).json()
    campaign_id = campaign["id"]
    for template in templates:
        body = {"temple_code": "baoguo-temple", "actor": "operator", **template}
        response = client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/workmanship_templates", json=body)
        assert response.status_code == 201, response.text
    started = client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "operator", "reason": "开工"})
    assert started.status_code == 200, started.text
    target_id = started.json()["targets"][0]["id"]
    return campaign_id, target_id


def all_checks_passed(template):
    return [{"code": item["code"], "passed": True, "note": "合格"} for item in template["checks"]]


def approve_round(client, round_id, signatories):
    for signatory in signatories:
        response = client.post(
            f"/api/temple/operations/acceptance_rounds/{round_id}/sign",
            json={"signatory_code": signatory["code"], "signatory_name": signatory["name"], "decision": "approved", "comment": "同意"},
        )
        assert response.status_code == 200, response.text
    return client.get(f"/api/temple/operations/acceptance_rounds/{round_id}").json()


def test_template_duplicate_and_dependency_cycle(client):
    campaign_id, _ = prepare_campaign(client)
    duplicate = client.post(
        f"/api/temple/operations/restoration_campaigns/{campaign_id}/workmanship_templates",
        json={"temple_code": "baoguo-temple", "actor": "operator", **WOOD_TEMPLATE},
    )
    assert duplicate.status_code == 409
    cyclic = {
        "code": "painting",
        "name": "彩绘工序",
        "sequence_no": 30,
        "checks": [{"code": "pigment", "name": "矿物颜料", "required": True}],
        "signatories": [{"code": "heritage-officer", "name": "文保员"}],
        "depends_on": ["painting"],
    }
    response = client.post(
        f"/api/temple/operations/restoration_campaigns/{campaign_id}/workmanship_templates",
        json={"temple_code": "baoguo-temple", "actor": "operator", **cyclic},
    )
    assert response.status_code == 422
    missing_dependency = {
        "code": "gilding",
        "name": "贴金工序",
        "sequence_no": 40,
        "checks": [{"code": "gold", "name": "金箔", "required": True}],
        "signatories": [{"code": "heritage-officer", "name": "文保员"}],
        "depends_on": ["nonexistent"],
    }
    response = client.post(
        f"/api/temple/operations/restoration_campaigns/{campaign_id}/workmanship_templates",
        json={"temple_code": "baoguo-temple", "actor": "operator", **missing_dependency},
    )
    assert response.status_code == 422


def test_submission_freezes_checks_and_full_approval_chain(client):
    campaign_id, target_id = prepare_campaign(client)
    detail = client.get(f"/api/temple/operations/restoration_targets/{target_id}/workmanship").json()
    assert detail["constructible_stages"] == ["timber-frame"]
    assert detail["work_allowed"] is True
    wood = next(item for item in detail["stages"] if item["code"] == "timber-frame")
    assert {item["code"] for item in wood["checks"]} == {"column", "beam"}
    assert {item["code"] for item in wood["signatories"]} == {"heritage-officer", "master-carpenter"}

    # 瓦作依赖木构，依赖未满足不能提交
    blocked = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/tile-roof/submit",
        json={"submitter": "foreman", "submission_key": "tile-first", "check_results": [{"code": "tile", "passed": True}]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["blocked_by_stage"] == "timber-frame"

    # 检查项必须逐项填报
    incomplete = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-bad", "check_results": [{"code": "column", "passed": True}]},
    )
    assert incomplete.status_code == 422

    submitted = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-round-1", "site_notes": "现场完成", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    )
    assert submitted.status_code == 201, submitted.text
    round_id = submitted.json()["id"]
    assert submitted.json()["required_approvals"] == 2

    # 非冻结名单内的责任人不能签署
    outsider = client.post(
        f"/api/temple/operations/acceptance_rounds/{round_id}/sign",
        json={"signatory_code": "visitor", "decision": "approved"},
    )
    assert outsider.status_code == 409

    final = approve_round(client, round_id, WOOD_TEMPLATE["signatories"])
    assert final["result"] == "approved"
    assert final["active_approvals"] == 2
    wood_stage = next(item for item in client.get(f"/api/temple/operations/restoration_targets/{target_id}/workmanship").json()["stages"] if item["code"] == "timber-frame")
    assert wood_stage["state"] == "approved"
    assert wood_stage["latest_round"]["round_no"] == 1

    # 木构通过后瓦作可施工
    detail = client.get(f"/api/temple/operations/restoration_targets/{target_id}/workmanship").json()
    assert detail["constructible_stages"] == ["tile-roof"]

    # 已通过工序不能重复提交
    again = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-again", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    )
    assert again.status_code == 409
    assert campaign_id


def test_duplicate_submission_and_parallel_signatures_are_deterministic(client):
    _, target_id = prepare_campaign(client)
    payload = {"submitter": "foreman", "submission_key": "dup-key", "check_results": all_checks_passed(WOOD_TEMPLATE)}
    first = client.post(f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit", json=payload)
    replay = client.post(f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit", json=payload)
    assert first.status_code == 201 and replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["duplicate"] is True

    changed = {**payload, "submission_key": "dup-key", "site_notes": "内容被篡改"}
    conflict = client.post(f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit", json=changed)
    assert conflict.status_code == 409

    round_id = first.json()["id"]
    sign = {"signatory_code": "heritage-officer", "decision": "approved"}
    one = client.post(f"/api/temple/operations/acceptance_rounds/{round_id}/sign", json=sign)
    two = client.post(f"/api/temple/operations/acceptance_rounds/{round_id}/sign", json=sign)
    assert one.status_code == two.status_code == 200
    assert two.json()["duplicate"] is True

    contradictory = {**sign, "decision": "rejected"}
    rejected = client.post(f"/api/temple/operations/acceptance_rounds/{round_id}/sign", json=contradictory)
    assert rejected.status_code == 409

    errors: list[Exception] = []

    def worker() -> None:
        try:
            service = WorkmanshipAcceptanceService(get_connection())
            service.sign_acceptance(round_id, {"signatory_code": "master-carpenter", "decision": "approved", "comment": ""})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    rows = get_connection().execute(
        "SELECT COUNT(*) FROM acceptance_signatures WHERE acceptance_round_id=? AND signatory_code='master-carpenter' AND state='active'",
        (round_id,),
    ).fetchone()[0]
    assert rows == 1
    final = client.get(f"/api/temple/operations/acceptance_rounds/{round_id}").json()
    assert final["result"] == "approved"


def test_rejection_creates_new_round_and_keeps_history(client):
    _, target_id = prepare_campaign(client)
    round_id = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-r1", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    ).json()["id"]
    reject = client.post(
        f"/api/temple/operations/acceptance_rounds/{round_id}/sign",
        json={"signatory_code": "heritage-officer", "decision": "rejected", "comment": "榫卯松动"},
    )
    assert reject.status_code == 200
    assert reject.json()["result"] == "rejected"
    # 轮次结束后其他责任人不能补签
    late = client.post(
        f"/api/temple/operations/acceptance_rounds/{round_id}/sign",
        json={"signatory_code": "master-carpenter", "decision": "approved"},
    )
    assert late.status_code == 409

    # 返工后第二轮提交，历史轮次保留
    round_two = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-r2", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    )
    assert round_two.status_code == 201
    assert round_two.json()["round_no"] == 2
    final = approve_round(client, round_two.json()["id"], WOOD_TEMPLATE["signatories"])
    assert final["result"] == "approved"

    detail = client.get(f"/api/temple/operations/restoration_targets/{target_id}/workmanship").json()
    wood = next(item for item in detail["stages"] if item["code"] == "timber-frame")
    assert wood["rounds_count"] == 2
    timeline_types = [event["event_type"] for event in detail["timeline"]]
    assert timeline_types[0] == "site_submitted"
    assert "round_rejected" in timeline_types
    assert timeline_types[-1] == "round_approved"


def test_revoke_signature_keeps_reason_and_history(client):
    _, target_id = prepare_campaign(client)
    round_id = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-revoke", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    ).json()["id"]
    client.post(
        f"/api/temple/operations/acceptance_rounds/{round_id}/sign",
        json={"signatory_code": "heritage-officer", "decision": "approved"},
    )
    revoke = client.post(
        f"/api/temple/operations/acceptance_rounds/{round_id}/revoke",
        json={"signatory_code": "heritage-officer", "revoked_by": "director", "reason": "发现检查记录存疑"},
    )
    assert revoke.status_code == 200
    round_detail = revoke.json()
    signature = next(item for item in round_detail["signatures"] if item["signatory_code"] == "heritage-officer")
    assert signature["state"] == "revoked"
    assert signature["revocations"][0]["reason"] == "发现检查记录存疑"
    assert round_detail["active_approvals"] == 0

    # 撤回理由不能为空
    bad_revoke = client.post(
        f"/api/temple/operations/acceptance_rounds/{round_id}/revoke",
        json={"signatory_code": "heritage-officer", "revoked_by": "director", "reason": "x"},
    )
    assert bad_revoke.status_code == 422
    # 没有有效签署可撤回
    missing = client.post(
        f"/api/temple/operations/acceptance_rounds/{round_id}/revoke",
        json={"signatory_code": "master-carpenter", "revoked_by": "director", "reason": "误操作撤回"},
    )
    assert missing.status_code == 404
    # 撤回后可以重新签署，原记录仍保留
    resign = client.post(
        f"/api/temple/operations/acceptance_rounds/{round_id}/sign",
        json={"signatory_code": "heritage-officer", "decision": "approved", "comment": "复核通过"},
    )
    assert resign.status_code == 200
    rows = get_connection().execute(
        "SELECT COUNT(*) FROM acceptance_signatures WHERE acceptance_round_id=? AND signatory_code='heritage-officer'",
        (round_id,),
    ).fetchone()[0]
    assert rows == 2
    revocations = get_connection().execute("SELECT COUNT(*) FROM acceptance_signature_revocations").fetchone()[0]
    assert revocations == 1


def test_critical_defect_blocks_later_stage_and_rectification_loop(client):
    _, target_id = prepare_campaign(client)
    round_id = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-defect", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    ).json()["id"]
    approve_round(client, round_id, WOOD_TEMPLATE["signatories"])

    # 木构验收后登记严重缺陷（梁架内部糟朽）
    defect = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/defects?stage_code=timber-frame",
        json={"title": "梁架内部糟朽", "detail": "东次间五架梁", "severity": "critical", "reporter": "heritage-officer"},
    )
    assert defect.status_code == 201
    defect_id = defect.json()["id"]

    detail = client.get(f"/api/temple/operations/restoration_targets/{target_id}/workmanship").json()
    assert detail["blocked_by_critical_defect"] is True
    assert detail["constructible_stages"] == []
    assert [item["code"] for item in detail["open_defects"]] == [defect.json()["code"]]

    # 严重缺陷未闭环，后续瓦作不能提交
    blocked = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/tile-roof/submit",
        json={"submitter": "foreman", "submission_key": "tile-blocked", "check_results": [{"code": "tile", "passed": True}]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["defect_code"] == defect.json()["code"]

    # 整改复验驳回 → 仍未闭环
    rectification = client.post(
        f"/api/temple/operations/defects/{defect_id}/rectifications",
        json={"rectification_key": "rect-1", "action": "局部填补", "evidence": ["photo-1"], "rectified_by": "carpenter-a"},
    )
    assert rectification.status_code == 201
    rectification_id = rectification.json()["id"]
    review_reject = client.post(
        f"/api/temple/operations/rectifications/{rectification_id}/review",
        json={"accepted": False, "comment": "糟朽未清除", "reviewer": "heritage-officer"},
    )
    assert review_reject.status_code == 200
    assert review_reject.json()["defect"]["state"] == "rectifying"
    assert get_connection().execute("SELECT state FROM workmanship_defects WHERE id=?", (defect_id,)).fetchone()["state"] == "rectifying"

    # 重复整改键返回同一记录；不同内容冲突
    replay = client.post(
        f"/api/temple/operations/defects/{defect_id}/rectifications",
        json={"rectification_key": "rect-1", "action": "局部填补", "evidence": ["photo-1"], "rectified_by": "carpenter-a"},
    )
    assert replay.status_code == 201 and replay.json()["duplicate"] is True
    changed = client.post(
        f"/api/temple/operations/defects/{defect_id}/rectifications",
        json={"rectification_key": "rect-1", "action": "更换梁架", "evidence": [], "rectified_by": "carpenter-a"},
    )
    assert changed.status_code == 409
    # 已评定记录不能重复复验
    repeat_review = client.post(
        f"/api/temple/operations/rectifications/{rectification_id}/review",
        json={"accepted": True, "reviewer": "heritage-officer"},
    )
    assert repeat_review.status_code == 409

    # 第二次整改复验通过 → 缺陷闭环，瓦作解除阻塞
    second = client.post(
        f"/api/temple/operations/defects/{defect_id}/rectifications",
        json={"rectification_key": "rect-2", "action": "剔除糟朽并墩接", "evidence": ["photo-2", "photo-3"], "rectified_by": "carpenter-a"},
    )
    assert second.json()["round_no"] == 2
    review_accept = client.post(
        f"/api/temple/operations/rectifications/{second.json()['id']}/review",
        json={"accepted": True, "comment": "复验合格", "reviewer": "heritage-officer"},
    )
    assert review_accept.status_code == 200
    assert review_accept.json()["defect"]["state"] == "closed"

    detail = client.get(f"/api/temple/operations/restoration_targets/{target_id}/workmanship").json()
    assert detail["blocked_by_critical_defect"] is False
    assert detail["constructible_stages"] == ["tile-roof"]
    assert detail["open_defects"] == []


def test_minor_defect_does_not_block_submission(client):
    _, target_id = prepare_campaign(client)
    client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/defects",
        json={"title": "轻微划痕", "severity": "minor", "reporter": "officer"},
    )
    response = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-minor", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    )
    assert response.status_code == 201


def test_target_and_campaign_pause_block_work_and_are_idempotent(client):
    _, target_id = prepare_campaign(client)
    pause = client.post(f"/api/temple/operations/restoration_targets/{target_id}/pause", json={"actor": "operator", "reason": "雨季暂停"})
    assert pause.status_code == 200
    pause_again = client.post(f"/api/temple/operations/restoration_targets/{target_id}/pause", json={"actor": "operator", "reason": "重复暂停"})
    assert pause_again.status_code == 200
    assert pause_again.json().get("already_paused") is True

    blocked = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "while-paused", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    )
    assert blocked.status_code == 409

    resume = client.post(f"/api/temple/operations/restoration_targets/{target_id}/resume", json={"actor": "operator", "reason": "天气转好"})
    assert resume.status_code == 200
    assert resume.json()["target_state"] == "active"
    resume_again = client.post(f"/api/temple/operations/restoration_targets/{target_id}/resume", json={"actor": "operator", "reason": "重复恢复"})
    assert resume_again.json().get("already_active") is True

    submitted = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "after-resume", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    )
    assert submitted.status_code == 201

    # 活动级暂停同样阻止签署
    round_id = submitted.json()["id"]
    campaign_id = client.get(f"/api/temple/operations/restoration_targets/{target_id}/workmanship").json()["restoration_campaign_id"]
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/pause", json={"actor": "operator", "reason": "活动暂停"})
    sign = client.post(
        f"/api/temple/operations/acceptance_rounds/{round_id}/sign",
        json={"signatory_code": "heritage-officer", "decision": "approved"},
    )
    assert sign.status_code == 409
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "operator", "reason": "活动恢复"})
    sign_again = client.post(
        f"/api/temple/operations/acceptance_rounds/{round_id}/sign",
        json={"signatory_code": "heritage-officer", "decision": "approved"},
    )
    assert sign_again.status_code == 200


def test_campaign_complete_requires_closed_stages_and_defects(client):
    campaign_id, target_id = prepare_campaign(client)
    blocked = client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/complete", json={"actor": "operator", "reason": "申请完工"})
    assert blocked.status_code == 409
    blockers = blocked.json()["error"]["context"]["blocked_targets"]
    assert blockers[0]["pending_stages"] == ["timber-frame", "tile-roof"]

    wood_round = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-final", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    ).json()["id"]
    approve_round(client, wood_round, WOOD_TEMPLATE["signatories"])
    tile_round = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/tile-roof/submit",
        json={"submitter": "foreman", "submission_key": "tile-final", "check_results": all_checks_passed(TILE_TEMPLATE)},
    ).json()["id"]
    approve_round(client, tile_round, TILE_TEMPLATE["signatories"])

    completed = client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/complete", json={"actor": "operator", "reason": "全部验收通过"})
    assert completed.status_code == 200
    assert completed.json()["state"] == "completed"

    # 已结束活动上的现场提交被拒绝
    after = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "after-complete", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    )
    assert after.status_code == 409


def test_detail_shows_full_chain_from_first_submission(client):
    _, target_id = prepare_campaign(client)
    wood_round = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-chain", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    ).json()["id"]
    client.post(
        f"/api/temple/operations/acceptance_rounds/{wood_round}/sign",
        json={"signatory_code": "heritage-officer", "decision": "rejected", "comment": "需要返工整修"},
    )
    wood_round_two = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/timber-frame/submit",
        json={"submitter": "foreman", "submission_key": "wood-chain-2", "check_results": all_checks_passed(WOOD_TEMPLATE)},
    ).json()["id"]
    approve_round(client, wood_round_two, WOOD_TEMPLATE["signatories"])
    tile_round = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/stages/tile-roof/submit",
        json={"submitter": "foreman", "submission_key": "tile-chain", "check_results": all_checks_passed(TILE_TEMPLATE)},
    ).json()["id"]
    defect = client.post(
        f"/api/temple/operations/restoration_targets/{target_id}/defects",
        json={"title": "瓦面色差", "severity": "minor", "reporter": "officer"},
    ).json()
    rect = client.post(
        f"/api/temple/operations/defects/{defect['id']}/rectifications",
        json={"rectification_key": "rect-chain", "action": "更换瓦件", "rectified_by": "tiler"},
    ).json()["id"]
    client.post(
        f"/api/temple/operations/rectifications/{rect}/review",
        json={"accepted": True, "reviewer": "heritage-officer"},
    )
    approve_round(client, tile_round, TILE_TEMPLATE["signatories"])

    detail = client.get(f"/api/temple/operations/restoration_targets/{target_id}/workmanship").json()
    chain = [event["event_type"] for event in detail["timeline"]]
    assert chain[0] == "site_submitted"  # 首次提交
    assert chain.count("site_submitted") == 3
    assert "round_rejected" in chain
    assert "defect_reported" in chain
    assert "rectification_accepted" in chain
    assert chain[-1] == "round_approved"  # 最终通过
    assert all(stage["state"] == "approved" for stage in detail["stages"])
    assert detail["constructible_stages"] == []
