from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import request_fingerprint
from app.database import get_connection, transaction
from app.temple.schema import ensure_temple_schema

# 严重（critical）缺陷未闭环前，验收不能通过、后续工序保持冻结。
BLOCKING_DEFECT_SEVERITIES = ("critical",)
CRAFT_TYPES = ("woodwork", "tiling", "painting", "sculpture", "stonework", "other")
PHASE_ORDER = ("blocked", "ready", "in_progress", "submitted", "accepted", "rejected")
RUNNING_PHASE_STATES = ("ready", "in_progress")
OPEN_DEFECT_STATES = ("open", "rectify_submitted")


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


class PhaseWorkflowService:
    """古建修缮工序验收闭环：模板、依赖、现场提交、多人签署、缺陷整改与复验。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 模板

    def create_phase_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        check_items = self._check_items(payload.get("check_items"))
        required_roles = self._required_roles(payload.get("required_roles"))
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO work_phase_templates(code,name,craft_type,check_items_json,required_roles_json,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        payload["code"], payload["name"], payload["craft_type"],
                        json.dumps(check_items, ensure_ascii=False),
                        json.dumps(required_roles, ensure_ascii=False),
                        payload["actor"], now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("工序模板编码已存在") from exc
            return self.phase_template_detail(cursor.lastrowid, connection)

    def list_phase_templates(self, state: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM work_phase_templates"
        params: list[Any] = []
        if state:
            sql += " WHERE state=?"
            params.append(state)
        sql += " ORDER BY craft_type,code,id"
        return [self._template(row) for row in self.connection.execute(sql, params).fetchall()]

    def retire_phase_template(self, template_id: int, actor: str, reason: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM work_phase_templates WHERE id=?", (template_id,)).fetchone()
            if row is None:
                raise NotFoundError("工序模板不存在")
            if row["state"] != "retired":
                now = to_storage(self.clock.now())
                connection.execute("UPDATE work_phase_templates SET state='retired',updated_at=? WHERE id=?", (now, template_id))
                self._event(connection, row_campaign=None, event_type="template_retired", actor=actor,
                            detail={"template_code": row["code"], "reason": reason}, now=now)
            return self.phase_template_detail(template_id, connection)

    def phase_template_detail(self, template_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM work_phase_templates WHERE id=?", (template_id,)).fetchone()
        if row is None:
            raise NotFoundError("工序模板不存在")
        return self._template(row)

    # ------------------------------------------------------------------ 工序

    def add_campaign_phases(self, restoration_campaign_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            campaign = connection.execute("SELECT * FROM restoration_campaigns WHERE id=?", (restoration_campaign_id,)).fetchone()
            if campaign is None:
                raise NotFoundError("修缮活动不存在")
            if campaign["state"] not in ("draft", "scheduled"):
                raise ConflictError("修缮活动启动后工序结构冻结，不能再新增工序")
            phases = payload["phases"]
            codes = [item["code"] for item in phases]
            if len(codes) != len(set(codes)):
                raise ValidationError("同一活动内工序编码不能重复")
            existing = {
                row["code"] for row in connection.execute(
                    "SELECT code FROM restoration_phases WHERE restoration_campaign_id=?",
                    (restoration_campaign_id,),
                ).fetchall()
            }
            duplicated = existing & set(codes)
            if duplicated:
                raise ConflictError("工序编码已存在", context={"codes": sorted(duplicated)})
            base_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence_no),0) FROM restoration_phases WHERE restoration_campaign_id=?",
                    (restoration_campaign_id,),
                ).fetchone()[0]
            )
            known = existing | set(codes)
            now = to_storage(self.clock.now())
            created: list[int] = []
            for offset, item in enumerate(phases, start=1):
                depends_on = item.get("depends_on") or []
                unknown = [code for code in depends_on if code not in known]
                if unknown:
                    raise ValidationError(f"工序 {item['code']} 依赖了不存在的工序：{','.join(unknown)}")
                if item["code"] in depends_on:
                    raise ValidationError(f"工序 {item['code']} 不能依赖自身")
                check_items, required_roles, craft_type, name = self._template_snapshot(connection, item)
                cursor = connection.execute(
                    "INSERT INTO restoration_phases(restoration_campaign_id,template_id,code,name,craft_type,sequence_no,"
                    "depends_on_json,check_items_json,required_roles_json,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        restoration_campaign_id, item.get("template_id"), item["code"], name, craft_type,
                        base_sequence + offset, json.dumps(depends_on),
                        json.dumps(check_items, ensure_ascii=False),
                        json.dumps(required_roles, ensure_ascii=False),
                        "ready" if not depends_on else "blocked", now, now,
                    ),
                )
                created.append(cursor.lastrowid)
            self._detect_cycle(connection, restoration_campaign_id)
            for phase_id in created:
                self._event(connection, campaign["id"], "phase_added", payload["actor"],
                            {"phase_id": phase_id}, now)
            return self.workflow_detail(restoration_campaign_id, connection)

    def workflow_detail(self, restoration_campaign_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        campaign = connection.execute("SELECT * FROM restoration_campaigns WHERE id=?", (restoration_campaign_id,)).fetchone()
        if campaign is None:
            raise NotFoundError("修缮活动不存在")
        phase_rows = connection.execute(
            "SELECT * FROM restoration_phases WHERE restoration_campaign_id=? ORDER BY sequence_no,id",
            (restoration_campaign_id,),
        ).fetchall()
        phases = [self._phase_detail(connection, row) for row in phase_rows]
        open_defect_rows = connection.execute(
            "SELECT * FROM phase_defects WHERE phase_id IN ("
            "SELECT id FROM restoration_phases WHERE restoration_campaign_id=?) AND state IN ('open','rectify_submitted') ORDER BY id",
            (restoration_campaign_id,),
        ).fetchall() if phase_rows else []
        open_defects = [self._defect(row) for row in open_defect_rows]
        blocking = [item for item in open_defects if item["severity"] in BLOCKING_DEFECT_SEVERITIES]
        if campaign["state"] == "running":
            constructible = [phase["code"] for phase in phases if phase["state"] in RUNNING_PHASE_STATES]
            blocked_reason = None
        else:
            constructible = []
            blocked_reason = "项目已暂停或结束，当前不允许施工" if campaign["state"] != "draft" else "项目尚未启动"
        chain = [self._event_row(row) for row in connection.execute(
            "SELECT * FROM phase_workflow_events WHERE restoration_campaign_id=? ORDER BY id",
            (restoration_campaign_id,),
        ).fetchall()]
        return {
            "campaign": {"id": campaign["id"], "code": campaign["code"], "name": campaign["name"], "state": campaign["state"]},
            "phases": phases,
            "constructible_phases": constructible,
            "constructible_blocked_reason": blocked_reason,
            "open_defects": open_defects,
            "blocking_defects": blocking,
            "chain": chain,
        }

    # ------------------------------------------------------------------ 执行

    def open_phase(self, phase_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            phase = self._phase(connection, phase_id)
            self._require_running_campaign(connection, phase["restoration_campaign_id"])
            if phase["state"] == "in_progress":
                return self._phase_detail(connection, phase)
            if phase["state"] != "ready":
                raise ConflictError("只有已就绪工序可以开工", context={"state": phase["state"]})
            connection.execute(
                "UPDATE restoration_phases SET state='in_progress',opened_at=COALESCE(opened_at,?),version=version+1,updated_at=? WHERE id=?",
                (now, now, phase_id),
            )
            self._event(connection, phase["restoration_campaign_id"], "phase_opened", actor,
                        {"phase_code": phase["code"], "reason": reason}, now, phase_id=phase_id)
            return self._phase_detail(connection, self._phase(connection, phase_id))

    def submit_phase(self, phase_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            phase = self._phase(connection, phase_id)
            digest = request_fingerprint({"submission_key": payload["submission_key"], "notes": payload.get("notes", ""), "by": payload["submitted_by"]})
            existing = connection.execute(
                "SELECT * FROM phase_submissions WHERE phase_id=? AND submission_key=?",
                (phase_id, payload["submission_key"]),
            ).fetchone()
            if existing is not None:
                if existing["payload_digest"] != digest:
                    raise ConflictError("相同提交键对应了不同的现场提交内容")
                return self._submission_detail(connection, existing)
            self._require_running_campaign(connection, phase["restoration_campaign_id"])
            if phase["state"] not in ("in_progress",):
                raise ConflictError("只有施工中的工序可以现场提交验收", context={"state": phase["state"]})
            check_items = _loads(phase["check_items_json"], [])
            required_roles = _loads(phase["required_roles_json"], [])
            round_no = int(connection.execute(
                "SELECT COALESCE(MAX(round_no),0)+1 FROM phase_submissions WHERE phase_id=?", (phase_id,)).fetchone()[0])
            cursor = connection.execute(
                "INSERT INTO phase_submissions(phase_id,round_no,submission_key,check_items_snapshot_json,approvers_snapshot_json,"
                "submitted_by,submitted_at,notes,payload_digest,state) VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
                (
                    phase_id, round_no, payload["submission_key"],
                    json.dumps(check_items, ensure_ascii=False),
                    json.dumps(required_roles, ensure_ascii=False),
                    payload["submitted_by"], now, payload.get("notes", ""), digest,
                ),
            )
            connection.execute(
                "UPDATE restoration_phases SET state='submitted',current_submission_id=?,submitted_at=?,version=version+1,updated_at=? WHERE id=?",
                (cursor.lastrowid, now, now, phase_id),
            )
            self._event(connection, phase["restoration_campaign_id"], "phase_submitted", payload["submitted_by"],
                        {"phase_code": phase["code"], "round_no": round_no, "submission_id": cursor.lastrowid,
                         "frozen_check_items": check_items, "frozen_approvers": required_roles},
                        now, phase_id=phase_id, submission_id=cursor.lastrowid)
            return self._submission_detail(connection, self._submission(connection, cursor.lastrowid))

    def record_approval(self, submission_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            submission = self._submission(connection, submission_id)
            phase = self._phase(connection, submission["phase_id"])
            self._require_running_campaign(connection, phase["restoration_campaign_id"])
            approver_key = payload["approver_key"]
            approvers = _loads(submission["approvers_snapshot_json"], [])
            if approver_key not in approvers:
                raise ValidationError("该责任人不在本次验收冻结的签署名单中")
            if submission["state"] != "pending":
                raise ConflictError("本次验收已有结论，不能再补充签署", context={"submission_state": submission["state"]})
            decision = payload["decision"]
            latest = self._latest_approvals(connection, submission_id)
            current = latest.get(approver_key)
            if current is not None and current["decision"] == decision and decision in ("approved", "rejected"):
                # 重复请求：同一责任人、同一结论，返回确定结果，不追加新记录。
                return self._submission_detail(connection, submission)
            if current is not None and current["decision"] in ("approved", "rejected") and current["decision"] != decision:
                # 改变已发表的结论必须先带理由撤回，不能直接覆盖。
                raise ConflictError("改变验收结论前必须先撤回原签署并填写理由", context={"current_decision": current["decision"]})
            defects = payload.get("defects") or []
            if decision == "rejected" and not defects and not payload.get("comment"):
                raise ValidationError("驳回必须填写意见或登记缺陷")
            cursor = connection.execute(
                "INSERT INTO phase_approvals(submission_id,approver_key,approver_role,decision,comment,withdrawn_reason,decided_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (submission_id, approver_key, payload.get("approver_role", approver_key), decision,
                 payload.get("comment", ""), "", now),
            )
            self._event(connection, phase["restoration_campaign_id"], "approval_recorded", approver_key,
                        {"phase_code": phase["code"], "round_no": submission["round_no"], "decision": decision,
                         "comment": payload.get("comment", "")}, now,
                        phase_id=phase["id"], submission_id=submission_id, approval_id=cursor.lastrowid)
            for defect in defects:
                self._insert_defect(connection, phase, submission, defect, approver_key, now)
            self._recompute_submission(connection, submission_id, now)
            return self._submission_detail(connection, self._submission(connection, submission_id))

    def withdraw_approval(self, submission_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        reason = payload.get("reason", "")
        if len(reason.strip()) < 2:
            raise ValidationError("撤回签署必须填写理由")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            submission = self._submission(connection, submission_id)
            phase = self._phase(connection, submission["phase_id"])
            self._require_running_campaign(connection, phase["restoration_campaign_id"])
            if submission["state"] != "pending":
                raise ConflictError("验收结论已冻结，不能撤回签署")
            latest = self._latest_approvals(connection, submission_id)
            current = latest.get(payload["approver_key"])
            if current is None or current["decision"] == "withdrawn":
                raise ConflictError("该责任人当前没有可撤回的签署")
            cursor = connection.execute(
                "INSERT INTO phase_approvals(submission_id,approver_key,approver_role,decision,comment,withdrawn_reason,decided_at) "
                "VALUES(?,?,?, 'withdrawn' ,?,?,?)",
                (submission_id, payload["approver_key"], current["approver_role"], "", reason, now),
            )
            self._event(connection, phase["restoration_campaign_id"], "approval_withdrawn", payload["approver_key"],
                        {"phase_code": phase["code"], "round_no": submission["round_no"], "reason": reason,
                         "previous_decision": current["decision"]},
                        now, phase_id=phase["id"], submission_id=submission_id, approval_id=cursor.lastrowid)
            self._recompute_submission(connection, submission_id, now)
            return self._submission_detail(connection, submission)

    def raise_defect(self, submission_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            submission = self._submission(connection, submission_id)
            phase = self._phase(connection, submission["phase_id"])
            self._require_running_campaign(connection, phase["restoration_campaign_id"])
            if submission["state"] != "pending":
                raise ConflictError("只有待结论的验收可以登记缺陷")
            row = self._insert_defect(connection, phase, submission, payload, payload["raised_by"], now)
            return self._defect(row)

    def submit_rectification(self, defect_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            defect = self._defect_row(connection, defect_id)
            phase = self._phase(connection, defect["phase_id"])
            self._require_running_campaign(connection, phase["restoration_campaign_id"])
            if defect["state"] == "rectify_submitted":
                return self._defect(defect)  # 重复提交：返回已登记的整改，确定结果。
            if defect["state"] != "open":
                raise ConflictError("已闭环缺陷不能再登记整改")
            connection.execute(
                "UPDATE phase_defects SET state='rectify_submitted',rectification_note=?,rectified_by=?,rectified_at=?,"
                "reverify_result='',reverify_note='',reverified_by=NULL,reverified_at=NULL,version=version+1 WHERE id=?",
                (payload["note"], payload["actor"], now, defect_id),
            )
            self._event(connection, phase["restoration_campaign_id"], "defect_rectified", payload["actor"],
                        {"phase_code": phase["code"], "defect_code": defect["defect_code"], "note": payload["note"]},
                        now, phase_id=phase["id"], submission_id=defect["submission_id"], defect_id=defect_id)
            return self._defect(self._defect_row(connection, defect_id))

    def reverify_defect(self, defect_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        result = payload["result"]
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            defect = self._defect_row(connection, defect_id)
            phase = self._phase(connection, defect["phase_id"])
            campaign = self._require_running_campaign(connection, phase["restoration_campaign_id"])
            del campaign
            if defect["state"] == "closed" and defect["reverify_result"] == result:
                return self._defect(defect)  # 重复复验：幂等返回闭环结果。
            if defect["state"] != "rectify_submitted":
                raise ConflictError("只有已提交整改的缺陷可以复验", context={"state": defect["state"]})
            if result == "passed":
                connection.execute(
                    "UPDATE phase_defects SET state='closed',reverify_result='passed',reverify_note=?,reverified_by=?,"
                    "reverified_at=?,closed_at=?,version=version+1 WHERE id=?",
                    (payload.get("note", ""), payload["actor"], now, now, defect_id),
                )
            else:
                connection.execute(
                    "UPDATE phase_defects SET state='open',reverify_result='failed',reverify_note=?,reverified_by=?,"
                    "reverified_at=?,closed_at=NULL,version=version+1 WHERE id=?",
                    (payload.get("note", ""), payload["actor"], now, defect_id),
                )
            self._event(connection, phase["restoration_campaign_id"], "defect_reverified", payload["actor"],
                        {"phase_code": phase["code"], "defect_code": defect["defect_code"], "result": result,
                         "note": payload.get("note", ""), "severity": defect["severity"]},
                        now, phase_id=phase["id"], submission_id=defect["submission_id"], defect_id=defect_id)
            updated = self._defect_row(connection, defect_id)
            # 严重缺陷闭环后，若全员已签署，尝试推进当前轮验收结论。
            # 缺陷可能挂在历史驳回轮上，因此以工序的当前提交为准。
            if result == "passed":
                current = connection.execute(
                    "SELECT * FROM phase_submissions WHERE id=(SELECT current_submission_id FROM restoration_phases WHERE id=?)",
                    (phase["id"],),
                ).fetchone()
                if current is not None and current["state"] == "pending":
                    self._recompute_submission(connection, current["id"], now)
            return self._defect(updated)

    # ------------------------------------------------------------------ 内部

    def _recompute_submission(self, connection: sqlite3.Connection, submission_id: int, now: str) -> None:
        submission = self._submission(connection, submission_id)
        phase = self._phase(connection, submission["phase_id"])
        required = _loads(submission["approvers_snapshot_json"], [])
        latest = self._latest_approvals(connection, submission_id)
        approved_keys = {key for key, row in latest.items() if row["decision"] == "approved"}
        rejected_keys = {key for key, row in latest.items() if row["decision"] == "rejected"}
        blocking = connection.execute(
            "SELECT 1 FROM phase_defects WHERE phase_id=? AND state<> 'closed' AND severity IN ('critical') LIMIT 1",
            (phase["id"],),
        ).fetchone()
        if rejected_keys:
            outcome = "rejected"
        elif blocking is not None:
            outcome = "pending"  # 全员签署也不能通过，保持待结论。
        elif required and all(key in approved_keys for key in required):
            outcome = "accepted"
        else:
            outcome = "pending"
        if outcome == submission["state"]:
            return
        if outcome == "accepted":
            connection.execute("UPDATE phase_submissions SET state='accepted',decided_at=? WHERE id=?", (now, submission_id))
            connection.execute(
                "UPDATE restoration_phases SET state='accepted',accepted_at=?,version=version+1,updated_at=? WHERE id=?",
                (now, now, phase["id"]),
            )
            self._unblock_successors(connection, phase, now)
            self._event(connection, phase["restoration_campaign_id"], "submission_accepted", "system",
                        {"phase_code": phase["code"], "round_no": submission["round_no"]}, now,
                        phase_id=phase["id"], submission_id=submission_id)
        elif outcome == "rejected":
            connection.execute("UPDATE phase_submissions SET state='rejected',decided_at=? WHERE id=?", (now, submission_id))
            connection.execute(
                "UPDATE restoration_phases SET state='in_progress',version=version+1,updated_at=? WHERE id=?",
                (now, phase["id"]),
            )
            self._event(connection, phase["restoration_campaign_id"], "submission_rejected",
                         sorted(rejected_keys)[0],
                         {"phase_code": phase["code"], "round_no": submission["round_no"], "rejected_by": sorted(rejected_keys)},
                         now, phase_id=phase["id"], submission_id=submission_id)

    def _unblock_successors(self, connection: sqlite3.Connection, accepted_phase: sqlite3.Row, now: str) -> None:
        campaign_id = accepted_phase["restoration_campaign_id"]
        progressed = True
        while progressed:
            progressed = False
            accepted_codes = {
                row["code"] for row in connection.execute(
                    "SELECT code FROM restoration_phases WHERE restoration_campaign_id=? AND state='accepted'",
                    (campaign_id,),
                ).fetchall()
            }
            ready = connection.execute(
                "SELECT * FROM restoration_phases WHERE restoration_campaign_id=? AND state='blocked' ORDER BY sequence_no,id",
                (campaign_id,),
            ).fetchall()
            for row in ready:
                depends_on = _loads(row["depends_on_json"], [])
                if depends_on and all(code in accepted_codes for code in depends_on):
                    connection.execute(
                        "UPDATE restoration_phases SET state='ready',version=version+1,updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    self._event(connection, campaign_id, "phase_ready", "system",
                                {"phase_code": row["code"], "unblocked_by": accepted_phase["code"]}, now, phase_id=row["id"])
                    progressed = True
                    break  # 重新计算 accepted 集合后继续解锁其下游。

    def _insert_defect(self, connection: sqlite3.Connection, phase: sqlite3.Row, submission: sqlite3.Row,
                       defect: dict[str, Any], actor: str, now: str) -> sqlite3.Row:
        severity = defect["severity"]
        if severity not in ("minor", "major", "critical"):
            raise ValidationError("缺陷等级无效")
        try:
            cursor = connection.execute(
                "INSERT INTO phase_defects(phase_id,submission_id,defect_code,description,severity,raised_by,raised_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (phase["id"], submission["id"], defect["defect_code"], defect["description"], severity, actor, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("工序内缺陷编码已存在") from exc
        self._event(connection, phase["restoration_campaign_id"], "defect_raised", actor,
                    {"phase_code": phase["code"], "round_no": submission["round_no"],
                     "defect_code": defect["defect_code"], "severity": severity, "description": defect["description"]},
                    now, phase_id=phase["id"], submission_id=submission["id"], defect_id=cursor.lastrowid)
        return self._defect_row(connection, cursor.lastrowid)

    @staticmethod
    def _latest_approvals(connection: sqlite3.Connection, submission_id: int) -> dict[str, sqlite3.Row]:
        latest: dict[str, sqlite3.Row] = {}
        for row in connection.execute(
            "SELECT * FROM phase_approvals WHERE submission_id=? ORDER BY id", (submission_id,)
        ).fetchall():
            latest[row["approver_key"]] = row
        return latest

    @staticmethod
    def _require_running_campaign(connection: sqlite3.Connection, campaign_id: int) -> sqlite3.Row:
        campaign = connection.execute("SELECT * FROM restoration_campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            raise NotFoundError("修缮活动不存在")
        if campaign["state"] != "running":
            raise ConflictError("修缮活动当前不处于运行状态，工序操作被冻结", context={"campaign_state": campaign["state"]})
        return campaign

    def _template_snapshot(self, connection: sqlite3.Connection, item: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str, str]:
        template_id = item.get("template_id")
        if template_id is not None:
            template = connection.execute("SELECT * FROM work_phase_templates WHERE id=?", (template_id,)).fetchone()
            if template is None:
                raise NotFoundError(f"工序模板不存在：{template_id}")
            if template["state"] == "retired":
                raise ConflictError(f"工序模板已退役：{template['code']}")
            return (
                _loads(template["check_items_json"], []),
                _loads(template["required_roles_json"], []),
                template["craft_type"],
                item.get("name") or template["name"],
            )
        craft_type = item.get("craft_type")
        if craft_type not in CRAFT_TYPES:
            raise ValidationError(f"工序 {item['code']} 缺少有效的工序类型")
        return self._check_items(item.get("check_items")), self._required_roles(item.get("required_roles")), craft_type, item["name"]

    @staticmethod
    def _detect_cycle(connection: sqlite3.Connection, campaign_id: int) -> None:
        phases = {
            row["code"]: _loads(row["depends_on_json"], [])
            for row in connection.execute(
                "SELECT code,depends_on_json FROM restoration_phases WHERE restoration_campaign_id=?",
                (campaign_id,),
            ).fetchall()
        }

        def visit(node: str, stack: set[str]) -> None:
            if node in stack:
                raise ValidationError(f"工序依赖存在环：{' -> '.join(list(stack) + [node])}")
            stack.add(node)
            for dependency in phases.get(node, []):
                visit(dependency, stack)
            stack.remove(node)

        for code in phases:
            visit(code, set())

    @staticmethod
    def _check_items(raw: Any) -> list[dict[str, Any]]:
        if not raw:
            raise ValidationError("工序至少需要一个检查项")
        items = list(raw)
        codes: list[str] = []
        normalized: list[dict[str, Any]] = []
        for item in items:
            code = str(item.get("code", "")).strip()
            name = str(item.get("name", "")).strip()
            if not code or not name:
                raise ValidationError("检查项必须包含编码和名称")
            codes.append(code)
            normalized.append({"code": code, "name": name})
        if len(codes) != len(set(codes)):
            raise ValidationError("检查项编码不能重复")
        return normalized

    @staticmethod
    def _required_roles(raw: Any) -> list[str]:
        if not raw:
            raise ValidationError("工序至少需要一个验收责任人角色")
        roles = [str(item).strip() for item in raw]
        if any(not role for role in roles) or len(roles) != len(set(roles)):
            raise ValidationError("验收责任人角色不能为空且不能重复")
        return roles

    @staticmethod
    def _phase(connection: sqlite3.Connection, phase_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM restoration_phases WHERE id=?", (phase_id,)).fetchone()
        if row is None:
            raise NotFoundError("修缮工序不存在")
        return row

    @staticmethod
    def _submission(connection: sqlite3.Connection, submission_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM phase_submissions WHERE id=?", (submission_id,)).fetchone()
        if row is None:
            raise NotFoundError("工序验收提交不存在")
        return row

    @staticmethod
    def _defect_row(connection: sqlite3.Connection, defect_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM phase_defects WHERE id=?", (defect_id,)).fetchone()
        if row is None:
            raise NotFoundError("工序缺陷不存在")
        return row

    def _phase_detail(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["depends_on"] = _loads(result.pop("depends_on_json"), [])
        result["check_items_frozen"] = _loads(result.pop("check_items_json"), [])
        result["required_roles_frozen"] = _loads(result.pop("required_roles_json"), [])
        submissions = connection.execute(
            "SELECT * FROM phase_submissions WHERE phase_id=? ORDER BY round_no,id", (row["id"],)).fetchall()
        result["submissions"] = [self._submission_detail(connection, item) for item in submissions]
        return result

    def _submission_detail(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["check_items_snapshot"] = _loads(result.pop("check_items_snapshot_json"), [])
        result["approvers_snapshot"] = _loads(result.pop("approvers_snapshot_json"), [])
        result["approvals"] = [dict(item) for item in connection.execute(
            "SELECT id,approver_key,approver_role,decision,comment,withdrawn_reason,decided_at "
            "FROM phase_approvals WHERE submission_id=? ORDER BY id", (row["id"],)).fetchall()]
        result["defects"] = [self._defect(item) for item in connection.execute(
            "SELECT * FROM phase_defects WHERE submission_id=? ORDER BY id", (row["id"],)).fetchall()]
        latest = self._latest_approvals(connection, row["id"])
        result["approved_by"] = sorted(key for key, item in latest.items() if item["decision"] == "approved")
        result["rejected_by"] = sorted(key for key, item in latest.items() if item["decision"] == "rejected")
        result["pending_approvers"] = [
            role for role in result["approvers_snapshot"]
            if role not in result["approved_by"] and role not in result["rejected_by"]
        ]
        return result

    @staticmethod
    def _defect(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _template(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["check_items"] = _loads(result.pop("check_items_json"), [])
        result["required_roles"] = _loads(result.pop("required_roles_json"), [])
        return result

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["detail"] = json.loads(result.pop("detail_json"))
        return result

    @staticmethod
    def _event(connection: sqlite3.Connection, row_campaign: int | None, event_type: str, actor: str,
               detail: dict[str, Any], now: str, *, phase_id: int | None = None,
               submission_id: int | None = None, defect_id: int | None = None,
               approval_id: int | None = None) -> None:
        connection.execute(
            "INSERT INTO phase_workflow_events(restoration_campaign_id,phase_id,submission_id,defect_id,approval_id,"
            "event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (row_campaign, phase_id, submission_id, defect_id, approval_id, event_type, actor,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )
