"""REQ-20260802-211032 停止 Trace 人工确认进证据编纂 —— 验收测试。

覆盖 AC-001~AC-006（AC-007 时延为线上观测，deferred 到 post-push）。
复用 test_dossier_trace_eligibility 的 seed helper 模式（tempfile DB）。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import app.core.db as db
from app.core.settings import settings
from app.dossier.evidence_override import (
    EvidenceOverrideError,
    approve_evidence_override,
    revoke_evidence_override,
)
from app.dossier.eligibility import (
    _is_approved_user_stop,
    assess_creation_trace,
    list_creation_trace_candidates,
)
from app.core.models import TraceLogEvent
from contracts.trace.payload import ContentAddressedPayloadStore


class EvidenceOverrideTest(unittest.TestCase):
    """停止 trace 人工确认进证据编纂全流程验收。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = settings.evolution_db
        self.old_payload_dir = settings.trace_payload_dir
        settings.evolution_db = str(Path(self.tmp.name) / "evolution.db")
        settings.trace_payload_dir = str(Path(self.tmp.name) / "payloads")
        db._conn = None
        db.init_db()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        settings.evolution_db = self.old_db
        settings.trace_payload_dir = self.old_payload_dir
        self.tmp.cleanup()

    # ── AC-002：终态单调性 —— 正交列判定，status 不被触碰 ──────────────

    def test_is_approved_user_stop_requires_all_three_conditions(self) -> None:
        """CON-002 放行条件：cancelled + user_stop + approved 三者同时成立。"""
        base = {"status": "cancelled", "evidence_override_approved": 1,
                "cancel_audit": json.dumps({"reason": "user_stop"})}
        # 三者齐全 → True
        self.assertTrue(_is_approved_user_stop(base))
        # 缺 approved → False
        self.assertFalse(_is_approved_user_stop({**base, "evidence_override_approved": 0}))
        # 非 cancelled → False
        self.assertFalse(_is_approved_user_stop({**base, "status": "completed"}))
        # 非 user_stop → False
        self.assertFalse(_is_approved_user_stop(
            {**base, "cancel_audit": json.dumps({"reason": "cancel_timeout"})}))
        # cancel_audit 为 NULL → False
        self.assertFalse(_is_approved_user_stop({**base, "cancel_audit": None}))
        # cancel_audit 损坏 → False
        self.assertFalse(_is_approved_user_stop({**base, "cancel_audit": "{bad json"}))

    # ── AC-001：产品负责人可对 user_stop trace 确认 ──────────────────

    def test_approve_writes_override_columns_without_touching_status(self) -> None:
        """AC-001/AC-002：确认写入正交列，status 保持 cancelled 不变。"""
        self._seed_cancelled_user_stop("trace-stop", with_write=True)

        result = approve_evidence_override(
            "trace-stop", approver_user_id="po-1", reason="半成品有价值"
        )

        self.assertTrue(result["approved"])
        self.assertEqual(result["approver"], "po-1")
        # status 列保持 cancelled，绝不变 completed（CON-001）
        run = db.query_one("SELECT status FROM runs WHERE trace_id='trace-stop'")
        self.assertEqual(run["status"], "cancelled")
        # 正交审计列被写入
        override = db.query_one(
            """SELECT evidence_override_approved, evidence_override_approver,
                      evidence_override_reason, evidence_override_approved_at,
                      evidence_override_revoked_at
               FROM runs WHERE trace_id='trace-stop'"""
        )
        self.assertEqual(override["evidence_override_approved"], 1)
        self.assertEqual(override["evidence_override_approver"], "po-1")
        self.assertEqual(override["evidence_override_reason"], "半成品有价值")
        self.assertIsNotNone(override["evidence_override_approved_at"])
        self.assertIsNone(override["evidence_override_revoked_at"])

    # ── EDGE-001：前置条件不满足时拒绝 ──────────────────────────────

    def test_approve_rejects_non_user_stop_or_uningested(self) -> None:
        """EDGE-001：非 user_stop / 未摄入 / 非 cancelled 都拒绝。"""
        # 未摄入
        with self.assertRaises(EvidenceOverrideError) as ctx:
            approve_evidence_override("nope", approver_user_id="po", reason="x")
        self.assertEqual(ctx.exception.status_code, 404)

        # cancel_timeout（非 user_stop）
        self._seed_cancelled("trace-timeout", reason="cancel_timeout", with_write=True)
        with self.assertRaises(EvidenceOverrideError) as ctx:
            approve_evidence_override("trace-timeout", approver_user_id="po", reason="x")
        self.assertEqual(ctx.exception.status_code, 422)

        # interrupted（非 cancelled）
        self._seed_run("trace-interrupted", status="interrupted")
        with self.assertRaises(EvidenceOverrideError) as ctx:
            approve_evidence_override("trace-interrupted", approver_user_id="po", reason="x")
        self.assertEqual(ctx.exception.status_code, 422)

        # cancel_audit 为 NULL
        self._seed_cancelled("trace-null-audit", reason=None, with_write=True)
        with self.assertRaises(EvidenceOverrideError):
            approve_evidence_override("trace-null-audit", approver_user_id="po", reason="x")

        # 空理由
        self._seed_cancelled_user_stop("trace-stop2", with_write=True)
        with self.assertRaises(EvidenceOverrideError):
            approve_evidence_override("trace-stop2", approver_user_id="po", reason="")

    # ── AC-004：三层资格同源，仅对已确认+user_stop 放行 ────────────────

    def test_three_layer_eligibility_only_approves_confirmed_user_stop(self) -> None:
        """AC-004/CON-002：四类 trace 的三层判定矩阵。

        (a) cancelled+user_stop+已确认 → 放行
        (b) cancelled+user_stop+未确认 → 拒绝
        (c) cancel_timeout → 拒绝
        (d) interrupted → 拒绝
        """
        # (a) 已确认停止 trace
        self._seed_cancelled_user_stop("trace-a", with_write=True, with_revision=True)
        approve_evidence_override("trace-a", approver_user_id="po", reason="有价值")
        # (b) 未确认停止 trace
        self._seed_cancelled_user_stop("trace-b", with_write=True, with_revision=True)
        # (c) cancel_timeout
        self._seed_cancelled("trace-c", reason="cancel_timeout", with_write=True, with_revision=True)
        # (d) interrupted
        self._seed_run("trace-d", status="interrupted")

        # assess_creation_trace 层
        self.assertTrue(assess_creation_trace("trace-a").eligible)
        self.assertFalse(assess_creation_trace("trace-b").eligible)
        self.assertFalse(assess_creation_trace("trace-c").eligible)
        self.assertFalse(assess_creation_trace("trace-d").eligible)

        # 候选列表层（只含 trace-a）
        candidates = list_creation_trace_candidates(limit=100, offset=0)
        ids = [item["trace_id"] for item in candidates["items"]]
        self.assertIn("trace-a", ids)
        self.assertNotIn("trace-b", ids)
        self.assertNotIn("trace-c", ids)
        self.assertNotIn("trace-d", ids)

    def test_compiler_eligibility_shares_same_source(self) -> None:
        """AC-004：编译器 _check_compile_eligibility 与候选同源（CON-002）。"""
        from app.dossier.compiler import _check_compile_eligibility

        self._seed_cancelled_user_stop("trace-comp", with_write=True, with_revision=True)
        approve_evidence_override("trace-comp", approver_user_id="po", reason="有价值")
        # 已确认 → 编译器资格通过（返回 None）
        self.assertIsNone(_check_compile_eligibility("trace-comp"))

    # ── AC-003：恢复产物走完整 hash/契约校验 ──────────────────────────

    def test_recovery_fails_without_approved_override(self) -> None:
        """AC-003/TD-001：recovery 默认拒绝 cancelled trace，需显式授权。"""
        from app.dossier.recovery import TracePayloadRecoveryError, recover_trace_artifacts

        self._seed_cancelled_user_stop("trace-rec", with_write=True)
        # 默认（allow_cancelled_approved=False）→ 抛错
        with self.assertRaises(TracePayloadRecoveryError):
            recover_trace_artifacts("trace-rec")
        # 未确认时即便授权也拒绝（不满足三条件）
        with self.assertRaises(TracePayloadRecoveryError):
            recover_trace_artifacts("trace-rec", allow_cancelled_approved=True)

    def test_recovery_with_empty_artifacts_does_not_block_approval(self) -> None:
        """EDGE-002：确认成功但恢复产物为空（停止过早无成功 write）。"""
        # 无 write 事件 → 恢复产物为空
        self._seed_cancelled_user_stop("trace-empty", with_write=False)

        result = approve_evidence_override(
            "trace-empty", approver_user_id="po", reason="试一下"
        )
        # 确认标记仍写入（人工判断被记录）
        self.assertTrue(result["approved"])
        # 恢复报告产物计数 0
        self.assertEqual(result["recovery"]["recovered_count"], 0)
        # 编纂时 _check_critical_evidence 仍失败（闸门不削弱，CON-003）
        from app.dossier.compiler import _check_critical_evidence
        facts = {"contract": {"available": True}, "deliveries": {}, "review_artifacts": {}}
        self.assertIsNotNone(_check_critical_evidence(facts))

    def test_recovery_unexpected_exception_does_not_raise(self) -> None:
        """F-B/EDGE-003：recover_trace_artifacts 抛非 TracePayloadRecoveryError 时，
        _trigger_recovery 的 except Exception 兜住，approve 不穿透成 500，返回 failed 报告。"""
        from unittest.mock import patch

        self._seed_cancelled_user_stop("trace-unex", with_write=True)
        # 模拟 recovery 物化阶段抛 OSError（非 TracePayloadRecoveryError）
        with patch("app.dossier.recovery.recover_trace_artifacts") as mock_rec:
            mock_rec.side_effect = OSError("disk I/O error")
            from app.dossier.evidence_override import _trigger_recovery
            report = _trigger_recovery("trace-unex")
        # 不抛错，返回 failed 报告
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["recovered_count"], 0)
        self.assertIn("恢复过程异常", report["error"])

    # ── EDGE-005：幂等 / 并发确认 ───────────────────────────────────

    def test_approve_is_idempotent_no_duplicate_recovery(self) -> None:
        """EDGE-005：重复确认返回当前状态且不重复恢复产物。"""
        self._seed_cancelled_user_stop("trace-idem", with_write=True, with_revision=True)

        first = approve_evidence_override("trace-idem", approver_user_id="po-a", reason="确认")
        # 第一次：要么 completed（recovery 成功创建了产物），要么 skipped（seed 已带
        # trace_payload_recovery 产物）。两种都意味着产物已就绪。
        self.assertIn(first["recovery"]["status"], ("completed", "skipped"))

        second = approve_evidence_override("trace-idem", approver_user_id="po-b", reason="再确认")
        # 幂等：approver 取首个成功者（po-a），不闪烁；恢复跳过
        self.assertEqual(second["approver"], "po-a")
        self.assertTrue(second["recovery"]["skipped"])

    # ── AC-006：撤回仅退标记，下游保留；非产品负责人被拒（守卫在 API 层） ──

    def test_revoke_only_clears_marker_downstream_preserved(self) -> None:
        """AC-006/DEC-003：撤回只设 revoked_at，不删已恢复产物。"""
        self._seed_cancelled_user_stop("trace-rev", with_write=True, with_revision=True)
        approve_evidence_override("trace-rev", approver_user_id="po", reason="有价值")

        # 确认产物存在
        before = db.query_one(
            "SELECT COUNT(*) AS c FROM artifact_revisions WHERE source_trace_id='trace-rev'"
        )["c"]

        result = revoke_evidence_override("trace-rev")
        self.assertTrue(result["revoked"])
        self.assertFalse(result["noop"])

        # 产物保留
        after = db.query_one(
            "SELECT COUNT(*) AS c FROM artifact_revisions WHERE source_trace_id='trace-rev'"
        )["c"]
        self.assertEqual(before, after)

        # 撤回后该 trace 回到被三道门挡死
        self.assertFalse(assess_creation_trace("trace-rev").eligible)
        # status 仍 cancelled
        run = db.query_one("SELECT status FROM runs WHERE trace_id='trace-rev'")
        self.assertEqual(run["status"], "cancelled")

    def test_revoke_is_idempotent(self) -> None:
        """FR-003：撤回幂等，对未确认/已撤回的 trace 无副作用。"""
        self._seed_cancelled_user_stop("trace-norev", with_write=True)
        # 未确认就撤回
        result = revoke_evidence_override("trace-norev")
        self.assertTrue(result["noop"])

    def test_reapprove_after_revoke_triggers_full_recovery(self) -> None:
        """EDGE-004：撤回后重新确认走完整恢复。"""
        self._seed_cancelled_user_stop("trace-reapprove", with_write=True, with_revision=True)
        approve_evidence_override("trace-reapprove", approver_user_id="po", reason="第一次")
        revoke_evidence_override("trace-reapprove")
        # 重新确认（覆盖 approver）
        result = approve_evidence_override("trace-reapprove", approver_user_id="po2", reason="重新确认")
        self.assertTrue(result["approved"])
        # 重新确认后再次满足资格
        self.assertTrue(assess_creation_trace("trace-reapprove").eligible)

    # ── AC-005：partial 卷宗 provenance ──────────────────────────────

    def test_partial_provenance_for_confirmed_stop_trace(self) -> None:
        """AC-005/FR-004：已确认停止 trace 编出的卷宗 provenance=partial。"""
        from app.dossier.api import _decide_dossier_provenance

        # 已确认未撤回 → partial
        self._seed_cancelled_user_stop("trace-partial", with_write=True, with_revision=True)
        approve_evidence_override("trace-partial", approver_user_id="po", reason="有价值")
        self.assertEqual(_decide_dossier_provenance("trace-partial"), "partial")

        # 标准 completed trace → trace_time / compile_time_snapshot
        self._seed_complete_creation("trace-normal")
        prov = _decide_dossier_provenance("trace-normal")
        self.assertIn(prov, ("trace_time", "compile_time_snapshot"))

        # 撤回后不再 partial（回到 trace_time/compile_time_snapshot）
        revoke_evidence_override("trace-partial")
        prov_after = _decide_dossier_provenance("trace-partial")
        self.assertIn(prov_after, ("trace_time", "compile_time_snapshot"))

    # ── FR-005：require_product_owner 守卫 ───────────────────────────

    def test_require_product_owner_dev_mode_when_empty(self) -> None:
        """DEC-005：白名单空时 dev 降级放行。"""
        from app.trace.access import require_product_owner
        from starlette.requests import Request

        old = settings.product_owner_user_ids
        settings.product_owner_user_ids = ""
        try:
            req = Request({"type": "http"})
            req.state.user_id = "anyone"
            self.assertEqual(require_product_owner(req), "anyone")
        finally:
            settings.product_owner_user_ids = old

    def test_require_product_owner_rejects_non_whitelisted(self) -> None:
        """FR-005：非白名单用户返回 403。"""
        from app.trace.access import require_product_owner
        from fastapi import HTTPException
        from starlette.requests import Request

        old = settings.product_owner_user_ids
        settings.product_owner_user_ids = "po-1,po-2"
        try:
            req = Request({"type": "http"})
            req.state.user_id = "intruder"
            with self.assertRaises(HTTPException) as ctx:
                require_product_owner(req)
            self.assertEqual(ctx.exception.status_code, 403)

            req2 = Request({"type": "http"})
            req2.state.user_id = "po-1"
            self.assertEqual(require_product_owner(req2), "po-1")
        finally:
            settings.product_owner_user_ids = old

    # ── seed helpers ────────────────────────────────────────────────

    def _seed_cancelled_user_stop(
        self, trace_id: str, *, with_write: bool = True, with_revision: bool = False
    ) -> None:
        self._seed_cancelled(trace_id, reason="user_stop", with_write=with_write,
                             with_revision=with_revision)

    def _seed_cancelled(
        self, trace_id: str, *, reason: str | None, with_write: bool, with_revision: bool = False
    ) -> None:
        cancel_audit = json.dumps({"reason": reason}) if reason else None
        db.execute(
            """INSERT INTO runs
               (trace_id, workspace_id, thread_id, session_name, endpoint, status,
                started_at, ended_at, event_count, ingested_at, owner_user_id,
                run_purpose, schema_version, service, workload, integrity_status,
                cancel_audit)
               VALUES (?, 'ws', 'thread', 'session', 'screenplay.generate.stream', 'cancelled',
                       ?, ?, 0, ?, 'owner', 'user_generation', 2, 'executor', 'creation', 'verified', ?)""",
            (trace_id, self._now(), self._now(), self._now(), cancel_audit),
        )
        self._seed_contract(trace_id)
        if with_write:
            self._seed_tool_end(trace_id, f"call-{trace_id}", "/chapter/chapter-01.md")
        if with_revision:
            self._seed_revision(trace_id, f"call-{trace_id}", "/chapter/chapter-01.md", "正文")

    def _seed_complete_creation(self, trace_id: str) -> None:
        # _seed_run 已内含 _seed_contract，这里只补 write + revision。
        self._seed_run(trace_id)
        self._seed_tool_end(trace_id, f"call-{trace_id}", "/chapter/chapter-01.md")
        self._seed_revision(trace_id, f"call-{trace_id}", "/chapter/chapter-01.md", "正文")

    def _seed_run(
        self, trace_id: str, *, status: str = "completed",
        service: str = "executor", workload: str = "creation",
    ) -> None:
        db.execute(
            """INSERT INTO runs
               (trace_id, workspace_id, thread_id, session_name, endpoint, status,
                started_at, ended_at, event_count, ingested_at, owner_user_id,
                run_purpose, schema_version, service, workload, integrity_status)
               VALUES (?, 'ws', 'thread', 'session', 'screenplay.generate.stream', ?,
                       ?, ?, 0, ?, 'owner', 'user_generation', 2, ?, ?, 'verified')""",
            (trace_id, status, self._now(), self._now(), self._now(), service, workload),
        )
        self._seed_contract(trace_id)

    def _seed_contract(self, trace_id: str) -> None:
        self._seed_event(TraceLogEvent(
            trace_id=trace_id, event_id=f"{trace_id}-contract", sequence=1,
            type="run_meta", status="completed", timestamp=self._now(), source="system",
            input={"contract_snapshot": {"task_type": "screenplay.generate.stream"}},
        ))

    def _seed_tool_end(self, trace_id: str, tool_call_id: str, path: str) -> None:
        # recovery.reconstruct_artifact_heads 需要 tool_start + tool_end 配对才能重建，
        # 故 seed 两个事件（start 的 payload_refs 携带 write 的 content payload）。
        content = "正文"
        store = ContentAddressedPayloadStore(settings.trace_payload_path)
        payload_ref = store.put({"content": content})
        # payload 元数据（recovery 读 payload 需要 payload_objects 行）
        db.execute(
            """INSERT INTO payload_objects
               (payload_id, content_hash, kind, size_bytes, sensitivity, expires_at,
                storage_path, sealed, created_at)
               VALUES (?, ?, ?, ?, 'internal', NULL, ?, 1, ?)
               ON CONFLICT(payload_id) DO NOTHING""",
            (payload_ref.payload_id, payload_ref.content_hash, payload_ref.kind,
             payload_ref.size_bytes,
             str(settings.trace_payload_path / f"{payload_ref.payload_id}.json"), self._now()),
        )
        self._seed_event(TraceLogEvent(
            trace_id=trace_id, event_id=f"{trace_id}-{tool_call_id}-start",
            sequence=self._next_sequence(trace_id), type="tool_start", status="running",
            timestamp=self._now(), source="middleware", agent_name="writing",
            tool_call_id=tool_call_id, tool_name="write_file",
            tool_args={"file_path": path, "content": content},
            payload_refs={"input": payload_ref},
        ))
        self._seed_event(TraceLogEvent(
            trace_id=trace_id, event_id=f"{trace_id}-{tool_call_id}-end",
            sequence=self._next_sequence(trace_id), type="tool_end", status="completed",
            timestamp=self._now(), source="middleware", agent_name="writing",
            tool_call_id=tool_call_id, tool_name="write_file",
            tool_args={"file_path": path, "content": content},
            payload_refs={"output": payload_ref},
        ))

    def _seed_revision(self, trace_id: str, tool_call_id: str, path: str, content: str) -> None:
        store = ContentAddressedPayloadStore(settings.trace_payload_path)
        payload_ref = store.put({"content": content})
        now = self._now()
        db.execute(
            """INSERT INTO payload_objects
               (payload_id, content_hash, kind, size_bytes, sensitivity, expires_at,
                storage_path, sealed, created_at)
               VALUES (?, ?, ?, ?, 'internal', NULL, ?, 1, ?)
               ON CONFLICT(payload_id) DO NOTHING""",
            (payload_ref.payload_id, payload_ref.content_hash, payload_ref.kind,
             payload_ref.size_bytes,
             str(settings.trace_payload_path / f"{payload_ref.payload_id}.json"), now),
        )
        revision_id = f"revision-{trace_id}"
        event = TraceLogEvent(
            trace_id=trace_id, event_id=f"{trace_id}-revision",
            sequence=self._next_sequence(trace_id), type="artifact_revision",
            status="completed", timestamp=now, source="runtime", agent_name="writing",
            tool_call_id=tool_call_id, tool_name="write_file",
            artifact_revision_id=revision_id,
            artifact={"logical_key": path, "artifact_type": "workspace_file",
                      "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest()},
            payload_refs={"output": payload_ref},
        )
        self._seed_event(event)
        # artifact_id 基于 (workspace, path) 的 hash，与 recovery.py 一致：
        # 同一 workspace 的同一文件是同一 artifact，不同 trace 是它的不同 revision。
        artifact_id = "artifact-" + hashlib.sha256(
            f"ws:workspace_file:{path}".encode("utf-8")
        ).hexdigest()[:32]
        db.execute(
            """INSERT INTO artifacts
               (artifact_id, artifact_type, workspace_id, logical_key, created_at)
               VALUES (?, 'workspace_file', 'ws', ?, ?)
               ON CONFLICT(artifact_id) DO NOTHING""",
            (artifact_id, path, now),
        )
        db.execute(
            """INSERT INTO artifact_revisions
               (artifact_revision_id, artifact_id, payload_id, content_hash,
                producer_trace_id, producer_event_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (revision_id, artifact_id, payload_ref.payload_id,
             hashlib.sha256(content.encode("utf-8")).hexdigest(),
             trace_id, event.event_id, now),
        )

    def _seed_event(self, event: TraceLogEvent) -> None:
        payload_json = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        db.execute(
            """INSERT INTO event_payloads
               (trace_id, sequence, type, timestamp, payload_json, event_id,
                event_hash, payload_refs_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.trace_id, event.sequence, event.type, event.timestamp,
             payload_json, event.event_id,
             hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
             json.dumps({k: ref.model_dump(mode="json") for k, ref in event.payload_refs.items()})),
        )

    def _next_sequence(self, trace_id: str) -> int:
        row = db.query_one(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS s FROM event_payloads WHERE trace_id=?",
            (trace_id,),
        )
        return int(row["s"])

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    unittest.main()
