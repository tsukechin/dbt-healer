from functools import lru_cache
from pathlib import Path
import logging
import re

from dotenv import set_key
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

dotenv_path = Path(__file__).resolve().parents[1] / ".env"


def parse_github_repo_link(repo_link: str | None) -> tuple[str | None, str | None]:
    """Parse repository link into namespace and repository name."""
    if not repo_link:
        return None, None

    link = repo_link.strip().removesuffix(".git").rstrip("/")
    if link.startswith("git@"):
        _, path = link.split(":", 1)
    else:
        match = re.match(r"^(?:https?://)?(?:www\.)?[^/\s]+/(?P<path>.+)$", link)
        if not match:
            logging.warning("Unable to parse GITHUB_REPO_LINK: %s", repo_link)
            return None, None
        path = match.group("path")

    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        logging.warning("Unable to parse GITHUB_REPO_LINK: %s", repo_link)
        return None, None

    return "/".join(parts[:-1]), parts[-1]


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=dotenv_path,
        env_file_encoding="utf-8",
        extra="ignore",
    )
    service_port: str = "8888"
    service_endpoint: str = Field(default="localhost", validation_alias="SERVICE_ENDPOINT")

    github_repo_link: str = Field(default="", validation_alias="GITHUB_REPO_LINK")
    git_platform: str = Field(default="Github", validation_alias="GIT_PLATFORM")
    ai_provider: str = Field(default="Ollama", validation_alias="AI_PROVIDER")
    ai_provider_type: str = Field(default="Ollama (API)", validation_alias="AI_PROVIDER_TYPE")
    ai_api_key: str = Field(default="", validation_alias="AI_API_KEY")
    ai_model: str = Field(default="", validation_alias="AI_MODEL")
    ollama_host: str = Field(default="", validation_alias="OLLAMA_HOST")
    ollama_num_ctx: int = Field(default=8192, validation_alias="OLLAMA_NUM_CTX")
    ai_max_input_chars: int = Field(default=24000, validation_alias="AI_MAX_INPUT_CHARS")
    healer_run_id: str = Field(default="", validation_alias="HEALER_RUN_ID")
    healer_review_enabled: bool = Field(default=True, validation_alias="HEALER_REVIEW_ENABLED")
    healer_analyze_on_failure_enabled: bool = Field(default=True, validation_alias="HEALER_ANALYZE_ON_FAILURE_ENABLED")
    github_token: str = ""
    telegram_bot_token: str = Field(default="", validation_alias="TELEGRAM_BOT_TOKEN")
    dbt_project_name: str = ""
    base_branch: str = "master"

    full_path_to_repo_str: str = Field(default="", validation_alias="FULL_PATH_TO_REPO")
    db_dbt_host: str = "localhost"
    db_dbt_username: str = "dbt_healer"
    db_dbt_password: str = "dbt_healer"
    db_dbt_port: int = 5432
    db_type: str = "postgres"

    notifier_db_username: str = Field(default="postgres", validation_alias="NOTIFIER_DB_USERNAME")
    notifier_db_password: str = Field(default="postgres", validation_alias="NOTIFIER_DB_PASSWORD")
    notifier_db_port: int = Field(default=5432, validation_alias="NOTIFIER_DB_PORT")

    @field_validator("github_repo_link")
    @classmethod
    def normalize_github_link(cls, v: str) -> str:
        """Normalize GitHub repository link to clone URL form."""
        if not v:
            return ""
        if not v.endswith(".git"):
            v = v.rstrip("/") + ".git"
        return v
    
    @field_validator("service_endpoint")
    @classmethod
    def normalize_service_endpoint(cls, v: str) -> str:
        """Normalize service endpoint to analyzer URL."""
        if v.startswith("http://"):
            v = "https://" + v.removeprefix("http://")
        if not v.endswith("/analyze/"):
            v = v.rstrip("/") + "/analyze/"
        return v

    @property
    def github_owner_repo(self) -> tuple[str | None, str | None]:
        """Return configured GitHub owner and repository name."""
        return parse_github_repo_link(self.github_repo_link)

    @property
    def github_name(self) -> str | None:
        """Return configured GitHub owner."""
        return self.github_owner_repo[0]

    @property
    def github_repo(self) -> str | None:
        """Return configured GitHub repository name."""
        return self.github_owner_repo[1]

    @property
    def repo_root(self) -> Path:
        """Return local path for cloned failed repository."""
        root = Path.home() / ".failedrepo" / (self.github_repo or "")
        if self.healer_run_id:
            return root / self.healer_run_id
        return root

    @property
    def dbt_log(self) -> Path:
        """Return path to dbt log in failed repository."""
        if self.dbt_project_name:
            return self.repo_root / self.dbt_project_name / "logs" / "dbt.log"
        return self.repo_root / "logs" / "dbt.log"

    @property
    def uploaded_dbt_log(self) -> Path:
        """Return path to uploaded CI dbt log."""
        return self.repo_root / "logs" / "payload_dbt.log"

    @property
    def get_profiles_path(self) -> Path:
        """Return dbt profiles.yml path when it exists."""
        path = self.full_path_to_repo / self.dbt_project_name / "profiles.yml"

        return path if path.exists() else None

    @property
    def db_dbt_schema(self) -> str:
        """Return dbt schema from profiles.yml."""
        if not self.full_path_to_repo or not self.dbt_project_name or not self.get_profiles_path:
            return None

        with open(self.get_profiles_path, mode="r", encoding="utf-8") as f:
            schema = re.search(pattern=r"schema:\s*(?P<schema>\S+)", string=f.read())
        if not schema:
            return
        return schema.group("schema").strip()

    @property
    def db_dbt_database(self) -> str:
        """Return dbt database from profiles.yml."""
        if not self.full_path_to_repo or not self.dbt_project_name or not self.get_profiles_path:
            return None

        with open(self.get_profiles_path, mode="r", encoding="utf-8") as f:
            dbname = re.search(pattern=r"dbname:\s*(?P<database>\S+)", string=f.read())
        if not dbname:
            return
        return dbname.group("database").strip()

    @property
    def bot_token(self) -> str:
        """Return configured Telegram bot token."""
        return self.telegram_bot_token

    @property
    def full_path_to_repo(self) -> Path:
        """Return configured local repository path."""
        return Path(self.full_path_to_repo_str)

    def save(self, config_dict: dict[str, str | None]) -> bool:
        """Persist configuration values to .env file."""
        try:
            data = self.model_dump()
            data.update({key: val for key, val in config_dict.items() if val is not None})

            ai_provider = str(data.get("ai_provider", self.ai_provider) or "Ollama")
            ai_provider_type = str(data.get("ai_provider_type", self.ai_provider_type) or "")
            if ai_provider == "Ollama":
                ai_provider_type = ai_provider_type or "Ollama (API)"
            else:
                ai_provider_type = ""

            set_key(dotenv_path, "AI_PROVIDER", ai_provider)
            set_key(dotenv_path, "AI_PROVIDER_TYPE", ai_provider_type)
            set_key(dotenv_path, "AI_API_KEY", str(data.get("ai_api_key", self.ai_api_key) or ""))
            set_key(dotenv_path, "AI_MODEL", str(data.get("ai_model", self.ai_model) or ""))
            set_key(dotenv_path, "OLLAMA_HOST", str(data.get("ollama_host", self.ollama_host) or ""))
            set_key(dotenv_path, "OLLAMA_NUM_CTX", str(data.get("ollama_num_ctx", self.ollama_num_ctx)))
            set_key(dotenv_path, "AI_MAX_INPUT_CHARS", str(data.get("ai_max_input_chars", self.ai_max_input_chars)))
            set_key(
                dotenv_path,
                "HEALER_REVIEW_ENABLED",
                str(data.get("healer_review_enabled", self.healer_review_enabled)).lower(),
            )
            set_key(
                dotenv_path,
                "HEALER_ANALYZE_ON_FAILURE_ENABLED",
                str(data.get("healer_analyze_on_failure_enabled", self.healer_analyze_on_failure_enabled)).lower(),
            )

            set_key(dotenv_path, "GITHUB_REPO_LINK", str(data.get("github_repo_link", self.github_repo_link) or ""))
            set_key(dotenv_path, "GITHUB_TOKEN", str(data.get("github_token", self.github_token) or ""))
            set_key(dotenv_path, "GIT_PLATFORM", str(data.get("git_platform", self.git_platform) or "Github"))
            set_key(dotenv_path, "BASE_BRANCH", str(data.get("base_branch", self.base_branch) or "master"))

            set_key(dotenv_path, "TELEGRAM_BOT_TOKEN", str(data.get("telegram_bot_token", self.telegram_bot_token) or ""))
            set_key(dotenv_path, "BOT_TOKEN", str(data.get("telegram_bot_token", self.telegram_bot_token) or ""))

            set_key(dotenv_path, "FULL_PATH_TO_REPO", str(data.get("full_path_to_repo_str", self.full_path_to_repo_str) or ""))
            set_key(dotenv_path, "DBT_PROJECT_NAME", str(data.get("dbt_project_name", self.dbt_project_name) or ""))
            set_key(dotenv_path, "DB_DBT_HOST", str(data.get("db_dbt_host", self.db_dbt_host) or "localhost"))
            set_key(dotenv_path, "DB_DBT_USERNAME", str(data.get("db_dbt_username", self.db_dbt_username) or "dbt_healer"))
            set_key(dotenv_path, "DB_DBT_PASSWORD", str(data.get("db_dbt_password", self.db_dbt_password) or "dbt_healer"))
            set_key(dotenv_path, "DB_DBT_DATABASE", str(data.get("db_dbt_database", self.db_dbt_database) or "dbt_healer"))
            set_key(dotenv_path, "DB_DBT_PORT", str(data.get("db_dbt_port", self.db_dbt_port) or 5432))
            set_key(dotenv_path, "DB_DBT_SCHEMA", str(data.get("db_dbt_schema", self.db_dbt_schema) or "dbt_healer"))
            set_key(dotenv_path, "DB_TYPE", str(data.get("db_type", self.db_type) or "postgres"))

            set_key(dotenv_path, "SERVICE_ENDPOINT", str(data.get("service_endpoint", self.service_endpoint) or "localhost"))
            set_key(dotenv_path, "SERVICE_PORT", str(data.get("service_port", self.service_port) or "8888"))

            set_key(dotenv_path, "NOTIFIER_DB_USERNAME", "postgres")
            set_key(dotenv_path, "NOTIFIER_DB_PASSWORD", "postgres")
            set_key(dotenv_path, "NOTIFIER_DB_DATABASE", "postgres")
            set_key(dotenv_path, "NOTIFIER_DB_PORT", "5432")

            get_config.cache_clear()
        except Exception as exc:
            logging.error("Error saving config: %s", exc)
            return False

        return True

    def __str__(self) -> str:
        """Return printable configuration with secrets masked."""
        return (
            f"SERVICE_ENDPOINT: {self.service_endpoint}\n"
            f"GITHUB_REPO_LINK: {self.github_repo_link}\n"
            f"GIT_PLATFORM: {self.git_platform}\n"
            f"AI_PROVIDER: {self.ai_provider}\n"
            f"AI_PROVIDER_TYPE: {self.ai_provider_type}\n"
            f"AI_API_KEY: {'***' if self.ai_api_key else None}\n"
            f"OLLAMA_HOST: {self.ollama_host or None}\n"
            f"OLLAMA_NUM_CTX: {self.ollama_num_ctx}\n"
            f"AI_MAX_INPUT_CHARS: {self.ai_max_input_chars}\n"
            f"HEALER_REVIEW_ENABLED: {self.healer_review_enabled}\n"
            f"HEALER_ANALYZE_ON_FAILURE_ENABLED: {self.healer_analyze_on_failure_enabled}\n"
            f"GITHUB_TOKEN: {'***' if self.github_token else None}\n"
            f"TELEGRAM_BOT_TOKEN: {'***' if self.telegram_bot_token else None}\n"
            f"DBT_PROJECT_NAME: {self.dbt_project_name}\n"
            f"BASE_BRANCH: {self.base_branch}\n"
            f"DB_DBT_USERNAME: {self.db_dbt_username}\n"
            f"DB_DBT_PASSWORD: {'***' if self.db_dbt_password else None}\n"
            f"DB_DBT_DATABASE: {self.db_dbt_database}\n"
            f"DB_DBT_PORT: {self.db_dbt_port}"
        )


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Return cached application configuration."""
    return Config()
