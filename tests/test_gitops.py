from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agentctl.config import ProjectConfig, ValidationCommand
from agentctl.gitops import (
    capture_diff,
    capture_external_worktree,
    create_independent_clone,
)
from agentctl.verifier import evaluate_policy


class GitIsolationTests(unittest.TestCase):
    def test_clone_has_no_origin_and_commits_cannot_hide_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            clone = base / "clone"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", "--quiet"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
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
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            create_independent_clone(repo, base_sha, clone)
            remotes = subprocess.run(
                ["git", "remote"],
                cwd=clone,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(remotes, "")
            history_count = subprocess.run(
                ["git", "rev-list", "HEAD", "--count"],
                cwd=clone,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(history_count, "1")

            (clone / "tracked.txt").write_text("after\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=clone, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Worker",
                    "-c",
                    "user.email=worker@example.test",
                    "commit",
                    "-m",
                    "worker commit",
                    "--quiet",
                ],
                cwd=clone,
                check=True,
            )
            snapshot = capture_diff(clone, base_sha)
            self.assertEqual(snapshot.changed_files, ("tracked.txt",))
            self.assertIn(b"+after", snapshot.patch)

    def test_patch_capture_does_not_execute_worker_textconv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            clone = base / "clone"
            marker = base / "textconv-ran"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", "--quiet"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
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
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            create_independent_clone(repo, base_sha, clone)
            script = base / "textconv.sh"
            script.write_text(
                f"#!/bin/sh\ntouch {marker}\ncat \"$1\"\n", encoding="utf-8"
            )
            script.chmod(0o755)
            subprocess.run(
                ["git", "config", "diff.evil.textconv", str(script)],
                cwd=clone,
                check=True,
            )
            (clone / ".gitattributes").write_text(
                "tracked.txt diff=evil\n", encoding="utf-8"
            )
            (clone / "tracked.txt").write_text("after\n", encoding="utf-8")
            snapshot = capture_diff(clone, base_sha)
            self.assertIn("tracked.txt", snapshot.changed_files)
            self.assertFalse(marker.exists())

    def test_external_capture_ignores_worker_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            worker = base / "worker"
            metadata = base / "metadata"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", "--quiet"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
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
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            create_independent_clone(repo, base_sha, worker)
            create_independent_clone(repo, base_sha, metadata)
            subprocess.run(
                ["git", "config", "core.worktree", str(repo)],
                cwd=worker,
                check=True,
            )
            (worker / "tracked.txt").write_text("after\n", encoding="utf-8")
            snapshot = capture_external_worktree(
                worker_workspace=worker,
                metadata_workspace=metadata,
                base_sha=base_sha,
            )
            self.assertEqual(snapshot.changed_files, ("tracked.txt",))
            self.assertIn(b"+after", snapshot.patch)
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(status, "")

    def test_deleted_symlink_is_detected_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            worker = base / "worker"
            metadata = base / "metadata"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", "--quiet"], cwd=repo, check=True)
            (repo / "target.txt").write_text("target\n", encoding="utf-8")
            (repo / "link.txt").symlink_to("target.txt")
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
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            create_independent_clone(repo, base_sha, worker)
            create_independent_clone(repo, base_sha, metadata)
            (worker / "link.txt").unlink()

            snapshot = capture_external_worktree(
                worker_workspace=worker,
                metadata_workspace=metadata,
                base_sha=base_sha,
            )
            self.assertEqual(snapshot.symlink_files, ("link.txt",))

            command = ValidationCommand(
                name="docs-check",
                profiles=("docs",),
                match=("**",),
                cwd=".",
                argv=("git", "diff", "--check"),
                env={},
                timeout_seconds=30,
            )
            config = ProjectConfig(
                path=repo / ".agentctl.toml",
                schema_version=1,
                project_id="fixture",
                worker_profile="fixture-worker",
                instruction_files=(),
                context_files=(),
                protected_paths=(),
                high_risk_paths=(),
                max_parallel=1,
                max_fix_rounds=1,
                max_files_per_unit=2,
                max_changed_lines_per_unit=20,
                max_patch_bytes=100000,
                validation_isolation="unsafe-host",
                validation_commands=(command,),
            )
            policy = evaluate_policy(
                task={
                    "allowed_paths": ["**"],
                    "forbidden_paths": [],
                    "validation_profiles": ["docs"],
                    "risk_flags": [],
                },
                config=config,
                workspace=worker,
                snapshot=snapshot,
            )
            self.assertIn(
                {"path": "link.txt", "reason": "symlink_patch_not_allowed"},
                policy["violations"],
            )

    def test_rename_reports_source_and_destination_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            worker = base / "worker"
            metadata = base / "metadata"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", "--quiet"], cwd=repo, check=True)
            (repo / "protected.txt").write_text("value\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "baseline", "--quiet"],
                cwd=repo,
                check=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            create_independent_clone(repo, base_sha, worker)
            create_independent_clone(repo, base_sha, metadata)
            (worker / "protected.txt").rename(worker / "allowed.txt")

            snapshot = capture_external_worktree(
                worker_workspace=worker,
                metadata_workspace=metadata,
                base_sha=base_sha,
            )
            self.assertEqual(snapshot.changed_files, ("allowed.txt", "protected.txt"))

    def test_forced_text_nul_patch_is_rejected_as_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            worker = base / "worker"
            metadata = base / "metadata"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", "--quiet"], cwd=repo, check=True)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "baseline", "--quiet"],
                cwd=repo,
                check=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            create_independent_clone(repo, base_sha, worker)
            create_independent_clone(repo, base_sha, metadata)
            (worker / ".gitattributes").write_text("*.bin diff\n", encoding="utf-8")
            (worker / "payload.bin").write_bytes(b"before\x00after")

            snapshot = capture_external_worktree(
                worker_workspace=worker,
                metadata_workspace=metadata,
                base_sha=base_sha,
            )
            self.assertIn("payload.bin", snapshot.binary_files)


if __name__ == "__main__":
    unittest.main()
