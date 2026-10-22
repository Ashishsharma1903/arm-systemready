# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise the real workflow summary command, including public failure annotations."""

import os
from pathlib import Path
import json
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
import yaml


@pytest.fixture
def summarize(tmp_path):
    root = Path(__file__).resolve().parents[2]
    workflow = root / ".github/workflows/log-parser-qa.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["parser-qa"]["steps"]
    command = next(step["run"] for step in steps
                   if step["name"] == "Summarize checks and verify JUnit reports")
    command = command.replace("--baseline-aware", "")
    reports = tmp_path / "common/reports"
    reports.mkdir(parents=True)
    helper = tmp_path / "common/acs_test_framework_runner/qa_report.py"
    helper.parent.mkdir(parents=True)
    shutil.copy2(root / helper.relative_to(tmp_path), helper)
    summary = tmp_path / "summary.md"
    env = dict(os.environ, PATH=f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
               GITHUB_STEP_SUMMARY=str(summary), GUIDE_URL="https://example.invalid/guide",
               YAML_RESULT="success", E2E_RESULT="success", BROWSER_RESULT="success",
               REPLAY_RESULT="success",
               SELFTEST_RESULT="success", REGRESSION_RESULT="success",
               DEPENDENCIES_RESULT="success", IMPACT_RESULT="success")

    def run(documents, **outcomes):
        for name, document in documents.items():
            (reports / name).write_text(document)
        inventory = shlex.join([argument for name in run.required_yaml
                                for argument in ("--required-yaml", name)])
        if run.baseline_aware:
            inventory += " --baseline-aware"
        result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command.rstrip() + " " + inventory],
                                cwd=tmp_path, env=dict(env, **outcomes),
                                text=True, capture_output=True, timeout=10)
        return result, summary.read_text()

    run.findings = reports / "qa-findings.json"
    run.html = reports / "index.html"
    run.required_yaml = ["yaml.xml"]
    run.baseline_aware = False
    return run


def report(problems=(), *, name="expected_compliance", message="assert 'Compliant' == 'Not Compliant'",
           names=None, text=None, finding=None):
    suite = ET.Element("testsuite")
    for index, problem in enumerate(problems or (None,)):
        case = ET.SubElement(suite, "testcase", classname="parser_contract",
                             name=names[index] if names else name)
        if finding is not None:
            properties = ET.SubElement(case, "properties")
            ET.SubElement(properties, "property", name="qa_finding",
                          value=json.dumps(finding))
        if problem:
            ET.SubElement(case, problem, message=message).text = text
    return ET.tostring(suite, encoding="unicode")


def test_passing_reports_have_no_error_annotations(summarize):
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report()})
    assert result.returncode == 0, result.stderr
    assert "::error" not in result.stdout
    assert "| unit-e2e.xml | 1 | 0 | 0 | 0 |" in summary
    assert "[Troubleshooting guide](https://example.invalid/guide)" in summary
    assert "PASS — Log-parser regression report" in summarize.html.read_text()


@pytest.mark.parametrize("stage", ["DEPENDENCIES_RESULT", "IMPACT_RESULT", "YAML_RESULT",
                                   "E2E_RESULT", "BROWSER_RESULT", "REPLAY_RESULT"])
@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled", ""])
def test_passing_xml_cannot_hide_incomplete_required_stage(summarize, stage, outcome):
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report()},
                                **{stage: outcome})
    assert result.returncode == 1, result.stderr
    assert "::error title=Incomplete required check::" in result.stdout
    assert "Required checks did not complete successfully" in summary


@pytest.mark.parametrize("problem", ["failure", "error", "skipped"])
def test_unsuccessful_cases_fail_and_publish_exact_assertion(summarize, problem):
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report([problem])},
                                E2E_RESULT="failure")
    assert result.returncode == 1, result.stderr
    assert (f"::error title=Log-parser QA::1 case(s); first parser_contract::expected_compliance: "
            f"{problem}: assert 'Compliant' == 'Not Compliant'") in result.stdout
    assert "Contract checks failed." in summary
    assert "All checks must complete without skips." in summary


def test_annotations_escape_commands_and_summary_escapes_html(summarize):
    name = "case%\r\n::warning::<script>|"
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(
        ["failure"], name=name, message="assert '50%' == '100%'\r\nmore context")})
    assert result.returncode == 1, result.stderr
    assert result.stdout.splitlines() == [
        "::error title=Log-parser QA::1 case(s); first parser_contract::case%25%0D%0A::warning::<script>|: "
        "failure: assert '50%25' == '100%25'"
    ]
    assert "&lt;script&gt;&#124;" in summary
    assert "<script>" not in summary
    page = summarize.html.read_text()
    assert "&lt;script&gt;" in page
    assert "::warning::<script>" not in page


def test_only_first_ten_failure_families_are_annotated(summarize):
    result, summary = summarize({"yaml.xml": report(),
                                "unit-e2e.xml": report(["failure"] * 12,
                                                       names=[f"family_{index}" for index in range(12)])})
    assert result.returncode == 1, result.stderr
    assert len(result.stdout.splitlines()) == 10
    assert "Showing 10 of 12 failure groups (12 cases)" in summary


def test_many_parameter_variants_do_not_hide_another_failure_family(summarize):
    names = [f"compliance[state_{index}]" for index in range(12)] + ["html_links[missing.html]"]
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(
        ["failure"] * len(names), names=names)})
    assert result.returncode == 1, result.stderr
    assert len(result.stdout.splitlines()) == 2
    assert "12 case(s); first parser_contract::compliance[state_0]" in result.stdout
    assert "1 case(s); first parser_contract::html_links[missing.html]" in result.stdout
    assert "Showing 2 of 2 failure groups (13 cases)" in summary
    assert "<code>parser_contract::compliance</code> | 12 |" in summary


def test_schema_cause_is_not_hidden_by_cli_header_or_exit_code(summarize):
    output = ("Standalone SystemReady log parser\n" + "[BSA] Parsing logs\n" * 30
              + "#x1B[0;31m*suite=BSA issue=#x1B[1;33mMISSING_KEY#x1B[0m: "
              "'Test_suite_info' is a required property\nassert 6 == 0")
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(
        ["error"], message="failed on setup with assert 6 == 0", text=output)})
    assert result.returncode == 1, result.stderr
    assert "issue=MISSING_KEY: 'Test_suite_info' is a required property" in result.stdout
    for text in (result.stdout, summary):
        assert "assert 6 == 0" not in text
        assert "#x1B" not in text
        assert "\x1b" not in text


@pytest.mark.parametrize("cause", ["FileNotFoundError: missing input.log",
                                   "subprocess.TimeoutExpired: command exceeded 45 seconds"])
def test_concrete_exception_is_preferred_to_exit_code(summarize, cause):
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(
        ["error"], message="assert 1 == 0", text=f"E   {cause}")})
    assert result.returncode == 1, result.stderr
    assert cause in result.stdout
    assert "assert 1 == 0" not in summary


def test_yaml_failures_cannot_hide_end_to_end_diagnostics(summarize):
    result, _summary = summarize({
        "yaml.xml": report(["failure"] * 12, names=[f"yaml_{index}" for index in range(12)]),
        "unit-e2e.xml": report(["failure"], name="suite_compliance[Recommended]"),
    })
    assert result.returncode == 1, result.stderr
    assert "suite_compliance[Recommended]" in result.stdout.splitlines()[0]
    assert len(result.stdout.splitlines()) == 10


@pytest.mark.parametrize("documents", [
    {"yaml.xml": report()},
    {"unit-e2e.xml": report()},
    {"unit-e2e.xml": report(), "yaml.xml": "<testsuite/>"},
    {"unit-e2e.xml": report(), "yaml.xml": "broken XML"},
])
def test_missing_empty_or_invalid_results_fail_closed(summarize, documents):
    result, summary = summarize(documents)
    assert result.returncode == 1, result.stderr
    assert "test results are missing, empty, invalid, failed, or skipped" in summary


def test_every_selected_yaml_report_is_required(summarize):
    summarize.required_yaml = ["first.xml", "second.xml"]
    result, summary = summarize({"unit-e2e.xml": report(), "first.xml": report(),
                                 "unrelated.xml": report()})
    assert result.returncode == 1, result.stderr
    assert "Missing required report: second.xml" in summary
    assert json.loads(summarize.findings.read_text())["required_reports"] == [
        "unit-e2e.xml", "first.xml", "second.xml"]


def test_multiple_selected_yaml_reports_can_all_pass(summarize):
    summarize.required_yaml = ["first.xml", "second.xml"]
    result, _summary = summarize({name: report() for name in
                                  ("unit-e2e.xml", "first.xml", "second.xml")})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("document", ["<testsuite/>", "not XML"])
def test_one_good_yaml_cannot_hide_an_empty_or_invalid_selected_report(summarize, document):
    summarize.required_yaml = ["first.xml", "second.xml"]
    result, _summary = summarize({"unit-e2e.xml": report(), "first.xml": report(),
                                  "second.xml": document})
    assert result.returncode == 1, result.stderr


def test_default_inventory_uses_the_runner_manifest_selection(tmp_path):
    from common.acs_test_framework_runner import qa_report, pytest_runner

    groups, warnings = pytest_runner.discover_test_groups(pytest_runner.discover_yaml_files())
    assert not warnings
    selected = pytest_runner.filter_test_groups_for_target(groups, "common/log_parser")
    expected = pytest_runner.build_selected_report_paths(
        pytest_runner.selected_runs_from_groups(selected), tmp_path, [])
    names = qa_report.required_yaml_reports(tmp_path)
    assert len(names) > 1
    assert names == sorted(path.name for path in expected.values())


@pytest.mark.parametrize("names", [[], ["unit-e2e.xml"], ["../escape.xml"],
                                  ["a.xml", "a.xml"]])
def test_invalid_or_empty_yaml_inventory_is_blocked(tmp_path, names):
    from common.acs_test_framework_runner import qa_report

    result = qa_report.collect(tmp_path, {variable: "success" for _, variable, _ in qa_report.STAGES}, names)
    assert result["status"] == "BLOCKED"
    assert any(item["issue_id"] == "ci.invalid-yaml-inventory"
               for item in result["required_check_blockers"])


def comparison(*, existing=(), regressions=(), blockers=()):
    """Build a comparator result without relying on the implementation under test."""
    return {"schema_version": 1,
            "status": "REGRESSION" if regressions else "BLOCKED" if blockers else "NO_NEW_REGRESSIONS",
            "base": {"parser_revision": "base"}, "candidate": {"parser_revision": "candidate"},
            "counts": {"base_cases": 2, "candidate_cases": 2, "passed": 2 - len(existing) - len(regressions),
                       "existing": len(existing), "resolved": 0, "new": len(regressions), "worsened": 0,
                       "blocked": len(blockers)},
            "existing_issues": list(existing), "resolved_issues": [], "regressions": list(regressions),
            "blockers": list(blockers), "coverage_limits": []}


def test_baseline_known_failures_are_informative_and_preserved(summarize):
    summarize.baseline_aware = True
    old = {"case_id": "BSA.known-failure", "diagnostic": "Same assertion fails on both revisions",
           "base_signature": "expected Not Run, got Compliant", "candidate_signature": "expected Not Run, got Compliant"}
    result, summary = summarize({"qa-selftests.xml": report(),
                                 "regression.json": json.dumps(comparison(existing=[old]))})
    assert result.returncode == 0, result.stderr
    assert "::error" not in result.stdout
    assert "NO_NEW_REGRESSIONS" in summary
    assert "Existing issues (informative" in summary
    assert old["case_id"] in summarize.html.read_text()
    assert json.loads(summarize.findings.read_text())["comparison"]["existing_issues"] == [old]


@pytest.mark.parametrize("classification", ["NEW_FAILURE", "WORSENED_FAILURE"])
def test_baseline_new_or_worsened_failures_still_block(summarize, classification):
    summarize.baseline_aware = True
    issue = {"case_id": "BSA.new-failure", "classification": classification,
             "base_signature": "PASS", "candidate_signature": "FAIL", "diagnostic": "BSA failure count changed"}
    value = comparison(regressions=[issue])
    if classification == "WORSENED_FAILURE":
        value["counts"].update(new=0, worsened=1)
    result, summary = summarize({"qa-selftests.xml": report(), "regression.json": json.dumps(value)})
    assert result.returncode == 1, result.stderr
    assert "BSA failure count changed" in result.stdout
    assert "Result: **FAIL**" in summary


def test_baseline_report_shows_real_expected_actual_instead_of_signature_hashes(summarize, finding):
    summarize.baseline_aware = True
    old = {**finding, "actual": "Not Compliant"}
    proposed = {**finding, "actual": "Compliant"}
    issue = {"case_id": "BSA.policy", "classification": "WORSENED_FAILURE",
             "base_signature": "a" * 64, "candidate_signature": "b" * 64,
             "base_findings": [old], "candidate_findings": [proposed],
             "diagnostic": "A failing BSA suite was displayed as Compliant"}
    result, summary = summarize({"qa-selftests.xml": report(),
                                 "regression.json": json.dumps(comparison(regressions=[issue]))})
    assert result.returncode == 1
    recorded = json.loads(summarize.findings.read_text())["findings"][0]
    assert "Required result:" in recorded["expected"]
    assert 'Base result: "Not Compliant"' in recorded["expected"]
    assert 'Proposed result: "Compliant"' in recorded["actual"]
    assert recorded["suite"] == "BSA" and recorded["mode"] == "DT" and recorded["stage"] == "merge_jsons"
    assert recorded["base_signature"] == "a" * 64
    assert "a" * 64 not in summary and "b" * 64 not in summary
    assert "a" * 64 not in summarize.html.read_text()
    assert "Changed existing failure" in summary


def test_legacy_baseline_report_shows_diagnostics_and_previous_outcome(summarize):
    summarize.baseline_aware = True
    issue = {"case_id": "legacy.counter", "classification": "WORSENED_FAILURE",
             "base_outcome": "failure", "candidate_outcome": "failure",
             "base_signature": "hash-before", "candidate_signature": "hash-after",
             "base_diagnostic": "Expected Failed: 1; observed Failed: 2",
             "candidate_diagnostic": "Expected Failed: 1; observed Failed: 3",
             "diagnostic": "Expected Failed: 1; observed Failed: 3"}
    result, summary = summarize({"qa-selftests.xml": report(),
                                 "regression.json": json.dumps(comparison(regressions=[issue]))})
    assert result.returncode == 1
    item = json.loads(summarize.findings.read_text())["findings"][0]
    assert "Base outcome: failure" in item["expected"]
    assert "observed Failed: 2" in item["expected"]
    assert "Proposed outcome: failure" in item["actual"]
    assert "observed Failed: 3" in item["actual"]
    assert "hash-before" not in summary and "hash-after" not in summary


@pytest.mark.parametrize("document", [None, "not JSON", "{}"])
def test_baseline_missing_or_invalid_comparison_never_passes(summarize, document):
    summarize.baseline_aware = True
    documents = {"qa-selftests.xml": report()}
    if document is not None:
        documents["regression.json"] = document
    result, summary = summarize(documents)
    assert result.returncode == 1, result.stderr
    assert "Cannot verify base/candidate comparison" in summary


@pytest.mark.parametrize("selftests", [None, "<testsuite/>", "not XML", report(["failure"]), report(["skipped"])])
def test_known_baseline_issues_cannot_hide_missing_or_failing_selftests(summarize, selftests):
    summarize.baseline_aware = True
    documents = {"regression.json": json.dumps(comparison())}
    if selftests is not None:
        documents["qa-selftests.xml"] = selftests
    result, _summary = summarize(documents)
    assert result.returncode == 1, result.stderr


@pytest.mark.parametrize("stage", ["DEPENDENCIES_RESULT", "IMPACT_RESULT", "SELFTEST_RESULT",
                                   "REGRESSION_RESULT", "REPLAY_RESULT"])
def test_known_baseline_issues_cannot_hide_failed_required_stage(summarize, stage):
    summarize.baseline_aware = True
    result, _summary = summarize({"qa-selftests.xml": report(), "regression.json": json.dumps(comparison())},
                                 **{stage: "failure"})
    assert result.returncode == 1, result.stderr


def test_baseline_report_retains_incomplete_comparison_blockers(summarize):
    summarize.baseline_aware = True
    value = comparison(blockers=[{"issue_id": "missing-yaml", "reason": "Missing selected YAML report: second.xml"}])
    result, summary = summarize({"qa-selftests.xml": report(), "regression.json": json.dumps(value)})
    assert result.returncode == 1, result.stderr
    assert "Missing selected YAML report: second.xml" in summary


@pytest.mark.parametrize("change", ["empty", "hidden-regression", "count-mismatch"])
def test_baseline_passing_label_cannot_hide_inconsistent_evidence(summarize, change):
    summarize.baseline_aware = True
    value = comparison()
    if change == "empty":
        value["counts"].update(base_cases=0, candidate_cases=0)
    elif change == "hidden-regression":
        value["regressions"] = [{"case_id": "new"}]
        value["counts"]["new"] = 1
    else:
        value["counts"]["existing"] = 1
    result, summary = summarize({"qa-selftests.xml": report(), "regression.json": json.dumps(value)})
    assert result.returncode == 1, result.stderr
    assert "Cannot verify base/candidate comparison" in summary


@pytest.fixture
def finding():
    return {"issue_id": "recommended-suite-failure", "suite": "BSA", "mode": "DT",
            "stage": "merge_jsons", "status": "FAIL", "expected": "Not Compliant: Failed 1",
            "actual": "Compliant", "reproduce": {
                "argv": ["python3", "-m", "pytest", "-q", "tests/test_policy.py::test_bsa[DT]"], "cwd": "."},
            "evidence": ["pytest-work/bsa/input.log", "pytest-work/bsa/merged_results.json"]}


def test_structured_finding_exposes_every_required_field(summarize, finding):
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(["failure"], finding=finding)})
    assert result.returncode == 1, result.stderr
    data = json.loads(summarize.findings.read_text())
    recorded = data["findings"][0]
    for key, value in finding.items():
        assert recorded[key] == value
    assert recorded["metadata_incomplete"] == []
    for text in ("BSA", "DT", "merge_jsons", "Not Compliant: Failed 1", "Compliant",
                 "tests/test_policy.py::test_bsa[DT]", "input.log"):
        assert text in summary


def test_long_observations_stay_complete_in_json_and_concise_in_summary(summarize, finding):
    finding["actual"] = "observed output " * 2000
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(["failure"], finding=finding)})
    assert result.returncode == 1, result.stderr
    assert json.loads(summarize.findings.read_text())["findings"][0]["actual"] == finding["actual"]
    assert "full value in qa-findings.json" in summary
    assert len(summary) < 5000


def test_same_underlying_issue_keeps_all_variants_and_distinct_stages(summarize, finding):
    suite = ET.fromstring(report(["failure"], name="bsa[DT]", finding=finding))
    related = dict(finding, suite="Post-Script", mode="SR", actual={"result": "Compliant"})
    suite.extend(ET.fromstring(report(["failure"], name="post_script[SR]", finding=related)))
    suite.extend(ET.fromstring(report(["failure"], name="report[SR]", finding=dict(related, stage="json_to_html"))))
    result, _summary = summarize({"yaml.xml": report(), "unit-e2e.xml": ET.tostring(suite, encoding="unicode")})
    assert result.returncode == 1, result.stderr
    data = json.loads(summarize.findings.read_text())
    assert len(data["groups"]) == 2
    assert data["groups"][0]["scenario_ids"] == ["parser_contract::bsa[DT]", "parser_contract::post_script[SR]"]
    assert data["findings"][1]["actual"] == {"result": "Compliant"}
    assert data["groups"][1]["stage"] == "json_to_html"


def test_legacy_failure_does_not_invent_suite_or_expected_value(summarize):
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(["failure"], name="bsa_DT")})
    assert result.returncode == 1, result.stderr
    item = json.loads(summarize.findings.read_text())["findings"][0]
    assert item["suite"] is None
    assert item["mode"] is None
    assert item["expected"] is None
    assert "suite" in item["metadata_incomplete"]
    assert "Metadata not recorded" in summary
    assert "assert 'Compliant' == 'Not Compliant'" in item["details"]


@pytest.mark.parametrize("field", ["issue_id", "suite", "mode", "stage", "expected", "actual", "status", "reproduce", "evidence"])
def test_incomplete_metadata_is_preserved_and_blocks(summarize, finding, field):
    del finding[field]
    result, _summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(["error"], finding=finding)})
    assert result.returncode == 1, result.stderr
    item = json.loads(summarize.findings.read_text())["findings"][0]
    assert field in item["metadata_incomplete"]
    assert item["recorded_metadata"] == finding
    for key in ("suite", "mode", "expected", "actual"):
        if key != field:
            assert item[key] == finding[key]


@pytest.mark.parametrize("status", ["FAIL", "BLOCKED"])
def test_recorded_problem_cannot_pass_just_because_junit_case_passed(summarize, finding, status):
    finding["status"] = status
    result, _summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(finding=finding)})
    assert result.returncode == 1, result.stderr
    assert json.loads(summarize.findings.read_text())["status"] == status


def test_required_setup_failure_is_blocked_with_complete_context(summarize):
    result, _summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report()}, DEPENDENCIES_RESULT="failure")
    assert result.returncode == 1, result.stderr
    data = json.loads(summarize.findings.read_text())
    assert data["status"] == "BLOCKED"
    item = data["required_check_blockers"][0]
    assert (item["suite"], item["mode"], item["stage"]) == ("ALL", "ALL", "setup")
    assert item["expected"] == "success"
    assert item["actual"] == "Dependency setup: failure"


@pytest.mark.parametrize("field,value", [
    ("suite", ""), ("stage", {}), ("status", "PASS"), ("evidence", "input.log"),
    ("reproduce", {}), ("reproduce", {"argv": "python3", "cwd": "."}),
    ("reproduce", {"argv": [1], "cwd": "."}), ("reproduce", {"argv": ["python3"], "cwd": ""}),
])
def test_invalid_metadata_types_cannot_make_a_green_report(summarize, finding, field, value):
    finding[field] = value
    result, _summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(finding=finding)})
    assert result.returncode == 1, result.stderr
    item = json.loads(summarize.findings.read_text())["findings"][0]
    assert field in item["metadata_incomplete"]


@pytest.mark.parametrize("encoded", ["{broken", "[]", '{"expected": NaN}'])
def test_invalid_property_json_is_reported_without_losing_artifact(summarize, encoded):
    tree = ET.fromstring(report(["failure"], finding={}))
    tree.find("testcase/properties/property").set("value", encoded)
    result, _summary = summarize({"yaml.xml": report(), "unit-e2e.xml": ET.tostring(tree, encoding="unicode")})
    assert result.returncode == 1, result.stderr
    item = json.loads(summarize.findings.read_text())["findings"][0]
    assert item["metadata_error"]
    assert item["details"]
