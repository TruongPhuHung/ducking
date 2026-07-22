from __future__ import annotations

import unittest

from agentctl.util import matches_pattern


class RootGlobTests(unittest.TestCase):
    def test_literal_and_single_star_are_root_anchored(self) -> None:
        self.assertTrue(matches_pattern("README.md", "README.md"))
        self.assertFalse(matches_pattern("docs/README.md", "README.md"))
        self.assertFalse(matches_pattern("nested/tool.py", "*.py"))

    def test_double_star_crosses_directories(self) -> None:
        self.assertTrue(matches_pattern("src/a/b.py", "src/**"))
        self.assertTrue(
            matches_pattern(
                "apps/server/priv/migrations/one.exs", "**/migrations/**"
            )
        )


if __name__ == "__main__":
    unittest.main()
