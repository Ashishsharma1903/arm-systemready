#!/usr/bin/env python3
# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier : Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Check workflow security, clear reporting and isolated browser execution."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from threading import Barrier
import xml.etree.ElementTree as ET

import pytest
import yaml

from .. import report_ui_browser_smoke as browser



# Workflow reporting checks.

def test_workflow_runs_once_on_python_312():
    """Use one CI runtime without duplicating the full DT/SR check run."""
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / ".github/workflows/log-parser-qa.yml").read_text())
    job = workflow["jobs"]["parser-qa"]
    assert "strategy" not in job
    assert job["name"] == "Parser QA (Python 3.12)"
    setup = next(step for step in job["steps"] if step["name"] == "Set up Python")
    assert setup["with"]["python-version"] == "3.12"
    upload = next(step for step in job["steps"]
                  if step["name"] == "Upload test reports and full logs")
    assert upload["with"]["name"] == "log-parser-qa-python-3.12"


@pytest.fixture
def summarize(tmp_path):
    """Provide the workflow summary command with isolated reports and outputs."""
    root = Path(__file__).resolve().parents[3]
    workflow = root / ".github/workflows/log-parser-qa.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["parser-qa"]["steps"]
    command = next(step["run"] for step in steps
                   if step["name"] == "Summarize checks and verify JUnit reports")
    command = command.replace("--baseline-aware", "")
    reports = tmp_path / "common/reports"
    reports.mkdir(parents=True)
    helper = tmp_path / "common/acs_test_framework_runner/log_parser_qa/qa_report.py"
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
        """Run the summary command on the supplied reports and stage outcomes."""
        for name, document in documents.items():
            (reports / name).write_text(document)
        inventory = shlex.join([argument for name in run.required_yaml
                                for argument in ("--required-yaml", name)])
        if run.baseline_aware:
            inventory += " --baseline-aware"
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c",
             command.rstrip() + " " + inventory],
            cwd=tmp_path,
            env=dict(env, **outcomes),
            text=True,
            capture_output=True,
            timeout=10)
        return result, summary.read_text()

    run.findings = reports / "qa-findings.json"
    run.html = reports / "index.html"
    run.required_yaml = ["yaml.xml"]
    run.baseline_aware = False
    return run


def report(problems=(),
           *,
           name="expected_compliance",
           message="assert 'Compliant' == 'Not Compliant'",
           names=None,
           text=None,
           finding=None):
    """Build a small JUnit document with optional failures and evidence."""
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
    """Check passing reports have no error annotations."""
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report()})
    assert result.returncode == 0, result.stderr
    assert "::error" not in result.stdout
    assert "| unit-e2e.xml | 1 | 0 | 0 | 0 |" in summary
    assert "[Troubleshooting guide](https://example.invalid/guide)" in summary
    assert "PASS — Log-parser regression report" in summarize.html.read_text()


@pytest.mark.parametrize("baseline_aware", [False, True])
def test_summary_still_reports_failed_dependency_setup_without_installed_packages(
        tmp_path, baseline_aware):
    """The always-run summary must explain setup failure without third-party imports."""
    reports = tmp_path / "reports"
    reports.mkdir()
    for name in ("yaml.xml", "unit-e2e.xml"):
        (reports / name).write_text(report())
    findings = reports / "qa-findings.json"
    summary = reports / "summary.md"
    command = [sys.executable, "-S", str(Path(__file__).with_name("qa_report.py")),
               "--reports", str(reports), "--summary", str(summary),
               "--findings", str(findings), "--required-yaml", "yaml.xml"]
    if baseline_aware:
        command.append("--baseline-aware")
    result = subprocess.run(
        command,
        cwd=tmp_path, text=True, capture_output=True, check=False, timeout=30,
        env={**os.environ, "PYTHONPATH": "", "DEPENDENCIES_RESULT": "failure"},
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert json.loads(findings.read_text())["status"] == "BLOCKED"
    assert "Dependency setup" in summary.read_text()
    assert "failure" in (reports / "index.html").read_text()


@pytest.mark.parametrize("baseline_aware", [False, True])
@pytest.mark.parametrize("stage,name,cause", [
    ("IMPACT_RESULT", "change-impact.stderr.log",
     "Change classification failed: Invalid JSON in common/log_parser/suite_registry.json "
     "at revision abc123: Expecting value (line 2, column 14)"),
    ("DEPENDENCIES_RESULT", "dependencies.log", "ERROR: Could not install required-package"),
])
def test_saved_stage_error_is_clear_in_artifact_and_summary(summarize, baseline_aware, stage, name,
                                                            cause):
    """Check saved stage error is clear in artifact and summary."""
    summarize.baseline_aware = baseline_aware
    documents = {"yaml.xml": report(), "unit-e2e.xml": report(),
                 "qa-selftests.xml": report(), "regression.json": json.dumps(comparison()),
                 name: "Old progress line\n" * 2000 + cause + "\n"}
    result, summary = summarize(documents, **{stage: "failure"})
    assert result.returncode == 1
    data = json.loads(summarize.findings.read_text())
    item = next(item for item in data["required_check_blockers"] if item["evidence"] == [name])
    assert item["diagnostic"] == cause
    assert cause in summary and name in summary
    assert cause in summarize.html.read_text() and name in summarize.html.read_text()
    assert len(item["details"]) <= 16000


@pytest.mark.parametrize("text", ["", "\n \n", "\x1b[31m\x1b[0m"])
def test_empty_stage_diagnostic_keeps_original_blocker(tmp_path, text):
    """Check empty stage diagnostic keeps original blocker."""
    from . import qa_report

    (tmp_path / "change-impact.stderr.log").write_text(text)
    item = qa_report.required_stage_blocker(tmp_path, "Change classification", "classification",
                                            "failure")
    assert item["status"] == "BLOCKED"
    assert item["actual"] == "Change classification: failure"


@pytest.mark.parametrize("stage", ["DEPENDENCIES_RESULT", "IMPACT_RESULT", "YAML_RESULT",
                                   "E2E_RESULT", "BROWSER_RESULT", "REPLAY_RESULT"])
@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled", ""])
def test_passing_xml_cannot_hide_incomplete_required_stage(summarize, stage, outcome):
    """Check passing XML cannot hide incomplete required stage."""
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report()},
                                **{stage: outcome})
    assert result.returncode == 1, result.stderr
    assert "::error title=Incomplete required check::" in result.stdout
    assert "Required checks did not complete successfully" in summary


@pytest.mark.parametrize("problem", ["failure", "error", "skipped"])
def test_unsuccessful_cases_fail_and_publish_exact_assertion(summarize, problem):
    """Check unsuccessful cases fail and publish exact assertion."""
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report([problem])},
                                E2E_RESULT="failure")
    assert result.returncode == 1, result.stderr
    assert (f"::error title=Log-parser QA::1 case(s); first parser_contract::expected_compliance: "
            f"{problem}: assert 'Compliant' == 'Not Compliant'") in result.stdout
    assert "Contract checks failed." in summary
    assert "All checks must complete without skips." in summary


def test_annotations_escape_commands_and_summary_escapes_html(summarize):
    """Check annotations escape commands and summary escapes HTML."""
    name = "case%\r\n::warning::<script>|"
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(
        ["failure"], name=name, message="assert '50%' == '100%'\r\nmore context")})
    assert result.returncode == 1, result.stderr
    assert result.stdout.splitlines() == [
        "::error title=Log-parser QA::1 case(s); first "
        "parser_contract::case%25%0D%0A::warning::<script>|: "
        "failure: assert '50%25' == '100%25'"
    ]
    assert "&lt;script&gt;&#124;" in summary
    assert "<script>" not in summary
    page = summarize.html.read_text()
    assert "&lt;script&gt;" in page
    assert "::warning::<script>" not in page


def test_only_first_ten_failure_families_are_annotated(summarize):
    """Check only first ten failure families are annotated."""
    result, summary = summarize({
        "yaml.xml":
        report(),
        "unit-e2e.xml":
        report(["failure"] * 12, names=[f"family_{index}" for index in range(12)])
    })
    assert result.returncode == 1, result.stderr
    assert len(result.stdout.splitlines()) == 10
    assert "Showing 10 of 12 failure groups (12 cases)" in summary


def test_many_parameter_variants_do_not_hide_another_failure_family(summarize):
    """Check many parameter variants do not hide another failure family."""
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
    """Check schema cause is not hidden by CLI header or exit code."""
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
    """Check concrete exception is preferred to exit code."""
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(
        ["error"], message="assert 1 == 0", text=f"E   {cause}")})
    assert result.returncode == 1, result.stderr
    assert cause in result.stdout
    assert "assert 1 == 0" not in summary


def test_yaml_failures_cannot_hide_end_to_end_diagnostics(summarize):
    """Check YAML failures cannot hide end to end diagnostics."""
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
    """Check missing empty or invalid results fail closed."""
    result, summary = summarize(documents)
    assert result.returncode == 1, result.stderr
    assert "test results are missing, empty, invalid, failed, or skipped" in summary


def test_every_selected_yaml_report_is_required(summarize):
    """Check every selected YAML report is required."""
    summarize.required_yaml = ["first.xml", "second.xml"]
    result, summary = summarize({"unit-e2e.xml": report(), "first.xml": report(),
                                 "unrelated.xml": report()})
    assert result.returncode == 1, result.stderr
    assert "Missing required report: second.xml" in summary
    assert json.loads(summarize.findings.read_text())["required_reports"] == [
        "unit-e2e.xml", "first.xml", "second.xml"]


def test_multiple_selected_yaml_reports_can_all_pass(summarize):
    """Check multiple selected YAML reports can all pass."""
    summarize.required_yaml = ["first.xml", "second.xml"]
    result, _summary = summarize({name: report() for name in
                                  ("unit-e2e.xml", "first.xml", "second.xml")})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("document", ["<testsuite/>", "not XML"])
def test_one_good_yaml_cannot_hide_an_empty_or_invalid_selected_report(summarize, document):
    """Check one good YAML cannot hide an empty or invalid selected report."""
    summarize.required_yaml = ["first.xml", "second.xml"]
    result, _summary = summarize({"unit-e2e.xml": report(), "first.xml": report(),
                                  "second.xml": document})
    assert result.returncode == 1, result.stderr


def test_default_inventory_uses_the_runner_manifest_selection(tmp_path):
    """Check default inventory uses the runner manifest selection."""
    from common.acs_test_framework_runner import pytest_runner
    from . import qa_report

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
    """Check invalid or empty YAML inventory is blocked."""
    from . import qa_report

    result = qa_report.collect(tmp_path, {
        variable: "success"
        for _, variable, _ in qa_report.STAGES
    }, names)
    assert result["status"] == "BLOCKED"
    assert any(item["issue_id"] == "ci.invalid-yaml-inventory"
               for item in result["required_check_blockers"])


def comparison(*, existing=(), regressions=(), blockers=()):
    """Build a comparator result without relying on the implementation under test."""
    return {
        "schema_version": 1,
        "status": "REGRESSION" if regressions else "BLOCKED" if blockers else "NO_NEW_REGRESSIONS",
        "base": {
            "parser_revision": "base"
        },
        "candidate": {
            "parser_revision": "candidate"
        },
        "counts": {
            "base_cases": 2,
            "candidate_cases": 2,
            "passed": 2 - len(existing) - len(regressions),
            "existing": len(existing),
            "resolved": 0,
            "new": len(regressions),
            "worsened": 0,
            "blocked": len(blockers)
        },
        "existing_issues": list(existing),
        "resolved_issues": [],
        "regressions": list(regressions),
        "blockers": list(blockers),
        "coverage_limits": []
    }


def test_baseline_known_failures_are_informative_and_preserved(summarize):
    """Check baseline known failures are informative and preserved."""
    summarize.baseline_aware = True
    old = {
        "case_id": "BSA.known-failure",
        "diagnostic": "Same assertion fails on both revisions",
        "base_signature": "expected Not Run, got Compliant",
        "candidate_signature": "expected Not Run, got Compliant"
    }
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
    """Check baseline new or worsened failures still block."""
    summarize.baseline_aware = True
    issue = {
        "case_id": "BSA.new-failure",
        "classification": classification,
        "base_signature": "PASS",
        "candidate_signature": "FAIL",
        "diagnostic": "BSA failure count changed"
    }
    value = comparison(regressions=[issue])
    if classification == "WORSENED_FAILURE":
        value["counts"].update(new=0, worsened=1)
    result, summary = summarize({
        "qa-selftests.xml": report(),
        "regression.json": json.dumps(value)
    })
    assert result.returncode == 1, result.stderr
    assert "BSA failure count changed" in result.stdout
    assert "Result: **FAIL**" in summary


def test_baseline_report_shows_real_expected_actual_instead_of_signature_hashes(summarize, finding):
    """Check baseline report shows real expected actual instead of signature hashes."""
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
    assert recorded["suite"] == "BSA" and recorded["mode"] == "DT" and recorded[
        "stage"] == "merge_jsons"
    assert recorded["base_signature"] == "a" * 64
    assert "a" * 64 not in summary and "b" * 64 not in summary
    assert "a" * 64 not in summarize.html.read_text()
    assert "Changed existing failure" in summary


def test_legacy_baseline_report_shows_diagnostics_and_previous_outcome(summarize):
    """Check legacy baseline report shows diagnostics and previous outcome."""
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
    """Check baseline missing or invalid comparison never passes."""
    summarize.baseline_aware = True
    documents = {"qa-selftests.xml": report()}
    if document is not None:
        documents["regression.json"] = document
    result, summary = summarize(documents)
    assert result.returncode == 1, result.stderr
    assert "Cannot verify base/candidate comparison" in summary


@pytest.mark.parametrize(
    "selftests",
    [None, "<testsuite/>", "not XML",
     report(["failure"]), report(["skipped"])])
def test_known_baseline_issues_cannot_hide_missing_or_failing_selftests(summarize, selftests):
    """Check known baseline issues cannot hide missing or failing selftests."""
    summarize.baseline_aware = True
    documents = {"regression.json": json.dumps(comparison())}
    if selftests is not None:
        documents["qa-selftests.xml"] = selftests
    result, _summary = summarize(documents)
    assert result.returncode == 1, result.stderr


@pytest.mark.parametrize("stage", ["DEPENDENCIES_RESULT", "IMPACT_RESULT", "SELFTEST_RESULT",
                                   "REGRESSION_RESULT", "REPLAY_RESULT"])
def test_known_baseline_issues_cannot_hide_failed_required_stage(summarize, stage):
    """Check known baseline issues cannot hide failed required stage."""
    summarize.baseline_aware = True
    result, _summary = summarize(
        {
            "qa-selftests.xml": report(),
            "regression.json": json.dumps(comparison())
        }, **{stage: "failure"})
    assert result.returncode == 1, result.stderr


def test_baseline_report_retains_incomplete_comparison_blockers(summarize):
    """Check baseline report retains incomplete comparison blockers."""
    summarize.baseline_aware = True
    value = comparison(blockers=[{
        "issue_id": "missing-yaml",
        "reason": "Missing selected YAML report: second.xml"
    }])
    result, summary = summarize({
        "qa-selftests.xml": report(),
        "regression.json": json.dumps(value)
    })
    assert result.returncode == 1, result.stderr
    assert "Missing selected YAML report: second.xml" in summary


@pytest.mark.parametrize("change", ["empty", "hidden-regression", "count-mismatch"])
def test_baseline_passing_label_cannot_hide_inconsistent_evidence(summarize, change):
    """Check baseline passing label cannot hide inconsistent evidence."""
    summarize.baseline_aware = True
    value = comparison()
    if change == "empty":
        value["counts"].update(base_cases=0, candidate_cases=0)
    elif change == "hidden-regression":
        value["regressions"] = [{"case_id": "new"}]
        value["counts"]["new"] = 1
    else:
        value["counts"]["existing"] = 1
    result, summary = summarize({
        "qa-selftests.xml": report(),
        "regression.json": json.dumps(value)
    })
    assert result.returncode == 1, result.stderr
    assert "Cannot verify base/candidate comparison" in summary


@pytest.fixture
def finding():
    """Create a structured finding for report rendering checks."""
    return {
        "issue_id": "recommended-suite-failure",
        "suite": "BSA",
        "mode": "DT",
        "stage": "merge_jsons",
        "status": "FAIL",
        "expected": "Not Compliant: Failed 1",
        "actual": "Compliant",
        "reproduce": {
            "argv": ["python3", "-m", "pytest", "-q", "tests/test_policy.py::test_bsa[DT]"],
            "cwd": "."
        },
        "evidence": ["pytest-work/bsa/input.log", "pytest-work/bsa/merged_results.json"]
    }


def test_structured_finding_exposes_every_required_field(summarize, finding):
    """Check structured finding exposes every required field."""
    result, summary = summarize({
        "yaml.xml": report(),
        "unit-e2e.xml": report(["failure"], finding=finding)
    })
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
    """Check long observations stay complete in JSON and concise in summary."""
    finding["actual"] = "observed output " * 2000
    result, summary = summarize({
        "yaml.xml": report(),
        "unit-e2e.xml": report(["failure"], finding=finding)
    })
    assert result.returncode == 1, result.stderr
    assert json.loads(summarize.findings.read_text())["findings"][0]["actual"] == finding["actual"]
    assert "full value in qa-findings.json" in summary
    assert len(summary) < 5000


def test_same_underlying_issue_keeps_all_variants_and_distinct_stages(summarize, finding):
    """Check same underlying issue keeps all variants and distinct stages."""
    suite = ET.fromstring(report(["failure"], name="bsa[DT]", finding=finding))
    related = dict(finding, suite="Post-Script", mode="SR", actual={"result": "Compliant"})
    suite.extend(ET.fromstring(report(["failure"], name="post_script[SR]", finding=related)))
    suite.extend(
        ET.fromstring(
            report(["failure"], name="report[SR]", finding=dict(related, stage="json_to_html"))))
    result, _summary = summarize({
        "yaml.xml": report(),
        "unit-e2e.xml": ET.tostring(suite, encoding="unicode")
    })
    assert result.returncode == 1, result.stderr
    data = json.loads(summarize.findings.read_text())
    assert len(data["groups"]) == 2
    assert data["groups"][0]["scenario_ids"] == [
        "parser_contract::bsa[DT]", "parser_contract::post_script[SR]"
    ]
    assert data["findings"][1]["actual"] == {"result": "Compliant"}
    assert data["groups"][1]["stage"] == "json_to_html"


def test_legacy_failure_does_not_invent_suite_or_expected_value(summarize):
    """Check legacy failure does not invent suite or expected value."""
    result, summary = summarize({
        "yaml.xml": report(),
        "unit-e2e.xml": report(["failure"], name="bsa_DT")
    })
    assert result.returncode == 1, result.stderr
    item = json.loads(summarize.findings.read_text())["findings"][0]
    assert item["suite"] is None
    assert item["mode"] is None
    assert item["expected"] is None
    assert "suite" in item["metadata_incomplete"]
    assert "Metadata not recorded" in summary
    assert "assert 'Compliant' == 'Not Compliant'" in item["details"]


@pytest.mark.parametrize(
    "field",
    ["issue_id", "suite", "mode", "stage", "expected", "actual", "status", "reproduce", "evidence"])
def test_incomplete_metadata_is_preserved_and_blocks(summarize, finding, field):
    """Check incomplete metadata is preserved and blocks."""
    del finding[field]
    result, _summary = summarize({
        "yaml.xml": report(),
        "unit-e2e.xml": report(["error"], finding=finding)
    })
    assert result.returncode == 1, result.stderr
    item = json.loads(summarize.findings.read_text())["findings"][0]
    assert field in item["metadata_incomplete"]
    assert item["recorded_metadata"] == finding
    for key in ("suite", "mode", "expected", "actual"):
        if key != field:
            assert item[key] == finding[key]


@pytest.mark.parametrize("status", ["FAIL", "BLOCKED"])
def test_recorded_problem_cannot_pass_just_because_junit_case_passed(summarize, finding, status):
    """Check recorded problem cannot pass just because junit case passed."""
    finding["status"] = status
    result, _summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(finding=finding)})
    assert result.returncode == 1, result.stderr
    assert json.loads(summarize.findings.read_text())["status"] == status


def test_required_setup_failure_is_blocked_with_complete_context(summarize):
    """Check required setup failure is blocked with complete context."""
    result, _summary = summarize({
        "yaml.xml": report(),
        "unit-e2e.xml": report()
    },
                                 DEPENDENCIES_RESULT="failure")
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
    """Check invalid metadata types cannot make a green report."""
    finding[field] = value
    result, _summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report(finding=finding)})
    assert result.returncode == 1, result.stderr
    item = json.loads(summarize.findings.read_text())["findings"][0]
    assert field in item["metadata_incomplete"]


@pytest.mark.parametrize("encoded", ["{broken", "[]", '{"expected": NaN}'])
def test_invalid_property_json_is_reported_without_losing_artifact(summarize, encoded):
    """Check invalid property JSON is reported without losing artifact."""
    tree = ET.fromstring(report(["failure"], finding={}))
    tree.find("testcase/properties/property").set("value", encoded)
    result, _summary = summarize({
        "yaml.xml": report(),
        "unit-e2e.xml": ET.tostring(tree, encoding="unicode")
    })
    assert result.returncode == 1, result.stderr
    item = json.loads(summarize.findings.read_text())["findings"][0]
    assert item["metadata_error"]
    assert item["details"]


# Workflow scope checks.

WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/log-parser-qa.yml"


def workflow():
    # BaseLoader keeps the YAML key `on` as text, not the YAML 1.1 boolean True.
    """Load the checked-in workflow for direct configuration assertions."""
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize("name", ["qa_run", "qa_report", "parser_artifacts",
                                  "parser_replay", "mutation_audit"])
@pytest.mark.parametrize("entrypoint", ["script", "module"])
def test_relocated_qa_commands_support_script_and_module_entrypoints(name, entrypoint):
    """The workflow and local users can still launch every relocated command."""
    root = Path(__file__).resolve().parents[3]
    if entrypoint == "script":
        command = [sys.executable, str(Path(__file__).with_name(name + ".py")), "--help"]
    else:
        command = [sys.executable, "-m",
                   f"common.acs_test_framework_runner.log_parser_qa.{name}", "--help"]
    result = subprocess.run(command, cwd=root, text=True, capture_output=True,
                            check=False, timeout=30,
                            env={**os.environ, "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout


def test_only_parser_changes_trigger_push_and_pr_jobs():
    """Check only parser changes trigger push and PR jobs."""
    events = workflow()["on"]
    assert set(events) == {"push", "pull_request"}
    for event in events.values():
        assert event == {"paths": ["common/log_parser/**"]}


def test_untrusted_code_has_no_write_token_or_privileged_trigger():
    """Check untrusted code has no write token or privileged trigger."""
    config = workflow()
    assert config["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in config["on"]
    for job in config["jobs"].values():
        assert not job.get("secrets")
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
                if step["uses"].startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] == "false"


def test_workflow_budget_covers_required_stages_and_saves_setup_diagnostics():
    """Check workflow budget covers required stages and saves setup diagnostics."""
    job = workflow()["jobs"]["parser-qa"]
    steps = job["steps"]
    # Budget for both revisions' YAML/contracts plus their comparison. No stage
    # is skipped or shortened to fit the old overall one-hour limit.
    regression = next(step for step in steps if step.get("id") == "regression")
    assert "--stage-timeout 1800" in regression["run"]
    assert int(regression["timeout-minutes"]) >= 5 * 30 + 5
    assert all(int(step["timeout-minutes"]) > 0 for step in steps)
    assert int(job["timeout-minutes"]) >= sum(int(step["timeout-minutes"]) for step in steps) + 5
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == "1"
    setup = next(step for step in steps if step.get("id") == "dependencies")
    assert "tee common/reports/dependencies.log" in setup["run"]
    assert "dpkg-query" in setup["run"] and "${#missing[@]}" in setup["run"]
    assert "Acquire::Retries=3" in setup["run"]
    assert "pip install --retries 3 --timeout 30" in setup["run"]
    impact = next(step for step in steps if step.get("id") == "impact")
    assert "tee common/reports/change-impact.stderr.log" in impact["run"]


def test_every_workflow_shell_script_has_valid_bash_syntax():
    """Check every workflow shell script has valid bash syntax."""
    for step in workflow()["jobs"]["parser-qa"]["steps"]:
        if "run" in step:
            result = subprocess.run(["bash", "-n"], input=step["run"], text=True,
                                    capture_output=True, check=False, timeout=10)
            assert result.returncode == 0, (step["name"], result.stderr)


def test_classification_shell_preserves_stderr_when_json_output_is_empty(tmp_path):
    """The exact workflow script keeps the original error in its artifact."""
    subprocess.run(["git", "init", "--initial-branch=main", str(tmp_path)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=CI test",
                    "-c", "user.email=ci@example.invalid", "commit", "--allow-empty", "-m", "base"],
                   check=True, capture_output=True)
    base = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                                   text=True).strip()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_python = bindir / "python"
    cause = ("Change classification failed: Invalid JSON in "
             "common/log_parser/suite_registry.json at revision " + base)
    fake_python.write_text("#!/bin/sh\nprintf '%s\\n' '" + cause + "' >&2\nexit 2\n")
    fake_python.chmod(0o755)
    step = next(step for step in workflow()["jobs"]["parser-qa"]["steps"]
                if step.get("id") == "impact")
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                            cwd=tmp_path,
                            text=True,
                            capture_output=True,
                            check=False,
                            timeout=10,
                            env={
                                **os.environ, "BASE_SHA": base,
                                "PATH": str(bindir) + os.pathsep + os.environ["PATH"]
                            })
    assert result.returncode == 2
    assert cause in result.stderr
    assert cause in (tmp_path / "common/reports/change-impact.stderr.log").read_text()
    assert (tmp_path / "common/reports/change-impact.json").read_text() == ""


@pytest.mark.parametrize("missing,pip_status", [("", 0), ("libharfbuzz-subset0", 0), ("", 2)])
def test_dependency_setup_skips_apt_if_installed_and_retains_pip_error(
        tmp_path, missing, pip_status):
    """Exercise the workflow shell without installing packages or using sudo."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    scripts = {
        "dpkg-query":
        '#!/bin/sh\n[ "$3" = "$MISSING_LIB" ] && exit 1\nprintf "install ok installed"\n',
        "sudo": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n',
        "python": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n'
        'if [ "$PIP_STATUS" != 0 ]; then echo "ERROR: pip setup failed" >&2; fi\n'
        'exit "$PIP_STATUS"\n',
        "chromium": '#!/bin/sh\necho "Chromium test stub"\n',
    }
    for name, script in scripts.items():
        path = bindir / name
        path.write_text(script)
        path.chmod(0o755)
    calls = tmp_path / "calls.log"
    step = next(step for step in workflow()["jobs"]["parser-qa"]["steps"]
                if step.get("id") == "dependencies")
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
        env={
            **os.environ, "MISSING_LIB": missing,
            "PIP_STATUS": str(pip_status),
            "CALL_LOG": str(calls),
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"]
        })
    assert result.returncode == pip_status, result.stdout + result.stderr
    commands = calls.read_text()
    assert "-m pip install --retries 3 --timeout 30" in commands
    assert ("apt-get" in commands) == bool(missing)
    if missing:
        assert "install -y --no-install-recommends " + missing in commands
        assert "libpango-1.0-0" not in commands
    if pip_status:
        assert "ERROR: pip setup failed" in (tmp_path /
                                             "common/reports/dependencies.log").read_text()


def test_new_suite_contracts_run_once_and_evidence_survives_failures():
    """Check new suite contracts run once and evidence survives failures."""
    steps = workflow()["jobs"]["parser-qa"]["steps"]
    assert not any(step.get("id") == "onboarding" for step in steps)
    contracts = [step for step in steps if step.get("id") == "regression"]
    assert len(contracts) == 1
    assert "qa_run.py" in contracts[0]["run"]
    assert "--base" in contracts[0]["run"]
    selftests = next(step for step in steps if step.get("id") == "selftests")
    assert "--ignore=common/acs_test_framework_runner/log_parser_qa/tests" in selftests["run"]
    summary = next(step for step in steps
                   if step["name"] == "Summarize checks and verify JUnit reports")
    assert "--baseline-aware" in summary["run"]
    for name in ("Summarize checks and verify JUnit reports", "Upload test reports and full logs"):
        assert next(step for step in steps if step["name"] == name)["if"] == "${{ always() }}"


def test_replay_uses_base_revision_and_candidate_on_identical_inputs():
    """Check replay uses base revision and candidate on identical inputs."""
    step = next(step for step in workflow()["jobs"]["parser-qa"]["steps"]
                if step.get("id") == "replay")
    assert 'git archive "$base"' in step["run"]
    assert "--base-parser" in step["run"]
    assert "--candidate-parser common/log_parser" in step["run"]


@pytest.mark.parametrize("available", [True, False])
def test_force_push_base_is_fetched_or_fails_without_changing_the_base(tmp_path, available):
    """A shallow clone fetches the exact old tip, even after a force push."""
    def git(directory, *arguments, check=True):
        """Run Git against a disposable repository with controllable error handling."""
        return subprocess.run(["git", "-C", str(directory), *arguments],
                              text=True, capture_output=True, check=check)

    remote, author, fresh = (tmp_path / name for name in ("remote.git", "author", "fresh"))
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True,
                   capture_output=True)
    # The local test server models GitHub's retained-commit SHA fetch support.
    git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    subprocess.run(["git", "init", "--initial-branch=main",
                    str(author)],
                   check=True,
                   capture_output=True)
    git(author, "config", "user.name", "Parser CI test")
    git(author, "config", "user.email", "parser-ci@example.invalid")
    source = author / "common/log_parser/input.txt"
    source.parent.mkdir(parents=True)
    source.write_text("old parser revision\n")
    git(author, "add", ".")
    git(author, "commit", "-m", "old tip")
    old_tip = git(author, "rev-parse", "HEAD").stdout.strip()
    git(author, "remote", "add", "origin", str(remote))
    git(author, "push", "origin", "main")
    git(author, "checkout", "--orphan", "replacement")
    source.write_text("replacement parser revision\n")
    git(author, "add", ".")
    git(author, "commit", "-m", "replacement root")
    git(author, "push", "--force", "origin", "HEAD:main")
    # --no-local avoids copying unreachable loose objects from the bare repo.
    subprocess.run(["git", "clone", "--depth=1", "--no-local", str(remote), str(fresh)],
                   check=True, capture_output=True)
    assert git(fresh, "cat-file", "-e", old_tip + "^{commit}", check=False).returncode != 0
    requested = old_tip if available else "1" * 40
    step = next(item for item in workflow()["jobs"]["parser-qa"]["steps"]
                if item.get("id") == "impact")
    prefix = step["run"].split(
        "python common/acs_test_framework_runner/log_parser_qa/qa_run.py --classify", 1
    )[0]
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c",
                             prefix + '\ngit rev-parse --verify "$BASE_SHA^{commit}"\n'],
                            cwd=fresh, env={**os.environ, "BASE_SHA": requested},
                            text=True, capture_output=True, check=False, timeout=30)
    if available:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == old_tip
        assert git(fresh, "cat-file", "-e", old_tip + "^{commit}").returncode == 0
    else:
        assert result.returncode != 0
        assert not result.stdout.strip(), "An unavailable nonzero base must not fall back to main"
        assert (fresh / "common/reports/change-impact.stderr.log").read_text()


@pytest.mark.parametrize("base", ["", "0" * 40])
def test_new_branch_shallow_checkout_fetches_exact_main(tmp_path, base):
    """A first push compares against main, not its own parent or an old ref."""
    def git(directory, *arguments):
        """Run Git in a disposable shallow-clone scenario and return its output."""
        return subprocess.run(["git", "-C", str(directory), *arguments], check=True,
                              text=True, capture_output=True).stdout.strip()

    remote, author, fresh = (tmp_path / name for name in ("remote.git", "author", "fresh"))
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(author)],
                   check=True, capture_output=True)
    git(author, "config", "user.name", "Parser CI test")
    git(author, "config", "user.email", "parser-ci@example.invalid")
    (author / "parser.txt").write_text("main\n")
    git(author, "add", ".")
    git(author, "commit", "-m", "main base")
    expected = git(author, "rev-parse", "HEAD")
    git(author, "remote", "add", "origin", str(remote))
    git(author, "push", "origin", "main")
    git(author, "checkout", "-b", "candidate")
    (author / "parser.txt").write_text("candidate\n")
    git(author, "commit", "-am", "candidate")
    git(author, "push", "origin", "candidate")
    subprocess.run(["git", "clone", "--depth=1", "--single-branch", "--branch", "candidate",
                    "--no-local", str(remote), str(fresh)], check=True, capture_output=True)
    assert git(fresh, "rev-parse", "HEAD") != expected
    step = next(step for step in workflow()["jobs"]["parser-qa"]["steps"]
                if step.get("id") == "impact")
    prefix = step["run"].split(
        "python common/acs_test_framework_runner/log_parser_qa/qa_run.py --classify", 1)[0]
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", prefix + '\nprintf "%s\\n" "$BASE_SHA"'],
        cwd=fresh,
        env={
            **os.environ, "BASE_SHA": base
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
    assert git(fresh, "rev-parse", "HEAD") != expected


@pytest.mark.parametrize("revision", ["base", "candidate"])
def test_bad_registry_names_path_revision_and_json_position(tmp_path, monkeypatch, capsys,
                                                            revision):
    """Check bad registry names path revision and JSON position."""
    from . import qa_run

    def fake_git(root, *arguments):
        """Supply controlled Git responses for malformed registry diagnostics."""
        if arguments[0] == "rev-parse":
            return ("a" * 40 if "base" in arguments[-1] else "b" * 40).encode()
        if arguments[0] == "ls-tree":
            return (qa_run.REGISTRY + "\0").encode()
        if arguments[0] == "diff":
            return (qa_run.REGISTRY + "\0").encode()
        if arguments[0] == "show":
            invalid = arguments[1].startswith("a" * 40 if revision == "base" else "b" * 40)
            return b'{\n  "suites": INVALID\n}' if invalid else b'{"suites": []}'
        raise AssertionError(arguments)

    monkeypatch.setattr(qa_run, "_impact_git", fake_git)
    assert qa_run.classify_main(["--base", "base", "--head", "candidate"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert qa_run.REGISTRY in captured.err
    assert ("a" * 40 if revision == "base" else "b" * 40) in captured.err
    assert "Expecting value (line 2, column 13)" in captured.err


# Browser runner checks.

PASS_DOM = '<body data-browser-smoke="PASS"></body>'


def test_browser_uses_short_private_temp_without_changing_parent(monkeypatch, tmp_path):
    """Check browser uses short private temp without changing parent."""
    inherited = str(tmp_path / ("long-" + "a" * 160))
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, inherited)
    observed = []

    def run(command, **kwargs):
        """Inspect the private browser environment and return a passing process."""
        environment = kwargs["env"]
        temporary = Path(environment["TMPDIR"])
        observed.append(temporary)
        assert temporary.parent == Path("/tmp")
        assert len(str(temporary)) < 70
        assert temporary.is_dir()
        assert all(environment[key] == str(temporary) for key in ("TMPDIR", "TMP", "TEMP"))
        assert f"--user-data-dir={temporary}/profile" in command
        assert environment["PATH"] == os.environ["PATH"]
        assert kwargs["timeout"] == 60
        return subprocess.CompletedProcess(command, 0, PASS_DOM, "")

    monkeypatch.setattr(browser.subprocess, "run", run)
    browser._run_page("fake-chromium", tmp_path, "short-temp", "<html></html>")
    assert observed and all(not path.exists() for path in observed)
    assert all(os.environ[key] == inherited for key in ("TMPDIR", "TMP", "TEMP"))


def test_concurrent_browsers_do_not_share_temp_or_profile(monkeypatch, tmp_path):
    """Check concurrent browsers do not share temp or profile."""
    barrier = Barrier(2)
    observed = []

    def run(command, **kwargs):
        """Capture concurrent browser environments while both calls overlap."""
        temporary = Path(kwargs["env"]["TMPDIR"])
        observed.append(temporary)
        barrier.wait(timeout=5)
        assert temporary.is_dir()
        assert f"--user-data-dir={temporary}/profile" in command
        return subprocess.CompletedProcess(command, 0, PASS_DOM, "")

    monkeypatch.setattr(browser.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(browser._run_page, "fake-chromium", tmp_path,
                               f"parallel-{index}", "<html></html>") for index in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert len(set(observed)) == 2
    assert all(not path.exists() for path in observed)


@pytest.mark.parametrize("output_kind", ["text", "bytes", "missing"])
def test_browser_timeout_keeps_partial_diagnostics_and_cleans_temp(
    monkeypatch, tmp_path, output_kind
):
    """Check browser timeout keeps partial diagnostics and cleans temp."""
    observed = []
    stdout = "FIRST DOM OUTPUT\n" + "intermediate DOM\n" * 500 + "LAST DOM OUTPUT"
    stderr = "FIRST STARTUP ERROR\n" + "stack frame\n" * 500 + "LAST STACK FRAME"
    if output_kind == "bytes":
        stdout, stderr = stdout.encode(), stderr.encode()
    elif output_kind == "missing":
        stdout = stderr = None

    def run(command, **kwargs):
        """Simulate a browser timeout with retained output for diagnostics."""
        observed.append(Path(kwargs["env"]["TMPDIR"]))
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired(command, kwargs["timeout"],
                                        output=stdout, stderr=stderr)

    monkeypatch.setattr(browser.subprocess, "run", run)
    with pytest.raises(browser.BrowserInfrastructureError) as error:
        browser._run_page("fake-chromium", tmp_path, "timeout", "<html></html>")
    message = str(error.value)
    assert "page=timeout.html" in message and "timeout=60s" in message
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)
    if output_kind != "missing":
        for marker in ("FIRST DOM OUTPUT", "LAST DOM OUTPUT",
                       "FIRST STARTUP ERROR", "LAST STACK FRAME"):
            assert marker in message
        assert "output truncated" in message
    else:
        assert "stdout=; stderr=" in message
    assert len(observed) == 1  # A timeout is not retried or accepted as a pass.
    assert all(not path.exists() for path in observed)


def test_browser_manifest_allows_the_multi_page_startup_budget():
    """Check browser manifest allows the multi page startup budget."""
    manifest = browser.PROJECT_ROOT / "common/acs_test_framework_manifests/report-ui.yaml"
    configuration = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    cases = [case for suite in configuration["suites"] for case in suite["cases"]]
    case = next(case for case in cases if case["name"] ==
                "browser_executes_layout_navigation_collapse_filters_links_and_print_scope")
    assert case["timeout_sec"] == 180
    assert case["timeout_sec"] > browser.BROWSER_PAGE_TIMEOUT_SEC


def test_browser_crash_is_unverified_and_keeps_first_and_last_errors(monkeypatch, tmp_path):
    """Check browser crash is unverified and keeps first and last errors."""
    stderr = "FATAL: Socket path too long\n" + "stack trace\n" * 500 + "FINAL STACK FRAME"
    monkeypatch.setattr(browser.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, -6, "", stderr))
    with pytest.raises(browser.BrowserInfrastructureError) as error:
        browser._run_page("fake-chromium", tmp_path, "startup", "<html></html>")
    assert "FATAL: Socket path too long" in str(error.value)
    assert "FINAL STACK FRAME" in str(error.value)
    assert "output truncated" in str(error.value)
    assert "exit=-6" in str(error.value)


def test_page_assertion_failure_is_not_a_browser_startup_failure(monkeypatch, tmp_path):
    """Check page assertion failure is not a browser startup failure."""
    dom = '<pre id="browser-smoke-result">Expected Passed &amp; Failed filters</pre>'
    monkeypatch.setattr(browser.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, dom, ""))
    with pytest.raises(RuntimeError, match="Expected Passed & Failed filters") as error:
        browser._run_page("fake-chromium", tmp_path, "page", "<html></html>")
    assert not isinstance(error.value, browser.BrowserInfrastructureError)


@pytest.mark.parametrize("length", [0, 10, 2400])
def test_short_diagnostic_is_not_truncated(length):
    """Check short diagnostic is not truncated."""
    value = "x" * length
    assert browser._diagnostic_excerpt(value, 2400) == value
