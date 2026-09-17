"""Independent log fixtures through the portable parser and artifact validator."""

import fnmatch
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from collections import Counter

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[4]
PARSER = ROOT / "common/log_parser"
SCENARIOS = yaml.safe_load(Path(__file__).with_name("scenarios.yaml").read_text())
CASES = SCENARIOS["scenarios"]


def run(command, cwd, expected=0):
    result = subprocess.run(
        [str(item) for item in command], cwd=cwd, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
        env={**os.environ, "MPLBACKEND": "Agg", "PYTHONPATH": "",
             "PYTHONNOUSERSITE": "1", "NO_COLOR": "1"},
        check=False,
    )
    assert result.returncode == expected, result.stdout
    return result.stdout


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


@pytest.fixture(scope="module")
def portable_parser(tmp_path_factory):
    destination = tmp_path_factory.mktemp("portable-parser") / "log_parser"
    shutil.copytree(PARSER, destination, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    return destination


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
                assert str(script.relative_to(ROOT)) in manifest_targets, script
        for supporting in suite.get("supporting_logs_to_json", []):
            script = PARSER / supporting
            assert script.is_file() and str(script.relative_to(ROOT)) in manifest_targets, script
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
        "--output", output, "--outputs", "summary", "--schema",
        "--acs-config", acs_config, "--system-config", system_config,
    ], tmp_path)

    # These expectations come from the log fixtures, never from another parser.
    for filename, expectation in case["raw"].items():
        assert_raw_expectations(output / "acs_jsons" / filename, expectation)
    merged = json.loads((output / "acs_jsons/merged_results.json").read_text())
    compliance = merged["Suite_Name: acs_info"]["ACS Results Summary"]
    assert compliance["Band"] == band
    assert compliance["Overall Compliance Result"].startswith("Not Compliant"), compliance
    for key, expected in case.get("compliance", {}).items():
        assert compliance[key] == expected, (key, compliance[key])
    command = [sys.executable, portable_parser / "validate.py", "artifacts", output,
               "--mode", case["mode"], "--selected-suites", selected]
    for filename, expectation in case["raw"].items():
        command.extend(["--expect-raw", f"{filename}={expectation['destination']}"])
    for auxiliary in case.get("auxiliary", []):
        command.extend(["--expect-aux", auxiliary])
    run(command, tmp_path)
    return case, output, command


def test_log_json_html_contract(generated_run):
    _, output, _ = generated_run
    assert (output / "html_detailed_summaries/acs_summary.html").is_file()


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
    validator = load_script(portable_parser / "validate.py", "selected_artifact_validator")
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
    "", "B_PE_01 : 1 : QA passing rule\nResult: PASSED\nB_PE_02 : 2 : QA unfinished rule\n",
    "B_PE_01 : 1 : QA rule\nResult: \n",
    "B_PE_01 : 1 : QA rule\nResult: UNKNOWN\n",
    "B_PE_01 : 1 : QA rule\nResult: not-a-result\n",
], ids=["header_only", "complete_then_truncated", "empty_result", "unknown_result", "invalid_result"])
def test_incomplete_log_is_not_a_success(tail, portable_parser, tmp_path):
    source = tmp_path / "BsaResults.log"
    source.write_text("*** Running PE tests ***\n" + tail)
    output = tmp_path / "output"
    run([sys.executable, portable_parser / "standalone_runner.py", "--mode", "DT",
         "--suite", "BSA", "--input-log", source, "--output", output,
         "--outputs", "summary", "--schema"], tmp_path, expected=5)
    assert not output.exists()
    assert not list(tmp_path.glob(".output.tmp-*"))


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
