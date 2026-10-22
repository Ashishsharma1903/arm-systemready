# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Mutation controls proving that replay cannot silently approve changed results."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from . import parser_replay as replay


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
