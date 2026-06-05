from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote
import subprocess
import logging
import json
import re
from common.config import get_config

from app.dbt_exps import (
    DBT_SOURCE_DIRS,
    DBT_SOURCE_EXTENSIONS,
    DBT_NODE_RESOURCE_TYPES,
    DbtRegularExpressions
)

config = get_config()
status_logger = logging.getLogger("uvicorn.error")
exp = DbtRegularExpressions()
CI_FETCH_DEPTH = "50"


def _status(message: str, run_id: str | None = None) -> None:
    """Log a user-facing status line for CLI log streaming."""
    if run_id:
        status_logger.info("(%s) STATUS: %s", run_id, message)
    else:
        status_logger.info("STATUS: %s", message)


def get_failed_repo_path() -> Path:
    """Return checked-out dbt project path."""
    if not config.dbt_project_name:
        raise RuntimeError("DBT_PROJECT_NAME is not configured")

    failed_repo_path = config.repo_root / config.dbt_project_name
    if not failed_repo_path.exists():
        raise RuntimeError(f"DBT project not found at {failed_repo_path}")

    return failed_repo_path


def _dedupe(items: list[str]) -> list[str]:
    """Return non-empty items without duplicates."""
    return list(dict.fromkeys(item for item in items if item))


def _clean_log_text(log_text: str | list[str] | None) -> str:
    """Normalize log text and remove ANSI escapes."""
    if isinstance(log_text, list):
        log_text = "\n".join(log_text)
    return exp.ANSI_ESCAPE_RE.sub("", str(log_text or ""))


def _normalize_dbt_source_path(raw_path: str | None) -> str | None:
    """Normalize raw path to a dbt source path."""
    if not raw_path:
        return None

    path = raw_path.strip().strip("`'\".,;:()[]{}").replace("\\", "/")
    if not path.lower().endswith(DBT_SOURCE_EXTENSIONS):
        return None

    for source_dir in DBT_SOURCE_DIRS:
        marker = f"{source_dir}/"
        marker_index = path.find(marker)
        if marker_index >= 0:
            return path[marker_index:]

    if "/target/" in f"/{path}":
        return None

    return path


def _read_dbt_manifest() -> dict:
    """Read dbt manifest from failed repository."""
    try:
        manifest_path = get_failed_repo_path() / "target" / "manifest.json"
    except RuntimeError:
        return {}

    if not manifest_path.exists():
        return {}

    try:
        with open(manifest_path, mode="r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Unable to read dbt manifest at %s: %s", manifest_path, exc)
        return {}


def _resolve_manifest_source(resource_name: str | None) -> str | None:
    """Resolve dbt resource name through manifest metadata."""
    if not resource_name:
        return None

    name = resource_name.strip().strip("`'\".,;:()[]{}").split(".")[-1]
    if not name:
        return None

    manifest = _read_dbt_manifest()
    for node in manifest.get("nodes", {}).values():
        original_file_path = node.get("original_file_path")
        if node.get("resource_type") not in DBT_NODE_RESOURCE_TYPES or not original_file_path:
            continue

        identifiers = {
            str(node.get("name") or ""),
            str(node.get("alias") or ""),
            Path(original_file_path).stem,
        }
        if name in identifiers:
            return _normalize_dbt_source_path(original_file_path)

    for macro in manifest.get("macros", {}).values():
        original_file_path = macro.get("original_file_path")
        if not original_file_path:
            continue

        identifiers = {
            str(macro.get("name") or ""),
            Path(original_file_path).stem,
        }
        if name in identifiers:
            return _normalize_dbt_source_path(original_file_path)

    return None


def _test_model_dependencies(manifest: dict, test_node: dict | None) -> list[tuple[str, dict]]:
    """Return dbt model-like dependencies for a test node."""
    nodes = manifest.get("nodes", {})
    dependencies = []
    for dep_id in (test_node or {}).get("depends_on", {}).get("nodes", []):
        dep_node = nodes.get(dep_id)
        if dep_node and dep_node.get("resource_type") in ("model", "snapshot", "seed"):
            dependencies.append((dep_id, dep_node))
    return dependencies


def _is_relationship_test_node(test_node: dict | None) -> bool:
    """Return whether a manifest test node is a dbt relationships test."""
    if not test_node:
        return False

    metadata = test_node.get("test_metadata") or {}
    if metadata.get("name") == "relationships":
        return True

    return str(test_node.get("name") or "").startswith("relationships_")


def _relationship_error_side(error_log: str | None) -> str | None:
    """Return the relationships test side that appears to have failed."""
    text = _clean_log_text(error_log)
    if re.search(r"\bas\s+from_field\b|\bfrom_field\b", text, flags=re.IGNORECASE):
        return "from"
    if re.search(r"\bas\s+to_field\b|\bto_field\b", text, flags=re.IGNORECASE):
        return "to"
    return None


def _test_attached_model(manifest: dict, test_node: dict | None) -> tuple[str, dict] | None:
    """Return the model attached to a generic dbt test when available."""
    attached_id = (test_node or {}).get("attached_node")
    attached_node = manifest.get("nodes", {}).get(attached_id)
    if attached_node and attached_node.get("resource_type") in ("model", "snapshot", "seed"):
        return attached_id, attached_node
    return None


def _ref_name(value: str | None) -> str | None:
    """Extract a dbt ref name from test metadata text."""
    match = re.search(
        r"\bref\s*\(\s*['\"]([^'\"]+)['\"](?:\s*,\s*['\"]([^'\"]+)['\"])?",
        str(value or ""),
    )
    return (match.group(2) or match.group(1)) if match else None


def _relationship_test_model_roles(
    manifest: dict,
    test_node: dict | None,
    test_name: str | None = None,
) -> dict[str, tuple[str, dict]]:
    """Return source and referenced model roles for a relationships test."""
    if not _is_relationship_test_node(test_node):
        return {}

    dependencies = _test_model_dependencies(manifest, test_node)
    if not dependencies:
        return {}

    metadata = (test_node or {}).get("test_metadata") or {}
    kwargs = metadata.get("kwargs") or {}
    ref_name = _ref_name(kwargs.get("to"))
    roles: dict[str, tuple[str, dict]] = {}

    for dep_id, dep_node in dependencies:
        dep_identifiers = {
            str(dep_node.get("name") or ""),
            str(dep_node.get("alias") or ""),
            Path(str(dep_node.get("original_file_path") or "")).stem,
        }
        if ref_name and ref_name in dep_identifiers:
            roles["to"] = (dep_id, dep_node)
            break

    attached = _test_attached_model(manifest, test_node)
    if attached:
        roles["from"] = attached

    names = [
        str(test_name or ""),
        str((test_node or {}).get("name") or ""),
        str((test_node or {}).get("alias") or ""),
    ]
    for name in names:
        clean_name = name.strip().strip("`'\".,;:()[]{}").split(".")[-1]
        if not clean_name:
            continue
        for dep_id, dep_node in sorted(
            dependencies,
            key=lambda item: len(str(item[1].get("name") or "")),
            reverse=True,
        ):
            dep_name = str(dep_node.get("name") or Path(str(dep_node.get("original_file_path") or "")).stem)
            if clean_name.startswith(f"relationships_{dep_name}_"):
                roles["from"] = (dep_id, dep_node)
                break
        if "from" in roles:
            break

    if len(dependencies) == 2:
        if "from" in roles and "to" not in roles:
            roles["to"] = next(dep for dep in dependencies if dep[0] != roles["from"][0])
        if "to" in roles and "from" not in roles:
            roles["from"] = next(dep for dep in dependencies if dep[0] != roles["to"][0])

    return roles


def _resolve_test_node_source(
    manifest: dict,
    test_node: dict,
    test_name: str,
    error_log: str | None,
) -> str | None:
    """Resolve the most relevant source file for a manifest test node."""
    if _is_relationship_test_node(test_node):
        roles = _relationship_test_model_roles(manifest, test_node, test_name)
        side = _relationship_error_side(error_log) or "from"
        preferred = roles.get(side) or roles.get("from") or roles.get("to")
        if preferred:
            resolved = _normalize_dbt_source_path(preferred[1].get("original_file_path"))
            if resolved:
                return resolved

    attached = _test_attached_model(manifest, test_node)
    if attached:
        resolved = _normalize_dbt_source_path(attached[1].get("original_file_path"))
        if resolved:
            return resolved

    for _, dep_node in _test_model_dependencies(manifest, test_node):
        resolved = _normalize_dbt_source_path(dep_node.get("original_file_path"))
        if resolved:
            return resolved

    return None


def _resolve_test_failure_source(
    test_name: str | None,
    raw_path: str | None,
    error_log: str | None = None,
) -> str | None:
    """Resolve tested source file for dbt test failure."""
    if not test_name:
        return None

    name = test_name.strip().strip("`'\".,;:()[]{}").split(".")[-1]
    manifest = _read_dbt_manifest()
    for node_id, node in manifest.get("nodes", {}).items():
        identifiers = {str(node.get("name") or ""), str(node.get("alias") or "")}
        if node.get("resource_type") != "test":
            continue
        if (
            name not in identifiers
            and f".{name}." not in node_id
            and not node_id.endswith(f".{name}")
        ):
            continue
        resolved = _resolve_test_node_source(manifest, node, name, error_log)
        if resolved:
            return resolved

    try:
        failed_repo_path = get_failed_repo_path()
    except RuntimeError:
        failed_repo_path = None

    if failed_repo_path:
        candidates = sorted(
            (path for path in (failed_repo_path / "models").rglob("*.sql")),
            key=lambda path: len(path.stem),
            reverse=True,
        )
        for path in candidates:
            if f"_{path.stem}_" in f"_{name}_":
                return path.relative_to(failed_repo_path).as_posix()

    resolved_path = _normalize_dbt_source_path(raw_path)
    if resolved_path and (resolved_path.endswith(".sql") or resolved_path.startswith("tests/")):
        return resolved_path
    return None


def _resolve_source_file(resource_name: str | None) -> str | None:
    """Resolve dbt source file by resource name."""
    resolved = _resolve_manifest_source(resource_name)
    if resolved:
        return resolved

    if not resource_name:
        return None

    name = resource_name.strip().strip("`'\".,;:()[]{}").split(".")[-1]
    if not name:
        return None

    try:
        failed_repo_path = get_failed_repo_path()
    except RuntimeError:
        return None

    matches = sorted(
        path for path in failed_repo_path.rglob(f"{name}.sql")
        if "target" not in path.parts and "dbt_packages" not in path.parts
    )
    if not matches:
        return None

    return matches[0].relative_to(failed_repo_path).as_posix()


def _resolve_dbt_error_reference(
    resource_type: str | None,
    resource_name: str | None,
    raw_path: str | None,
    error_log: str | None = None,
) -> str | None:
    """Resolve dbt error reference to source file path."""
    if resource_type and resource_type.lower() == "test":
        return _resolve_test_failure_source(resource_name, raw_path, error_log)

    return _normalize_dbt_source_path(raw_path) or _resolve_source_file(resource_name)


def get_error_files_from_dbt_log(log_text: str | list[str] | None) -> list[str]:
    """Extract failing dbt source files from dbt's own error lines."""
    text = _clean_log_text(log_text)
    if not text:
        return []

    files = []
    unresolved_test_failure = False
    for pattern in exp.DBT_EXPLICIT_ERROR_PATTERNS:
        for match in pattern.finditer(text):
            resource = match.groupdict().get("resource")
            resolved = _resolve_dbt_error_reference(
                resource,
                match.groupdict().get("name"),
                match.groupdict().get("path"),
                text,
            )
            if resolved:
                files.append(resolved)
            elif resource and resource.lower() == "test":
                unresolved_test_failure = True

    if files or unresolved_test_failure:
        return _dedupe(files)

    for match in exp.DBT_SOURCE_PATH_RE.finditer(text):
        resolved = _normalize_dbt_source_path(match.group("path"))
        if resolved:
            files.append(resolved)

    if files:
        return _dedupe(files)

    for match in exp.DBT_STATUS_MODEL_RE.finditer(text):
        relation = match.group("relation")
        resolved = _resolve_source_file(relation.split(".")[-1])
        if resolved:
            files.append(resolved)

    for match in exp.DBT_NATURAL_MODEL_RE.finditer(text):
        resolved = _resolve_source_file(match.group("name"))
        if resolved:
            files.append(resolved)

    return _dedupe(files)

def get_context_log() -> str:
    """Return the uploaded CI dbt log or the local dbt log."""
    for path in (config.uploaded_dbt_log, config.dbt_log):
        if not path.exists():
            continue
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logging.warning("Unable to read dbt log at %s: %s", path, exc)

    return ""


def get_instruction(name: str) -> str:
    """
    Get available instructions:
    - handle_solution
    """
    path = Path(__file__).resolve().parents[1] / "common" / "instructions"

    with open(path / f"{name}.md", mode="r", encoding="utf-8") as f:
        return f.read()


def has_dbt_dependencies(dbt_project_path: Path) -> bool:
    """Check whether dbt project declares package dependencies."""
    return any((dbt_project_path / name).exists() for name in ("packages.yml", "dependencies.yml"))


def prepare_dbt_metadata(dbt_project_path: Path, run_id: str | None = None) -> None:
    """Refresh dbt dependencies and manifest metadata."""
    if has_dbt_dependencies(dbt_project_path):
        try:
            subprocess.run(["dbt", "deps"], cwd=dbt_project_path, check=True)
        except subprocess.CalledProcessError as e:
            logging.warning(
                f"dbt deps failed; continuing without refreshed packages. "
                f"Lineage context may be unavailable. Error: {e}"
            )
    else:
        logging.info("No dbt package config found; skipping dbt deps.")

    try:
        _status("Running dbt parse", run_id)
        subprocess.run(["dbt", "--show-all-deprecations", "parse"], cwd=dbt_project_path, check=True)
        _status("dbt parse completed", run_id)
    except subprocess.CalledProcessError as e:
        _status("dbt parse failed", run_id)
        logging.warning(
            f"dbt parse failed; continuing without manifest lineage context. Error: {e}"
        )


def _authenticated_repo_url(repo: str) -> str:
    """Return clone URL with configured token when available."""
    if not config.github_token or not repo.startswith("https://"):
        return repo

    token = quote(config.github_token, safe="")
    if config.git_platform.lower() == "gitlab":
        return repo.replace("https://", f"https://oauth2:{token}@")
    return repo.replace("https://", f"https://{config.github_name}:{token}@")


def _ci_repo_root(repo: str, run_id: str | None = None) -> Path:
    """Return CI workspace root for a repository and optional run id."""
    workdir = Path(Path.home() / ".failedrepo")
    workdir.mkdir(parents=True, exist_ok=True)
    repo_name = repo.split("/")[-1].replace(".git", "")
    repo_dir = workdir / repo_name
    if run_id:
        repo_dir = repo_dir / run_id
    return repo_dir


def _is_empty_commit(commit_hash: str | None) -> bool:
    """Return whether CI provided an empty all-zero commit placeholder."""
    return not commit_hash or set(commit_hash.strip()) == {"0"}


def clone_repo_from_ci(
    repo: str,
    commit_hash: str,
    dbt_path: str,
    log_file: BinaryIO | None = None,
    run_id: str | None = None,
    branch_name: str | None = None,
    diff_base: str | None = None,
) -> None:
    """Clone CI repository workspace and optionally store uploaded dbt log."""
    repo_dir = _ci_repo_root(repo, run_id)
    auth_repo = _authenticated_repo_url(repo)

    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", CI_FETCH_DEPTH, auth_repo, str(repo_dir)], check=True)

    if branch_name:
        subprocess.run(
            [
                "git",
                "fetch",
                "origin",
                f"refs/heads/{branch_name}:refs/remotes/origin/{branch_name}",
                "--depth",
                CI_FETCH_DEPTH,
            ],
            cwd=repo_dir,
            check=True,
        )
    else:
        subprocess.run(["git", "fetch", "origin", commit_hash, "--depth", CI_FETCH_DEPTH], cwd=repo_dir, check=False)

    if not _is_empty_commit(diff_base):
        subprocess.run(["git", "fetch", "origin", diff_base, "--depth", CI_FETCH_DEPTH], cwd=repo_dir, check=False)
        (repo_dir / ".healer_diff_base").write_text(diff_base.strip(), encoding="utf-8")

    subprocess.run(
        [
            "git",
            "fetch",
            "origin",
            f"{config.base_branch}:refs/remotes/origin/{config.base_branch}",
            "--depth",
            CI_FETCH_DEPTH,
        ],
        cwd=repo_dir,
        check=False,
    )
    subprocess.run(["git", "checkout", commit_hash], cwd=repo_dir, check=True)

    if log_file is not None:
        logs_dir = repo_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        with open(logs_dir / "payload_dbt.log", "wb") as f:
            f.write(log_file.file.read())

    failed_repo_path = repo_dir / dbt_path
    if not failed_repo_path.exists():
        raise RuntimeError(f"DBT project not found at {failed_repo_path}")

    prepare_dbt_metadata(failed_repo_path, run_id)
