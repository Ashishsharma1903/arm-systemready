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
    return summary.get(f"Suite_Name: {REQUIREMENT_LABELS[requirement]}  : {suite}_compliance", "<missing>")


def expected_suite_status(failed, waived):
    if failed:
        return f"Not Compliant: Failed {failed}"
    if waived:
        return f"Compliant with waivers: Waivers {waived}"
    return "Compliant"


def expected_overall_status(failed, waived, requirement):
    if failed and requirement in CASES["overall"]["failed_blocks"]:
        return "Not Compliant"
    if waived and requirement in CASES["overall"]["failed_blocks"]:
        return "Compliant with waivers"
    return "Compliant"


def test_policy_covers_every_registered_requirement():
    registry = json.loads((PARSER_DIR / "suite_registry.json").read_text())
    for suite in registry["suites"]:
        if not suite.get("included_suites"):
            assert set(suite.get("requirements", {})) == set(suite["modes"]), (
                f"{suite['canonical']}: declare a compliance requirement for every "
                f"supported mode {suite['modes']}"
            )
    expected = {(suite["requirement_key"], mode) for suite in registry["suites"]
                for mode in suite.get("requirements", {})}
    covered = {(case["suite"], mode) for case in CASES["suites"] for mode in case["modes"]}
    assert covered == expected, (
        f"Missing compliance policy scenarios: {sorted(expected - covered)}; "
        f"unregistered scenarios: {sorted(covered - expected)}"
    )


@pytest.mark.parametrize("requirements", [{}, {"SR": "M"}], ids=["absent-policy", "missing-dt-policy"])
def test_onboarding_rejects_unclassified_suite(monkeypatch, tmp_path, requirements):
    registry = json.loads((PARSER_DIR / "suite_registry.json").read_text())
    registry["suites"].append({
        "canonical": "QA-NEW-SUITE", "modes": ["SR", "DT"],
        "requirement_key": "QA-NEW-SUITE", "requirements": requirements,
    })
    (tmp_path / "suite_registry.json").write_text(json.dumps(registry))
    monkeypatch.setitem(globals(), "PARSER_DIR", tmp_path)
    with pytest.raises(AssertionError, match="QA-NEW-SUITE: declare a compliance requirement"):
        test_policy_covers_every_registered_requirement()


@pytest.mark.parametrize("case,mode,requirement", SUITE_MODES)
def test_registry_policy_matches_expected_contract(merger, case, mode, requirement):
    assert dict(merger.requirement_table(mode))[case["suite"]] == case.get("when_missing", requirement)


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("scope", CASES["scopes"])
@pytest.mark.parametrize("failed,waived", CASES["counts"], ids=COUNT_IDS)
@pytest.mark.parametrize("wrapped", [False, True], ids=["list", "object"])
def test_failure_counts(merger, shape, scope, failed, waived, wrapped):
    groups = [result_group(shape, failed, waived, scope)]
    data = {"test_results": groups} if wrapped else groups
    before = copy.deepcopy(data)
    assert merger.count_fails_in_json(data) == (failed, waived)
    assert data == before, "Compliance filtering must not remove visible test results"


@pytest.mark.parametrize("case,mode,requirement", SUITE_MODES)
@pytest.mark.parametrize("scope", CASES["scopes"])
@pytest.mark.parametrize("failed,waived", CASES["counts"], ids=COUNT_IDS)
@pytest.mark.parametrize("selected", [False, True], ids=["full-run", "selected-run"])
@pytest.mark.parametrize("aspect", ["suite", "overall"])
def test_suite_and_overall_policy(merger, monkeypatch, tmp_path, qa, case, mode, requirement,
                                  scope, failed, waived, selected, aspect):
    data = {"test_results": [result_group(case["shape"], failed, waived, scope)]}
    merged, summary = run_merge(
        merger, monkeypatch, tmp_path, {case["file"]: data}, mode,
        [(case["suite"], requirement)], selected,
    )
    ignored = mode == "SR" and case["suite"] == "OS_TEST" and str(scope).strip().lower() == "recommended"
    expected_failed, expected_waived = (0, 0) if ignored else (failed, waived)
    expected_suite = expected_suite_status(expected_failed, expected_waived)
    if aspect == "suite":
        qa.check(suite_result(summary, case["suite"], mode, requirement), expected_suite,
                 issue_id="suite-compliance-status", suite=case["suite"], mode=mode, stage="merge")
    else:
        expected_overall = expected_overall_status(expected_failed, expected_waived, requirement)
        qa.check(summary["Overall Compliance Result"].split(" : ", 1)[0], expected_overall,
                 issue_id="recommended-overall-policy" if requirement == "R" else "overall-compliance-policy",
                 suite=case["suite"], mode=mode, stage="merge")
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
        *counts, requirement
    )


@pytest.mark.parametrize("case,mode,requirement", SUITE_MODES)
@pytest.mark.parametrize("selected", [False, True], ids=["full-run", "selected-run"])
@pytest.mark.parametrize("aspect", ["suite", "overall"])
def test_missing_suite_policy(merger, monkeypatch, tmp_path, qa, case, mode, requirement, selected, aspect):
    requirement = case.get("when_missing", requirement)
    _, summary = run_merge(
        merger, monkeypatch, tmp_path,
        {"acs_info.json": {"ACS Results Summary": {"Overall Compliance Result": "Unknown"}}},
        mode, [(case["suite"], requirement)], selected,
    )
    blocks_compliance = requirement in CASES["overall"]["missing_blocks"]
    expected_suite = "Not Compliant: not run" if blocks_compliance else "Not Run"
    expected_overall = "Not Compliant" if blocks_compliance else "Compliant"
    if aspect == "suite":
        qa.check(suite_result(summary, case["suite"], mode, requirement), expected_suite,
                 issue_id="missing-suite-status", suite=case["suite"], mode=mode, stage="merge")
    else:
        qa.check(summary["Overall Compliance Result"].split(" : ", 1)[0], expected_overall,
                 issue_id="recommended-overall-policy" if requirement == "R" else "missing-required-suite-policy",
                 suite=case["suite"], mode=mode, stage="merge")


@pytest.mark.qa_context(suite="BSA,POST-SCRIPT", mode="DT", stage="merge")
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


def merge_mixed_case(merger, monkeypatch, tmp_path, case, selected=False, reverse=False, inputs=None):
    mode = case["mode"]
    suite_cases = {suite["suite"]: suite for suite in CASES["suites"]}
    requirements = [(name, suite_cases[name].get("when_missing", suite_cases[name]["modes"][mode]))
                    for name in case["states"]]
    if inputs is None:
        inputs = {"acs_info.json": {"ACS Results Summary": {"Overall Compliance Result": "Unknown"}}}
        for name, state in case["states"].items():
            if state != "missing":
                suite = suite_cases[name]
                inputs[suite["file"]] = {"test_results": [
                    result_group(suite["shape"], int(state == "failed"), 0)
                ]}
    if reverse:
        requirements.reverse()
        inputs = dict(reversed(list(inputs.items())))
    return run_merge(merger, monkeypatch, tmp_path, inputs, mode, requirements, selected)


def compliance_rows(summary):
    rows = {}
    for key, value in summary.items():
        if key.startswith("Suite_Name:") and key.endswith("_compliance"):
            requirement, name = key.removeprefix("Suite_Name:").removesuffix("_compliance").rsplit(":", 1)
            rows.setdefault(name.strip(), []).append((requirement.strip(), value))
    return rows


@pytest.mark.parametrize("case", CASES["mixed_suites"], ids=lambda case: case["name"])
@pytest.mark.parametrize("selected", [False, True], ids=["full-run", "selected-run"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize("aspect", ["requirement", "suite", "overall"])
def test_mixed_suite_identity_and_compliance(merger, monkeypatch, tmp_path, qa, case, selected, reverse, aspect):
    _, summary = merge_mixed_case(merger, monkeypatch, tmp_path, case, selected, reverse)
    if aspect == "overall":
        actual, expected = summary["Overall Compliance Result"].split(" : ", 1)[0], case["overall"]
    else:
        index = 0 if aspect == "requirement" else 1
        actual = {name: [row[index] for row in rows] for name, rows in compliance_rows(summary).items()}
        expected = {name: [REQUIREMENT_LABELS[row["requirement"]] if index == 0 else row["result"]]
                    for name, row in case["expected"].items()}
    qa.check(actual, expected, issue_id=f"mixed-suite-{aspect}", suite=",".join(case["states"]),
             mode=case["mode"], stage="merge", evidence=(tmp_path / "merged.json",))


@pytest.mark.parametrize("mode", ["DT", "SR"])
@pytest.mark.parametrize("selected", [False, True], ids=["full-run", "selected-run"])
@pytest.mark.parametrize("state", ["passed", "failed", "waived", "missing"])
@pytest.mark.parametrize("mandatory_failed", [False, True], ids=["mandatory-pass", "mandatory-fail"])
@pytest.mark.parametrize("aspect", ["suite", "overall"])
def test_recommended_isolated_from_overall(merger, monkeypatch, tmp_path, qa, mode, selected,
                                         state, mandatory_failed, aspect):
    inputs = {"sct.json": {"test_results": [result_group("subtests", int(mandatory_failed), 0)]}}
    if state != "missing":
        inputs["fwts.json"] = {"test_results": [result_group("subtests", int(state == "failed"), int(state == "waived"))]}
    _, summary = run_merge(merger, monkeypatch, tmp_path, inputs, mode, [("SCT", "M"), ("FWTS", "R")], selected)
    if aspect == "suite":
        actual = suite_result(summary, "FWTS", mode, "R")
        expected = "Not Run" if state == "missing" else expected_suite_status(int(state == "failed"), int(state == "waived"))
    else:
        actual = summary["Overall Compliance Result"].split(" : ", 1)[0]
        expected = "Not Compliant" if mandatory_failed else "Compliant"
    qa.check(actual, expected, issue_id="recommended-suite-status" if aspect == "suite" else "recommended-overall-policy",
             suite="FWTS", mode=mode, stage="merge", evidence=(tmp_path / "merged.json",))


@pytest.mark.parametrize("states", list(itertools.product(
    ("missing", "passed", "failed", "waived"), repeat=4
)), ids=lambda states: "_".join(
    f"{requirement}-{state}" for requirement, state in
    zip(("mandatory", "recommended", "conditional", "extension"), states)
))
@pytest.mark.parametrize("selected", [False, True], ids=["full-run", "selected-run"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.qa_context(suite="FWTS,BSA,PFDI,BBSR-TPM", mode="DT", stage="merge")
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

    mandatory, _recommended, conditional, _extension = states
    blocked = mandatory in ("missing", "failed") or conditional == "failed"
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
@pytest.mark.qa_context(suite="BBSR-TPM,BBSR-FWTS,BBSR-SCT", stage="merge")
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
