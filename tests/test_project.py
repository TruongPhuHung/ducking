from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentctl.project import attach_project, detach_project
from agentctl.errors import AgentCtlError
from agentctl.store import project_dispatch_slot


def init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", "--quiet"], cwd=root, check=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
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
        cwd=root,
        check=True,
    )


class ProjectLifecycleTests(unittest.TestCase):
    def test_attach_and_detach_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            state = base / "state"
            with patch.dict(os.environ, {"AGENTCTL_STATE_HOME": str(state)}):
                preview = attach_project(repo, dry_run=True)
                self.assertEqual(preview["action"], "would_attach")
                self.assertFalse((repo / ".agentctl.toml").exists())
                attached = attach_project(repo, dry_run=False)
                self.assertEqual(attached["action"], "attached")
                repeated = attach_project(repo, dry_run=False)
                self.assertEqual(repeated["action"], "already_attached")

                detach_preview = detach_project(repo, dry_run=True)
                self.assertEqual(detach_preview["action"], "would_detach")
                detached = detach_project(repo, dry_run=False)
                self.assertEqual(detached["action"], "detached")
                self.assertFalse((repo / ".agentctl.toml").exists())

    def test_project_dispatch_slot_enforces_parallel_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            with patch.dict(os.environ, {"AGENTCTL_STATE_HOME": str(state)}):
                with project_dispatch_slot("fixture-key", 1):
                    with self.assertRaisesRegex(AgentCtlError, "active worker"):
                        with project_dispatch_slot("fixture-key", 1):
                            self.fail("second slot should not be acquired")

if __name__ == "__main__":
    unittest.main()
