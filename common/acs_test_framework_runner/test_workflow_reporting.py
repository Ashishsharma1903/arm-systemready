"""Exercise the real workflow summary command, including public failure annotations."""

import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
import yaml


@pytest.fixture
def summarize(tmp_path):
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/log-parser-qa.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["parser-qa"]["steps"]
    command = next(step["run"] for step in steps
                   if step["name"] == "Summarize checks and verify JUnit reports")
    reports = tmp_path / "common/reports"
    reports.mkdir(parents=True)
    summary = tmp_path / "summary.md"
    env = dict(os.environ, PATH=f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
               GITHUB_STEP_SUMMARY=str(summary), GUIDE_URL="https://example.invalid/guide",
               YAML_RESULT="success", E2E_RESULT="success", BROWSER_RESULT="success",
               ONBOARDING_RESULT="skipped", DEPENDENCIES_RESULT="success")

    def run(documents, **outcomes):
        for name, document in documents.items():
            (reports / name).write_text(document)
        result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command],
                                cwd=tmp_path, env=dict(env, **outcomes),
                                text=True, capture_output=True, timeout=10)
        return result, summary.read_text()

    return run


def report(problems=(), *, name="expected_compliance", message="assert 'Compliant' == 'Not Compliant'",
           names=None):
    suite = ET.Element("testsuite")
    for index, problem in enumerate(problems or (None,)):
        case = ET.SubElement(suite, "testcase", classname="parser_contract",
                             name=names[index] if names else name)
        if problem:
            ET.SubElement(case, problem, message=message)
    return ET.tostring(suite, encoding="unicode")


def test_passing_reports_have_no_error_annotations(summarize):
    result, summary = summarize({"yaml.xml": report(), "unit-e2e.xml": report()})
    assert result.returncode == 0, result.stderr
    assert "::error" not in result.stdout
    assert "| unit-e2e.xml | 1 | 0 | 0 | 0 |" in summary
    assert "[Troubleshooting guide](https://example.invalid/guide)" in summary


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
