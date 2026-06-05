import logging
import re
import subprocess
from pathlib import Path

from app import utils

REVIEW_BLOCK_RE = re.compile(r"<review>(.*?)</review>", re.DOTALL)
MAX_REVIEW_TOTAL_CHARS = 32000
MAX_REVIEW_FILE_DIFF_CHARS = 12000
MAX_REVIEW_SOURCE_CHARS = 8000
REVIEW_SOURCE_EXTENSIONS = {".sql", ".yml", ".yaml"}


def _is_empty_commit(commit_hash: str | None) -> bool:
    """Return whether CI provided an empty all-zero commit placeholder."""
    return not commit_hash or set(commit_hash.strip()) == {"0"}


def _git_revision_exists(repo_path, revision: str) -> bool:
    """Check whether git revision exists in repository."""
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", revision],
        cwd=repo_path,
        capture_output=True,
    ).returncode == 0


def _git_output_or_empty(repo_path, args: list[str]) -> str:
    """Run git and return stdout or empty string on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _ci_diff_base(repo_path) -> str | None:
    """Return CI-provided diff base when it is available."""
    path = repo_path / ".healer_diff_base"
    if not path.is_file():
        return None
    diff_base = path.read_text(encoding="utf-8").strip()
    if _is_empty_commit(diff_base) or not _git_revision_exists(repo_path, diff_base):
        return None
    logging.info("Review diff base selected: %s (CI diff base)", diff_base)
    return diff_base


def _base_revision(repo_path) -> str | None:
    """Return best available base revision for review diff."""
    ci_base = _ci_diff_base(repo_path)
    if ci_base:
        return ci_base

    base_ref = f"origin/{utils.config.base_branch}"
    if _git_revision_exists(repo_path, base_ref):
        merge_base = _git_output_or_empty(repo_path, ["merge-base", base_ref, "HEAD"])
        if merge_base:
            logging.info("Review diff base selected: %s (merge-base of %s and HEAD)", merge_base, base_ref)
            return merge_base
        logging.warning("Review merge-base for %s and HEAD is unavailable; using %s directly.", base_ref, base_ref)
        return base_ref

    if _git_revision_exists(repo_path, "HEAD^"):
        logging.info("Review diff base selected: HEAD^")
        return "HEAD^"

    logging.warning("Review diff base is unavailable; no review context can be built.")
    return None


def _git_output(repo_path, args: list[str]) -> str:
    """Run git and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _truncate(text: str, max_chars: int) -> str:
    """Trim long review sections while keeping the truncation explicit."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[truncated]"


def _changed_files(repo_path, base: str, pathspec: str | None = None) -> list[tuple[str, str]]:
    """Return changed file status and path from git diff."""
    args = ["diff", "--name-status", base, "HEAD", "--"]
    if pathspec:
        args.append(pathspec)
    output = _git_output(repo_path, args)
    files = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        files.append((parts[0], parts[-1]))
    return files


def _current_source(repo_path, file_path: str) -> str:
    """Return current file source for dbt SQL/YAML files."""
    path = repo_path / file_path
    if path.suffix.lower() not in REVIEW_SOURCE_EXTENSIONS or not path.is_file():
        return ""
    return _truncate(path.read_text(encoding="utf-8", errors="replace"), MAX_REVIEW_SOURCE_CHARS)


def _review_file_context(repo_path, base: str, status: str, file_path: str) -> str:
    """Build independent review context for one changed file."""
    diff = _git_output(repo_path, ["diff", "--unified=80", base, "HEAD", "--", file_path]) or "NO_DIFF"
    source = _current_source(repo_path, file_path)
    source_block = f"\n<CURRENT_FILE>\n{source}\n</CURRENT_FILE>" if source else ""
    return (
        f"<REVIEW_FILE path=\"{file_path}\" status=\"{status}\">\n"
        f"<FILE_DIFF>\n{_truncate(diff, MAX_REVIEW_FILE_DIFF_CHARS)}\n</FILE_DIFF>"
        f"{source_block}\n"
        f"</REVIEW_FILE>"
    )


def build_review_context() -> str:
    """Build compact review context from changed dbt project files."""
    dbt_project_path = utils.get_failed_repo_path()
    repo_path = Path(_git_output(dbt_project_path, ["rev-parse", "--show-toplevel"]))
    try:
        project_pathspec = dbt_project_path.relative_to(repo_path).as_posix()
    except ValueError:
        project_pathspec = None

    base = _base_revision(repo_path)
    if not base:
        return ""

    changed_files = _changed_files(repo_path, base, project_pathspec)
    logging.info(
        "Review changed files from %s to HEAD (%s): %s",
        base,
        len(changed_files),
        ", ".join(path for _, path in changed_files) or "NO_CHANGED_FILES",
    )
    if not changed_files and project_pathspec:
        all_changed_files = _changed_files(repo_path, base)
        logging.info(
            "Review found no changed files under %s; all changed files from %s to HEAD: %s",
            project_pathspec,
            base,
            ", ".join(path for _, path in all_changed_files) or "NO_CHANGED_FILES",
        )
    changed_file_list = "\n".join(f"{status}\t{path}" for status, path in changed_files) or "NO_CHANGED_FILES"
    review_files = "\n\n".join(
        _review_file_context(repo_path, base, status, path)
        for status, path in changed_files
    ) or "NO_REVIEW_FILES"

    context = (
        f"<BASE_REVISION>{base}</BASE_REVISION>\n\n"
        f"<CHANGED_FILES>\n{changed_file_list}\n</CHANGED_FILES>\n\n"
        f"<REVIEW_FILES>\n{review_files}\n</REVIEW_FILES>"
    )
    return _truncate(context, MAX_REVIEW_TOTAL_CHARS)


def review_finding(response: str) -> str:
    """Return review finding text or empty string."""
    match = REVIEW_BLOCK_RE.search(response or "")
    if not match:
        return ""
    text = match.group(1).strip()
    if not text or text.rstrip(".").upper() == "NO_FINDINGS":
        return ""
    return text
