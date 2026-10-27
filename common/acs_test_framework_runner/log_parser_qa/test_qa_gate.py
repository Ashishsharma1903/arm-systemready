# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check CI isolation, change detection, result comparison and recorded evidence."""

import copy
from copy import deepcopy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
import yaml

from . import mutation_audit as audit
from . import parser_replay as replay
from . import qa_run as gate
from . import qa_run as run
from .qa_report import ContractEvidence, json_value, run_command
from .qa_run import REGISTRY, classify_changes, inspect_change



# Impact checks.

@pytest.fixture
def registry():
    return {"suites": [{"canonical": "BSA", "logs_to_json": "bsa/logs_to_json.py"}],
            "standalone": {"suite_execution": {"BSA": {"handler": "multi_log"}}}}


@pytest.mark.parametrize("path,kind", [
    ("common/log_parser/bsa/logs_to_json.py", "existing-suite"),
    ("common/log_parser/acs-results-schema.json", "shared-schema-qa"),
    ("common/log_parser/merge_jsons.py", "shared-schema-qa"),
    ("common/acs_test_framework_manifests/logs-to-json.yaml", "shared-schema-qa"),
    ("common/acs_test_framework_runner/log_parser_qa/qa_run.py", "shared-schema-qa"),
    (".github/workflows/log-parser-qa.yml", "shared-schema-qa"),
    ("README.md", "no-parser-change"),
])
def test_existing_shared_and_unrelated_changes(registry, path, kind):
    result = classify_changes([path], registry, registry, {"common/log_parser/bsa/logs_to_json.py"})
    assert result["kinds"] == [kind]
    assert result["new_suites"] == []


@pytest.mark.parametrize("entry", ["canonical", "execution"])
def test_new_registry_entry_in_existing_directory(registry, entry):
    updated = deepcopy(registry)
    if entry == "canonical":
        updated["suites"].append({"canonical": "NEW", "logs_to_json": "bsa/logs_to_json.py"})
    else:
        updated["standalone"]["suite_execution"]["NEW"] = {"handler": "multi_log"}
    result = classify_changes([REGISTRY], registry, updated, {REGISTRY})
    assert result["new_suites"] == ["NEW"]
    assert result["kinds"] == ["new-suite", "shared-schema-qa"]


def test_existing_metadata_is_not_a_new_suite(registry):
    updated = deepcopy(registry)
    updated["suites"][0]["requirements"] = {"DT": "R"}
    updated["standalone"]["suite_execution"]["BSA"]["handler"] = "single_log"
    result = classify_changes([REGISTRY], registry, updated, {REGISTRY})
    assert result["kinds"] == ["shared-schema-qa"]
    assert result["new_suites"] == []


def test_new_directory_is_detected_without_registry_entry(registry):
    result = classify_changes(["common/log_parser/new_suite/logs_to_json.py"],
                              registry, registry, {"common/log_parser/bsa/logs_to_json.py"})
    assert result["new_suites"] == ["directory: new_suite"]


def test_git_comparison_uses_committed_data_and_zero_base_fallback(tmp_path, registry):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "qa@example.invalid")
    git("config", "user.name", "Parser QA")
    path = tmp_path / REGISTRY
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(registry))
    git("add", ".")
    git("commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD")
    first_push = inspect_change(tmp_path, "0" * 40)
    assert first_push["base"] == ""
    assert first_push["new_suites"] == ["BSA"]
    registry["standalone"]["suite_execution"]["NEW"] = {}
    path.write_text(json.dumps(registry))
    git("add", ".")
    git("commit", "-qm", "add execution")
    path.write_text("uncommitted invalid JSON")
    result = inspect_change(tmp_path, "0" * 40)
    assert result["base"] == baseline
    assert result["new_suites"] == ["NEW"]
    with pytest.raises(subprocess.CalledProcessError):
        inspect_change(tmp_path, "--output=untrusted")
    assert not (tmp_path / "untrusted").exists()


# Qa run checks.

def test_command_failure_preserves_log_and_exit_code(tmp_path):
    log = tmp_path / "failure.log"
    result = run.run_stage([sys.executable, "-c", "print('controlled failure'); raise SystemExit(1)"], tmp_path, log, 10)
    assert result["exit_code"] == 1
    assert "controlled failure" in log.read_text()


def test_timeout_is_infrastructure_not_a_baseline_failure(tmp_path):
    result = run.run_stage([sys.executable, "-c", "import time; time.sleep(20)"], tmp_path, tmp_path / "timeout.log", 0.05)
    assert result["exit_code"] == 2
    assert "timed out" in result["infrastructure_error"]


def test_missing_command_is_infrastructure(tmp_path):
    result = run.run_stage([str(tmp_path / "no-such-program")], tmp_path, tmp_path / "missing.log", 1)
    assert result["exit_code"] == 2
    assert result["infrastructure_error"]


def test_child_cannot_inherit_a_different_parser_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PARSER_QA_PARSER_DIR", "/outside/old-parser")
    monkeypatch.setenv("PARSER_QA_TEST_MARKER", "preserved")
    log = tmp_path / "environment.log"
    command = [sys.executable, "-c", "import json, os; print(json.dumps(["
               "os.environ.get('LOG_PARSER_QA_PARSER_DIR'), os.environ.get('PARSER_QA_TEST_MARKER')]))"]
    result = run.run_stage(command, tmp_path, log, 10)
    assert result["exit_code"] == 0
    assert json.loads(log.read_text()) == [None, "preserved"]
    assert os.environ["LOG_PARSER_QA_PARSER_DIR"] == "/outside/old-parser"


def test_each_stage_runs_even_after_parser_contract_failures(tmp_path, monkeypatch):
    stages = []
    def execute(argv, cwd, log, timeout):
        stages.append(log.stem)
        log.write_text("existing parser defect\n")
        return {"exit_code": 1, "argv": [str(part) for part in argv]}
    monkeypatch.setattr(run, "run_stage", execute)
    manifest = run.execute_revision(tmp_path / "runtime", tmp_path / "reports", {}, 10, 2)
    assert stages == ["yaml", "contracts"]
    assert manifest["execution"] == {"yaml": 1, "contracts": 1}
    assert json.loads((tmp_path / "reports/run-manifest.json").read_text())["execution"] == manifest["execution"]
    assert not (tmp_path / "reports/browser.xml").exists()
    assert manifest["stages"]["yaml"]["argv"][0] == str(Path(sys.executable).absolute())


def test_browser_is_not_executed_twice_outside_the_yaml_manifest(tmp_path, monkeypatch):
    stages = []
    def execute(argv, cwd, log, timeout):
        stages.append(log.stem)
        log.write_text("ok\n")
        return {"exit_code": 0}
    monkeypatch.setattr(run, "run_stage", execute)
    manifest = run.execute_revision(tmp_path / "runtime", tmp_path / "reports", {}, 10, 1)
    assert stages == ["yaml", "contracts"]
    assert set(manifest["execution"]) == {"yaml", "contracts"}


def test_digest_includes_file_names_and_ignores_runtime_caches(tmp_path):
    folder = tmp_path / "harness"
    folder.mkdir()
    (folder / "test.py").write_text("test")
    before = run.digest_files(tmp_path, [Path("harness")])
    cache = folder / "__pycache__"
    cache.mkdir()
    (cache / "test.pyc").write_text("generated")
    assert run.digest_files(tmp_path, [Path("harness")]) == before
    (folder / "test.py").rename(folder / "different.py")
    assert run.digest_files(tmp_path, [Path("harness")]) != before


def fake_archive(member_name, kind=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(member_name)
        if kind:
            entry.type = kind
            entry.linkname = "../../outside"
            archive.addfile(entry)
        else:
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b"x"))
    return buffer.getvalue()


@pytest.mark.parametrize("name,kind", [("../outside", None), ("/absolute", None), ("link", tarfile.SYMTYPE)])
def test_base_archive_cannot_escape_the_isolated_runtime(tmp_path, monkeypatch, name, kind):
    def git(source, *args):
        return b"a" * 40 if args[0] == "rev-parse" else fake_archive(name, kind)
    monkeypatch.setattr(run, "_git", git)
    with pytest.raises(ValueError, match="unsafe base archive"):
        run.prepare_snapshots(tmp_path, "base", tmp_path / "runtime")


def test_base_and_candidate_use_same_harness_but_different_parser_sources(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    for relative in ("common/log_parser/parser.py", "common/log_parser/deleted.py",
                     "docs/log_parser_guide.md", "LICENSE.md"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("base")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=QA", "-c", "user.email=qa@example.invalid",
                    "-c", "core.hooksPath=/dev/null", "commit", "-qm", "isolated test fixture"], check=True)
    (source / "common/log_parser/parser.py").write_text("candidate")
    (source / "common/log_parser/deleted.py").unlink()
    (source / "common/log_parser/new.py").write_text("new candidate file")
    (source / ".gitignore").write_text("common/log_parser/ignored/\n")
    ignored = source / "common/log_parser/ignored/stale.py"
    ignored.parent.mkdir()
    ignored.write_text("stale private local file")
    for relative in (run.HARNESS / "test_probe.py", run.MANIFESTS / "probe.yaml", run.WORKFLOW):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("shared current QA")
    roots, _ = run.prepare_snapshots(source, "HEAD", tmp_path / "runtime")
    assert (roots["base"] / "common/log_parser/parser.py").read_text() == "base"
    assert (roots["candidate"] / "common/log_parser/parser.py").read_text() == "candidate"
    assert (roots["base"] / "common/log_parser/deleted.py").read_text() == "base"
    assert not (roots["candidate"] / "common/log_parser/deleted.py").exists()
    assert (roots["candidate"] / "common/log_parser/new.py").read_text() == "new candidate file"
    assert not (roots["candidate"] / "common/log_parser/ignored").exists()
    for root in roots.values():
        assert (root / run.HARNESS / "test_probe.py").read_text() == "shared current QA"
    assert (source / "common/log_parser/parser.py").read_text() == "candidate"


def test_orchestration_setup_failure_produces_blocked_report(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "runtime").mkdir()
    assert run.main(["--source", str(tmp_path), "--base", "HEAD", "--reports", str(reports)]) == 2
    assert json.loads((reports / "regression.json").read_text())["status"] == "BLOCKED"


# Regression gate checks.

def finding(actual=0, expected=1, status="FAIL"):
    return {"issue_id": "suite.counter", "suite": "BSA", "mode": "DT", "stage": "merge",
            "expected": expected, "actual": actual, "status": status,
            "reproduce": {"cwd": ".", "argv": ["python3", "-m", "pytest", "test_case"]}, "evidence": []}


def write_report(path, cases):
    suite = ET.Element("testsuite", tests=str(len(cases)),
                       failures=str(sum(item.get("outcome") == "failure" for item in cases)),
                       errors=str(sum(item.get("outcome") == "error" for item in cases)),
                       skipped=str(sum(item.get("outcome") == "skipped" for item in cases)))
    for item in cases:
        case = ET.SubElement(suite, "testcase", classname=item.get("classname", "tests.parser"), name=item.get("name", "test_contract"))
        if item.get("finding") is not None:
            properties = ET.SubElement(case, "properties")
            value = item["finding"]
            ET.SubElement(properties, "property", name="qa_finding", value=json.dumps(value))
        if item.get("outcome", "pass") != "pass":
            problem = ET.SubElement(case, item["outcome"], type=item.get("type", "AssertionError"), message=item.get("message", "counter mismatch"))
            problem.text = item.get("diagnostic", "assert actual == expected\nE assert 0 == 1")
    ET.ElementTree(suite).write(path, encoding="unicode")


@pytest.fixture
def runs(tmp_path):
    def generate(base_cases=None, candidate_cases=None):
        values = {}
        for side, cases in (("base", base_cases), ("candidate", candidate_cases)):
            root = tmp_path / side
            root.mkdir(exist_ok=True)
            reports = root / "reports"
            reports.mkdir(exist_ok=True)
            cases = cases if cases is not None else [{"name": "test_contract"}]
            write_report(reports / "unit-e2e.xml", cases)
            write_report(reports / "parser-yaml.xml", [{"name": "test_yaml_manifest"}])
            manifest = {"harness_digest": "same-harness", "fixtures_digest": "same-fixtures", "python_version": "3.12.10",
                        "test_command": ["python3", "-m", "pytest", str(root / "harness/tests")],
                        "repository_root": str(root / "harness"), "parser_revision": side + "-revision",
                        "parser_root": str(root / "runtime/log_parser"), "work_root": str(root),
                        "execution": {"yaml": 0, "contracts": int(any(item.get("outcome", "pass") != "pass" for item in cases))},
                        "expected_reports": ["unit-e2e.xml", "parser-yaml.xml"]}
            path = root / "run-manifest.json"
            path.write_text(json.dumps(manifest))
            values[side] = {"root": root, "reports": reports, "manifest": path, "data": manifest}
        return values
    return generate


def compare(runs):
    return gate.compare(runs["base"]["reports"], runs["candidate"]["reports"], runs["base"]["manifest"], runs["candidate"]["manifest"])


def update_manifest(run, field, value):
    run["data"][field] = value
    run["manifest"].write_text(json.dumps(run["data"]))


def test_identical_passes_and_existing_failures_are_informative(runs):
    failed = {"name": "test_failed", "outcome": "failure", "finding": finding()}
    result = compare(runs([{"name": "test_pass"}, failed], [{"name": "test_pass"}, failed]))
    assert result["status"] == "NO_NEW_REGRESSIONS"
    assert result["counts"] == {"base_cases": 3, "candidate_cases": 3, "passed": 2, "existing": 1,
                                "resolved": 0, "new": 0, "worsened": 0, "blocked": 0}
    assert result["existing_issues"][0]["candidate_findings"][0]["actual"] == 0


@pytest.mark.parametrize("mutation", ["actual", "expected", "issue_id", "suite", "mode", "stage", "diagnostic", "message", "type"])
def test_changed_existing_failure_is_not_hidden(runs, mutation):
    old = {"outcome": "failure", "finding": finding()}
    new = copy.deepcopy(old)
    if mutation in new["finding"]:
        new["finding"][mutation] = 2 if mutation in ("actual", "expected") else "changed"
    else:
        new[mutation] = "different failure evidence"
    result = compare(runs([old], [new]))
    assert result["status"] == "REGRESSION"
    assert result["counts"]["worsened"] == 1
    assert result["regressions"][0]["base_signature"] != result["regressions"][0]["candidate_signature"]


def test_new_failure_and_resolved_issue_are_separate(runs):
    passed = {"name": "test_new"}
    failed = {"name": "test_new", "outcome": "failure", "finding": finding()}
    resolved = {"name": "test_resolved", "outcome": "failure", "finding": finding()}
    result = compare(runs([passed, resolved], [failed, {"name": "test_resolved"}]))
    assert result["status"] == "REGRESSION"
    assert result["counts"]["new"] == 1 and result["counts"]["resolved"] == 1


def test_legacy_failure_diagnostic_changes_are_regressions(runs):
    result = compare(runs([{"outcome": "failure", "diagnostic": "assert failed == 2"}],
                          [{"outcome": "failure", "diagnostic": "assert failed == 3"}]))
    assert result["status"] == "REGRESSION"
    assert "failed == 2" in result["regressions"][0]["base_diagnostic"]
    assert "failed == 3" in result["regressions"][0]["candidate_diagnostic"]


def test_recorded_roots_and_generated_time_do_not_create_false_regressions(runs):
    values = runs()
    for side in ("base", "candidate"):
        run = values[side]
        actual = {"path": str(run["root"] / "output/results.json"),
                  "generated_at": "2026-10-22T10:00:01Z" if side == "base" else "2026-10-22T11:03:44Z",
                  "address": "0x12345678", "failed": 2}
        diagnostic = str(run["root"] / "runtime/log_parser/merge_jsons.py") + ":123: mismatch " + actual["generated_at"]
        write_report(run["reports"] / "unit-e2e.xml", [{"outcome": "failure", "finding": finding(actual), "diagnostic": diagnostic}])
        update_manifest(run, "execution", {"yaml": 0, "contracts": 1})
    result = compare(values)
    assert result["status"] == "NO_NEW_REGRESSIONS"


def test_addresses_numbers_and_nearby_paths_stay_significant():
    normalise = gate.Normalizer({"work_root": "/tmp/run"})
    assert normalise("/tmp/runner/result 0x12345678 tests=12") == "/tmp/runner/result 0x12345678 tests=12"
    assert normalise("/tmp/run/result") == "<WORK>/result"
    assert normalise("<Result at 0x12345678>") == "<Result at <OBJECT_ADDRESS>>"
    assert normalise({"generated_at": "2026-10-22Tinvalid"}) == {"generated_at": "2026-10-22Tinvalid"}


def test_report_date_metadata_is_normalised_only_in_its_field():
    normalise = gate.Normalizer({})
    assert normalise("{'Date': '2026-10-22 06:20:35', 'Failed': 1}") == "{'Date': '<TIMESTAMP>', 'Failed': 1}"
    assert normalise({"Date": "2026-10-22 06:20:35", "Test": "2026-10-22 06:20:35"}) == {
        "Date": "<TIMESTAMP>", "Test": "2026-10-22 06:20:35"}
    for value in ("2026-13-22 06:20:35", "2026-13-22T06:20:35Z", "not a date", "0 failed"):
        assert normalise({"Date": value}) == {"Date": value}


def test_only_known_staging_paths_below_recorded_work_are_normalised():
    normalise = gate.Normalizer({"work_root": "/recorded/run"})
    assert normalise("/recorded/run/work/test0/.output.tmp-m2c2fkqt/acs_jsons/fwts.json") == (
        "<WORK>/work/test0/.output.tmp-<STAGING>/acs_jsons/fwts.json")
    for value in ("/elsewhere/.output.tmp-m2c2fkqt/fwts.json", ".output.tmp-m2c2fkqt",
                  "/recorded/run/.different.tmp-m2c2fkqt/fwts.json",
                  "/recorded/run/.output.tmp-incorrect-length/fwts.json"):
        assert normalise(value) == value.replace("/recorded/run", "<WORK>")


def test_date_or_staging_noise_does_not_hide_a_result_change(runs):
    values = runs()
    for side, count, timestamp, suffix in (("base", 1, "06:20:35", "m2c2fkqt"),
                                           ("candidate", 2, "06:26:57", "8auksenp")):
        run = values[side]
        diagnostic = ("AssertionError: {'Date': '2026-10-22 " + timestamp + "', 'Failed': " + str(count) + "}\n"
                      + str(run["root"] / ("work/test0/.output.tmp-" + suffix + "/acs_jsons/fwts.json")))
        write_report(run["reports"] / "unit-e2e.xml", [{"outcome": "failure", "diagnostic": diagnostic}])
        update_manifest(run, "execution", {"yaml": 0, "contracts": 1})
    assert compare(values)["status"] == "REGRESSION"


def test_chromium_pid_time_and_devtools_banner_are_not_new_defects(runs):
    old = {"outcome": "failure", "diagnostic":
           "[1234:1235:1022/120001.112233:ERROR:dbus/bus.cc:408] connection failed\n"
           "DevTools listening on ws://127.0.0.1:35223/devtools/browser/5ea441ab-1b56-40ea-9087-93f607772fb3"}
    new = {"outcome": "failure", "diagnostic":
           "[2234:2235:1022/120101.223344:ERROR:dbus/bus.cc:408] connection failed\n"
           "DevTools listening on ws://127.0.0.1:41221/devtools/browser/1557ba59-cc5d-4007-9651-88d43324a651"}
    assert compare(runs([old], [new]))["status"] == "NO_NEW_REGRESSIONS"
    new["diagnostic"] = new["diagnostic"].replace("connection failed", "report result marker missing")
    assert compare(runs([old], [new]))["status"] == "REGRESSION"


def test_chromium_normalisation_does_not_remove_network_or_device_data():
    normalise = gate.Normalizer({})
    value = "connection http://127.0.0.1:12345 device 1234:1235 at 0x12345678"
    assert normalise(value) == value


@pytest.mark.parametrize("outcome,exit_code", [("failure", 0), ("pass", 1)])
def test_stage_exit_must_agree_with_recorded_checks(runs, outcome, exit_code):
    values = runs([{ "outcome": outcome}], [{"outcome": outcome}])
    for side in ("base", "candidate"):
        update_manifest(values[side], "execution", {"yaml": 0, "contracts": exit_code})
    assert compare(values)["status"] == "BLOCKED"


@pytest.mark.parametrize("outcome,status", [("error", "BLOCKED"), ("failure", "BLOCKED")])
def test_identical_production_setup_error_is_known_with_explicit_coverage_limit(runs, outcome, status):
    error = {"outcome": outcome, "type": "IndexError", "finding": finding("parser crashed", status=status),
             "diagnostic": "logs_to_json.py:88: IndexError: list index out of range"}
    result = compare(runs([error], [error]))
    assert result["status"] == "NO_NEW_REGRESSIONS"
    assert result["counts"]["existing"] == 1
    assert "not verified" in result["coverage_limits"][0]["reason"]


def test_changed_or_new_production_setup_error_is_blocked(runs):
    error = {"outcome": "error", "diagnostic": "logs_to_json.py:88: IndexError: parser crashed"}
    changed = {**error, "diagnostic": "logs_to_json.py:88: IndexError: new crash"}
    for old, new in (({"outcome": "pass"}, error), (error, changed)):
        result = compare(runs([old], [new]))
        assert result["status"] == "BLOCKED"
        assert any(item["issue_id"] == "case.unverified" for item in result["blockers"])


@pytest.mark.parametrize("error", [
    "PermissionError: [Errno 13] Permission denied: <PARSER>/suite_registry.json",
    *[f"OSError: [Errno {number}] Host resource unavailable: <PARSER>/suite_registry.json"
      for number in (12, 24, 28, 30)],
])
def test_reproduced_host_resource_errors_are_still_unverified(runs, error):
    case = {"outcome": "error", "type": error.split(":", 1)[0],
            "message": error, "diagnostic": error}
    result = compare(runs([case], [case]))
    assert result["status"] == "BLOCKED"
    assert result["counts"]["existing"] == 0
    assert any(item["issue_id"] == "case.infrastructure" for item in result["blockers"])


def test_other_oserrors_can_remain_a_reproduced_parser_defect(runs):
    error = "OSError: [Errno 22] Invalid argument: <PARSER>/suite_registry.json"
    case = {"outcome": "error", "type": "OSError", "message": error, "diagnostic": error}
    result = compare(runs([case], [case]))
    assert result["status"] == "NO_NEW_REGRESSIONS"
    assert result["counts"]["existing"] == 1
    assert result["coverage_limits"]


@pytest.mark.parametrize("error", [
    {"outcome": "skipped"},
    {"outcome": "error", "diagnostic": "E ModuleNotFoundError: No module named 'yaml'"},
    {"outcome": "failure", "diagnostic": "subprocess.TimeoutExpired: main_log_parser.sh timed out"},
    {"outcome": "error", "diagnostic": "RuntimeError: Required parser dependency unavailable"},
    {"outcome": "error", "diagnostic": "RuntimeError: unrelated harness fixture crashed"},
    {"outcome": "failure", "diagnostic": "report_browser.BrowserInfrastructureError: Chrome exited -6 before validating the page"},
    {"outcome": "error", "diagnostic":
     "<REPOSITORY>/common/acs_test_framework_runner/log_parser_qa/tests/test_end_to_end.py:23: "
     "RuntimeError: unrelated harness fixture crashed"},
])
def test_skips_infrastructure_and_unclassified_setup_errors_never_become_known_success(runs, error):
    assert compare(runs([error], [error]))["status"] == "BLOCKED"


@pytest.mark.parametrize("mutation", ["missing-report", "missing-case", "new-case", "duplicate-case", "zero-cases", "malformed-xml", "invalid-counts"])
def test_incomplete_or_different_inventory_is_blocked(runs, mutation):
    values = runs()
    path = values["candidate"]["reports"] / "unit-e2e.xml"
    if mutation == "missing-report":
        path.unlink()
    elif mutation == "malformed-xml":
        path.write_text("<not-closed>")
    elif mutation == "invalid-counts":
        path.write_text('<testsuite tests="99"><testcase name="test_contract"/></testsuite>')
    else:
        cases = [] if mutation in ("missing-case", "zero-cases") else [{"name": "test_contract"}, {"name": "test_contract" if mutation == "duplicate-case" else "test_added"}]
        write_report(path, cases)
    assert compare(values)["status"] == "BLOCKED"


@pytest.mark.parametrize("field,value", [("harness_digest", "changed"), ("fixtures_digest", "changed"),
                                         ("python_version", "3.10.1"), ("test_command", ["different-test"]),
                                         ("execution", {"yaml": 0, "contracts": 2}),
                                         ("execution", {"yaml": 0}),
                                         ("execution", {"yaml": 0, "contracts": 0, "browser": 2}),
                                         ("execution", None),
                                         ("execution", []),
                                         ("expected_reports", ["absent.xml"])])
def test_different_harness_or_unfinished_execution_is_blocked(runs, field, value):
    values = runs()
    update_manifest(values["candidate"], field, value)
    assert compare(values)["status"] == "BLOCKED"


@pytest.mark.parametrize("inventory", [None, [], ["unit-e2e.xml"], ["arbitrary.xml"],
                                       ["unit-e2e.xml", "unit-e2e.xml"],
                                       ["unit-e2e.xml", "../outside.xml"],
                                       ["unit-e2e.xml", "parser-yaml.xml", "extra.xml"]])
def test_both_sides_need_complete_explicit_report_inventory(runs, inventory):
    values = runs()
    for side in ("base", "candidate"):
        update_manifest(values[side], "expected_reports", inventory)
    assert compare(values)["status"] == "BLOCKED"


def test_both_manifests_omitting_report_inventory_is_blocked(runs):
    values = runs()
    for run in values.values():
        del run["data"]["expected_reports"]
        run["manifest"].write_text(json.dumps(run["data"]))
    assert compare(values)["status"] == "BLOCKED"


def test_invalid_execution_cannot_crash_an_existing_production_error(runs):
    error = {"outcome": "error", "diagnostic": "logs_to_json.py:88: IndexError: parser crashed"}
    values = runs([error], [error])
    update_manifest(values["candidate"], "execution", None)
    assert compare(values)["status"] == "BLOCKED"


def test_missing_or_invalid_structured_findings_are_not_silently_compared(runs):
    value = finding()
    del value["actual"]
    invalid = {"outcome": "failure", "finding": value}
    assert compare(runs([invalid], [invalid]))["status"] == "BLOCKED"


def test_failure_findings_on_passing_cases_are_blocked(runs):
    contradictory = {"outcome": "pass", "finding": finding()}
    assert compare(runs([contradictory], [contradictory]))["status"] == "BLOCKED"


@pytest.mark.parametrize("state,expected_exit", [("pass", 0), ("new", 1), ("blocked", 2)])
def test_cli_writes_complete_report_and_exit_status(runs, tmp_path, state, expected_exit):
    candidate = [{"outcome": "failure", "finding": finding()}] if state == "new" else None
    values = runs(candidate_cases=candidate)
    if state == "blocked":
        values["candidate"]["manifest"].write_text("invalid json")
    output = tmp_path / "comparison.json"
    code = gate.compare_main(["--base-reports", str(values["base"]["reports"]),
                      "--candidate-reports", str(values["candidate"]["reports"]),
                      "--base-manifest", str(values["base"]["manifest"]),
                      "--candidate-manifest", str(values["candidate"]["manifest"]),
                      "--output", str(output)])
    assert code == expected_exit
    result = json.loads(output.read_text())
    assert result["schema_version"] == 1
    assert result["status"] == {0: "NO_NEW_REGRESSIONS", 1: "REGRESSION", 2: "BLOCKED"}[expected_exit]


# Qa evidence checks.

def test_explicit_failure_is_structured_and_blocking():
    node = SimpleNamespace(nodeid="test_probe.py::test_case[SR]", user_properties=[])
    evidence = ContractEvidence(node)
    context = dict(issue_id="missing-control", suite="SCT", mode="SR", stage="log-to-json")
    evidence.check(1, 1, **context)
    assert node.user_properties == []
    with pytest.raises(AssertionError, match="BLOCKED missing-control"):
        evidence.check(0, 1, status="BLOCKED", evidence=[Path("input.log")], **context)
    finding = json.loads(node.user_properties[0][1])
    assert finding["expected"] == 1 and finding["actual"] == 0
    assert finding["status"] == "BLOCKED"
    assert finding["reproduce"]["argv"][-1] == node.nodeid
    assert finding["evidence"] == ["input.log"]


def test_pytest_hooks_preserve_failures_errors_and_exact_scenarios(tmp_path):
    root = Path(__file__).resolve().parent
    qa_dir = tmp_path / "common/acs_test_framework_runner/log_parser_qa"
    qa_dir.mkdir(parents=True)
    # Recreate the real directory layout, including the shared JSON helper.
    for path in root.parent.glob("*.py"):
        shutil.copy2(path, qa_dir.parent / path.name)
    for name in ("conftest.py", "qa_report.py"):
        shutil.copy2(root / name, qa_dir / name)
    (qa_dir / "test_probe.py").write_text('''import pytest
@pytest.mark.parametrize("suite,mode", [("BSA", "DT"), ("SBSA", "SR")])
def test_counts(suite, mode):
    actual = 0
    assert actual == 1
@pytest.fixture
def unavailable():
    raise RuntimeError("Required parser dependency unavailable")
def test_setup(unavailable):
    assert False, "must not execute"
def test_pass(qa):
    assert 1 == 1
@pytest.mark.qa_context(suite="BSA", mode="DT", stage="merge")
def test_caught_comparison_is_not_evidence_for_later_failure():
    with pytest.raises(AssertionError):
        assert 0 == 999
    assert False, "unrelated final failure"
@pytest.fixture(params=[{"suites": ["SCT", "BBSR-SCT"], "mode": "SR"}])
def generated_run(request):
    raise RuntimeError("Native control could not generate reports")
@pytest.mark.qa_context(stage="native-route")
def test_generated_setup(generated_run):
    assert False, "must not execute"
@pytest.mark.qa_context(mode="SR", stage="end-to-end")
@pytest.mark.parametrize("outcome", [{"suite": "FWTS", "status": "failed"}])
def test_selected(outcome):
    assert "Compliant" == "Not Compliant"
''')
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"--junitxml={tmp_path / 'result.xml'}"],
        cwd=qa_dir, text=True, capture_output=True, check=False, timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    cases = list(ET.parse(tmp_path / "result.xml").iter("testcase"))
    assert len(cases) == 7
    for case in cases:
        properties = {entry.get("name"): entry.get("value") for entry in case.findall("properties/property")}
        if case.get("name") == "test_pass":
            assert "qa_finding" not in properties
            continue
        finding = json.loads(properties["qa_finding"])
        assert case.get("name") in finding["reproduce"]["argv"][-1]
        if case.get("name").startswith("test_counts"):
            assert case.find("failure") is not None
            assert finding["actual"] == 0 and finding["expected"] == 1
            assert (finding["suite"], finding["mode"]) in (("BSA", "DT"), ("SBSA", "SR"))
        elif case.get("name") == "test_setup":
            assert case.find("error") is not None
            assert finding["status"] == "BLOCKED"
            assert finding["stage"] == "qa-contract-setup"
            assert "dependency unavailable" in finding["actual"]
        elif case.get("name").startswith("test_generated_setup"):
            assert case.find("error") is not None
            assert (finding["suite"], finding["mode"], finding["status"]) == ("SCT,BBSR-SCT", "SR", "BLOCKED")
            assert finding["stage"] == "native-route-setup"
        elif case.get("name").startswith("test_selected"):
            assert case.find("failure") is not None
            assert (finding["suite"], finding["mode"], finding["stage"]) == ("FWTS", "SR", "end-to-end")
        else:
            assert finding["status"] == "FAIL"
            assert (finding["suite"], finding["mode"], finding["stage"]) == ("BSA", "DT", "merge")
            assert "unrelated final failure" in finding["actual"]
            assert "unrelated final failure" in finding["expected"]


def test_evidence_json_handles_invalid_data_without_hiding_it():
    assert json_value(float("nan")) == "NaN"
    assert json_value({("invalid", "json-key"): 1}) == "{('invalid', 'json-key'): 1}"


@pytest.mark.parametrize("code", [0, 1])
def test_command_evidence_keeps_success_and_failure_output(tmp_path, code):
    argv = [sys.executable, "-c", f"print('fixture evidence'); raise SystemExit({code})"]
    result = run_command(argv, tmp_path, tmp_path, name="probe")
    assert result.returncode == code
    assert (tmp_path / "probe.log").read_text() == "fixture evidence\n"
    assert json.loads((tmp_path / "probe.command.json").read_text()) == {"argv": argv, "cwd": str(tmp_path)}


def test_command_setup_failure_keeps_reproduction(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_command([tmp_path / "missing-python"], tmp_path, tmp_path)
    assert "missing-python" in (tmp_path / "command.log").read_text()
    assert (tmp_path / "command.command.json").is_file()


def test_command_timeout_keeps_partial_output(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, "-u", "-c", "import time; print('started'); time.sleep(30)"],
                    tmp_path, tmp_path, timeout=1)
    assert "started" in (tmp_path / "command.log").read_text()
    assert (tmp_path / "command.command.json").is_file()


def test_end_to_end_command_failure_keeps_full_diagnostics(monkeypatch, tmp_path):
    helper = importlib.import_module(".tests.test_end_to_end", __package__)
    command = ["python3", tmp_path / "parser with spaces.py", "--check"]
    observed = SimpleNamespace(returncode=6, stdout="original error\n")
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: observed)
    with pytest.raises(AssertionError) as first:
        helper.run(command, tmp_path)
    assert str(command[1]) in str(first.value)
    assert "Working directory:" in str(first.value)
    assert "original error" in str(first.value)
    assert "CompletedProcess" not in str(first.value)
    observed.stdout = "different error\n"
    with pytest.raises(AssertionError) as second:
        helper.run(command, tmp_path)
    assert str(first.value) != str(second.value)
    assert helper.run(command, tmp_path, expected=6) == "different error\n"


# Mutation audit checks.

@pytest.mark.parametrize(
    "base,mutant,text,status",
    [
        ({"exit_code": 0}, {"exit_code": 1}, "Expected mutation signal", "DETECTED"),
        ({"exit_code": 1}, {"exit_code": 1}, "Expected mutation signal", "BLOCKED"),
        ({"exit_code": 0}, {"exit_code": 0}, "", "MISSED"),
        ({"exit_code": 0}, {"exit_code": 1}, "Unrelated infrastructure error", "BLOCKED"),
        ({"exit_code": 0}, {"exit_code": None, "timed_out": True}, "", "BLOCKED"),
        ({"exit_code": 0}, {"exit_code": 5, "test_count": 0}, "Expected mutation signal", "BLOCKED"),
        ({"exit_code": 0, "valid_evidence": False}, {"exit_code": 1}, "Expected mutation signal", "BLOCKED"),
        ({"exit_code": 0, "failures": [{"message": "hidden failure"}]}, {"exit_code": 1},
         "Expected mutation signal", "BLOCKED"),
    ],
)
def test_detection_requires_passing_control_and_specific_failure(base, mutant, text, status):
    assert audit.classify({"valid_evidence": True, **base},
                          {"valid_evidence": True, **mutant},
                          ("Expected mutation signal",), text)[0] == status


@pytest.mark.parametrize("copies", [0, 2])
def test_ambiguous_source_anchor_is_rejected(tmp_path, copies):
    mutation = audit.MUTATIONS[0]
    path = tmp_path / audit.PARSER / mutation.path
    path.parent.mkdir(parents=True)
    original = mutation.before * copies
    path.write_text(original)
    with pytest.raises(ValueError, match="expected one source anchor"):
        audit.apply_mutation(tmp_path, mutation)
    assert path.read_text() == original


def test_mutation_changes_only_exact_file(tmp_path):
    mutation = audit.MUTATIONS[0]
    path = tmp_path / audit.PARSER / mutation.path
    path.parent.mkdir(parents=True)
    path.write_text("# original\n" + mutation.before)
    neighbour = path.with_name("neighbour.py")
    neighbour.write_text("unchanged")
    patch = audit.apply_mutation(tmp_path, mutation)
    assert mutation.after in path.read_text()
    assert neighbour.read_text() == "unchanged"
    assert mutation.path in patch


def test_mutation_definitions_are_safe_and_unique():
    """The optional audit owns stale-anchor checks, not the normal PR gate."""
    assert len({case.name for case in audit.MUTATIONS}) == len(audit.MUTATIONS)
    for mutation in audit.MUTATIONS:
        path = Path(mutation.path)
        assert not path.is_absolute() and ".." not in path.parts
        assert mutation.before and mutation.before != mutation.after
        assert mutation.signals


def test_audit_preserves_source_and_refuses_existing_evidence(tmp_path, monkeypatch):
    source = tmp_path / "source"
    mutation = audit.MUTATIONS[0]
    original = mutation.before
    path = source / audit.PARSER / mutation.path
    path.parent.mkdir(parents=True)
    path.write_text(original)
    output = tmp_path / "evidence"

    def copy_minimal(src, dst):
        target = dst / audit.PARSER / mutation.path
        target.parent.mkdir(parents=True)
        target.write_text((src / audit.PARSER / mutation.path).read_text())

    def fake_check(checkout, case, evidence):
        evidence.mkdir(parents=True)
        changed = case.after in (checkout / audit.PARSER / case.path).read_text()
        (evidence / "check.log").write_text("SyntaxError" if changed else "PASS")
        return {"valid_evidence": True, "exit_code": int(changed)}

    monkeypatch.setattr(audit, "copy_checkout", copy_minimal)
    monkeypatch.setattr(audit, "run_check", fake_check)
    report = audit.audit(source, output, [mutation])
    assert report["counts"] == {"DETECTED": 1}
    assert report["source_unchanged"]
    assert path.read_text() == original
    with pytest.raises(ValueError, match="existing evidence"):
        audit.audit(source, output, [mutation])


@pytest.mark.parametrize("relative", [audit.PARSER, audit.RUNNER, Path("common/acs_test_framework_manifests")])
def test_audit_evidence_cannot_be_written_into_source_trees(tmp_path, relative):
    with pytest.raises(ValueError, match="outside parser"):
        audit.audit(tmp_path, tmp_path / relative / "evidence", [])


# Parser replay checks.

def observation(snapshot=None, errors=()):
    return {"input_sha256": {"input.log": "fixed"}, "snapshot": snapshot or {"test": "PASSED"},
            "correctness_errors": list(errors), "exit_code": 0}


@pytest.mark.parametrize("mutation", [
    {"test": "FAILED"}, {"test": "PASSED", "invented": "PASSED"}, {},
    {"test": None}, {"test": "NOT RUN"}, {"test": "PASSED (WITH WAIVER)"},
])
def test_result_changes_need_review_even_when_both_parsers_exit_zero(mutation):
    base = observation()
    candidate = observation()
    candidate["snapshot"] = mutation
    assert replay.classify(base, candidate)[0] == "RESULT_CHANGE_REQUIRES_REVIEW"


def test_matching_broken_baseline_is_not_a_pass():
    broken = observation(errors=["incorrect compliance"])
    assert replay.classify(broken, copy.deepcopy(broken))[0] == "KNOWN_BASELINE_DEFECT"


@pytest.mark.parametrize("field", ["snapshot", "input_sha256", "exit_code", "correctness_errors"])
def test_missing_evidence_is_blocked_even_when_both_revisions_are_equally_incomplete(field):
    broken = observation(errors=["old failure"])
    broken.pop(field)
    assert replay.classify(broken, copy.deepcopy(broken))[0] == "BLOCKED"


@pytest.mark.parametrize("exit_code", [1, 2, 5, 6, None, True])
def test_unsuccessful_replay_never_becomes_a_known_baseline(exit_code):
    broken = observation(errors=["old failure"])
    broken["exit_code"] = exit_code
    assert replay.classify(broken, copy.deepcopy(broken))[0] == "BLOCKED"


def test_fewer_baseline_findings_are_informative_when_results_are_unchanged():
    assert replay.classify(observation(errors=["schema", "HTML"]), observation(errors=["schema"]))[0] == "BASELINE_DEFECT_IMPROVED"
    assert replay.classify(observation(errors=["schema"]), observation())[0] == "BASELINE_DEFECT_IMPROVED"


def test_repeated_baseline_finding_increases_are_new_regressions():
    assert replay.classify(observation(errors=["bad count"]), observation(errors=["bad count", "bad count"]))[0] == "REGRESSION"


def test_new_correctness_failure_is_regression_even_when_outputs_match():
    assert replay.classify(observation(), observation(errors=["HTML badge wrong"]))[0] == "REGRESSION"


def test_existing_schema_problem_cannot_hide_a_new_html_problem():
    base = observation(errors=["schema mismatch"])
    candidate = observation(errors=["schema mismatch", "HTML overall result falsely green"])
    assert replay.classify(base, candidate)[0] == "REGRESSION"


def test_fix_requires_review_instead_of_automatic_golden_update():
    assert replay.classify(observation(errors=["wrong result"]), observation({"test": "FAILED"}))[0] == "RESULT_CHANGE_REQUIRES_REVIEW"


@pytest.mark.parametrize("which", ["base", "candidate"])
def test_missing_revision_is_blocked(which):
    values = {"base": observation(), "candidate": observation()}
    values[which]["blocked"] = "execution timed out"
    assert replay.classify(**values)[0] == "BLOCKED"


def test_different_inputs_are_never_compared_as_a_parser_regression():
    candidate = observation()
    candidate["input_sha256"]["input.log"] = "different"
    assert replay.classify(observation(), candidate)[0] == "BLOCKED"


@pytest.mark.parametrize("valid", [True, False])
def test_only_exact_explicit_reviewed_change_is_allowed(valid):
    base, candidate = observation(), observation({"test": "FAILED"})
    reviewed = {"base_sha256": replay.snapshot_digest(base["snapshot"]),
                "candidate_sha256": replay.snapshot_digest(candidate["snapshot"]), "reason": "Approved parser correction"}
    if not valid:
        reviewed["candidate_sha256"] = "different"
    assert replay.classify(base, candidate, reviewed)[0] == ("PASS_REVIEWED_CHANGE" if valid else "RESULT_CHANGE_REQUIRES_REVIEW")


def test_review_never_waives_correctness_failures():
    base, candidate = observation(), observation({"test": "FAILED"}, ["wrong HTML status"])
    reviewed = {"base_sha256": replay.snapshot_digest(base["snapshot"]),
                "candidate_sha256": replay.snapshot_digest(candidate["snapshot"]), "reason": "cannot waive defects"}
    assert replay.classify(base, candidate, reviewed)[0] == "REGRESSION"


def test_equal_totals_cannot_hide_swapped_tests():
    before = {"totals": {"pass": 1, "fail": 1}, "tests": [{"id": "a", "status": "PASS"}, {"id": "b", "status": "FAIL"}]}
    after = copy.deepcopy(before)
    after["tests"][0]["status"], after["tests"][1]["status"] = "FAIL", "PASS"
    changes = replay.differences(before, after)
    assert {change["path"] for change in changes} == {"/tests/0/status", "/tests/1/status"}


@pytest.mark.parametrize("actual,expected", [(True, 1), (False, 0), (1.0, 1),
    ({"PASSED": True}, {"PASSED": 1}), ([True], [1])])
def test_fixture_comparisons_do_not_confuse_boolean_and_numeric_counters(actual, expected):
    assert not replay._equal(actual, expected)


def test_boolean_counter_is_a_fixture_failure(tmp_path):
    directory = tmp_path / "acs_jsons"
    directory.mkdir()
    (directory / "bsa.json").write_text('{"test_results": [], "suite_summary": {"total_passed": true}}')
    case = {"raw": {"bsa.json": {"records": [], "assertions": [
        {"path": ["suite_summary", "total_passed"], "value": 1}]}}}
    assert any("expected 1, got True" in error for error in replay.independent_expectations(tmp_path, case))


def test_snapshot_ignores_only_documented_generation_timestamps(tmp_path):
    directory = tmp_path / "acs_jsons"
    directory.mkdir()
    value = {"Suite_Name: acs_info": {"System Info": {"Summary Generated On": "clock"},
             "ACS Results Summary": {"Date": "clock", "Overall Compliance Result": "Not Compliant"}},
             "Suite_Name: Test": {"Date": "TEST-ID-MUST-STAY", "reason": "evidence"}}
    (directory / "merged_results.json").write_text(json.dumps(value))
    snapshot = replay.semantic_snapshot(tmp_path, tmp_path / "input")
    assert snapshot["merged_results.json"]["Suite_Name: Test"]["Date"] == "TEST-ID-MUST-STAY"
    assert "Date" not in snapshot["merged_results.json"]["Suite_Name: acs_info"]["ACS Results Summary"]
    assert snapshot["merged_results.json"]["Suite_Name: acs_info"]["ACS Results Summary"]["Overall Compliance Result"] == "Not Compliant"


def test_snapshot_ignores_only_suite_list_order_not_missing_or_duplicate_failures(tmp_path):
    directory = tmp_path / "acs_jsons"
    directory.mkdir()
    path = directory / "merged_results.json"
    def snapshot(explanation):
        path.write_text(json.dumps({"Suite_Name: acs_info": {"ACS Results Summary": {"Overall Compliance Result": explanation}}}))
        return replay.semantic_snapshot(tmp_path, tmp_path / "input")
    before = snapshot("Not Compliant : Mandatory - (failed: BSA, FWTS) : Recommended - (failed: POST_SCRIPT)")
    assert before == snapshot("Not Compliant : Mandatory - (failed: FWTS, BSA) : Recommended - (failed: POST_SCRIPT)")
    assert before != snapshot("Not Compliant : Mandatory - (failed: BSA, BSA) : Recommended - (failed: POST_SCRIPT)")


@pytest.mark.parametrize("unsafe", ["../secret", "/secret", "a/../../secret"])
def test_fixture_paths_cannot_escape(tmp_path, unsafe):
    with pytest.raises(ValueError):
        replay._relative(tmp_path, unsafe)


def minimal_case():
    return {"name": "approved", "mode": "DT", "suites": ["BSA"], "files": {},
            "raw": {"bsa.json": {"destination": "Suite_Name: BSA", "records": [], "assertions": []}}}


@pytest.mark.parametrize("kind,expected_status,expected_exit,baseline_defects", [
    ("baseline", "NO_NEW_REGRESSIONS", 0, 1),
    ("clean", "NO_NEW_REGRESSIONS", 0, 0),
    ("new_diagnostic", "FAIL", 1, 0),
    ("new_result", "FAIL", 1, 0),
    ("blocked", "BLOCKED", 2, 0),
])
def test_cli_baseline_policy_status_exit_and_counts(tmp_path, monkeypatch, kind, expected_status, expected_exit, baseline_defects):
    fixtures = tmp_path / "fixtures.yaml"
    fixtures.write_text(yaml.safe_dump({"scenarios": [minimal_case()]}))
    base = observation(errors=[] if kind == "clean" else ["known schema issue"])
    candidate = copy.deepcopy(base)
    if kind == "new_diagnostic":
        candidate["correctness_errors"].append("new HTML result corruption")
    elif kind == "new_result":
        candidate["snapshot"] = {"test": "FAILED"}
    elif kind == "blocked":
        base["blocked"] = candidate["blocked"] = "dependency unavailable"
    def fake_revision(parser_dir, *args):
        return base if parser_dir.name == "base" else candidate
    monkeypatch.setattr(replay, "replay_revision", fake_revision)
    output = tmp_path / "report"
    result = replay.main(["--base-parser", str(tmp_path / "base"), "--candidate-parser", str(tmp_path / "candidate"),
                          "--output", str(output), "--fixtures", str(fixtures)])
    report = json.loads((output / "result.json").read_text())
    assert result == expected_exit
    assert report["status"] == expected_status
    assert report["baseline_defects"] == baseline_defects
    assert report["status_counts"] == {report["cases"][0]["status"]: 1}
    if kind == "baseline":
        assert report["baseline_findings"] == 1
        assert report["cases"][0]["candidate_errors"] == ["known schema issue"]
        assert "not that the parser has no bugs" in report["note"]


def test_relative_output_keeps_input_paths_valid_in_child_working_directory(tmp_path, monkeypatch):
    """The workflow uses a relative output; parser children run inside each input."""
    parser_dir = tmp_path / "parser"
    parser_dir.mkdir()
    (parser_dir / "main_log_parser.sh").write_text("""#!/bin/bash
set -eu
output=''
while [ "$#" -gt 0 ]; do
    case "$1" in
        --input-log|--acs-config|--system-config)
            case "$2" in /*) ;; *) echo 'input path is not absolute'; exit 7 ;; esac
            test -e "$2"
            ;;
        --output)
            output="$2"
            case "$output" in /*) ;; *) echo 'output path is not absolute'; exit 7 ;; esac
            ;;
    esac
    shift
done
mkdir -p "$output/acs_jsons"
printf '%s' '{}' > "$output/acs_jsons/merged_results.json"
pwd > "$output/child-cwd.txt"
""")
    (tmp_path / "fixtures.yaml").write_text(yaml.safe_dump({"scenarios": [minimal_case()]}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(replay, "independent_expectations", lambda *_: [])
    monkeypatch.setattr(replay.artifacts, "_run_artifact_validation", lambda *_: 0)
    assert replay.main(["--base-parser", "parser", "--candidate-parser", "parser",
                        "--fixtures", "fixtures.yaml", "--output", "common/reports/replay"]) == 0
    for side in ("base", "candidate"):
        directory = tmp_path / "common/reports/replay/approved" / side
        command = json.loads((directory / "parser.command.json").read_text())
        assert command["cwd"] == str(directory / "input")
        assert (directory / "output/child-cwd.txt").read_text().strip() == str(directory / "input")
    report = json.loads((tmp_path / "common/reports/replay/result.json").read_text())
    assert report["status"] == "NO_NEW_REGRESSIONS"


@pytest.mark.parametrize("state", ["valid", "wrong_hash", "unapproved", "symlink"])
def test_external_corpus_requires_public_approval_and_exact_hashes(tmp_path, state):
    fixtures = tmp_path / "fixtures.yaml"
    fixtures.write_text(yaml.safe_dump({"scenarios": []}))
    source = tmp_path / "log.txt"
    source.write_text("sanitized log\n")
    name = "log.txt"
    if state == "symlink":
        (tmp_path / "alias.txt").symlink_to(source)
        name = "alias.txt"
    case = minimal_case()
    case["files"] = {"results/log.txt": {"path": name, "sha256": "bad" if state == "wrong_hash" else hashlib.sha256(source.read_bytes()).hexdigest()}}
    corpus = tmp_path / "manifest.json"
    corpus.write_text(json.dumps({"version": 1, "public_sanitized": state != "unapproved", "cases": [case]}))
    if state == "valid":
        assert replay.load_cases(fixtures, corpus)[0]["files"]["results/log.txt"] == source.read_bytes()
    else:
        with pytest.raises(ValueError):
            replay.load_cases(fixtures, corpus)


def test_invalid_json_does_not_erase_earlier_findings_or_stop_next_case(tmp_path):
    output = tmp_path / "acs_jsons"
    output.mkdir()
    (output / "first.json").write_text('{"test_results": null}')
    (output / "second.json").write_text('{"test_results": []}')
    case = {"raw": {name: {"records": [{"path": ["test_results", 0], "fields": {"Test_result": "PASSED"}}], "assertions": []}
                    for name in ("first.json", "second.json")}}
    errors = replay.independent_expectations(tmp_path, case)
    assert any("first.json" in error for error in errors)
    assert any("second.json" in error for error in errors)


def test_replay_exception_preserves_result_json(tmp_path, monkeypatch):
    fixtures = tmp_path / "fixtures.yaml"
    fixtures.write_text(yaml.safe_dump({"scenarios": [minimal_case()]}))
    def fail(*args, **kwargs):
        raise RuntimeError("controlled failure")
    monkeypatch.setattr(replay, "replay_revision", fail)
    output = tmp_path / "report"
    assert replay.main(["--base-parser", str(tmp_path), "--candidate-parser", str(tmp_path),
                        "--output", str(output), "--fixtures", str(fixtures)]) == 2
    assert "controlled failure" in json.loads((output / "result.json").read_text())["error"]
