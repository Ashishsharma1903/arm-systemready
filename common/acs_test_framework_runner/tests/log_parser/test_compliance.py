"""Independent failure-count and compliance policy checks for every result shape."""

import copy
import importlib.util
import itertools
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


PARSER_DIR = Path(__file__).resolve().parents[3] / "log_parser"
CASES = yaml.safe_load(Path(__file__).with_name("compliance_cases.yaml").read_text())
SUITE_MODES = [
    pytest.param(case, mode, requirement, id=f"{case['suite']}-{mode}")
    for case in CASES["suites"]
    for mode, requirement in case["modes"].items()
]
SHAPES = ("testcases", "subtests", "buckets", "nested")
COUNT_IDS = [f"failed-{failed}_waived-{waived}" for failed, waived in CASES["counts"]]
REQUIREMENT_LABELS = {
    "M": "Mandatory", "R": "Recommended", "CM": "Conditional-Mandatory", "EM": "Extension"
}


@pytest.fixture(scope="module")
def merger():
    spec = importlib.util.spec_from_file_location("qa_compliance_merger", PARSER_DIR / "merge_jsons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result_group(shape, failed, waived, scope=None, name="qa_policy_probe"):
    """Build counts from known leaves; expected results never call production counters."""
    statuses = ["FAILED"] * failed + ["FAILED (WITH WAIVER)"] * waived
    statuses += ["PASSED", "SKIPPED", "ABORTED", "WARNING"]
    group = {"Test_suite": name}
    if scope is not None:
        group["SRS scope"] = scope
    if shape == "testcases":
        group["testcases"] = [
            {"Test_case": f"case-{number}", "Test_result": status}
            for number, status in enumerate(statuses)
        ]
    else:
        subtests = [
            {"sub_test_name": f"case-{number}", "sub_test_result": status}
            for number, status in enumerate(statuses)
        ]
        if shape == "buckets":
            subtests = [{"sub_test_name": "aggregate", "sub_test_result": {
                "FAILED": failed, "FAILED_WITH_WAIVER": waived,
                "PASSED": 1, "SKIPPED": 1, "ABORTED": 1, "WARNING": 1,
            }}]
        if shape == "nested":
            group["Test_cases"] = [{"Test_case": "nested-case", "subtests": subtests}]
        else:
            group["subtests"] = subtests
    return group


def run_merge(merger, monkeypatch, tmp_path, inputs, mode, requirements, selected=False,
              categories=None):
    # Isolate the policy under test from unrelated missing-suite reports.
    monkeypatch.setattr(merger, "DT_OR_SR_MODE", mode)
    monkeypatch.setattr(merger, f"{mode}_SRS_SCOPE_TABLE", requirements)
    monkeypatch.setattr(merger, "_REQUIREMENT_MAP", {})
    monkeypatch.setattr(merger, "_SELECTED_SUITE_FILTER", set(dict(requirements)) if selected else None)
    monkeypatch.setattr(merger, "test_cat_dict", categories or {})
    files = []
    for name, data in inputs.items():
        path = tmp_path / name
        path.write_text(json.dumps(data))
        files.append(str(path))
    output = tmp_path / "merged.json"
    merger.merge_json_files(files, str(output))
    merged = json.loads(output.read_text())
    return merged, merged["Suite_Name: acs_info"]["ACS Results Summary"]


def suite_result(summary, suite, mode, requirement):
    if suite in ("FWTS", "SCT"):
        suite = f"{'EBBR' if mode == 'DT' else 'SBBR'}-{suite}"
    return summary[f"Suite_Name: {REQUIREMENT_LABELS[requirement]}  : {suite}_compliance"]


def expected_suite_status(failed, waived):
    if failed:
        return f"Not Compliant: Failed {failed}"
    if waived:
        return f"Compliant with waivers: Waivers {waived}"
    return "Compliant"


def expected_overall_status(failed, waived, requirement, selected):
    if failed and (requirement in ("M", "CM") or (selected and requirement == "R")):
        return "Not Compliant"
    if waived and requirement in ("M", "CM"):
        return "Compliant with waivers"
    return "Compliant"


def test_policy_covers_every_registered_requirement():
    registry = json.loads((PARSER_DIR / "suite_registry.json").read_text())
    expected = {(suite["requirement_key"], mode) for suite in registry["suites"]
                for mode in suite.get("requirements", {})}
    covered = {(case["suite"], mode) for case in CASES["suites"] for mode in case["modes"]}
    assert covered == expected, (
        f"Missing compliance policy scenarios: {sorted(expected - covered)}; "
        f"unregistered scenarios: {sorted(covered - expected)}"
    )


@pytest.mark.parametrize("case,mode,requirement", SUITE_MODES)
def test_registry_policy_matches_expected_contract(merger, case, mode, requirement):
    assert dict(merger.requirement_table(mode))[case["suite"]] == case.get("when_missing", requirement)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("scope", CASES["scopes"])
@pytest.mark.parametrize("failed,waived", CASES["counts"], ids=COUNT_IDS)
@pytest.mark.parametrize("wrapped", [False, True], ids=["list", "object"])
@pytest.mark.parametrize("skip_recommended", [False, True], ids=["all-results", "sr-os-policy"])
def test_failure_counts(merger, shape, scope, failed, waived, wrapped, skip_recommended):
    groups = [result_group(shape, failed, waived, scope)]
    data = {"test_results": groups} if wrapped else groups
    before = copy.deepcopy(data)
    expected = (failed, waived)
    if skip_recommended and str(scope).strip().lower() == "recommended":
        expected = (0, 0)
    assert merger.count_fails_in_json(data, skip_recommended=skip_recommended) == expected
    assert data == before, "Compliance filtering must not remove visible test results"


@pytest.mark.parametrize("case,mode,requirement", SUITE_MODES)
@pytest.mark.parametrize("scope", CASES["scopes"])
@pytest.mark.parametrize("failed,waived", CASES["counts"], ids=COUNT_IDS)
@pytest.mark.parametrize("selected", [False, True], ids=["full-run", "selected-run"])
def test_suite_and_overall_policy(merger, monkeypatch, tmp_path, case, mode, requirement,
                                  scope, failed, waived, selected):
    data = {"test_results": [result_group(case["shape"], failed, waived, scope)]}
    merged, summary = run_merge(
        merger, monkeypatch, tmp_path, {case["file"]: data}, mode,
        [(case["suite"], requirement)], selected,
    )
    ignored = mode == "SR" and case["suite"] == "OS_TEST" and str(scope).strip().lower() == "recommended"
    expected_failed, expected_waived = (0, 0) if ignored else (failed, waived)
    expected_suite = expected_suite_status(expected_failed, expected_waived)
    assert suite_result(summary, case["suite"], mode, requirement) == expected_suite

    expected_overall = expected_overall_status(expected_failed, expected_waived, requirement, selected)
    assert summary["Overall Compliance Result"].split(" : ", 1)[0] == expected_overall
    # Recommended SR OS failures remain visible even when excluded from compliance.
    groups = [group for key, value in merged.items() if key != "Suite_Name: acs_info"
              for group in (value.get("test_results", []) if isinstance(value, dict) else value)]
    assert groups == data["test_results"]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("mandatory,recommended", list(itertools.product(CASES["counts"], repeat=2)))
def test_sr_os_mixed_scope_counts(merger, monkeypatch, tmp_path, shape, mandatory, recommended):
    groups = [result_group(shape, *mandatory, "Mandatory"),
              result_group(shape, *recommended, "Recommended")]
    _, summary = run_merge(merger, monkeypatch, tmp_path, {"os_test.json": {"test_results": groups}},
                           "SR", [("OS_TEST", "M")])
    assert suite_result(summary, "OS_TEST", "SR", "M") == expected_suite_status(*mandatory)


@pytest.mark.parametrize("case,mode,requirement", SUITE_MODES)
@pytest.mark.parametrize("failed,waived", CASES["counts"], ids=COUNT_IDS)
def test_selected_suite_cli(tmp_path, case, mode, requirement, failed, waived):
    source = tmp_path / case["file"]
    source.write_text(json.dumps({"test_results": [
        result_group(case["shape"], failed, waived, "Recommended")
    ]}))
    output = tmp_path / "merged.json"
    result = subprocess.run(
        [sys.executable, str(PARSER_DIR / "merge_jsons.py"), "--mode", mode,
         "--selected-suites", case["suite"], str(output), str(source)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(output.read_text())["Suite_Name: acs_info"]["ACS Results Summary"]
    counts = (0, 0) if mode == "SR" and case["suite"] == "OS_TEST" else (failed, waived)
    assert suite_result(summary, case["suite"], mode, requirement) == expected_suite_status(*counts)
    assert summary["Overall Compliance Result"].split(" : ", 1)[0] == expected_overall_status(
        *counts, requirement, True
    )


@pytest.mark.parametrize("case,mode,requirement", SUITE_MODES)
@pytest.mark.parametrize("selected", [False, True], ids=["full-run", "selected-run"])
def test_missing_suite_policy(merger, monkeypatch, tmp_path, case, mode, requirement, selected):
    requirement = case.get("when_missing", requirement)
    _, summary = run_merge(
        merger, monkeypatch, tmp_path,
        {"acs_info.json": {"ACS Results Summary": {"Overall Compliance Result": "Unknown"}}},
        mode, [(case["suite"], requirement)], selected,
    )
    blocks_compliance = requirement == "M" or (mode == "DT" and requirement == "R")
    expected_suite = "Not Compliant: not run" if blocks_compliance else "Not Run"
    expected_overall = "Not Compliant" if blocks_compliance else "Compliant"
    assert suite_result(summary, case["suite"], mode, requirement) == expected_suite
    assert summary["Overall Compliance Result"].split(" : ", 1)[0] == expected_overall


def test_bsa_and_post_script_recommended_failures_remain_visible(merger, monkeypatch, tmp_path):
    """Reproduce the reported inconsistency using the real DT category enrichment."""
    data, summary = run_merge(
        merger, monkeypatch, tmp_path,
        {"bsa.json": {"test_results": [result_group("testcases", 1, 0, name="PE")]},
         "post_script.json": {"test_results": [result_group("subtests", 5, 0)]}},
        "DT", [("BSA", "R"), ("POST_SCRIPT", "R")],
        categories=merger.build_testcategory_dict(merger.load_test_category_data("DT")),
    )
    assert data["Suite_Name: BSA"]["test_results"][0]["SRS scope"] == "Recommended"
    assert suite_result(summary, "BSA", "DT", "R") == "Not Compliant: Failed 1"
    assert suite_result(summary, "POST_SCRIPT", "DT", "R") == "Not Compliant: Failed 5"
    assert summary["Overall Compliance Result"] == "Compliant"


@pytest.mark.parametrize("states", list(itertools.product(
    ("missing", "passed", "failed", "waived"), repeat=4
)), ids=lambda states: "_".join(
    f"{requirement}-{state}" for requirement, state in
    zip(("mandatory", "recommended", "conditional", "extension"), states)
))
@pytest.mark.parametrize("selected", [False, True], ids=["full-run", "selected-run"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_cross_suite_compliance_precedence(merger, monkeypatch, tmp_path, states,
                                         selected, reverse):
    requirements = [("FWTS", "M"), ("BSA", "R"), ("PFDI", "CM"), ("BBSR-TPM", "EM")]
    files = ["fwts.json", "bsa.json", "pfdi.json", "bbsr_tpm.json"]
    inputs = {"acs_info.json": {"ACS Results Summary": {"Overall Compliance Result": "Unknown"}}}
    for filename, state in zip(files, states):
        if state != "missing":
            inputs[filename] = {"test_results": [
                result_group("buckets", int(state == "failed"), int(state == "waived"))
            ]}
    if reverse:
        requirements.reverse()
        inputs = dict(reversed(list(inputs.items())))
    _, summary = run_merge(merger, monkeypatch, tmp_path, inputs, "DT", requirements, selected)

    mandatory, recommended, conditional, _extension = states
    blocked = (mandatory in ("missing", "failed") or recommended == "missing"
               or conditional == "failed" or (selected and recommended == "failed"))
    waived = mandatory == "waived" or conditional == "waived"
    expected = "Not Compliant" if blocked else "Compliant with waivers" if waived else "Compliant"
    assert summary["Overall Compliance Result"].split(" : ", 1)[0] == expected


@pytest.mark.parametrize("mode", ["DT", "SR"])
@pytest.mark.parametrize("selected", [False, True], ids=["full-run", "selected-run"])
@pytest.mark.parametrize("states", list(itertools.product(
    ("missing", "passed", "failed", "waived"), repeat=3
)), ids=lambda states: "_".join(
    f"{suite}-{state}" for suite, state in zip(("tpm", "fwts", "sct"), states)
))
def test_bbsr_combined_compliance(merger, monkeypatch, tmp_path, mode, selected, states):
    suites = ("BBSR-TPM", "BBSR-FWTS", "BBSR-SCT")
    inputs = {"acs_info.json": {"ACS Results Summary": {"Overall Compliance Result": "Unknown"}}}
    for suite, state in zip(suites, states):
        if state != "missing":
            filename = suite.lower().replace("-", "_") + ".json"
            inputs[filename] = {"test_results": [
                result_group("buckets", int(state == "failed"), int(state == "waived"))
            ]}
    _, summary = run_merge(merger, monkeypatch, tmp_path, inputs, mode,
                           [(suite, "EM") for suite in suites], selected)
    if all(state == "missing" for state in states):
        expected = "Not run"
    elif "failed" in states or "missing" in states:
        expected = "Not Compliant"
    elif "waived" in states:
        expected = "Compliant with waivers"
    else:
        expected = "Compliant"
    assert summary["BBSR compliance results"].split(" : ", 1)[0] == expected
    assert summary["Overall Compliance Result"] == "Compliant"
