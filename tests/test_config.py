import unittest

from common.config import Config


class ConfigTests(unittest.TestCase):
    def test_repo_root_includes_run_id_when_configured(self):
        """Check per-run workspaces are isolated by HEALER_RUN_ID."""
        config = Config(
            GITHUB_REPO_LINK="https://github.com/acme/shops.git",
            HEALER_RUN_ID="run-123",
        )

        self.assertEqual(config.repo_root.name, "run-123")
        self.assertEqual(config.repo_root.parent.name, "shops")


if __name__ == "__main__":
    unittest.main()
