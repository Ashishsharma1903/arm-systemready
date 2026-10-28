# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Independent log fixtures through the portable parser and artifact validator."""

import copy
import fnmatch
from functools import cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
from collections import Counter

import pytest
import yaml

from common.acs_test_framework_runner.log_parser_qa.qa_report import (
    assert_parser_output, assert_runtime_matches, run_checked_command as run, run_command,
)


ROOT = Path(__file__).resolve().parents[4]
PARSER = Path(os.environ.get("LOG_PARSER_QA_PARSER_DIR", ROOT / "common/log_parser")).resolve()
ARTIFACT_VALIDATOR = ROOT / "common/acs_test_framework_runner/log_parser_qa/parser_artifacts.py"
SCENARIOS = yaml.safe_load(Path(__file__).with_name("fixtures.yaml").read_text())
CASES = SCENARIOS["scenarios"]


def result_paths(value, path=()):
    if isinstance(value, dict):
        if "Test_result" in value or "sub_test_result" in value:
            yield path
        for key, child in value.items():
            yield from result_paths(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from result_paths(child, (*path, index))


def assert_raw_expectations(path, expectation):
    data = json.loads(path.read_text())
    records = expectation["records"]
    assert set(result_paths(data)) == {tuple(record["path"]) for record in records}, (
        path.name, "result inventory differs from log fixture")
    for assertion in expectation["assertions"]:
        actual = data
        for part in assertion["path"]:
            actual = actual[part]
        assert actual == assertion["value"], (path.name, assertion["path"], actual)
    for record in records:
        actual = data
        for part in record["path"]:
            actual = actual[part]
        for field, expected in record["fields"].items():
            assert field in actual, (path.name, record["path"], "missing", field)
            value = actual[field]
            if isinstance(expected, dict):
                assert isinstance(value, dict) and expected.keys() <= value.keys(), (
                    path.name, record["path"], "missing result fields", expected)
                assert not {key: count for key, count in value.items()
                            if key not in expected and isinstance(count, (int, float)) and count}, (
                    path.name, record["path"], "unexpected nonzero result count", value)
                value = {key: value[key] for key in expected}
            assert value == expected, (path.name, record["path"], field, value, expected)
    return data


@pytest.fixture(scope="session")
def portable_parser(tmp_path_factory):
    """Run the shipped archive, not a copy that can hide packaging omissions."""
    directory = tmp_path_factory.mktemp("portable-parser")
    archive = directory / "parser.tar.gz"
    output = run([PARSER / "package_standalone.sh", archive], directory)
    (directory / "package-build.log").write_text(output)
    checksum = Path(str(archive) + ".sha256").read_text().split()
    assert len(checksum) == 2 and checksum[1].lstrip("*") == archive.name, checksum
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == checksum[0], "Archive checksum differs"
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        assert members, "Standalone archive is empty"
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            assert (path.parts and path.parts[0] == "systemready-log-parser" and
                    not path.is_absolute() and ".." not in path.parts and
                    (member.isfile() or member.isdir()) and str(path) not in names), (
                        "Unsafe or duplicate archive member", member.name)
            names.add(str(path))
        # Entries were checked explicitly for Python versions without safe filters.
        bundle.extractall(directory / "extracted", members=members)
    destination = directory / "extracted/systemready-log-parser/common/log_parser"
    assert_runtime_matches(PARSER, destination, directory / "package-integrity.json")
    run([destination / "main_log_parser.sh", "--standalone", "--help"], directory)
    run([destination / "main_log_parser.sh", "--standalone", "--list-suites"], directory)
    return destination


def artifact_validator(parser, name="qa_artifact_validator"):
    """Use the current QA checker against either parser revision under test."""
    module = load_script(ARTIFACT_VALIDATOR, name)
    module.SCRIPT_DIR = parser
    return module


def test_scenarios_cover_registry_execution():
    registry = json.loads((PARSER / "suite_registry.json").read_text())
    expected = set(registry["standalone"]["suite_execution"])
    covered = {suite for case in CASES for suite in case["suites"]}
    assert covered == expected, f"Missing: {expected - covered}; unknown: {covered - expected}"
    assert {case["mode"] for case in CASES} == {"DT", "SR"}
    suite_by_name = {suite["canonical"]: suite for suite in registry["suites"]}
    expected_modes = {(name, mode) for name in expected for mode in suite_by_name[name]["modes"]}
    covered_modes = {(name, case["mode"]) for case in CASES for name in case["suites"]}
    assert covered_modes == expected_modes, (
        f"Missing suite/mode fixtures: {sorted(expected_modes - covered_modes)}; "
        f"unsupported: {sorted(covered_modes - expected_modes)}")
    manifest_targets = set()
    for manifest in (ROOT / "common/acs_test_framework_manifests").glob("*.yaml"):
        for suite in yaml.safe_load(manifest.read_text()).get("suites", []):
            manifest_targets.update(suite.get("files", []))
    registered_directories = set()
    for suite in registry["suites"]:
        if suite.get("logs_to_json"):
            assert suite["canonical"] in expected, f"{suite['canonical']}: execution route is missing"
        for field in ("logs_to_json", "json_to_html", "sr_logs_to_json"):
            if suite.get(field):
                script = PARSER / suite[field]
                assert script.is_file(), f"{suite['canonical']}: missing {field} {script}"
                registered_directories.add(script.parent)
                assert "common/log_parser/" + suite[field] in manifest_targets, script
        for supporting in suite.get("supporting_logs_to_json", []):
            script = PARSER / supporting
            assert script.is_file() and "common/log_parser/" + supporting in manifest_targets, script
        # PFDI retains its old direct CLI alongside its registered BSA-based route.
        if suite["canonical"] == "PFDI":
            registered_directories.add(PARSER / "pfdi")
        if suite["canonical"] in expected:
            assert suite.get("schema"), f"{suite['canonical']}: raw schema is missing"
            schema_name, _, fragment = suite["schema"].partition("#")
            schema = json.loads((PARSER / schema_name).read_text())
            for part in fragment.strip("/").split("/") if fragment else []:
                schema = schema[part.replace("~1", "/").replace("~0", "~")]
    actual_directories = {path.parent for name in ("logs_to_json.py", "json_to_html.py")
                          for path in PARSER.rglob(name)}
    assert actual_directories <= registered_directories, actual_directories - registered_directories
    for case in CASES:
        for name in case["suites"]:
            suite = suite_by_name[name]
            patterns = [suite["json_output"], *suite.get("json_output_patterns", [])]
            outputs = [raw for raw in case["raw"] if any(fnmatch.fnmatchcase(raw, pattern)
                       for pattern in patterns)]
            assert outputs, f"{case['name']}: {name} has no expected produced JSON"
            assert all(case["raw"][raw]["assertions"] and case["raw"][raw]["records"]
                       for raw in outputs)
        for expectation in case["raw"].values():
            for record in expectation["records"]:
                fields = record["fields"]
                assert {"Test_result", "sub_test_result"} & fields.keys(), record
                assert {"Test_case", "sub_Test_Number", "sub_Test_GUID"} & fields.keys(), record
                assert {"Test_case_description", "sub_Test_Description"} & fields.keys(), record


@pytest.fixture(scope="module", params=CASES, ids=lambda case: case["name"])
def generated_run(request, portable_parser, tmp_path_factory):
    case = request.param
    tmp_path = tmp_path_factory.mktemp(case["name"])
    for relative, content in case["files"].items():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    results = tmp_path / "results"
    (results / "linux_dump").mkdir(parents=True, exist_ok=True)
    (results / "linux_dump/dmidecode.txt").write_text(
        "BIOS Information\n    Version: QA-FW\nSystem Information\n"
        "    Manufacturer: QA Vendor\n    Product Name: QA Board\n    Family: QA SoC\n"
    )
    (results / "uefi_dump").mkdir(exist_ok=True)
    (results / "uefi_dump/uefi_version.log").write_text("UEFI v2.10\n", encoding="utf-16")
    band = "SystemReady Devicetree band" if case["mode"] == "DT" else "SystemReady band"
    suffix = "_dt" if case["mode"] == "DT" else ""
    acs_config = tmp_path / f"acs_config{suffix}.txt"
    acs_config.write_text(
        "ACS version: ACS v26.03_3.1.2\nSRS version: SRS 3.1.1\n"
        "BSA version: BSA v1.2\nSBSA version: SBSA v8.0\n"
        "PFDI version: PFDI 1.0 BET0\nBBR version: BBR v2.1\n"
        "SBBR version: SBBR v1.2\nSBMR version: SBMR v1.0\n"
        "EBBR version: EBBR v2.2.0\nBBSR version: BBSR v1.3\n"
        f"SCMI version: SCMI v3.2\nDevice Tree Version: v0.4\nBand: {band}\n"
    )
    system_config = tmp_path / f"system_config{suffix}.txt"
    system_config.write_text(
        "FW source code: Unknown\nFlashing instructions: Unknown\n"
        "product website: Unknown\nTested operated Systems: Unknown\n"
        "Testlab assistance: Unknown\nTotal_number_of_network_controllers= 0\n"
    )
    selected = ",".join(case["suites"])
    output = tmp_path / "output"
    run([
        "bash", portable_parser / "main_log_parser.sh", "--standalone",
        "--mode", case["mode"], "--suite", selected, "--input-log", results,
        "--output", output, "--outputs", "summary",
        "--acs-config", acs_config, "--system-config", system_config,
    ], tmp_path)

    # The checked command above guarantees exit zero. Only mismatches against
    # these independent log expectations get parser-output attribution; input
    # loading and host errors still keep their original exception types.
    for filename, expectation in case["raw"].items():
        assert_parser_output(0, assert_raw_expectations, output / "acs_jsons" / filename, expectation)
    merged = json.loads((output / "acs_jsons/merged_results.json").read_text())
    compliance = merged["Suite_Name: acs_info"]["ACS Results Summary"]
    assert compliance["Band"] == band
    command = [sys.executable, ARTIFACT_VALIDATOR, "artifacts", output,
               "--parser-dir", portable_parser, "--mode", case["mode"], "--selected-suites", selected]
    for filename, expectation in case["raw"].items():
        command.extend(["--expect-raw", f"{filename}={expectation['destination']}"])
    for auxiliary in case.get("auxiliary", []):
        command.extend(["--expect-aux", auxiliary])
    return case, output, command


def test_log_json_html_contract(generated_run):
    _, output, command = generated_run
    assert (output / "html_detailed_summaries/acs_summary.html").is_file()
    # Keep strict artifact/schema checks, but do not let one schema finding
    # prevent independent browser and mutation checks from examining reports.
    run(command, output.parent)


def test_generated_run_compliance(generated_run):
    """A known verdict defect must not block independent browser checks."""
    case, output, _ = generated_run
    merged = json.loads((output / "acs_jsons/merged_results.json").read_text())
    summary = merged["Suite_Name: acs_info"]["ACS Results Summary"]
    assert summary["Overall Compliance Result"].split(" : ", 1)[0] == case.get("overall", "Not Compliant"), summary
    for key, expected in case.get("compliance", {}).items():
        assert summary[key] == expected, (key, summary[key])


@pytest.mark.qa_context(suite="FWTS", mode="SR", stage="log-to-json")
def test_swapped_log_statuses_are_detected_despite_equal_totals(portable_parser, tmp_path):
    log = CASES[0]["files"]["results/fwts/FWTSResults.log"]
    log = log.replace("PASSED: Test 1, QA passed", "QA_SWAP_MARKER")
    log = log.replace("FAILED [HIGH] QA failed", "PASSED: Test 2, QA passed")
    log = log.replace("QA_SWAP_MARKER", "FAILED [HIGH] QA failed")
    source = tmp_path / "fwts.log"
    source.write_text(log)
    output = tmp_path / "output"
    run([sys.executable, portable_parser / "standalone_runner.py", "--mode", "SR",
         "--suite", "FWTS", "--input-log", source, "--output", output,
         "--outputs", "json", "--schema"], tmp_path)
    raw = output / "acs_jsons/fwts.json"
    summary = json.loads(raw.read_text())["suite_summary"]
    assert summary["total_passed"] == summary["total_failed"] == 1
    with pytest.raises(AssertionError, match="sub_test_result"):
        assert_raw_expectations(raw, CASES[0]["raw"]["fwts.json"])


def load_script(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("outcome", SCENARIOS["outcomes"], ids=lambda item: item["name"])
@pytest.mark.qa_context(mode="SR", stage="end-to-end")
def test_selected_pass_fail_waiver(outcome, portable_parser, tmp_path):
    suite = outcome["suite"]
    relative, group = {
        "FWTS": ("fwts/FWTSResults.log", "uefirtmisc"),
        "SCT": ("sct_results/Overall/Summary.log", "RuntimeServicesTest"),
        "BSA": ("uefi/BsaResults.log", "PE"),
    }[suite]
    log = CASES[0]["files"]["results/" + relative]
    if outcome["status"] == "passed":
        log = log.replace("FAILED [HIGH] QA failed", "PASSED: Test 2, QA passed")
    source = tmp_path / "results" / relative
    source.parent.mkdir(parents=True)
    source.write_text(log)
    category = tmp_path / "category.json"
    category.write_text(json.dumps({"qa": [{"Suite": suite, "Test Suite": group,
        "Waivable": "yes", "SRS scope": "core", "Main Readiness Grouping": "QA"}]}))
    output = tmp_path / "output"
    command = ["bash", portable_parser / "main_log_parser.sh", "--standalone", "--mode", "SR",
               "--suite", suite, "--input-log", tmp_path / "results", "--output", output,
               "--outputs", "summary", "--schema", "--test-category", category]
    if outcome["status"] == "waived":
        waiver = tmp_path / "waiver.json"
        waiver.write_text(json.dumps({"Suites": [{"Suite": suite, "Reason": "QA approved waiver"}]}))
        command.extend(["--waiver", waiver])
    run(command, tmp_path)
    validator = artifact_validator(portable_parser, "selected_artifact_validator")
    raw_path = output / "acs_jsons" / (suite.lower() + ".json")
    raw = json.loads(raw_path.read_text())
    renderer = {"BSA": "bsa", "FWTS": "bbr/fwts", "SCT": "bbr/sct"}[suite] + "/json_to_html.py"
    validator._assert_raw_internal_counts(raw_path, raw, renderer)
    merged = json.loads((output / "acs_jsons/merged_results.json").read_text())
    compliance = merged["Suite_Name: acs_info"]["ACS Results Summary"]
    suite_values = [value for key, value in compliance.items() if key.endswith("_compliance")]
    assert suite_values == [outcome["compliance"]]
    overall = {"failed": "Not Compliant", "passed": "Compliant",
               "waived": "Compliant with waivers"}[outcome["status"]]
    assert compliance["Overall Compliance Result"].startswith(overall), compliance
    expected = {"total": 2, "passed": outcome["passed"], "failed": outcome["failed"],
                "failed_with_waiver": outcome["waived"]}
    reports = output / "html_detailed_summaries"
    for path in reports.glob("*.html"):
        document = validator._read_html(path)
        summaries = validator._summary_maps(document, path)
        assert len(summaries) == 1, path.name
        assert {key: summaries[0][key] for key in expected} == expected, path.name
        if path.name.endswith("_detailed.html"):
            records, _ = validator._detail_records(document, path)
            expected_rows = Counter({status: number for status, number in expected.items()
                                     if status != "total" and number})
            if suite == "BSA":
                expected_rows["failed_with_waiver"] += 1  # Nested child is also waived.
            assert Counter(record[2] for record in records) == expected_rows


@pytest.mark.parametrize("arguments", [
    ["--suite", "UNKNOWN"], ["--mode", "SR", "--suite", "SCMI"],
    ["--input-log", "absent"], ["--outputs", "invalid"], ["--waiver", "absent.json"],
])
def test_invalid_standalone_request_does_not_publish(arguments, portable_parser, tmp_path):
    source = tmp_path / "input.log"
    source.write_text(CASES[0]["files"]["results/uefi/BsaResults.log"])
    output = tmp_path / "output"
    result = subprocess.run(
        [sys.executable, str(portable_parser / "standalone_runner.py"), "--mode", "DT",
         "--suite", "BSA", "--input-log", str(source), "--output", str(output),
         "--outputs", "json", *arguments], cwd=tmp_path, capture_output=True, text=True,
        timeout=15, check=False,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert not output.exists()
    assert not list(tmp_path.glob(".output.tmp-*"))


@pytest.mark.parametrize("tail", [
    "B_PE_01 : 1 : QA passing rule\nResult: PASSED\nB_PE_02 : 2 : QA unfinished rule\n",
    "B_PE_01 : 1 : QA rule\nResult: \n",
    "B_PE_01 : 1 : QA rule\nResult: UNKNOWN\n",
    "B_PE_01 : 1 : QA rule\nResult: not-a-result\n",
], ids=["complete_then_truncated", "empty_result", "unknown_result", "invalid_result"])
@pytest.mark.qa_context(suite="BSA", mode="DT", stage="log-to-json")
def test_incomplete_log_is_not_a_success(tail, portable_parser, tmp_path):
    source = tmp_path / "BsaResults.log"
    source.write_text("*** Running PE tests ***\n" + tail)
    output = tmp_path / "output"
    run([sys.executable, portable_parser / "standalone_runner.py", "--mode", "DT",
         "--suite", "BSA", "--input-log", source, "--output", output,
         "--outputs", "summary", "--schema"], tmp_path, expected=5)
    assert not output.exists()
    assert not list(tmp_path.glob(".output.tmp-*"))


@pytest.mark.qa_context(suite="BSA", mode="DT", stage="failure-cleanup")
def test_existing_output_is_preserved(portable_parser, tmp_path):
    source = tmp_path / "BsaResults.log"
    source.write_text(CASES[0]["files"]["results/uefi/BsaResults.log"])
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "existing.html"
    sentinel.write_text("Previous successful report")
    run([sys.executable, portable_parser / "standalone_runner.py", "--mode", "DT",
         "--suite", "BSA", "--input-log", source, "--output", output,
         "--outputs", "summary"], tmp_path, expected=8)
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_text() == "Previous successful report"
    assert not list(tmp_path.glob(".output.tmp-*"))


@pytest.mark.parametrize("kind", ["raw_count", "html_count", "missing_artifact"])
@pytest.mark.qa_context(stage="artifact-validation")
def test_every_artifact_rejects_corruption(generated_run, kind):
    case, output, command = generated_run
    if kind == "raw_count":
        paths = [output / "acs_jsons" / name for name in case["raw"]]
    else:
        paths = sorted((output / "html_detailed_summaries").glob("*.html"))
    assert paths
    for path in paths:
        original = path.read_bytes()
        try:
            if kind == "raw_count":
                data = json.loads(original)
                summary = data["suite_summary"]
                key = "Passed" if "Passed" in summary else "total_passed"
                summary[key] += 1
                path.write_text(json.dumps(data))
            elif kind == "html_count":
                text, count = re.subn(
                    r'(<td[^>]*class="(?:total-tests|pass)"[^>]*>)\d+(</td>)',
                    r'\g<1>999999\2', original.decode(), count=1,
                )
                assert count == 1, f"No summary count found in {path.name}"
                path.write_text(text)
            else:
                path.unlink()
            diagnostics = run(command, output.parent, expected=1)
            if kind == "raw_count":
                assert f"raw suite summary mismatch in {path.name}" in diagnostics, diagnostics
            elif kind == "missing_artifact":
                assert "missing" in diagnostics and path.name in diagnostics, diagnostics
            elif path.name == "acs_summary.html":
                assert "acs_summary.html links" in diagnostics and "wrong summary card" in diagnostics, diagnostics
            else:
                detail_name = path.name.replace("_summary.html", "_detailed.html")
                assert f"detailed and summary HTML counts disagree for {detail_name}" in diagnostics, diagnostics
        finally:
            path.write_bytes(original)


BROWSER_ASSERTIONS = r"""
<script>
window.addEventListener("load", function () {
  window.setTimeout(function () {
    var failures = window.qaErrors || [];
    function expect(condition, message) { if (!condition) failures.push(message); }
    try {
      expect(document.body.classList.contains("acs-report-ui"), "Report UI did not initialize");
      expect(document.querySelector("h1, h2"), "Report has no heading");
      document.querySelectorAll("table.summary-table").forEach(function (table) {
        var summary = table.closest(".acs-compact-summary");
        expect(summary, "Source summary was not converted to visible summary");
        if (!summary) return;
        var source = Array.from(table.querySelectorAll("tr")).filter(function (row) {
          return row.cells.length >= 2 && !row.querySelector("th") &&
            !/^Total\b/i.test(row.cells[0].textContent.trim());
        }).map(function (row) { return Number(row.cells[row.cells.length - 1].textContent); });
        var visible = Array.from(summary.querySelectorAll(".acs-progress-count"))
          .map(function (item) { return Number(item.textContent); });
        expect(JSON.stringify(source) === JSON.stringify(visible), "Visible status counts changed");
      });
      var search = document.querySelector('input[type="search"]');
      if (document.body.getAttribute("data-acs-view") === "detail") {
        expect(search, "Detailed report search is missing");
      }
      if (search && document.body.getAttribute("data-acs-view") === "detail") {
        var counter = document.querySelector(".acs-filter-count");
        var initial = counter.textContent;
        search.value = "qa-no-result-79c498f8";
        search.dispatchEvent(new Event("input", {bubbles: true}));
        expect(counter.textContent !== initial && /0/.test(counter.textContent), "Search did not filter rows");
        search.value = "";
        search.dispatchEvent(new Event("input", {bubbles: true}));
        expect(counter.textContent === initial, "Clearing search did not restore rows");
        var filter = document.querySelector('.acs-status-filter[aria-pressed="false"]');
        if (filter) {
          filter.click();
          expect(filter.getAttribute("aria-pressed") === "true", "Status filter did not activate");
          filter.click();
          expect(counter.textContent === initial, "Status filter did not restore rows");
        }
      }
      expect(document.documentElement.scrollWidth <= window.innerWidth + 2, "Report overflows viewport");
    } catch (error) { failures.push(String(error)); }
    var result = document.createElement("pre");
    result.id = "browser-smoke-result";
    result.textContent = failures.join("; ") || "PASS";
    document.body.appendChild(result);
    if (!failures.length) document.body.setAttribute("data-browser-smoke", "PASS");
  }, 100);
});
</script>
"""


@pytest.mark.parametrize("window_size", ["1600,900", "430,900"])
@pytest.mark.qa_context(stage="browser")
def test_generated_report_browser(generated_run, window_size, tmp_path):
    _, output, _ = generated_run
    helper = ROOT / "common/acs_test_framework_runner/report_ui_browser_smoke.py"
    browser = load_script(helper, "report_browser")
    executable = browser._chromium_binary()
    reports = tmp_path / "reports"
    shutil.copytree(output / "html_detailed_summaries", reports)
    for report in sorted(reports.glob("*.html")):
        content = report.read_text()
        content = content.replace("<head>", '<head><script>window.qaErrors=[];'
            'window.addEventListener("error",function(e){window.qaErrors.push(e.message);});</script>', 1)
        content = content.replace("</body>", BROWSER_ASSERTIONS + "</body>", 1)
        browser._run_page(executable, reports, report.stem, content, window_size)


# Native producer inputs and waiver boundaries.

CONTRACTS = SCENARIOS
NATIVE = [{**case, "mode": mode} for case in CONTRACTS["native"] for mode in case["modes"]]
NEGATIVE = [(case, variant) for case in NATIVE for variant in case["negatives"]]


@pytest.mark.qa_context(stage="qa-contract")
def test_native_empty_input_policy_covers_every_route():
    """A newly registered parser needs an explicit no-execution contract too."""
    registry = json.loads((PARSER / "suite_registry.json").read_text())
    expected = {(suite["canonical"], mode) for suite in registry["suites"]
                if suite.get("logs_to_json") for mode in suite["modes"]}
    covered = {(case["suite"], case["mode"]) for case in NATIVE
               if {"empty", "header"} <= set(case["negatives"])}
    assert covered == expected, {"missing": sorted(expected - covered), "unknown": sorted(covered - expected)}


def native_log(case, variant="positive", failed=0):
    family, suite = case["format"], case["suite"]
    if family == "fixture":
        # Reuse reviewed producer-format inputs, not a second handwritten copy.
        fixture = next(item for item in CASES if item["name"] == case["fixture"])
        files = {}
        for relative in case["files"]:
            content = fixture["files"][relative]
            if variant == "empty":
                content = ""
            elif variant == "header":
                content = case["header"]
            elif variant == "truncated":
                assert case["truncate_before"] in content
                content = content.split(case["truncate_before"], 1)[0]
            elif variant == "malformed":
                before, after = case["malformed"]
                assert before in content
                content = content.replace(before, after)
            files[relative.removeprefix("results/") if relative.startswith("results/") else "../" + relative] = content
        return files
    if variant == "empty":
        return ""
    if family == "bsa":
        header = f" {suite} Architecture Compliance Suite\n          Version 1.2.1\n"
        prefix = "B" if suite == "BSA" else "S"
        return header if variant == "header" else (
            header + "\n    *** Running PE tests ***\n\n"
            f"{prefix}_PE_01 : 1 : Native architecture check\nResult: PASSED\n"
            "---------- ACS Summary ----------\nTotal Rules Run : 1\nPassed : 1\nFailed : 0\n"
            f"*** {suite} tests complete. Reset the system. ***\n")
    if family == "fwts":
        header = "Results generated by fwts: Version V26.01.00.\n"
        if variant == "header":
            return header
        group = case["group"]
        assertions = "".join(f"{'FAILED' if failed else 'PASSED'}: Test 1, native assertion {i + 1}.\n"
                             for i in range(failed or 1))
        return (header + f"Running tests: {group}\n" + "-" * 80
                + f"\n{group}: Native firmware check.\n" + "-" * 80
                + "\nTest 1 of 1: Native firmware check.\n" + assertions + "\n" + "=" * 80
                + f"\n{int(not failed)} passed, {failed} failed, 0 warning, 0 aborted, 0 skipped, 0 info only.\n"
                + "=" * 80 + "\n")
    if family == "sct":
        if variant == "header":
            return "BBR ACS 2.1.1 (SBBR)\n"
        text = ""
        status = "FAIL" if variant == "parent_failure" else "PASS"
        for i, name in enumerate(case["entries"], 1):
            guid = f"00000000-0000-4000-8000-{i:012d}"
            text += (f"BBR ACS 2.1.1 (SBBR)\n{name}\nRevision 0x00010001\n"
                     f"Test Entry Point GUID: {guid}\nTest Configuration #0\n"
                     "Native SCT check\nTest Started: 09/17/26 05:55a\n")
            if variant != "parent_failure":
                text += f"Native check {i} -- PASS\n{guid}\n/native/Test.c:{i}:native passed result\n"
            text += f"\nReturned Status Code: 0x00000000\n{name}: [{status}]\n"
        return text
    if family == "robot":
        if variant == "header":
            return '<robot><suite name="SBMR-ACS"/></robot>'
        status = "UNKNOWN" if variant == "unknown_status" else "PASS"
        terminal = "" if variant == "missing_status" else f'<status status="{status}"/>'
        return ('<robot><suite name="SBMR-ACS"><suite name="Host">'
                f'<test name="Native command">{terminal}</test></suite></suite></robot>')
    if family == "tpm":
        return "Capture ended before TPM results\n" if variant == "header" else "Verify native TPM measurement : PASS\n"
    if family == "capsule":
        text = "Testing ESRT FW version update\nINFO: Native ESRT check\n"
        if variant == "header":
            return "INFO: Starting Capsule Update checks\n"
        return text if variant == "missing_completion" else text + "RESULTS: Overall Capsule Update Result: PASSED\n"
    raise AssertionError(f"Unknown native fixture format: {family}")


@pytest.fixture(scope="module")
def native_context(portable_parser, tmp_path_factory):
    return {"parser": portable_parser, "root": tmp_path_factory.mktemp("native-contracts"), "controls": {},
            "validator": artifact_validator(portable_parser, "native_artifact_validator"),
            "registry": {item["canonical"]: item for item in json.loads(
                (portable_parser / "suite_registry.json").read_text())["suites"]}}


def invoke(context, case, name, text, waiver=None):
    folder = context["root"] / case["mode"] / case["suite"] / name
    files = text if isinstance(text, dict) else {case["relative"]: text}
    sources = []
    for relative, content in files.items():
        source = folder / "input/acs_results" / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content)
        sources.append(source)
    band = "SystemReady Devicetree band" if case["mode"] == "DT" else "SystemReady band"
    config = folder / "acs_config.txt"
    config.write_text("ACS version: Native QA\nSRS version: SRS 3.1.1\nBSA version: BSA v1.2\n"
                      "SBSA version: SBSA v8.0\nSBBR version: BBR v2.1\nEBBR version: EBBR v2.2.0\n"
                      "BBSR version: BBSR v1.3\nSBMR version: SBMR v2.1\n"
                      f"Device Tree Version: v0.4\nBand: {band}\n")
    system = folder / "system_config.txt"
    system.write_text("FW source code: Native QA fixture\n")
    output = folder / "output"
    command = [sys.executable, context["parser"] / "standalone_runner.py", "--mode", case["mode"],
               "--suite", case["suite"], "--input-log", folder / "input/acs_results",
               "--output", output, "--outputs", "summary", "--acs-config", config, "--system-config", system]
    if case.get("category"):
        category = folder / "category.json"
        category.write_text(json.dumps(case["category"]))
        command.extend(["--test-category", category])
    if waiver is not None:
        path = folder / "waiver.json"
        path.write_text(json.dumps(waiver))
        command.extend(["--waiver", path])
    result = run_command(command, cwd=folder, evidence_dir=folder, name="parser")
    observation = {"exit": result.returncode, "folder": folder, "output": output,
                   "evidence": [folder / "parser.command.json", folder / "parser.log", *sources],
                   "traceback": "Traceback (most recent call last)" in result.stdout}
    registration = context["registry"][case["suite"]]
    raw_path = output / "acs_jsons" / registration["json_output"]
    merged_path = output / "acs_jsons/merged_results.json"
    raw_paths = [raw_path] if raw_path.is_file() else sorted({
        path for pattern in registration.get("json_output_patterns", [])
        for path in (output / "acs_jsons").glob(pattern)})
    raws = [json.loads(path.read_text()) for path in raw_paths]
    raw = raws[0] if raws else {}
    merged = json.loads(merged_path.read_text()) if merged_path.is_file() else {}
    label = (("EBBR-" if case["mode"] == "DT" else "SBBR-") + case["suite"]
             if case["suite"] in ("FWTS", "SCT") else registration["requirement_key"])
    summary = merged.get("Suite_Name: acs_info", {}).get("ACS Results Summary", {})
    counts, records = {}, []
    for item in raws:
        for key, value in context["validator"]._summary_values(item["suite_summary"]).items():
            counts[key] = counts.get(key, 0) + value
        records.extend(context["validator"]._leaf_result_records(item, registration["json_to_html"]))
    observation.update(raw=raw, counts=counts,
                       records=records,
                       suite_status=[value for key, value in summary.items()
                                     if key.rsplit(":", 1)[-1].strip() == label + "_compliance" or
                                     (case["suite"] == "OS-TESTS" and case["mode"] == "DT" and
                                      key.rsplit(":", 1)[-1].strip().startswith("OS_") and key.endswith("_compliance"))])
    observation["evidence"].extend([*raw_paths, merged_path])
    html_counts = []
    for filename in (registration["detailed_html"], registration["summary_html"]):
        path = output / "html_detailed_summaries" / filename
        if path.is_file():
            document = context["validator"]._read_html(path)
            html_counts.extend(context["validator"]._summary_maps(document, path))
            if filename == registration["detailed_html"]:
                observation["html_records"], _ = context["validator"]._detail_records(document, path)
            observation["evidence"].append(path)
    observation["html_counts"] = html_counts
    return observation


@pytest.mark.qa_context(stage="qa-contract")
def test_reserved_memory_failure_keeps_completed_results(native_context, qa):
    """Exercise the producer's ERROR line without blocking every DT scenario."""
    case = next(item for item in NATIVE if item["suite"] == "RESERVED-MEMORY-MAP")
    positive_control(native_context, qa, case)
    files = native_log(case)
    relative = next(iter(files))
    files[relative] = files[relative].replace(
        "INFO: Memory Reservation Block Compliance Test: PASSED",
        "ERROR: Memory Reservation Block entry 0 (0x80000000-0x80000fff) is not fully covered by Reserved segments in memmap.\n"
        "INFO: Memory Reservation Block Compliance Test: FAILED").replace("RESULTS: PASSED", "RESULTS: FAILED")
    observation = invoke(native_context, case, "producer-mrb-error", files)
    check(qa, observation["exit"], 0, case, observation,
          "RESERVED_MEMORY_PRODUCER_ERROR", "native-input")
    check(qa, {key: observation["counts"].get(key, 0) for key in ("passed", "failed")},
          {"passed": 1, "failed": 1}, case, observation,
          "RESERVED_MEMORY_PRODUCER_ERROR", "log-json-counts")
    check(qa, observation["suite_status"], ["Not Compliant: Failed 1"], case, observation,
          "RESERVED_MEMORY_PRODUCER_ERROR", "merged-compliance")


def check(qa, actual, expected, case, observation, issue, stage, status="FAIL"):
    qa.check(actual, expected, issue_id=issue, suite=case["suite"], mode=case["mode"],
             stage=stage, status=status, evidence=observation["evidence"])


def assert_counts(qa, observation, expected, case, issue, stage, status="FAIL"):
    check(qa, observation["exit"], 0, case, observation, issue, stage, status)
    counts = observation.get("counts", {})
    check(qa, {key: counts.get(key, 0) for key in expected}, expected,
          case, observation, issue, stage, status)
    check(qa, {key: value for key, value in counts.items() if key != "total" and value},
          {key: value for key, value in expected.items() if value},
          case, observation, issue, stage, status)
    if "total" in counts:
        check(qa, counts["total"], sum(expected.values()), case, observation, issue, stage, status)
    html = observation.get("html_counts", [])
    check(qa, len(html), 2, case, observation, issue, stage, status)
    # Standalone's chart counts test cases; their raw JSON separately counts
    # assertions. Reviewed expectations preserve both levels, not identical totals.
    html_expected = case.get("html_counts", expected) if stage == "positive-control" else expected
    for summary in html:
        check(qa, {key: summary.get(key, 0) for key in html_expected}, html_expected,
              case, observation, issue, stage, status)
        check(qa, {key: value for key, value in summary.items() if key != "total" and value},
              {key: value for key, value in html_expected.items() if value},
              case, observation, issue, stage, status)
        check(qa, summary.get("total"), sum(html_expected.values()), case, observation, issue, stage, status)


def assert_records(qa, observation, expected, case, issue, stage, status="FAIL"):
    for field in ("records", "html_records"):
        actual = sorted([list(record) for record in observation.get(field, [])])
        check(qa, actual, sorted(expected), case, observation, issue, stage, status)


def expected_records(case):
    """Project reviewed fixture rows without calling a production counter."""
    if "records" in case:
        return case["records"]
    fixture = next(item for item in CASES if item["name"] == case["fixture"])
    status_names = {"PASSED": "passed", "PASS": "passed", "FAILED": "failed", "FAIL": "failed",
                    "SKIPPED": "skipped", "ABORTED": "aborted", "WARNINGS": "warnings"}
    records = []
    raw_names = case["raw"] if isinstance(case["raw"], list) else [case["raw"]]
    for record in [record for name in raw_names for record in fixture["raw"][name]["records"]]:
        fields = record["fields"]
        identity = next(fields[key] for key in ("sub_Test_GUID", "sub_Test_Number", "Test_case") if key in fields)
        description = fields.get("sub_Test_Description", fields.get("Test_case_description"))
        value = fields.get("sub_test_result", fields.get("Test_result"))
        if isinstance(value, dict):
            active = [name for name in status_names if value.get(name)]
            assert len(active) == 1, "Fixture must declare one exact leaf outcome"
            value = active[0]
        records.append([str(identity), description, status_names[value]])
    return records


def positive_control(context, qa, case):
    key = (case["suite"], case["mode"])
    if key not in context["controls"]:
        context["controls"][key] = invoke(context, case, "positive", native_log(case))
    observation = context["controls"][key]
    expected = case.get("counts", {"passed": case.get("passed", 0), "failed": 0, "failed_with_waiver": 0})
    assert_parser_output(observation["exit"], assert_counts, qa, observation, expected,
                         case, "NATIVE_POSITIVE_CONTROL", "positive-control", "BLOCKED")
    expected_status = "Not Compliant: Failed " + str(expected["failed"]) if expected.get("failed") else "Compliant"
    assert_parser_output(observation["exit"], check, qa, observation.get("suite_status"),
                         case.get("suite_status", [expected_status]), case, observation,
                         "NATIVE_POSITIVE_CONTROL", "positive-control", "BLOCKED")
    # Project the reviewed fixture outside the wrapper: a broken oracle is not
    # a production parser defect and must continue to block the comparison.
    records = expected_records(case)
    assert_parser_output(observation["exit"], assert_records, qa, observation, records, case,
                         "NATIVE_POSITIVE_CONTROL", "positive-control-identity", "BLOCKED")
    return observation


@pytest.mark.parametrize("case", NATIVE, ids=lambda case: case["suite"] + "-" + case["mode"])
@pytest.mark.qa_context(stage="qa-contract")
def test_native_positive_control(native_context, qa, case):
    positive_control(native_context, qa, case)


@pytest.mark.parametrize("case,variant", NEGATIVE,
                         ids=[case["suite"] + "-" + case["mode"] + "-" + variant for case, variant in NEGATIVE])
@pytest.mark.qa_context(stage="qa-contract")
def test_native_incomplete_evidence_cannot_be_green(native_context, qa, case, variant):
    positive_control(native_context, qa, case)
    observation = invoke(native_context, case, variant, native_log(case, variant))
    issue = "NATIVE_" + variant.upper()
    empty = variant in ("empty", "header")
    check(qa, observation["exit"] == 0 if empty else observation["exit"] in (0, 5),
          True, case, observation, issue, "native-input")
    check(qa, observation["traceback"], False, case, observation, issue, "native-input")
    if observation["exit"] == 5 and variant != "parent_failure" and not empty:
        check(qa, observation["output"].exists(), False, case, observation, issue, "failure-cleanup")
        return
    if variant == "parent_failure":
        assert_counts(qa, observation, {"passed": 0, "failed": 2, "failed_with_waiver": 0},
                      case, issue, "log-json-html-counts")
        entries = observation["raw"]["test_results"]
        check(qa, [(entry.get("Test_case"), entry.get("test_result")) for entry in entries],
              [(name, "FAILED") for name in case["entries"]], case, observation, issue, "log-json-identity")
    elif empty:
        # A producer banner is not an executed test. Do not invent a failure
        # or a skipped outcome to make an empty input look completed.
        check(qa, {key: count for key, count in observation.get("counts", {}).items() if count},
              {}, case, observation, issue, "zero-executed-outcomes")
        check(qa, observation.get("records", []), [], case, observation, issue, "zero-executed-outcomes")
    else:
        check(qa, observation.get("counts", {}).get("passed"), 0,
              case, observation, issue, "no-invented-pass")
        check(qa, len(observation.get("html_counts", [])), 2,
              case, observation, issue, "html-summary-presence")
        for summary in observation["html_counts"]:
            check(qa, summary.get("passed", 0), 0, case, observation, issue, "html-no-invented-pass")
    statuses = observation.get("suite_status", [])
    check(qa, bool(statuses), True, case, observation, issue, "merged-compliance")
    if case["suite"] != "OS-TESTS":
        check(qa, len(statuses), 1, case, observation, issue, "merged-compliance")
    if empty:
        check(qa, all("not run" in item.strip().lower() for item in statuses), True,
              case, observation, issue, "merged-compliance")
    elif not statuses[0].strip().lower().startswith(("not compliant", "not run")):
        check(qa, statuses[0], "Not Compliant or Not Run", case, observation, issue, "merged-compliance")


def whole_suite_waiver(name):
    return {"Suites": [{"Suite": name, "Reason": "Approved native QA waiver"}]}


def matched_device_waiver(description):
    return {"Suites": [{"Suite": "Standalone", "TestSuites": [{"TestCase": {"SubTests": [
        {"sub_Test_Description": description, "Reason": "Approved native QA waiver"}]}}]}]}


@pytest.mark.parametrize("scenario", ["fwts_two_assertions", "passed_match", "runtime", "unmatched", "idempotence"])
@pytest.mark.qa_context(stage="qa-contract")
def test_native_waiver_contracts(native_context, qa, scenario):
    key = "fwts" if scenario in ("fwts_two_assertions", "idempotence") else "runtime" if scenario == "runtime" else "kselftest"
    case = CONTRACTS["waivers"][key]
    failed = 2 if scenario == "fwts_two_assertions" else 1 if key != "kselftest" else 0
    text = native_log(case, failed=failed) if key == "fwts" else case["text"]
    before = invoke(native_context, case, scenario + "-control", text)
    expected_before = {"passed": int(not failed), "failed": failed, "failed_with_waiver": 0}
    assert_counts(qa, before, expected_before, case, "WAIVER_CONTROL", "unwaived-control", "BLOCKED")
    assert_records(qa, before, case["records"], case, "WAIVER_CONTROL", "unwaived-control-identity", "BLOCKED")
    waiver = (matched_device_waiver("Native passing device check" if scenario == "passed_match" else "Not present in this log")
              if key == "kselftest" else whole_suite_waiver("FWTS" if key == "fwts" else "Standalone"))
    after = invoke(native_context, case, scenario, text, waiver)
    expected_after = {"passed": int(not failed), "failed": 0, "failed_with_waiver": failed}
    status = "BLOCKED" if scenario == "idempotence" else "FAIL"
    assert_counts(qa, after, expected_after, case, "WAIVER_" + scenario.upper(), "waiver-json-html", status)
    expected_records = [[identity, description, "failed_with_waiver" if value == "failed" else value]
                        for identity, description, value in case["records"]]
    assert_records(qa, after, expected_records, case, "WAIVER_" + scenario.upper(), "waived-result-identity", status)
    if key == "kselftest":
        check(qa, after["raw"]["test_results"][0]["subtests"], before["raw"]["test_results"][0]["subtests"],
              case, after, "WAIVER_" + scenario.upper(), "unchanged-passed-results")
    if scenario == "idempotence":
        folder = after["folder"] / "second-application"
        folder.mkdir()
        raw = folder / "fwts.json"
        raw.write_text(json.dumps(after["raw"]))
        result = run_command([sys.executable, native_context["parser"] / "apply_waivers.py", "FWTS", raw,
                              after["folder"] / "waiver.json", native_context["parser"] / "test_categoryDT.json", "--quiet"],
                             cwd=folder, evidence_dir=folder, name="parser")
        after["evidence"].extend([raw, folder / "parser.command.json", folder / "parser.log"])
        check(qa, result.returncode, 0, case, after, "WAIVER_IDEMPOTENCE", "second-application")
        check(qa, json.loads(raw.read_text()), after["raw"], case, after, "WAIVER_IDEMPOTENCE", "second-application")
    compliance = f"Compliant with waivers: Waivers {failed}" if failed else "Compliant"
    check(qa, after.get("suite_status"), [compliance], case, after,
          "WAIVER_SUITE_COMPLIANCE", "merged-compliance")


@pytest.mark.qa_context(stage="qa-contract")
def test_whole_parent_bsa_waiver_preserves_passed_descendants(native_context, qa):
    case = CONTRACTS["waivers"]["bsa"]
    before = invoke(native_context, case, "parent-control", case["text"])
    assert_counts(qa, before, {"passed": 0, "failed": 1, "failed_with_waiver": 0},
                  case, "BSA_PARENT_WAIVER_CONTROL", "unwaived-control", "BLOCKED")
    assert_records(qa, before, case["records"], case, "BSA_PARENT_WAIVER_CONTROL", "unwaived-control-identity", "BLOCKED")
    waiver = {"Suites": [{"Suite": "BSA", "TestSuites": [{"TestSuite": "PE", "TestCases": [{
        "Test_case": "B_PE_01 : 1", "Reason": "Approved whole-parent waiver",
        "SubTests": [{"sub_Rule_ID": "B_CHILD_01", "Reason": "Approved child waiver"}],
    }]}]}]}
    after = invoke(native_context, case, "parent-waiver", case["text"], waiver)
    assert_counts(qa, after, {"passed": 0, "failed": 0, "failed_with_waiver": 1},
                  case, "BSA_WHOLE_PARENT_WAIVER", "waiver-json-html")
    expected_records = [[identity, description, "failed_with_waiver" if value == "failed" else value]
                        for identity, description, value in case["records"]]
    assert_records(qa, after, expected_records, case, "BSA_WHOLE_PARENT_WAIVER", "waived-result-identity")
    check(qa, after.get("suite_status"), ["Compliant with waivers: Waivers 1"],
          case, after, "BSA_WHOLE_PARENT_WAIVER", "merged-compliance")
    parent = after["raw"]["test_results"][0]["testcases"][0]
    check(qa, parent["Test_result"], "FAILED (WITH WAIVER)", case, after, "BSA_WHOLE_PARENT_WAIVER", "parent-result")
    check(qa, [child["sub_test_result"] for child in parent["subtests"]],
          ["FAILED (WITH WAIVER)", "FAILED (WITH WAIVER)", "PASSED"],
          case, after, "BSA_WHOLE_PARENT_WAIVER", "descendant-results")


# Incomplete BSA evidence and completed-result controls.

SPEC = importlib.util.spec_from_file_location(
    "qa_bsa_logs", Path(os.environ.get("LOG_PARSER_QA_PARSER_DIR",
         Path(__file__).resolve().parents[3] / "log_parser")) / "bsa/logs_to_json.py"
)
BSA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BSA)


@pytest.mark.parametrize("text", [
    "*** Running PE tests ***\nB_PE_01 : 1 : Pass\nResult: PASSED\nB_PE_02 : 2 : Unfinished\n",
    "*** Running PE tests ***\nB_PE_01 : 1 : Unfinished\nB_PE_02 : 2 : Pass\nResult: PASSED\n",
    "START PE B_PE_01 1 : Unfinished\nSTART PE B_PE_02 2 : Pass\nEND B_PE_02 PASSED\n",
    "START PE PARENT 1 : Parent\n=== Start tests for rules referenced by PARENT ===\n"
    "START PE CHILD 2 : Child\nEND PARENT PASSED\nEND CHILD PASSED\n",
    "*** Running PE tests ***\nPARENT : 1 : Parent\n"
    "=== Start tests for rules referenced by PARENT ===\nCHILD : 2 : Unfinished\n"
    "SIBLING : 3 : Pass\nResult: PASSED\nResult: PASSED\nResult: PASSED\n",
], ids=["eof", "new-rule", "legacy-new-rule", "legacy-parent-before-child", "unfinished-sibling"])
@pytest.mark.qa_context(suite="BSA", mode="mode-independent", stage="log-to-json")
def test_incomplete_rule_preserves_existing_output(tmp_path, text):
    log, output = tmp_path / "BsaResults.log", tmp_path / "bsa.json"
    log.write_text(text)
    output.write_text("previous report")
    with pytest.raises(ValueError, match="incomplete ACS log.*missing Result/END"):
        BSA.main([str(log)], str(output))
    assert output.read_text() == "previous report"


@pytest.mark.qa_context(suite="BSA", mode="mode-independent", stage="log-to-json")
def test_unfinished_rule_cannot_continue_in_another_input_file(tmp_path):
    first, second = tmp_path / "first.log", tmp_path / "second.log"
    first.write_text("START PE B_PE_01 1 : Unfinished\n")
    second.write_text("START PE B_PE_02 2 : Pass\nEND B_PE_02 PASSED\n")
    output = tmp_path / "bsa.json"
    with pytest.raises(ValueError, match="B_PE_01"):
        BSA.main([str(first), str(second)], str(output))
    assert not output.exists()


@pytest.mark.parametrize("status", ["", "UNKNOWN", "not-a-result"],
                         ids=["empty", "unknown", "invalid"])
@pytest.mark.parametrize("template", [
    "*** Running PE tests ***\nB_PE_01 : 1 : Test\nResult: {status}\n",
    "START PE B_PE_01 1 : Test\nEND B_PE_01 {status}\n",
    "*** Running PE tests ***\nB_PE_01 : 1 : Test Result: {status}\n",
    "*** Running PE tests ***\nPARENT : 1 : Parent\n"
    "=== Start tests for rules referenced by PARENT ===\nB_PE_01 : 1 : Child\nResult: {status}\n"
    "=== End tests for rules referenced by PARENT ===\nResult: PASSED\n",
], ids=["modern", "legacy", "inline", "nested"])
@pytest.mark.qa_context(suite="BSA", mode="mode-independent", stage="log-to-json")
def test_unrecognized_results_preserve_existing_output(tmp_path, status, template):
    log, output = tmp_path / "BsaResults.log", tmp_path / "bsa.json"
    log.write_text(template.format(status=status))
    output.write_text("previous report")
    result = subprocess.run([sys.executable, str(BSA.__file__), str(log), str(output)],
                            capture_output=True, text=True, check=False, timeout=15)
    return_code = result.returncode
    assert return_code != 0, "Unrecognized verdict was accepted: " + result.stdout + result.stderr
    assert output.read_text() == "previous report"


@pytest.mark.parametrize("text,expected", [
    ("*** Running PE tests ***\nB_PE_01 : 1 : Partial\nResult: PASSED (PARTIAL)\n", "PASSED(*PARTIAL)"),
    ("START PE B_PE_01 1 : Pass\nEND B_PE_01 PASSED\n", "PASSED"),
    ("*** Running PE tests ***\nB_PE_01 : 1 : Compact Result: PASSED\n", "PASSED"),
    ("*** Running PE tests ***\nPARENT : 1 : Parent\n"
     "=== Start tests for rules referenced by PARENT ===\nCHILD : 2 : Child\nResult: PASSED\n"
     "=== End tests for rules referenced by PARENT ===\nResult: PASSED\n", "PASSED"),
    *[(f"*** Running PE tests ***\nB_PE_01 : 1 : Test\nResult: {status}\n", result)
      for status, result in [
          ("FAILED", "FAILED"), ("FAILED WITH WAIVER", "FAILED (WITH WAIVER)"),
          ("SKIPPED", "SKIPPED"), ("PAL NOT SUPPORTED", "PAL NOT SUPPORTED"),
          ("TEST NOT IMPLEMENTED", "TEST NOT IMPLEMENTED"), ("WARNING", "WARNING"),
      ]],
], ids=["partial-result", "legacy", "inline", "nested", "failed", "waiver",
        "skipped", "unsupported", "unimplemented", "warning"])
@pytest.mark.qa_context(suite="BSA", mode="mode-independent", stage="log-to-json")
def test_completed_rules_remain_supported(tmp_path, text, expected):
    log, output = tmp_path / "BsaResults.log", tmp_path / "bsa.json"
    log.write_text(text)
    BSA.main([str(log)], str(output))
    data = json.loads(output.read_text())
    assert data["suite_summary"]["Total Rules Run"] == 1
    assert data["test_results"][0]["testcases"][0]["Test_result"] == expected


# Normal reruns cannot reuse stale BSA results.

def observe(folder, validator):
    output = folder / "acs_summary"
    merged_path = output / "acs_jsons/merged_results.json"
    merged = json.loads(merged_path.read_text()) if merged_path.is_file() else {}
    summary = merged.get("Suite_Name: acs_info", {}).get("ACS Results Summary", {})
    statuses = [value for key, value in summary.items() if key.endswith("  : BSA_compliance")]
    raw_path = output / "acs_jsons/bsa.json"
    raw = json.loads(raw_path.read_text()) if raw_path.is_file() else {}
    detail = output / "html_detailed_summaries/bsa_detailed.html"
    html_counts = validator._summary_maps(validator._read_html(detail), detail) if detail.is_file() else []
    return {
        "status": statuses, "counts": validator._summary_values(raw.get("suite_summary", {})),
        "html_counts": html_counts, "raw_present": raw_path.is_file(),
        "detail_present": detail.is_file(),
    }


@pytest.fixture(scope="module")
def normal_runs(tmp_path_factory, portable_parser):
    validator = artifact_validator(portable_parser, "normal_artifact_validator")
    folder = tmp_path_factory.mktemp("normal-stale-results")
    mode = "DT" if Path("/mnt/yocto_image.flag").is_file() else "SR"
    band = "SystemReady Devicetree band" if mode == "DT" else "SystemReady band"
    acs, system = folder / "acs_config.txt", folder / "system_config.txt"
    acs.write_text(f"ACS version: QA\nSRS version: SRS 3.1.1\nBSA version: BSA v1.2\nBand: {band}\n")
    system.write_text("FW source code: QA fixture\n")
    inputs = folder / "reused/acs_results"
    log = inputs / "uefi/BsaResults.log"
    log.parent.mkdir(parents=True)
    log.write_text(CASES[0]["files"]["results/uefi/BsaResults.log"].replace("FAILED", "PASSED"))

    def invoke(source, name):
        process = run_command(["bash", portable_parser / "main_log_parser.sh", source, acs, system, ""],
                              folder, folder, name=name)
        return {"exit": process.returncode, **observe(source, validator)}

    first = invoke(inputs, "first")
    if (inputs / "acs_summary").is_dir():
        shutil.copytree(inputs / "acs_summary", folder / "first-report")
    @cache
    def missing_runs():
        # Only reached after a valid positive control; never alter the host mode flag.
        log.rename(folder / "saved-BsaResults.log")
        rerun = invoke(inputs, "missing-rerun")
        fresh_inputs = folder / "fresh/acs_results"
        fresh_inputs.mkdir(parents=True)
        fresh = invoke(fresh_inputs, "fresh-missing")
        (folder / "observations.json").write_text(json.dumps({"first": first, "rerun": rerun, "fresh": fresh}, indent=2))
        return rerun, fresh

    return mode, folder, first, missing_runs


@pytest.mark.parametrize("aspect", ["merged-status", "raw-json", "detailed-html"])
@pytest.mark.qa_context(stage="qa-contract")
def test_normal_missing_input_matches_fresh_run(normal_runs, qa, aspect):
    mode, folder, first, missing_runs = normal_runs
    context = dict(issue_id="normal-stale-bsa-results", suite="BSA", mode=mode,
                   stage="normal-execution", evidence=[folder])
    qa.check(first["exit"], 0, status="BLOCKED", **context)
    qa.check(first["status"], ["Compliant"], status="BLOCKED", **context)
    qa.check({key: value for key, value in first["counts"].items() if value},
             {"total": 2, "passed": 2}, status="BLOCKED", **context)
    qa.check([{key: value for key, value in counts.items() if value} for counts in first["html_counts"]],
             [{"total": 2, "passed": 2}], status="BLOCKED", **context)
    rerun, fresh = missing_runs()
    qa.check(fresh["exit"], 0, status="BLOCKED", **context)
    qa.check(len(fresh["status"]) == 1 and "not run" in fresh["status"][0].lower(),
             True, status="BLOCKED", **context)
    qa.check(rerun["exit"], fresh["exit"], **context)
    key = {"merged-status": "status", "raw-json": "raw_present", "detailed-html": "detail_present"}[aspect]
    qa.check(rerun[key], fresh[key], **context)


# The independent fixture oracle rejects balanced corruption.

@pytest.fixture(scope="module")
def assert_raw():
    return assert_raw_expectations


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
@pytest.mark.qa_context(stage="log-expectations")
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


@pytest.mark.qa_context(stage="log-expectations")
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
