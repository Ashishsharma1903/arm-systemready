"""Validator regressions for strict counts and renderer-specific row identities."""

import importlib.util
from html import escape
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture(scope="module")
def validator():
    path = Path(__file__).resolve().parents[2] / "parser_artifacts.py"
    spec = importlib.util.spec_from_file_location("qa_artifact_validator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("text,diagnostic", [
    ('{"value": 1, "value": 2}', "duplicate JSON key"),
    ('{"nested": {"value": 1, "value": 2}}', "duplicate JSON key"),
    ('{"value": NaN}', "non-standard JSON value"),
    ('{"value": Infinity}', "non-standard JSON value"),
    ('{"value": -Infinity}', "non-standard JSON value"),
])
def test_strict_json_decoding_is_opt_in(validator, tmp_path, text, diagnostic):
    path = tmp_path / "input.json"
    path.write_text(text)
    assert json.dumps(validator._load_json(path)) == json.dumps(json.loads(text))
    with pytest.raises(ValueError, match=diagnostic):
        validator._load_json(path, strict=True)


def test_strict_json_decoding_preserves_valid_data(validator, tmp_path):
    data = {"value": [1, 0.5, None, True, {"result": "PASSED"}]}
    path = tmp_path / "input.json"
    path.write_text(json.dumps(data))
    assert validator._load_json(path, strict=True) == validator._load_json(path) == data


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("corrupt_target", ["json", "schema"])
def test_raw_validation_forwards_strict_decoding(validator, tmp_path, strict, corrupt_target):
    path, schema = tmp_path / "input.json", tmp_path / "schema.json"
    path.write_text('{}')
    schema.write_text('{}')
    (path if corrupt_target == "json" else schema).write_text('{"title": "old", "title": "new"}')
    suite = {"canonical": "QA", "schema": schema, "schema_ref": str(schema), "schema_fragment": ""}
    result = validator._validate_one(path, suite, strict=strict)
    if strict:
        assert result["fatal"][0] == ("JSON_FILE" if corrupt_target == "json" else "SCHEMA_FILE")
        assert "duplicate JSON key" in result["fatal"][1]
    else:
        assert result["errors"] == []


def test_merged_validation_retains_legacy_json_loading(validator, tmp_path):
    path, schema = tmp_path / "input.json", tmp_path / "schema.json"
    path.write_text('{"value": 1, "value": NaN}')
    schema.write_text('{}')
    assert validator._run_merged_validation(path, schema, max_paths=1) == 0


@pytest.mark.parametrize("invalid", [-1, 1.5, True, False, "1", None])
@pytest.mark.parametrize("method,key", [("_status_counts", "PASSED"), ("_summary_values", "total_passed")])
def test_invalid_counts_rejected(validator, invalid, method, key):
    with pytest.raises(validator.ArtifactValidationError, match="invalid .* count"):
        getattr(validator, method)({key: invalid})


def test_fwts_evidence_is_not_a_counter(validator):
    assert validator._status_counts({"PASSED": 2, "FAILED": 1, "pass_reasons": ["pass"],
                                     "fail_reasons": ["fail"]}) == {"passed": 2, "failed": 1}


def test_merger_may_add_documented_metadata_without_altering_raw_results(validator):
    raw = {"test_results": [{"Test_case": "QA", "Test_result": "FAILED"}]}
    merged = {"test_results": [{"Test_case": "QA", "Test_result": "FAILED", "SRS scope": "Recommended"}]}
    assert validator._raw_preserved(raw, merged)
    merged["test_results"][0]["Test_result"] = "PASSED"
    assert not validator._raw_preserved(raw, merged)


def test_merger_parity_does_not_allow_arbitrary_fields_or_metadata_overrides(validator):
    assert not validator._raw_preserved({"count": 1}, {"count": True})
    assert not validator._raw_preserved({"Test_case": "QA"}, {"Test_case": "QA", "invented": "extra"})
    assert not validator._raw_preserved({"SRS scope": "Mandatory"}, {"SRS scope": "Recommended"})


@pytest.mark.parametrize("provided", [False, True])
def test_missing_suite_info_requires_an_explicit_run_category(validator, tmp_path, provided):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"type": "object", "required": ["Test_suite_info", "SRS scope"],
        "properties": {"Test_suite_info": {"type": "array"}, "SRS scope": {"type": "string"}}}))
    check = validator._schema_for_run(path, category_provided=provided)
    assert bool(list(check.iter_errors({"SRS scope": "Mandatory"}))) is provided
    assert list(check.iter_errors({}))  # Never relax the remaining metadata.
    assert list(check.iter_errors({"SRS scope": "Mandatory", "Test_suite_info": 0}))


def test_selected_schema_keeps_selected_requirements_but_not_unselected_ones(validator, tmp_path):
    path = tmp_path / "schema.json"
    summary = {"type": "object", "required": ["Overall Compliance Result", "BBSR compliance results",
        "Suite_Name: Mandatory  : BSA_compliance", "Suite_Name: Mandatory  : SBSA_compliance"]}
    path.write_text(json.dumps({"$ref": "#/definitions/acs_results_summary", "definitions": {"acs_results_summary": summary}}))
    registry = [{"canonical": "BSA", "aliases": ["BSA"]}, {"canonical": "SBSA", "aliases": ["SBSA"]}]
    check = validator._schema_for_run(path, selected={"BSA"}, registry=registry)
    value = {"Overall Compliance Result": "Not Compliant", "Suite_Name: Mandatory  : BSA_compliance": "Not Compliant"}
    assert not list(check.iter_errors(value))
    value.pop("Suite_Name: Mandatory  : BSA_compliance")
    assert list(check.iter_errors(value))


@pytest.mark.parametrize("prefix", ["EBBR", "SBBR"])
@pytest.mark.parametrize("suite", ["FWTS", "SCT"])
def test_selected_schema_understands_band_specific_display_labels(validator, tmp_path, prefix, suite):
    path = tmp_path / "schema.json"
    key = f"Suite_Name: Mandatory  : {prefix}-{suite}_compliance"
    summary = {"type": "object", "required": ["Overall Compliance Result", key],
               "properties": {key: {"type": "string"}}}
    path.write_text(json.dumps({"$ref": "#/definitions/acs_results_summary",
        "definitions": {"acs_results_summary": summary}}))
    registry = [{"canonical": name, "aliases": [name]} for name in ("FWTS", "SCT", "BSA")]
    value = {"Overall Compliance Result": "Compliant"}
    assert not list(validator._schema_for_run(path, selected={"BSA"}, registry=registry).iter_errors(value))
    assert list(validator._schema_for_run(path, selected={suite}, registry=registry).iter_errors(value))
    value[key] = 0
    assert list(validator._schema_for_run(path, selected={"BSA"}, registry=registry).iter_errors(value))


def test_combined_sbmr_channel_identities_are_preserved(validator):
    document = validator._ReportHTMLParser()
    document.feed('<div data-acs-sbmr-channel="SBMR-IB">IB</div>'
                  '<div data-acs-sbmr-channel="SBMR-OOB">OOB</div>')
    assert document.sbmr_channels == {"SBMR-IB": 1, "SBMR-OOB": 1}


def test_absent_zero_summary_counters_are_equivalent(validator):
    validator._assert_summary({"failed": 1}, {"failed": 1, "skipped": 0}, Path("report.html"))
    with pytest.raises(validator.ArtifactValidationError, match="summary count mismatch"):
        validator._assert_summary({"failed": 1}, {"failed": 1, "skipped": 1}, Path("report.html"))


@pytest.mark.parametrize("value", [None, {}, "bad", 0])
def test_standalone_invalid_test_results_has_clear_error(validator, value):
    with pytest.raises(validator.ArtifactValidationError, match="test_results must be a list"):
        validator._standalone_summary({"test_results": value}, "standalone_tests/json_to_html.py")


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


@pytest.mark.qa_context(suite="SCT", mode="mode-independent", stage="json-to-html")
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


@pytest.mark.qa_context(suite="BSA", mode="mode-independent", stage="waiver")
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


def compliance_document(validator, rows):
    document = validator._ReportHTMLParser()
    document.feed("<table>" + "".join(
        "<tr>" + (f"<th>{escape(label)}</th>" if label else "")
        + f"<td>{escape(value)}</td></tr>" for label, value in rows
    ) + "</table>")
    return document


@pytest.fixture
def compliance_report():
    summary = {
        "Overall Compliance Result": "Not Compliant : Mandatory - (not run: BSA; failed: FWTS) : Recommended - (failed: POST_SCRIPT)",
        "BBSR compliance results": "Not Compliant : Mandatory - (failed: BBSR-TPM)",
        "SCMI compliance results": "Not Compliant : Mandatory - (SCMI)",
    }
    rows = [
        ("SRS requirements compliance results", "Not Compliant"),
        ("", "Mandatory: not run: BSA; failed: FWTS"),
        ("", "Recommended: failed: POST_SCRIPT"),
        ("BBSR compliance results", "Not Compliant"),
        ("", "Mandatory: failed: BBSR-TPM"),
        ("SCMI compliance results", "Not Compliant"),
        ("", "Mandatory: failed: SCMI"),
    ]
    return {"Suite_Name: acs_info": {"ACS Results Summary": summary}}, rows


def test_compliance_details_match_merged_report(validator, compliance_report):
    merged, rows = compliance_report
    rows[1] = ("", "Mandatory:  not run: BSA ;  failed: FWTS")
    validator._assert_compliance_parity(merged, compliance_document(validator, rows), Path("acs.html"))


def test_overall_html_verdict_cannot_turn_green_with_unchanged_failed_json(validator, compliance_report):
    merged, rows = compliance_report
    rows[0] = (rows[0][0], "Compliant")
    with pytest.raises(validator.ArtifactValidationError, match="compliance status mismatch"):
        validator._assert_compliance_parity(merged, compliance_document(validator, rows), Path("acs.html"))


@pytest.mark.parametrize("display", [None, "Unknown", "Compliant", "Not Compliant", "Not Applicable", "Unknown: passed"])
def test_selected_html_bbsr_default_never_claims_an_unsupported_result(validator, display):
    merged = {"Suite_Name: acs_info": {"ACS Results Summary": {"Overall Compliance Result": "Compliant"}}}
    rows = [("SRS requirements compliance results", "Compliant")]
    if display is not None:
        rows.append(("BBSR compliance results", display))
    document = compliance_document(validator, rows)
    if display not in (None, "Unknown"):
        with pytest.raises(validator.ArtifactValidationError, match="compliance status mismatch"):
            validator._assert_compliance_parity(merged, document, Path("acs.html"), selected={"PCIE-OPTION-ROM-ARCH-AUDIT"})
    else:
        validator._assert_compliance_parity(merged, document, Path("acs.html"), selected={"PCIE-OPTION-ROM-ARCH-AUDIT"})


@pytest.mark.parametrize("selected", [None, {"BBSR-SCT"}])
def test_unknown_bbsr_without_applicable_selection_evidence_is_not_assumed_valid(validator, selected):
    merged = {"Suite_Name: acs_info": {"ACS Results Summary": {"Overall Compliance Result": "Compliant"}}}
    rows = [("SRS requirements compliance results", "Compliant"), ("BBSR compliance results", "Unknown")]
    with pytest.raises(validator.ArtifactValidationError, match="unexpected compliance row"):
        validator._assert_compliance_parity(merged, compliance_document(validator, rows), Path("acs.html"), selected=selected)


def test_selected_bbsr_placeholder_cannot_hide_present_bbsr_raw_results(validator):
    merged = {"Suite_Name: acs_info": {"ACS Results Summary": {"Overall Compliance Result": "Compliant"}},
              "Suite_Name: BBSR-SCT": {"test_results": []}}
    rows = [("SRS requirements compliance results", "Compliant"), ("BBSR compliance results", "Unknown")]
    with pytest.raises(validator.ArtifactValidationError, match="unexpected compliance row"):
        validator._assert_compliance_parity(merged, compliance_document(validator, rows), Path("acs.html"), selected={"BSA"})


def test_selected_bbsr_placeholder_cannot_be_repeated_or_add_unsupported_details(validator):
    merged = {"Suite_Name: acs_info": {"ACS Results Summary": {"Overall Compliance Result": "Compliant"}}}
    rows = [("SRS requirements compliance results", "Compliant"), ("BBSR compliance results", "Unknown")]
    for extra in [("BBSR compliance results", "Unknown"), ("", "Mandatory: failed: BBSR-SCT")]:
        with pytest.raises(validator.ArtifactValidationError, match="compliance (status|detail) mismatch"):
            validator._assert_compliance_parity(merged, compliance_document(validator, rows + [extra]), Path("acs.html"), selected={"BSA"})


@pytest.mark.parametrize("mutation", [
    "wrong_failed_suite", "wrong_not_run_suite", "missing_suite", "extra_suite", "duplicate_suite",
    "wrong_category", "wrong_reason", "missing_detail", "extra_detail", "duplicate_detail",
    "wrong_bbsr_suite", "wrong_scmi_suite", "missing_status", "duplicate_status", "unexpected_status",
])
def test_compliance_detail_corruption_rejected(validator, compliance_report, mutation):
    merged, rows = compliance_report
    if mutation == "wrong_failed_suite":
        rows[1] = ("", "Mandatory: not run: BSA; failed: INVENTED_SUITE")
    elif mutation == "wrong_not_run_suite":
        rows[1] = ("", "Mandatory: not run: SCT; failed: FWTS")
    elif mutation == "missing_suite":
        rows[1] = ("", "Mandatory: failed: FWTS")
    elif mutation == "extra_suite":
        rows[1] = ("", "Mandatory: not run: BSA, SCT; failed: FWTS")
    elif mutation == "duplicate_suite":
        rows[1] = ("", "Mandatory: not run: BSA, BSA; failed: FWTS")
    elif mutation == "wrong_category":
        rows[2] = ("", "Mandatory: failed: POST_SCRIPT")
    elif mutation == "wrong_reason":
        rows[2] = ("", "Recommended: not run: POST_SCRIPT")
    elif mutation == "missing_detail":
        del rows[2]
    elif mutation == "extra_detail":
        rows.insert(3, ("", "Recommended: failed: INVENTED_SUITE"))
    elif mutation == "duplicate_detail":
        rows.insert(3, rows[2])
    elif mutation == "wrong_bbsr_suite":
        rows[4] = ("", "Mandatory: failed: BBSR-SCT")
    elif mutation == "wrong_scmi_suite":
        rows[6] = ("", "Mandatory: failed: BSA")
    elif mutation == "missing_status":
        del rows[5:]
    elif mutation == "duplicate_status":
        rows.extend(rows[5:])
    elif mutation == "unexpected_status":
        rows.append(("Unexpected compliance results", "Compliant"))
    with pytest.raises(validator.ArtifactValidationError, match="compliance (detail|status|row)"):
        validator._assert_compliance_parity(merged, compliance_document(validator, rows), Path("acs.html"))


@pytest.mark.parametrize("status", ["Compliant", "Compliant with waivers", "Not Run",
                                    "Not Compliant (Not Run)", "Not Run (no input logs)"])
@pytest.mark.parametrize("corruption", ["none", "extra_text", "missing_qualifier"])
def test_compliance_primary_text_is_exact(validator, status, corruption):
    summary = {"Overall Compliance Result": status, "BBSR compliance results": status}
    rows = [("SRS requirements compliance results", status), ("BBSR compliance results", status)]
    displayed = status
    if corruption == "extra_text":
        displayed += " arbitrary wrong text"
    elif corruption == "missing_qualifier":
        displayed = status.split("(", 1)[0].strip()
    rows[0] = (rows[0][0], displayed)
    merged = {"Suite_Name: acs_info": {"ACS Results Summary": summary}}
    document = compliance_document(validator, rows)
    if displayed != status:
        with pytest.raises(validator.ArtifactValidationError, match="compliance status mismatch"):
            validator._assert_compliance_parity(merged, document, Path("acs.html"))
    else:
        validator._assert_compliance_parity(merged, document, Path("acs.html"))
