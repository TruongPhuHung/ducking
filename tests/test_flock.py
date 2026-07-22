from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from agentctl.errors import AgentCtlError
from agentctl.flock import (
    SNAPSHOT_MAX_BYTES,
    cancel_flock,
    claim_semantic_snapshot,
    flock_finish,
    flock_heartbeat,
    flock_status,
    flock_sweep,
    flock_tick,
    init_flock,
    next_semantic_snapshot,
    record_semantic_delivery_failure,
    recover_flock,
    submit_semantic_action,
)
from agentctl.semantic import dispatch_semantic_supervisor, extract_semantic_action
from agentctl.store import RunStore
from agentctl.util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    json_hash,
    sha256_bytes,
    sha256_file,
)
from agentctl.worker import ProcessResult


PROJECT_CONFIG = '''schema_version = 1
project_id = "flock-test"
worker_profile = "supervisor"
instruction_files = []
context_files = []
protected_paths = [".agentctl.toml"]
high_risk_paths = []
max_parallel = 1
max_fix_rounds = 0
max_files_per_unit = 2
max_changed_lines_per_unit = 20
validation_isolation = "unsafe-host"

[[validation.commands]]
name = "test"
profiles = ["always", "default"]
match = ["**"]
cwd = "."
argv = ["/usr/bin/true"]
timeout_seconds = 10
'''


USER_CONFIG = '''schema_version = 1

[worker_profiles.supervisor]
adapter = "generic-cli"
argv = ["/usr/bin/true", "{workspace}", "{request_file}"]
probe_argv = ["/usr/bin/printf", "test-supervisor"]
probe_contains = "test-supervisor"
env_allow = []
timeout_seconds = 30
max_output_bytes = 4096
isolation = "unsafe-host"
runtime_id = "test-supervisor"

[semantic_roles]
mother = "supervisor"
top = "supervisor"
'''


class FlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.user_config = self.root / "user-config.toml"
        self.user_config.write_text(USER_CONFIG, encoding="utf-8")
        (self.project / ".agentctl.toml").write_text(
            PROJECT_CONFIG, encoding="utf-8"
        )
        self._git("init", "-q")
        self._git("config", "user.name", "Flock Test")
        self._git("config", "user.email", "flock@example.invalid")
        self._git("add", ".agentctl.toml")
        self._git("commit", "-qm", "initial")
        self.base_sha = self._git("rev-parse", "HEAD").stdout.strip()
        self.state_home = self.root / "state"
        self.environment = mock.patch.dict(
            os.environ,
            {"AGENTCTL_STATE_HOME": os.fspath(self.state_home)},
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def _git(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *argv],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
        )

    def _plan_path(
        self,
        task_count: int,
        *,
        liveness_soft: int = 10,
        liveness_hard: int = 20,
        progress_soft: int = 10,
        progress_hard: int = 20,
    ) -> Path:
        units = []
        for index in range(task_count):
            units.append(
                {
                    "contract_version": 1,
                    "task_id": f"unit-{index + 1}",
                    "base_sha": self.base_sha,
                    "objective": f"Complete unit {index + 1}.",
                    "acceptance": [
                        {
                            "id": "done",
                            "claim": "The bounded unit is complete.",
                            "proof": "test",
                        }
                    ],
                    "non_goals": [],
                    "context_files": [],
                    "allowed_paths": [f"unit-{index + 1}/**"],
                    "forbidden_paths": [".git/**"],
                    "validation_profiles": ["default"],
                    "risk_flags": [],
                    "budget": {"wall_seconds": 30, "max_fix_rounds": 0},
                    "depends_on": [],
                }
            )
        plan = {
            "contract_version": 2,
            "plan_id": f"plan-{task_count}",
            "base_sha": self.base_sha,
            "goal": "Exercise the fixed flock runtime.",
            "assumptions": [],
            "non_goals": [],
            "units": units,
            "retry": {"max_attempts": 3, "delays_seconds": [0]},
            "lease": {
                "liveness_soft_seconds": liveness_soft,
                "liveness_hard_seconds": liveness_hard,
                "progress_soft_seconds": progress_soft,
                "progress_hard_seconds": progress_hard,
            },
        }
        path = self.root / f"plan-{task_count}.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def _start(self, task_count: int, **lease: int) -> dict:
        return init_flock(
            self.project,
            self._plan_path(task_count, **lease),
            user_config_path=self.user_config,
            allow_unsafe_worker=True,
            allow_unsafe_supervisor=True,
        )

    @staticmethod
    def _state(started: dict) -> dict:
        root = Path(started["flock_root"])
        return json.loads((root / "state.json").read_text(encoding="utf-8"))

    @staticmethod
    def _action(pending: dict, command_name: str) -> dict:
        command = next(
            item for item in pending["commands"] if item["name"] == command_name
        )
        return {
            "contract_version": 1,
            "snapshot_id": pending["snapshot_id"],
            "snapshot_sha256": pending["snapshot_sha256"],
            "selected_command_id": command["command_id"],
            "reason_code": f"test_{command_name}",
        }

    def _soft_stall_snapshot(self) -> tuple[dict, dict]:
        started = self._start(
            1,
            liveness_soft=10,
            liveness_hard=20,
            progress_soft=2,
            progress_hard=20,
        )
        issued_at = datetime.now(UTC)
        flock_tick(self.project, started["flock_id"], now=issued_at)
        flock_sweep(
            self.project,
            started["flock_id"],
            now=issued_at + timedelta(seconds=3),
        )
        pending = next_semantic_snapshot(
            self.project, started["flock_id"], role="mother"
        )
        return started, pending

    def _verified_child_run(
        self,
        started: dict,
        assignment: dict,
        *,
        high_risk: bool = False,
        mutate_contract: bool = False,
    ) -> str:
        flock_state = self._state(started)
        task = json.loads(json.dumps(assignment["task"]))
        if mutate_contract:
            task["objective"] = "A substituted child objective."
        run_id = (
            f"child-{task['task_id']}-{assignment['lease']['lease_id'][:8]}"
        )
        store = RunStore.create(
            project_key=flock_state["project_key"],
            project_id=flock_state["project_id"],
            run_id=run_id,
            project_root=self.project,
            base_sha=self.base_sha,
            config_sha256=flock_state["config_sha256"],
            task_sha256=json_hash(task),
        )
        atomic_write_json(store.root / "task.json", task)
        patch = b"diff --git a/unit b/unit\nnew file mode 100644\n"
        patch_sha = sha256_bytes(patch)
        atomic_write_bytes(store.root / "patch.diff", patch)
        evidence = {
            "run_id": run_id,
            "passed": True,
            "patch_sha256": patch_sha,
            "commands": [],
            "scope_violations": [],
            "high_risk_files": ["unit/risky.py"] if high_risk else [],
            "high_risk_flags": ["security"] if high_risk else [],
        }
        evidence_path = store.root / "evidence.json"
        atomic_write_json(evidence_path, evidence)
        store.update_fields(
            state="ready_for_review",
            patch_sha256=patch_sha,
            evidence_sha256=sha256_file(evidence_path),
        )
        return run_id

    def test_pool_has_six_slots_and_seventh_task_stays_queued(self) -> None:
        with mock.patch("agentctl.semantic.run_worker") as run_worker:
            started = self._start(7)
            tick = flock_tick(self.project, started["flock_id"])
            status = flock_status(self.project, started["flock_id"])

        self.assertEqual(started["duck_count"], 6)
        self.assertEqual(len(tick["assignments"]), 6)
        self.assertEqual(
            [assignment["slot_id"] for assignment in tick["assignments"]],
            list(range(6)),
        )
        self.assertEqual(
            tick["assignments"][0]["task"]["flock_attempt"]["branch_ref"],
            tick["assignments"][0]["lease"]["branch_ref"],
        )
        self.assertEqual(status["duck_count"], 6)
        self.assertEqual(status["active_ducks"], 6)
        self.assertEqual(status["task_summary"], {"leased": 6, "queued": 1})
        run_worker.assert_not_called()

    def test_failure_replaces_only_the_assigned_duck(self) -> None:
        started = self._start(1)
        first = flock_tick(self.project, started["flock_id"])["assignments"][0]
        flock_finish(
            self.project,
            started["flock_id"],
            slot_id=first["slot_id"],
            lease_id=first["lease"]["lease_id"],
            outcome="retryable_failure",
            reason="worker_crash",
        )

        state = self._state(started)
        self.assertEqual(
            [duck["incarnation"] for duck in state["ducks"]],
            [2, 1, 1, 1, 1, 1],
        )
        second = flock_tick(self.project, started["flock_id"])["assignments"]
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["slot_id"], 0)
        self.assertEqual(second[0]["duck_incarnation"], 2)
        self.assertEqual(second[0]["lease"]["attempt"], 2)

    def test_replaced_duck_rejects_its_stale_lease(self) -> None:
        started = self._start(1)
        first = flock_tick(self.project, started["flock_id"])["assignments"][0]
        old_lease = first["lease"]["lease_id"]
        flock_finish(
            self.project,
            started["flock_id"],
            slot_id=0,
            lease_id=old_lease,
            outcome="retryable_failure",
            reason="worker_crash",
        )
        replacement = flock_tick(
            self.project, started["flock_id"]
        )["assignments"][0]
        self.assertNotEqual(replacement["lease"]["lease_id"], old_lease)

        with self.assertRaises(AgentCtlError) as raised:
            flock_heartbeat(
                self.project,
                started["flock_id"],
                slot_id=0,
                lease_id=old_lease,
                progress_seq=1,
                phase="working",
                summary="late heartbeat",
            )
        self.assertEqual(raised.exception.code, "stale_lease")

    def test_mother_recovery_fences_old_epoch_and_rebuilds_slot(self) -> None:
        started = self._start(1)
        issued_at = datetime.now(UTC)
        first = flock_tick(
            self.project, started["flock_id"], now=issued_at
        )["assignments"][0]

        recovered = recover_flock(
            self.project,
            started["flock_id"],
            expected_epoch=1,
            operation_id="recover-once",
            now=issued_at + timedelta(seconds=1),
        )
        state = self._state(started)

        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["coordinator_epoch"], 2)
        self.assertEqual(state["ducks"][0]["incarnation"], 2)
        self.assertEqual(state["tasks"]["unit-1"]["state"], "retry_wait")
        self.assertFalse(
            state["tasks"]["unit-1"]["attempt_history"][0]["worker_eof_seen"]
        )
        with self.assertRaises(AgentCtlError) as raised:
            flock_heartbeat(
                self.project,
                started["flock_id"],
                slot_id=0,
                lease_id=first["lease"]["lease_id"],
                progress_seq=1,
                phase="working",
                summary="late old-epoch event",
            )
        self.assertEqual(raised.exception.code, "stale_lease")
        duplicate = recover_flock(
            self.project,
            started["flock_id"],
            expected_epoch=1,
            operation_id="recover-once",
            now=issued_at + timedelta(seconds=2),
        )
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(duplicate["coordinator_epoch"], 2)
        with self.assertRaises(AgentCtlError) as stale:
            recover_flock(
                self.project,
                started["flock_id"],
                expected_epoch=1,
                operation_id="different-recovery",
                now=issued_at + timedelta(seconds=3),
            )
        self.assertEqual(stale.exception.code, "stale_recovery")

    def test_recovery_during_final_review_is_a_replay_safe_noop(self) -> None:
        started = self._start(1)
        flock_id = started["flock_id"]
        assignment = flock_tick(self.project, flock_id)["assignments"][0]
        child_run_id = self._verified_child_run(started, assignment)
        flock_finish(
            self.project,
            flock_id,
            slot_id=assignment["slot_id"],
            lease_id=assignment["lease"]["lease_id"],
            outcome="succeeded",
            reason="completed",
            child_run_id=child_run_id,
        )
        mother = next_semantic_snapshot(self.project, flock_id, role="mother")
        submit_semantic_action(
            self.project, flock_id, self._action(mother, "notify_top")
        )
        top_task = next_semantic_snapshot(self.project, flock_id, role="top")
        submit_semantic_action(self.project, flock_id, self._action(top_task, "ack"))
        aggregate = next_semantic_snapshot(self.project, flock_id, role="top")

        recovered = recover_flock(
            self.project,
            flock_id,
            expected_epoch=1,
            operation_id="review-noop",
        )
        duplicate = recover_flock(
            self.project,
            flock_id,
            expected_epoch=1,
            operation_id="review-noop",
        )
        after = next_semantic_snapshot(self.project, flock_id, role="top")

        self.assertFalse(recovered["recovered"])
        self.assertEqual(recovered["state"], "final_review")
        self.assertEqual(recovered["coordinator_epoch"], 1)
        self.assertTrue(duplicate["idempotent"])
        self.assertTrue(after["pending"])
        self.assertEqual(after["snapshot_id"], aggregate["snapshot_id"])

    def test_recovery_replays_unaffected_semantic_obligations(self) -> None:
        started = self._start(3)
        flock_id = started["flock_id"]
        assignments = flock_tick(self.project, flock_id)["assignments"]
        child_run_id = self._verified_child_run(started, assignments[0])
        flock_finish(
            self.project,
            flock_id,
            slot_id=assignments[0]["slot_id"],
            lease_id=assignments[0]["lease"]["lease_id"],
            outcome="succeeded",
            reason="completed",
            child_run_id=child_run_id,
        )
        flock_finish(
            self.project,
            flock_id,
            slot_id=assignments[1]["slot_id"],
            lease_id=assignments[1]["lease"]["lease_id"],
            outcome="retryable_failure",
            reason="ambiguous_failure",
        )

        recovered = recover_flock(
            self.project,
            flock_id,
            expected_epoch=1,
            operation_id="mixed-recovery",
        )
        state = self._state(started)
        current = [
            item
            for item in state["outbox"]
            if item["state"] == "pending"
            and item["coordinator_epoch"] == recovered["coordinator_epoch"]
            and item["flock_revision"] == state["aggregate_revision"]
            and item["subject_revision"]
            == state["tasks"][item["subject_id"]]["revision"]
        ]

        self.assertEqual(recovered["coordinator_epoch"], 2)
        self.assertEqual(recovered["replayed_semantic_obligations"], 2)
        self.assertEqual(
            {item["subject_id"] for item in current},
            {"unit-1", "unit-2"},
        )
        self.assertEqual(state["tasks"]["unit-3"]["state"], "retry_wait")
        self.assertTrue(state["tasks"]["unit-3"]["retry_authorized"])

    def test_hard_expiry_records_controller_eof_and_retries(self) -> None:
        started = self._start(1)
        issued_at = datetime.now(UTC)
        flock_tick(self.project, started["flock_id"], now=issued_at)

        sweep = flock_sweep(
            self.project,
            started["flock_id"],
            now=issued_at + timedelta(seconds=21),
        )
        state = self._state(started)
        task = state["tasks"]["unit-1"]
        eof = task["attempt_history"][0]

        self.assertEqual(sweep["events"][0]["reason"], "lease_hard_expired")
        self.assertEqual(task["state"], "retry_wait")
        self.assertIsNone(task["task_eof"])
        self.assertEqual(eof["outcome"], "retryable_failure")
        self.assertEqual(eof["reason"], "lease_hard_expired")
        self.assertFalse(eof["worker_eof_seen"])
        retry = flock_tick(
            self.project,
            started["flock_id"],
            now=issued_at + timedelta(seconds=21),
        )["assignments"][0]
        self.assertEqual(retry["lease"]["attempt"], 2)
        self.assertEqual(retry["duck_incarnation"], 2)

    def test_cumulative_wall_budget_forces_terminal_eof(self) -> None:
        started = self._start(1)
        issued_at = datetime.now(UTC)
        flock_tick(self.project, started["flock_id"], now=issued_at)

        sweep = flock_sweep(
            self.project,
            started["flock_id"],
            now=issued_at + timedelta(seconds=31),
        )
        state = self._state(started)
        task = state["tasks"]["unit-1"]

        self.assertEqual(sweep["events"][0]["reason"], "task_wall_budget_exhausted")
        self.assertEqual(task["state"], "dead_lettered")
        self.assertEqual(task["task_eof"]["outcome"], "dead_lettered")
        self.assertEqual(task["task_eof"]["reason"], "task_wall_budget_exhausted")
        self.assertEqual(state["state"], "escalated")

    def test_success_requires_bound_green_child_run(self) -> None:
        started = self._start(1)
        assignment = flock_tick(self.project, started["flock_id"])["assignments"][0]

        with self.assertRaises(AgentCtlError) as raised:
            flock_finish(
                self.project,
                started["flock_id"],
                slot_id=assignment["slot_id"],
                lease_id=assignment["lease"]["lease_id"],
                outcome="succeeded",
                reason="completed",
            )

        self.assertEqual(raised.exception.code, "unverified_child_result")
        self.assertEqual(
            self._state(started)["tasks"]["unit-1"]["state"], "leased"
        )

    def test_high_risk_child_requires_hash_bound_human_gate(self) -> None:
        started = self._start(1)
        assignment = flock_tick(self.project, started["flock_id"])["assignments"][0]
        child_run_id = self._verified_child_run(
            started, assignment, high_risk=True
        )

        with self.assertRaises(AgentCtlError) as raised:
            flock_finish(
                self.project,
                started["flock_id"],
                slot_id=assignment["slot_id"],
                lease_id=assignment["lease"]["lease_id"],
                outcome="succeeded",
                reason="completed",
                child_run_id=child_run_id,
            )

        self.assertEqual(raised.exception.code, "human_gate_required")

    def test_child_run_cannot_substitute_the_flock_contract(self) -> None:
        started = self._start(1)
        assignment = flock_tick(self.project, started["flock_id"])["assignments"][0]
        child_run_id = self._verified_child_run(
            started, assignment, mutate_contract=True
        )

        with self.assertRaises(AgentCtlError) as raised:
            flock_finish(
                self.project,
                started["flock_id"],
                slot_id=assignment["slot_id"],
                lease_id=assignment["lease"]["lease_id"],
                outcome="succeeded",
                reason="completed",
                child_run_id=child_run_id,
            )

        self.assertEqual(raised.exception.code, "review_mismatch")

    def test_late_reported_success_is_rejected_by_wall_budget(self) -> None:
        started = self._start(1)
        issued_at = datetime.now(UTC)
        assignment = flock_tick(
            self.project, started["flock_id"], now=issued_at
        )["assignments"][0]
        child_run_id = self._verified_child_run(started, assignment)

        result = flock_finish(
            self.project,
            started["flock_id"],
            slot_id=assignment["slot_id"],
            lease_id=assignment["lease"]["lease_id"],
            outcome="succeeded",
            reason="completed",
            child_run_id=child_run_id,
            now=issued_at + timedelta(seconds=31),
        )
        status = flock_status(self.project, started["flock_id"])

        self.assertFalse(result["ok"])
        self.assertEqual(result["attempt_eof"]["outcome"], "fatal_failure")
        self.assertEqual(result["task_state"], "dead_lettered")
        self.assertEqual(status["state"], "escalated")
        self.assertTrue(status["eof"])

    def test_escalation_fences_sibling_and_reaches_eof(self) -> None:
        started = self._start(2)
        assignments = flock_tick(self.project, started["flock_id"])["assignments"]

        flock_finish(
            self.project,
            started["flock_id"],
            slot_id=assignments[0]["slot_id"],
            lease_id=assignments[0]["lease"]["lease_id"],
            outcome="fatal_failure",
            reason="fatal_contract_failure",
        )
        status = flock_status(self.project, started["flock_id"])

        self.assertEqual(status["state"], "escalated")
        self.assertEqual(status["active_ducks"], 0)
        self.assertTrue(status["eof"])
        self.assertEqual(
            {item["state"] for item in status["tasks"]},
            {"dead_lettered", "escalated"},
        )

    def test_worker_cancel_escalates_flock_and_reaches_eof(self) -> None:
        started = self._start(2)
        assignments = flock_tick(self.project, started["flock_id"])["assignments"]

        flock_finish(
            self.project,
            started["flock_id"],
            slot_id=assignments[0]["slot_id"],
            lease_id=assignments[0]["lease"]["lease_id"],
            outcome="cancelled",
            reason="worker_cancelled",
        )
        status = flock_status(self.project, started["flock_id"])

        self.assertEqual(status["state"], "escalated")
        self.assertEqual(status["active_ducks"], 0)
        self.assertTrue(status["eof"])
        self.assertEqual(
            {item["state"] for item in status["tasks"]},
            {"cancelled", "escalated"},
        )

    def test_cancel_invalidates_pending_success_action(self) -> None:
        started = self._start(1)
        assignment = flock_tick(self.project, started["flock_id"])["assignments"][0]
        child_run_id = self._verified_child_run(started, assignment)
        flock_finish(
            self.project,
            started["flock_id"],
            slot_id=assignment["slot_id"],
            lease_id=assignment["lease"]["lease_id"],
            outcome="succeeded",
            reason="completed",
            child_run_id=child_run_id,
        )
        pending = next_semantic_snapshot(
            self.project, started["flock_id"], role="mother"
        )
        action = self._action(pending, "notify_top")

        cancel_flock(self.project, started["flock_id"])
        with self.assertRaises(AgentCtlError) as raised:
            submit_semantic_action(self.project, started["flock_id"], action)

        self.assertEqual(raised.exception.code, "stale_decision")
        self.assertEqual(
            flock_status(self.project, started["flock_id"])["state"],
            "cancelled",
        )

    def test_tick_replays_unacknowledged_assignment_and_finish_is_idempotent(self) -> None:
        started = self._start(1)
        first = flock_tick(self.project, started["flock_id"])["assignments"][0]
        replay = flock_tick(self.project, started["flock_id"])["assignments"][0]
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["lease"]["lease_id"], first["lease"]["lease_id"])

        result = flock_finish(
            self.project,
            started["flock_id"],
            slot_id=first["slot_id"],
            lease_id=first["lease"]["lease_id"],
            outcome="retryable_failure",
            reason="worker_crash",
        )
        duplicate = flock_finish(
            self.project,
            started["flock_id"],
            slot_id=first["slot_id"],
            lease_id=first["lease"]["lease_id"],
            outcome="retryable_failure",
            reason="worker_crash",
        )
        self.assertFalse(result["ok"])
        self.assertTrue(duplicate["idempotent"])

    def test_successful_semantic_handoff_reaches_complete_eof(self) -> None:
        started = self._start(1)
        flock_id = started["flock_id"]
        assignment = flock_tick(self.project, flock_id)["assignments"][0]
        child_run_id = self._verified_child_run(started, assignment)
        flock_finish(
            self.project,
            flock_id,
            slot_id=assignment["slot_id"],
            lease_id=assignment["lease"]["lease_id"],
            outcome="succeeded",
            reason="completed",
            child_run_id=child_run_id,
        )

        mother = next_semantic_snapshot(self.project, flock_id, role="mother")
        self.assertEqual(mother["trigger"], "task_terminal")
        submit_semantic_action(
            self.project, flock_id, self._action(mother, "notify_top")
        )

        top_task = next_semantic_snapshot(self.project, flock_id, role="top")
        self.assertEqual(top_task["trigger"], "task_terminal")
        submit_semantic_action(
            self.project, flock_id, self._action(top_task, "ack")
        )
        top_aggregate = next_semantic_snapshot(self.project, flock_id, role="top")
        self.assertEqual(top_aggregate["trigger"], "aggregate_ready")
        submit_semantic_action(
            self.project,
            flock_id,
            self._action(top_aggregate, "open_final_review"),
        )

        sol = next_semantic_snapshot(self.project, flock_id, role="sol")
        self.assertEqual(sol["trigger"], "aggregate_final_review")
        completed = submit_semantic_action(
            self.project, flock_id, self._action(sol, "approve_flock")
        )
        status = flock_status(self.project, flock_id)

        self.assertEqual(completed["state"], "reviewed")
        self.assertEqual(status["state"], "reviewed")
        self.assertTrue(status["eof"])
        self.assertEqual(status["active_ducks"], 0)
        self.assertEqual(status["tasks"][0]["task_eof"]["outcome"], "succeeded")

    def test_invalid_semantic_output_keeps_task_and_lease_unchanged(self) -> None:
        started = self._start(
            1,
            liveness_soft=10,
            liveness_hard=20,
            progress_soft=2,
            progress_hard=20,
        )
        flock_id = started["flock_id"]
        issued_at = datetime.now(UTC)
        flock_tick(self.project, flock_id, now=issued_at)
        flock_sweep(
            self.project,
            flock_id,
            now=issued_at + timedelta(seconds=3),
        )
        before = self._state(started)

        def invalid_runner(*args: object, **kwargs: object) -> ProcessResult:
            events_path = Path(kwargs["events_file"])
            stderr_path = Path(kwargs["stderr_file"])
            events_path.write_text("not a semantic action\n", encoding="utf-8")
            stderr_path.write_bytes(b"")
            return ProcessResult(
                exit_code=0,
                duration_ms=1,
                cancelled=False,
                timed_out=False,
                output_limited=False,
                stdout_path=events_path,
                stderr_path=stderr_path,
            )

        with mock.patch(
            "agentctl.semantic.run_worker", side_effect=invalid_runner
        ) as run_worker:
            with self.assertRaises(AgentCtlError) as raised:
                dispatch_semantic_supervisor(
                    self.project,
                    flock_id,
                    role="mother",
                    allow_unsafe_supervisor=True,
                )

        after = self._state(started)
        self.assertEqual(raised.exception.code, "semantic_invalid_response")
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["ducks"], before["ducks"])
        pending = next_semantic_snapshot(self.project, flock_id, role="mother")
        self.assertTrue(pending["pending"])
        self.assertEqual(pending["delivery_attempts"], 1)
        run_worker.assert_called_once()

    def test_semantic_snapshot_claim_prevents_duplicate_model_call(self) -> None:
        started = self._start(
            1,
            liveness_soft=10,
            liveness_hard=20,
            progress_soft=2,
            progress_hard=20,
        )
        flock_id = started["flock_id"]
        issued_at = datetime.now(UTC)
        flock_tick(self.project, flock_id, now=issued_at)
        flock_sweep(
            self.project,
            flock_id,
            now=issued_at + timedelta(seconds=3),
        )

        first = claim_semantic_snapshot(
            self.project,
            flock_id,
            role="mother",
            now=issued_at + timedelta(seconds=4),
        )
        duplicate = claim_semantic_snapshot(
            self.project,
            flock_id,
            role="mother",
            now=issued_at + timedelta(seconds=5),
        )

        self.assertTrue(first["pending"])
        self.assertFalse(duplicate["pending"])

    def test_expired_semantic_claim_cannot_clear_newer_claim(self) -> None:
        started, _ = self._soft_stall_snapshot()
        base = datetime.now(UTC)
        first = claim_semantic_snapshot(
            self.project,
            started["flock_id"],
            role="mother",
            claim_seconds=1,
            now=base,
        )
        second = claim_semantic_snapshot(
            self.project,
            started["flock_id"],
            role="mother",
            claim_seconds=30,
            now=base + timedelta(seconds=2),
        )

        with self.assertRaises(AgentCtlError) as raised:
            record_semantic_delivery_failure(
                self.project,
                started["flock_id"],
                first["snapshot_id"],
                claim_id=first["claim_id"],
                error_code="timeout",
                now=base + timedelta(seconds=3),
            )
        self.assertEqual(raised.exception.code, "stale_claim")
        state = self._state(started)
        item = next(
            value
            for value in state["outbox"]
            if value["snapshot_id"] == second["snapshot_id"]
        )
        self.assertEqual(item["claim_id"], second["claim_id"])

    def test_expired_semantic_claim_cannot_apply_an_action(self) -> None:
        started, _ = self._soft_stall_snapshot()
        base = datetime.now(UTC)
        claim = claim_semantic_snapshot(
            self.project,
            started["flock_id"],
            role="mother",
            claim_seconds=1,
            now=base,
        )

        with self.assertRaises(AgentCtlError) as raised:
            submit_semantic_action(
                self.project,
                started["flock_id"],
                self._action(claim, "recycle"),
                claim_id=claim["claim_id"],
                now=base + timedelta(seconds=2),
            )

        self.assertEqual(raised.exception.code, "stale_claim")
        status = flock_status(self.project, started["flock_id"])
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["active_ducks"], 1)

    def test_stale_delivery_failure_cannot_escalate_newer_progress(self) -> None:
        started, _ = self._soft_stall_snapshot()
        base = datetime.now(UTC)
        claim = claim_semantic_snapshot(
            self.project,
            started["flock_id"],
            role="mother",
            claim_seconds=30,
            now=base,
        )
        state = self._state(started)
        lease = state["tasks"]["unit-1"]["current_lease"]
        flock_heartbeat(
            self.project,
            started["flock_id"],
            slot_id=lease["slot_id"],
            lease_id=lease["lease_id"],
            progress_seq=1,
            phase="working",
            summary="new progress",
            now=base + timedelta(seconds=1),
        )

        with self.assertRaises(AgentCtlError) as raised:
            record_semantic_delivery_failure(
                self.project,
                started["flock_id"],
                claim["snapshot_id"],
                claim_id=claim["claim_id"],
                error_code="timeout",
                now=base + timedelta(seconds=2),
            )

        self.assertEqual(raised.exception.code, "stale_decision")
        after = self._state(started)
        self.assertEqual(after["state"], "running")
        self.assertEqual(after["tasks"]["unit-1"]["state"], "leased")
        item = next(
            value
            for value in after["outbox"]
            if value["snapshot_id"] == claim["snapshot_id"]
        )
        self.assertEqual(item["delivery_attempts"], 0)

    def test_top_or_sol_ack_authorizes_an_ambiguous_retry(self) -> None:
        for route in ("top", "sol"):
            with self.subTest(route=route):
                started = self._start(1)
                flock_id = started["flock_id"]
                assignment = flock_tick(self.project, flock_id)["assignments"][0]
                flock_finish(
                    self.project,
                    flock_id,
                    slot_id=assignment["slot_id"],
                    lease_id=assignment["lease"]["lease_id"],
                    outcome="retryable_failure",
                    reason="ambiguous_failure",
                )
                mother = next_semantic_snapshot(self.project, flock_id, role="mother")
                submit_semantic_action(
                    self.project, flock_id, self._action(mother, "notify_top")
                )
                top = next_semantic_snapshot(self.project, flock_id, role="top")
                if route == "sol":
                    submit_semantic_action(
                        self.project, flock_id, self._action(top, "escalate_sol")
                    )
                    decision = next_semantic_snapshot(
                        self.project, flock_id, role="sol"
                    )
                else:
                    decision = top
                submit_semantic_action(
                    self.project, flock_id, self._action(decision, "ack")
                )

                retry = flock_tick(self.project, flock_id)["assignments"]
                self.assertEqual(len(retry), 1)
                self.assertEqual(retry[0]["lease"]["attempt"], 2)

    def test_semantic_delivery_budget_escalates_to_sol_and_eof(self) -> None:
        started, _ = self._soft_stall_snapshot()
        base = datetime.now(UTC)
        moments = (0, 6, 37)
        for index, offset in enumerate(moments, start=1):
            claim = claim_semantic_snapshot(
                self.project,
                started["flock_id"],
                role="mother",
                claim_seconds=1,
                now=base + timedelta(seconds=offset),
            )
            self.assertTrue(claim["pending"])
            failure = record_semantic_delivery_failure(
                self.project,
                started["flock_id"],
                claim["snapshot_id"],
                claim_id=claim["claim_id"],
                error_code="timeout",
                now=base + timedelta(seconds=offset),
            )
            self.assertEqual(failure["delivery_attempts"], index)

        status = flock_status(self.project, started["flock_id"])
        sol = next_semantic_snapshot(
            self.project, started["flock_id"], role="sol"
        )
        self.assertEqual(status["state"], "escalated")
        self.assertTrue(status["eof"])
        self.assertEqual(sol["trigger"], "semantic_delivery_exhausted")

    def test_dispatch_rejects_tampered_snapshot_bytes_before_worker(self) -> None:
        started, pending = self._soft_stall_snapshot()
        snapshot_path = Path(pending["snapshot_path"])
        snapshot_path.write_bytes(snapshot_path.read_bytes() + b" ")

        with mock.patch("agentctl.semantic.run_worker") as run_worker:
            with self.assertRaises(AgentCtlError) as raised:
                dispatch_semantic_supervisor(
                    self.project,
                    started["flock_id"],
                    role="mother",
                    allow_unsafe_supervisor=True,
                )

        self.assertEqual(raised.exception.code, "artifact_tampered")
        run_worker.assert_not_called()

    def test_missing_snapshot_consumes_the_bounded_delivery_budget(self) -> None:
        started, pending = self._soft_stall_snapshot()
        Path(pending["snapshot_path"]).unlink()

        with mock.patch("agentctl.semantic.run_worker") as run_worker:
            with self.assertRaises(AgentCtlError) as raised:
                dispatch_semantic_supervisor(
                    self.project,
                    started["flock_id"],
                    role="mother",
                    allow_unsafe_supervisor=True,
                )

        self.assertEqual(raised.exception.code, "artifact_tampered")
        state = self._state(started)
        item = next(
            value
            for value in state["outbox"]
            if value["snapshot_id"] == pending["snapshot_id"]
        )
        self.assertEqual(item["delivery_attempts"], 1)
        self.assertIsNone(item.get("claim_id"))
        run_worker.assert_not_called()

    def test_dispatch_binds_snapshot_identity_and_role(self) -> None:
        for field, replacement in (
            ("snapshot_id", "different-snapshot"),
            ("role", "top"),
        ):
            with self.subTest(field=field):
                started, pending = self._soft_stall_snapshot()
                snapshot_path = Path(pending["snapshot_path"])
                snapshot = json.loads(snapshot_path.read_bytes())
                snapshot[field] = replacement
                payload = canonical_json(snapshot)
                snapshot_path.write_bytes(payload)

                state_path = Path(started["flock_root"]) / "state.json"
                state = json.loads(state_path.read_bytes())
                item = next(
                    value
                    for value in state["outbox"]
                    if value["snapshot_id"] == pending["snapshot_id"]
                )
                item["snapshot_sha256"] = sha256_bytes(payload)
                atomic_write_json(state_path, state)

                with mock.patch("agentctl.semantic.run_worker") as run_worker:
                    with self.assertRaises(AgentCtlError) as raised:
                        dispatch_semantic_supervisor(
                            self.project,
                            started["flock_id"],
                            role="mother",
                            allow_unsafe_supervisor=True,
                        )

                self.assertEqual(raised.exception.code, "artifact_tampered")
                run_worker.assert_not_called()

    def test_semantic_retry_clears_cancel_and_uses_fresh_workspace(self) -> None:
        started, pending = self._soft_stall_snapshot()
        item_root = (
            Path(started["flock_root"]) / "semantic" / pending["snapshot_id"]
        )
        item_root.mkdir(parents=True)
        cancel_path = item_root / "cancel.request"
        action = self._action(pending, "wait")
        workspaces: list[Path] = []

        def runner(*args: object, **kwargs: object) -> ProcessResult:
            self.assertFalse(Path(kwargs["cancel_file"]).exists())
            workspace = Path(kwargs["workspace"])
            workspaces.append(workspace)
            events_path = Path(kwargs["events_file"])
            stderr_path = Path(kwargs["stderr_file"])
            stderr_path.write_bytes(b"")
            if len(workspaces) == 1:
                exit_code = 1
            else:
                events_path.write_text(json.dumps(action), encoding="utf-8")
                exit_code = 0
            return ProcessResult(
                exit_code=exit_code,
                duration_ms=1,
                cancelled=False,
                timed_out=False,
                output_limited=False,
                stdout_path=events_path,
                stderr_path=stderr_path,
            )

        with mock.patch(
            "agentctl.flock.SEMANTIC_RETRY_DELAYS_SECONDS", (0,)
        ), mock.patch("agentctl.semantic.run_worker", side_effect=runner):
            cancel_path.write_text("stale", encoding="utf-8")
            first = dispatch_semantic_supervisor(
                self.project,
                started["flock_id"],
                role="mother",
                allow_unsafe_supervisor=True,
            )
            cancel_path.write_text("stale again", encoding="utf-8")
            second = dispatch_semantic_supervisor(
                self.project,
                started["flock_id"],
                role="mother",
                allow_unsafe_supervisor=True,
            )

        self.assertFalse(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(workspaces), 2)
        self.assertNotEqual(workspaces[0], workspaces[1])
        self.assertTrue(
            all(path.name.startswith("workspace-") for path in workspaces)
        )

    def test_snapshot_is_bounded_transcript_free_and_stale_after_progress(self) -> None:
        started = self._start(
            1,
            liveness_soft=10,
            liveness_hard=20,
            progress_soft=2,
            progress_hard=20,
        )
        issued_at = datetime.now(UTC)
        assignment = flock_tick(
            self.project, started["flock_id"], now=issued_at
        )["assignments"][0]
        flock_sweep(
            self.project,
            started["flock_id"],
            now=issued_at + timedelta(seconds=3),
        )
        pending = next_semantic_snapshot(
            self.project, started["flock_id"], role="mother"
        )
        payload = Path(pending["snapshot_path"]).read_bytes()
        snapshot = json.loads(payload)

        self.assertLessEqual(len(payload), SNAPSHOT_MAX_BYTES)
        self.assertEqual(
            snapshot["context_policy"],
            {
                "worker_transcript_included": False,
                "worker_prose_included": False,
                "prior_conversation_included": False,
                "raw_logs_included": False,
            },
        )
        self.assertNotIn("worker_transcript", snapshot)
        self.assertNotIn("raw_logs", snapshot)

        flock_heartbeat(
            self.project,
            started["flock_id"],
            slot_id=0,
            lease_id=assignment["lease"]["lease_id"],
            progress_seq=1,
            phase="working",
            summary="made progress",
            now=issued_at + timedelta(seconds=4),
        )
        current = next_semantic_snapshot(
            self.project, started["flock_id"], role="mother"
        )
        self.assertFalse(current["pending"])
        wait_command = next(
            command for command in pending["commands"] if command["name"] == "wait"
        )
        action = {
            "contract_version": 1,
            "snapshot_id": pending["snapshot_id"],
            "snapshot_sha256": pending["snapshot_sha256"],
            "selected_command_id": wait_command["command_id"],
            "reason_code": "observe_progress",
        }
        with self.assertRaises(AgentCtlError) as raised:
            submit_semantic_action(
                self.project,
                started["flock_id"],
                action,
                now=issued_at + timedelta(seconds=5),
            )
        self.assertEqual(raised.exception.code, "stale_decision")


class SemanticExtractionTests(unittest.TestCase):
    def test_extracts_action_from_streamed_text_parts(self) -> None:
        action = {
            "contract_version": 1,
            "snapshot_id": "snapshot-one",
            "snapshot_sha256": "a" * 64,
            "selected_command_id": "command-one",
            "reason_code": "bounded_choice",
        }
        text = json.dumps(action)
        midpoint = len(text) // 2
        payload = "\n".join(
            json.dumps({"part": {"text": chunk}})
            for chunk in (text[:midpoint], text[midpoint:])
        ).encode()

        self.assertEqual(extract_semantic_action(payload), action)

    def test_rejects_trailing_or_multiple_json_documents(self) -> None:
        action = {
            "contract_version": 1,
            "snapshot_id": "snapshot-one",
            "snapshot_sha256": "a" * 64,
            "selected_command_id": "command-one",
            "reason_code": "bounded_choice",
        }
        document = json.dumps(action)
        payloads = {
            "trailing": f"{document}\ntrailing text".encode(),
            "multiple": f"{document}\n{document}".encode(),
            "streamed_trailing": json.dumps(
                {"part": {"text": f"{document} trailing"}}
            ).encode(),
        }
        for name, payload in payloads.items():
            with self.subTest(name=name):
                with self.assertRaises(AgentCtlError) as raised:
                    extract_semantic_action(payload)
                self.assertEqual(
                    raised.exception.code, "semantic_invalid_response"
                )


if __name__ == "__main__":
    unittest.main()
