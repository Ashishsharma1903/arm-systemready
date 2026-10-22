# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Prove that baseline comparison never hides a changed or unverified result."""

import copy
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from . import regression_gate as gate


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
    {"outcome": "skipped"},
    {"outcome": "error", "diagnostic": "E ModuleNotFoundError: No module named 'yaml'"},
    {"outcome": "failure", "diagnostic": "subprocess.TimeoutExpired: main_log_parser.sh timed out"},
    {"outcome": "error", "diagnostic": "RuntimeError: Required parser dependency unavailable"},
    {"outcome": "error", "diagnostic": "RuntimeError: unrelated harness fixture crashed"},
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
    code = gate.main(["--base-reports", str(values["base"]["reports"]),
                      "--candidate-reports", str(values["candidate"]["reports"]),
                      "--base-manifest", str(values["base"]["manifest"]),
                      "--candidate-manifest", str(values["candidate"]["manifest"]),
                      "--output", str(output)])
    assert code == expected_exit
    result = json.loads(output.read_text())
    assert result["schema_version"] == 1
    assert result["status"] == {0: "NO_NEW_REGRESSIONS", 1: "REGRESSION", 2: "BLOCKED"}[expected_exit]
