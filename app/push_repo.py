from pathlib import Path
from datetime import datetime
import re
import logging
import os
from urllib.parse import quote

os.environ["GIT_PYTHON_REFRESH"] = "quiet"

import github
import requests

from common.config import get_config

config = get_config()

SolutionPart = tuple[str, str, str]


def _timestamp() -> str:
    """Return timestamp suffix for generated git objects."""
    return datetime.now().strftime('%Y%m%d_%H%M%S')


def extract_solution_parts(solution: str) -> list[SolutionPart]:
    """Parse solution content, target file path, and commit summary."""
    solution_parts = []

    for idx, sol in enumerate(solution.split("----")):
        content_match = re.search(r"<solution>(.*?)</solution>", sol, re.DOTALL)
        file_match = re.search(r"<file>(.*?)</file>", sol, re.DOTALL)
        summary_match = re.search(r"<summary>(.*?)</summary>", sol, re.DOTALL)

        if not content_match or not file_match:
            logging.warning("Solution part №%s must contain <solution> and <file> blocks", idx + 1)
            continue

        solution_parts.append((
            content_match.group(1),
            file_match.group(1),
            summary_match.group(1) if summary_match else "",
        ))

    return solution_parts


def build_repo_file_path(raw_path: str) -> str:
    """Normalize path to repository format."""
    normalized = raw_path.strip().replace("\\", "/")

    if config.dbt_project_name and normalized.startswith(config.dbt_project_name):
        return normalized

    return str(Path(config.dbt_project_name) / normalized).replace("\\", "/")


def solution_files(solution_parts: list[SolutionPart]) -> str:
    """Return printable target file list."""
    return ", ".join(part[1].strip() for part in solution_parts)


def _clean_summary(summary: str) -> str:
    """Normalize model-provided summary for commit and request bodies."""
    lines = [line.strip() for line in (summary or "").splitlines() if line.strip()]
    return "\n".join(lines)[:1600]


def _fallback_summary(file_path: str) -> str:
    """Return fallback details when model summary is unavailable."""
    return "\n".join(
        [
            f"Изменено: обновлен файл {file_path}.",
            f"Ошибка: dbt-ошибка была найдена в CI-логах для {file_path}.",
            "Причина: сгенерированный патч должен восстановить корректное выполнение dbt build/test.",
        ]
    )


def commit_message(file_path: str, summary: str) -> str:
    """Build detailed commit message for generated patch."""
    details = _clean_summary(summary) or _fallback_summary(file_path)
    return (
        f"Исправлена dbt-ошибка в {file_path}\n\n"
        f"{details}\n\n"
        f"Сгенерировано dbt-healer: {_timestamp()}"
    )


def solution_summaries(solution_parts: list[SolutionPart]) -> str:
    """Return printable patch summaries for pull or merge request body."""
    sections = []
    for _, file_path, summary in solution_parts:
        file_path = file_path.strip()
        sections.append(f"### {file_path}\n{_clean_summary(summary) or _fallback_summary(file_path)}")
    return "\n\n".join(sections)


def create_branch(repo: github.Repository.Repository, base_branch: str) -> str:
    """Create dbt-healer branch from base."""
    branch_name = f"healer/dbt-fix-patch-{_timestamp()}"
    base_ref = repo.get_git_ref(f"heads/{base_branch}")
    repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_ref.object.sha)
    return branch_name


def update_file_in_branch(
    repo: github.Repository.Repository,
    file_path: str,
    content: str,
    branch: str,
    summary: str = "",
) -> None:
    """Update target file in specified branch."""
    current = repo.get_contents(file_path, ref=branch)
    repo.update_file(
        path=current.path,
        message=commit_message(file_path, summary),
        content=content,
        sha=current.sha,
        branch=branch,
    )


def create_pull_request(
    repo: github.Repository.Repository,
    branch: str,
    solution_file: str,
    summary: str = "",
) -> github.PullRequest.PullRequest:
    """Open pull request for the generated patch."""
    return repo.create_pull(
        title=f"Автоисправление dbt-healer {_timestamp()}",
        body=f"Файлы исправления: {solution_file}\n\n{summary or 'Описание исправления отсутствует.'}",
        head=branch,
        base=config.base_branch,
    )


def _gitlab_project() -> tuple[str, str]:
    """Return GitLab API base URL and encoded project path."""
    repo_link = config.github_repo_link.strip().removesuffix(".git").rstrip("/")
    if repo_link.startswith("git@"):
        host, project_path = repo_link.removeprefix("git@").split(":", 1)
    else:
        match = re.match(r"^(?:https?://)?(?P<host>[^/\s]+)/(?P<path>.+)$", repo_link)
        if not match:
            raise ValueError(f"Unsupported GitLab repository link: {config.github_repo_link}")
        host, project_path = match.group("host"), match.group("path")

    return f"https://{host}/api/v4", quote(project_path.strip("/"), safe="")


def _gitlab_request(method: str, path: str, **kwargs) -> dict:
    """Call GitLab API with configured token."""
    api_base, project_path = _gitlab_project()
    response = requests.request(
        method,
        f"{api_base}/projects/{project_path}{path}",
        headers={"PRIVATE-TOKEN": config.github_token},
        timeout=120,
        **kwargs,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = response.text.strip()
        if detail:
            raise RuntimeError(f"GitLab API request failed: {detail}") from exc
        raise
    return response.json() if response.text else {}


def create_gitlab_branch(base_branch: str) -> str:
    """Create GitLab dbt-healer branch from base."""
    branch_name = f"healer/dbt-fix-patch-{_timestamp()}"
    _gitlab_request("POST", "/repository/branches", data={"branch": branch_name, "ref": base_branch})
    return branch_name


def update_file_in_gitlab_branch(file_path: str, content: str, branch: str, summary: str = "") -> None:
    """Update target file in GitLab branch."""
    encoded_path = quote(file_path, safe="")
    _gitlab_request(
        "PUT",
        f"/repository/files/{encoded_path}",
        data={
            "branch": branch,
            "content": content,
            "commit_message": commit_message(file_path, summary),
        },
    )


def create_gitlab_merge_request(branch: str, solution_file: str, summary: str = "") -> dict:
    """Open GitLab merge request for the generated patch."""
    return _gitlab_request(
        "POST",
        "/merge_requests",
        data={
            "source_branch": branch,
            "target_branch": config.base_branch,
            "title": f"Автоисправление dbt-healer {_timestamp()}",
            "description": f"Файлы исправления: {solution_file}\n\n{summary or 'Описание исправления отсутствует.'}",
        },
    )
