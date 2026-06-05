from __future__ import annotations

from dataclasses import asdict
import fnmatch
import hashlib
from pathlib import Path
from typing import Any

from helpers import files, plugins, projects

from .index_store import ProjectIndexStore
from .navigation_engine import (
    build_syntax_chunks,
    collect_references,
    collect_symbols,
    resolve_scope,
    serialize_tree,
)
from .runtime_support import (
    SUPPORTED_LANGUAGES,
    TreeSitterRuntimeError,
    canonicalize_language,
    detect_language_from_path,
    get_language,
    parse_source,
    query_runtime_is_available,
    require_query_runtime,
)


PLUGIN_NAME = "tree_sitter"
INDEX_ROOT = files.get_abs_path("usr/plugins/tree_sitter/data/indexes")

# Directories always excluded from indexing (generated/vendor content)
_DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset({
    "node_modules", "__pycache__", ".git", "venv", ".venv", "env",
    "dist", "build", ".next", ".nuxt", ".cache", ".tox",
    ".mypy_cache", ".pytest_cache", ".sass-cache", "target",
    ".idea", ".vscode", "coverage", ".coverage",
})


def _load_gitignore_patterns(root: Path) -> list[str]:
    """Read .gitignore from root and return simplified patterns."""
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return []
    patterns: list[str] = []
    for line in gitignore.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _should_ignore_path(
    rel_path: Path,
    *,
    gitignore_patterns: list[str],
    index_hidden_files: bool,
) -> bool:
    """Check if a path should be excluded from indexing."""
    parts = rel_path.parts
    # Skip hidden files/dirs unless explicitly enabled
    if not index_hidden_files and any(p.startswith(".") for p in parts):
        return True
    # Skip known generated/vendor directories
    if any(p in _DEFAULT_IGNORE_DIRS for p in parts):
        return True
    # Check .gitignore patterns (directory-level and file-level)
    rel_str = str(rel_path).replace("\\", "/")
    for pattern in gitignore_patterns:
        # Directory pattern (trailing slash or pattern without dots/slashes)
        clean = pattern.rstrip("/")
        # Match against any path component
        if clean in parts:
            return True
        # Match against full relative path
        if fnmatch.fnmatch(rel_str, pattern) or fnmatch.fnmatch(rel_str, pattern.lstrip("/")):
            return True
        # Match against filename only
        if fnmatch.fnmatch(rel_path.name, pattern):
            return True
    return False


def get_config(agent=None) -> dict[str, Any]:
    cfg = plugins.get_plugin_config(PLUGIN_NAME, agent=agent) or {}
    return {
        "max_file_bytes": int(cfg.get("max_file_bytes", 200_000)),
        "max_chunk_chars": int(cfg.get("max_chunk_chars", 1600)),
        "max_tree_depth": int(cfg.get("max_tree_depth", 3)),
        "max_query_matches": int(cfg.get("max_query_matches", 50)),
        "index_hidden_files": bool(cfg.get("index_hidden_files", False)),
        "index_max_files": int(cfg.get("index_max_files", 500)),
    }


def inspect_file(
    path: str,
    *,
    language: str | None = None,
    query: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = config or get_config()
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_text = file_path.read_text(encoding="utf-8")
    if len(source_text.encode("utf-8")) > cfg["max_file_bytes"]:
        raise TreeSitterRuntimeError(
            f"File is too large for direct Tree-sitter inspection: {file_path}"
        )

    selected_language = language or detect_language_from_path(file_path)
    canonical_language = canonicalize_language(selected_language)
    if not canonical_language:
        raise TreeSitterRuntimeError(
            f"Could not infer a supported Tree-sitter language for {file_path.name}"
        )

    tree = parse_source(source_text, canonical_language)
    source_bytes = source_text.encode("utf-8")
    symbols = collect_symbols(
        tree.root_node,
        source_bytes,
        str(file_path),
        canonical_language,
    )
    chunks = build_syntax_chunks(
        tree.root_node,
        source_text,
        str(file_path),
        canonical_language,
        max_chars=cfg["max_chunk_chars"],
    )
    payload: dict[str, Any] = {
        "path": str(file_path),
        "language": canonical_language,
        "symbols": [asdict(symbol) for symbol in symbols],
        "chunks": [asdict(chunk) for chunk in chunks],
        "tree": serialize_tree(
            tree.root_node,
            source_bytes,
            depth_limit=cfg["max_tree_depth"],
        ),
    }
    if query:
        payload["query_matches"] = run_query(
            source_bytes=source_bytes,
            language=canonical_language,
            root_node=tree.root_node,
            query_source=query,
            limit=cfg["max_query_matches"],
        )
    return payload


def references_for_symbol(
    path: str,
    *,
    symbol: str,
    language: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inspection = inspect_file(path, language=language, config=config)
    symbol_records = inspection["symbols"]
    exact_matches = [record for record in symbol_records if record["name"] == symbol]
    exclude_ranges = []
    for record in exact_matches:
        exclude_ranges.append(
            (
                (record.get("name_start_line") or record["start_line"], record.get("name_start_col") or record["start_col"]),
                (record.get("name_end_line") or record["end_line"], record.get("name_end_col") or record["end_col"]),
            )
        )
    file_path = inspection["path"]
    source_text = Path(file_path).read_text(encoding="utf-8")
    tree = parse_source(source_text, inspection["language"])
    refs = collect_references(
        tree.root_node,
        source_text.encode("utf-8"),
        file_path,
        inspection["language"],
        symbol,
        exclude_ranges=exclude_ranges,
    )
    return {
        "path": file_path,
        "language": inspection["language"],
        "symbol": symbol,
        "definitions": exact_matches,
        "references": refs,
    }


def scope_for_position(
    path: str,
    *,
    line: int,
    column: int,
    language: str | None = None,
) -> dict[str, Any]:
    file_path = Path(path).expanduser().resolve()
    source_text = file_path.read_text(encoding="utf-8")
    canonical_language = canonicalize_language(language or detect_language_from_path(file_path))
    if not canonical_language:
        raise TreeSitterRuntimeError(
            f"Could not infer a supported Tree-sitter language for {file_path.name}"
        )
    tree = parse_source(source_text, canonical_language)
    scope = resolve_scope(
        tree.root_node,
        source_text.encode("utf-8"),
        str(file_path),
        canonical_language,
        line,
        column,
    )
    return {
        "path": str(file_path),
        "language": canonical_language,
        "scope": scope,
    }


def build_index(
    root_path: str,
    *,
    agent=None,
    project_name: str | None = None,
) -> dict[str, Any]:
    cfg = get_config(agent=agent)
    root = Path(root_path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Index root not found: {root}")

    gitignore_patterns = _load_gitignore_patterns(root)

    records: list[dict[str, Any]] = []
    indexed_files = 0
    for file_path in root.rglob("*"):
        if indexed_files >= cfg["index_max_files"]:
            break
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(root)
        if _should_ignore_path(
            rel_path,
            gitignore_patterns=gitignore_patterns,
            index_hidden_files=cfg["index_hidden_files"],
        ):
            continue
        language = detect_language_from_path(file_path)
        if not language:
            continue

        source_bytes = file_path.read_bytes()
        if len(source_bytes) > cfg["max_file_bytes"]:
            continue

        source_text = source_bytes.decode("utf-8", errors="ignore")
        tree = parse_source(source_text, language)
        symbols = collect_symbols(
            tree.root_node,
            source_text.encode("utf-8"),
            str(file_path.relative_to(root)),
            language,
        )
        chunks = build_syntax_chunks(
            tree.root_node,
            source_text,
            str(file_path.relative_to(root)),
            language,
            max_chars=cfg["max_chunk_chars"],
        )
        records.append(
            {
                "path": str(file_path.relative_to(root)),
                "language": language,
                "mtime": file_path.stat().st_mtime,
                "source_hash": hashlib.sha1(source_bytes).hexdigest(),
                "symbols": [asdict(symbol) for symbol in symbols],
                "chunks": [asdict(chunk) for chunk in chunks],
            }
        )
        indexed_files += 1

    store = ProjectIndexStore(INDEX_ROOT)
    key = project_key_for_root(root, project_name=project_name)
    manifest = store.save_index(key, str(root), records)
    manifest["project_key"] = key
    return manifest


def get_index_status(
    root_path: str,
    *,
    project_name: str | None = None,
) -> dict[str, Any] | None:
    root = Path(root_path).expanduser().resolve()
    store = ProjectIndexStore(INDEX_ROOT)
    return store.load_manifest(project_key_for_root(root, project_name=project_name))


def lookup_symbol(
    root_path: str,
    *,
    symbol: str,
    project_name: str | None = None,
) -> dict[str, Any]:
    root = Path(root_path).expanduser().resolve()
    store = ProjectIndexStore(INDEX_ROOT)
    key = project_key_for_root(root, project_name=project_name)
    return {
        "project_key": key,
        "root_path": str(root),
        "symbol": symbol,
        "matches": store.lookup_symbol(key, symbol),
    }


def resolve_root_path(explicit_root: str | None = None, *, context=None) -> tuple[str, str | None]:
    if explicit_root:
        return str(Path(explicit_root).expanduser().resolve()), None

    if context is not None:
        project_name = projects.get_context_project_name(context)
        if project_name:
            return projects.get_project_folder(project_name), project_name

    raise TreeSitterRuntimeError(
        "No root path provided and no active Agent Zero project is available."
    )


def project_key_for_root(root_path: str | Path, *, project_name: str | None = None) -> str:
    if project_name:
        return f"project-{project_name}"
    resolved = str(Path(root_path).expanduser().resolve())
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]


def run_query(
    *,
    source_bytes: bytes,
    language: str,
    root_node,
    query_source: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not query_runtime_is_available():
        raise TreeSitterRuntimeError(
            "Tree-sitter query support is unavailable until the runtime dependency is installed."
        )

    query_module = require_query_runtime()
    language_obj = get_language(language)
    query = query_module.Query(language_obj, query_source)
    cursor = query_module.QueryCursor(query)
    matches: list[dict[str, Any]] = []
    for pattern_index, captures in cursor.matches(root_node):
        serialised_captures = {}
        for capture_name, nodes in captures.items():
            node_list = nodes if isinstance(nodes, list) else [nodes]
            serialised_captures[capture_name] = [
                {
                    "type": node.type,
                    "text": _slice_capture(source_bytes, node),
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "start_col": node.start_point[1] + 1,
                    "end_col": node.end_point[1] + 1,
                }
                for node in node_list
            ]
        matches.append(
            {
                "pattern_index": pattern_index,
                "captures": serialised_captures,
            }
        )
        if len(matches) >= limit:
            break
    return matches


def _slice_capture(source_bytes: bytes, node) -> str:
    lines = source_bytes.decode("utf-8").splitlines(keepends=True)
    start_row, start_col = node.start_point
    end_row, end_col = node.end_point
    if start_row == end_row:
        return lines[start_row][start_col:end_col]
    parts = [lines[start_row][start_col:]]
    for row in range(start_row + 1, end_row):
        parts.append(lines[row])
    parts.append(lines[end_row][:end_col])
    return "".join(parts)
