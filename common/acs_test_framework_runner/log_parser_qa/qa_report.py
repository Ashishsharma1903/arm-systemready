# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Report required CI stages and structured pytest findings without changing verdicts."""

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET


if not __package__:
    # Direct CLI execution can load the parent framework when it is needed.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def json_value(value):
    """Load the shared evidence helper only when tests record a finding.

    The report CLI must still work if dependency installation failed, so normal
    summary generation cannot import the framework's third-party dependencies.
    """
    if __package__:
        from ..runner_checks import json_value as shared_json_value
    else:
        from runner_checks import json_value as shared_json_value
    return shared_json_value(value)


def run_command(argv, cwd, evidence_dir, name="command", timeout=120):
    """Keep the exact command and output, including setup failures and timeouts."""
    argv = [str(part) for part in argv]
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / f"{name}.command.json").write_text(
        json.dumps({"argv": argv, "cwd": str(cwd)}, indent=2) + "\n"
    )
    log = evidence_dir / f"{name}.log"
    try:
        result = subprocess.run(
            argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, check=False, env={**os.environ, "MPLBACKEND": "Agg",
                "PYTHONNOUSERSITE": "1", "PYTHONPATH": "", "NO_COLOR": "1",
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")},
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        log.write_text(output.decode(errors="replace") if isinstance(output, bytes) else output)
        raise
    except OSError as error:
        log.write_text(str(error) + "\n")
        raise
    log.write_text(result.stdout)
    return result


def run_checked_command(command, cwd, expected=0):
    """Keep command failures readable without importing a parser test module."""
    argv = [str(item) for item in command]
    result = subprocess.run(
        argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=120, check=False, env={**os.environ, "MPLBACKEND": "Agg",
            "PYTHONNOUSERSITE": "1", "PYTHONPATH": "", "NO_COLOR": "1",
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")},
    )
    # Compare the number, not CompletedProcess: pytest shortens that object's
    # paths differently in base and candidate reports. Retain the actual error.
    if result.returncode != expected:
        raise AssertionError(
            f"Command: {shlex.join(argv)}\nWorking directory: {cwd}\n{result.stdout}"
        )
    return result.stdout


def assert_runtime_matches(source, destination, evidence_path):
    """Compare every shipped byte; report paths and hashes, never file contents."""
    source, destination, evidence_path = Path(source), Path(destination), Path(evidence_path)
    expected = {path.relative_to(source): path for path in source.rglob("*")
                if path.is_file() and not any(part in {"tests", "__pycache__"}
                for part in path.relative_to(source).parts) and path.suffix != ".pyc"}
    actual = {path.relative_to(destination): path for path in destination.rglob("*")
              if path.is_file()}

    def metadata(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(chunk)
        return {"size": path.stat().st_size, "sha256": digest.hexdigest()}

    def same_bytes(first, second):
        # Read both files even when their sizes and timestamps happen to match.
        # A digest is useful evidence, but does not replace the byte comparison.
        with first.open("rb") as left, second.open("rb") as right:
            while True:
                left_chunk, right_chunk = left.read(64 * 1024), right.read(64 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True

    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    changed = [name for name in sorted(expected.keys() & actual.keys())
               if not same_bytes(expected[name], actual[name])]
    if not (missing or extra or changed):
        return
    finding = {
        "missing": [{"path": str(name), "source": metadata(expected[name])} for name in missing],
        "extra": [{"path": str(name), "package": metadata(actual[name])} for name in extra],
        "changed": [{"path": str(name), "source": metadata(expected[name]),
                     "package": metadata(actual[name])} for name in changed],
    }
    # Preserve every affected path and digest separately. Repeating complete
    # Python source bytes in each dependent pytest failure made reports huge.
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(finding, indent=2) + "\n")
    lines = ["Extracted runtime differs from the source parser: "
             f"{len(missing)} missing, {len(extra)} extra, {len(changed)} changed files."]
    for kind, entries in finding.items():
        for entry in entries[:8]:
            name = entry["path"]
            name = name if len(name) <= 160 else name[:77] + "..." + name[-80:]
            details = "; ".join(f"{side}: {value['size']} bytes, sha256 {value['sha256']}"
                                for side, value in entry.items() if side != "path")
            lines.append(f"{kind}: {name} ({details})")
        if len(entries) > 8:
            lines.append(f"{kind}: {len(entries) - 8} more paths in the evidence file")
    lines.append(f"All differing paths and content hashes: {evidence_path}")
    raise AssertionError("\n".join(lines))


class ContractEvidence:
    def __init__(self, node):
        self.node = node
        self.comparison = None

    def record(self, actual, expected, *, issue_id, suite, mode, stage,
               status="FAIL", reproduce=None, evidence=()):
        finding = {
            "issue_id": issue_id, "suite": suite, "mode": mode, "stage": stage,
            "status": status, "expected": json_value(expected), "actual": json_value(actual),
            "reproduce": reproduce or {
                "argv": ["python3", "-m", "pytest", "-q", self.node.nodeid], "cwd": ".",
            },
            "evidence": [str(path) for path in evidence],
        }
        self.node.user_properties.append(("qa_finding", json.dumps(finding, default=str)))
        return finding

    def check(self, actual, expected, **context):
        if actual != expected:
            finding = self.record(actual, expected, **context)
            raise AssertionError(
                f"{finding['status']} {finding['issue_id']} "
                f"[{finding['suite']} / {finding['mode']} / {finding['stage']}]\n"
                f"Expected: {finding['expected']!r}\nActual: {finding['actual']!r}"
            )


ANSI = re.compile(r"(?:\x1b|#x1B)\[[0-?]*[ -/]*[@-~]")
FIELDS = ("issue_id", "suite", "mode", "stage", "expected", "actual", "status", "reproduce", "evidence")
STAGES = (
    ("Dependency setup", "DEPENDENCIES_RESULT", "setup"),
    ("Change classification", "IMPACT_RESULT", "classification"),
    ("Parser YAML groups", "YAML_RESULT", "yaml"),
    ("Contracts and end-to-end scenarios", "E2E_RESULT", "end-to-end"),
    ("Desktop and mobile browser checks", "BROWSER_RESULT", "browser"),
    ("Base-versus-candidate replay", "REPLAY_RESULT", "replay"),
)
BASELINE_STAGES = (
    ("Dependency setup", "DEPENDENCIES_RESULT", "setup"),
    ("Change classification", "IMPACT_RESULT", "classification"),
    ("QA framework self-tests", "SELFTEST_RESULT", "self-tests"),
    ("Base-versus-candidate checks", "REGRESSION_RESULT", "regression"),
    ("Base-versus-candidate replay", "REPLAY_RESULT", "replay"),
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


def required_stage_blocker(reports, label, stage, outcome):
    """Show the saved setup/classification error instead of only saying failure."""
    item = blocker("ci.required-stage." + stage, stage, "success", f"{label}: {outcome}")
    name = {"classification": "change-impact.stderr.log", "setup": "dependencies.log"}.get(stage)
    if name:
        path = reports / name
        try:
            # The full file stays in the artifact. Limit only its summary excerpt.
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 16000))
                detail = stream.read().decode("utf-8", errors="replace").strip()
        except OSError:
            detail = ""
        if detail:
            lines = [line.strip() for line in ANSI.sub("", detail).splitlines() if line.strip()]
            if lines:
                cause = next((line for line in reversed(lines)
                              if "Change classification failed:" in line or "Error:" in line
                              or "ERROR:" in line or "fatal:" in line), lines[-1])
                item.update(actual=f"{label}: {outcome}. {cause}", diagnostic=cause,
                            details=detail, evidence=[name])
    return item


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


def required_yaml_reports(reports, names=None):
    """Use the same manifest selection and filenames as the YAML execution stage."""
    if names is None:
        if __package__:
            from .. import pytest_runner as runner
        else:
            import pytest_runner as runner
        groups, warnings = runner.discover_test_groups(runner.discover_yaml_files())
        if warnings:
            raise ValueError("; ".join(warnings))
        selected = runner.filter_test_groups_for_target(groups, "common/log_parser")
        paths = runner.build_selected_report_paths(
            runner.selected_runs_from_groups(selected), reports, [])
        names = [path.name for path in paths.values()]
    names = list(names)
    if not names:
        raise ValueError("No parser YAML manifests were selected")
    if (len(set(names)) != len(names)
            or any(not isinstance(name, str) or not name.endswith(".xml")
                   or Path(name).name != name or "/" in name or "\\" in name
                   or name == "unit-e2e.xml" for name in names)):
        raise ValueError("Expected YAML reports must be unique XML filenames, not unit-e2e.xml")
    return sorted(names)


def read_reports(files):
    """Retain every testcase finding, including malformed or empty report failures."""
    report_results, findings, blockers = [], [], []
    for path in files:
        try:
            cases = list(ET.parse(path).iter("testcase"))
        except (ET.ParseError, OSError) as error:
            blockers.append(blocker("ci.invalid-report." + path.name, "reporting", "Readable JUnit XML",
                                    f"Cannot read JUnit report: {error}", [str(path)]))
            continue
        counts = {tag: sum(case.find(tag) is not None for case in cases) for tag in ("failure", "error", "skipped")}
        report_results.append({"name": path.name, "tests": len(cases), **counts})
        if not cases:
            blockers.append(blocker("ci.zero-checks." + path.name, "reporting", "At least one executed test",
                                    "JUnit report contains zero testcases: " + path.name, [str(path)]))
        for case in cases:
            findings.extend(read_case(case, path.name))
    return report_results, findings, blockers


def collect(reports, environment, required_yaml=None):
    stages = list(STAGES)
    stage_results = []
    blockers = []
    for label, variable, stage in stages:
        outcome = environment.get(variable) or "missing"
        stage_results.append({"name": label, "stage": stage, "outcome": outcome})
        if outcome != "success":
            blockers.append(required_stage_blocker(reports, label, stage, outcome))
    files = sorted(reports.glob("*.xml"), key=lambda path: (path.name != "unit-e2e.xml", path.name))
    required = ["unit-e2e.xml"]
    try:
        required.extend(required_yaml_reports(reports, required_yaml))
    except Exception as error:  # A broken inventory must not hide missing checks.
        blockers.append(blocker("ci.invalid-yaml-inventory", "yaml",
                                "Discover every selected YAML report",
                                "Cannot establish required YAML reports: " + str(error)))
    for name in required:
        if not (reports / name).is_file():
            blockers.append(blocker("ci.missing-report." + name, "reporting", "Required JUnit report exists",
                                    "Missing required report: " + name))
    report_results, findings, report_blockers = read_reports(files)
    blockers.extend(report_blockers)
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
            "required_reports": required,
            "findings": findings, "groups": list(groups.values()), "required_check_blockers": blockers}


def collect_baseline(reports, environment):
    """Block regressions and missing evidence; retain matching base defects as information."""
    stage_results, blockers = [], []
    for label, variable, stage in BASELINE_STAGES:
        outcome = environment.get(variable) or "missing"
        stage_results.append({"name": label, "stage": stage, "outcome": outcome})
        if outcome != "success":
            blockers.append(required_stage_blocker(reports, label, stage, outcome))
    selftest = reports / "qa-selftests.xml"
    report_results, findings, report_blockers = read_reports([selftest] if selftest.is_file() else [])
    blockers.extend(report_blockers)
    if not selftest.is_file():
        blockers.append(blocker("ci.missing-report.qa-selftests.xml", "self-tests",
                                "QA self-test report exists", "Missing required report: qa-selftests.xml"))
    comparison = None
    try:
        comparison = json.loads((reports / "regression.json").read_text(encoding="utf-8"))
        if (not isinstance(comparison, dict) or comparison.get("schema_version") != 1
                or comparison.get("status") not in ("NO_NEW_REGRESSIONS", "REGRESSION", "BLOCKED")
                or not all(isinstance(comparison.get(name), list) for name in
                           ("existing_issues", "resolved_issues", "regressions", "blockers", "coverage_limits"))
                or not isinstance(comparison.get("counts"), dict)):
            raise ValueError("Unsupported or incomplete regression comparison")
        if not all(isinstance(item, dict) for key in ("existing_issues", "resolved_issues", "regressions", "blockers")
                   for item in comparison[key]):
            raise ValueError("Comparison findings must be objects")
        if not all(type(comparison["counts"].get(key)) is int and comparison["counts"][key] >= 0
                   for key in ("base_cases", "candidate_cases", "passed", "existing", "resolved", "new", "worsened", "blocked")):
            raise ValueError("Comparison counts must be non-negative integers")
        if comparison["status"] == "NO_NEW_REGRESSIONS" and not all(
                comparison["counts"][key] > 0 for key in ("base_cases", "candidate_cases")):
            raise ValueError("A passing comparison requires executed base and candidate checks")
        for name, key in (("existing_issues", "existing"), ("resolved_issues", "resolved"),
                          ("blockers", "blocked")):
            if comparison["counts"].get(key) != len(comparison[name]):
                raise ValueError("Comparison count does not match " + name)
        if comparison["counts"].get("new", 0) + comparison["counts"].get("worsened", 0) != len(comparison["regressions"]):
            raise ValueError("Comparison regression count is inconsistent")
        if comparison["status"] == "NO_NEW_REGRESSIONS" and (comparison["regressions"] or comparison["blockers"]):
            raise ValueError("Comparison claims no regressions despite blocking findings")
        if comparison["status"] != "NO_NEW_REGRESSIONS" and not (comparison["regressions"] or comparison["blockers"]):
            raise ValueError("Comparison failed without recording the blocking evidence")
        for item in comparison["regressions"]:
            previous = item.get("base_findings") or []
            proposed = item.get("candidate_findings") or []

            def readable(values, label, outcome, detail):
                if values:
                    return "\n\n".join(
                        f"{value.get('suite', 'ALL')} / {value.get('mode', 'ALL')} / {value.get('stage', 'check')}\n"
                        f"Required result: {json.dumps(value.get('expected'), ensure_ascii=True)}\n"
                        f"{label} result: {json.dumps(value.get('actual'), ensure_ascii=True)}"
                        for value in values)
                explanation = detail or ("The check passed." if outcome == "pass" else
                                          "Diagnostic was not recorded; full comparison evidence is in regression.json.")
                return f"{label} outcome: {outcome or 'not recorded'}\n" + explanation

            finding = blocker(item.get("case_id", "ci.new-regression"), "regression",
                              readable(previous, "Base", item.get("base_outcome"), item.get("base_diagnostic")),
                              readable(proposed, "Proposed", item.get("candidate_outcome"),
                                       item.get("candidate_diagnostic") or item.get("diagnostic")),
                              item.get("evidence", []))
            sample = (proposed or previous or [{}])[0]
            label = "New failure" if item.get("classification") == "NEW_FAILURE" else "Changed existing failure"
            finding.update(status="FAIL", kind="regression", scenario_id=item.get("case_id", "unknown"),
                           suite=sample.get("suite", "ALL"), mode=sample.get("mode", "ALL"),
                           stage=sample.get("stage", "regression"),
                           diagnostic=label + ": " + (item.get("diagnostic") or item.get("case_id", "Parser check changed")),
                           base_signature=item.get("base_signature"), candidate_signature=item.get("candidate_signature"),
                           details=item.get("diagnostic", ""))
            findings.append(finding)
        for item in comparison["blockers"]:
            blockers.append(blocker(item.get("issue_id", "ci.comparison-blocked"), "regression",
                                    "Complete comparable evidence", item.get("reason", "Comparison blocked"),
                                    item.get("evidence", [])))
    except (OSError, ValueError, TypeError, AttributeError) as error:
        blockers.append(blocker("ci.invalid-comparison", "regression", "Complete regression.json",
                                "Cannot verify base/candidate comparison: " + str(error)))
        comparison = None
    groups = [{"issue_id": item["issue_id"], "stage": item["stage"], "grouping": "regression",
               "finding_indices": [index], "scenario_ids": [item["scenario_id"]]}
              for index, item in enumerate(findings)]
    status = ("FAIL" if any(item["status"] == "FAIL" for item in findings)
              else "BLOCKED" if findings or blockers else "NO_NEW_REGRESSIONS")
    return {"status": status, "stages": stage_results, "reports": report_results,
            "findings": findings, "groups": groups, "required_check_blockers": blockers,
            "required_reports": ["qa-selftests.xml", "regression.json"],
            "comparison": comparison, "baseline_aware": True}


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
    comparison = result.get("comparison")
    if comparison:
        rows.extend(["", "### Base-versus-candidate comparison", "",
                     "Only new or worsened regressions block this job. Matching existing issues remain visible, not fixed or ignored.",
                     "", "| New | Worsened | Existing issues | Resolved | Blocked |", "| ---: | ---: | ---: | ---: | ---: |",
                     "| " + " | ".join(str(comparison["counts"][key]) for key in
                                       ("new", "worsened", "existing", "resolved", "blocked")) + " |",
                     "", "[Complete comparison and existing-issue evidence](regression.json)"])
        if comparison["existing_issues"]:
            rows.extend(["", "Existing issues (informative; reproduced on the base revision):"])
            rows.extend("- " + display(item.get("case_id", "Existing issue")) + ": "
                        + display(item.get("diagnostic", "Same failure reproduced on both revisions"))
                        for item in comparison["existing_issues"][:10])
        if comparison["coverage_limits"]:
            rows.extend(["", "Coverage limits: " + display(comparison["coverage_limits"])])
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
            if item["evidence"]:
                rows.append("  Supporting file: " + display(item["evidence"]))
            annotate(item["actual"], "Incomplete required check")
    for stage in result["stages"]:
        if stage["outcome"] == "failure" and stage["stage"] == "end-to-end":
            rows.extend(["", "Contract checks failed. Inspect the expected/actual evidence and reproduce the named scenario."])
    if result["status"] not in ("PASS", "NO_NEW_REGRESSIONS"):
        rows.extend(["", result["status"] + ": test results are missing, empty, invalid, failed, or skipped. All checks must complete without skips."])
    rows.extend(["", f"[Troubleshooting guide]({guide_url})", "",
                 "Full logs, JUnit XML, and qa-findings.json are in the downloadable QA artifact."])
    return "\n".join(rows) + "\n"


def render_html(result):
    """Give artifact readers a compact summary before the full technical evidence."""
    def escaped(value):
        return html.escape(value if isinstance(value, str) else json.dumps(value, indent=2))

    tests = sum(report["tests"] for report in result["reports"])
    rows = []
    for stage in result["stages"]:
        rows.append(f'<tr><td>{escaped(stage["name"])}</td><td>{escaped(stage["outcome"])}</td></tr>')
    cards = []
    for group in result["groups"]:
        examples = []
        for index in group["finding_indices"]:
            finding = result["findings"][index]
            command = finding.get("reproduce")
            replay = shlex.join(command["argv"]) if command else "Not recorded"
            examples.append(
                f'<details><summary>{escaped(finding["scenario_id"])}</summary>'
                f'<p>{escaped(finding["diagnostic"])}</p><div class="comparison">'
                f'<div><h4>Expected</h4><pre>{escaped(finding["expected"])}</pre></div>'
                f'<div><h4>Observed</h4><pre>{escaped(finding["actual"])}</pre></div></div>'
                f'<h4>Reproduce</h4><pre>{escaped(replay)}</pre>'
                f'<h4>Supporting files</h4><pre>{escaped(finding["evidence"])}</pre></details>')
        sample = result["findings"][group["finding_indices"][0]]
        cards.append(f'<article data-status="{escaped(sample["status"])}"><h3>'
                     f'{escaped(group["issue_id"])} <small>{len(examples)} assertion(s)</small></h3>'
                     f'<p>{escaped(sample["suite"])} · {escaped(sample["mode"])} · '
                     f'{escaped(sample["stage"])}</p>{"".join(examples)}</article>')
    blockers = "".join(
        f'<li>{escaped(item["actual"])}'
        + (f'<pre>Supporting files: {escaped(item["evidence"])}</pre>' if item["evidence"] else '')
        + '</li>' for item in result["required_check_blockers"])
    comparison = result.get("comparison")
    comparison_html = ""
    if comparison:
        counts = comparison["counts"]
        known = "".join('<details><summary>' + escaped(item.get("case_id", "Existing issue"))
                        + '</summary><p>' + escaped(item.get("diagnostic", "Same issue on both revisions"))
                        + '</p><pre>' + escaped(item) + '</pre></details>'
                        for item in comparison["existing_issues"])
        comparison_html = ('<section><h2>What changed compared with the base?</h2><p>'
                           + str(counts["new"]) + ' new · ' + str(counts["worsened"]) + ' worsened · '
                           + str(counts["existing"]) + ' existing · ' + str(counts["resolved"]) + ' resolved · '
                           + str(counts["blocked"]) + ' blocked</p><p>Only new or worsened regressions block the job. '
                           'Existing issues are informative, not fixed. Missing checks still block approval.</p>'
                           '<a href="regression.json">Full comparison · JSON</a><h3>Existing issues</h3>'
                           + (known or '<p>No existing issues reproduced.</p>')
                           + '<h3>Coverage limits</h3><pre>' + escaped(comparison["coverage_limits"]) + '</pre></section>')
    return '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Log-parser CI results</title>
<style>body{margin:0;background:#f3f6fc;color:#193653;font:16px/1.5 system-ui,sans-serif}
main{max-width:1180px;margin:auto;padding:28px}header{background:#12365c;color:white;padding:28px;border-radius:16px}
h1{margin:8px 0}article,section{background:white;border:1px solid #d9e2ef;border-radius:12px;padding:20px;margin-top:18px}
article{border-left:5px solid #b52c25}article[data-status=BLOCKED]{border-left-color:#8860b0}
h3{margin:0}small{float:right;font-size:13px;color:#60718a}details{border-top:1px solid #dfe5ef;padding:10px 0}
summary{cursor:pointer;font-weight:600;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f6fc;padding:12px;border-radius:8px}
.comparison{display:grid;grid-template-columns:1fr 1fr;gap:16px}table{width:100%;border-collapse:collapse}td{border-bottom:1px solid #e5ebf2;padding:8px}
button,a{display:inline-block;padding:8px 14px;border:1px solid #cbd7e8;border-radius:7px;background:white;color:#143c65;text-decoration:none;cursor:pointer;margin:4px}
@media(max-width:650px){main{padding:12px}.comparison{grid-template-columns:1fr}small{float:none;display:block}}
@media print{button{display:none}details{display:block}article{break-inside:avoid}}</style><main>
<header><div>SYSTEMREADY ACS · PARSER CI</div><h1>''' + escaped(result["status"]) + ''' — Log-parser regression report</h1>
<p>''' + str(tests) + ''' assertions · ''' + str(len(result["groups"])) + ''' finding groups</p>
<p>These are parser checks, not hardware certification. Repeated assertions may describe the same bug.</p></header>
<section><h2>Start here</h2><p>Check incomplete stages first. Then open a finding to see the expected result, observed result and reproduction command.</p>
<a href="qa-findings.json">All findings · JSON</a><a href="replay/result.json">Base/candidate comparison</a>
<table>''' + "".join(rows) + '''</table><ul>''' + blockers + '''</ul></section>
<section><button onclick="filter('ALL')">All findings</button><button onclick="filter('FAIL')">Failures</button>
<button onclick="filter('BLOCKED')">Blocked checks</button><p id="visible" aria-live="polite"></p></section>
''' + comparison_html + ("".join(cards) or '<section>No new failed assertions were recorded. Check required stages and existing issues above.</section>') + '''
<script>function filter(status){let n=0;document.querySelectorAll('article').forEach(e=>{e.hidden=status!=='ALL'&&e.dataset.status!==status;if(!e.hidden)n++});document.getElementById('visible').textContent=n+' finding groups shown'}filter('ALL')</script></main></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--findings", type=Path, required=True)
    parser.add_argument("--baseline-aware", action="store_true",
                        help="Require a complete base/candidate comparison; existing issues are informative.")
    parser.add_argument("--required-yaml", action="append", metavar="REPORT.xml",
                        help="Explicit selected YAML report name; repeat for each manifest. "
                             "Default: discover all parser manifests using the runner selection.")
    args = parser.parse_args(argv)
    result = (collect_baseline(args.reports, os.environ) if args.baseline_aware
              else collect(args.reports, os.environ, args.required_yaml))
    args.findings.parent.mkdir(parents=True, exist_ok=True)
    args.findings.write_text(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
    (args.reports / "index.html").write_text(render_html(result), encoding="utf-8")
    with args.summary.open("a", encoding="utf-8") as stream:
        stream.write(render(result, os.environ.get("GUIDE_URL", "docs/log_parser_pr_qa.md")))
    return 0 if result["status"] in ("PASS", "NO_NEW_REGRESSIONS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
