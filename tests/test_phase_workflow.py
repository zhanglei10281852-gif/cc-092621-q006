from __future__ import annotations

from app.temple.phase_workflow import PhaseWorkflowService
from app.temple.rules import DEFAULT_RULES


def prepare_campaign(client, *, campaign_code="dabei-ge-restoration"):
    client.post(
        "/api/temple/temples",
        json={"code": "dabei-temple", "name": "大悲寺", "temple_type": "heritage", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 100, "ventilation_capacity": 1000},
    )
    client.post(
        "/api/temple/temples/dabei-temple/halls",
        json={"code": "main-hall", "name": "大雄宝殿", "visit_order": 1, "expected_visit_seconds": 600, "ventilation_capacity": 400},
    )
    policy = client.post("/api/temple/temples/dabei-temple/policies", json={"rules": DEFAULT_RULES, "actor": "tests"}).json()
    client.post(f"/api/temple/policies/{policy['id']}/publish", json={"actor": "tests", "effective_from": "2026-09-01T00:00:00Z"})
    campaign = client.post(
        "/api/temple/operations/restoration_campaigns",
        json={"temple_code": "dabei-temple", "safety_policy_id": policy["id"], "code": campaign_code, "name": "大雄宝殿古建修缮", "strategy": "halls", "hall_codes": ["main-hall"], "actor": "planner"},
    ).json()
    return campaign["id"]


WOOD_TEMPLATE = {
    "code": "wood-structure",
    "name": "木构工序",
    "craft_type": "woodwork",
    "check_items": [
        {"code": "beam-splicing", "name": "梁枋榫卯拼接"},
        {"code": "column-foundation", "name": "柱础复位"},
    ],
    "required_roles": ["heritage_officer", "craft_master"],
    "actor": "planner",
}

TILE_PAYLOAD = {
    "code": "tiling",
    "name": "瓦作工序",
    "craft_type": "tiling",
    "depends_on": ["wood"],
    "check_items": [{"code": "tile-lap", "name": "瓦垄搭接"}],
    "required_roles": ["heritage_officer"],
}

PAINT_PAYLOAD = {
    "code": "painting",
    "name": "彩绘工序",
    "craft_type": "painting",
    "depends_on": ["tiling"],
    "check_items": [{"code": "pigment-layer", "name": "颜料层附着"}],
    "required_roles": ["heritage_officer", "craft_master"],
}


def setup_three_phase_campaign(client, campaign_id):
    template = client.post("/api/temple/operations/phase_templates", json=WOOD_TEMPLATE)
    assert template.status_code == 201, template.text
    template_id = template.json()["id"]
    created = client.post(
        f"/api/temple/operations/restoration_campaigns/{campaign_id}/phases",
        json={"phases": [
            {"template_id": template_id, "code": "wood"},
            TILE_PAYLOAD,
            PAINT_PAYLOAD,
        ], "actor": "planner"},
    )
    assert created.status_code == 201, created.text
    phases = {item["code"]: item for item in created.json()["phases"]}
    assert phases["wood"]["state"] == "ready"
    assert phases["tiling"]["state"] == "blocked"
    assert phases["painting"]["state"] == "blocked"
    # 模板内容在配置时冻结到工序。
    assert [item["code"] for item in phases["wood"]["check_items_frozen"]] == ["beam-splicing", "column-foundation"]
    return phases


def approve_everyone(client, submission_id, roles, *, defects=None):
    for role in roles:
        response = client.post(
            f"/api/temple/operations/submissions/{submission_id}/approvals",
            json={"approver_key": role, "approver_role": role, "decision": "approved", "defects": defects or []},
        )
        assert response.status_code == 200, response.text
    return response.json()


def test_phase_template_duplicate_conflict(client):
    first = client.post("/api/temple/operations/phase_templates", json=WOOD_TEMPLATE)
    assert first.status_code == 201
    second = client.post("/api/temple/operations/phase_templates", json=WOOD_TEMPLATE)
    assert second.status_code == 409


def test_full_phase_chain_with_parallel_signing(client):
    campaign_id = prepare_campaign(client)
    phases = setup_three_phase_campaign(client, campaign_id)
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "planner", "reason": "进场"})

    detail = client.get(f"/api/temple/operations/restoration_campaigns/{campaign_id}/workflow")
    assert detail.json()["constructible_phases"] == ["wood"]

    wood_phase_id = phases["wood"]["id"]
    opened = client.post(f"/api/temple/operations/phases/{wood_phase_id}/open", json={"actor": "foreman", "reason": "木构进场"})
    assert opened.status_code == 200
    assert opened.json()["state"] == "in_progress"

    submitted = client.post(
        f"/api/temple/operations/phases/{wood_phase_id}/submit",
        json={"submission_key": "wood-submit-001", "submitted_by": "foreman", "notes": "木构完工"},
    )
    assert submitted.status_code == 200, submitted.text
    submission = submitted.json()
    assert submission["round_no"] == 1
    # 检查项与责任人在提交时冻结。
    assert [item["code"] for item in submission["check_items_snapshot"]] == ["beam-splicing", "column-foundation"]
    assert submission["approvers_snapshot"] == ["heritage_officer", "craft_master"]

    # 名单外责任人不能签署。
    outsider = client.post(
        f"/api/temple/operations/submissions/{submission['id']}/approvals",
        json={"approver_key": "tourist", "decision": "approved"},
    )
    assert outsider.status_code == 422

    first = client.post(
        f"/api/temple/operations/submissions/{submission['id']}/approvals",
        json={"approver_key": "heritage_officer", "decision": "approved"},
    )
    assert first.json()["state"] == "pending"
    assert first.json()["pending_approvers"] == ["craft_master"]

    # 重复签署请求返回确定结果，不新增记录。
    repeated = client.post(
        f"/api/temple/operations/submissions/{submission['id']}/approvals",
        json={"approver_key": "heritage_officer", "decision": "approved"},
    )
    assert repeated.status_code == 200
    assert len(repeated.json()["approvals"]) == 1

    final = approve_everyone(client, submission["id"], ["craft_master"])
    assert final["state"] == "accepted"

    detail = client.get(f"/api/temple/operations/restoration_campaigns/{campaign_id}/workflow").json()
    assert {item["code"]: item["state"] for item in detail["phases"]}["tiling"] == "ready"
    assert detail["constructible_phases"] == ["tiling"]

    # 走完瓦作、彩绘，验证依赖逐级解锁。
    for code, roles in (("tiling", ["heritage_officer"]), ("painting", ["heritage_officer", "craft_master"])):
        phase = next(item for item in detail["phases"] if item["code"] == code)
        client.post(f"/api/temple/operations/phases/{phase['id']}/open", json={"actor": "foreman", "reason": f"{code}开工"})
        sub = client.post(
            f"/api/temple/operations/phases/{phase['id']}/submit",
            json={"submission_key": f"{code}-submit-001", "submitted_by": "foreman"},
        ).json()
        approve_everyone(client, sub["id"], roles)
        detail = client.get(f"/api/temple/operations/restoration_campaigns/{campaign_id}/workflow").json()

    assert {item["code"]: item["state"] for item in detail["phases"]} == {"wood": "accepted", "tiling": "accepted", "painting": "accepted"}
    assert detail["constructible_phases"] == []
    assert detail["open_defects"] == []
    event_types = [event["event_type"] for event in detail["chain"]]
    assert event_types[0] == "phase_added"
    assert "submission_accepted" in event_types
    # 链路从首次提交到最终通过完整可追溯。
    assert event_types.count("phase_submitted") == 3

    # 全部验收闭环后才允许结束修缮活动。
    completed = client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/complete", json={"actor": "planner", "reason": "修缮完成"})
    assert completed.status_code == 200


def test_critical_defect_blocks_acceptance_and_downstream_until_closed(client):
    campaign_id = prepare_campaign(client, campaign_code="critical-defect-case")
    phases = setup_three_phase_campaign(client, campaign_id)
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "planner", "reason": "进场"})
    wood_id = phases["wood"]["id"]
    client.post(f"/api/temple/operations/phases/{wood_id}/open", json={"actor": "foreman", "reason": "开工"})
    submission = client.post(
        f"/api/temple/operations/phases/{wood_id}/submit",
        json={"submission_key": "wood-submit-critical", "submitted_by": "foreman"},
    ).json()

    # 第一位签署人通过但登记严重缺陷；即便全员通过也不得放行。
    officer = client.post(
        f"/api/temple/operations/submissions/{submission['id']}/approvals",
        json={"approver_key": "heritage_officer", "decision": "approved", "defects": [
            {"defect_code": "crack-beam", "description": "主梁出现贯通裂隙", "severity": "critical"}
        ]},
    )
    assert officer.status_code == 200, officer.text
    defect_id = officer.json()["defects"][0]["id"]

    master = client.post(
        f"/api/temple/operations/submissions/{submission['id']}/approvals",
        json={"approver_key": "craft_master", "decision": "approved"},
    )
    assert master.json()["state"] == "pending"
    detail = client.get(f"/api/temple/operations/restoration_campaigns/{campaign_id}/workflow").json()
    assert [item["defect_code"] for item in detail["blocking_defects"]] == ["crack-beam"]
    assert {item["code"]: item["state"] for item in detail["phases"]}["tiling"] == "blocked"

    # 只提交整改、尚未复验通过，仍然阻断。
    rectified = client.post(f"/api/temple/operations/defects/{defect_id}/rectification", json={"actor": "carpenter", "note": "墩接补强并加铁箍"})
    assert rectified.status_code == 200
    detail = client.get(f"/api/temple/operations/restoration_campaigns/{campaign_id}/workflow").json()
    assert len(detail["blocking_defects"]) == 1

    # 复验失败，缺陷重新打开。
    failed = client.post(f"/api/temple/operations/defects/{defect_id}/reverify", json={"actor": "heritage_officer", "result": "failed", "note": "裂隙仍扩展"})
    assert failed.json()["state"] == "open"
    assert failed.json()["reverify_result"] == "failed"

    # 再次整改并复验通过，严重缺陷闭环，验收自动通过、下游解锁。
    client.post(f"/api/temple/operations/defects/{defect_id}/rectification", json={"actor": "carpenter", "note": "更换梁枋"})
    passed = client.post(f"/api/temple/operations/defects/{defect_id}/reverify", json={"actor": "heritage_officer", "result": "passed", "note": "复验合格"})
    assert passed.json()["state"] == "closed"

    detail = client.get(f"/api/temple/operations/restoration_campaigns/{campaign_id}/workflow").json()
    wood = next(item for item in detail["phases"] if item["code"] == "wood")
    assert wood["state"] == "accepted"
    assert {item["code"]: item["state"] for item in detail["phases"]}["tiling"] == "ready"
    assert detail["open_defects"] == []
    events = [event["event_type"] for event in detail["chain"]]
    assert events.count("defect_rectified") == 2
    assert events.count("defect_reverified") == 2


def test_rejection_reopens_phase_and_second_round_passes(client):
    campaign_id = prepare_campaign(client, campaign_code="rework-case")
    phases = setup_three_phase_campaign(client, campaign_id)
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "planner", "reason": "进场"})
    wood_id = phases["wood"]["id"]
    client.post(f"/api/temple/operations/phases/{wood_id}/open", json={"actor": "foreman", "reason": "开工"})
    first = client.post(
        f"/api/temple/operations/phases/{wood_id}/submit",
        json={"submission_key": "wood-submit-r1", "submitted_by": "foreman"},
    ).json()

    rejected = client.post(
        f"/api/temple/operations/submissions/{first['id']}/approvals",
        json={"approver_key": "craft_master", "decision": "rejected", "comment": "榫卯间隙超标", "defects": [
            {"defect_code": "loose-tenon", "description": "榫头松动", "severity": "major"}
        ]},
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"
    phase = client.post(f"/api/temple/operations/phases/{wood_id}/open", json={"actor": "foreman", "reason": "无需操作"})
    assert phase.status_code == 200  # 驳回后工序回到施工中，重复开工幂等返回。
    assert phase.json()["state"] == "in_progress"

    # 首轮记录不可篡改：再次提交产生新一轮。
    second = client.post(
        f"/api/temple/operations/phases/{wood_id}/submit",
        json={"submission_key": "wood-submit-r2", "submitted_by": "foreman", "notes": "返工完成"},
    )
    assert second.json()["round_no"] == 2
    assert second.json()["check_items_snapshot"][0]["code"] == "beam-splicing"

    detail = client.get(f"/api/temple/operations/restoration_campaigns/{campaign_id}/workflow").json()
    wood = next(item for item in detail["phases"] if item["code"] == "wood")
    assert len(wood["submissions"]) == 2
    assert wood["submissions"][0]["state"] == "rejected"


def test_withdraw_requires_reason_and_keeps_history(client):
    campaign_id = prepare_campaign(client, campaign_code="withdraw-case")
    phases = setup_three_phase_campaign(client, campaign_id)
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "planner", "reason": "进场"})
    wood_id = phases["wood"]["id"]
    client.post(f"/api/temple/operations/phases/{wood_id}/open", json={"actor": "foreman", "reason": "开工"})
    submission = client.post(
        f"/api/temple/operations/phases/{wood_id}/submit",
        json={"submission_key": "wood-submit-withdraw", "submitted_by": "foreman"},
    ).json()
    client.post(
        f"/api/temple/operations/submissions/{submission['id']}/approvals",
        json={"approver_key": "heritage_officer", "decision": "approved"},
    )
    missing_reason = client.post(
        f"/api/temple/operations/submissions/{submission['id']}/withdraw",
        json={"approver_key": "heritage_officer", "reason": ""},
    )
    assert missing_reason.status_code == 422
    withdrawn = client.post(
        f"/api/temple/operations/submissions/{submission['id']}/withdraw",
        json={"approver_key": "heritage_officer", "reason": "发现新监测数据需要复核"},
    )
    assert withdrawn.status_code == 200
    approvals = withdrawn.json()["approvals"]
    assert [item["decision"] for item in approvals] == ["approved", "withdrawn"]
    assert approvals[1]["withdrawn_reason"] == "发现新监测数据需要复核"
    assert "heritage_officer" not in withdrawn.json()["approved_by"]

    # 撤回后允许重新签署。
    resigned = client.post(
        f"/api/temple/operations/submissions/{submission['id']}/approvals",
        json={"approver_key": "heritage_officer", "decision": "approved"},
    )
    assert len(resigned.json()["approvals"]) == 3


def test_duplicate_submission_key_is_idempotent_but_content_conflicts(client):
    campaign_id = prepare_campaign(client, campaign_code="idempotent-case")
    phases = setup_three_phase_campaign(client, campaign_id)
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "planner", "reason": "进场"})
    wood_id = phases["wood"]["id"]
    client.post(f"/api/temple/operations/phases/{wood_id}/open", json={"actor": "foreman", "reason": "开工"})
    payload = {"submission_key": "wood-dup-key", "submitted_by": "foreman", "notes": "首次"}
    first = client.post(f"/api/temple/operations/phases/{wood_id}/submit", json=payload)
    assert first.status_code == 200
    repeated = client.post(f"/api/temple/operations/phases/{wood_id}/submit", json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["id"] == first.json()["id"]
    conflict = client.post(
        f"/api/temple/operations/phases/{wood_id}/submit",
        json={"submission_key": "wood-dup-key", "submitted_by": "foreman", "notes": "内容被篡改"},
    )
    assert conflict.status_code == 409


def test_paused_campaign_freezes_phase_operations(client):
    campaign_id = prepare_campaign(client, campaign_code="paused-case")
    phases = setup_three_phase_campaign(client, campaign_id)
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "planner", "reason": "进场"})
    wood_id = phases["wood"]["id"]
    client.post(f"/api/temple/operations/phases/{wood_id}/open", json={"actor": "foreman", "reason": "开工"})
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/pause", json={"actor": "planner", "reason": "雨季暂停"})

    detail = client.get(f"/api/temple/operations/restoration_campaigns/{campaign_id}/workflow").json()
    assert detail["constructible_phases"] == []
    assert detail["constructible_blocked_reason"] is not None

    blocked_submit = client.post(
        f"/api/temple/operations/phases/{wood_id}/submit",
        json={"submission_key": "wood-paused", "submitted_by": "foreman"},
    )
    assert blocked_submit.status_code == 409

    # 恢复后提交成功。
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "planner", "reason": "复工"})
    submitted = client.post(
        f"/api/temple/operations/phases/{wood_id}/submit",
        json={"submission_key": "wood-paused", "submitted_by": "foreman"},
    )
    assert submitted.status_code == 200


def test_campaign_cannot_complete_with_open_defects(client):
    campaign_id = prepare_campaign(client, campaign_code="open-defect-complete")
    phases = setup_three_phase_campaign(client, campaign_id)
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "planner", "reason": "进场"})
    wood_id = phases["wood"]["id"]
    client.post(f"/api/temple/operations/phases/{wood_id}/open", json={"actor": "foreman", "reason": "开工"})
    submission = client.post(
        f"/api/temple/operations/phases/{wood_id}/submit",
        json={"submission_key": "wood-open-defect", "submitted_by": "foreman"},
    ).json()
    client.post(
        f"/api/temple/operations/submissions/{submission['id']}/approvals",
        json={"approver_key": "craft_master", "decision": "rejected", "comment": "需要返工", "defects": [
            {"defect_code": "gap", "description": "缝隙过大", "severity": "minor"}
        ]},
    )
    denied = client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/complete", json={"actor": "planner", "reason": "提前结束"})
    assert denied.status_code == 409
    assert denied.json()["error"]["context"]["open_defects"] == 1


def test_dependency_cycle_rejected(client):
    campaign_id = prepare_campaign(client, campaign_code="cycle-case")
    response = client.post(
        f"/api/temple/operations/restoration_campaigns/{campaign_id}/phases",
        json={"phases": [
            {"code": "a", "name": "甲", "craft_type": "woodwork", "depends_on": ["b"], "check_items": [{"code": "x", "name": "项"}], "required_roles": ["r"]},
            {"code": "b", "name": "乙", "craft_type": "tiling", "depends_on": ["a"], "check_items": [{"code": "y", "name": "项"}], "required_roles": ["r"]},
        ], "actor": "planner"},
    )
    assert response.status_code == 422


def test_phases_frozen_after_campaign_start(client):
    campaign_id = prepare_campaign(client, campaign_code="frozen-case")
    setup_three_phase_campaign(client, campaign_id)
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "planner", "reason": "进场"})
    response = client.post(
        f"/api/temple/operations/restoration_campaigns/{campaign_id}/phases",
        json={"phases": [{"code": "late", "name": "追加工序", "craft_type": "other", "check_items": [{"code": "z", "name": "项"}], "required_roles": ["r"]}], "actor": "planner"},
    )
    assert response.status_code == 409


def test_parallel_signatures_are_serialized_with_a_deterministic_outcome(client):
    import threading

    campaign_id = prepare_campaign(client, campaign_code="parallel-case")
    phases = setup_three_phase_campaign(client, campaign_id)
    client.post(f"/api/temple/operations/restoration_campaigns/{campaign_id}/start", json={"actor": "planner", "reason": "进场"})
    wood_id = phases["wood"]["id"]
    client.post(f"/api/temple/operations/phases/{wood_id}/open", json={"actor": "foreman", "reason": "开工"})
    submission = client.post(
        f"/api/temple/operations/phases/{wood_id}/submit",
        json={"submission_key": "wood-parallel-submit", "submitted_by": "foreman"},
    ).json()

    from app.database import get_connection
    outcomes: dict[str, object] = {}
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def sign(role: str) -> None:
        service = PhaseWorkflowService()  # 每个线程独立连接，真正竞争写锁。
        barrier.wait()
        try:
            outcomes[role] = service.record_approval(submission["id"], {
                "approver_key": role, "approver_role": role, "decision": "approved", "defects": [],
            })
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=sign, args=(role,)) for role in ("heritage_officer", "craft_master")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors

    final = client.get(f"/api/temple/operations/restoration_campaigns/{campaign_id}/workflow").json()
    wood = next(item for item in final["phases"] if item["code"] == "wood")
    assert wood["state"] == "accepted"
    states = {value["state"] for value in outcomes.values()}
    assert states <= {"pending", "accepted"}
    assert "accepted" in states
