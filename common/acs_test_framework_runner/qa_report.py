"""Report required CI stages and structured pytest findings without changing verdicts."""

import argparse
import html
import json
import os
from pathlib import Path
import re
import shlex
import xml.etree.ElementTree as ET


ANSI = re.compile(r"(?:\x1b|#x1B)\[[0-?]*[ -/]*[@-~]")
FIELDS = ("issue_id", "suite", "mode", "stage", "expected", "actual", "status", "reproduce", "evidence")
STAGES = (
    ("Dependency setup", "DEPENDENCIES_RESULT", "setup"),
    ("Change classification", "IMPACT_RESULT", "classification"),
    ("Parser YAML groups", "YAML_RESULT", "yaml"),
    ("Contracts and end-to-end scenarios", "E2E_RESULT", "end-to-end"),
    ("Desktop and mobile browser checks", "BROWSER_RESULT", "browser"),
)


def diagnostic(problem):
    detail = ANSI.sub("", "\n".join(filter(None, (problem.get("message"), problem.text))))
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    cause = next((line for line in lines if "issue=" in line), None)
    if cause is None:
        cause = next((line for line in lines if re.match(
            r"(?:E\s+)?(?![\w.]*AssertionError\b)[\w.]*(?:Error|Exception|TimeoutExpired):\s+\S",
            line)), None)
    return cause or next((line for line in lines if "assert " in line), lines[0] if lines else problem.tag), detail


def metadata_errors(value):
    if not isinstance(value, dict):
        return list(FIELDS)
    missing = [field for field in FIELDS if field not in value]
    for field in ("issue_id", "suite", "mode", "stage"):
        if field in value and (not isinstance(value[field], str) or not value[field].strip()):
            missing.append(field)
    if "status" in value and value["status"] not in ("FAIL", "BLOCKED"):
        missing.append("status")
    if "evidence" in value and (not isinstance(value["evidence"], list)
                                or not all(isinstance(path, str) for path in value["evidence"])):
        missing.append("evidence")
    if "reproduce" in value:
        reproduce = value["reproduce"]
        if (not isinstance(reproduce, dict) or not isinstance(reproduce.get("cwd"), str)
                or not reproduce["cwd"] or not isinstance(reproduce.get("argv"), list)
                or not reproduce["argv"]
                or not all(isinstance(arg, str) and arg for arg in reproduce["argv"])):
            missing.append("reproduce")
    return sorted(set(missing))


def blocker(issue_id, stage, expected, actual, evidence=()):
    return {"kind": "required-check", "issue_id": issue_id, "scenario_id": issue_id,
            "status": "BLOCKED", "suite": "ALL", "mode": "ALL", "stage": stage,
            "expected": expected, "actual": actual, "reproduce": None,
            "evidence": list(evidence), "metadata_incomplete": ["reproduce"],
            "diagnostic": str(actual), "details": str(actual)}


def read_case(case, report):
    name = "::".join(filter(None, (case.get("classname"), case.get("name")))) or report
    problems = [child for child in case if child.tag in ("failure", "error", "skipped")]
    properties = [prop.get("value", "") for prop in case.findall("properties/property")
                  if prop.get("name") == "qa_finding"]
    if not problems and not properties:
        return []
    problem = problems[0] if problems else None
    short, full = diagnostic(problem) if problem is not None else ("Recorded finding on a passing JUnit case", "")
    if len(problems) > 1:
        full = "\n\n".join(diagnostic(item)[1] for item in problems)
    status = "FAIL" if any(item.tag == "failure" for item in problems) else "BLOCKED"
    base = {"kind": "test", "scenario_id": name, "report": report,
            "source_file": case.get("file"), "source_line": case.get("line"),
            "status": status, "issue_id": name.split("[", 1)[0],
            "suite": None, "mode": None, "stage": None, "expected": None, "actual": None,
            "reproduce": None, "evidence": [report],
            "diagnostic": ((problem.tag + ": ") if problem is not None else "") + short,
            "details": full, "system_out": case.findtext("system-out") or "", "structured": False,
            "metadata_incomplete": ["issue_id", "suite", "mode", "stage", "expected", "actual", "reproduce"]}
    if not properties:
        return [base]
    findings = []
    for raw in properties:
        finding = dict(base)
        try:
            value = json.loads(raw)
            json.dumps(value, allow_nan=False)
            invalid = metadata_errors(value)
            if invalid:
                if isinstance(value, dict):
                    finding.update({field: value[field] for field in FIELDS if field in value and field not in invalid})
                finding["metadata_incomplete"] = invalid
                finding["metadata_error"] = "Invalid or missing qa_finding fields: " + ", ".join(invalid)
                finding["recorded_metadata"] = value
            else:
                finding.update({field: value[field] for field in FIELDS})
                finding["metadata_incomplete"] = []
                finding["structured"] = True
        except (ValueError, TypeError) as error:
            finding["metadata_error"] = "Invalid qa_finding JSON: " + str(error)
            finding["metadata_incomplete"] = list(FIELDS)
        findings.append(finding)
    return findings


def collect(reports, environment):
    stages = list(STAGES)
    selected = environment.get("ONBOARDING_REQUIRED", "")
    onboarding_ran = environment.get("ONBOARDING_RESULT") not in (None, "", "skipped")
    if selected == "true" or onboarding_ran:
        stages.append(("New-suite onboarding", "ONBOARDING_RESULT", "onboarding"))
    stage_results = []
    blockers = []
    for label, variable, stage in stages:
        outcome = environment.get(variable) or "missing"
        stage_results.append({"name": label, "stage": stage, "outcome": outcome})
        if outcome != "success":
            blockers.append(blocker("ci.required-stage." + stage, stage, "success", f"{label}: {outcome}"))
    if selected not in ("true", "false"):
        blockers.append(blocker("ci.onboarding-selection", "classification", "true or false",
                                "New-suite classification result is missing or invalid"))
    files = sorted(reports.glob("*.xml"), key=lambda path: (path.name != "unit-e2e.xml", path.name))
    required = ["unit-e2e.xml"] + (["new-suite-onboarding.xml"] if selected == "true" or onboarding_ran else [])
    for name in required:
        if not (reports / name).is_file():
            blockers.append(blocker("ci.missing-report." + name, "reporting", "Required JUnit report exists",
                                    "Missing required report: " + name))
    if not any(path.name not in ("unit-e2e.xml", "new-suite-onboarding.xml") for path in files):
        blockers.append(blocker("ci.missing-yaml-results", "yaml", "At least one executed YAML report",
                                "No YAML JUnit reports were produced"))
    report_results = []
    findings = []
    for path in files:
        try:
            cases = list(ET.parse(path).iter("testcase"))
        except (ET.ParseError, OSError) as error:
            blockers.append(blocker("ci.invalid-report." + path.name, "reporting", "Readable JUnit XML",
                                    f"Cannot read JUnit report: {error}", [path.name]))
            continue
        counts = {tag: sum(case.find(tag) is not None for case in cases) for tag in ("failure", "error", "skipped")}
        report_results.append({"name": path.name, "tests": len(cases), **counts})
        if not cases:
            blockers.append(blocker("ci.zero-checks." + path.name, "reporting", "At least one executed test",
                                    "JUnit report contains zero testcases: " + path.name, [path.name]))
        for case in cases:
            findings.extend(read_case(case, path.name))
    groups = {}
    for index, finding in enumerate(findings):
        key = (finding["issue_id"], finding["stage"] if finding.get("structured") else None)
        group = groups.setdefault(key, {"issue_id": key[0], "stage": key[1],
                                       "grouping": "issue-stage" if finding.get("structured") else "test-family",
                                       "finding_indices": [], "scenario_ids": []})
        group["finding_indices"].append(index)
        group["scenario_ids"].append(finding["scenario_id"])
    status = "FAIL" if any(item["status"] == "FAIL" for item in findings) else "BLOCKED" if findings or blockers else "PASS"
    return {"status": status, "stages": stage_results, "reports": report_results,
            "findings": findings, "groups": list(groups.values()), "required_check_blockers": blockers}


def display(value, limit=None):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True)
    text = " ".join(ANSI.sub("", text).split())
    if limit is not None and len(text) > limit:
        text = text[:limit] + "... (full value in qa-findings.json)"
    return html.escape(text).replace("|", "&#124;")


def annotate(message, title="Log-parser QA"):
    message = ANSI.sub("", message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title={title}::{message}")


def render(result, guide_url):
    rows = ["## Log-parser QA", "", "Result: **" + result["status"] + "**", "",
            "| Check | Result |", "| --- | --- |"]
    rows.extend(f"| {stage['name']} | {display(stage['outcome'])} |" for stage in result["stages"])
    rows.extend(["", "| JUnit report | Tests | Failures | Errors | Skipped |", "| --- | ---: | ---: | ---: | ---: |"])
    rows.extend(f"| {display(report['name'])} | {report['tests']} | {report['failure']} | {report['error']} | {report['skipped']} |"
                for report in result["reports"])
    groups = result["groups"]
    if groups:
        rows.extend(["", "### Failure groups", "", "| Issue | Cases | First failing scenario | Diagnostic |", "| --- | ---: | --- | --- |"])
        for group in groups[:10]:
            finding = result["findings"][group["finding_indices"][0]]
            count = len(group["finding_indices"])
            name = finding["scenario_id"]
            detail = " ".join(finding["diagnostic"].split())[:240]
            annotate(f"{count} case(s); first {name}: {detail}")
            rows.append(f"| <code>{display(group['issue_id'])}</code> | {count} | <code>{display(name)}</code> | <code>{display(detail)}</code> |")
        for group in groups[:10]:
            finding = result["findings"][group["finding_indices"][0]]
            rows.extend(["", f"<details><summary>{display(group['issue_id'])}: evidence</summary>", "",
                         f"Suite: `{display(finding['suite'])}`; mode: `{display(finding['mode'])}`; stage: `{display(finding['stage'])}`.",
                         "", "Expected: " + display(finding["expected"], 600),
                         "", "Actual: " + display(finding["actual"], 600)])
            reproduce = finding["reproduce"]
            if reproduce:
                rows.extend(["", "Reproduce from " + display(reproduce["cwd"]) + ":",
                             "", "<code>" + display(shlex.join(reproduce["argv"])) + "</code>"])
            if finding["metadata_incomplete"]:
                rows.extend(["", "Metadata not recorded: " + ", ".join(finding["metadata_incomplete"]) + "."])
            if finding.get("metadata_error"):
                rows.extend(["", "Metadata error: " + display(finding["metadata_error"])])
            rows.extend(["", "Evidence: " + display(finding["evidence"], 600), "", "</details>"])
        rows.append(f"\nShowing {min(10, len(groups))} of {len(groups)} failure groups "
                    f"({len(result['findings'])} cases); qa-findings.json contains every failing scenario.")
    if result["required_check_blockers"]:
        rows.extend(["", "Required checks did not complete successfully:"])
        for item in result["required_check_blockers"]:
            rows.append("- " + display(item["actual"]))
            annotate(item["actual"], "Incomplete required check")
    for stage in result["stages"]:
        if stage["outcome"] == "failure" and stage["stage"] == "end-to-end":
            rows.extend(["", "Contract checks failed. Inspect the expected/actual evidence and reproduce the named scenario."])
    if result["status"] != "PASS":
        rows.extend(["", result["status"] + ": test results are missing, empty, invalid, failed, or skipped. All checks must complete without skips."])
    rows.extend(["", f"[Troubleshooting guide]({guide_url})", "",
                 "Full logs, JUnit XML, and qa-findings.json are in the downloadable QA artifact."])
    return "\n".join(rows) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--findings", type=Path, required=True)
    args = parser.parse_args(argv)
    result = collect(args.reports, os.environ)
    args.findings.parent.mkdir(parents=True, exist_ok=True)
    args.findings.write_text(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
    with args.summary.open("a", encoding="utf-8") as stream:
        stream.write(render(result, os.environ.get("GUIDE_URL", "docs/log_parser_pr_qa.md")))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
