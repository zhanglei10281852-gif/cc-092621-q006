from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import request_fingerprint
from app.database import get_connection, transaction
from app.temple.operations import TempleRestorationService
from app.temple.repository import TempleRepository
from app.temple.schema import ensure_temple_schema

BLOCKING_DEFECT_SEVERITY = "critical"


class WorkmanshipAcceptanceService:
    """古建修缮工序模板、现场提交、多人签署、缺陷整改与复验闭环。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = TempleRepository(self.connection)

    # ------------------------------------------------------------------ 模板

    def create_workmanship_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        campaign = self._campaign(payload["restoration_campaign_id"])
        if campaign["temple_id"] != temple["id"]:
            raise ValidationError("修缮活动不属于目标寺院")
        if campaign["state"] in {"completed", "cancelled"}:
            raise ConflictError("已结束的修缮活动不能再增加工序模板")
        checks = [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in payload["checks"]]
        signatories = [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in payload["signatories"]]
        depends_on = list(payload.get("depends_on") or [])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            existing_codes = {
                row["code"]
                for row in connection.execute(
                    "SELECT code FROM workmanship_templates WHERE restoration_campaign_id=? AND code<>?",
                    (campaign["id"], payload["code"]),
                ).fetchall()
            }
            missing = [code for code in depends_on if code not in existing_codes]
            if missing:
                raise ValidationError("依赖工序不存在：" + ",".join(missing))
            try:
                cursor = connection.execute(
                    "INSERT INTO workmanship_templates(restoration_campaign_id,code,name,sequence_no,checks_json,signatories_json,depends_on_json,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        campaign["id"], payload["code"], payload["name"], payload["sequence_no"],
                        json.dumps(checks, ensure_ascii=False, sort_keys=True),
                        json.dumps(signatories, ensure_ascii=False, sort_keys=True),
                        json.dumps(depends_on, ensure_ascii=False),
                        payload["actor"], now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("工序编码或顺序在该修缮活动中已存在") from exc
            self._event(connection, "workmanship_template", cursor.lastrowid, "created", payload["actor"], {"code": payload["code"]}, now)
            return self.workmanship_template_detail(cursor.lastrowid, connection)

    def list_workmanship_templates(self, restoration_campaign_id: int) -> list[dict[str, Any]]:
        self._campaign(restoration_campaign_id)
        rows = self.connection.execute(
            "SELECT * FROM workmanship_templates WHERE restoration_campaign_id=? ORDER BY sequence_no,id",
            (restoration_campaign_id,),
        ).fetchall()
        return [self._template_dict(row) for row in rows]

    def workmanship_template_detail(self, template_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM workmanship_templates WHERE id=?", (template_id,)).fetchone()
        if row is None:
            raise NotFoundError("工序模板不存在")
        return self._template_dict(row)

    # ------------------------------------------------------------ 实例化/图

    def materialize_campaign(self, restoration_campaign_id: int, connection: sqlite3.Connection | None = None) -> None:
        """把活动下的工序模板按目标冻结为工序实例，幂等。"""
        def _run(conn: sqlite3.Connection) -> None:
            templates = conn.execute(
                "SELECT * FROM workmanship_templates WHERE restoration_campaign_id=? ORDER BY sequence_no,id",
                (restoration_campaign_id,),
            ).fetchall()
            self._validate_template_graph(templates)
            targets = conn.execute(
                "SELECT id FROM restoration_targets WHERE restoration_campaign_id=?",
                (restoration_campaign_id,),
            ).fetchall()
            for target in targets:
                existing = {
                    row["code"]
                    for row in conn.execute(
                        "SELECT code FROM workmanship_stage_instances WHERE restoration_target_id=?",
                        (target["id"],),
                    ).fetchall()
                }
                for template in templates:
                    if template["code"] in existing:
                        continue
                    conn.execute(
                        "INSERT INTO workmanship_stage_instances(restoration_target_id,workmanship_template_id,code,name,sequence_no,template_version,checks_json,signatories_json,depends_on_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            target["id"], template["id"], template["code"], template["name"], template["sequence_no"],
                            template["version"], template["checks_json"], template["signatories_json"], template["depends_on_json"],
                        ),
                    )

        if connection is not None:
            _run(connection)
            return
        with transaction(immediate=True) as locked:
            _run(locked)

    @staticmethod
    def _validate_template_graph(templates: list[sqlite3.Row]) -> None:
        by_code = {row["code"]: row for row in templates}
        for row in templates:
            for dependency in json.loads(row["depends_on_json"]):
                if dependency not in by_code:
                    raise ValidationError(f"工序 {row['code']} 依赖了不存在的工序：{dependency}")
        # DFS 环检测
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(code: str, stack: list[str]) -> None:
            if code in visited:
                return
            if code in visiting:
                raise ValidationError("工序依赖存在循环：" + " -> ".join([*stack, code]))
            visiting.add(code)
            for dependency in json.loads(by_code[code]["depends_on_json"]):
                visit(dependency, [*stack, code])
            visiting.discard(code)
            visited.add(code)

        for code in by_code:
            visit(code, [])

    # ------------------------------------------------------------- 现场提交

    def submit_site_work(self, restoration_target_id: int, stage_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        content = {
            "submitter": payload["submitter"],
            "site_notes": payload.get("site_notes", ""),
            "check_results": payload.get("check_results") or [],
        }
        digest = request_fingerprint(content)
        duplicate = self.connection.execute(
            "SELECT r.* FROM acceptance_rounds r JOIN workmanship_stage_instances s ON s.id=r.stage_instance_id "
            "WHERE s.restoration_target_id=? AND r.submission_key=?",
            (restoration_target_id, payload["submission_key"]),
        ).fetchone()
        if duplicate is not None:
            if duplicate["payload_digest"] != digest:
                raise ConflictError("相同提交键对应了不同现场内容")
            return self.acceptance_round_detail(duplicate["id"], duplicate_replayed=True)

        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            winner = connection.execute(
                "SELECT r.* FROM acceptance_rounds r JOIN workmanship_stage_instances s ON s.id=r.stage_instance_id "
                "WHERE s.restoration_target_id=? AND r.submission_key=?",
                (restoration_target_id, payload["submission_key"]),
            ).fetchone()
            if winner is not None:
                if winner["payload_digest"] != digest:
                    raise ConflictError("相同提交键对应了不同现场内容")
                return self.acceptance_round_detail(winner["id"], connection, duplicate_replayed=True)
            target = self._target(connection, restoration_target_id)
            campaign = self._campaign(target["restoration_campaign_id"], connection)
            self._require_active_work(campaign, target)
            self.materialize_campaign(campaign["id"], connection)
            stage = self._stage(connection, restoration_target_id, stage_code)
            if stage["state"] == "submitted":
                raise ConflictError("该工序已有待签署的现场提交")
            if stage["state"] == "approved":
                raise ConflictError("该工序已验收通过，不能重复提交")
            self._require_dependencies_approved(connection, target["id"], stage)
            blocking = self._blocking_defect(connection, target["id"])
            if blocking is not None:
                raise ConflictError("目标存在未闭环的严重缺陷，后续工序不能提交", context={"defect_code": blocking["code"]})
            check_results = self._validated_check_results(stage, payload.get("check_results") or [])
            cursor = connection.execute(
                "UPDATE workmanship_stage_instances SET state='submitted',started_at=COALESCE(started_at,?),first_submitted_at=COALESCE(first_submitted_at,?),version=version+1 "
                "WHERE id=? AND state IN ('pending','in_progress')",
                (now, now, stage["id"]),
            )
            if cursor.rowcount == 0:
                raise ConflictError("该工序已有待签署的现场提交")
            round_no = self._next_round_no(connection, stage["id"])
            try:
                round_cursor = connection.execute(
                    "INSERT INTO acceptance_rounds(stage_instance_id,round_no,submission_key,payload_digest,submitted_by,site_notes,check_results_json,checks_json,signatories_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        stage["id"], round_no, payload["submission_key"], digest, payload["submitter"],
                        payload.get("site_notes", ""), json.dumps(check_results, ensure_ascii=False, sort_keys=True),
                        stage["checks_json"], stage["signatories_json"], now,
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                winner = self.connection.execute("SELECT * FROM acceptance_rounds WHERE submission_key=?", (payload["submission_key"],)).fetchone()
                if winner is None:
                    raise ConflictError("现场提交冲突，请重试")
                if winner["payload_digest"] != digest:
                    raise ConflictError("相同提交键对应了不同现场内容")
                return self.acceptance_round_detail(winner["id"], duplicate_replayed=True)
            self._event(connection, "acceptance_round", round_cursor.lastrowid, "site_submitted", payload["submitter"], {"stage_code": stage_code, "round_no": round_no}, now)
            return self.acceptance_round_detail(round_cursor.lastrowid, connection, duplicate_replayed=False)

    # ----------------------------------------------------------------- 签署

    def sign_acceptance(self, acceptance_round_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            round_row = self._round(connection, acceptance_round_id)
            stage = connection.execute("SELECT * FROM workmanship_stage_instances WHERE id=?", (round_row["stage_instance_id"],)).fetchone()
            target = self._target(connection, stage["restoration_target_id"])
            campaign = self._campaign(target["restoration_campaign_id"], connection)
            frozen_signatories = json.loads(round_row["signatories_json"])
            signatory = self._frozen_signatory(frozen_signatories, payload["signatory_code"])

            existing = connection.execute(
                "SELECT * FROM acceptance_signatures WHERE acceptance_round_id=? AND signatory_code=? AND state='active' ORDER BY id DESC LIMIT 1",
                (acceptance_round_id, payload["signatory_code"]),
            ).fetchone()
            if existing is not None:
                if existing["decision"] != payload["decision"]:
                    raise ConflictError("重复签署与原决定不一致，如需变更请先撤回原签署")
                return self.acceptance_round_detail(acceptance_round_id, connection, duplicate_replayed=True)
            if round_row["result"] != "pending":
                raise ConflictError("本轮验收已经结束，不能补充签署")
            self._require_active_work(campaign, target)

            try:
                signature_cursor = connection.execute(
                    "INSERT INTO acceptance_signatures(acceptance_round_id,signatory_code,signatory_name,role_name,decision,comment,signed_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        acceptance_round_id, signatory["code"],
                        payload.get("signatory_name") or signatory["name"],
                        signatory.get("role_name", ""), payload["decision"], payload.get("comment", ""), now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                winner = connection.execute(
                    "SELECT * FROM acceptance_signatures WHERE acceptance_round_id=? AND signatory_code=? AND state='active' ORDER BY id DESC LIMIT 1",
                    (acceptance_round_id, payload["signatory_code"]),
                ).fetchone()
                if winner is not None and winner["decision"] == payload["decision"]:
                    return self.acceptance_round_detail(acceptance_round_id, connection, duplicate_replayed=True)
                raise ConflictError("该责任人已经完成签署") from exc

            self._event(
                connection, "acceptance_round", acceptance_round_id, "signature_recorded",
                payload["signatory_code"], {"decision": payload["decision"], "signature_id": signature_cursor.lastrowid}, now,
            )

            if payload["decision"] == "rejected":
                closed = connection.execute(
                    "UPDATE acceptance_rounds SET result='rejected',closed_at=? WHERE id=? AND result='pending'",
                    (now, acceptance_round_id),
                )
                if closed.rowcount == 1:
                    connection.execute("UPDATE workmanship_stage_instances SET state='in_progress',version=version+1 WHERE id=?", (stage["id"],))
                    self._event(connection, "acceptance_round", acceptance_round_id, "round_rejected", payload["signatory_code"], {"comment": payload.get("comment", "")}, now)
                return self.acceptance_round_detail(acceptance_round_id, connection)

            approvals = connection.execute(
                "SELECT COUNT(*) FROM acceptance_signatures WHERE acceptance_round_id=? AND state='active' AND decision='approved'",
                (acceptance_round_id,),
            ).fetchone()[0]
            if approvals >= len(frozen_signatories):
                closed = connection.execute(
                    "UPDATE acceptance_rounds SET result='approved',closed_at=? WHERE id=? AND result='pending'",
                    (now, acceptance_round_id),
                )
                if closed.rowcount == 1:
                    connection.execute(
                        "UPDATE workmanship_stage_instances SET state='approved',approved_at=?,version=version+1 WHERE id=?",
                        (now, stage["id"]),
                    )
                    self._event(connection, "acceptance_round", acceptance_round_id, "round_approved", payload["signatory_code"], {"required": len(frozen_signatories)}, now)
            return self.acceptance_round_detail(acceptance_round_id, connection)

    def revoke_signature(self, acceptance_round_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            round_row = self._round(connection, acceptance_round_id)
            if round_row["result"] != "pending":
                raise ConflictError("本轮验收已经结束，签署记录不能撤回")
            signature = connection.execute(
                "SELECT * FROM acceptance_signatures WHERE acceptance_round_id=? AND signatory_code=? AND state='active' ORDER BY id DESC LIMIT 1",
                (acceptance_round_id, payload["signatory_code"]),
            ).fetchone()
            if signature is None:
                raise NotFoundError("该责任人没有可撤回的有效签署")
            connection.execute("UPDATE acceptance_signatures SET state='revoked' WHERE id=?", (signature["id"],))
            connection.execute(
                "INSERT INTO acceptance_signature_revocations(acceptance_signature_id,acceptance_round_id,signatory_code,revoked_by,reason,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (signature["id"], acceptance_round_id, payload["signatory_code"], payload["revoked_by"], payload["reason"], now),
            )
            self._event(
                connection, "acceptance_round", acceptance_round_id, "signature_revoked",
                payload["revoked_by"], {"signatory_code": payload["signatory_code"], "reason": payload["reason"], "signature_id": signature["id"]}, now,
            )
            return self.acceptance_round_detail(acceptance_round_id, connection)

    # ----------------------------------------------------------------- 缺陷

    def report_defect(self, restoration_target_id: int, payload: dict[str, Any], *, stage_code: str | None = None, acceptance_round_id: int | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            target = self._target(connection, restoration_target_id)
            stage_id = None
            if stage_code:
                stage = self._stage(connection, restoration_target_id, stage_code)
                stage_id = stage["id"]
            if acceptance_round_id is not None:
                round_row = self._round(connection, acceptance_round_id)
                linked_stage = connection.execute(
                    "SELECT id FROM workmanship_stage_instances WHERE id=? AND restoration_target_id=?",
                    (round_row["stage_instance_id"], restoration_target_id),
                ).fetchone()
                if linked_stage is None:
                    raise ValidationError("验收轮次不属于该修缮目标")
                stage_id = linked_stage["id"]
            sequence = connection.execute(
                "SELECT COUNT(*) FROM workmanship_defects WHERE restoration_target_id=?",
                (restoration_target_id,),
            ).fetchone()[0] + 1
            code = f"D{restoration_target_id}-{sequence:04d}"
            try:
                cursor = connection.execute(
                    "INSERT INTO workmanship_defects(restoration_target_id,stage_instance_id,acceptance_round_id,code,title,detail,severity,reported_by,opened_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        restoration_target_id, stage_id, acceptance_round_id, code, payload["title"],
                        payload.get("detail", ""), payload["severity"], payload["reporter"], now,
                    ),
                )
            except sqlite3.IntegrityError:
                next_id = connection.execute("SELECT COALESCE(MAX(id),0)+1 FROM workmanship_defects").fetchone()[0]
                code = f"D{restoration_target_id}-{next_id:04d}"
                cursor = connection.execute(
                    "INSERT INTO workmanship_defects(restoration_target_id,stage_instance_id,acceptance_round_id,code,title,detail,severity,reported_by,opened_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        restoration_target_id, stage_id, acceptance_round_id, code, payload["title"],
                        payload.get("detail", ""), payload["severity"], payload["reporter"], now,
                    ),
                )
            self._event(
                connection, "workmanship_defect", cursor.lastrowid, "defect_reported", payload["reporter"],
                {"code": code, "severity": payload["severity"], "stage_code": stage_code}, now,
            )
            return self.defect_detail(cursor.lastrowid, connection)

    def list_defects(self, restoration_target_id: int, *, open_only: bool = False) -> list[dict[str, Any]]:
        self._target(self.connection, restoration_target_id)
        sql = "SELECT * FROM workmanship_defects WHERE restoration_target_id=?"
        if open_only:
            sql += " AND state<>'closed'"
        sql += " ORDER BY CASE severity WHEN 'critical' THEN 3 WHEN 'major' THEN 2 ELSE 1 END DESC,id"
        rows = self.connection.execute(sql, (restoration_target_id,)).fetchall()
        return [self._defect_dict(row, self.connection) for row in rows]

    def defect_detail(self, defect_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM workmanship_defects WHERE id=?", (defect_id,)).fetchone()
        if row is None:
            raise NotFoundError("缺陷不存在")
        return self._defect_dict(row, connection)

    def start_rectification(self, defect_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            defect = self._defect(connection, defect_id)
            if defect["state"] == "closed":
                raise ConflictError("缺陷已闭环，不能再登记整改")
            if defect["state"] != "rectifying":
                connection.execute("UPDATE workmanship_defects SET state='rectifying',version=version+1 WHERE id=?", (defect_id,))
                self._event(connection, "workmanship_defect", defect_id, "rectification_started", actor, {}, now)
            return self.defect_detail(defect_id, connection)

    def submit_rectification(self, defect_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        content = {
            "action": payload["action"],
            "evidence": payload.get("evidence") or [],
            "rectified_by": payload["rectified_by"],
        }
        digest = request_fingerprint(content)
        duplicate = self.connection.execute(
            "SELECT * FROM workmanship_rectifications WHERE rectification_key=?",
            (payload["rectification_key"],),
        ).fetchone()
        if duplicate is not None:
            if duplicate["payload_digest"] != digest:
                raise ConflictError("相同整改键对应了不同整改内容")
            if duplicate["workmanship_defect_id"] != defect_id:
                raise ConflictError("整改键已用于其他缺陷")
            return self.rectification_detail(duplicate["id"], duplicate_replayed=True)

        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            winner = connection.execute(
                "SELECT * FROM workmanship_rectifications WHERE rectification_key=?",
                (payload["rectification_key"],),
            ).fetchone()
            if winner is not None:
                if winner["payload_digest"] != digest:
                    raise ConflictError("相同整改键对应了不同整改内容")
                if winner["workmanship_defect_id"] != defect_id:
                    raise ConflictError("整改键已用于其他缺陷")
                return self.rectification_detail(winner["id"], connection, duplicate_replayed=True)
            defect = self._defect(connection, defect_id)
            if defect["state"] == "closed":
                raise ConflictError("缺陷已闭环，不能再提交整改")
            target = self._target(connection, defect["restoration_target_id"])
            campaign = self._campaign(target["restoration_campaign_id"], connection)
            if campaign["state"] != "running" or target["state"] != "active":
                raise ConflictError("修缮活动或目标已暂停，不能提交整改")
            round_no = connection.execute(
                "SELECT COALESCE(MAX(round_no),0)+1 FROM workmanship_rectifications WHERE workmanship_defect_id=?",
                (defect_id,),
            ).fetchone()[0]
            try:
                cursor = connection.execute(
                    "INSERT INTO workmanship_rectifications(workmanship_defect_id,round_no,rectification_key,payload_digest,action,evidence_json,rectified_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        defect_id, round_no, payload["rectification_key"], digest, payload["action"],
                        json.dumps(payload.get("evidence") or [], ensure_ascii=False), payload["rectified_by"], now,
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                winner = self.connection.execute("SELECT * FROM workmanship_rectifications WHERE rectification_key=?", (payload["rectification_key"],)).fetchone()
                if winner is None:
                    raise ConflictError("整改提交冲突，请重试")
                if winner["payload_digest"] != digest:
                    raise ConflictError("相同整改键对应了不同整改内容")
                if winner["workmanship_defect_id"] != defect_id:
                    raise ConflictError("整改键已用于其他缺陷")
                return self.rectification_detail(winner["id"], duplicate_replayed=True)
            connection.execute("UPDATE workmanship_defects SET state='rectifying',version=version+1 WHERE id=? AND state<>'rectifying'", (defect_id,))
            self._event(connection, "workmanship_defect", defect_id, "rectification_submitted", payload["rectified_by"], {"round_no": round_no, "rectification_id": cursor.lastrowid}, now)
            return self.rectification_detail(cursor.lastrowid, connection)

    def review_rectification(self, rectification_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM workmanship_rectifications WHERE id=?", (rectification_id,)).fetchone()
            if row is None:
                raise NotFoundError("整改记录不存在")
            if row["result"] != "submitted":
                raise ConflictError("整改已经复验，不能重复评定")
            defect = self._defect(connection, row["workmanship_defect_id"])
            target = self._target(connection, defect["restoration_target_id"])
            campaign = self._campaign(target["restoration_campaign_id"], connection)
            if campaign["state"] != "running" or target["state"] != "active":
                raise ConflictError("修缮活动或目标已暂停，不能复验")
            if payload["accepted"]:
                connection.execute(
                    "UPDATE workmanship_rectifications SET result='accepted',review_comment=?,reviewed_by=?,reviewed_at=? WHERE id=?",
                    (payload.get("comment", ""), payload["reviewer"], now, rectification_id),
                )
                connection.execute(
                    "UPDATE workmanship_defects SET state='closed',closed_at=?,version=version+1 WHERE id=?",
                    (now, defect["id"]),
                )
                self._event(connection, "workmanship_defect", defect["id"], "rectification_accepted", payload["reviewer"], {"round_no": row["round_no"]}, now)
            else:
                connection.execute(
                    "UPDATE workmanship_rectifications SET result='rejected',review_comment=?,reviewed_by=?,reviewed_at=? WHERE id=?",
                    (payload.get("comment", ""), payload["reviewer"], now, rectification_id),
                )
                connection.execute("UPDATE workmanship_defects SET state='rectifying',version=version+1 WHERE id=?", (defect["id"],))
                self._event(connection, "workmanship_defect", defect["id"], "rectification_rejected", payload["reviewer"], {"round_no": row["round_no"], "comment": payload.get("comment", "")}, now)
            return self.rectification_detail(rectification_id, connection)

    # ------------------------------------------------------------- 目标暂停

    def pause_target(self, restoration_target_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            target = self._target(connection, restoration_target_id)
            if target["state"] != "active":
                return self.target_workmanship_detail(restoration_target_id, connection, already_paused=True)
            connection.execute(
                "UPDATE restoration_targets SET state='paused',version=version+1 WHERE id=? AND state='active'",
                (restoration_target_id,),
            )
            self._event(connection, "restoration_target", restoration_target_id, "target_paused", actor, {"reason": reason}, now)
            return self.target_workmanship_detail(restoration_target_id, connection)

    def resume_target(self, restoration_target_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            target = self._target(connection, restoration_target_id)
            campaign = self._campaign(target["restoration_campaign_id"], connection)
            if target["state"] == "active":
                return self.target_workmanship_detail(restoration_target_id, connection, already_active=True)
            if campaign["state"] != "running":
                raise ConflictError("修缮活动未处于运行状态，请先恢复活动再恢复目标")
            connection.execute(
                "UPDATE restoration_targets SET state='active',version=version+1 WHERE id=? AND state='paused'",
                (restoration_target_id,),
            )
            self._event(connection, "restoration_target", restoration_target_id, "target_resumed", actor, {"reason": reason}, now)
            return self.target_workmanship_detail(restoration_target_id, connection)

    # ------------------------------------------------------------- 闭环门禁

    def assert_campaign_closable(self, restoration_campaign_id: int, connection: sqlite3.Connection | None = None) -> None:
        """活动完成前：所有目标工序通过、缺陷闭环。"""
        def _run(conn: sqlite3.Connection) -> None:
            blockers: list[dict[str, Any]] = []
            targets = conn.execute(
                "SELECT * FROM restoration_targets WHERE restoration_campaign_id=?",
                (restoration_campaign_id,),
            ).fetchall()
            for target in targets:
                pending_stages = conn.execute(
                    "SELECT code FROM workmanship_stage_instances WHERE restoration_target_id=? AND state<>'approved' ORDER BY sequence_no,id",
                    (target["id"],),
                ).fetchall()
                open_defects = conn.execute(
                    "SELECT code,severity FROM workmanship_defects WHERE restoration_target_id=? AND state<>'closed' ORDER BY id",
                    (target["id"],),
                ).fetchall()
                if pending_stages or open_defects:
                    blockers.append({
                        "target_id": target["id"],
                        "hall_id": target["hall_id"],
                        "cohort_key": target["cohort_key"],
                        "pending_stages": [row["code"] for row in pending_stages],
                        "open_defects": [dict(row) for row in open_defects],
                    })
            if blockers:
                raise ConflictError("存在未通过验收的工序或未闭环缺陷，不能完成修缮活动", context={"blocked_targets": blockers})

        if connection is not None:
            _run(connection)
            return
        with transaction(immediate=True) as locked:
            _run(locked)

    # ----------------------------------------------------------------- 详情

    def acceptance_round_detail(self, acceptance_round_id: int, connection: sqlite3.Connection | None = None, *, duplicate_replayed: bool = False) -> dict[str, Any]:
        connection = connection or self.connection
        round_row = self._round(connection, acceptance_round_id)
        result = self._round_dict(round_row, connection)
        stage = connection.execute("SELECT * FROM workmanship_stage_instances WHERE id=?", (round_row["stage_instance_id"],)).fetchone()
        result["stage_instance_id"] = stage["id"]
        result["stage_code"] = stage["code"]
        result["stage_name"] = stage["name"]
        result["restoration_target_id"] = stage["restoration_target_id"]
        result["duplicate"] = duplicate_replayed
        return result

    def rectification_detail(self, rectification_id: int, connection: sqlite3.Connection | None = None, *, duplicate_replayed: bool = False) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM workmanship_rectifications WHERE id=?", (rectification_id,)).fetchone()
        if row is None:
            raise NotFoundError("整改记录不存在")
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json"))
        defect = connection.execute("SELECT * FROM workmanship_defects WHERE id=?", (row["workmanship_defect_id"],)).fetchone()
        result["defect"] = self._defect_dict(defect, connection)
        result["duplicate"] = duplicate_replayed
        return result

    def target_workmanship_detail(self, restoration_target_id: int, connection: sqlite3.Connection | None = None, *, already_paused: bool = False, already_active: bool = False) -> dict[str, Any]:
        """当前可施工工序、未闭环缺陷与从首次提交到最终通过的完整链路。"""
        owns_connection = connection is None
        if owns_connection:
            target_row = self.connection.execute("SELECT * FROM restoration_targets WHERE id=?", (restoration_target_id,)).fetchone()
            if target_row is None:
                raise NotFoundError("修缮目标不存在")
            has_templates = self.connection.execute(
                "SELECT COUNT(*) FROM workmanship_templates WHERE restoration_campaign_id=?",
                (target_row["restoration_campaign_id"],),
            ).fetchone()[0]
            has_stages = self.connection.execute(
                "SELECT COUNT(*) FROM workmanship_stage_instances WHERE restoration_target_id=?",
                (restoration_target_id,),
            ).fetchone()[0]
            if has_templates and not has_stages:
                self.materialize_campaign(target_row["restoration_campaign_id"])
            connection = self.connection
        target = connection.execute(
            "SELECT t.*,c.id AS campaign_id,c.state AS campaign_state,c.code AS campaign_code,c.name AS campaign_name,c.temple_id "
            "FROM restoration_targets t JOIN restoration_campaigns c ON c.id=t.restoration_campaign_id WHERE t.id=?",
            (restoration_target_id,),
        ).fetchone()
        if target is None:
            raise NotFoundError("修缮目标不存在")
        stages = connection.execute(
            "SELECT * FROM workmanship_stage_instances WHERE restoration_target_id=? ORDER BY sequence_no,id",
            (restoration_target_id,),
        ).fetchall()
        stage_by_code = {row["code"]: row for row in stages}
        open_defect_rows = connection.execute(
            "SELECT * FROM workmanship_defects WHERE restoration_target_id=? AND state<>'closed' ORDER BY CASE severity WHEN 'critical' THEN 3 WHEN 'major' THEN 2 ELSE 1 END DESC,id",
            (restoration_target_id,),
        ).fetchall()
        critical_open = any(row["severity"] == BLOCKING_DEFECT_SEVERITY for row in open_defect_rows)
        work_allowed = target["state"] == "active" and target["campaign_state"] == "running" and not critical_open

        stage_items: list[dict[str, Any]] = []
        constructible: list[str] = []
        for row in stages:
            item = self._stage_dict(row, connection)
            dependencies = json.loads(row["depends_on_json"])
            deps_approved = all(stage_by_code.get(dep) is not None and stage_by_code[dep]["state"] == "approved" for dep in dependencies)
            item["dependencies_satisfied"] = deps_approved
            item["constructible"] = bool(work_allowed and deps_approved and row["state"] in ("pending", "in_progress"))
            if item["constructible"]:
                constructible.append(row["code"])
            stage_items.append(item)

        result = {
            "restoration_target_id": restoration_target_id,
            "restoration_campaign_id": target["campaign_id"],
            "campaign_code": target["campaign_code"],
            "campaign_name": target["campaign_name"],
            "campaign_state": target["campaign_state"],
            "target_state": target["state"],
            "hall_id": target["hall_id"],
            "cohort_key": target["cohort_key"],
            "work_allowed": work_allowed,
            "constructible_stages": constructible,
            "blocked_by_critical_defect": critical_open,
            "stages": stage_items,
            "open_defects": [self._defect_dict(row, connection) for row in open_defect_rows],
            "timeline": self._target_timeline(connection, restoration_target_id, stages, open_defect_rows),
        }
        if already_paused:
            result["already_paused"] = True
        if already_active:
            result["already_active"] = True
        return result

    # ------------------------------------------------------------- 内部辅助

    def _target_timeline(self, connection: sqlite3.Connection, target_id: int, stages: list[sqlite3.Row], defects: list[sqlite3.Row]) -> list[dict[str, Any]]:
        del defects
        stage_ids = [row["id"] for row in stages]
        rounds = []
        if stage_ids:
            placeholders = ",".join("?" * len(stage_ids))
            rounds = connection.execute(
                f"SELECT id,stage_instance_id FROM acceptance_rounds WHERE stage_instance_id IN ({placeholders}) ORDER BY id",
                stage_ids,
            ).fetchall()
        round_ids = [row["id"] for row in rounds]
        defect_ids = [
            row["id"]
            for row in connection.execute("SELECT id FROM workmanship_defects WHERE restoration_target_id=?", (target_id,)).fetchall()
        ]
        clauses = ["(resource_type='restoration_target' AND resource_id=?)"]
        params: list[Any] = [target_id]
        if round_ids:
            placeholders = ",".join("?" * len(round_ids))
            clauses.append(f"(resource_type='acceptance_round' AND resource_id IN ({placeholders}))")
            params.extend(round_ids)
        if defect_ids:
            placeholders = ",".join("?" * len(defect_ids))
            clauses.append(f"(resource_type='workmanship_defect' AND resource_id IN ({placeholders}))")
            params.extend(defect_ids)
        rows = connection.execute(
            "SELECT * FROM restoration_events WHERE " + " OR ".join(clauses) + " ORDER BY id",
            params,
        ).fetchall()
        stage_code_by_round = {
            row["id"]: next((stage["code"] for stage in stages if stage["id"] == row["stage_instance_id"]), None)
            for row in rounds
        }
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            if item["resource_type"] == "acceptance_round":
                item["resource_code"] = stage_code_by_round.get(item["resource_id"])
            result.append(item)
        return result

    @staticmethod
    def _require_active_work(campaign: sqlite3.Row, target: sqlite3.Row) -> None:
        if campaign["state"] != "running":
            raise ConflictError("修缮活动未运行，现场作业已暂停")
        if target["state"] != "active":
            raise ConflictError("修缮目标已暂停，不能进行现场作业或签署")

    @staticmethod
    def _require_dependencies_approved(connection: sqlite3.Connection, target_id: int, stage: sqlite3.Row) -> None:
        for dependency in json.loads(stage["depends_on_json"]):
            dep = connection.execute(
                "SELECT state FROM workmanship_stage_instances WHERE restoration_target_id=? AND code=?",
                (target_id, dependency),
            ).fetchone()
            if dep is None or dep["state"] != "approved":
                raise ConflictError("前置工序尚未通过验收", context={"blocked_by_stage": dependency})

    @staticmethod
    def _blocking_defect(connection: sqlite3.Connection, target_id: int) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM workmanship_defects WHERE restoration_target_id=? AND severity=? AND state<>'closed' ORDER BY id LIMIT 1",
            (target_id, BLOCKING_DEFECT_SEVERITY),
        ).fetchone()

    @staticmethod
    def _validated_check_results(stage: sqlite3.Row, provided: list[dict[str, Any]]) -> list[dict[str, Any]]:
        frozen_checks = json.loads(stage["checks_json"])
        codes = [item["code"] for item in frozen_checks]
        if not provided:
            raise ValidationError("必须逐项填报现场检查结果")
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for entry in provided:
            code = entry.get("code")
            if code not in codes:
                raise ValidationError(f"检查项不属于该工序：{code}")
            if code in seen:
                raise ValidationError(f"检查项重复填报：{code}")
            if not isinstance(entry.get("passed"), bool):
                raise ValidationError(f"检查项 {code} 必须给出布尔判定")
            seen.add(code)
            normalized.append({"code": code, "passed": entry["passed"], "note": str(entry.get("note", ""))[:500]})
        missing = [code for code in codes if code not in seen]
        if missing:
            raise ValidationError("存在未填报的检查项：" + ",".join(missing))
        order = {code: index for index, code in enumerate(codes)}
        normalized.sort(key=lambda item: order[item["code"]])
        return normalized

    @staticmethod
    def _frozen_signatory(signatories: list[dict[str, Any]], code: str) -> dict[str, Any]:
        for item in signatories:
            if item["code"] == code:
                return item
        raise ConflictError(f"责任人 {code} 不在本次验收冻结的签署名单中")

    @staticmethod
    def _next_round_no(connection: sqlite3.Connection, stage_id: int) -> int:
        return int(connection.execute(
            "SELECT COALESCE(MAX(round_no),0)+1 FROM acceptance_rounds WHERE stage_instance_id=?",
            (stage_id,),
        ).fetchone()[0])

    def _campaign(self, campaign_id: int, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM restoration_campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row is None:
            raise NotFoundError("修缮活动不存在")
        return row

    def _target(self, connection: sqlite3.Connection, target_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM restoration_targets WHERE id=?", (target_id,)).fetchone()
        if row is None:
            raise NotFoundError("修缮目标不存在")
        return row

    def _stage(self, connection: sqlite3.Connection, target_id: int, code: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM workmanship_stage_instances WHERE restoration_target_id=? AND code=?",
            (target_id, code),
        ).fetchone()
        if row is None:
            raise NotFoundError("工序不存在或尚未按模板展开")
        return row

    def _round(self, connection: sqlite3.Connection, round_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM acceptance_rounds WHERE id=?", (round_id,)).fetchone()
        if row is None:
            raise NotFoundError("验收轮次不存在")
        return row

    def _defect(self, connection: sqlite3.Connection, defect_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM workmanship_defects WHERE id=?", (defect_id,)).fetchone()
        if row is None:
            raise NotFoundError("缺陷不存在")
        return row

    @staticmethod
    def _template_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["checks"] = json.loads(result.pop("checks_json"))
        result["signatories"] = json.loads(result.pop("signatories_json"))
        result["depends_on"] = json.loads(result.pop("depends_on_json"))
        return result

    @staticmethod
    def _stage_dict(row: sqlite3.Row, connection: sqlite3.Connection) -> dict[str, Any]:
        result = dict(row)
        result["checks"] = json.loads(result.pop("checks_json"))
        result["signatories"] = json.loads(result.pop("signatories_json"))
        result["depends_on"] = json.loads(result.pop("depends_on_json"))
        latest = connection.execute(
            "SELECT id,round_no,result,created_at,closed_at FROM acceptance_rounds WHERE stage_instance_id=? ORDER BY round_no DESC,id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        result["latest_round"] = dict(latest) if latest else None
        result["rounds_count"] = connection.execute(
            "SELECT COUNT(*) FROM acceptance_rounds WHERE stage_instance_id=?", (row["id"],),
        ).fetchone()[0]
        return result

    def _round_dict(self, row: sqlite3.Row, connection: sqlite3.Connection) -> dict[str, Any]:
        result = dict(row)
        result["checks"] = json.loads(result.pop("checks_json"))
        result["signatories"] = json.loads(result.pop("signatories_json"))
        result["check_results"] = json.loads(result.pop("check_results_json"))
        signatures = connection.execute(
            "SELECT * FROM acceptance_signatures WHERE acceptance_round_id=? ORDER BY id", (row["id"],),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for signature in signatures:
            item = dict(signature)
            item["revocations"] = [
                dict(revocation)
                for revocation in connection.execute(
                    "SELECT * FROM acceptance_signature_revocations WHERE acceptance_signature_id=? ORDER BY id",
                    (signature["id"],),
                ).fetchall()
            ]
            items.append(item)
        result["signatures"] = items
        approvals = sum(1 for item in items if item["state"] == "active" and item["decision"] == "approved")
        result["active_approvals"] = approvals
        result["required_approvals"] = len(result["signatories"])
        return result

    @staticmethod
    def _defect_dict(row: sqlite3.Row, connection: sqlite3.Connection) -> dict[str, Any]:
        result = dict(row)
        rectifications = connection.execute(
            "SELECT * FROM workmanship_rectifications WHERE workmanship_defect_id=? ORDER BY round_no,id",
            (row["id"],),
        ).fetchall()
        items = []
        for item in rectifications:
            parsed = dict(item)
            parsed["evidence"] = json.loads(parsed.pop("evidence_json"))
            items.append(parsed)
        result["rectifications"] = items
        return result

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.repository.temple_by_code(code)
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    @staticmethod
    def _event(connection: sqlite3.Connection, resource_type: str, resource_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        TempleRestorationService._event(connection, resource_type, resource_id, event_type, actor, detail, now)
