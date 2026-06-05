import logging
import os
import asyncio

os.environ["GIT_PYTHON_REFRESH"] = "quiet"

import github
from github import Github

from common.config import get_config

from app.push_repo import (
    extract_solution_parts,
    build_repo_file_path,
    create_branch,
    create_gitlab_branch,
    create_gitlab_merge_request,
    update_file_in_branch,
    update_file_in_gitlab_branch,
    create_pull_request,
    solution_files,
    solution_summaries,
)

from app.utils import get_context_log
from app.provider_builder import build_provider
from notifier.utils import notify_about_pr

config = get_config()


def _status(message: str) -> None:
    """Log a user-facing status line for CLI log streaming."""
    if config.healer_run_id:
        logging.info("(%s) STATUS: %s", config.healer_run_id, message)
    else:
        logging.info("STATUS: %s", message)


def _create_gitlab_request(solution_parts: list[tuple[str, str, str]], files: str) -> str:
    branch_name = create_gitlab_branch(config.base_branch)
    for solution_content, solution_file, summary in solution_parts:
        update_file_in_gitlab_branch(
            build_repo_file_path(solution_file),
            solution_content,
            branch_name,
            summary,
        )
    merge_request = create_gitlab_merge_request(branch_name, files, solution_summaries(solution_parts))
    return merge_request["web_url"]


def _create_github_request(solution_parts: list[tuple[str, str, str]], files: str) -> str:
    client = Github(auth=github.Auth.Token(config.github_token))
    repo = client.get_repo(f"{config.github_name}/{config.github_repo}")
    branch_name = create_branch(repo, config.base_branch)

    for solution_content, solution_file, summary in solution_parts:
        file_path = build_repo_file_path(solution_file)
        try:
            logging.info("Accessing file: %s", file_path)
            update_file_in_branch(repo, file_path, solution_content, branch_name, summary)
        except github.GithubException as exc:
            logging.info("GitHub API error: %s - %s", exc.status, exc.data.get("message", ""))
            raise

    pull_request = create_pull_request(repo, branch_name, files, solution_summaries(solution_parts))
    return pull_request.html_url


async def main() -> None:
    """Orchestrate solution retrieval, commit, and PR creation."""
    _status("Failure analysis started")
    context = get_context_log()
    model = build_provider(
        ai_provider=config.ai_provider,
        context=context,
        ollama_type=config.ai_provider_type,
    )
    _status("Generating solution")
    solution = model.get_solution()
    logging.info(solution)
    if not solution.strip():
        _status("Solution was not generated")
        logging.warning("No solution generated; skipping pull request creation.")
        return

    solution_parts = [
        part
        for part in extract_solution_parts(solution)
        if part[0].strip() != "NO_FIX"
    ]
    if not solution_parts:
        _status("Solution was not generated")
        logging.warning("No valid solution blocks generated; skipping pull request creation.")
        return

    _status("Solution successfully generated")
    files = solution_files(solution_parts)
    _status("Creating pull/merge request")
    if config.git_platform.lower() == "gitlab":
        request_url = _create_gitlab_request(solution_parts, files)
    else:
        request_url = _create_github_request(solution_parts, files)

    _status("Pull/merge request created successfully")
    logging.info("Pull/merge request created successfully.")

    await notify_about_pr(files, request_url)
    _status("Pull/merge request notification sent")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    asyncio.run(main())
