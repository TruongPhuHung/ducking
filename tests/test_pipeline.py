from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentctl.orchestrator import (
    _prepare_worker_runtime,
    create_review_pack,
    dispatch_unit,
    init_run,
    integrate_run,
    submit_decision,
    verify_unit,
)
from agentctl.store import RunStore
from agentctl.store import project_state_key
from agentctl.errors import AgentCtlError


def quote(value: str) -> str:
    return json.dumps(value)


class PipelineTests(unittest.TestCase):
    def test_worker_runtime_replaces_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace = base / "workspace"
            target = base / "outside"
            workspace.mkdir()
            target.mkdir()
            sentinel = target / "sentinel.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            (workspace / ".agentctl-runtime").symlink_to(target, target_is_directory=True)

            request_path = _prepare_worker_runtime(workspace, {"run_id": "fixture"})

            self.assertTrue(sentinel.is_file())
            self.assertFalse((workspace / ".agentctl-runtime").is_symlink())
            self.assertEqual(json.loads(request_path.read_text()), {"run_id": "fixture"})

    def test_init_rejects_project_config_missing_from_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            task_path = base / "task.json"
            subprocess.run(["git", "init", "-b", "main", "--quiet"], cwd=repo, check=True)
            (repo / "AGENTS.md").write_text("Keep changes bounded.\n", encoding="utf-8")
            (repo / ".gitignore").write_text(".agentctl.toml\n", encoding="utf-8")
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
                    "baseline",
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
            (repo / ".agentctl.toml").write_text(
                '''schema_version = 1
project_id = "fixture-project"
worker_profile = "fixture-implementer"
instruction_files = ["AGENTS.md"]
context_files = []
protected_paths = [".agentctl.toml"]
high_risk_paths = []
validation_isolation = "unsafe-host"

[[validation.commands]]
name = "content-check"
profiles = ["default"]
match = ["generated.txt"]
cwd = "."
argv = ["git", "diff", "--check"]
''',
                encoding="utf-8",
            )
            task_path.write_text(
                json.dumps(
                    {
                        "contract_version": 1,
                        "task_id": "fixture-task",
                        "base_sha": head,
                        "objective": "Create generated.txt.",
                        "acceptance": [
                            {"id": "ac1", "claim": "File exists.", "proof": "diff"}
                        ],
                        "non_goals": [],
                        "context_files": ["AGENTS.md"],
                        "allowed_paths": ["generated.txt"],
                        "forbidden_paths": [],
                        "validation_profiles": ["default"],
                        "risk_flags": [],
                        "budget": {"wall_seconds": 30, "max_fix_rounds": 1},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(AgentCtlError) as caught:
                init_run(
                    repo,
                    task_path,
                    user_config_path=base / "missing-user-config.toml",
                )
            self.assertEqual(caught.exception.code, "project_config_not_in_base")

    def test_sequential_worker_verify_review_and_integrate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            state_home = base / "state"
            user_config = base / "user-config.toml"
            task_path = base / "task.json"
            review_path = base / "review.json"

            subprocess.run(["git", "init", "-b", "main", "--quiet"], cwd=repo, check=True)
            (repo / "AGENTS.md").write_text("Keep changes bounded.\n", encoding="utf-8")
            validator_code = (
                "from pathlib import Path; "
                "assert Path('generated.txt').read_text() == 'ok\\n'"
            )
            project_config = f'''schema_version = 1
project_id = "fixture-project"
worker_profile = "fixture-implementer"
instruction_files = ["AGENTS.md"]
context_files = []
protected_paths = [".agentctl.toml", ".git/**", ".env*"]
high_risk_paths = []
max_parallel = 1
max_fix_rounds = 1
max_files_per_unit = 2
max_changed_lines_per_unit = 20
validation_isolation = "unsafe-host"

[[validation.commands]]
name = "git-diff-check"
profiles = ["always"]
match = ["**"]
cwd = "."
argv = ["git", "diff", "--check"]

[[validation.commands]]
name = "content-check"
profiles = ["default"]
match = ["generated.txt"]
cwd = "."
argv = [{quote(sys.executable)}, "-c", {quote(validator_code)}]
'''
            (repo / ".agentctl.toml").write_text(project_config, encoding="utf-8")
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
                    "baseline",
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

            worker_code = (
                "from pathlib import Path; "
                "Path('generated.txt').write_text('ok\\n')"
            )
            user_config.write_text(
                f'''schema_version = 1

[worker_profiles.fixture-implementer]
adapter = "generic-cli"
argv = [{quote(sys.executable)}, "-c", {quote(worker_code)}, "{{workspace}}", "{{prompt}}"]
probe_argv = [{quote(sys.executable)}, "--version"]
probe_contains = "Python"
env_allow = []
timeout_seconds = 30
max_output_bytes = 100000
prompt = "Read {{request_file}}"
isolation = "unsafe-host"
runtime_id = "fixture-python"
''',
                encoding="utf-8",
            )
            task = {
                "contract_version": 1,
                "task_id": "fixture-task",
                "base_sha": head,
                "objective": "Create generated.txt with the expected content.",
                "acceptance": [
                    {
                        "id": "ac1",
                        "claim": "generated.txt contains ok followed by a newline.",
                        "proof": "test",
                    }
                ],
                "non_goals": [],
                "context_files": ["AGENTS.md"],
                "allowed_paths": ["generated.txt"],
                "forbidden_paths": [],
                "validation_profiles": ["default"],
                "risk_flags": [],
                "budget": {"wall_seconds": 30, "max_fix_rounds": 1},
            }
            task_path.write_text(json.dumps(task), encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "AGENTCTL_STATE_HOME": str(state_home),
                    "AGENTCTL_CONFIG": str(user_config),
                },
            ):
                initialized = init_run(
                    repo,
                    task_path,
                    user_config_path=user_config,
                    allow_unsafe_worker=True,
                )
                run_id = initialized["run_id"]
                candidate = dispatch_unit(repo, run_id)
                self.assertTrue(candidate["ok"])
                self.assertEqual(candidate["changed_files"], ["generated.txt"])

                verified = verify_unit(
                    repo, run_id, allow_unsafe_validation=True
                )
                self.assertTrue(verified["ok"])
                packed = create_review_pack(repo, run_id)
                self.assertTrue(Path(packed["review_pack"]).is_file())

                review = {
                    "contract_version": 1,
                    "run_id": run_id,
                    "patch_sha256": candidate["patch_sha256"],
                    "task_sha256": initialized["task_sha256"],
                    "evidence_sha256": RunStore(
                        initialized["project_key"], run_id
                    ).load()["evidence_sha256"],
                    "decision": "accept",
                    "criteria": [
                        {
                            "id": "ac1",
                            "status": "pass",
                            "evidence": "content-check exited zero",
                        }
                    ],
                    "findings": [],
                }
                review_path.write_text(json.dumps(review), encoding="utf-8")
                accepted = submit_decision(repo, run_id, review_path)
                self.assertEqual(accepted["state"], "accepted")

                dry_run = integrate_run(repo, run_id, dry_run=True)
                self.assertEqual(dry_run["action"], "integration_check_passed")
                store = RunStore(initialized["project_key"], run_id)
                patch_path = store.root / "patch.diff"
                original_patch = patch_path.read_bytes()
                patch_path.write_bytes(original_patch + b"\n")
                with self.assertRaisesRegex(AgentCtlError, "integrity"):
                    integrate_run(repo, run_id, dry_run=True)
                patch_path.write_bytes(original_patch)
                integrated = integrate_run(repo, run_id, dry_run=False)
                self.assertEqual(integrated["action"], "integrated")
                self.assertEqual((repo / "generated.txt").read_text(), "ok\n")
                state = RunStore(
                    project_state_key(repo, "fixture-project"), run_id
                ).load()
                self.assertEqual(state["state"], "integrated")


if __name__ == "__main__":
    unittest.main()
