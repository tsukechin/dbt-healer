import tempfile
import unittest
from pathlib import Path

from app.ci_generator import GithubCIGenerator, GitlabCIGenerator
from common.config import Config


def _config(tmp: str, **overrides) -> Config:
    """Build CI generator config for tests."""
    repo = Path(tmp)
    (repo / "shops_dwh").mkdir()
    return Config(
        FULL_PATH_TO_REPO=str(repo),
        dbt_project_name="shops_dwh",
        GITHUB_REPO_LINK="https://github.com/acme/shops_dwh.git",
        SERVICE_ENDPOINT="https://healer.example/analyze/",
        base_branch="main",
        **overrides,
    )


class CIGeneratorTests(unittest.TestCase):
    def test_github_ci_can_disable_review_and_failure_analysis(self):
        """Check setup flags remove all healer calls from GitHub CI."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(
                tmp,
                HEALER_REVIEW_ENABLED=False,
                HEALER_ANALYZE_ON_FAILURE_ENABLED=False,
            )
            generator = GithubCIGenerator(config)

            self.assertEqual(generator.create_ci_file(), "created")
            content = (Path(tmp) / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertNotIn("/create/", content)
        self.assertNotIn("/review/", content)
        self.assertNotIn("Push failure to healer", content)
        self.assertNotIn("{healer_", content)

    def test_gitlab_ci_can_disable_only_review(self):
        """Check GitLab CI keeps failure analysis while removing review."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(
                tmp,
                HEALER_REVIEW_ENABLED=False,
                HEALER_ANALYZE_ON_FAILURE_ENABLED=True,
            )
            generator = GitlabCIGenerator(config)

            self.assertEqual(generator.create_ci_file(), "created")
            content = (Path(tmp) / ".gitlab-ci.yml").read_text(encoding="utf-8")

        self.assertIn("/create/", content)
        self.assertNotIn("/review/", content)
        self.assertIn("after_script:", content)
        self.assertIn("/analyze/", content)
        self.assertNotIn("{healer_", content)

    def test_ci_behavior_force_overwrites_existing_ci_file(self):
        """Check setup can rewrite existing CI when behavior flags change."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(
                tmp,
                HEALER_REVIEW_ENABLED=False,
                HEALER_ANALYZE_ON_FAILURE_ENABLED=False,
            )
            generator = GithubCIGenerator(config)
            ci_file = Path(tmp) / ".github" / "workflows" / "ci.yml"
            ci_file.parent.mkdir(parents=True)
            ci_file.write_text("old workflow", encoding="utf-8")

            self.assertEqual(generator.create_ci_file(force=True), "created")
            content = ci_file.read_text(encoding="utf-8")

        self.assertNotEqual(content, "old workflow")
        self.assertNotIn("/review/", content)


if __name__ == "__main__":
    unittest.main()
