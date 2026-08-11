from __future__ import annotations

import copy
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "boardctl.py"
SAMPLE = ROOT / "assets" / "sample-project.json"
VERSION = ROOT / "VERSION"

if SCRIPT.exists():
    SPEC = importlib.util.spec_from_file_location("boardctl", SCRIPT)
    assert SPEC and SPEC.loader
    BOARDCTL = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(BOARDCTL)
else:
    BOARDCTL = None


class BoardctlTests(unittest.TestCase):
    def boardctl(self):
        if BOARDCTL is None:
            self.fail(f"boardctl.py is missing: {SCRIPT}")
        return BOARDCTL

    def valid_project(self):
        return self.boardctl().load_json(SAMPLE)

    def validate_without_type_error(self, project):
        try:
            return self.boardctl().validate_project(project)
        except TypeError as error:
            self.fail(f"validation raised TypeError: {error}")

    def test_canonical_skill_identity_is_heige_pm(self):
        expected = "heige-pm"
        skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        evals = json.loads((ROOT / "evals" / "evals.json").read_text(encoding="utf-8"))
        retired = "project" + "-dashboard-builder"
        offenders = []
        for path in ROOT.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".json", ".txt"}:
                if retired in path.read_text(encoding="utf-8"):
                    offenders.append(path.relative_to(ROOT).as_posix())

        self.assertEqual(expected, ROOT.name)
        self.assertIn(f"name: {expected}\n", skill_text)
        self.assertEqual(expected, evals["skill_name"])
        self.assertEqual([], offenders)

    def test_windows_lock_backend_locks_one_byte_and_unlocks_after_exception(self):
        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def __init__(self):
                self.locking = mock.Mock()

        fake_msvcrt = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / ".dashboard.lock"
            with mock.patch.object(self.boardctl(), "_fcntl", None, create=True), mock.patch.object(
                self.boardctl(), "_msvcrt", fake_msvcrt, create=True
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                    with self.boardctl().exclusive_lock(lock_path):
                        raise RuntimeError("synthetic failure")

            self.assertEqual(b"\0", lock_path.read_bytes())
            self.assertEqual(2, fake_msvcrt.locking.call_count)
            self.assertEqual((fake_msvcrt.LK_NBLCK, 1), fake_msvcrt.locking.call_args_list[0].args[1:])
            self.assertEqual((fake_msvcrt.LK_UNLCK, 1), fake_msvcrt.locking.call_args_list[1].args[1:])

    def test_windows_lock_retries_contention_with_nonblocking_lock(self):
        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def __init__(self):
                self.attempts = 0
                self.calls = []

            def locking(self, file_descriptor, mode, size):
                self.calls.append((file_descriptor, mode, size))
                if mode == self.LK_UNLCK:
                    return
                self.assertEqual(self.LK_NBLCK, mode)
                self.attempts += 1
                if self.attempts <= 2:
                    raise OSError(errno.EACCES, "locked")

            def assertEqual(self, expected, actual):
                if expected != actual:
                    raise AssertionError(f"expected lock mode {expected}, got {actual}")

        fake_msvcrt = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as temporary_directory, mock.patch("time.sleep") as sleeper:
            with mock.patch.object(self.boardctl(), "_fcntl", None), mock.patch.object(
                self.boardctl(), "_msvcrt", fake_msvcrt
            ):
                with self.boardctl().exclusive_lock(Path(temporary_directory) / ".dashboard.lock"):
                    pass

        self.assertEqual(3, fake_msvcrt.attempts)
        self.assertEqual([fake_msvcrt.LK_NBLCK] * 3 + [fake_msvcrt.LK_UNLCK], [call[1] for call in fake_msvcrt.calls])
        self.assertEqual([mock.call(0.01), mock.call(0.01)], sleeper.call_args_list)

    def test_windows_lock_reraises_non_contention_errors_without_sleeping(self):
        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def __init__(self):
                self.calls = []

            def locking(self, file_descriptor, mode, size):
                self.calls.append((file_descriptor, mode, size))
                if mode != self.LK_NBLCK:
                    raise AssertionError(f"expected lock mode {self.LK_NBLCK}, got {mode}")
                raise OSError(errno.EPERM, "denied")

        fake_msvcrt = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as temporary_directory, mock.patch("time.sleep") as sleeper:
            with mock.patch.object(self.boardctl(), "_fcntl", None), mock.patch.object(
                self.boardctl(), "_msvcrt", fake_msvcrt
            ):
                with self.assertRaisesRegex(OSError, "denied"):
                    with self.boardctl().exclusive_lock(Path(temporary_directory) / ".dashboard.lock"):
                        self.fail("lock should not be acquired")

        self.assertEqual(1, len(fake_msvcrt.calls))
        sleeper.assert_not_called()

    def test_lock_without_backend_errors_before_creating_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / ".dashboard.lock"
            with mock.patch.object(self.boardctl(), "_fcntl", None, create=True), mock.patch.object(
                self.boardctl(), "_msvcrt", None, create=True
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "No supported file-lock backend is available"
                ):
                    with self.boardctl().exclusive_lock(lock_path):
                        self.fail("lock should not be acquired")

            self.assertFalse(lock_path.exists())

    def test_valid_synthetic_project(self):
        project = self.valid_project()

        self.assertEqual([], self.boardctl().validate_project(project))

    def test_schema_version_must_be_exactly_string_1_0_before_merge_or_render(self):
        for value in (None, 1, "0.9", "999"):
            with self.subTest(value=repr(value)):
                project = self.valid_project()
                if value is None:
                    project["meta"].pop("schema_version")
                else:
                    project["meta"]["schema_version"] = value

                self.assertEqual(
                    ["meta.schema_version: expected string '1.0'"],
                    self.boardctl().validate_project(project),
                )
                with self.assertRaisesRegex(ValueError, r"meta\.schema_version: expected string '1\.0'"):
                    self.boardctl().merge_project(project, {})
                with tempfile.TemporaryDirectory() as temporary_directory:
                    invalid = Path(temporary_directory) / "project.json"
                    invalid.write_text(json.dumps(project), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, r"meta\.schema_version: expected string '1\.0'"):
                        self.boardctl().render_project(invalid, Path(temporary_directory) / "dashboard", "private")

    def test_package_versions_match_sample_and_initialized_project(self):
        canonical_version = VERSION.read_text(encoding="utf-8").strip()
        sample = self.valid_project()
        with tempfile.TemporaryDirectory() as temporary_directory:
            initialized = self.boardctl().init_project(Path(temporary_directory) / "dashboard")
            project = self.boardctl().load_json(initialized)

        self.assertEqual(canonical_version, sample["meta"]["skill_version"])
        self.assertEqual(canonical_version, project["meta"]["skill_version"])

    def test_init_uses_a_valid_canonical_semver_version(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "VERSION").write_text("2.3.4-rc.1+build.5\n", encoding="utf-8")
            with mock.patch.object(self.boardctl(), "PACKAGE_ROOT", directory):
                initialized = self.boardctl().init_project(directory / "dashboard")
            project = self.boardctl().load_json(initialized)

        self.assertEqual("2.3.4-rc.1+build.5", project["meta"]["skill_version"])
        self.assertEqual([], self.boardctl().validate_project(project))

    def test_init_rejects_invalid_canonical_versions_without_creating_project(self):
        for raw_version in ("", "\n", " 1.0.0\n", "1.0.0 \n", "1.0.0\n2.0.0\n", "not-a-version\n"):
            with self.subTest(raw_version=repr(raw_version)), tempfile.TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                target = directory / "dashboard"
                (directory / "VERSION").write_text(raw_version, encoding="utf-8")

                with mock.patch.object(self.boardctl(), "PACKAGE_ROOT", directory), mock.patch.object(
                    sys, "stderr", new=io.StringIO()
                ) as stderr:
                    self.assertEqual(2, self.boardctl().main(["init", str(target)]))

                self.assertEqual("VERSION: expected one non-empty SemVer line\n", stderr.getvalue())

                self.assertFalse((target / "project.json").exists())

    def test_malformed_skill_versions_are_rejected(self):
        for version in (
            "",
            "1.0",
            "01.0.0",
            "1\u0662.0.0",
            "1.0.0-01",
            "1.0.0-1\u0662",
            "1.0.0-\u0661rc",
            "1.0.0+build..5",
            "1.0.0+build\u0662",
            "1.0.0 beta",
        ):
            with self.subTest(version=version):
                project = self.valid_project()
                project["meta"]["skill_version"] = version

                self.assertEqual(
                    ["meta.skill_version: expected SemVer"],
                    self.boardctl().validate_project(project),
                )

    def test_workflow_and_milestone_visibility_are_required(self):
        project = self.valid_project()
        project["workflow"][0].pop("visibility", None)
        project["project"]["milestones"][0].pop("visibility", None)

        self.assertEqual(
            [
                "project.milestones[0].visibility: expected string",
                "workflow[0].visibility: expected string",
            ],
            self.boardctl().validate_project(project),
        )

    def test_workflow_and_milestone_visibility_must_be_valid(self):
        project = self.valid_project()
        project["workflow"][0]["visibility"] = "internal"
        project["project"]["milestones"][0]["visibility"] = "internal"

        self.assertEqual(
            [
                "project.milestones[0].visibility: invalid value 'internal'; expected one of private, public, team",
                "workflow[0].visibility: invalid value 'internal'; expected one of private, public, team",
            ],
            self.boardctl().validate_project(project),
        )

    def test_duplicate_ids_are_rejected(self):
        project = self.valid_project()
        project["tasks"].append(copy.deepcopy(project["tasks"][0]))

        self.assertEqual(
            ["tasks[1].id: duplicate ID 'task-plan' (first at tasks[0].id)"],
            self.boardctl().validate_project(project),
        )

    def test_missing_source_refs_are_rejected(self):
        project = self.valid_project()
        project["tasks"][0]["source_refs"] = ["src-missing"]

        self.assertEqual(
            ["tasks[0].source_refs[0]: unknown source ID 'src-missing'"],
            self.boardctl().validate_project(project),
        )

    def test_invalid_enums_are_rejected(self):
        project = self.valid_project()
        project["tasks"][0]["reported_status"] = "finished"

        self.assertEqual(
            [
                "tasks[0].reported_status: invalid value 'finished'; expected one of "
                "blocked, cancelled, conflict, done, in_progress, not_started, unknown"
            ],
            self.boardctl().validate_project(project),
        )

    def test_non_string_enum_values_yield_path_errors(self):
        project = self.valid_project()
        project["meta"]["theme"] = ["warm"]
        project["tasks"][0]["reported_status"] = {"value": "done"}
        project["decisions"][0]["decision_state"] = ["decided"]

        self.assertEqual(
            [
                "decisions[0].decision_state: expected string",
                "meta.theme: expected string",
                "tasks[0].reported_status: expected string",
            ],
            self.validate_without_type_error(project),
        )

    def test_unsafe_url_protocols_are_rejected(self):
        project = self.valid_project()
        project["sources"][0]["location"] = "javascript:alert('synthetic')"

        self.assertEqual(
            ["sources[0].location: unsafe URL protocol 'javascript'"],
            self.boardctl().validate_project(project),
        )

    def test_url_protocols_cannot_be_obscured_with_ascii_controls(self):
        for control in ("\t", "\r", "\n"):
            with self.subTest(control=repr(control)):
                project = self.valid_project()
                project["sources"][0]["location"] = f"java{control}script:alert('synthetic')"

                self.assertEqual(
                    ["sources[0].location: unsafe URL protocol 'javascript'"],
                    self.boardctl().validate_project(project),
                )

    def test_unsafe_html_in_source_text_is_rejected(self):
        project = self.valid_project()
        project["tasks"][0]["description"] = "<script>alert('synthetic')</script>"

        self.assertEqual(
            ["tasks[0].description: contains unsafe HTML"],
            self.boardctl().validate_project(project),
        )

    def test_absolute_local_paths_are_rejected(self):
        project = self.valid_project()
        project["sources"][0]["location"] = "/" + "Users/synthetic-person/private/notes.md"

        self.assertEqual(
            ["sources[0].location: contains absolute local path"],
            self.boardctl().validate_project(project),
        )

    def test_common_absolute_tilde_drive_and_unc_paths_are_rejected(self):
        local_paths = (
            "/" + "Applications/Synthetic.app/Contents/info.txt",
            "/" + "Library/Synthetic/settings.json",
            "~" + "/private/notes.md",
            "C:" + "\\Synthetic\\private.txt",
            "\\\\" + "synthetic-host\\private\\notes.txt",
        )

        for local_path in local_paths:
            with self.subTest(local_path=local_path):
                self.assertEqual(
                    ["note: contains absolute local path"],
                    self.boardctl().scan_sensitive({"note": local_path}),
                )

    def test_generic_posix_absolute_paths_are_rejected(self):
        local_paths = (
            "/" + "workspace/acme/private.json",
            "/" + "custom/private/notes.md",
            "/" + "app",
            "/" + "data",
            "/" + "mnt",
            "`" + "/" + "Users/synthetic/file.md`",
        )

        for local_path in local_paths:
            with self.subTest(local_path=local_path):
                self.assertEqual(
                    ["note: contains absolute local path"],
                    self.boardctl().scan_sensitive({"note": local_path}),
                )

    def test_urls_and_markdown_links_are_not_local_paths(self):
        payload = {
            "href": "/" + "Users/synthetic/profile",
            "markdown": "[Synthetic guide](" + "/" + "Users/synthetic/profile)",
            "url": "https://example.test/Users/synthetic/profile",
        }

        self.assertEqual([], self.boardctl().scan_sensitive(payload))

    def test_unhashable_source_ref_yields_path_error(self):
        project = self.valid_project()
        project["tasks"][0]["source_refs"] = [{"id": "src-kickoff"}]

        self.assertEqual(
            ["tasks[0].source_refs[0]: expected string"],
            self.validate_without_type_error(project),
        )

    def test_unhashable_evidence_task_id_yields_path_error(self):
        project = self.valid_project()
        project["evidence"][0]["task_id"] = ["task-plan"]

        self.assertEqual(
            ["evidence[0].task_id: expected string"],
            self.validate_without_type_error(project),
        )

    def test_evidence_task_id_must_reference_an_existing_task(self):
        project = self.valid_project()
        project["evidence"][0]["task_id"] = "task-missing"

        self.assertEqual(
            ["evidence[0].task_id: unknown task ID 'task-missing'"],
            self.boardctl().validate_project(project),
        )

    def test_done_task_cannot_claim_elevated_verification_without_evidence(self):
        project = self.valid_project()
        project["tasks"][0]["reported_status"] = "done"
        project["tasks"][0]["verification_level"] = "local_verified"
        project["evidence"] = []

        self.assertEqual(
            [
                "tasks[0].verification_level: 'local_verified' requires evidence "
                "for done task 'task-plan'"
            ],
            self.boardctl().validate_project(project),
        )

    def test_scan_sensitive_finds_secret_patterns(self):
        payload = {"note": "api_key=synthetic-secret-value-123456"}

        self.assertEqual(
            ["note: contains possible secret"],
            self.boardctl().scan_sensitive(payload),
        )

    def test_validation_errors_are_sorted_and_repeatable(self):
        project = self.valid_project()
        project["tasks"][0]["reported_status"] = "finished"
        project["sources"][0]["location"] = "data:text/html,<script>x</script>"

        first = self.boardctl().validate_project(project)
        second = self.boardctl().validate_project(project)

        self.assertEqual(first, second)
        self.assertEqual(sorted(first), first)
        self.assertTrue(all(": " in error for error in first))

    def test_mutable_records_require_integer_revisions(self):
        project = self.valid_project()
        del project["tasks"][0]["revision"]
        project["decisions"][0]["revision"] = "1"

        self.assertEqual(
            [
                "decisions[0].revision: expected integer",
                "tasks[0].revision: expected integer",
            ],
            self.boardctl().validate_project(project),
        )

    def test_decision_supersedes_must_reference_existing_decision(self):
        project = self.valid_project()
        project["decisions"][0]["supersedes"] = ["decision-missing"]

        self.assertEqual(
            ["decisions[0].supersedes[0]: unknown decision ID 'decision-missing'"],
            self.boardctl().validate_project(project),
        )

    def test_decision_cannot_supersede_itself(self):
        project = self.valid_project()
        project["decisions"][0]["supersedes"] = ["decision-scope"]

        self.assertEqual(
            ["decisions[0].supersedes[0]: decision cannot supersede itself"],
            self.boardctl().validate_project(project),
        )

    def test_decision_supersedes_history_cannot_contain_cycle(self):
        project = self.valid_project()
        project["decisions"][0]["supersedes"] = ["decision-next"]
        next_decision = copy.deepcopy(project["decisions"][0])
        next_decision["id"] = "decision-next"
        next_decision["supersedes"] = ["decision-scope"]
        project["decisions"].append(next_decision)

        self.assertEqual(
            [
                "decisions[0].supersedes[0]: decision history contains a cycle",
                "decisions[1].supersedes[0]: decision history contains a cycle",
            ],
            self.boardctl().validate_project(project),
        )

    def test_merge_task_with_matching_revision_updates_and_increments(self):
        base = self.valid_project()
        patch = {
            "tasks": [
                {
                    "id": "task-plan",
                    "base_revision": 1,
                    "reported_status": "blocked",
                    "blocked_reason": "Waiting for the reviewed source.",
                }
            ]
        }

        merged = self.boardctl().merge_project(base, patch)

        self.assertEqual("in_progress", base["tasks"][0]["reported_status"])
        self.assertEqual("blocked", merged["tasks"][0]["reported_status"])
        self.assertEqual(2, merged["tasks"][0]["revision"])
        self.assertNotIn("base_revision", merged["tasks"][0])
        self.assertEqual([], self.boardctl().validate_project(merged))

    def test_merge_rejects_stale_task_revision(self):
        base = self.valid_project()
        patch = {
            "tasks": [
                {
                    "id": "task-plan",
                    "base_revision": 0,
                    "reported_status": "blocked",
                }
            ]
        }

        with self.assertRaisesRegex(
            self.boardctl().MergeConflict,
            r"tasks\[0\]\.base_revision: conflict for task 'task-plan': expected 1, got 0",
        ):
            self.boardctl().merge_project(base, patch)

    def test_merge_rejects_unknown_task_id(self):
        base = self.valid_project()
        patch = {
            "tasks": [
                {
                    "id": "task-missing",
                    "base_revision": 1,
                    "reported_status": "blocked",
                }
            ]
        }

        with self.assertRaisesRegex(
            self.boardctl().MergeConflict,
            r"tasks\[0\]\.id: unknown task ID 'task-missing'",
        ):
            self.boardctl().merge_project(base, patch)

    def test_merge_rejects_changed_decision_conclusion_in_place(self):
        base = self.valid_project()
        patch = {
            "decisions": [
                {
                    "id": "decision-scope",
                    "revision": 1,
                    "conclusion": "Publish a hosted dashboard.",
                }
            ]
        }

        with self.assertRaisesRegex(
            self.boardctl().MergeConflict,
            r"decisions\[0\]\.id: decision 'decision-scope' is immutable; add a new decision with supersedes",
        ):
            self.boardctl().merge_project(base, patch)

    def test_merge_adds_replacement_decision_with_explicit_supersedes(self):
        base = self.valid_project()
        patch = {
            "decisions": [
                {
                    "id": "decision-hosting",
                    "topic": "Hosting approach",
                    "conclusion": "Publish only after separate human approval.",
                    "decision_state": "proposed",
                    "decided_by": "member-coordinator",
                    "source_refs": ["src-kickoff"],
                    "decided_at": "2026-08-11T08:10:00Z",
                    "revision": 1,
                    "supersedes": ["decision-scope"],
                }
            ]
        }

        merged = self.boardctl().merge_project(base, patch)

        self.assertEqual("decision-hosting", merged["decisions"][-1]["id"])
        self.assertEqual(["decision-scope"], merged["decisions"][-1]["supersedes"])
        self.assertEqual([], self.boardctl().validate_project(merged))

    def test_merge_rejects_new_decision_without_explicit_supersedes(self):
        base = self.valid_project()
        patch = {"decisions": [{"id": "decision-unrelated", "revision": 1}]}

        with self.assertRaisesRegex(
            self.boardctl().PatchFormatError,
            r"decisions\[0\]\.supersedes: missing required field",
        ):
            self.boardctl().merge_project(base, patch)

    def test_merge_accepts_new_unrelated_decision_with_explicit_empty_supersedes(self):
        base = self.valid_project()
        patch = {
            "decisions": [
                {
                    "id": "decision-reporting",
                    "topic": "Reporting cadence",
                    "conclusion": "Publish a weekly update.",
                    "decision_state": "proposed",
                    "decided_by": "member-coordinator",
                    "source_refs": ["src-kickoff"],
                    "decided_at": "2026-08-11T08:20:00Z",
                    "revision": 1,
                    "supersedes": [],
                }
            ]
        }

        merged = self.boardctl().merge_project(base, patch)

        self.assertEqual([], merged["decisions"][-1]["supersedes"])
        self.assertEqual([], self.boardctl().validate_project(merged))

    def test_merge_appends_sources_evidence_and_updates_with_source_refs(self):
        base = self.valid_project()
        patch = {
            "sources": [
                {
                    "id": "src-review",
                    "type": "text",
                    "location": "https://example.test/sources/review",
                    "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "revision": "revision-2",
                    "read_at": "2026-08-11T08:00:00Z",
                    "sensitivity": "team",
                }
            ],
            "evidence": [
                {
                    "id": "evidence-review",
                    "task_id": "task-plan",
                    "artifact": "Reviewed meeting notes",
                    "command": "",
                    "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "target": "local",
                    "recorded_at": "2026-08-11T08:15:00Z",
                    "result": "passed",
                    "source_refs": ["src-review"],
                }
            ],
            "updates": [
                {
                    "id": "update-review",
                    "date": "2026-08-11",
                    "title": "Review complete",
                    "summary": "The meeting source was recorded.",
                    "sections": [],
                    "task_changes": [],
                    "risks": [],
                    "learnings": [],
                    "source_refs": ["src-review"],
                }
            ],
        }

        merged = self.boardctl().merge_project(base, patch)

        self.assertEqual("src-review", merged["sources"][-1]["id"])
        self.assertEqual("evidence-review", merged["evidence"][-1]["id"])
        self.assertEqual("update-review", merged["updates"][-1]["id"])
        self.assertEqual([], self.boardctl().validate_project(merged))

    def test_merge_cli_leaves_output_unchanged_on_conflict(self):
        base = self.valid_project()
        patch = {
            "tasks": [
                {
                    "id": "task-plan",
                    "base_revision": 0,
                    "reported_status": "blocked",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            base_path = directory / "base.json"
            patch_path = directory / "patch.json"
            output_path = base_path
            original = json.dumps(base)
            base_path.write_text(original, encoding="utf-8")
            patch_path.write_text(json.dumps(patch), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "merge",
                    str(base_path),
                    str(patch_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(1, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual(original, output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "conflict: tasks[0].base_revision: conflict for task 'task-plan': expected 1, got 0\n",
                result.stderr,
            )

    def test_merge_cli_reports_malformed_patch_as_format_error(self):
        base = self.valid_project()
        patch = {"tasks": {"id": "task-plan", "base_revision": 1}}

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            project_path = directory / "project.json"
            patch_path = directory / "patch.json"
            original = json.dumps(base)
            project_path.write_text(original, encoding="utf-8")
            patch_path.write_text(json.dumps(patch), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "merge",
                    str(project_path),
                    str(patch_path),
                    "--output",
                    str(project_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual("tasks: expected array\n", result.stderr)
            self.assertEqual(original, project_path.read_text(encoding="utf-8"))

    def test_merge_cli_reports_missing_supersedes_as_format_error(self):
        base = self.valid_project()
        patch = {"decisions": [{"id": "decision-reporting", "revision": 1}]}

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            project_path = directory / "project.json"
            patch_path = directory / "patch.json"
            original = json.dumps(base)
            project_path.write_text(original, encoding="utf-8")
            patch_path.write_text(json.dumps(patch), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "merge",
                    str(project_path),
                    str(patch_path),
                    "--output",
                    str(project_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual(
                "decisions[0].supersedes: missing required field\n",
                result.stderr,
            )
            self.assertEqual(original, project_path.read_text(encoding="utf-8"))

    def test_concurrent_in_place_merges_allow_exactly_one_revision_winner(self):
        base = self.valid_project()
        patches = (
            {
                "tasks": [
                    {
                        "id": "task-plan",
                        "base_revision": 1,
                        "reported_status": "blocked",
                        "blocked_reason": "Waiting for review.",
                    }
                ]
            },
            {
                "tasks": [
                    {
                        "id": "task-plan",
                        "base_revision": 1,
                        "reported_status": "cancelled",
                    }
                ]
            },
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            project_path = directory / "project.json"
            project_path.write_text(json.dumps(base), encoding="utf-8")
            fifo_paths = (directory / "patch-one.fifo", directory / "patch-two.fifo")
            for fifo_path in fifo_paths:
                os.mkfifo(fifo_path)

            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "merge",
                        str(project_path),
                        str(fifo_path),
                        "--output",
                        str(project_path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for fifo_path in fifo_paths
            ]
            writers = []
            try:
                deadline = time.monotonic() + 5
                for fifo_path in fifo_paths:
                    while True:
                        try:
                            writers.append(os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK))
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                self.fail(f"merge process did not open patch FIFO: {fifo_path}")
                            time.sleep(0.01)
                for writer, patch in zip(writers, patches):
                    os.write(writer, json.dumps(patch).encode("utf-8"))
                    os.close(writer)
                writers.clear()
                outputs = [process.communicate(timeout=5) for process in processes]
            finally:
                for writer in writers:
                    os.close(writer)
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait()

            returncodes = [process.returncode for process in processes]
            try:
                merged = json.loads(project_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                self.fail(f"concurrent merge produced invalid JSON: {error}")

            self.assertEqual([0, 1], sorted(returncodes), outputs)
            self.assertEqual(2, merged["tasks"][0]["revision"])
            self.assertIn(merged["tasks"][0]["reported_status"], {"blocked", "cancelled"})
            self.assertEqual(1, sum(stdout.startswith("merged: ") for stdout, _ in outputs))
            self.assertEqual(
                1,
                sum(
                    "conflict for task 'task-plan': expected 2, got 1" in stderr
                    for _, stderr in outputs
                ),
            )

    def test_merge_cli_updates_in_place_successfully(self):
        base = self.valid_project()
        patch = {
            "tasks": [
                {
                    "id": "task-plan",
                    "base_revision": 1,
                    "reported_status": "blocked",
                    "blocked_reason": "Waiting for review.",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            project_path = directory / "project.json"
            patch_path = directory / "patch.json"
            project_path.write_text(json.dumps(base), encoding="utf-8")
            patch_path.write_text(json.dumps(patch), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "merge",
                    str(project_path),
                    str(patch_path),
                    "--output",
                    str(project_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            merged = json.loads(project_path.read_text(encoding="utf-8"))

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(2, merged["tasks"][0]["revision"])
            self.assertEqual("blocked", merged["tasks"][0]["reported_status"])
            self.assertEqual([], self.boardctl().validate_project(merged))

    def test_merge_cli_final_validation_failure_preserves_existing_output(self):
        base = self.valid_project()
        patch = {
            "tasks": [
                {
                    "id": "task-plan",
                    "base_revision": 1,
                    "reported_status": "finished",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            project_path = directory / "project.json"
            patch_path = directory / "patch.json"
            original = json.dumps(base)
            project_path.write_text(original, encoding="utf-8")
            patch_path.write_text(json.dumps(patch), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "merge",
                    str(project_path),
                    str(patch_path),
                    "--output",
                    str(project_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertIn("merged project is invalid:", result.stderr)
            self.assertEqual(original, project_path.read_text(encoding="utf-8"))

    def test_merge_cli_rejects_existing_output_that_differs_from_base(self):
        base = self.valid_project()
        existing = copy.deepcopy(base)
        existing["tasks"][0]["revision"] = 2
        existing["tasks"][0]["reported_status"] = "blocked"
        patch = {
            "tasks": [
                {
                    "id": "task-plan",
                    "base_revision": 1,
                    "reported_status": "cancelled",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            base_path = directory / "base.json"
            patch_path = directory / "patch.json"
            output_path = directory / "output.json"
            original = json.dumps(existing)
            base_path.write_text(json.dumps(base), encoding="utf-8")
            patch_path.write_text(json.dumps(patch), encoding="utf-8")
            output_path.write_text(original, encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "merge",
                    str(base_path),
                    str(patch_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(1, result.returncode)
            self.assertEqual(
                f"conflict: {output_path}: existing output differs from base {base_path}\n",
                result.stderr,
            )
            self.assertEqual(original, output_path.read_text(encoding="utf-8"))

    def test_atomic_write_failure_preserves_old_output_and_removes_temp(self):
        project = self.valid_project()
        self.assertTrue(
            hasattr(self.boardctl(), "_write_json_atomic"),
            "atomic JSON writer is missing",
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_path = directory / "project.json"
            output_path.write_text("unchanged", encoding="utf-8")

            with mock.patch("os.replace", side_effect=OSError("synthetic replace failure")):
                with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    self.boardctl()._write_json_atomic(output_path, project)

            self.assertEqual("unchanged", output_path.read_text(encoding="utf-8"))
            self.assertEqual([], list(directory.glob(".project.json.*.tmp")))

    def test_init_cli_creates_a_valid_project_with_selected_theme(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "dashboard"

            initialized = subprocess.run(
                [sys.executable, str(SCRIPT), "init", str(target), "--theme", "paper"],
                capture_output=True,
                text=True,
                check=False,
            )
            project_path = target / "project.json"

            self.assertEqual(0, initialized.returncode, initialized.stderr)
            self.assertTrue(project_path.is_file())
            self.assertEqual("paper", json.loads(project_path.read_text())["meta"]["theme"])

            validated = subprocess.run(
                [sys.executable, str(SCRIPT), "validate", str(project_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, validated.returncode, validated.stderr)
            self.assertEqual(f"valid: {project_path}\n", validated.stdout)

    def test_validate_cli_exits_nonzero_and_prints_deterministic_errors(self):
        project = self.valid_project()
        project["tasks"][0]["reported_status"] = "finished"

        with tempfile.TemporaryDirectory() as temporary_directory:
            project_path = Path(temporary_directory) / "project.json"
            project_path.write_text(json.dumps(project), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "validate", str(project_path)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual(
            "tasks[0].reported_status: invalid value 'finished'; expected one of "
            "blocked, cancelled, conflict, done, in_progress, not_started, unknown\n",
            result.stderr,
        )

    def test_filter_for_audience_removes_private_sources_and_redacts_refs(self):
        project = self.valid_project()
        project["meta"]["audience"] = "team"
        project["sources"].append(
            {
                "id": "src-public",
                "type": "text",
                "location": "https://example.test/public",
                "sha256": "c" * 64,
                "revision": "public-1",
                "read_at": "2026-08-10T08:00:00Z",
                "sensitivity": "public",
            }
        )
        project["sources"][0]["sensitivity"] = "team"
        project["sources"][-1]["private_note"] = "do not publish"
        project["updates"][0]["visibility"] = "public"
        project["updates"][0]["source_refs"] = ["src-kickoff", "src-public"]

        public = self.boardctl().filter_for_audience(project, "public")
        team = self.boardctl().filter_for_audience(project, "team")
        private = self.boardctl().filter_for_audience(project, "private")

        self.assertEqual(["src-public"], [source["id"] for source in public["sources"]])
        self.assertNotIn("sha256", public["sources"][0])
        self.assertNotIn("private_note", public["sources"][0])
        self.assertNotIn("source_refs", public["updates"][0])
        self.assertEqual("Restricted hidden source", public["updates"][0]["source_summary"])
        self.assertIn("src-kickoff", [source["id"] for source in team["sources"]])
        self.assertEqual("do not publish", private["sources"][-1]["private_note"])
        self.assertEqual("team", private["meta"]["audience"])

    def test_nonprivate_delivery_source_refs_are_never_exposed(self):
        for audience, source_visibility, expected_summary in (
            ("public", "public", "Visible source"),
            ("team", "team", "Visible source"),
            ("public", "private", "Restricted hidden source"),
            ("team", "private", "Restricted hidden source"),
        ):
            with self.subTest(audience=audience, source_visibility=source_visibility):
                project = self.valid_project()
                project["sources"][0]["sensitivity"] = source_visibility
                project["deliveries"].append(
                    {
                        "id": f"delivery-{audience}-{source_visibility}",
                        "name": "Audience delivery",
                        "description": "Audience-safe handoff",
                        "visibility": audience,
                        "source_refs": ["src-kickoff"],
                    }
                )
                invalid = copy.deepcopy(project)
                invalid["deliveries"][0]["source_refs"] = ["src-missing"]

                self.assertEqual(
                    ["deliveries[0].source_refs[0]: unknown source ID 'src-missing'"],
                    self.boardctl().validate_project(invalid),
                )
                view = self.boardctl().filter_for_audience(project, audience)
                delivery = view["deliveries"][0]

                self.assertNotIn("source_refs", delivery)
                self.assertEqual(expected_summary, delivery["source_summary"])
                self.assertNotIn("src-kickoff", json.dumps(delivery, sort_keys=True))
                if source_visibility == "private":
                    self.assertNotIn("src-kickoff", json.dumps(view, sort_keys=True))

        private = self.valid_project()
        private["deliveries"].append(
            {
                "id": "delivery-private",
                "name": "Private delivery",
                "description": "Private handoff",
                "visibility": "private",
                "source_refs": ["src-kickoff"],
            }
        )
        self.assertEqual(["src-kickoff"], self.boardctl().filter_for_audience(private, "private")["deliveries"][0]["source_refs"])

    def test_delivery_source_refs_are_optional(self):
        project = self.valid_project()
        project["deliveries"].append(
            {
                "id": "delivery-no-source",
                "name": "Source-free delivery",
                "description": "A valid delivery without provenance references",
                "visibility": "private",
            }
        )

        self.assertEqual([], self.boardctl().validate_project(project))

    def test_audience_filter_fails_closed_for_workflow_and_milestones_without_visibility(self):
        project = self.valid_project()
        project["workflow"][0].pop("visibility", None)
        project["project"]["milestones"][0].pop("visibility", None)

        public = self.boardctl().filter_for_audience(project, "public")
        private = self.boardctl().filter_for_audience(project, "private")

        self.assertEqual([], public["workflow"])
        self.assertEqual([], public["project"]["milestones"])
        self.assertEqual("stage-plan", private["workflow"][0]["id"])
        self.assertEqual("milestone-alpha", private["project"]["milestones"][0]["id"])

    def test_render_html_escapes_unsafe_content_and_omits_active_or_remote_content(self):
        project = self.valid_project()
        project["project"]["name"] = '<img src=x onerror=alert(1)>'
        project["workflow"][0]["description"] = '<script>alert(1)</script>'
        project["sources"][0]["location"] = "javascript:alert(1)"

        html = self.boardctl().render_html(project, "private", "warm")

        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("<form", html.lower())
        self.assertNotIn("javascript:", html.lower())
        self.assertNotRegex(html.lower(), r"https?://")
        self.assertIn("<details>", html)
        self.assertIn("<header>", html)
        self.assertIn("<main>", html)

    def test_render_html_supports_all_themes_and_avoids_fixed_overflow_widths(self):
        for theme in ("warm", "clean", "dark", "paper"):
            with self.subTest(theme=theme):
                html = self.boardctl().render_html(self.valid_project(), "private", theme)
                self.assertIn(f'data-theme="{theme}"', html)
                self.assertIn("@media print", html)
                self.assertIn("@media (max-width: 40rem)", html)
                self.assertNotRegex(html, r"(?:width|min-width):\s*[4-9]\d\dpx")

    def test_render_writes_private_artifacts_with_matching_manifest_hashes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "site"
            self.boardctl().render_project(SAMPLE, output, "public", "paper")

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {"brief.md", "index.html", "project.public.json"}, set(manifest["files"])
            )
            for name, digest in manifest["files"].items():
                self.assertEqual(digest, hashlib.sha256((output / name).read_bytes()).hexdigest())
            filtered_json = (output / "project.public.json").read_text(encoding="utf-8")
            brief = (output / "brief.md").read_text(encoding="utf-8")
            self.assertNotIn("src-kickoff", filtered_json)
            self.assertNotIn("aaaaaaaa", filtered_json)
            self.assertNotIn("src-kickoff", brief)

    def test_successful_render_removes_only_stale_audience_json_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "site"
            self.boardctl().render_project(SAMPLE, output, "private")
            self.boardctl().render_project(SAMPLE, output, "team")
            unrelated = output / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")

            self.boardctl().render_project(SAMPLE, output, "public")

            self.assertFalse((output / "project.private.json").exists())
            self.assertFalse((output / "project.team.json").exists())
            self.assertTrue((output / "project.public.json").is_file())
            self.assertEqual("keep", unrelated.read_text(encoding="utf-8"))
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {"brief.md", "index.html", "project.public.json"}, set(manifest["files"])
            )
            for name, digest in manifest["files"].items():
                self.assertEqual(digest, hashlib.sha256((output / name).read_bytes()).hexdigest())

    def test_failed_render_leaves_existing_audience_json_files_untouched(self):
        project = self.valid_project()
        project["sources"][0]["location"] = "javascript:alert(1)"
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output = directory / "site"
            invalid = directory / "invalid.json"
            self.boardctl().render_project(SAMPLE, output, "private")
            original = (output / "project.private.json").read_bytes()
            invalid.write_text(json.dumps(project), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "project is invalid"):
                self.boardctl().render_project(invalid, output, "public")

            self.assertEqual(original, (output / "project.private.json").read_bytes())
            self.assertFalse((output / "project.public.json").exists())

    def test_nonprivate_filter_closes_cross_collection_private_references(self):
        project = self.valid_project()
        project["project"]["private_extension"] = "PROJECT_SECRET"
        project["project"]["milestones"][0]["private_extension"] = "MILESTONE_SECRET"
        project["members"].append(
            {
                "id": "member-private",
                "display_name": "PRIVATE_MEMBER",
                "responsibility": "PRIVATE_ROLE",
                "visibility": "private",
                "extension": "MEMBER_SECRET",
            }
        )
        project["sources"][0]["location"] = "https://example.test/private-source"
        project["sources"][0]["sha256"] = "d" * 64
        project["tasks"][0].update(
            {
                "visibility": "public",
                "owner": {"private": "OWNER_SECRET"},
                "source_refs": ["src-kickoff"],
                "extension": "TASK_SECRET",
            }
        )
        project["decisions"][0].update(
            {
                "visibility": "team",
                "decided_by": "member-private",
                "extension": "DECISION_SECRET",
            }
        )
        project["updates"][0].update(
            {
                "visibility": "public",
                "source_refs": ["src-kickoff"],
                "extension": "UPDATE_SECRET",
            }
        )
        project["evidence"][0].update(
            {
                "visibility": "team",
                "task_id": "task-private",
                "extension": "EVIDENCE_SECRET",
            }
        )

        for audience in ("team", "public"):
            with self.subTest(audience=audience):
                view = self.boardctl().filter_for_audience(project, audience)
                payload = json.dumps(view, sort_keys=True)
                for secret in (
                    "PROJECT_SECRET", "MILESTONE_SECRET", "PRIVATE_MEMBER", "PRIVATE_ROLE",
                    "MEMBER_SECRET", "TASK_SECRET", "DECISION_SECRET", "UPDATE_SECRET",
                    "EVIDENCE_SECRET", "src-kickoff", "private-source", "d" * 64,
                    "member-private", "task-private", "OWNER_SECRET",
                ):
                    self.assertNotIn(secret, payload)
                self.assertIn("Restricted hidden source", payload)
                self.assertNotIn("1 restricted source", payload)

    def test_sensitive_scan_rejects_url_credentials_and_markdown_unsafe_links(self):
        findings = self.boardctl().scan_sensitive(
            {
                "note": "See [also](<javascript:alert(2)>) and https://user:pass@example.test/path",
            }
        )

        self.assertIn("note: unsafe URL protocol 'javascript'", findings)
        self.assertIn("note: URL must not contain username or password", findings)

    def test_render_inherits_theme_language_and_resets_dark_print_tokens(self):
        project = self.valid_project()
        project["meta"]["theme"] = "dark"
        project["meta"]["language"] = "zh-CN"

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "site"
            project_path = Path(temporary_directory) / "project.json"
            project_path.write_text(json.dumps(project), encoding="utf-8")
            self.boardctl().render_project(project_path, output, "public")

            page = (output / "index.html").read_text(encoding="utf-8")
            data = json.loads((output / "project.public.json").read_text(encoding="utf-8"))
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn('lang="zh-CN"', page)
            self.assertIn('data-theme="dark"', page)
            self.assertEqual("dark", data["meta"]["theme"])
            self.assertEqual("dark", manifest["theme"])
            self.assertIn("--page:#fff; --ink:#000; --card:#fff;", page)

            self.boardctl().render_project(project_path, output, "public", "paper")
            overridden = (output / "index.html").read_text(encoding="utf-8")
            overridden_data = json.loads((output / "project.public.json").read_text(encoding="utf-8"))
            overridden_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn('data-theme="paper"', overridden)
            self.assertEqual("paper", overridden_data["meta"]["theme"])
            self.assertEqual("paper", overridden_manifest["theme"])
        project["meta"].pop("language")
        self.assertIn('lang="und"', self.boardctl().render_html(project, "private"))

    def test_render_shows_milestones_and_unassigned_owner_fallback(self):
        project = self.valid_project()
        project["meta"]["language"] = "zh-CN"
        project["tasks"][0].pop("owner", None)

        page = self.boardctl().render_html(project, "private")
        self.assertIn("里程碑", page)
        self.assertIn('class="mstone"', page)
        self.assertIn("Review the first dashboard", page)
        self.assertIn("待指派", page)

        project["meta"]["language"] = "en"
        english = self.boardctl().render_html(project, "private")
        self.assertIn("Milestones", english)
        self.assertIn("Unassigned", english)

        public = self.boardctl().render_html(project, "public")
        self.assertNotIn('class="mstone"', public)

    def test_brief_escapes_markdown_metacharacters(self):
        project = self.valid_project()
        project["project"]["name"] = "[unsafe](javascript:alert(1)) # title"

        brief = self.boardctl()._brief_markdown(
            self.boardctl().filter_for_audience(project, "private"), "private"
        )

        self.assertIn(r"\[unsafe\]\(javascript:alert\(1\)\) \# title", brief)

    def test_render_transaction_restores_previous_complete_set_on_replace_failure(self):
        original_replace = os.replace
        for failure_call in (1, 3, 5, 8):
            with self.subTest(failure_call=failure_call), tempfile.TemporaryDirectory() as temporary_directory:
                output = Path(temporary_directory) / "site"
                self.boardctl().render_project(SAMPLE, output, "private")
                (output / "keep.txt").write_text("keep", encoding="utf-8")
                before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
                calls = 0

                def fail_once(source, target):
                    nonlocal calls
                    calls += 1
                    if calls == failure_call:
                        raise OSError("synthetic replace failure")
                    return original_replace(source, target)

                with mock.patch.object(self.boardctl().os, "replace", side_effect=fail_once):
                    with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                        self.boardctl().render_project(SAMPLE, output, "public")

                after = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
                self.assertEqual(before, after)

    def test_render_cli_outputs_requested_audience_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "rendered"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "render",
                    str(SAMPLE),
                    "--output",
                    str(output),
                    "--audience",
                    "team",
                    "--theme",
                    "clean",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((output / "index.html").is_file())
            self.assertTrue((output / "project.team.json").is_file())
            self.assertIn(f"rendered: {output}", result.stdout)

    def test_render_cli_inherits_meta_theme_unless_overridden(self):
        project = self.valid_project()
        project["meta"]["theme"] = "dark"
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            project_path = directory / "project.json"
            inherited_output = directory / "inherited"
            overridden_output = directory / "overridden"
            project_path.write_text(json.dumps(project), encoding="utf-8")

            inherited = subprocess.run(
                [sys.executable, str(SCRIPT), "render", str(project_path), "--output", str(inherited_output), "--audience", "private"],
                capture_output=True, text=True, check=False,
            )
            overridden = subprocess.run(
                [sys.executable, str(SCRIPT), "render", str(project_path), "--output", str(overridden_output), "--audience", "private", "--theme", "clean"],
                capture_output=True, text=True, check=False,
            )

            self.assertEqual(0, inherited.returncode, inherited.stderr)
            self.assertEqual(0, overridden.returncode, overridden.stderr)
            self.assertIn('data-theme="dark"', (inherited_output / "index.html").read_text(encoding="utf-8"))
            self.assertIn('data-theme="clean"', (overridden_output / "index.html").read_text(encoding="utf-8"))

    def test_workflow_visibility_typos_fail_validation_and_filter_closed(self):
        project = self.valid_project()
        project["workflow"][0]["visibility"] = "pubic"
        project["workflow"][0]["description"] = "WORKFLOW_CANARY"

        self.assertEqual(
            ["workflow[0].visibility: invalid value 'pubic'; expected one of private, public, team"],
            self.boardctl().validate_project(project),
        )
        public = self.boardctl().filter_for_audience(project, "public")
        self.assertNotIn("WORKFLOW_CANARY", json.dumps(public))

    def test_url_field_credentials_with_punctuation_are_rejected_and_redacted(self):
        project = self.valid_project()
        credential_url = "https://us(er):pass@example.test/path"
        project["sources"][0]["sensitivity"] = "public"
        project["sources"][0]["location"] = credential_url

        self.assertIn(
            "sources[0].location: URL must not contain username or password",
            self.boardctl().validate_project(project),
        )
        public = self.boardctl().filter_for_audience(project, "public")
        self.assertNotIn(credential_url, json.dumps(public))

    def test_concurrent_renders_finish_with_one_complete_audience_set(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            project_path = directory / "project.json"
            project_path.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            for round_number in range(5):
                output = directory / f"site-{round_number}"
                commands = [
                    [sys.executable, str(SCRIPT), "render", str(project_path), "--output", str(output), "--audience", audience]
                    for audience in ("public", "team")
                ]
                processes = [
                    subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for command in commands
                ]
                results = [process.communicate(timeout=10) for process in processes]

                self.assertEqual([0, 0], sorted(process.returncode for process in processes), results)
                audience_files = sorted(output.glob("project.*.json"))
                self.assertEqual(1, len(audience_files), results)
                current = audience_files[0]
                audience = current.stem.split(".")[-1]
                data = json.loads(current.read_text(encoding="utf-8"))
                manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(audience, data["meta"]["audience"])
                self.assertEqual({"brief.md", "index.html", current.name}, set(manifest["files"]))
                for name, digest in manifest["files"].items():
                    self.assertEqual(digest, hashlib.sha256((output / name).read_bytes()).hexdigest())
                self.assertTrue((directory / f".site-{round_number}.render.lock").is_file())

    def make_skill(self, directory: Path, *, missing_runtime: bool = False) -> Path:
        skill = directory / "portable-skill"
        (skill / "scripts").mkdir(parents=True)
        (skill / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (skill / "SKILL.md").write_text(
            "---\nname: portable-skill\ndescription: Use when testing.\n---\n"
            "Run [the tool](scripts/runtime.py).\n",
            encoding="utf-8",
        )
        if not missing_runtime:
            (skill / "scripts" / "runtime.py").write_text("print('synthetic')\n", encoding="utf-8")
        return skill

    def test_preview_cli_contains_all_themes_and_print_safe_responsive_css(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "preview.html"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "preview", "--output", str(output)],
                capture_output=True, text=True, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            page = output.read_text(encoding="utf-8")
            for theme in ("warm", "clean", "dark", "paper"):
                self.assertIn(f'data-theme="{theme}"', page)
            self.assertIn("@media (max-width: 40rem)", page)
            self.assertIn("@media print", page)
            self.assertNotIn("<script", page.lower())
            self.assertNotIn("http://", page.lower())
            self.assertNotIn("https://", page.lower())

    def test_package_layout_is_deterministic_and_excludes_client_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            (skill / "dist").mkdir()
            (skill / "dist" / "stale.txt").write_text("skip", encoding="utf-8")
            (skill / "scripts" / "__pycache__").mkdir()
            (skill / "scripts" / "__pycache__" / "runtime.pyc").write_bytes(b"skip")
            first = directory / "first.zip"
            second = directory / "second.zip"

            self.boardctl().package_skill(skill, "standard", first)
            self.boardctl().package_skill(skill, "standard", second)

            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), hashlib.sha256(second.read_bytes()).hexdigest())
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                self.assertTrue(names and all(name.startswith("portable-skill/") for name in names))
                self.assertIn("portable-skill/SKILL.md", names)
                self.assertNotIn("portable-skill/dist/stale.txt", names)
                self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))
                for info in archive.infolist():
                    self.assertEqual((1980, 1, 1, 0, 0, 0), info.date_time)
                core = archive.read("portable-skill/scripts/runtime.py").decode("utf-8")
                self.assertNotIn(str(directory), core)

    def test_cli_package_and_install_accept_relative_skill_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            archive = directory / "skill.zip"
            destination = directory / "skills"
            package = subprocess.run(
                [sys.executable, str(SCRIPT), "package", "--skill-dir", ".", "--target", "standard", "--output", str(archive)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            install = subprocess.run(
                [sys.executable, str(SCRIPT), "install", "--skill-dir", ".", "--target", "claude", "--destination", str(destination)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, package.returncode, package.stderr)
            self.assertTrue(archive.is_file())
            self.assertEqual(0, install.returncode, install.stderr)
            self.assertTrue((destination / ROOT.name / "SKILL.md").is_file())

    def test_package_rejects_unsafe_resolved_skill_root_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory).rename(directory / "AUX")

            with self.assertRaisesRegex(ValueError, r"unsafe relative path: AUX"):
                self.boardctl().package_skill(skill, "standard", directory / "skill.zip")

    def test_runtime_avoids_python_3_10_zip_strict_argument(self):
        self.assertNotIn("strict=True", SCRIPT.read_text(encoding="utf-8"))

    def test_workbuddy_and_openclaw_package_layouts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            workbuddy = directory / "workbuddy.zip"
            openclaw = directory / "openclaw.zip"

            self.boardctl().package_skill(skill, "workbuddy", workbuddy)
            self.boardctl().package_skill(skill, "openclaw", openclaw)

            with zipfile.ZipFile(workbuddy) as archive:
                self.assertIn("SKILL.md", archive.namelist())
                self.assertFalse(any(name.startswith("portable-skill/") for name in archive.namelist()))
            with zipfile.ZipFile(openclaw) as archive:
                self.assertIn("portable-skill/SKILL.md", archive.namelist())

    def test_package_excludes_temporary_artifacts_without_excluding_near_misses(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            for name in ("temp", "tmp", ".tmp", ".portable-skill.install.synthetic", "portable-skill.backup"):
                (skill / name).mkdir()
                (skill / name / "discard.txt").write_text("skip", encoding="utf-8")
            for name in ("scratch.tmp", "report.temp", "previous.bak"):
                (skill / name).write_text("skip", encoding="utf-8")
            for name in ("template", "attempt.md"):
                (skill / name).write_text("keep", encoding="utf-8")

            for target in ("standard", "workbuddy", "openclaw"):
                with self.subTest(target=target):
                    archive_path = directory / f"{target}.zip"
                    self.boardctl().package_skill(skill, target, archive_path)
                    with zipfile.ZipFile(archive_path) as archive:
                        names = archive.namelist()
                    self.assertTrue(any(name.endswith("template") for name in names))
                    self.assertTrue(any(name.endswith("attempt.md") for name in names))
                    self.assertFalse(any(
                        any(part in {"temp", "tmp", ".tmp", ".portable-skill.install.synthetic", "portable-skill.backup"} for part in Path(name).parts)
                        or name.endswith((".tmp", ".temp", ".bak"))
                        for name in names
                    ))

    def test_package_rejects_missing_runtime_and_symlinked_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            missing = self.make_skill(directory / "missing", missing_runtime=True)
            with self.assertRaisesRegex(ValueError, r"referenced file is missing.*scripts/runtime.py"):
                self.boardctl().package_skill(missing, "standard", directory / "missing.zip")

            linked = self.make_skill(directory / "linked")
            (linked / "scripts" / "runtime.py").unlink()
            (linked / "scripts" / "runtime.py").symlink_to(directory / "outside.py")
            with self.assertRaisesRegex(ValueError, r"symlink"):
                self.boardctl().package_skill(linked, "standard", directory / "linked.zip")

    def test_package_rejects_cross_platform_unsafe_source_entry_names(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            for name in (r"safe\..\..\escape.txt", r"C:\absolute.txt", r"\\server\share\escape.txt"):
                (skill / name).write_text("unsafe", encoding="utf-8")
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, r"unsafe.*path"):
                        self.boardctl().package_skill(skill, "standard", directory / "safe.zip")
                (skill / name).unlink()

    def test_package_rejects_windows_reserved_names_and_normalized_collisions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for index, name in enumerate(("AUX.txt", "CONIN$.txt", "CONOUT$.txt", "safe:stream.txt", "trailing. ", "bad?.md", "bad\x01.md", "bad\x7f.md")):
                skill = self.make_skill(directory / f"reserved-{index}")
                (skill / name).write_text("unsafe", encoding="utf-8")
                for target in ("standard", "workbuddy", "openclaw"):
                    with self.subTest(name=name, target=target):
                        with self.assertRaisesRegex(ValueError, r"unsafe.*path"):
                            self.boardctl().package_skill(skill, target, directory / f"{target}.zip")

            for target in ("standard", "workbuddy", "openclaw"):
                with self.subTest(collision=target):
                    with self.assertRaisesRegex(ValueError, r"Windows-normalized collision"):
                        self.boardctl()._package_names(
                            "portable-skill", (Path("Notes.md"), Path("notes.md")), target
                        )

    def test_package_output_is_atomic_and_cannot_overlap_or_be_symlinked(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            source_bytes = (skill / "scripts" / "runtime.py").read_bytes()
            with self.assertRaisesRegex(ValueError, r"inside skill source"):
                self.boardctl().package_skill(skill, "standard", skill / "package.zip")

            output = directory / "package.zip"
            output.write_bytes(b"old archive")
            with mock.patch.object(self.boardctl().os, "replace", side_effect=OSError("synthetic replace failure")):
                with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    self.boardctl().package_skill(skill, "standard", output)
            self.assertEqual(b"old archive", output.read_bytes())
            self.assertEqual(source_bytes, (skill / "scripts" / "runtime.py").read_bytes())

            link = directory / "linked.zip"
            link.symlink_to(output)
            with self.assertRaisesRegex(ValueError, r"symlink"):
                self.boardctl().package_skill(skill, "standard", link)

    def test_package_preserves_executable_file_permissions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            runtime = skill / "scripts" / "runtime.py"
            runtime.chmod(0o755)
            output = directory / "package.zip"

            self.boardctl().package_skill(skill, "standard", output)

            with zipfile.ZipFile(output) as archive:
                info = archive.getinfo("portable-skill/scripts/runtime.py")
            self.assertEqual(0o755, (info.external_attr >> 16) & 0o777)

    def test_reference_validation_requires_extensionless_links_and_ignores_commands(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            skill_md = skill / "SKILL.md"
            skill_md.write_text(
                "[config](config \"settings file\")\n`python scripts/runtime.py --check`\n"
                "```\nscripts/not-a-reference.py\n```\n"
                "[web](https://example.test) [mail](mailto:synthetic@example.test) [anchor](#section)\n"
                "[same config](./config 'settings') [tool](./scripts/runtime.py (Tool title))\n"
                "`--force` `private` `codex` `scripts/runtime.py`\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, r"referenced file is missing: config"):
                self.boardctl().package_skill(skill, "standard", directory / "package.zip")
            (skill / "config").write_text("ok", encoding="utf-8")
            self.boardctl().package_skill(skill, "standard", directory / "package.zip")

    def test_reference_scanner_ignores_code_fences_inline_code_and_escaped_links(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            (skill / "SKILL.md").write_text(
                "Use `first [inline](missing-inline.md)` and `second [inline](missing-second.md)`.\n"
                "```markdown\n[fenced](missing-fence.md)\n```\n"
                "~~~~ text\n[tilde fenced](missing-tilde.md)\n~~~~\n"
                "\\[escaped\\](missing-escaped.md)\n"
                "Plain [config](config).\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, r"referenced file is missing: config"):
                self.boardctl().package_skill(skill, "standard", directory / "package.zip")

            (skill / "config").write_text("ok", encoding="utf-8")
            self.boardctl().package_skill(skill, "standard", directory / "package.zip")

    def test_reference_scanner_does_not_suppress_links_after_unmatched_or_escaped_backticks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for index, source in enumerate((
                "Unmatched ` then [missing](missing-unmatched.md).\n",
                "Escaped \\` then [missing](missing-escaped-backtick.md).\n",
            )):
                skill = self.make_skill(directory / f"backtick-{index}")
                (skill / "SKILL.md").write_text(source, encoding="utf-8")
                with self.subTest(source=source):
                    with self.assertRaisesRegex(ValueError, r"referenced file is missing: missing-"):
                        self.boardctl().package_skill(skill, "standard", directory / f"backtick-{index}.zip")

    def test_reference_scanner_requires_clean_closing_fences(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for index, marker in enumerate(("```", "~~~")):
                skill = self.make_skill(directory / f"fence-{index}")
                (skill / "SKILL.md").write_text(
                    f"{marker} markdown\n[inside](missing-inside.md)\n{marker} trailing\n"
                    f"[still inside](missing-still-inside.md)\n{marker}   \n"
                    "Plain [missing](missing-after-fence.md).\n",
                    encoding="utf-8",
                )
                with self.subTest(marker=marker):
                    with self.assertRaisesRegex(ValueError, r"referenced file is missing: missing-after-fence.md"):
                        self.boardctl().package_skill(skill, "standard", directory / f"fence-{index}.zip")

    def test_reference_validation_ignores_inline_code_but_rejects_markdown_link_traversal(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            (skill / "SKILL.md").write_text("`../escape.py`\n", encoding="utf-8")
            self.boardctl().package_skill(skill, "standard", directory / "inline-code.zip")
            (skill / "SKILL.md").write_text("[escape](../escape.py)\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"unsafe relative path"):
                self.boardctl().package_skill(skill, "standard", directory / "package.zip")

    def test_install_uses_only_the_revalidated_whitelist_and_rejects_source_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            (skill / "portable-skill.backup").mkdir()
            (skill / "portable-skill.backup" / "extra.txt").write_text("exclude", encoding="utf-8")
            destination = directory / "skills"
            installed = self.boardctl().install_skill(skill, "claude", destination)
            installed_files = sorted(path.relative_to(installed).as_posix() for path in installed.rglob("*") if path.is_file())
            self.assertEqual(["SKILL.md", "VERSION", "scripts/runtime.py"], installed_files)

            changing = self.make_skill(directory / "changing")
            real_entries = self.boardctl()._skill_file_entries
            calls = 0

            def mutate_before_recheck(path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    (changing / "scripts" / "runtime.py").write_text("changed", encoding="utf-8")
                return real_entries(path)

            with mock.patch.object(self.boardctl(), "_skill_file_entries", side_effect=mutate_before_recheck):
                with self.assertRaisesRegex(OSError, r"source changed"):
                    self.boardctl().install_skill(changing, "claude", directory / "changing-skills")

    def test_concurrent_installers_are_serialized_with_conflicts_and_unique_backups(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            destination = directory / "skills"
            install = [sys.executable, str(SCRIPT), "install", "--skill-dir", str(skill), "--target", "claude", "--destination", str(destination)]
            first = subprocess.Popen(install, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            second = subprocess.Popen(install, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            first_result = first.communicate(timeout=10)
            second_result = second.communicate(timeout=10)
            self.assertEqual([0, 2], sorted((first.returncode, second.returncode)), (first_result, second_result))

            force = install + ["--force"]
            first_force = subprocess.Popen(force, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            second_force = subprocess.Popen(force, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            first_force_result = first_force.communicate(timeout=10)
            second_force_result = second_force.communicate(timeout=10)
            self.assertEqual([0, 0], sorted((first_force.returncode, second_force.returncode)), (first_force_result, second_force_result))
            self.assertTrue((destination / "portable-skill.backup").is_dir())
            self.assertTrue((destination / "portable-skill.backup-2").is_dir())

    def test_install_uses_environment_roots_refuses_conflicts_and_byte_matches(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            home = directory / "home"
            codex_home = directory / "codex-home"
            with mock.patch.dict(os.environ, {"HOME": str(home), "CODEX_HOME": str(codex_home)}, clear=False):
                claude_target = self.boardctl().install_skill(skill, "claude")
                codex_target = self.boardctl().install_skill(skill, "codex")
                workbuddy_target = self.boardctl().install_skill(skill, "workbuddy")

            self.assertEqual(home / ".claude" / "skills" / "portable-skill", claude_target)
            self.assertEqual(home / ".agents" / "skills" / "portable-skill", codex_target)
            self.assertEqual(home / ".workbuddy" / "skills" / "portable-skill", workbuddy_target)
            self.assertEqual((skill / "SKILL.md").read_bytes(), (workbuddy_target / "SKILL.md").read_bytes())
            self.assertEqual((skill / "SKILL.md").read_bytes(), (claude_target / "SKILL.md").read_bytes())
            with self.assertRaisesRegex(FileExistsError, r"already exists"):
                self.boardctl().install_skill(skill, "claude", home / ".claude" / "skills")

    def test_force_install_rolls_back_after_copy_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            skill = self.make_skill(directory)
            destination = directory / "skills"
            target = destination / "portable-skill"
            target.mkdir(parents=True)
            (target / "old.txt").write_text("old", encoding="utf-8")
            original_replace = os.replace
            calls = 0

            def fail_after_backup(source, target_path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic replace failure")
                return original_replace(source, target_path)

            with mock.patch.object(self.boardctl().os, "replace", side_effect=fail_after_backup):
                with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    self.boardctl().install_skill(skill, "claude", destination, force=True)

            self.assertEqual("old", (target / "old.txt").read_text(encoding="utf-8"))
            self.assertFalse(list(destination.glob("portable-skill.backup-*")))


if __name__ == "__main__":
    unittest.main()
