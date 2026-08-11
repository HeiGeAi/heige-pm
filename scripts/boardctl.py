#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import errno
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit
import zipfile

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None


REPORTED_STATUSES = frozenset(
    {"unknown", "not_started", "in_progress", "blocked", "done", "conflict", "cancelled"}
)
VERIFICATION_LEVELS = (
    "none",
    "source_report",
    "source_backed",
    "artifact_present",
    "local_verified",
    "target_verified",
    "e2e_verified",
)
APPROVAL_STATES = frozenset({"draft", "reviewed", "accepted"})
DECISION_STATES = frozenset({"proposed", "decided", "reversed", "conflict"})
VISIBILITIES = frozenset({"private", "team", "public"})
THEMES = frozenset({"warm", "clean", "dark", "paper"})
AUDIENCE_ORDER = {"public": 0, "team": 1, "private": 2}
VIEW_FIELDS = {
    "workflow": {"id", "name", "description", "human_gate"},
    "members": {"id", "display_name", "responsibility"},
    "sources": {"id", "type", "location", "revision", "read_at"},
    "tasks": {"id", "description", "owner", "reported_status", "verification_level", "approval_state", "due_date", "blocked_reason", "source_refs"},
    "decisions": {"id", "topic", "conclusion", "decision_state", "decided_by", "decided_at", "supersedes", "source_refs"},
    "updates": {"id", "date", "title", "summary", "sections", "risks", "learnings", "source_refs"},
    "evidence": {"id", "task_id", "artifact", "target", "recorded_at", "result", "source_refs"},
    "deliveries": {"id", "name", "description", "source_refs"},
}
VIEW_META_FIELDS = {"schema_version", "skill_version", "theme", "audience", "updated_at", "language"}
VIEW_PROJECT_FIELDS = {"id", "name", "goal", "milestones", "cadence"}
VIEW_MILESTONE_FIELDS = {"id", "name", "due_date"}

REQUIRED_OBJECTS = ("meta", "project")
REQUIRED_COLLECTIONS = (
    "workflow",
    "members",
    "sources",
    "tasks",
    "decisions",
    "updates",
    "evidence",
    "deliveries",
)
ID_COLLECTIONS = REQUIRED_COLLECTIONS + ("project.milestones",)
SOURCE_REF_COLLECTIONS = ("tasks", "decisions", "updates", "evidence", "deliveries")
URL_FIELDS = frozenset({"url", "location", "href", "link"})
SITE_RELATIVE_URL_FIELDS = frozenset({"url", "href", "link"})
SAFE_URL_PROTOCOLS = frozenset({"http", "https"})
UNSAFE_URL_PROTOCOLS = frozenset({"data", "file", "javascript", "vbscript"})

HTML_PATTERN = re.compile(
    r"<\s*(?:script|iframe|object|embed|svg)\b|\bon[a-z]+\s*=",
    re.IGNORECASE,
)
URL_PROTOCOL_PATTERN = re.compile(r"^([a-z][a-z0-9+.-]*):", re.IGNORECASE)
URL_LITERAL_PATTERN = re.compile(r"https?://[^\s<>()\[\]\\\"']+", re.IGNORECASE)
MARKDOWN_LINK_PATTERN = re.compile(r"\]\(\s*([^)]+?)\s*\)", re.DOTALL)
LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_PATTERN = re.compile(
    rf"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    rf"(?:-{SEMVER_IDENTIFIER}(?:\.{SEMVER_IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
LOCAL_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'=`])(?:"
    r"/(?!/)[^\s\"'`<>]+"
    r"|~[\\/]"
    r"|[a-z]:[\\/]"
    r"|\\\\[^\\/\s]+[\\/]"
    r")",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|password|secret)\b\s*[:=]\s*[\"']?"
        r"[a-z0-9._/-]{12,}",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:gh[pousr]_[a-z0-9]{20,}|sk-[a-z0-9]{20,})\b", re.IGNORECASE),
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PROJECT = PACKAGE_ROOT / "assets" / "sample-project.json"
MERGE_APPEND_COLLECTIONS = ("sources", "evidence", "updates")
MERGE_COLLECTIONS = frozenset((*MERGE_APPEND_COLLECTIONS, "tasks", "decisions"))
LOCK_CONTENTION_ERRNOS = frozenset(
    {errno.EACCES, errno.EAGAIN, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
)


class MergeConflict(ValueError):
    """A patch cannot safely be applied to the supplied project revision."""


class PatchFormatError(ValueError):
    """A patch does not have the partial-merge shape."""


@contextmanager
def exclusive_lock(path: str | Path):
    """Hold a blocking exclusive lock backed by a persistent sidecar file."""
    if _fcntl is None and _msvcrt is None:
        raise RuntimeError(
            "No supported file-lock backend is available; install Python with fcntl or msvcrt support"
        )

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if _fcntl is not None:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
            return

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        while True:
            lock_file.seek(0)
            try:
                _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if error.errno not in LOCK_CONTENTION_ERRNOS:
                    raise
                time.sleep(0.01)
        try:
            yield
        finally:
            lock_file.seek(0)
            _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)


def load_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{source}: invalid JSON at line {error.lineno}, column {error.colno}"
        ) from None
    except OSError as error:
        raise ValueError(f"{source}: {error.strerror or error}") from None
    if not isinstance(value, dict):
        raise ValueError("$: expected object")
    return value


def _walk_strings(value: Any, path: str = "", field: str = ""):
    if isinstance(value, dict):
        for key in sorted(value):
            child_path = f"{path}.{key}" if path else key
            yield from _walk_strings(value[key], child_path, key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]", field)
    elif isinstance(value, str):
        yield path or "$", field, value


def scan_sensitive(value: Any) -> list[str]:
    findings: list[str] = []
    for path, field, text in _walk_strings(value):
        url_text = text.strip().translate({9: None, 10: None, 13: None})
        protocol_match = URL_PROTOCOL_PATTERN.match(url_text)
        if protocol_match:
            protocol = protocol_match.group(1).lower()
            if (field in URL_FIELDS or protocol in UNSAFE_URL_PROTOCOLS) and protocol not in SAFE_URL_PROTOCOLS:
                findings.append(f"{path}: unsafe URL protocol '{protocol}'")
        if field in URL_FIELDS:
            parsed_field_url = urlsplit(url_text)
            if parsed_field_url.username is not None or parsed_field_url.password is not None:
                findings.append(f"{path}: URL must not contain username or password")
        for markdown_url in MARKDOWN_LINK_PATTERN.findall(text):
            normalized_url = markdown_url.strip().strip("<>").translate({9: None, 10: None, 13: None})
            embedded_protocol = URL_PROTOCOL_PATTERN.match(normalized_url)
            if embedded_protocol and embedded_protocol.group(1).lower() not in SAFE_URL_PROTOCOLS:
                findings.append(
                    f"{path}: unsafe URL protocol '{embedded_protocol.group(1).lower()}'"
                )
        for url in URL_LITERAL_PATTERN.findall(text):
            parsed = urlsplit(url)
            if parsed.username is not None or parsed.password is not None:
                findings.append(f"{path}: URL must not contain username or password")
        if HTML_PATTERN.search(text):
            findings.append(f"{path}: contains unsafe HTML")
        site_relative_url = (
            field in SITE_RELATIVE_URL_FIELDS
            and re.match(r"^/(?!/)", text.strip()) is not None
        )
        if not site_relative_url and LOCAL_PATH_PATTERN.search(text):
            findings.append(f"{path}: contains absolute local path")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            findings.append(f"{path}: contains possible secret")
    return sorted(set(findings))


def _collection(project: dict[str, Any], path: str) -> list[Any]:
    if path == "project.milestones":
        project_meta = project.get("project")
        value = project_meta.get("milestones") if isinstance(project_meta, dict) else None
    else:
        value = project.get(path)
    return value if isinstance(value, list) else []


def _validate_enum(errors: list[str], path: str, value: Any, allowed: set[str] | frozenset[str] | tuple[str, ...]) -> None:
    if not isinstance(value, str):
        errors.append(f"{path}: expected string")
    elif value not in allowed:
        expected = ", ".join(sorted(allowed))
        errors.append(f"{path}: invalid value {value!r}; expected one of {expected}")


def _validate_semver(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, str) or SEMVER_PATTERN.fullmatch(value) is None:
        errors.append(f"{path}: expected SemVer")


def _canonical_skill_version() -> str:
    lines = (PACKAGE_ROOT / "VERSION").read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or SEMVER_PATTERN.fullmatch(lines[0]) is None:
        raise ValueError("VERSION: expected one non-empty SemVer line")
    return lines[0]


def _validate_integer(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{path}: expected integer")


def _has_path(graph: dict[str, list[str]], start: str, target: str) -> bool:
    pending = [start]
    visited: set[str] = set()
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node not in visited:
            visited.add(node)
            pending.extend(graph.get(node, ()))
    return False


def validate_project(project: Any) -> list[str]:
    if not isinstance(project, dict):
        return ["$: expected object"]

    errors: list[str] = []
    for name in REQUIRED_OBJECTS:
        if name not in project:
            errors.append(f"{name}: missing required object")
        elif not isinstance(project[name], dict):
            errors.append(f"{name}: expected object")
    for name in REQUIRED_COLLECTIONS:
        if name not in project:
            errors.append(f"{name}: missing required collection")
        elif not isinstance(project[name], list):
            errors.append(f"{name}: expected array")

    meta = project.get("meta")
    if isinstance(meta, dict):
        if meta.get("schema_version") != "1.0":
            errors.append("meta.schema_version: expected string '1.0'")
        _validate_semver(errors, "meta.skill_version", meta.get("skill_version"))
        _validate_enum(errors, "meta.theme", meta.get("theme"), THEMES)
        _validate_enum(errors, "meta.audience", meta.get("audience"), VISIBILITIES)
        language = meta.get("language", "und")
        if not isinstance(language, str) or LANGUAGE_PATTERN.fullmatch(language) is None:
            errors.append("meta.language: expected BCP-47-like language tag")

    for collection_name in ID_COLLECTIONS:
        seen: dict[str, str] = {}
        for index, item in enumerate(_collection(project, collection_name)):
            item_path = f"{collection_name}[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{item_path}: expected object")
                continue
            item_id = item.get("id")
            id_path = f"{item_path}.id"
            if not isinstance(item_id, str) or not item_id:
                errors.append(f"{id_path}: expected non-empty string")
            elif item_id in seen:
                errors.append(f"{id_path}: duplicate ID '{item_id}' (first at {seen[item_id]})")
            else:
                seen[item_id] = id_path

    for index, member in enumerate(_collection(project, "members")):
        if isinstance(member, dict):
            _validate_enum(errors, f"members[{index}].visibility", member.get("visibility"), VISIBILITIES)

    for index, workflow in enumerate(_collection(project, "workflow")):
        if isinstance(workflow, dict):
            _validate_enum(
                errors, f"workflow[{index}].visibility", workflow.get("visibility"), VISIBILITIES
            )

    for index, milestone in enumerate(_collection(project, "project.milestones")):
        if isinstance(milestone, dict):
            _validate_enum(
                errors,
                f"project.milestones[{index}].visibility",
                milestone.get("visibility"),
                VISIBILITIES,
            )

    for index, source in enumerate(_collection(project, "sources")):
        if not isinstance(source, dict):
            continue
        _validate_enum(errors, f"sources[{index}].sensitivity", source.get("sensitivity"), VISIBILITIES)
        digest = source.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            errors.append(f"sources[{index}].sha256: expected 64 hexadecimal characters")

    for index, task in enumerate(_collection(project, "tasks")):
        if not isinstance(task, dict):
            continue
        base = f"tasks[{index}]"
        _validate_integer(errors, f"{base}.revision", task.get("revision"))
        _validate_enum(errors, f"{base}.reported_status", task.get("reported_status"), REPORTED_STATUSES)
        _validate_enum(errors, f"{base}.verification_level", task.get("verification_level"), VERIFICATION_LEVELS)
        _validate_enum(errors, f"{base}.approval_state", task.get("approval_state"), APPROVAL_STATES)
        _validate_enum(errors, f"{base}.visibility", task.get("visibility"), VISIBILITIES)

    decision_ids = {
        decision.get("id")
        for decision in _collection(project, "decisions")
        if isinstance(decision, dict) and isinstance(decision.get("id"), str)
    }
    for index, decision in enumerate(_collection(project, "decisions")):
        if isinstance(decision, dict):
            _validate_integer(errors, f"decisions[{index}].revision", decision.get("revision"))
            _validate_enum(
                errors,
                f"decisions[{index}].decision_state",
                decision.get("decision_state"),
                DECISION_STATES,
            )
            supersedes = decision.get("supersedes")
            supersedes_path = f"decisions[{index}].supersedes"
            if not isinstance(supersedes, list):
                errors.append(f"{supersedes_path}: expected array")
            else:
                for supersedes_index, decision_id in enumerate(supersedes):
                    if not isinstance(decision_id, str):
                        errors.append(f"{supersedes_path}[{supersedes_index}]: expected string")
                    elif decision_id == decision.get("id"):
                        errors.append(
                            f"{supersedes_path}[{supersedes_index}]: "
                            "decision cannot supersede itself"
                        )
                    elif decision_id not in decision_ids:
                        errors.append(
                            f"{supersedes_path}[{supersedes_index}]: "
                            f"unknown decision ID {decision_id!r}"
                        )

    decision_graph: dict[str, list[str]] = {}
    for decision in _collection(project, "decisions"):
        if not isinstance(decision, dict) or not isinstance(decision.get("id"), str):
            continue
        supersedes = decision.get("supersedes")
        if isinstance(supersedes, list):
            decision_graph.setdefault(decision["id"], []).extend(
                decision_id
                for decision_id in supersedes
                if isinstance(decision_id, str)
                and decision_id in decision_ids
                and decision_id != decision["id"]
            )
    for index, decision in enumerate(_collection(project, "decisions")):
        if not isinstance(decision, dict) or not isinstance(decision.get("id"), str):
            continue
        supersedes = decision.get("supersedes")
        if not isinstance(supersedes, list):
            continue
        for supersedes_index, decision_id in enumerate(supersedes):
            if (
                isinstance(decision_id, str)
                and decision_id in decision_ids
                and decision_id != decision["id"]
                and _has_path(decision_graph, decision_id, decision["id"])
            ):
                errors.append(
                    f"decisions[{index}].supersedes[{supersedes_index}]: "
                    "decision history contains a cycle"
                )

    source_ids = {
        source.get("id")
        for source in _collection(project, "sources")
        if isinstance(source, dict) and isinstance(source.get("id"), str)
    }
    for collection_name in SOURCE_REF_COLLECTIONS:
        for index, item in enumerate(_collection(project, collection_name)):
            if not isinstance(item, dict):
                continue
            if collection_name == "deliveries" and "source_refs" not in item:
                continue
            refs = item.get("source_refs")
            refs_path = f"{collection_name}[{index}].source_refs"
            if not isinstance(refs, list):
                errors.append(f"{refs_path}: expected array")
                continue
            for ref_index, source_id in enumerate(refs):
                ref_path = f"{refs_path}[{ref_index}]"
                if not isinstance(source_id, str):
                    errors.append(f"{ref_path}: expected string")
                elif source_id not in source_ids:
                    errors.append(f"{ref_path}: unknown source ID {source_id!r}")

    task_ids = {
        task.get("id")
        for task in _collection(project, "tasks")
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    evidence_task_ids: set[str] = set()
    for index, evidence in enumerate(_collection(project, "evidence")):
        if not isinstance(evidence, dict):
            continue
        task_id = evidence.get("task_id")
        if isinstance(task_id, str):
            if task_id in task_ids:
                evidence_task_ids.add(task_id)
            else:
                errors.append(f"evidence[{index}].task_id: unknown task ID {task_id!r}")
        else:
            errors.append(f"evidence[{index}].task_id: expected string")
    threshold = VERIFICATION_LEVELS.index("source_report")
    for index, task in enumerate(_collection(project, "tasks")):
        if not isinstance(task, dict) or task.get("reported_status") != "done":
            continue
        level = task.get("verification_level")
        if level in VERIFICATION_LEVELS and VERIFICATION_LEVELS.index(level) > threshold:
            task_id = task.get("id")
            if isinstance(task_id, str) and task_id not in evidence_task_ids:
                errors.append(
                    f"tasks[{index}].verification_level: {level!r} requires evidence "
                    f"for done task {task_id!r}"
                )

    errors.extend(scan_sensitive(project))
    return sorted(set(errors))


def init_project(target: str | Path, theme: str = "warm") -> Path:
    if theme not in THEMES:
        expected = ", ".join(sorted(THEMES))
        raise ValueError(f"theme: invalid value {theme!r}; expected one of {expected}")
    target_path = Path(target)
    project_path = target_path / "project.json"
    if project_path.exists():
        raise FileExistsError(f"{project_path}: already exists")
    project = load_json(SAMPLE_PROJECT)
    project["meta"]["theme"] = theme
    project["meta"]["skill_version"] = _canonical_skill_version()
    target_path.mkdir(parents=True, exist_ok=True)
    project_path.write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return project_path


def _patch_entries(patch: dict[str, Any], collection: str) -> list[Any]:
    entries = patch.get(collection, [])
    if not isinstance(entries, list):
        raise PatchFormatError(f"{collection}: expected array")
    return entries


def _append_records(merged: dict[str, Any], patch: dict[str, Any], collection: str) -> None:
    existing_ids = {
        item.get("id")
        for item in _collection(merged, collection)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for index, entry in enumerate(_patch_entries(patch, collection)):
        path = f"{collection}[{index}]"
        if not isinstance(entry, dict):
            raise PatchFormatError(f"{path}: expected object")
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise PatchFormatError(f"{path}.id: expected non-empty string")
        if item_id in existing_ids:
            raise MergeConflict(f"{path}.id: duplicate ID '{item_id}'")
        merged[collection].append(copy.deepcopy(entry))
        existing_ids.add(item_id)


def merge_project(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a small revision-checked update without mutating *base*."""
    base_errors = validate_project(base)
    if base_errors:
        raise ValueError("base project is invalid:\n" + "\n".join(base_errors))
    if not isinstance(patch, dict):
        raise PatchFormatError("$: expected object")
    unknown_collections = sorted(set(patch) - MERGE_COLLECTIONS)
    if unknown_collections:
        raise PatchFormatError(f"$: unsupported patch collection {unknown_collections[0]!r}")

    merged = copy.deepcopy(base)
    for collection in MERGE_APPEND_COLLECTIONS:
        _append_records(merged, patch, collection)

    task_by_id = {
        task["id"]: task
        for task in merged["tasks"]
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    changed_task_ids: set[str] = set()
    for index, change in enumerate(_patch_entries(patch, "tasks")):
        path = f"tasks[{index}]"
        if not isinstance(change, dict):
            raise PatchFormatError(f"{path}: expected object")
        task_id = change.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise PatchFormatError(f"{path}.id: expected non-empty string")
        if task_id not in task_by_id:
            raise MergeConflict(f"{path}.id: unknown task ID {task_id!r}")
        if task_id in changed_task_ids:
            raise PatchFormatError(f"{path}.id: duplicate task change {task_id!r}")
        base_revision = change.get("base_revision")
        if not isinstance(base_revision, int) or isinstance(base_revision, bool):
            raise PatchFormatError(f"{path}.base_revision: expected integer")
        task = task_by_id[task_id]
        if base_revision != task["revision"]:
            raise MergeConflict(
                f"{path}.base_revision: conflict for task {task_id!r}: "
                f"expected {task['revision']}, got {base_revision}"
            )
        for key, value in change.items():
            if key not in {"id", "base_revision", "revision"}:
                task[key] = copy.deepcopy(value)
        task["revision"] = base_revision + 1
        changed_task_ids.add(task_id)

    decision_ids = {
        decision.get("id")
        for decision in merged["decisions"]
        if isinstance(decision, dict) and isinstance(decision.get("id"), str)
    }
    for index, decision in enumerate(_patch_entries(patch, "decisions")):
        path = f"decisions[{index}]"
        if not isinstance(decision, dict):
            raise PatchFormatError(f"{path}: expected object")
        decision_id = decision.get("id")
        if not isinstance(decision_id, str) or not decision_id:
            raise PatchFormatError(f"{path}.id: expected non-empty string")
        if decision_id in decision_ids:
            raise MergeConflict(
                f"{path}.id: decision {decision_id!r} is immutable; "
                "add a new decision with supersedes"
            )
        if "supersedes" not in decision:
            raise PatchFormatError(f"{path}.supersedes: missing required field")
        supersedes = decision["supersedes"]
        if not isinstance(supersedes, list):
            raise PatchFormatError(f"{path}.supersedes: expected array")
        for supersedes_index, superseded_id in enumerate(supersedes):
            if not isinstance(superseded_id, str):
                raise PatchFormatError(f"{path}.supersedes[{supersedes_index}]: expected string")
            if superseded_id not in decision_ids:
                raise MergeConflict(
                    f"{path}.supersedes[{supersedes_index}]: unknown decision ID {superseded_id!r}"
                )
        merged["decisions"].append(copy.deepcopy(decision))
        decision_ids.add(decision_id)

    errors = validate_project(merged)
    if errors:
        raise ValueError("merged project is invalid:\n" + "\n".join(errors))
    return merged


def _write_json_atomic(output: str | Path, project: dict[str, Any]) -> None:
    output_path = Path(output)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(project, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def merge_project_file(
    base: str | Path,
    patch: str | Path,
    output: str | Path,
) -> Path:
    base_path = Path(base)
    output_path = Path(output)
    patch_data = load_json(patch)
    resolved_output = output_path.resolve()
    lock_path = resolved_output.with_name(f".{resolved_output.name}.lock")

    with exclusive_lock(lock_path):
        base_data = load_json(base_path)
        if output_path.exists() and base_path.resolve() != resolved_output:
            output_data = load_json(output_path)
            if output_data != base_data:
                raise MergeConflict(
                    f"{output_path}: existing output differs from base {base_path}"
                )
            base_data = output_data
        merged = merge_project(base_data, patch_data)
        _write_json_atomic(output_path, merged)
    return output_path


def _visible_to_audience(visibility: Any, audience: str) -> bool:
    return isinstance(visibility, str) and visibility in AUDIENCE_ORDER and (
        AUDIENCE_ORDER[visibility] <= AUDIENCE_ORDER[audience]
    )


def _record_visibility(record: dict[str, Any], collection: str) -> str:
    field = "sensitivity" if collection == "sources" else "visibility"
    if field in record:
        value = record.get(field)
        return value if value in VISIBILITIES else "private"
    return "private"


def _project_view(project: dict[str, Any], audience: str) -> dict[str, Any]:
    if audience == "private":
        return copy.deepcopy(project.get("project", {}))
    view = {
        key: copy.deepcopy(value)
        for key, value in project.get("project", {}).items()
        if key in VIEW_PROJECT_FIELDS and key != "milestones"
    }
    milestones = project.get("project", {}).get("milestones", [])
    if isinstance(milestones, list):
        view["milestones"] = [
            {key: copy.deepcopy(value) for key, value in milestone.items() if key in VIEW_MILESTONE_FIELDS}
            for milestone in milestones
            if isinstance(milestone, dict)
            and _visible_to_audience(milestone.get("visibility"), audience)
        ]
    return view


def _view_record(record: dict[str, Any], collection: str, audience: str) -> dict[str, Any]:
    if audience == "private":
        return copy.deepcopy(record)
    return {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key in VIEW_FIELDS[collection]
    }


def _source_label(refs: Any, visible_source_ids: set[str]) -> tuple[str, list[str] | None]:
    if not isinstance(refs, list) or not refs:
        return "No source recorded", None
    visible_refs = [ref for ref in refs if isinstance(ref, str) and ref in visible_source_ids]
    if len(visible_refs) == len(refs):
        return "Visible source", visible_refs
    return "Restricted hidden source", None


def _genericize_hidden_reference(
    record: dict[str, Any], field: str, visible_ids: set[str], label: str
) -> None:
    value = record.get(field)
    if field not in record:
        return
    if value is None or (isinstance(value, str) and not value.strip()):
        record[field] = ""
        return
    if not isinstance(value, str) or value not in visible_ids:
        record.pop(field, None)
        record[f"{field}_summary"] = label


def filter_for_audience(project: dict[str, Any], audience: str) -> dict[str, Any]:
    """Return a copy that contains only records safe for *audience*."""
    if audience not in VISIBILITIES:
        expected = ", ".join(sorted(VISIBILITIES))
        raise ValueError(f"audience: invalid value {audience!r}; expected one of {expected}")

    filtered: dict[str, Any] = {
        "meta": copy.deepcopy(project.get("meta", {})) if audience == "private" else {
            key: copy.deepcopy(value) for key, value in project.get("meta", {}).items() if key in VIEW_META_FIELDS
        },
        "project": _project_view(project, audience),
    }
    if audience != "private" and isinstance(filtered["meta"], dict):
        filtered["meta"]["audience"] = audience
    collections = ("members", "sources", "tasks", "decisions", "workflow", "updates", "evidence", "deliveries")
    visible_records = {
        collection: [
            record for record in project.get(collection, [])
            if isinstance(record, dict)
            and _visible_to_audience(_record_visibility(record, collection), audience)
        ]
        for collection in collections
    }
    visible_ids = {
        collection: {
            record["id"] for record in records if isinstance(record.get("id"), str)
        }
        for collection, records in visible_records.items()
    }
    filtered["sources"] = []
    for source in visible_records["sources"]:
        safe_source = _view_record(source, "sources", audience)
        if audience != "private" and "location" in safe_source and _safe_url(safe_source["location"]) is None:
            safe_source.pop("location", None)
        filtered["sources"].append(safe_source)

    for collection in ("workflow", "members", "tasks", "decisions", "updates", "evidence", "deliveries"):
        items: list[dict[str, Any]] = []
        for record in visible_records[collection]:
            safe_record = _view_record(record, collection, audience)
            if collection == "tasks":
                _genericize_hidden_reference(
                    safe_record, "owner", visible_ids["members"], "Restricted member"
                )
            elif collection == "decisions":
                _genericize_hidden_reference(
                    safe_record, "decided_by", visible_ids["members"], "Restricted member"
                )
                supersedes = safe_record.get("supersedes")
                if isinstance(supersedes, list):
                    safe_record["supersedes"] = [
                        decision_id for decision_id in supersedes
                        if isinstance(decision_id, str) and decision_id in visible_ids["decisions"]
                    ]
            elif collection == "evidence":
                _genericize_hidden_reference(
                    safe_record, "task_id", visible_ids["tasks"], "Restricted task"
                )
            if collection in SOURCE_REF_COLLECTIONS:
                label, refs = _source_label(safe_record.get("source_refs"), visible_ids["sources"])
                safe_record["source_summary"] = label
                if collection == "deliveries" and audience != "private":
                    safe_record.pop("source_refs", None)
                elif refs is None:
                    safe_record.pop("source_refs", None)
                else:
                    safe_record["source_refs"] = refs
            items.append(safe_record)
        filtered[collection] = items
    return filtered


def _display(value: Any, fallback: str = "Not recorded") -> str:
    if value is None or value == "":
        return fallback
    return html.escape(str(value), quote=True)


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or any(control in value for control in "\t\r\n"):
        return None
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.lower() not in SAFE_URL_PROTOCOLS
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value.strip()


def _source_link(source: dict[str, Any], fallback: str = "Source") -> str:
    url = _safe_url(source.get("location"))
    label = _display(source.get("type"), fallback)
    if url is None:
        return f"<span>{label}</span>"
    return f'<a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">{label}</a>'


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{text}</span>'


STATUS_TONES = {
    "in_progress": "accent",
    "done": "ok",
    "blocked": "bad",
    "conflict": "warn",
    "not_started": "neutral",
    "cancelled": "neutral",
    "unknown": "neutral",
}
STATUS_ORDER = ("in_progress", "blocked", "conflict", "not_started", "done", "cancelled", "unknown")
DECISION_TONES = {"decided": "ok", "proposed": "accent", "reversed": "warn", "conflict": "bad"}
RESULT_TONES = {"passed": "ok", "failed": "bad"}

LABELS = {
    "en": {
        "colon": ": ",
        "eyebrow": "Project dashboard",
        "audience.private": "Private view",
        "audience.team": "Team view",
        "audience.public": "Public view",
        "updated": "Updated",
        "tasks": "Current tasks",
        "milestones": "Milestones",
        "unassigned": "Unassigned",
        "workflow": "Workflow gates",
        "members": "Team roles",
        "timeline": "Update timeline",
        "decisions": "Decisions",
        "deliveries": "Deliveries",
        "risks": "Risks and learnings",
        "risks_summary": "Review recorded risks and learnings",
        "risks_label": "Risks",
        "learnings_label": "Learnings",
        "evidence": "Evidence",
        "sources": "Sources",
        "none": "None recorded",
        "not_recorded": "Not recorded",
        "no_tasks": "No current tasks",
        "no_source": "No source recorded",
        "visible_source": "Visible source",
        "restricted_source": "Restricted hidden source",
        "restricted_member": "Restricted member",
        "owner": "Owner",
        "due": "Due",
        "blocked": "Blocked",
        "gate_yes": "Human gate",
        "gate_no": "No human gate",
        "read_at": "Read",
        "source_fallback": "Source",
        "brief_audience": "Audience",
        "brief_goal": "Goal",
        "footer": "Static dashboard · no scripts · print ready",
        "status.unknown": "Unknown",
        "status.not_started": "Not started",
        "status.in_progress": "In progress",
        "status.blocked": "Blocked",
        "status.done": "Done",
        "status.conflict": "Conflict",
        "status.cancelled": "Cancelled",
        "ver.none": "No verification",
        "ver.source_report": "Source report",
        "ver.source_backed": "Source backed",
        "ver.artifact_present": "Artifact present",
        "ver.local_verified": "Locally verified",
        "ver.target_verified": "Target verified",
        "ver.e2e_verified": "End-to-end verified",
        "appr.draft": "Draft",
        "appr.reviewed": "Reviewed",
        "appr.accepted": "Accepted",
        "dec.proposed": "Proposed",
        "dec.decided": "Decided",
        "dec.reversed": "Reversed",
        "dec.conflict": "Conflict",
    },
    "zh": {
        "colon": "：",
        "eyebrow": "项目看板",
        "audience.private": "内部视图",
        "audience.team": "团队视图",
        "audience.public": "公开视图",
        "updated": "最近更新",
        "tasks": "当前任务",
        "milestones": "里程碑",
        "unassigned": "待指派",
        "workflow": "流程闸门",
        "members": "团队分工",
        "timeline": "推进时间线",
        "decisions": "拍板决策",
        "deliveries": "交付物",
        "risks": "风险与经验",
        "risks_summary": "展开查看各期风险与经验",
        "risks_label": "风险",
        "learnings_label": "经验",
        "evidence": "验证证据",
        "sources": "来源台账",
        "none": "暂无记录",
        "not_recorded": "未记录",
        "no_tasks": "暂无任务",
        "no_source": "无来源记录",
        "visible_source": "来源可见",
        "restricted_source": "来源受限已隐藏",
        "restricted_member": "成员受限",
        "owner": "负责",
        "due": "截止",
        "blocked": "受阻原因",
        "gate_yes": "人工闸门",
        "gate_no": "自动流转",
        "read_at": "读取于",
        "source_fallback": "来源",
        "brief_audience": "受众",
        "brief_goal": "目标",
        "footer": "静态看板 · 零脚本 · 可直接打印",
        "status.unknown": "未知",
        "status.not_started": "未开始",
        "status.in_progress": "推进中",
        "status.blocked": "受阻",
        "status.done": "已完成",
        "status.conflict": "冲突待裁",
        "status.cancelled": "已取消",
        "ver.none": "未验证",
        "ver.source_report": "来源口径",
        "ver.source_backed": "来源背书",
        "ver.artifact_present": "产物已存在",
        "ver.local_verified": "本地已验证",
        "ver.target_verified": "目标环境已验证",
        "ver.e2e_verified": "端到端已验证",
        "appr.draft": "草稿",
        "appr.reviewed": "已审阅",
        "appr.accepted": "已验收",
        "dec.proposed": "提议中",
        "dec.decided": "已拍板",
        "dec.reversed": "已推翻",
        "dec.conflict": "冲突待裁",
    },
}


def _labels(language: str) -> dict[str, str]:
    key = "zh" if isinstance(language, str) and language.lower().startswith("zh") else "en"
    return LABELS[key]


def _enum_label(labels: dict[str, str], prefix: str, value: Any) -> str:
    if isinstance(value, str) and f"{prefix}.{value}" in labels:
        return labels[f"{prefix}.{value}"]
    return _display(value, labels["not_recorded"])


def _theme_css() -> str:
    return """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink); font: 16px/1.65 "Inter", -apple-system, BlinkMacSystemFont, "PingFang SC", "Segoe UI", "Microsoft YaHei", sans-serif; }
a { color: var(--accent-deep); }
a:focus-visible, summary:focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; }
.shell { max-width: 68rem; margin: auto; padding: 2rem 1.5rem 3rem; }
h1, h2, h3, p, summary { overflow-wrap: anywhere; }
header { padding: 1rem 0 1.6rem; border-bottom: 1px solid var(--line); }
.eyebrow { display: inline-flex; align-items: center; gap: .5rem; margin: 0 0 1.1rem; padding: .3rem .85rem; border: 1px solid var(--line); border-radius: 999px; background: var(--card); color: var(--muted); font-size: .8rem; letter-spacing: .06em; box-shadow: var(--shadow-soft); }
.eyebrow::before { content: ""; width: .5rem; height: .5rem; border-radius: 50%; background: var(--accent); }
h1 { margin: 0 0 .6rem; font-family: Georgia, "Songti SC", "Source Han Serif SC", "Noto Serif CJK SC", "SimSun", serif; font-size: clamp(2.1rem, 5vw, 3.1rem); line-height: 1.15; }
.goal { margin: 0 0 .7rem; max-width: 46rem; font-size: 1.15rem; color: var(--ink-soft); }
.meta { color: var(--muted); font-size: .85rem; margin: .3rem 0 0; }
h2 { display: flex; align-items: center; gap: .6rem; margin: 2.4rem 0 .9rem; font-size: 1.3rem; }
h2::before { content: ""; flex: none; width: .4rem; height: 1.2rem; border-radius: 999px; background: var(--accent); }
h3 { margin: 0 0 .3rem; font-size: 1.02rem; }
article, details { background: var(--card); border: 1px solid var(--line); border-radius: 1.25rem; padding: 1rem 1.15rem; margin: .7rem 0; box-shadow: var(--shadow); }
details > article { box-shadow: none; border-radius: .9rem; }
summary { cursor: pointer; color: var(--accent-deep); font-weight: 600; }
.none { color: var(--muted); font-size: .9rem; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(7.5rem, 1fr)); gap: .8rem; }
.mstones { display: grid; grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr)); gap: .8rem; }
.mstone { background: var(--card); border: 1px solid var(--line); border-left: .35rem solid var(--accent); border-radius: 1rem; padding: .8rem 1rem .7rem; box-shadow: var(--shadow); }
.mstone time { font-size: .78rem; font-weight: 600; letter-spacing: .04em; color: var(--accent-deep); }
.mstone h3 { margin: .2rem 0 0; font-size: .95rem; }
.stat { background: var(--card); border: 1px solid var(--line); border-top: .25rem solid var(--line); border-radius: 1rem; padding: .8rem 1rem .7rem; box-shadow: var(--shadow); }
.stat.accent { border-top-color: var(--accent); } .stat.ok { border-top-color: var(--ok); } .stat.bad { border-top-color: var(--bad); } .stat.warn { border-top-color: var(--warn); }
.stat-n { display: block; font-family: Georgia, "Songti SC", serif; font-size: 1.9rem; line-height: 1.15; }
.stat-l { color: var(--muted); font-size: .8rem; }
.pills { display: flex; flex-wrap: wrap; gap: .4rem; margin: .35rem 0; }
.pill { display: inline-block; padding: .14rem .6rem; border: 1px solid transparent; border-radius: 999px; font-size: .76rem; }
.pill.accent { background: var(--accent); color: var(--on-accent); }
.pill.ok { background: var(--ok-bg); color: var(--ok); }
.pill.bad { background: var(--bad-bg); color: var(--bad); }
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.line, .pill.neutral { border-color: var(--line); color: var(--muted); background: transparent; }
.pill.gate { background: var(--tag); color: var(--accent-deep); }
.warnline { color: var(--bad); font-size: .85rem; margin: .2rem 0; }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 1.4rem; }
.step { display: flex; gap: .9rem; }
.step-n { flex: none; min-width: 1.9rem; font-family: Georgia, "Songti SC", serif; font-size: 1.35rem; font-weight: 700; color: var(--accent); }
.member { display: flex; gap: .8rem; }
.avatar { flex: none; width: 2.4rem; height: 2.4rem; display: flex; align-items: center; justify-content: center; border-radius: 50%; background: var(--tag); color: var(--accent-deep); font-weight: 700; }
.timeline { margin-left: .3rem; padding-left: 1.2rem; border-left: 2px solid var(--line); }
.timeline article { position: relative; }
.timeline article::before { content: ""; position: absolute; left: -1.75rem; top: 1.3rem; width: .7rem; height: .7rem; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 .28rem var(--page); }
time { display: block; font-family: Georgia, "Songti SC", serif; font-weight: 700; font-size: .95rem; color: var(--accent-deep); }
footer { margin-top: 2.6rem; padding-top: 1rem; border-top: 1px solid var(--line); color: var(--muted); font-size: .8rem; }
[data-theme="warm"] { --page:#fff8f1; --card:#ffffff; --ink:#1d1e1c; --ink-soft:#4a4a47; --line:#e3d6c5; --accent:#fa5d00; --accent-deep:#d94f00; --on-accent:#ffffff; --muted:#615f5c; --tag:#fee3b5; --focus:#005fcc; --ok:#1d7a3c; --ok-bg:#e3f4e6; --bad:#c72c1e; --bad-bg:#fde6e2; --warn:#9a6b00; --warn-bg:#fdf1d7; --shadow:0 .4rem 1.5rem rgba(250,166,0,.14), 0 1px 2px rgba(29,30,28,.05); --shadow-soft:0 2px .75rem rgba(227,214,197,.55); }
[data-theme="clean"] { --page:#f6f9fb; --card:#ffffff; --ink:#16202a; --ink-soft:#3c4c5c; --line:#d6e0e8; --accent:#126a8a; --accent-deep:#0e5570; --on-accent:#ffffff; --muted:#526373; --tag:#ddedf4; --focus:#7342d6; --ok:#1d7a3c; --ok-bg:#e3f4e6; --bad:#c72c1e; --bad-bg:#fdeae7; --warn:#8a6100; --warn-bg:#f6eed3; --shadow:0 .3rem 1.1rem rgba(22,32,42,.08), 0 1px 2px rgba(22,32,42,.05); --shadow-soft:0 2px .6rem rgba(22,32,42,.06); }
[data-theme="dark"] { color-scheme: dark; --page:#11161c; --card:#1b242e; --ink:#edf3f8; --ink-soft:#c6d2dd; --line:#3b4c5c; --accent:#71d0ff; --accent-deep:#9adcff; --on-accent:#0d1a24; --muted:#b3c1ce; --tag:#263746; --focus:#ffd166; --ok:#6fdd8b; --ok-bg:#173423; --bad:#ff8a7a; --bad-bg:#3a1d18; --warn:#ffd166; --warn-bg:#3a2f14; --shadow:0 .4rem 1.5rem rgba(0,0,0,.35), 0 1px 2px rgba(0,0,0,.4); --shadow-soft:0 2px .6rem rgba(0,0,0,.3); }
[data-theme="paper"] { --page:#f4f0e7; --card:#fdfcf7; --ink:#201f1b; --ink-soft:#45423a; --line:#d8d0bf; --accent:#8a5a2b; --accent-deep:#6e4722; --on-accent:#ffffff; --muted:#625e55; --tag:#e9e2d1; --focus:#005fcc; --ok:#2c6e3c; --ok-bg:#e7efdf; --bad:#a83226; --bad-bg:#f4e2dc; --warn:#8a6100; --warn-bg:#f0e8cf; --shadow:0 .3rem 1rem rgba(90,80,60,.14), 0 1px 2px rgba(32,31,27,.05); --shadow-soft:0 2px .6rem rgba(90,80,60,.1); }
@media (max-width: 40rem) { .shell { padding: 1.4rem 1rem 2rem; } .grid { grid-template-columns: 1fr; } .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } .timeline { padding-left: 1rem; } .timeline article::before { left: -1.55rem; } }
@media print { [data-theme="warm"], [data-theme="clean"], [data-theme="dark"], [data-theme="paper"] { color-scheme: light; --page:#fff; --ink:#000; --card:#fff; --ink-soft:#000; --line:#000; --accent:#000; --accent-deep:#000; --on-accent:#fff; --muted:#000; --tag:#fff; --focus:#000; --ok:#000; --ok-bg:#fff; --bad:#000; --bad-bg:#fff; --warn:#000; --warn-bg:#fff; --shadow:none; --shadow-soft:none; } body { background: #fff; color: #000; } .shell { max-width: none; } article, details, .stat { break-inside: avoid; box-shadow: none; } .pill { border-color: #000; } a { color: #000; text-decoration: underline; } }
"""


def _resolve_theme(project: dict[str, Any], theme: str | None) -> str:
    selected = theme or project.get("meta", {}).get("theme", "warm")
    if selected not in THEMES:
        expected = ", ".join(sorted(THEMES))
        raise ValueError(f"theme: invalid value {selected!r}; expected one of {expected}")
    return selected


def _language(project: dict[str, Any]) -> str:
    value = project.get("meta", {}).get("language", "und")
    return value if isinstance(value, str) and LANGUAGE_PATTERN.fullmatch(value) else "und"


def render_html(project: dict[str, Any], audience: str = "private", theme: str | None = None) -> str:
    theme = _resolve_theme(project, theme)
    data = filter_for_audience(project, audience)
    labels = _labels(_language(project))
    colon = labels["colon"]
    tasks = data["tasks"]
    member_names = {
        member["id"]: _display(member.get("display_name"), labels["not_recorded"])
        for member in data["members"]
        if isinstance(member.get("id"), str)
    }
    source_summaries = {
        "Visible source": labels["visible_source"],
        "Restricted hidden source": labels["restricted_source"],
        "No source recorded": labels["no_source"],
    }

    def source_tag(item: dict[str, Any]) -> str:
        raw = item.get("source_summary")
        return source_summaries.get(raw, _display(raw, labels["no_source"]))

    def cards(items: list[dict[str, Any]], body) -> str:
        rendered = "".join(f"<article>{body(item)}</article>" for item in items)
        return rendered or f'<p class="none">{labels["none"]}</p>'

    counts: dict[str, int] = {}
    for task in {task.get("id"): task for task in tasks if task.get("id")}.values():
        status = str(task.get("reported_status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    ordered = [status for status in STATUS_ORDER if status in counts]
    ordered.extend(status for status in sorted(counts) if status not in STATUS_ORDER)
    stat_cards = "".join(
        f'<div class="stat {STATUS_TONES.get(status, "neutral")}"><span class="stat-n">{counts[status]}</span>'
        f'<span class="stat-l">{_enum_label(labels, "status", status)}</span></div>'
        for status in ordered
    ) or f'<div class="stat neutral"><span class="stat-n">0</span><span class="stat-l">{labels["no_tasks"]}</span></div>'

    def task_card(item: dict[str, Any]) -> str:
        status = str(item.get("reported_status", "unknown"))
        pills = [_pill(_enum_label(labels, "status", status), STATUS_TONES.get(status, "neutral"))]
        for prefix, field in (("ver", "verification_level"), ("appr", "approval_state")):
            if item.get(field):
                pills.append(_pill(_enum_label(labels, prefix, item.get(field)), "line"))
        meta_parts = []
        owner = item.get("owner")
        if isinstance(owner, str) and owner in member_names:
            meta_parts.append(f"{labels['owner']}{colon}{member_names[owner]}")
        elif item.get("owner_summary"):
            meta_parts.append(f"{labels['owner']}{colon}{labels['restricted_member']}")
        elif isinstance(owner, str) and owner:
            meta_parts.append(f"{labels['owner']}{colon}{_display(owner)}")
        else:
            meta_parts.append(f"{labels['owner']}{colon}{labels['unassigned']}")
        if item.get("due_date"):
            meta_parts.append(f"{labels['due']}{colon}{_display(item.get('due_date'))}")
        blocked = (
            f'<p class="warnline">{labels["blocked"]}{colon}{_display(item.get("blocked_reason"))}</p>'
            if item.get("blocked_reason") else ""
        )
        meta = f'<p class="meta">{" · ".join(meta_parts)}</p>' if meta_parts else ""
        return (
            f'<h3>{_display(item.get("description"), labels["not_recorded"])}</h3>'
            f'<div class="pills">{"".join(pills)}</div>{blocked}{meta}'
        )

    steps = "".join(
        f'<article class="step"><span class="step-n">{index:02d}</span><div>'
        f'<h3>{_display(item.get("name"), labels["not_recorded"])}</h3>'
        f'<p>{_display(item.get("description"), labels["not_recorded"])}</p>'
        f'<div class="pills">{_pill(labels["gate_yes"] if item.get("human_gate") else labels["gate_no"], "gate" if item.get("human_gate") else "line")}</div>'
        f"</div></article>"
        for index, item in enumerate(data["workflow"], start=1)
    ) or f'<p class="none">{labels["none"]}</p>'

    def member_card(item: dict[str, Any]) -> str:
        name = item.get("display_name")
        initial = html.escape(str(name)[:1], quote=True) if isinstance(name, str) and name else "?"
        return (
            f'<article class="member"><span class="avatar" aria-hidden="true">{initial}</span><div>'
            f'<h3>{_display(name, labels["not_recorded"])}</h3>'
            f'<p>{_display(item.get("responsibility"), labels["not_recorded"])}</p></div></article>'
        )

    members_html = "".join(member_card(item) for item in data["members"]) or f'<p class="none">{labels["none"]}</p>'

    timeline = cards(data["updates"], lambda item: (
        f'<time>{_display(item.get("date"), labels["not_recorded"])}</time>'
        f'<h3>{_display(item.get("title"), labels["not_recorded"])}</h3>'
        f'<p>{_display(item.get("summary"), labels["not_recorded"])}</p>'
        f'<p class="meta">{source_tag(item)}</p>'
    ))

    def decision_card(item: dict[str, Any]) -> str:
        state = item.get("decision_state")
        meta_parts = []
        decided_by = item.get("decided_by")
        if isinstance(decided_by, str) and decided_by in member_names:
            meta_parts.append(member_names[decided_by])
        elif item.get("decided_by_summary"):
            meta_parts.append(labels["restricted_member"])
        if item.get("decided_at"):
            meta_parts.append(_display(item.get("decided_at")))
        meta_parts.append(source_tag(item))
        return (
            f'<div class="pills">{_pill(_enum_label(labels, "dec", state), DECISION_TONES.get(str(state), "neutral"))}</div>'
            f'<h3>{_display(item.get("topic"), labels["not_recorded"])}</h3>'
            f'<p>{_display(item.get("conclusion"), labels["not_recorded"])}</p>'
            f'<p class="meta">{" · ".join(meta_parts)}</p>'
        )

    decisions = cards(data["decisions"], decision_card)

    risk_items = cards(data["updates"], lambda item: (
        f'<h3>{_display(item.get("title"), labels["not_recorded"])}</h3>'
        f'<p>{labels["risks_label"]}{colon}{_display("，".join(map(str, item.get("risks", []))) if colon == "：" else ", ".join(map(str, item.get("risks", []))), labels["none"])}</p>'
        f'<p>{labels["learnings_label"]}{colon}{_display("，".join(map(str, item.get("learnings", []))) if colon == "：" else ", ".join(map(str, item.get("learnings", []))), labels["none"])}</p>'
    ))

    def evidence_card(item: dict[str, Any]) -> str:
        result = item.get("result")
        pills = [_pill(_display(result, labels["not_recorded"]), RESULT_TONES.get(str(result), "neutral"))]
        if item.get("target"):
            pills.append(_pill(_display(item.get("target")), "line"))
        meta_parts = [part for part in (_display(item.get("recorded_at"), ""), source_tag(item)) if part]
        return (
            f'<h3>{_display(item.get("artifact"), labels["not_recorded"])}</h3>'
            f'<div class="pills">{"".join(pills)}</div><p class="meta">{" · ".join(meta_parts)}</p>'
        )

    evidence = cards(data["evidence"], evidence_card)

    sources = cards(data["sources"], lambda item: (
        f'<h3>{_source_link(item, labels["source_fallback"])}</h3>'
        f'<p class="meta">{labels["read_at"]}{colon}{_display(item.get("read_at"), labels["not_recorded"])}</p>'
    ))

    deliveries_section = ""
    if data["deliveries"]:
        deliveries_section = (
            f'<section><h2>{labels["deliveries"]}</h2>'
            + cards(data["deliveries"], lambda item: (
                f'<h3>{_display(item.get("name"), labels["not_recorded"])}</h3>'
                f'<p>{_display(item.get("description"), labels["not_recorded"])}</p>'
                f'<p class="meta">{source_tag(item)}</p>'
            ))
            + "</section>"
        )

    milestones_section = ""
    milestones = data["project"].get("milestones")
    if isinstance(milestones, list):
        milestone_cards = "".join(
            '<article class="mstone">'
            f'<time>{_display(item.get("due_date"), labels["not_recorded"])}</time>'
            f'<h3>{_display(item.get("name"), labels["not_recorded"])}</h3>'
            "</article>"
            for item in milestones
            if isinstance(item, dict)
        )
        if milestone_cards:
            milestones_section = (
                f'<section><h2>{labels["milestones"]}</h2>'
                f'<div class="mstones">{milestone_cards}</div></section>'
            )

    audience_label = labels.get(f"audience.{audience}", _display(audience))
    return f"""<!doctype html>
<html lang="{html.escape(_language(project), quote=True)}" data-theme="{theme}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{_display(data['project'].get('name'), labels['not_recorded'])}</title><style>{_theme_css()}</style></head>
<body><div class="shell"><header><p class="eyebrow">{labels['eyebrow']} · {audience_label}</p><h1>{_display(data['project'].get('name'), labels['not_recorded'])}</h1><p class="goal">{_display(data['project'].get('goal'), labels['not_recorded'])}</p><p class="meta">{labels['updated']}{colon}{_display(data['meta'].get('updated_at'), labels['not_recorded'])}</p></header>
<main>{milestones_section}<section aria-labelledby="tasks-title"><h2 id="tasks-title">{labels['tasks']}</h2><div class="stats">{stat_cards}</div>{cards(tasks, task_card)}</section>
<div class="grid"><section><h2>{labels['workflow']}</h2>{steps}</section><section><h2>{labels['members']}</h2>{members_html}</section></div>
<section><h2>{labels['timeline']}</h2><div class="timeline">{timeline}</div></section>
<section><h2>{labels['decisions']}</h2>{decisions}</section>{deliveries_section}
<section><h2>{labels['risks']}</h2><details><summary>{labels['risks_summary']}</summary>{risk_items}</details></section>
<section><h2>{labels['evidence']}</h2>{evidence}</section>
<section><h2>{labels['sources']}</h2>{sources}</section></main>
<footer>{labels['footer']}</footer></div></body></html>"""


def _markdown_text(value: Any, fallback: str = "Not recorded") -> str:
    if value is None or value == "":
        return fallback
    return re.sub(r"([\\`*_{}\[\]()<>#+!|])", r"\\\1", str(value))


def _brief_markdown(data: dict[str, Any], audience: str) -> str:
    labels = _labels(_language(data))
    colon = labels["colon"]
    fallback = labels["not_recorded"]

    def enum_text(prefix: str, value: Any) -> str:
        key = f"{prefix}.{value}"
        if isinstance(value, str) and key in labels:
            return labels[key]
        return _markdown_text(value, fallback)

    task_counts: dict[str, int] = {}
    for task in {task.get("id"): task for task in data["tasks"] if task.get("id")}.values():
        status = str(task.get("reported_status", "unknown"))
        task_counts[status] = task_counts.get(status, 0) + 1
    ordered = [status for status in STATUS_ORDER if status in task_counts]
    ordered.extend(status for status in sorted(task_counts) if status not in STATUS_ORDER)
    lines = [
        f"# {_markdown_text(data['project'].get('name'), fallback)}",
        "",
        f"{labels['brief_audience']}{colon}{labels.get(f'audience.{audience}', audience)}",
        f"{labels['brief_goal']}{colon}{_markdown_text(data['project'].get('goal'), fallback)}",
        f"{labels['updated']}{colon}{_markdown_text(data['meta'].get('updated_at'), fallback)}",
        "",
        f"## {labels['tasks']}",
    ]
    lines.extend(f"- {enum_text('status', status)}{colon}{task_counts[status]}" for status in ordered)
    lines.append(f"\n## {labels['timeline']}")
    lines.extend(
        f"- {_markdown_text(item.get('date'), fallback)}{colon}{_markdown_text(item.get('title'), fallback)}"
        for item in data["updates"]
    )
    lines.append(f"\n## {labels['decisions']}")
    lines.extend(
        f"- {_markdown_text(item.get('topic'), fallback)}{colon}{_markdown_text(item.get('conclusion'), fallback)}"
        for item in data["decisions"]
    )
    return "\n".join(lines) + "\n"


def _render_file_names(audience: str) -> tuple[str, ...]:
    return (f"project.{audience}.json", "index.html", "brief.md", "manifest.json")


def _file_like(path: Path) -> bool:
    return path.is_file() or path.is_symlink()


def _remove_stage(stage: Path) -> None:
    for path in stage.iterdir():
        if _file_like(path):
            path.unlink()
    stage.rmdir()


def _commit_render(stage: Path, output: Path, audience: str) -> None:
    current = _render_file_names(audience)
    stale = tuple(f"project.{name}.json" for name in sorted(VISIBILITIES - {audience}))
    backup_order = ("manifest.json", *stale, "index.html", "brief.md", current[0])
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for name in backup_order:
            target = output / name
            if _file_like(target):
                backup = stage / f"backup.{name}"
                os.replace(target, backup)
                backups.append((target, backup))
        for name in current:
            target = output / name
            os.replace(stage / name, target)
            installed.append(target)
    except OSError:
        for target in reversed(installed):
            if _file_like(target):
                target.unlink()
        for target, backup in reversed(backups):
            if _file_like(backup):
                os.replace(backup, target)
        raise


def render_project(
    project_path: str | Path,
    output: str | Path,
    audience: str,
    theme: str | None = None,
) -> Path:
    output_path = Path(output)
    lock_path = output_path.parent / f".{output_path.name}.render.lock"
    with exclusive_lock(lock_path):
        project = load_json(project_path)
        errors = validate_project(project)
        if errors:
            raise ValueError("project is invalid:\n" + "\n".join(errors))
        data = filter_for_audience(project, audience)
        selected_theme = _resolve_theme(project, theme)
        data["meta"]["theme"] = selected_theme
        output_path.mkdir(parents=True, exist_ok=True)
        files = {
            "index.html": render_html(project, audience, selected_theme).encode("utf-8"),
            f"project.{audience}.json": (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            "brief.md": _brief_markdown(data, audience).encode("utf-8"),
        }
        manifest = {
            "audience": audience,
            "theme": selected_theme,
            "files": {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())},
        }
        files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        stage = Path(tempfile.mkdtemp(dir=output_path.parent, prefix=f".{output_path.name}.render."))
        try:
            for name, content in files.items():
                staged = stage / name
                staged.write_bytes(content)
                if hashlib.sha256(staged.read_bytes()).hexdigest() != hashlib.sha256(content).hexdigest():
                    raise OSError(f"{staged}: staged content hash mismatch")
            for name, digest in manifest["files"].items():
                if hashlib.sha256((stage / name).read_bytes()).hexdigest() != digest:
                    raise OSError(f"{stage / name}: manifest hash mismatch")
            _commit_render(stage, output_path, audience)
        finally:
            _remove_stage(stage)
    return output_path


def preview_html() -> str:
    cards = "".join(
        f'<article class="preview-card" data-theme="{theme}"><p class="eyebrow">{theme} theme</p>'
        f'<h2>Project dashboard</h2><p>Hero, stat band, workflow steps, timeline, and status pills in the {theme} palette.</p>'
        f'<div class="pills"><span class="pill accent">In progress</span><span class="pill ok">Done</span>'
        f'<span class="pill bad">Blocked</span><span class="pill gate">Human gate</span></div>'
        f'<div class="stats"><div class="stat accent"><span class="stat-n">3</span><span class="stat-l">In progress</span></div>'
        f'<div class="stat ok"><span class="stat-n">5</span><span class="stat-l">Done</span></div></div></article>'
        for theme in sorted(THEMES)
    )
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Project dashboard themes</title><style>{_theme_css()}
.preview {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:1rem; }}
.preview-card {{ margin:0; min-height:13rem; background:var(--page); color:var(--ink); }}
@media (max-width: 40rem) {{ .preview {{ grid-template-columns:1fr; }} }}
@media print {{ .preview {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }} }}
</style></head><body data-theme="clean"><main class="shell"><header><p class="eyebrow">No JavaScript · print safe</p><h1>Theme preview</h1></header><section class="preview">{cards}</section></main></body></html>'''


def preview_file(output: str | Path) -> Path:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(preview_html(), encoding="utf-8")
    return output_path


def _excluded_skill_path(relative: Path) -> bool:
    return (
        any(
            part.startswith(".")
            or part in {"dist", "__pycache__", "temp", "tmp"}
            or re.fullmatch(r".+\.backup(?:-\d+)?", part) is not None
            for part in relative.parts
        )
        or relative.suffix in {".pyc", ".tmp", ".temp", ".bak"}
    )


def _safe_relative_path(value: str | Path) -> str:
    raw = value.as_posix() if isinstance(value, Path) else value
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    components = raw.split("/")
    reserved_devices = {"con", "prn", "aux", "nul", "conin$", "conout$", *(f"com{number}" for number in range(1, 10)), *(f"lpt{number}" for number in range(1, 10))}
    if (
        not raw
        or "\\" in raw
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or any(part in {"", ".", ".."} for part in components)
        or any(part in {".", ".."} for part in (*posix.parts, *windows.parts))
        or any(
            any(character in '<>:"/\\|?*' for character in part)
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].rstrip(" .").casefold() in reserved_devices
            for part in components
        )
    ):
        raise ValueError(f"unsafe relative path: {raw}")
    return raw


def _skill_file_entries(skill_dir: str | Path) -> tuple[Path, tuple[Path, ...]]:
    requested = Path(skill_dir)
    if requested.is_symlink():
        raise ValueError(f"skill directory is not a real directory: {requested}")
    source = requested.resolve()
    if not source.is_dir():
        raise ValueError(f"skill directory is not a real directory: {requested}")
    _safe_relative_path(source.name)
    required = (source / "SKILL.md", source / "VERSION")
    for path in required:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"required file is missing or unsafe: {path.name}")

    files: list[Path] = []
    for root, directories, names in os.walk(source, followlinks=False):
        root_path = Path(root)
        for name in [*directories, *names]:
            path = root_path / name
            if path.is_symlink():
                raise ValueError(f"symlink is not allowed in skill directory: {path.relative_to(source)}")
        directories[:] = sorted(name for name in directories if not _excluded_skill_path((root_path / name).relative_to(source)))
        for name in sorted(names):
            path = root_path / name
            relative = path.relative_to(source)
            if _excluded_skill_path(relative):
                continue
            if not path.is_file():
                raise ValueError(f"skill entry is not a regular file: {relative}")
            _safe_relative_path(relative)
            files.append(relative)

    entries = tuple(sorted(files, key=lambda path: path.as_posix()))
    _validate_skill_references(source, entries)
    return source, entries


def _validate_skill_references(source: Path, entries: tuple[Path, ...]) -> None:
    text = (source / "SKILL.md").read_text(encoding="utf-8")
    available = {path.as_posix() for path in entries}
    for reference in _markdown_link_destinations(text):
        candidate = _markdown_link_target(reference).split("#", 1)[0]
        while candidate.startswith("./"):
            candidate = candidate[2:]
        parsed = urlsplit(candidate)
        if (
            not candidate
            or candidate.startswith("#")
            or parsed.netloc
            or (parsed.scheme and not (len(parsed.scheme) == 1 and candidate[1:2] == ":"))
        ):
            continue
        normalized = _safe_relative_path(candidate)
        if normalized not in available:
            raise ValueError(f"referenced file is missing: {normalized}")


def _markdown_link_destinations(text: str) -> list[str]:
    destinations: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        if fence is not None:
            current_fence = _markdown_fence(line, closing=True)
            if current_fence is not None and current_fence[0] == fence[0] and current_fence[1] >= fence[1]:
                fence = None
            continue
        current_fence = _markdown_fence(line)
        if current_fence is not None:
            fence = current_fence
            continue
        index = 0
        inline_delimiter: int | None = None
        while index < len(line):
            if line[index] == "`" and not _markdown_escaped(line, index):
                end = index
                while end < len(line) and line[end] == "`":
                    end += 1
                length = end - index
                if inline_delimiter is None and _markdown_has_inline_closer(line, end, length):
                    inline_delimiter = length
                elif inline_delimiter == length:
                    inline_delimiter = None
                index = end
                continue
            if inline_delimiter is None and line[index] == "[" and not _markdown_escaped(line, index):
                destination = _markdown_destination_at(line, index)
                if destination is not None:
                    value, index = destination
                    destinations.append(value)
                    continue
            index += 1
    return destinations


def _markdown_fence(line: str, closing: bool = False) -> tuple[str, int] | None:
    match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
    if match is None or (closing and line[match.end():].strip()):
        return None
    marker = match.group(1)
    return marker[0], len(marker)


def _markdown_escaped(text: str, index: int) -> bool:
    preceding = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        preceding += 1
        index -= 1
    return preceding % 2 == 1


def _markdown_has_inline_closer(line: str, start: int, delimiter: int) -> bool:
    index = start
    while index < len(line):
        if line[index] == "`" and not _markdown_escaped(line, index):
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            if end - index == delimiter:
                return True
            index = end
        else:
            index += 1
    return False


def _markdown_destination_at(line: str, opening: int) -> tuple[str, int] | None:
    closing = opening + 1
    while closing + 1 < len(line):
        if (
            line[closing] == "]"
            and not _markdown_escaped(line, closing)
            and line[closing + 1] == "("
            and not _markdown_escaped(line, closing + 1)
        ):
            start = closing + 2
            depth = 1
            quote = ""
            escaped = False
            for index in range(start, len(line)):
                character = line[index]
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif quote:
                    if character == quote:
                        quote = ""
                elif character in {"'", '"'}:
                    quote = character
                elif character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0:
                        return line[start:index], index + 1
            return None
        closing += 1
    return None


def _markdown_link_target(reference: str) -> str:
    content = reference.strip()
    if content.startswith("<"):
        closing = content.find(">")
        if closing <= 1:
            raise ValueError(f"unsafe Markdown link target: {reference}")
        return content[1:closing]
    depth = 0
    for index, character in enumerate(content):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character.isspace() and depth == 0:
            return content[:index]
    return content


def _windows_normalized_archive_name(name: str) -> str:
    return "/".join(component.rstrip(" .").casefold() for component in name.split("/"))


def _package_names(source_name: str, entries: tuple[Path, ...], target: str) -> tuple[str, ...]:
    prefix = "" if target == "workbuddy" else f"{_safe_relative_path(source_name)}/"
    names = tuple(prefix + _safe_relative_path(relative) for relative in entries)
    normalized = {_windows_normalized_archive_name(name) for name in names}
    if len(normalized) != len(names):
        raise ValueError("Windows-normalized collision in archive paths")
    return names


def _validate_package_output(source: Path, entries: tuple[Path, ...], output: Path) -> None:
    if output.is_symlink():
        raise ValueError(f"package output must not be a symlink: {output}")
    source_root = source.resolve()
    resolved_output = output.resolve(strict=False)
    try:
        resolved_output.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError(f"package output is inside skill source: {output}")
    if output.exists() and not output.is_file():
        raise ValueError(f"package output is not a regular file: {output}")
    for relative in entries:
        if resolved_output == (source / relative).resolve():
            raise ValueError(f"package output overlaps skill source entry: {output}")


def _validate_package_archive(
    archive_path: Path, source_name: str, entries: tuple[Path, ...], target: str
) -> None:
    expected = _package_names(source_name, entries, target)
    with zipfile.ZipFile(archive_path) as archive:
        names = tuple(archive.namelist())
    if names != expected or len(names) != len(set(names)):
        raise OSError("package archive has unsafe layout")
    for name in names:
        _safe_relative_path(name)


def package_skill(skill_dir: str | Path, target: str, output: str | Path) -> Path:
    if target not in {"standard", "workbuddy", "openclaw"}:
        raise ValueError(f"target: invalid value {target!r}")
    source, entries = _skill_file_entries(skill_dir)
    output_path = Path(output)
    _validate_package_output(source, entries, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent, prefix=f".{output_path.name}.package.", suffix=".zip"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        names = _package_names(source.name, entries, target)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, name in zip(entries, names):
                source_path = source / relative
                mode = 0o755 if source_path.stat().st_mode & 0o111 else 0o644
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100000 | mode) << 16
                archive.writestr(info, source_path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        _validate_package_archive(temporary, source.name, entries, target)
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output_path


def _install_root(target: str) -> Path:
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    if target == "claude":
        return home / ".claude" / "skills"
    if target == "codex":
        return home / ".agents" / "skills"
    if target == "workbuddy":
        return home / ".workbuddy" / "skills"
    raise ValueError(f"target: invalid value {target!r}")


def _skill_snapshot(source: Path, entries: tuple[Path, ...]) -> tuple[tuple[str, str, int], ...]:
    snapshot: list[tuple[str, str, int]] = []
    for relative in entries:
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise OSError(f"source changed after validation: {relative}")
        snapshot.append((relative.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o111))
    return tuple(snapshot)


def _copy_snapshot(source: Path, destination: Path, snapshot: tuple[tuple[str, str, int], ...]) -> None:
    for name, digest, executable in snapshot:
        relative = Path(name)
        source_path = source / relative
        content = source_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise OSError(f"source changed during copy: {relative}")
        target_path = destination / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(content)
        os.chmod(target_path, 0o755 if executable else 0o644)


def _verify_copy(destination: Path, snapshot: tuple[tuple[str, str, int], ...]) -> None:
    expected = {name: (digest, executable) for name, digest, executable in snapshot}
    actual: set[str] = set()
    for root, directories, names in os.walk(destination, followlinks=False):
        root_path = Path(root)
        for name in [*directories, *names]:
            path = root_path / name
            if path.is_symlink():
                raise OSError(f"installed tree contains symlink: {path.relative_to(destination)}")
        for name in names:
            path = root_path / name
            relative = _safe_relative_path(path.relative_to(destination))
            if not path.is_file():
                raise OSError(f"installed tree contains non-file entry: {relative}")
            actual.add(relative)
            if relative not in expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected[relative][0]:
                raise OSError(f"installed file does not match source: {relative}")
    if actual != set(expected):
        raise OSError("installed tree does not match validated whitelist")


def install_skill(skill_dir: str | Path, target: str, destination: str | Path | None = None, force: bool = False) -> Path:
    source, entries = _skill_file_entries(skill_dir)
    snapshot = _skill_snapshot(source, entries)
    root = Path(destination) if destination is not None else _install_root(target)
    installed = root / source.name
    lock_path = root / f".{source.name}.install.lock"
    with exclusive_lock(lock_path):
        rechecked_source, rechecked_entries = _skill_file_entries(source)
        if rechecked_source != source or rechecked_entries != entries or _skill_snapshot(source, entries) != snapshot:
            raise OSError("source changed after validation")
        if installed.exists() and not force:
            raise FileExistsError(f"install target already exists: {installed}")
        stage_root = Path(tempfile.mkdtemp(dir=root, prefix=f".{source.name}.install."))
        staged = stage_root / source.name
        backup: Path | None = None
        try:
            staged.mkdir()
            _copy_snapshot(source, staged, snapshot)
            if _skill_snapshot(source, entries) != snapshot:
                raise OSError("source changed during copy")
            _verify_copy(staged, snapshot)
            if installed.exists():
                backup = root / f"{source.name}.backup"
                index = 2
                while backup.exists():
                    backup = root / f"{source.name}.backup-{index}"
                    index += 1
                os.replace(installed, backup)
            try:
                os.replace(staged, installed)
                _verify_copy(installed, snapshot)
            except OSError:
                if installed.exists():
                    shutil.rmtree(installed)
                if backup is not None and backup.exists():
                    os.replace(backup, installed)
                    backup = None
                raise
        finally:
            if stage_root.exists():
                shutil.rmtree(stage_root)
    return installed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize and validate project dashboards.")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="Initialize a project directory.")
    init_parser.add_argument("target")
    init_parser.add_argument("--theme", choices=sorted(THEMES), default="warm")

    validate_parser = commands.add_parser("validate", help="Validate a project JSON file.")
    validate_parser.add_argument("project")

    merge_parser = commands.add_parser("merge", help="Merge a revision-checked partial patch.")
    merge_parser.add_argument("base")
    merge_parser.add_argument("patch")
    merge_parser.add_argument("--output", required=True)

    render_parser = commands.add_parser("render", help="Render a static audience-safe dashboard.")
    render_parser.add_argument("project")
    render_parser.add_argument("--output", required=True)
    render_parser.add_argument("--audience", choices=sorted(VISIBILITIES), required=True)
    render_parser.add_argument("--theme", choices=sorted(THEMES))

    preview_parser = commands.add_parser("preview", help="Write a static preview of every theme.")
    preview_parser.add_argument("--output", required=True)

    package_parser = commands.add_parser("package", help="Build a portable skill archive.")
    package_parser.add_argument("--skill-dir", required=True)
    package_parser.add_argument("--target", choices=("standard", "workbuddy", "openclaw"), required=True)
    package_parser.add_argument("--output", required=True)

    install_parser = commands.add_parser("install", help="Install a verified local skill copy.")
    install_parser.add_argument("--skill-dir", required=True)
    install_parser.add_argument("--target", choices=("claude", "codex", "workbuddy"), required=True)
    install_parser.add_argument("--destination")
    install_parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            project_path = init_project(args.target, args.theme)
            print(f"initialized: {project_path}")
            return 0

        if args.command == "merge":
            output_path = merge_project_file(args.base, args.patch, args.output)
            print(f"merged: {output_path}")
            return 0

        if args.command == "render":
            output_path = render_project(args.project, args.output, args.audience, args.theme)
            print(f"rendered: {output_path}")
            return 0

        if args.command == "preview":
            output_path = preview_file(args.output)
            print(f"previewed: {output_path}")
            return 0

        if args.command == "package":
            output_path = package_skill(args.skill_dir, args.target, args.output)
            print(f"packaged: {output_path}")
            return 0

        if args.command == "install":
            output_path = install_skill(args.skill_dir, args.target, args.destination, args.force)
            print(f"installed: {output_path}")
            return 0

        project_path = Path(args.project)
        errors = validate_project(load_json(project_path))
        if errors:
            print(*errors, sep="\n", file=sys.stderr)
            return 1
        print(f"valid: {project_path}")
        return 0
    except MergeConflict as error:
        print(f"conflict: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
