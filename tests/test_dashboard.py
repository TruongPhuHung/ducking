from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from agentctl.dashboard import create_dashboard_server, dashboard_snapshot
from agentctl.flock import init_flock


PROJECT_CONFIG = '''schema_version = 1
project_id = "dashboard-test"
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
probe_argv = ["/usr/bin/printf", "dashboard-supervisor"]
probe_contains = "dashboard-supervisor"
env_allow = []
timeout_seconds = 30
max_output_bytes = 4096
isolation = "unsafe-host"
runtime_id = "dashboard-supervisor"

[semantic_roles]
mother = "supervisor"
top = "supervisor"
'''


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".agentctl.toml").write_text(
            PROJECT_CONFIG, encoding="utf-8"
        )
        self.user_config = self.root / "user-config.toml"
        self.user_config.write_text(USER_CONFIG, encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.name", "Dashboard Test")
        self._git("config", "user.email", "dashboard@example.invalid")
        self._git("add", ".agentctl.toml")
        self._git("commit", "-qm", "initial")
        base_sha = self._git("rev-parse", "HEAD").stdout.strip()
        plan = {
            "contract_version": 2,
            "plan_id": "dashboard-plan",
            "base_sha": base_sha,
            "goal": "Exercise the live dashboard.",
            "assumptions": [],
            "non_goals": [],
            "units": [
                {
                    "contract_version": 1,
                    "task_id": "dashboard-unit",
                    "base_sha": base_sha,
                    "objective": "Exercise one dashboard task.",
                    "acceptance": [
                        {"id": "done", "claim": "It works.", "proof": "test"}
                    ],
                    "non_goals": [],
                    "context_files": [],
                    "allowed_paths": ["dashboard/**"],
                    "forbidden_paths": [".git/**"],
                    "validation_profiles": ["default"],
                    "risk_flags": [],
                    "budget": {"wall_seconds": 30, "max_fix_rounds": 0},
                    "depends_on": [],
                }
            ],
            "retry": {"max_attempts": 3, "delays_seconds": [0]},
            "lease": {
                "liveness_soft_seconds": 10,
                "liveness_hard_seconds": 20,
                "progress_soft_seconds": 10,
                "progress_hard_seconds": 20,
            },
        }
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.state_home = self.root / "state"
        self.environment = mock.patch.dict(
            os.environ, {"AGENTCTL_STATE_HOME": os.fspath(self.state_home)}
        )
        self.environment.start()
        started = init_flock(
            self.project,
            self.plan_path,
            user_config_path=self.user_config,
            allow_unsafe_worker=True,
            allow_unsafe_supervisor=True,
        )
        self.flock_id = started["flock_id"]
        self.server = None
        self.thread = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
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

    def test_snapshot_projects_six_ducks_without_credentials(self) -> None:
        snapshot = dashboard_snapshot(self.project, self.flock_id)

        self.assertEqual(len(snapshot["ducks"]), 6)
        self.assertEqual(snapshot["flock"]["active_ducks"], 0)
        self.assertEqual(snapshot["tasks"][0]["task_id"], "dashboard-unit")
        self.assertNotIn("semantic_profiles", json.dumps(snapshot))
        self.assertNotIn("argv", json.dumps(snapshot))

    def test_page_sse_and_token_protected_tick(self) -> None:
        self.server = create_dashboard_server(
            self.project,
            self.flock_id,
            port=0,
            allow_unsafe_supervisor=False,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

        with urllib.request.urlopen(f"{base_url}/", timeout=3) as response:
            page = response.read().decode()
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("Ducking control", page)
        self.assertNotIn("__DUCKING_BOOTSTRAP__", page)

        stream_request = urllib.request.Request(
            f"{base_url}/events", headers={"Cookie": cookie}
        )
        with urllib.request.urlopen(stream_request, timeout=3) as response:
            self.assertEqual(response.readline().decode().strip(), "event: state")
            self.assertTrue(response.readline().decode().startswith("data: "))

        denied = urllib.request.Request(
            f"{base_url}/api/actions/tick",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(denied, timeout=3)
        self.assertEqual(raised.exception.code, 403)

        allowed = urllib.request.Request(
            f"{base_url}/api/actions/tick",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
                "X-Ducking-Token": self.server.action_token,
            },
            method="POST",
        )
        with urllib.request.urlopen(allowed, timeout=3) as response:
            payload = json.loads(response.read())

        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["result"]["assignments"]), 1)
        self.assertEqual(payload["state"]["flock"]["active_ducks"], 1)


if __name__ == "__main__":
    unittest.main()
