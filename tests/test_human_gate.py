from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentctl.errors import AgentCtlError
from agentctl.orchestrator import approve_human_decision
from agentctl.store import RunStore, project_state_key
from agentctl.util import (
    atomic_write_bytes,
    atomic_write_json,
    json_hash,
    sha256_bytes,
    sha256_file,
)


class HumanGateTests(unittest.TestCase):
    def test_human_approval_is_bound_to_all_reviewed_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            state_home = base / "state"
            subprocess.run(["git", "init", "-b", "main", "--quiet"], cwd=repo, check=True)
            config = '''schema_version = 1
project_id = "human-fixture"
worker_profile = "fixture"
instruction_files = []
context_files = []
protected_paths = [".agentctl.toml"]
high_risk_paths = ["src/**"]
max_parallel = 1
max_fix_rounds = 1
max_files_per_unit = 2
max_changed_lines_per_unit = 20
validation_isolation = "unsafe-host"

[[validation.commands]]
name = "check"
profiles = ["always", "default"]
match = ["**"]
cwd = "."
argv = ["git", "diff", "--check"]
'''
            (repo / ".agentctl.toml").write_text(config, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.test",
                    "commit",
                    "-m",
                    "base",
                    "--quiet",
                ],
                cwd=repo,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            task = {
                "contract_version": 1,
                "task_id": "human-task",
                "base_sha": head,
                "objective": "Exercise the human gate.",
                "acceptance": [{"id": "ac1", "claim": "Reviewed.", "proof": "manual"}],
                "context_files": [],
                "allowed_paths": ["src/**"],
                "forbidden_paths": [],
                "validation_profiles": ["default"],
                "budget": {"wall_seconds": 30, "max_fix_rounds": 0},
                "risk_flags": ["security"],
            }
            task_hash = json_hash(task)
            patch_bytes = b""
            patch_hash = sha256_bytes(patch_bytes)

            with patch.dict("os.environ", {"AGENTCTL_STATE_HOME": str(state_home)}):
                key = project_state_key(repo, "human-fixture")
                store = RunStore.create(
                    project_key=key,
                    project_id="human-fixture",
                    run_id="human-run",
                    project_root=repo,
                    base_sha=head,
                    config_sha256=sha256_file(repo / ".agentctl.toml"),
                    task_sha256=task_hash,
                )
                atomic_write_json(store.root / "task.json", task)
                atomic_write_bytes(store.root / "patch.diff", patch_bytes)
                evidence = {
                    "passed": True,
                    "patch_sha256": patch_hash,
                    "high_risk_files": ["src/risky.py"],
                    "high_risk_flags": ["security"],
                }
                atomic_write_json(store.root / "evidence.json", evidence)
                evidence_hash = sha256_file(store.root / "evidence.json")
                review = {
                    "contract_version": 1,
                    "run_id": "human-run",
                    "patch_sha256": patch_hash,
                    "task_sha256": task_hash,
                    "evidence_sha256": evidence_hash,
                    "decision": "human_required",
                    "criteria": [{"id": "ac1", "status": "pass", "evidence": "manual"}],
                    "findings": [{
                        "id": "human1",
                        "severity": "major",
                        "failure_mode": "High-risk change needs product authority.",
                        "required_change": "Obtain explicit human approval.",
                    }],
                }
                atomic_write_json(store.root / "review.json", review)
                review_hash = sha256_file(store.root / "review.json")
                store.transition("dispatching", actor="test", reason="test")
                store.transition(
                    "candidate",
                    actor="test",
                    reason="test",
                    fields={"patch_sha256": patch_hash},
                )
                store.transition("verifying", actor="test", reason="test")
                store.transition(
                    "ready_for_review",
                    actor="test",
                    reason="test",
                    fields={"evidence_sha256": evidence_hash},
                )
                store.transition("reviewing", actor="test", reason="test")
                store.transition(
                    "human_required",
                    actor="test",
                    reason="test",
                    fields={"review_sha256": review_hash},
                )
                approval = {
                    "contract_version": 1,
                    "run_id": "human-run",
                    "patch_sha256": patch_hash,
                    "task_sha256": task_hash,
                    "evidence_sha256": evidence_hash,
                    "review_sha256": "f" * 64,
                    "approved_by": "fixture-human",
                    "rationale": "Reviewed the exact high-risk patch.",
                }
                approval_path = base / "approval.json"
                approval_path.write_text(json.dumps(approval), encoding="utf-8")
                with self.assertRaisesRegex(AgentCtlError, "does not match"):
                    approve_human_decision(repo, "human-run", approval_path)
                approval["review_sha256"] = review_hash
                approval_path.write_text(json.dumps(approval), encoding="utf-8")
                result = approve_human_decision(repo, "human-run", approval_path)
                self.assertEqual(result["state"], "accepted")


if __name__ == "__main__":
    unittest.main()
