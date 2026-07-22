from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentctl import __version__
from agentctl.errors import AgentCtlError
from agentctl.store import RunStore, new_run_id


class MetadataTests(unittest.TestCase):
    def test_versions_match(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plugin = json.loads(
            (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        version_file = (root / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(plugin["version"].split("+", 1)[0], __version__)
        self.assertEqual(version_file, __version__)

    def test_run_ids_stay_valid_for_maximum_task_id(self) -> None:
        run_id = new_run_id("a" * 128)
        self.assertLessEqual(len(run_id), 128)
        self.assertRegex(run_id, r"^\d{8}t\d{6}z-[a-z0-9-]+$")

    def test_run_store_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory():
            with self.assertRaises(AgentCtlError):
                RunStore("fixture", "../outside")


if __name__ == "__main__":
    unittest.main()
