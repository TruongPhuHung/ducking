from __future__ import annotations

import unittest

from agentctl.contracts import (
    validate_plan,
    validate_review,
    validate_semantic_action,
    validate_task,
)
from agentctl.errors import AgentCtlError


def valid_task() -> dict:
    return {
        "contract_version": 1,
        "task_id": "unit-one",
        "base_sha": "a" * 40,
        "objective": "Create one bounded file.",
        "acceptance": [
            {"id": "ac1", "claim": "The file exists.", "proof": "test"}
        ],
        "non_goals": [],
        "context_files": ["AGENTS.md"],
        "allowed_paths": ["src/**"],
        "forbidden_paths": [".env*"],
        "validation_profiles": ["default"],
        "risk_flags": [],
        "budget": {"wall_seconds": 30, "max_fix_rounds": 1},
    }


def valid_plan() -> dict:
    first = valid_task()
    first["depends_on"] = []
    second = valid_task()
    second["task_id"] = "unit-two"
    second["depends_on"] = ["unit-one"]
    return {
        "contract_version": 2,
        "plan_id": "plan-one",
        "base_sha": "a" * 40,
        "goal": "Complete two bounded units in order.",
        "assumptions": [],
        "non_goals": [],
        "units": [first, second],
        "retry": {"max_attempts": 3, "delays_seconds": [0, 5]},
        "lease": {
            "liveness_soft_seconds": 10,
            "liveness_hard_seconds": 20,
            "progress_soft_seconds": 30,
            "progress_hard_seconds": 60,
        },
    }


def valid_semantic_action() -> dict:
    return {
        "contract_version": 1,
        "snapshot_id": "snapshot-one",
        "snapshot_sha256": "b" * 64,
        "selected_command_id": "command-one",
        "reason_code": "bounded-choice",
    }


class ContractTests(unittest.TestCase):
    def test_valid_task(self) -> None:
        task = valid_task()
        self.assertIs(validate_task(task), task)

    def test_task_rejects_path_escape(self) -> None:
        task = valid_task()
        task["allowed_paths"] = ["../outside"]
        with self.assertRaisesRegex(AgentCtlError, "traverse"):
            validate_task(task)

    def test_valid_plan_with_dependency(self) -> None:
        plan = valid_plan()
        self.assertIs(validate_plan(plan), plan)

    def test_plan_rejects_dependency_cycle(self) -> None:
        plan = valid_plan()
        plan["units"][0]["depends_on"] = ["unit-two"]
        with self.assertRaisesRegex(AgentCtlError, "cycle"):
            validate_plan(plan)

    def test_semantic_action_requires_exact_schema_keys(self) -> None:
        for mutation in ("missing", "extra"):
            with self.subTest(mutation=mutation):
                action = valid_semantic_action()
                if mutation == "missing":
                    action.pop("reason_code")
                else:
                    action["explanation"] = "not allowed"
                with self.assertRaisesRegex(AgentCtlError, "exactly") as raised:
                    validate_semantic_action(action)
                self.assertEqual(raised.exception.code, "invalid_contract")

    def test_accept_review_rejects_major_finding(self) -> None:
        review = {
            "contract_version": 1,
            "run_id": "run-one",
            "patch_sha256": "b" * 64,
            "task_sha256": "c" * 64,
            "evidence_sha256": "d" * 64,
            "decision": "accept",
            "criteria": [
                {"id": "ac1", "status": "pass", "evidence": "test passed"}
            ],
            "findings": [
                {
                    "id": "f1",
                    "severity": "major",
                    "failure_mode": "Behavior is wrong.",
                    "required_change": "Fix the behavior.",
                }
            ],
        }
        with self.assertRaisesRegex(AgentCtlError, "major"):
            validate_review(review)

    def test_review_rejects_duplicate_criteria(self) -> None:
        review = {
            "contract_version": 1,
            "run_id": "run-one",
            "patch_sha256": "b" * 64,
            "task_sha256": "c" * 64,
            "evidence_sha256": "d" * 64,
            "decision": "accept",
            "criteria": [
                {"id": "ac1", "status": "pass", "evidence": "one"},
                {"id": "ac1", "status": "pass", "evidence": "two"},
            ],
            "findings": [],
        }
        with self.assertRaisesRegex(AgentCtlError, "Duplicate"):
            validate_review(review)


if __name__ == "__main__":
    unittest.main()
