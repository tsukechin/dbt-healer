from pathlib import Path
import json
import logging
import re
import subprocess

from app import utils
from app.rag import (
    extract_error_signals,
    extract_macro_calls,
    node_symbols,
    relevance_score,
    structured_sql_context,
)

MODEL_TYPES = {"model", "snapshot", "seed"}
MAX_DIAGNOSTIC_MODELS = 1
MAX_IMPACT_MODELS = 1
MAX_UPSTREAM_DEPTH = 1
MAX_DOWNSTREAM_DEPTH = 1
COMPILED_SQL_RE = re.compile(r"compiled code at\s+(?P<path>target/[^\s]+\.sql)", re.IGNORECASE)
ERROR_LOCATION_RE = re.compile(r"\b(?:line|LINE)\s+\d+|\[\d+:\d+\]")
COLUMN_ERROR_RE = re.compile(
    r"\b(?:column|field|identifier)\b|does not exist|ambiguous column|invalid identifier",
    re.IGNORECASE,
)


def _read_source_text(path: Path) -> str:
    """Read source file with utf-8 fallback."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _relative_to_failed_repo(path: Path) -> str:
    """Return path relative to failed repository when possible."""
    try:
        return path.relative_to(utils.get_failed_repo_path()).as_posix()
    except (RuntimeError, ValueError):
        return path.as_posix()


def _git_revision_exists(repo_path: Path, revision: str) -> bool:
    """Check whether git revision exists in repository."""
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", revision],
        cwd=repo_path,
        capture_output=True,
    ).returncode == 0


def _get_file_diff(path: Path) -> str:
    """Return diff for source file against available base revision."""
    try:
        failed_repo_path = utils.get_failed_repo_path()
        relative_path = path.relative_to(failed_repo_path)
    except (RuntimeError, ValueError):
        return "NO_DIFF"

    base_revision = next(
        (
            revision
            for revision in ("HEAD^", f"origin/{utils.config.base_branch}")
            if _git_revision_exists(failed_repo_path, revision)
        ),
        None,
    )
    if not base_revision:
        return "NO_DIFF"

    result = subprocess.run(
        ["git", "diff", base_revision, "--", relative_path.as_posix()],
        cwd=failed_repo_path,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        logging.warning("Unable to get git diff for %s: %s", relative_path, result.stderr.strip())
        return "NO_DIFF"

    return result.stdout.strip() or "NO_DIFF"


def _error_log() -> str:
    log = utils.get_context_log()
    if isinstance(log, list):
        return "\n".join(log)
    return str(log or "")


def _test_failure_name(error_log: str) -> str | None:
    for match in utils.exp.DBT_FAILURE_RE.finditer(error_log):
        if match.groupdict().get("resource", "").lower() == "test":
            return match.groupdict().get("name")
    return None


def _needs_lineage_context(error_log: str) -> bool:
    return bool(_test_failure_name(error_log) or COLUMN_ERROR_RE.search(error_log))


def _context_text(source: str, signals: set[str]) -> str:
    return structured_sql_context(source, signals)


def _read_manifest(failed_repo_path: Path) -> dict:
    manifest_path = failed_repo_path / "target" / "manifest.json"
    if not manifest_path.exists():
        logging.warning("dbt manifest not found at %s; skipping lineage context.", manifest_path)
        return {}

    try:
        with manifest_path.open(mode="r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Unable to read dbt manifest at %s: %s", manifest_path, exc)
        return {}


def _find_node(manifest: dict, file: str) -> tuple[str | None, dict | None]:
    normalized = file.replace("\\", "/")
    stem = Path(file).stem

    for node_id, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") not in MODEL_TYPES or not node.get("original_file_path"):
            continue
        path = node.get("original_file_path", "").replace("\\", "/")
        if normalized == path or stem in {node.get("name"), Path(path).stem}:
            return node_id, node

    return None, None


def _find_test_node(manifest: dict, error_log: str) -> tuple[str | None, dict | None]:
    name = _test_failure_name(error_log)
    if not name:
        return None, None

    name = name.strip().strip("`'\".,;:()[]{}").split(".")[-1]
    for node_id, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "test":
            continue
        identifiers = {str(node.get("name") or ""), str(node.get("alias") or "")}
        if name in identifiers or f".{name}." in node_id or node_id.endswith(f".{name}"):
            return node_id, node

    return None, None


def _model_source(failed_repo_path: Path, node: dict) -> str:
    return _read_source_text(failed_repo_path / node["original_file_path"])


def _upstream_model_ids(manifest: dict, node_id: str) -> list[str]:
    nodes = manifest.get("nodes", {})
    return [
        parent_id
        for parent_id in nodes.get(node_id, {}).get("depends_on", {}).get("nodes", [])
        if nodes.get(parent_id, {}).get("resource_type") in MODEL_TYPES
    ]


def _downstream_model_ids(manifest: dict, node_id: str) -> list[str]:
    nodes = manifest.get("nodes", {})
    return [
        child_id
        for child_id in manifest.get("child_map", {}).get(node_id, [])
        if nodes.get(child_id, {}).get("resource_type") in MODEL_TYPES
    ]


def _test_model_ids(manifest: dict, test_node: dict | None) -> list[str]:
    nodes = manifest.get("nodes", {})
    return [
        dep_id
        for dep_id in (test_node or {}).get("depends_on", {}).get("nodes", [])
        if nodes.get(dep_id, {}).get("resource_type") in MODEL_TYPES
    ]


def _ranked_model_ids(failed_repo_path: Path, manifest: dict, node_ids: list[str], signals: set[str]) -> list[str]:
    return sorted(
        node_ids,
        key=lambda node_id: relevance_score(
            _model_source(failed_repo_path, manifest["nodes"][node_id]),
            manifest["nodes"][node_id].get("name", ""),
            manifest["nodes"][node_id].get("original_file_path", ""),
            signals,
        ),
        reverse=True,
    )


def _model_signals(failed_repo_path: Path, manifest: dict, node_id: str) -> set[str]:
    node = manifest["nodes"][node_id]
    return node_symbols(node["name"], node["original_file_path"], _model_source(failed_repo_path, node))


def _flat_text(value) -> str:
    if isinstance(value, dict):
        return " ".join(_flat_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flat_text(item) for item in value)
    return str(value or "")


def _test_signals(manifest: dict, test_node: dict | None) -> set[str]:
    if not test_node:
        return set()

    parts = [
        test_node.get("name"),
        test_node.get("column_name"),
        _flat_text(test_node.get("test_metadata")),
    ]
    for model_id in _test_model_ids(manifest, test_node):
        node = manifest["nodes"][model_id]
        parts += [node.get("name"), node.get("alias"), Path(node.get("original_file_path", "")).stem]

    return extract_error_signals(" ".join(str(part or "") for part in parts))


def _test_failure_context(manifest: dict, test_node: dict | None) -> str:
    if not test_node:
        return ""

    metadata = test_node.get("test_metadata") or {}
    test_type = metadata.get("name") or str(test_node.get("name", "")).split("_", 1)[0]
    lines = [
        f"name: {test_node.get('name')}",
        f"type: {test_type}",
        f"schema_file: {test_node.get('original_file_path')}",
    ]
    if test_node.get("column_name"):
        lines.append(f"column: {test_node.get('column_name')}")

    for key, value in (metadata.get("kwargs") or {}).items():
        lines.append(f"{key}: {value}")

    for model_id in _test_model_ids(manifest, test_node):
        node = manifest["nodes"][model_id]
        lines.append(f"depends_on_model: {node.get('name')} ({node.get('original_file_path')})")

    return "<DBT_TEST_FAILURE>\n" + "\n".join(lines) + "\n</DBT_TEST_FAILURE>"


def _relationship_side_label(side: str | None) -> str:
    """Return dbt relationships compiled-column label for a side."""
    if side == "from":
        return "from_field"
    if side == "to":
        return "to_field"
    return "unknown"


def _relationship_error_hints(error_log: str) -> list[str]:
    """Extract compact dbt error hints useful for relationships tests."""
    hints = []
    missing = list(
        dict.fromkeys(
            match.group(1)
            for match in re.finditer(
                r"\b(?:column|field|identifier)\s+\"([^\"]+)\"",
                error_log,
                flags=re.IGNORECASE,
            )
        )
    )
    if missing:
        hints.append(f"missing_identifier: {', '.join(missing)}")

    hint_match = re.search(r"\bHINT:\s*(.+)", error_log)
    if hint_match:
        hints.append(f"db_hint: {hint_match.group(1).strip()}")

    return hints


def _relationship_test_context(manifest: dict, test_node: dict | None, error_log: str) -> str:
    """Build narrow source/target context for dbt relationships tests."""
    if not utils._is_relationship_test_node(test_node):
        return ""

    roles = utils._relationship_test_model_roles(manifest, test_node, _test_failure_name(error_log))
    metadata = (test_node or {}).get("test_metadata") or {}
    kwargs = metadata.get("kwargs") or {}
    from_column = (test_node or {}).get("column_name") or kwargs.get("column_name")
    to_column = kwargs.get("field")
    side = utils._relationship_error_side(error_log)

    lines = [
        f"name: {(test_node or {}).get('name')}",
        "type: relationships",
        f"error_side: {_relationship_side_label(side)}",
    ]

    from_role = roles.get("from")
    if from_role:
        from_node = from_role[1]
        lines.append(f"from_model: {from_node.get('name')} ({from_node.get('original_file_path')})")
    if from_column:
        lines.append(f"from_column: {from_column}")

    to_role = roles.get("to")
    if to_role:
        to_node = to_role[1]
        lines.append(f"to_model: {to_node.get('name')} ({to_node.get('original_file_path')})")
    if to_column:
        lines.append(f"to_column: {to_column}")

    lines.extend(_relationship_error_hints(error_log))
    lines.append(
        "resolution_rule: from_field means fix from_model/from_column; "
        "to_field means fix to_model/to_column."
    )

    return "<RELATIONSHIP_TEST_CONTEXT>\n" + "\n".join(lines) + "\n</RELATIONSHIP_TEST_CONTEXT>"


def _lineage_model_ids(
    failed_repo_path: Path,
    manifest: dict,
    root_id: str,
    signals: set[str],
    next_ids,
    max_depth: int,
    max_models: int,
    relevance_after_first: bool = False,
) -> list[tuple[str, int, set[str]]]:
    selected = []
    seen = {root_id}
    queue = [(root_id, 0, signals)]

    while queue and len(selected) < max_models:
        parent_id, depth, parent_signals = queue.pop(0)
        if depth >= max_depth:
            continue

        ranked_ids = _ranked_model_ids(
            failed_repo_path,
            manifest,
            next_ids(manifest, parent_id),
            parent_signals,
        )
        for node_id in ranked_ids:
            if node_id in seen:
                continue
            node = manifest["nodes"][node_id]
            if relevance_after_first and depth > 0:
                score = relevance_score(
                    _model_source(failed_repo_path, node),
                    node["name"],
                    node["original_file_path"],
                    parent_signals,
                )
                if score <= 0:
                    continue

            seen.add(node_id)
            selected.append((node_id, depth + 1, parent_signals))
            queue.append(
                (
                    node_id,
                    depth + 1,
                    _model_signals(failed_repo_path, manifest, node_id),
                )
            )
            if len(selected) >= max_models:
                break

    return selected


def _node_context(
    failed_repo_path: Path,
    manifest: dict,
    node_id: str,
    signals: set[str],
    label: str,
    depth: int,
) -> str:
    node = manifest["nodes"][node_id]
    source = _model_source(failed_repo_path, node)
    body = _context_text(source, signals)
    return (
        f"<{label} name=\"{node['name']}\" path=\"{node['original_file_path']}\" depth=\"{depth}\">\n"
        f"{body}\n"
        f"</{label}>"
    )


def _related_test_contexts(
    failed_repo_path: Path,
    manifest: dict,
    test_node: dict | None,
    primary_id: str,
    signals: set[str],
) -> dict[str, str]:
    contexts = {}
    for index, model_id in enumerate(_test_model_ids(manifest, test_node), start=1):
        if model_id == primary_id:
            continue
        node = manifest["nodes"][model_id]
        contexts[f"test_model_{index}"] = _node_context(
            failed_repo_path,
            manifest,
            model_id,
            signals,
            "RELATED_TEST_MODEL",
            1,
        )
    return contexts


def _definition_text(path: Path, signals: set[str]) -> str:
    text = _read_source_text(path)
    if path.suffix.lower() not in {".yml", ".yaml"}:
        return _context_text(text, signals)

    lines = [
        line.rstrip()
        for line in text.splitlines()
        if any(signal and signal in line.lower() for signal in signals)
    ]
    compact = "\n".join(lines) or text
    return compact[:1600].strip()


def _macro_contexts(failed_repo_path: Path, manifest: dict, node: dict, source: str) -> list[str]:
    macro_ids = list(node.get("depends_on", {}).get("macros", []))
    macro_names = extract_macro_calls(source)

    for macro_id, macro in manifest.get("macros", {}).items():
        if macro.get("name") in macro_names:
            macro_ids.append(macro_id)

    contexts = []
    for macro_id in list(dict.fromkeys(macro_ids))[:4]:
        macro = manifest.get("macros", {}).get(macro_id)
        path = macro.get("original_file_path") if macro else None
        if not path:
            continue
        text = _context_text(_read_source_text(failed_repo_path / path), macro_names)
        contexts.append(f"<MACRO_CONTEXT name=\"{macro.get('name')}\" path=\"{path}\">\n{text}\n</MACRO_CONTEXT>")
    return contexts


def _definition_contexts(
    failed_repo_path: Path,
    manifest: dict,
    node: dict,
    error_log: str,
    signals: set[str],
) -> list[str]:
    contexts = []

    for source_id in node.get("depends_on", {}).get("nodes", []):
        source = manifest.get("sources", {}).get(source_id)
        path = source.get("original_file_path") if source else None
        if path:
            text = _definition_text(failed_repo_path / path, signals)
            contexts.append(
                f"<SOURCE_DEFINITION name=\"{source.get('name')}\" path=\"{path}\">\n"
                f"{text}\n"
                f"</SOURCE_DEFINITION>"
            )

    for match in utils.exp.DBT_SOURCE_PATH_RE.finditer(error_log):
        path = utils._normalize_dbt_source_path(match.group("path"))
        if path and path.endswith((".yml", ".yaml")) and path != node.get("original_file_path"):
            text = _definition_text(failed_repo_path / path, signals)
            if text:
                contexts.append(
                    f"<SCHEMA_DEFINITION path=\"{path}\">\n"
                    f"{text}\n"
                    f"</SCHEMA_DEFINITION>"
                )

    return list(dict.fromkeys(contexts))[:3]


def _compiled_sql_context(failed_repo_path: Path, error_log: str) -> str:
    match = COMPILED_SQL_RE.search(error_log)
    if not match:
        return ""

    path = match.group("path")
    text = _read_source_text(failed_repo_path / path)
    if not text:
        return ""

    location = ERROR_LOCATION_RE.search(error_log)
    location_text = f' location="{location.group(0)}"' if location else ""
    text = text[:3000]
    return f"<COMPILED_SQL path=\"{path}\"{location_text}>\n{text}\n</COMPILED_SQL>"


def parse_lineage_models(model: str) -> dict[str, str]:
    """Collect selective upstream diagnostic context from manifest."""
    error_log = _error_log()
    if not _needs_lineage_context(error_log):
        return {}

    if "macros" in Path(model).parts:
        return {}

    failed_repo_path = utils.get_failed_repo_path()
    manifest = _read_manifest(failed_repo_path)
    node_id, node = _find_node(manifest, model)
    if not node_id or not node:
        logging.warning("Model for %s not found in dbt manifest; skipping lineage context.", model)
        return {}

    _, test_node = _find_test_node(manifest, error_log)
    primary_source = _model_source(failed_repo_path, node)
    signals = extract_error_signals(error_log, primary_source) | _test_signals(manifest, test_node)
    contexts = {}
    is_relationship_test = utils._is_relationship_test_node(test_node)

    test_context = _test_failure_context(manifest, test_node)
    if test_context:
        contexts["test_failure"] = test_context

    relationship_context = _relationship_test_context(manifest, test_node, error_log)
    if relationship_context:
        contexts["relationship_test"] = relationship_context

    contexts.update(
        _related_test_contexts(
            failed_repo_path,
            manifest,
            test_node,
            node_id,
            signals,
        )
    )

    if not is_relationship_test:
        for upstream_id, depth, context_signals in _lineage_model_ids(
            failed_repo_path,
            manifest,
            node_id,
            signals,
            _upstream_model_ids,
            MAX_UPSTREAM_DEPTH,
            MAX_DIAGNOSTIC_MODELS,
            relevance_after_first=True,
        ):
            upstream = manifest["nodes"][upstream_id]
            contexts[upstream["name"]] = _node_context(
                failed_repo_path,
                manifest,
                upstream_id,
                context_signals,
                "UPSTREAM_MODEL",
                depth,
            )

    for index, macro_context in enumerate(
        _macro_contexts(failed_repo_path, manifest, node, primary_source),
        start=1,
    ):
        contexts[f"macro_{index}"] = macro_context

    for index, definition_context in enumerate(
        _definition_contexts(failed_repo_path, manifest, node, error_log, signals),
        start=1,
    ):
        contexts[f"definition_{index}"] = definition_context

    return contexts


def get_impact_context(file: str) -> str:
    """Build downstream context for impact validation."""
    error_log = _error_log()
    if not _needs_lineage_context(error_log):
        return ""

    failed_repo_path = utils.get_failed_repo_path()
    manifest = _read_manifest(failed_repo_path)
    node_id, node = _find_node(manifest, file)
    if not node_id or not node:
        return ""

    primary_source = _model_source(failed_repo_path, node)
    _, test_node = _find_test_node(manifest, error_log)
    if utils._is_relationship_test_node(test_node):
        return ""

    signals = extract_error_signals(error_log, primary_source) | _test_signals(manifest, test_node)
    sections = [
        _node_context(
            failed_repo_path,
            manifest,
            downstream_id,
            context_signals,
            "DOWNSTREAM_MODEL",
            depth,
        )
        for downstream_id, depth, context_signals in _lineage_model_ids(
            failed_repo_path,
            manifest,
            node_id,
            signals,
            _downstream_model_ids,
            MAX_DOWNSTREAM_DEPTH,
            MAX_IMPACT_MODELS,
        )
    ]
    return "\n".join(sections)


def _file_path(file: str) -> Path:
    path = Path(file)
    if path.is_absolute():
        return path
    return utils.get_failed_repo_path() / path


def _primary_context_file(file: str, error_log: str) -> str:
    path = _file_path(file)
    if path.suffix.lower() not in {".yml", ".yaml"}:
        return file

    failed_repo_path = utils.get_failed_repo_path()
    manifest = _read_manifest(failed_repo_path)
    _, test_node = _find_test_node(manifest, error_log)
    if utils._is_relationship_test_node(test_node):
        roles = utils._relationship_test_model_roles(manifest, test_node, _test_failure_name(error_log))
        side = utils._relationship_error_side(error_log) or "from"
        preferred = roles.get(side) or roles.get("from") or roles.get("to")
        if preferred:
            return preferred[1]["original_file_path"]

    attached = utils._test_attached_model(manifest, test_node)
    if attached:
        return attached[1]["original_file_path"]

    for model_id in _test_model_ids(manifest, test_node):
        return manifest["nodes"][model_id]["original_file_path"]
    return file


def get_file_context(files: list[str] | str) -> str:
    """Build primary source and diagnostic context for files."""
    sources = []

    if isinstance(files, str):
        files = [files]

    for file in files:
        error_log = _error_log()
        file = _primary_context_file(file, error_log)
        path = _file_path(file)
        if "target" in path.parts or not path.is_file():
            continue

        relative_path = _relative_to_failed_repo(path)
        diagnostic_context = "\n".join(parse_lineage_models(file).values())
        impact_context = get_impact_context(file)

        sources.append(
            f"<PRIMARY_ERROR_MODEL path=\"{relative_path}\">\n"
            f"SOURCE OF {relative_path}:\n{_read_source_text(path)}\n"
            f"FILE DIFF:\n{_get_file_diff(path)}\n"
            f"{_compiled_sql_context(utils.get_failed_repo_path(), error_log)}\n"
            f"</PRIMARY_ERROR_MODEL>\n\n"
            f"<DIAGNOSTIC_CONTEXT>\n{diagnostic_context or 'NO_DIAGNOSTIC_CONTEXT'}\n</DIAGNOSTIC_CONTEXT>\n\n"
            f"<IMPACT_CONTEXT>\n{impact_context or 'NO_IMPACT_CONTEXT'}\n</IMPACT_CONTEXT>"
        )

    return "\n".join(sources)
