import tempfile
import os
import shutil
import subprocess
import textwrap
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
    def test_github_full_build_runs_once_and_preserves_failure(self):
        bash = shutil.which("bash")
        if os.name == "nt":
            bash = str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
        if not bash or not Path(bash).exists():
            self.skipTest("Bash is required to execute the CI build script")
        with tempfile.TemporaryDirectory() as tmp:
            generator = GithubCIGenerator(_config(tmp))
            generator.create_ci_file()
            content = (generator.ci_dir / generator.ci_file_name).read_text(encoding="utf-8")
            script = textwrap.dedent(content.split("      - name: Build dbt project\n", 1)[1].split("        run: |\n", 1)[1].split("      - name:", 1)[0])
            script = script.replace("${{ github.event.before }}", "abc123").replace("${{ github.sha }}", "def456")
            for exit_code in (0, 7):
                with self.subTest(exit_code=exit_code):
                    stubs = (
                        'export PATH="/usr/bin:$PATH"\n'
                        "git() { echo shops_dwh/macros/money.sql; }\n"
                        f"dbt() {{ echo BUILD_CALLED; return {exit_code}; }}\n"
                    )
                    result = subprocess.run([bash, "-c", stubs + script], cwd=tmp, capture_output=True, text=True)
                    self.assertIn("starting full build", result.stdout, result.stderr)
                    self.assertEqual(result.returncode, exit_code, result.stderr)
                    self.assertEqual(result.stdout.count("BUILD_CALLED"), 1)

    def test_gitlab_profile_uses_configured_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            config.dbt_project_name = "analytics"
            generator = GitlabCIGenerator(config)
            generator.create_ci_file()
            content = (generator.ci_dir / generator.ci_file_name).read_text(encoding="utf-8")
        self.assertIn("analytics/profiles.yml", content)
        self.assertNotIn("shops_dwh/profiles.yml", content)

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
