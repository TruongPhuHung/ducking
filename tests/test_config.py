from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentctl.config import load_project_config
from agentctl.errors import AgentCtlError


VALID_CONFIG = '''schema_version = 1
project_id = "sample"
worker_profile = "default-implementer"
instruction_files = []
context_files = []
protected_paths = [".agentctl.toml"]
high_risk_paths = []
max_parallel = 1
max_fix_rounds = 2
max_files_per_unit = 4
max_changed_lines_per_unit = 100

[[validation.commands]]
name = "diff-check"
profiles = ["always"]
match = ["**"]
cwd = "."
argv = ["git", "diff", "--check"]
'''


class ConfigTests(unittest.TestCase):
    def test_project_config_requires_argv_array(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".agentctl.toml").write_text(
                VALID_CONFIG.replace(
                    'argv = ["git", "diff", "--check"]',
                    'argv = "git diff --check"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AgentCtlError, "array"):
                load_project_config(root)

    def test_project_config_parses_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".agentctl.toml").write_text(VALID_CONFIG, encoding="utf-8")
            config = load_project_config(root)
            self.assertEqual(config.project_id, "sample")
            self.assertEqual(config.validation_commands[0].argv, ("git", "diff", "--check"))

    def test_project_config_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "outside.toml"
            target.write_text(VALID_CONFIG, encoding="utf-8")
            (root / ".agentctl.toml").symlink_to(target)
            with self.assertRaisesRegex(AgentCtlError, "regular file"):
                load_project_config(root)


if __name__ == "__main__":
    unittest.main()
