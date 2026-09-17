"""Prove independent log expectations reject balanced but incorrect results."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def assert_raw():
    path = Path(__file__).with_name("test_end_to_end.py")
    spec = importlib.util.spec_from_file_location("qa_fixture_expectations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.assert_raw_expectations


def golden_result():
    data = {"suite_summary": {"Passed": 1, "Failed": 1}, "test_results": [{"testcases": [
        {"Test_case": "QA_01", "Test_case_description": "Known passing rule", "Test_result": "PASSED"},
        {"Test_case": "QA_02", "Test_case_description": "Known failing rule", "Test_result": "FAILED",
         "subtests": [{"sub_Test_Number": "QA_CHILD", "sub_Test_Description": "Known failing child",
                       "sub_test_result": "FAILED"}]},
    ]}]}
    expected = {
        "assertions": [{"path": ["suite_summary", "Passed"], "value": 1},
                       {"path": ["suite_summary", "Failed"], "value": 1}],
        "records": [
            {"path": ["test_results", 0, "testcases", 0], "fields": {
                "Test_case": "QA_01", "Test_case_description": "Known passing rule", "Test_result": "PASSED"}},
            {"path": ["test_results", 0, "testcases", 1], "fields": {
                "Test_case": "QA_02", "Test_case_description": "Known failing rule", "Test_result": "FAILED"}},
            {"path": ["test_results", 0, "testcases", 1, "subtests", 0], "fields": {
                "sub_Test_Number": "QA_CHILD", "sub_Test_Description": "Known failing child",
                "sub_test_result": "FAILED"}},
        ],
    }
    return data, expected


@pytest.mark.parametrize("mutation,diagnostic", [
    ("swap_status", "Test_result"), ("swap_identity", "Test_case"),
    ("change_description", "Test_case_description"), ("unknown_status", "Test_result"),
    ("omit_failed_rule", "result inventory differs"), ("omit_child", "result inventory differs"),
    ("omit_result_field", "result inventory differs"), ("append_result", "result inventory differs"),
    ("move_child", "result inventory differs"),
])
def test_balanced_corruption_cannot_pass_log_expectations(assert_raw, tmp_path, mutation, diagnostic):
    data, expected = golden_result()
    path = tmp_path / "bsa.json"
    path.write_text(json.dumps(data))
    assert_raw(path, expected)
    cases = data["test_results"][0]["testcases"]
    if mutation == "swap_status":
        cases[0]["Test_result"], cases[1]["Test_result"] = "FAILED", "PASSED"
    elif mutation == "swap_identity":
        cases[0]["Test_case"], cases[1]["Test_case"] = "QA_02", "QA_01"
    elif mutation == "change_description":
        cases[1]["Test_case_description"] = "Different rule"
    elif mutation == "unknown_status":
        cases[1]["Test_result"] = "UNKNOWN"
    elif mutation == "omit_failed_rule":
        cases.pop()
        data["suite_summary"]["Failed"] = 0
    elif mutation == "omit_child":
        cases[1]["subtests"].clear()
    elif mutation == "omit_result_field":
        del cases[0]["Test_result"]
    elif mutation == "append_result":
        cases.append(copy.deepcopy(cases[0]))
    else:
        cases[0]["subtests"] = cases[1].pop("subtests")
    path.write_text(json.dumps(data))
    with pytest.raises(AssertionError, match=diagnostic):
        assert_raw(path, expected)


def test_unlisted_active_status_is_not_ignored(assert_raw, tmp_path):
    result = {"PASSED": 1, "FAILED": 0, "FAILED_WITH_WAIVER": 0, "pass_reasons": ["QA pass"]}
    data = {"test_results": [{"subtests": [{"sub_test_result": result}]}]}
    expected = {"assertions": [], "records": [{
        "path": ["test_results", 0, "subtests", 0],
        "fields": {"sub_test_result": {"PASSED": 1, "FAILED": 0}},
    }]}
    path = tmp_path / "fwts.json"
    path.write_text(json.dumps(data))
    assert_raw(path, expected)
    result["FAILED_WITH_WAIVER"] = 1
    path.write_text(json.dumps(data))
    with pytest.raises(AssertionError, match="FAILED_WITH_WAIVER"):
        assert_raw(path, expected)
