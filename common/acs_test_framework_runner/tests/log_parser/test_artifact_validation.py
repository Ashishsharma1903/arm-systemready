"""Validator regressions for strict counts and renderer-specific row identities."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture(scope="module")
def validator():
    path = Path(__file__).resolve().parents[3] / "log_parser" / "validate.py"
    spec = importlib.util.spec_from_file_location("qa_artifact_validator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("invalid", [-1, 1.5, True, False, "1", None])
@pytest.mark.parametrize("method,key", [("_status_counts", "PASSED"), ("_summary_values", "total_passed")])
def test_invalid_counts_rejected(validator, invalid, method, key):
    with pytest.raises(validator.ArtifactValidationError, match="invalid .* count"):
        getattr(validator, method)({key: invalid})


def test_fwts_evidence_is_not_a_counter(validator):
    assert validator._status_counts({"PASSED": 2, "FAILED": 1, "pass_reasons": ["pass"],
                                     "fail_reasons": ["fail"]}) == {"passed": 2, "failed": 1}


@pytest.mark.parametrize("summary_name", ["test_case_summary", "test_suite_summary", "suite_summary"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_case_and_suite_summaries_checked(validator, summary_name, corrupt):
    data = {"subtests": [{"sub_test_result": "FAILED"}],
            summary_name: {"total_failed": 0 if corrupt else 1}}
    if corrupt:
        with pytest.raises(validator.ArtifactValidationError, match="raw (test|suite) summary mismatch"):
            validator._assert_raw_internal_counts(Path("sct.json"), data, "bbr/sct/json_to_html.py")
    else:
        validator._assert_raw_internal_counts(Path("sct.json"), data, "bbr/sct/json_to_html.py")


def test_list_children_are_not_counted_twice(validator):
    data = {"Test_result": "FAILED", "subtests": [{"sub_test_result": "FAILED"}]}
    assert validator._leaf_status_counts(data) == {"failed": 1}


@pytest.mark.parametrize("renderer,expected", [
    ("bbr/sct/json_to_html.py", "11111111-2222-3333-4444-555555555555"),
    ("bbr/tpm/json_to_html.py", "1"),
    ("standalone_tests/json_to_html.py", "11111111-2222-3333-4444-555555555555"),
])
def test_renderer_identity(validator, renderer, expected):
    data = {"subtests": [{"sub_Test_Number": "17",
                           "sub_Test_GUID": "11111111-2222-3333-4444-555555555555",
                           "sub_Test_Description": "", "sub_test_result": "PASSED"}]}
    assert validator._leaf_result_records(data, renderer) == [(expected, "", "passed")]


def test_nested_bsa_parent_and_child_rows(validator):
    document = validator._ReportHTMLParser()
    document.feed("""<table><tr><th>Test Case</th><th>Description</th><th>Result</th></tr>
      <tr><td><button>-</button>parent</td><td>root</td><td>FAILED</td></tr>
      <tr><td colspan="3"><table><tr><th>Subtest #</th><th>Description</th><th>Result</th></tr>
      <tr><td>child</td><td></td><td>FAILED</td></tr></table></td></tr></table>""")
    records, tables = validator._detail_records(document, Path("bsa.html"))
    assert records == [("parent", "root", "failed"), ("child", "", "failed")]
    assert tables == 2
    document.tables[0]["rows"][1].pop()
    with pytest.raises(validator.ArtifactValidationError, match="short result row"):
        validator._detail_records(document, Path("bsa.html"))


def test_raw_declared_total_matches_results(validator):
    data = {"test_results": [{"testcases": [{"Test_result": "PASSED"}]}],
            "suite_summary": {"Total Rules Run": 2, "Passed": 1}}
    with pytest.raises(validator.ArtifactValidationError, match="raw total mismatch"):
        validator._assert_raw_internal_counts(Path("bsa.json"), data, "bsa/json_to_html.py")


def test_sct_waivers_included_in_report_total(validator, tmp_path):
    source = tmp_path / "sct.json"
    data = {"test_results": [], "suite_summary": {
        "total_passed": 1, "total_failed": 0, "total_failed_with_waiver": 1,
        "total_aborted": 0, "total_skipped": 0, "total_warnings": 0, "total_ignored": 0,
    }}
    source.write_text(json.dumps(data))
    detail, summary = tmp_path / "detail.html", tmp_path / "summary.html"
    renderer = validator.SCRIPT_DIR / "bbr/sct/json_to_html.py"
    result = subprocess.run([sys.executable, str(renderer), str(source), str(detail), str(summary)],
                            capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    for path in (detail, summary):
        totals = validator._summary_maps(validator._read_html(path), path)
        assert len(totals) == 1
        assert totals[0]["total"] == 2
        assert totals[0]["failed"] == 0
        assert totals[0]["failed_with_waiver"] == 1


def test_bsa_waiver_updates_case_summary(validator, tmp_path):
    source, waiver = tmp_path / "bsa.json", tmp_path / "waiver.json"
    summary = {"Total Rules Run": 1, "Failed": 1, "Total_failed_with_waiver": 0}
    source.write_text(json.dumps({"suite_summary": summary, "test_results": [{
        "Test_suite": "QA group", "test_suite_summary": summary,
        "testcases": [{"Test_case": "QA_01", "Test_result": "FAILED",
                       "Test_case_summary": summary}],
    }]}))
    waiver.write_text(json.dumps({"Suites": [{"Suite": "BSA", "Reason": "QA waiver"}]}))
    result = subprocess.run([sys.executable, str(validator.SCRIPT_DIR / "apply_waivers.py"),
                             "BSA", str(source), str(waiver)], capture_output=True,
                            text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(source.read_text())
    case = data["test_results"][0]["testcases"][0]
    assert case["Test_result"] == "FAILED (WITH WAIVER)"
    assert case["Test_case_summary"]["Failed"] == 0
    assert case["Test_case_summary"]["Total_failed_with_waiver"] == 1
    validator._assert_raw_internal_counts(source, data, "bsa/json_to_html.py")


@pytest.mark.parametrize("kind", ["suite", "acs-summary"])
@pytest.mark.parametrize("target_state", ["present", "missing", "empty"])
def test_report_file_links_require_nonempty_targets(validator, tmp_path, kind, target_state):
    report = tmp_path / "report.html"
    report.write_text(f'<body data-acs-report-kind="{kind}"><a href="target.html">Open</a></body>')
    target = tmp_path / "target.html"
    if target_state != "missing":
        target.write_text('<body id="target">Report</body>' if target_state == "present" else "")
    document = validator._read_html(report)
    if target_state == "present":
        validator._assert_local_links(report, document, tmp_path, {})
    else:
        with pytest.raises(validator.ArtifactValidationError, match="local link target is missing or empty.*target.html"):
            validator._assert_local_links(report, document, tmp_path, {})


@pytest.mark.parametrize("href", ["#section", "target.html#section", "target%20report.html#section"])
@pytest.mark.parametrize("anchor_state", ["present", "missing", "duplicate"])
def test_report_link_fragments(validator, tmp_path, href, anchor_state):
    report = tmp_path / "report.html"
    anchors = '<div id="section">Result</div>' * {"present": 1, "missing": 0, "duplicate": 2}[anchor_state]
    report.write_text('<body data-acs-report-kind="suite">'
                      f'<a href="{href}">Open</a>{anchors if href.startswith("#") else ""}</body>')
    if not href.startswith("#"):
        target_name = href.split("#")[0].replace("%20", " ")
        (tmp_path / target_name).write_text(f'<body class="report">{anchors}</body>')
    if anchor_state == "present":
        validator._assert_local_links(report, validator._read_html(report), tmp_path, {})
    else:
        diagnostic = "duplicate element IDs" if anchor_state == "duplicate" else "local link fragment does not exist"
        with pytest.raises(validator.ArtifactValidationError, match=diagnostic):
            validator._assert_local_links(report, validator._read_html(report), tmp_path, {})


@pytest.mark.parametrize("suite,kind,link_class,allowed", [
    ("sbmr", "suite", "report-card-btn", True),
    ("sbmr", "suite", "other report-card-btn", True),
    ("bsa", "suite", "report-card-btn", False),
    ("sbmr", "acs-summary", "report-card-btn", False),
    ("sbmr", "suite", "", False),
    ("sbmr", "suite", "not-report-card-btn", False),
])
@pytest.mark.parametrize("href", ["../source.html#section", "source-alias.html#section"])
def test_only_sbmr_source_report_links_can_leave_html_directory(
        validator, tmp_path, suite, kind, link_class, allowed, href):
    html_dir = tmp_path / "html"
    html_dir.mkdir()
    source = tmp_path / "source.html"
    source.write_text('<body class="robot"><div id="section">Source report</div></body>')
    (html_dir / "source-alias.html").symlink_to(source)
    report = html_dir / "detail.html"
    report.write_text(f'<body data-acs-suite="{suite}" data-acs-report-kind="{kind}">'
                      f'<a class="{link_class}" href="{href}">Open report.html</a></body>')
    document = validator._read_html(report)
    if allowed:
        validator._assert_local_links(report, document, html_dir, {})
    else:
        with pytest.raises(validator.ArtifactValidationError, match="local link escapes report directory"):
            validator._assert_local_links(report, document, html_dir, {})


@pytest.mark.parametrize("target_state", ["missing", "empty", "missing_fragment"])
def test_sbmr_source_report_links_still_validate_the_target(validator, tmp_path, target_state):
    html_dir = tmp_path / "html"
    html_dir.mkdir()
    report = html_dir / "detail.html"
    report.write_text('<body data-acs-suite="sbmr" data-acs-report-kind="suite">'
                      '<a class="report-card-btn" href="../source.html#section">Open report.html</a></body>')
    if target_state != "missing":
        (tmp_path / "source.html").write_text('<body class="robot">Source report</body>'
                                              if target_state == "missing_fragment" else "")
    diagnostic = "local link fragment does not exist" if target_state == "missing_fragment" else "local link target is missing or empty"
    with pytest.raises(validator.ArtifactValidationError, match=diagnostic):
        validator._assert_local_links(report, validator._read_html(report), html_dir, {})


def test_generated_sbmr_source_report_link(validator, tmp_path):
    html_dir = tmp_path / "html"
    html_dir.mkdir()
    source_json = tmp_path / "sbmr_ib.json"
    source_json.write_text(json.dumps({"test_results": [], "suite_summary": {"total_passed": 0}}))
    source_report = tmp_path / "report.html"
    source_report.write_text('<body class="robot">Original Robot report</body>')
    detail, summary = html_dir / "detail.html", html_dir / "summary.html"
    result = subprocess.run([
        sys.executable, str(validator.SCRIPT_DIR / "sbmr/json_to_html.py"),
        str(source_json), str(detail), str(summary), str(source_report),
    ], capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    document = validator._read_html(detail)
    assert any(link["href"] == "../report.html" for link in document.links)
    validator._assert_local_links(detail, document, html_dir, {})
    source_report.unlink()
    with pytest.raises(validator.ArtifactValidationError, match="local link target is missing or empty.*report.html"):
        validator._assert_local_links(detail, document, html_dir, {})
