import re
from pathlib import Path

from app.dbt_exps import (
    SQL_ALIAS_RE,
    SQL_CONFIG_RE,
    SQL_CTE_RE,
    SQL_ERROR_SIGNAL_RE,
    SQL_IDENTIFIER_RE,
    SQL_MACRO_CALL_RE,
    SQL_QUOTED_IDENTIFIER_RE,
    SQL_REF_RE,
    SQL_STOP_WORDS,
)


def _clean_symbol(value: str) -> str:
    return value.strip().strip("`'\".,;:()[]{}").split(".")[-1].lower()


def _symbols(values) -> set[str]:
    return {
        symbol
        for value in values
        for symbol in [_clean_symbol(value)]
        if symbol and symbol not in SQL_STOP_WORDS
    }


def extract_refs(text: str) -> set[str]:
    return _symbols(item for match in SQL_REF_RE.findall(text or "") for item in match if item)


def extract_macro_calls(text: str) -> set[str]:
    ignored = {"config", "ref", "source", "var", "env_var", "doc"}
    return {
        symbol
        for symbol in _symbols(SQL_MACRO_CALL_RE.findall(text or ""))
        if symbol not in ignored
    }


def extract_aliases(text: str) -> set[str]:
    return _symbols(SQL_ALIAS_RE.findall(text or ""))


def extract_error_signals(error_text: str, source: str = "") -> set[str]:
    """Extract model, macro, and column hints from dbt error text."""
    text = error_text or ""
    signals = set()
    signals |= _symbols(SQL_ERROR_SIGNAL_RE.findall(text))
    signals |= _symbols(SQL_QUOTED_IDENTIFIER_RE.findall(text))
    signals |= extract_refs(text)
    signals |= extract_macro_calls(text)
    signals |= _symbols(token for token in SQL_IDENTIFIER_RE.findall(text) if "_" in token or "$" in token)
    return signals | extract_refs(source)


def node_symbols(name: str, path: str, source: str) -> set[str]:
    """Return searchable symbols defined or used by a dbt node."""
    source = source or ""
    return (
        {_clean_symbol(name), _clean_symbol(Path(path).stem)}
        | extract_refs(source)
        | extract_macro_calls(source)
        | extract_aliases(source)
        | _symbols(SQL_CTE_RE.findall(source))
    )


def relevance_score(source: str, name: str, path: str, signals: set[str]) -> int:
    """Score a node by overlap with error symbols."""
    symbols = node_symbols(name, path, source)
    source_lower = (source or "").lower()
    return sum(3 if signal in symbols else int(bool(signal and signal in source_lower)) for signal in signals)


def _lines_matching(source: str, pattern: str) -> str:
    rx = re.compile(pattern, re.IGNORECASE)
    return "\n".join(line.rstrip() for line in source.splitlines() if rx.search(line))


def _final_select(source: str, max_chars: int) -> str:
    source = source or ""
    matches = list(re.finditer(r"\bselect\b", source, re.IGNORECASE))
    if not matches:
        return ""
    return source[matches[-1].start():][:max_chars].strip()


def _cap(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[truncated]"


def _line_windows(
    source: str,
    signals: set[str],
    context_lines: int = 2,
    max_chars: int = 650,
) -> list[tuple[str, str, int]]:
    """Return compact windows around lines containing relevant signals."""
    patterns = [
        re.compile(rf"(?<![\w$]){re.escape(signal)}(?![\w$])", re.IGNORECASE)
        for signal in signals
        if signal
    ]
    if not patterns:
        return []

    lines = source.splitlines()
    windows = []
    for index, line in enumerate(lines):
        if not any(pattern.search(line) for pattern in patterns):
            continue

        start = max(0, index - context_lines)
        end = min(len(lines), index + context_lines + 1)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))

    chunks = []
    for index, (start, end) in enumerate(windows[:8], start=1):
        body = "\n".join(lines[start:end]).strip()
        if body:
            chunks.append((f"RELEVANT_WINDOW_{index}", _cap(body, max_chars), 85))
    return chunks


def _pk_lines(source: str) -> str:
    """Return lines that look like dbt unique keys or primary key expressions."""
    patterns = (
        r"\bunique_key\b",
        r"\bprimary[_ ]key\b",
        r"\bsurrogate_key\b",
        r"\bgenerate_surrogate_key\b",
        r"\b[a-zA-Z_][\w$]*(?:_id|_key)\b",
    )
    rx = re.compile("|".join(patterns), re.IGNORECASE)
    return "\n".join(line.rstrip() for line in source.splitlines() if rx.search(line))


def _sql_chunks(source: str, signals: set[str], max_chars: int) -> list[tuple[str, str, int]]:
    source = source or ""
    config = "\n".join(match.group(0) for match in SQL_CONFIG_RE.finditer(source))
    refs = ", ".join(sorted(extract_refs(source)))
    macros = ", ".join(sorted(extract_macro_calls(source)))
    aliases = ", ".join(sorted(extract_aliases(source)))
    ctes = ", ".join(sorted(_symbols(SQL_CTE_RE.findall(source))))
    joins = _lines_matching(source, r"\bjoin\b|\bon\b")
    filters = _lines_matching(source, r"\bwhere\b|\bgroup\s+by\b|\bhaving\b|\bqualify\b")
    relevant = "\n".join(
        line.rstrip()
        for line in source.splitlines()
        if any(signal and signal in line.lower() for signal in signals)
    )

    chunks = [
        ("FINAL_SELECT", _final_select(source, max_chars // 2), 100),
        ("RELEVANT_LINES", relevant, 90),
        ("RELEVANT_WINDOWS", "\n\n".join(body for _, body, _ in _line_windows(source, signals)), 85),
        ("PK_OR_UNIQUE_KEYS", _pk_lines(source), 82),
        ("ALIASES", aliases, 80),
        ("REFS", refs, 75),
        ("MACRO_CALLS", macros, 75),
        ("CTES", ctes, 70),
        ("JOIN_CLAUSES", joins, 60),
        ("WHERE_GROUP_BY", filters, 60),
        ("CONFIG", config, 50),
    ]
    return [(title, body.strip(), priority) for title, body, priority in chunks if body and body.strip()]


def _lexical_retrieve(chunks: list[tuple[str, str, int]], signals: set[str], k: int) -> list[tuple[str, str]]:
    def score(item):
        _, body, priority = item
        body_lower = body.lower()
        matches = sum(1 for signal in signals if signal and signal in body_lower)
        return priority + matches * 20

    return [(title, body) for title, body, _ in sorted(chunks, key=score, reverse=True)[:k]]


def _dedupe_chunks(chunks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen = set()
    result = []
    for title, body in chunks:
        key = body.strip()
        if key and key not in seen:
            result.append((title, key))
            seen.add(key)
    return result


def retrieve_sql_context(
    source: str,
    signals: set[str],
    max_chars: int = 1600,
    k: int = 4,
) -> str:
    """Retrieve focused SQL chunks with deterministic lexical ranking."""
    source = source or ""
    chunks = _sql_chunks(source, signals, max_chars)
    if not chunks:
        return ""

    pinned = [
        (title, body)
        for title, body, _ in chunks
        if title in {"CONFIG", "PK_OR_UNIQUE_KEYS", "RELEVANT_LINES", "RELEVANT_WINDOWS"}
    ][:4]
    selected = _lexical_retrieve(chunks, signals, k)

    sections = _dedupe_chunks(pinned + selected)
    text = "\n\n".join(f"{title}:\n{_cap(body, 500)}" for title, body in sections)
    header = f"[RAG lexical windows from {len(source)} chars]\n"
    return header + _cap(text, max(0, max_chars - len(header)))


def structured_sql_context(
    source: str,
    signals: set[str],
    max_chars: int = 1600,
) -> str:
    """Return compact SQL context prioritized for dbt error repair."""
    source = source or ""
    if len(source) <= max_chars:
        return source

    return retrieve_sql_context(source, signals, max_chars=max_chars)
