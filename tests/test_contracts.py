from __future__ import annotations

import unittest

from agentctl.contracts import validate_review, validate_task
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


class ContractTests(unittest.TestCase):
    def test_valid_task(self) -> None:
        task = valid_task()
        self.assertIs(validate_task(task), task)

    def test_task_rejects_path_escape(self) -> None:
        task = valid_task()
        task["allowed_paths"] = ["../outside"]
        with self.assertRaisesRegex(AgentCtlError, "traverse"):
            validate_task(task)

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
