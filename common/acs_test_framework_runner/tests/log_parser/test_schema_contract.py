"""Schema boundaries must reject corrupt counters and compliance labels."""

import copy
import importlib.util
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest


PARSER = Path(__file__).resolve().parents[3] / "log_parser"
SCHEMA = json.loads((PARSER / "acs-results-schema.json").read_text())


def validator(definition):
    return Draft202012Validator({
        "$ref": f"#/definitions/{definition}", "definitions": SCHEMA["definitions"]
    })


@pytest.mark.parametrize("count", [-1, -999, 0.5, "0", "1", True, False, None, [], {}])
def test_counters_reject_non_counts(count):
    assert not validator("non_negative_int").is_valid(count)


@pytest.mark.parametrize("count", [0, 1, 2, 999, 1000000])
def test_counters_accept_nonnegative_integers(count):
    assert validator("non_negative_int").is_valid(count)


@pytest.mark.parametrize("status", [
    "Compliant", "Compliant with waivers", "Compliant with waivers: Waivers 2",
    "Not Run", "Not run", "Not Compliant", "Not compliant",
    "Not Compliant: not run", "Not Compliant: Failed 3",
    "Not Compliant : Mandatory - (failed: BSA)",
])
def test_supported_compliance_labels(status):
    assert validator("compliance_status").is_valid(status)


@pytest.mark.parametrize("status", [
    "", "PASS", "FAIL", "Unknown", "Complient", "Not Compliant: Failed 0",
    "Not Compliant: Failed -1", "Compliant with waivers: Waivers 0", 0, None,
])
def test_invalid_compliance_labels(status):
    assert not validator("compliance_status").is_valid(status)


def test_compliance_label_constraints_apply_to_summary_fields():
    fields = SCHEMA["definitions"]["acs_results_summary"]["allOf"][0]["then"]["required"]
    summary = dict.fromkeys(fields, "Compliant")
    summary.update({"Band": "SystemReady Devicetree band", "Date": "2026-01-01",
                    "BBSR compliance results": "Compliant",
                    "Overall Compliance Result": "Compliant"})
    check = validator("acs_results_summary")
    check.validate(summary)
    for field in [*fields, "BBSR compliance results", "Overall Compliance Result"]:
        corrupt = {**summary, field: "Not Compliant: Failed -1"}
        assert not check.is_valid(corrupt), field


def os_result():
    return {"Test_suite": "os_test", "Test_suite_description": "os test checks",
            "Test_case": "os_testing", "Test_case_description": "OS checks",
            "subtests": [{"sub_Test_Number": "1", "sub_Test_Description": "OS present",
                          "sub_test_result": {"PASSED": 1}}],
            "test_suite_summary": {"total_passed": 1, "total_failed": 0,
                                   "total_aborted": 0, "total_skipped": 0,
                                   "total_warnings": 0, "total_failed_with_waivers": 0}}


def test_default_sr_os_data_needs_no_invented_category_metadata():
    data = os_result()
    check = validator("os_tests_test_result")
    check.validate(data)
    for field in ("subtests", "test_suite_summary", "Test_case"):
        corrupt = copy.deepcopy(data)
        del corrupt[field]
        assert not check.is_valid(corrupt), field
    corrupt = copy.deepcopy(data)
    corrupt["test_suite_summary"]["total_failed"] = -1
    assert not check.is_valid(corrupt)


@pytest.mark.parametrize("field", ["Main Readiness Grouping", "SRS scope", "Waivable"])
def test_dt_os_classification_remains_required(field):
    data = {**os_result(), "Test_suite": "Network", "Test_case": "ethtool_test",
            "Main Readiness Grouping": "Network readiness", "SRS scope": "Mandatory",
            "Waivable": "no"}
    check = validator("os_tests_test_result")
    check.validate(data)
    del data[field]
    assert not check.is_valid(data)


@pytest.mark.parametrize("suite,case", [
    ("Network", "ethtool_test_linux-rhel"),
    ("Boot sources", "read_write_check_blk_devices_linux-sles"),
])
def test_only_known_sr_supplemental_groups_allow_missing_category_metadata(suite, case):
    data = {**os_result(), "Test_suite": suite, "Test_case": case, "SRS scope": "Recommended"}
    check = validator("os_tests_test_result")
    check.validate(data)
    for mutation in ({"SRS scope": "Mandatory"}, {"Test_case": "unregistered-case"},
                     {"Test_suite": "unregistered-suite"}):
        assert not check.is_valid({**data, **mutation}), mutation
    del data["SRS scope"]
    assert not check.is_valid(data)


@pytest.mark.parametrize("definition", ["subtest_base", "bsa_subtest"])
@pytest.mark.parametrize("reason", ["QA approved waiver", 1, True, [], {}, None])
def test_subtest_waiver_reason_has_a_strict_type(definition, reason):
    data = {"sub_Test_Number": "1", "sub_Test_Description": "QA rule",
            "sub_test_result": "FAILED (WITH WAIVER)", "waiver_reason": reason}
    assert validator(definition).is_valid(data) == isinstance(reason, str)


def test_sr_category_description_optional_but_classification_required():
    category = {"Main Readiness Grouping": "boot", "SRS scope": "core", "Waivable": "no"}
    check = validator("test_category_base")
    check.validate(category)
    for field in category:
        assert not check.is_valid({k: v for k, v in category.items() if k != field})


def test_tpm_raw_enrichment_matches_merger_category_alias(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PARSER))
    spec = importlib.util.spec_from_file_location("qa_enrichment", PARSER / "enrich_suite_json.py")
    enrichment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(enrichment)
    category = json.loads((PARSER / "test_categoryDT.json").read_text())
    rows = enrichment._category_index(category)
    path = tmp_path / "bbsr_tpm.json"
    path.write_text(json.dumps({"test_results": [{"Test_suite": "BBSR-TPM"}]}))
    assert enrichment.enrich_file(path, "BBSR-TPM", rows) == (1, 0)
    result = json.loads(path.read_text())["test_results"][0]
    expected = enrichment._metadata_from_row(rows["bbsr-standalone"]["measured boot log"])
    assert {key: result[key] for key in expected} == expected
