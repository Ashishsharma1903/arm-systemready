"""Independent paired-suite checks through existing parser/report commands.

Raw schema checks live separately. Missing compliance UI is an explicit failed
contract, not a reason to skip the working parser/count/aggregate controls.
"""

from collections import Counter
from html.parser import HTMLParser
import json
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from common.acs_test_framework_runner.qa_evidence import run_command
from test_artifact_validation import validator
from test_compliance import CASES, REQUIREMENT_LABELS, compliance_rows
from test_end_to_end import portable_parser
from test_native_contracts import CONTRACTS, native_log


class BadgeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.badges = [], {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "acs-suite-compliance" in attrs.get("class", "").split():
            owner = next((item.get("id", "") for _, item in reversed(self.stack)
                          if "summary" in item.get("class", "").split()), "<outside-card>")
            attrs["badge_text"] = []
            self.badges.setdefault(owner, []).append(attrs["badge_text"])
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.stack.append((tag, attrs))

    def handle_data(self, data):
        for _, attrs in reversed(self.stack):
            if "badge_text" in attrs:
                attrs["badge_text"].append(data)
                break

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


@pytest.fixture(scope="module", params=CASES["mixed_suites"], ids=lambda case: case["name"])
def mixed_report(request, tmp_path_factory, portable_parser):
    case = request.param
    directory = tmp_path_factory.mktemp(case["name"])
    source = directory / "input/acs_results"
    source.mkdir(parents=True)
    output = directory / "output"
    html = output / "html_detailed_summaries"
    merged_path = output / "acs_jsons/merged_results.json"
    registry = {item["canonical"]: item for item in json.loads(
        (portable_parser / "suite_registry.json").read_text())["suites"]}
    native_cases = {item["suite"]: item for item in CONTRACTS["native"]}
    result = {"case": case, "directory": directory, "html": html, "raw": {}, "error": None,
              "registry": registry, "merged": merged_path}
    for suite, state in case["states"].items():
        if state == "missing":
            continue
        native = native_cases[suite]
        text = native_log(native)
        if state == "failed":
            if native["format"] == "robot":
                document = ET.fromstring(text)
                for status in document.findall(".//test/status"):
                    status.set("status", "FAIL")
                text = ET.tostring(document, encoding="unicode")
            else:
                text = text.replace("Result: PASSED", "Result: FAILED").replace(
                    "Passed : 1", "Passed : 0").replace("Failed : 0", "Failed : 1")
        path = source / native["relative"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    config, system = directory / "acs_config.txt", directory / "system_config.txt"
    config.write_text("ACS version: Native QA\nSRS version: SRS 3.1.1\nBSA version: BSA v1.2\n"
                      "SBSA version: SBSA v8.0\nSBBR version: BBR v2.1\nEBBR version: EBBR v2.2.0\n"
                      "BBSR version: BBSR v1.3\nSBMR version: SBMR v2.1\n"
                      "Device Tree Version: v0.4\nBand: SystemReady band\n")
    system.write_text("FW source code: Native QA fixture\n")
    command = [sys.executable, portable_parser / "standalone_runner.py", "--mode", case["mode"],
               "--suite", ",".join(case["states"]), "--input-log", source, "--output", output,
               "--outputs", "summary", "--acs-config", config, "--system-config", system]
    try:
        completed = run_command(command, directory, directory / "commands", name="standalone")
    except (OSError, subprocess.TimeoutExpired) as error:
        result["error"] = str(error)
        return result
    if completed.returncode:
        result["error"] = {"exit": completed.returncode, "log": str(directory / "commands/standalone.log")}
        return result
    if not merged_path.is_file() or not (html / "acs_summary.html").is_file():
        result["error"] = "Standalone returned success without merged JSON and combined HTML"
        return result
    result["summary"] = json.loads(merged_path.read_text())["Suite_Name: acs_info"]["ACS Results Summary"]
    for suite in case["states"]:
        raw = output / "acs_jsons" / registry[suite]["json_output"]
        if raw.is_file():
            result["raw"][suite] = json.loads(raw.read_text())
    return result


@pytest.mark.parametrize("aspect", ["raw-counts", "html-counts", "requirement", "suite", "overall",
                                    "aggregate-details", "badges", "detail-rows"])
def test_mixed_report_contract(mixed_report, validator, qa, aspect):
    case, directory, html = (mixed_report[key] for key in ("case", "directory", "html"))
    context = {"suite": ",".join(case["states"]), "mode": case["mode"],
               "stage": "html-detail" if aspect == "detail-rows" else "html-summary",
               "evidence": (directory / "commands", directory / "input", mixed_report["merged"], html)}
    qa.check(mixed_report["error"], None, issue_id="mixed-report-positive-control", status="BLOCKED", **context)
    expected_counts = {suite: (int(state == "passed"), int(state == "failed"))
                       for suite, state in case["states"].items() if state != "missing"}
    if aspect == "raw-counts":
        actual = {}
        for suite, data in mixed_report["raw"].items():
            counts = data["suite_summary"]
            keys = ("Passed", "Failed") if suite in ("BSA", "SBSA") else ("total_passed", "total_failed")
            actual[suite] = tuple(counts[key] for key in keys)
        expected = expected_counts
        context["stage"] = "log-to-json"
    elif aspect == "html-counts":
        actual = {}
        for suite in expected_counts:
            counts = []
            for field in ("summary_html", "detailed_html"):
                path = html / mixed_report["registry"][suite][field]
                if not path.is_file():
                    counts.append(f"<missing {path.name}>")
                    continue
                summaries = validator._summary_maps(validator._read_html(path), path)
                counts.extend((summary.get("passed"), summary.get("failed")) for summary in summaries)
            actual[suite] = counts
        expected = {suite: [counts, counts] for suite, counts in expected_counts.items()}
    elif aspect in ("requirement", "suite"):
        index = 0 if aspect == "requirement" else 1
        actual = {suite: [row[index] for row in rows] for suite, rows in compliance_rows(mixed_report["summary"]).items()}
        expected = {suite: [REQUIREMENT_LABELS[row["requirement"]] if index == 0 else row["result"]]
                    for suite, row in case["expected"].items()}
        context["stage"] = "merge"
    else:
        document = validator._read_html(html / "acs_summary.html")
        if aspect in ("overall", "aggregate-details"):
            rows = next(table["rows"] for table in document.tables if any(
                cell["text"] == "SRS requirements compliance results" for row in table["rows"] for cell in row))
            if aspect == "overall":
                actual = [row[-1]["text"] for row in rows if row[0]["text"] == "SRS requirements compliance results"]
                expected = [case["overall"]]
            else:
                actual = Counter(row[0]["text"].strip() for row in rows if len(row) == 1)
                details = []
                for category in ("M", "R"):
                    names = [name for name, row in case["expected"].items() if row["requirement"] == category]
                    parts = []
                    for state, prefix in (("missing", "not run"), ("failed", "failed")):
                        affected = [name for name in names if case["states"][name] == state]
                        if affected:
                            parts.append(prefix + ": " + ", ".join(affected))
                    if parts and case["overall"] == "Not Compliant":
                        details.append(REQUIREMENT_LABELS[category] + ": " + "; ".join(parts))
                expected = Counter(details)
        elif aspect == "badges":
            parser = BadgeParser()
            parser.feed((html / "acs_summary.html").read_text())
            actual = {owner: [" ".join("".join(text).split()) for text in values]
                      for owner, values in parser.badges.items()}
            expected = {owner: [label] for owner, label in case["badges"].items()}
        else:
            actual = Counter()
            for page in html.glob("*_detailed.html"):
                for table in validator._read_html(page).tables:
                    for row in table["rows"]:
                        if len(row) == 3 and all(cell["tag"] == "td" for cell in row):
                            values = tuple(cell["text"] for cell in row)
                            if values[1] in REQUIREMENT_LABELS.values():
                                actual[values] += 1
            expected = Counter()
            for suite, row in case["expected"].items():
                present = suite in expected_counts
                if not present and not (suite.startswith("SBMR-") and expected_counts):
                    continue
                label = "Not Compliant (Not Run)" if case["states"][suite] == "missing" and row["requirement"] == "M" else row["result"].split(":", 1)[0]
                expected[(suite, REQUIREMENT_LABELS[row["requirement"]], label)] += 1
    qa.check(actual, expected, issue_id=f"mixed-report-{aspect}", **context)
