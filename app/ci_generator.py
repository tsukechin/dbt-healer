from abc import ABC, abstractmethod
import logging
from pathlib import Path

from common.config import Config, get_config
from common.exceptions import DBTProfilesExistsError


class AbstractCIGenerator(ABC):
    APP_DIR = Path(__file__).resolve().parent.parent

    @property
    @abstractmethod
    def ci_dir(self):
        """CI directory path."""
        ...

    @property
    @abstractmethod
    def ci_content(self):
        """CI content path."""
        ...

    @property
    @abstractmethod
    def ci_file_name(self):
        """CI filename."""
        ...

    def __init__(self, config: Config | None = None):
        """Initialize generator with config and dbt path."""
        self.config = config or get_config()
        self.dbt_path = self.config.full_path_to_repo / self.config.dbt_project_name

    @property
    def _needs_healer_instance(self) -> bool:
        """Return whether CI should create a healer workspace."""
        return self.config.healer_review_enabled or self.config.healer_analyze_on_failure_enabled

    def _check_ci_profile(self) -> bool:
        """Checks if dbt profiles.yml exists in the project."""
        profiles_file = self.dbt_path / "profiles.yml"
        if not profiles_file.exists():
            raise DBTProfilesExistsError(f"profiles.yml not found, create it first in {self.dbt_path}")

        return "ci:" in profiles_file.read_text(encoding="utf-8")

    def create_ci_profile(self):
        """Creates CI profile for dbt project."""
        if self._check_ci_profile():
            logging.warning("CI profile already exists. Skipping creation.")
            return "exists"

        ci_profile = (
            "\nci:\n"
            "  target: ci\n"
            "  outputs:\n"
            "    ci:\n"
            f"      type: {self.config.db_type}\n"
            f"      host: {self.config.db_dbt_host}\n"
            f"      user: {self.config.db_dbt_username}\n"
            f"      password: {self.config.db_dbt_password}\n"
            f"      port: {self.config.db_dbt_port}\n"
            f"      dbname: {self.config.db_dbt_database}\n"
            f"      schema: {self.config.db_dbt_schema}\n"
        )

        with open(self.dbt_path / "profiles.yml", "a", encoding="utf-8") as f:
            f.write(ci_profile)

        return "created"

    def _github_feature_blocks(self) -> dict[str, str]:
        """Return optional GitHub Actions healer blocks."""
        create_step = ""
        if self._needs_healer_instance:
            create_step = """      - name: Create healer instance
        id: create-healer
        shell: bash
        run: |
          set -euo pipefail
          HEALER_ENDPOINT="{service_endpoint}"
          HEALER_BASE_URL="${HEALER_ENDPOINT%/analyze/}"
          CREATE_RESPONSE=$(
            curl -sS -X 'POST' \
              "${HEALER_BASE_URL}/create/" \
              -F "repo={github_link}" \
              -F "commit_hash=${{ steps.get-hash.outputs.commit_hash }}" \
              -F "dbt_path={dbt_project_name}" \
              -F "branch_name=${{ github.ref_name }}" \
              -F "diff_base=${{ github.event.before }}"
          )
          HEALER_RUN_ID=$(printf '%s' "$CREATE_RESPONSE" | python3 -c 'import json, sys; print(json.load(sys.stdin)["run_id"])')
          echo "run_id=$HEALER_RUN_ID" >> $GITHUB_OUTPUT"""

        review_step = ""
        if self.config.healer_review_enabled:
            review_step = """      - name: Review changed business logic
        shell: bash
        run: |
          set -euo pipefail
          HEALER_ENDPOINT="{service_endpoint}"
          HEALER_BASE_URL="${HEALER_ENDPOINT%/analyze/}"
          curl -sS -X 'POST' \
            "${HEALER_BASE_URL}/review/" \
            -F "run_id=${{ steps.create-healer.outputs.run_id }}"
"""

        analyze_step = ""
        if self.config.healer_analyze_on_failure_enabled:
            analyze_step = """      - name: Push failure to healer
        if: ${{ failure() && startsWith(github.ref, 'refs/heads/feature/') }}
        run: |
          curl -X 'POST' \
          '{service_endpoint}' \
          -F "repo={github_link}" \
          -F "commit_hash=${{ steps.get-hash.outputs.commit_hash }}" \
          -F "dbt_path={dbt_project_name}" \
          -F "run_id=${{ steps.create-healer.outputs.run_id }}" \
          -F "branch_name=${{ github.ref_name }}" \
          -F "diff_base=${{ github.event.before }}" \
          -F "log_file=@{dbt_project_name}/logs/dbt.log"
"""

        return {
            "{healer_create_step}": create_step,
            "{healer_review_step}": review_step,
            "{healer_analyze_failure_step}": analyze_step,
        }

    def _gitlab_feature_blocks(self) -> dict[str, str]:
        """Return optional GitLab CI healer blocks."""
        create_script = ""
        if self._needs_healer_instance:
            create_script = """      HEALER_ENDPOINT="{service_endpoint}"
      HEALER_BASE_URL="${HEALER_ENDPOINT%/analyze/}"
      CREATE_RESPONSE=$(
        curl -sS -X 'POST' \
          "${HEALER_BASE_URL}/create/" \
          -F "repo={github_link}" \
          -F "commit_hash=$CI_COMMIT_SHA" \
          -F "dbt_path={dbt_project_name}" \
          -F "branch_name=$CI_COMMIT_REF_NAME" \
          -F "diff_base=$CI_COMMIT_BEFORE_SHA"
      )
      HEALER_RUN_ID=$(printf '%s' "$CREATE_RESPONSE" | python -c 'import json, sys; print(json.load(sys.stdin)["run_id"])')
      echo "$HEALER_RUN_ID" > .healer_run_id"""

        review_script = ""
        if self.config.healer_review_enabled:
            review_script = """      curl -sS -X 'POST' \
        "${HEALER_BASE_URL}/review/" \
        -F "run_id=$HEALER_RUN_ID"
"""

        after_script = ""
        if self.config.healer_analyze_on_failure_enabled:
            after_script = """  after_script:
    - |
      HEALER_RUN_ID="$(cat .healer_run_id 2>/dev/null || true)"
      if [[ "$CI_JOB_STATUS" == "failed" && "$CI_COMMIT_REF_NAME" == feature/* && -f "{dbt_project_name}/logs/dbt.log" ]]; then
        curl -X 'POST' \
          '{service_endpoint}' \
          -F "repo={github_link}" \
          -F "commit_hash=$CI_COMMIT_SHA" \
          -F "dbt_path={dbt_project_name}" \
          -F "run_id=$HEALER_RUN_ID" \
          -F "branch_name=$CI_COMMIT_REF_NAME" \
          -F "diff_base=$CI_COMMIT_BEFORE_SHA" \
          -F "log_file=@{dbt_project_name}/logs/dbt.log"
      fi"""

        return {
            "{healer_create_script}": create_script,
            "{healer_review_script}": review_script,
            "{healer_after_script}": after_script,
        }

    def _feature_blocks(self) -> dict[str, str]:
        """Return optional CI feature blocks for current provider."""
        if self.ci_file_name == "ci.yml":
            return self._github_feature_blocks()
        return self._gitlab_feature_blocks()

    def create_ci_file(self, force: bool = False):
        """Creates CI file for configured CI provider."""
        ci_file = self.ci_dir / self.ci_file_name
        ci_file.parent.mkdir(parents=True, exist_ok=True)

        if not force and ci_file.exists() and ci_file.read_text(encoding="utf-8"):
            logging.warning("CI file already exists. Skipping creation.")
            return "exists"

        try:
            content = self.ci_content.read_text(encoding="utf-8")
            for key, val in self._feature_blocks().items():
                content = content.replace(key, val)

            for key, val in {
                "{service_endpoint}": self.config.service_endpoint,
                "{github_link}": self.config.github_repo_link,
                "{dbt_project_name}": self.config.dbt_project_name,
                "{base_branch}": self.config.base_branch,
                "{db_user}": self.config.db_dbt_username,
                "{db_password}": self.config.db_dbt_password,
                "{db_name}": self.config.db_dbt_database,
            }.items():
                content = content.replace(key, str(val))

            ci_file.write_text(content, encoding="utf-8")
        except Exception as e:
            logging.error(f"Failed to create CI file: {e}")
            raise RuntimeError(f"Failed to create CI file: {e}") from e

        return "created"


class GithubCIGenerator(AbstractCIGenerator):
    @property
    def ci_dir(self):
        """Return GitHub Actions workflows directory."""
        return self.dbt_path.parent / ".github" / "workflows"

    @property
    def ci_content(self):
        """Return GitHub Actions template path."""
        return self.APP_DIR / "common" / "ci_examples" / "ci_example_github.yml"

    @property
    def ci_file_name(self):
        """Return GitHub Actions workflow filename."""
        return "ci.yml"


class GitlabCIGenerator(AbstractCIGenerator):
    @property
    def ci_dir(self):
        """Return GitLab CI directory."""
        return self.dbt_path.parent

    @property
    def ci_content(self):
        """Return GitLab CI template path."""
        return self.APP_DIR / "common" / "ci_examples" / "ci_example_gitlab.yml"

    @property
    def ci_file_name(self):
        """Return GitLab CI filename."""
        return ".gitlab-ci.yml"
