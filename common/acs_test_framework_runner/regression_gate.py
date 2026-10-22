# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Compare identical QA checks on the base and candidate parser runtimes.

An unchanged, reproduced parser defect is information, not a new regression.
Missing checks and infrastructure failures are never evidence that a change is
safe. This module reads reports only; the separate runner executes both copies.
"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

try:
    from .qa_report import metadata_errors
except ImportError:
    from qa_report import metadata_errors


STAGES = ("yaml", "contracts")
OUTCOME_TAGS = ("failure", "error", "skipped")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}| UTC)\b")
VOLATILE_KEYS = {"generated_at", "run_timestamp", "timestamp", "started_at", "finished_at"}
INFRA_ERROR = re.compile(
    r"(?:^|\n)(?:E\s+)?(?:[\w.]+\.)?"
    r"(?:ImportError|ModuleNotFoundError|TimeoutExpired|TimeoutError|KeyboardInterrupt|"
    r"Interrupted|UsageError|CollectionError)(?::|$)", re.MULTILINE)
PRODUCTION_FILE = re.compile(
    r"(?:<PARSER>(?:/|\b)|(?:/|\b)log_parser/|\b(?:main_log_parser\.sh|"
    r"merge_jsons\.py|logs_to_json\.py|json_to_html\.py|standalone_runner\.py|"
    r"apply_waivers\.py)\b)")
CHROMIUM_PREFIX = re.compile(r"\[\d+:\d+:\d{4}/\d{6}\.\d+:(?=(?:VERBOSE\d*|INFO|WARNING|ERROR|FATAL):)")
DEVTOOLS_BANNER = re.compile(
    r"(?m)^DevTools listening on ws://(?:127\.0\.0\.1|localhost|\[::1\]):\d+"
    r"/devtools/browser/[0-9a-fA-F-]{36}\s*$")


def canonical(value):
    """Keep the complete value: changing an already-red actual must be noticed."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Normalizer:
    """Remove recorded run locations and timestamps, not arbitrary result data."""

    def __init__(self, manifest, extra_roots=()):
        roots = []
        for field, label in (("parser_root", "PARSER"), ("work_root", "WORK"),
                             ("repository_root", "REPOSITORY"), ("python_executable", "PYTHON")):
            if manifest.get(field):
                roots.append((str(manifest[field]).rstrip("/"), "<" + label + ">"))
        for specification in extra_roots:
            if "=" not in specification:
                raise ValueError("Root mappings must be NAME=/absolute/path")
            label, root = specification.split("=", 1)
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", label) or not Path(root).is_absolute():
                raise ValueError("Root mappings must be NAME=/absolute/path")
            roots.append((root.rstrip("/"), "<" + label + ">"))
        if any(root in ("", "/") for root, _ in roots):
            raise ValueError("A normalisation root must name a specific directory or executable")
        self.roots = sorted(set(roots), key=lambda item: (-len(item[0]), item[1]))

    def __call__(self, value, key=None):
        if isinstance(value, dict):
            return {str(name): self(item, str(name)) for name, item in value.items()}
        if isinstance(value, list):
            return [self(item) for item in value]
        if not isinstance(value, str):
            return value
        text = ANSI.sub("", value).replace("\r\n", "\n")
        for root, label in self.roots:
            # Do not replace /work/run inside the different path /work/runner.
            text = re.sub(re.escape(root) + r"(?=$|[/\s'\"\],):;])", lambda _: label, text)
        text = TIMESTAMP.sub("<TIMESTAMP>", text)
        if key in VOLATILE_KEYS and re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ].+", text):
            try:
                datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                pass  # Malformed date values remain visible to the contract.
            else:
                text = "<TIMESTAMP>"
        # Python object identities are volatile; device addresses are not.
        text = re.sub(r"(?<= at )0x[0-9a-fA-F]+(?=>)", "<OBJECT_ADDRESS>", text)
        # Chromium adds process IDs and wall-clock time before its actual error.
        # Keep its severity, source file and message; do not erase arbitrary IDs.
        text = CHROMIUM_PREFIX.sub("[<PID>:<TID>:<TIME>:", text)
        text = DEVTOOLS_BANNER.sub("DevTools listening on <LOCAL_BROWSER_SESSION>", text)
        return text


def blocked(issue_id, reason, evidence=(), case_id=None):
    item = {"issue_id": issue_id, "reason": reason, "evidence": [str(path) for path in evidence]}
    if case_id:
        item["case_id"] = case_id
    return item


def read_manifest(path, side, blockers):
    try:
        value = json.loads(Path(path).read_text())
        canonical(value)
    except (OSError, ValueError, TypeError) as error:
        blockers.append(blocked("manifest.unreadable", f"{side}: {error}", [path]))
        return {}
    if not isinstance(value, dict):
        blockers.append(blocked("manifest.invalid", f"{side}: the manifest is not an object", [path]))
        return {}
    for field in ("harness_digest", "fixtures_digest", "python_version", "parser_revision", "parser_root", "work_root"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            blockers.append(blocked("manifest.missing-field", f"{side}: missing or invalid {field}", [path]))
    for field in ("parser_root", "work_root", "repository_root", "python_executable"):
        if field in value and (not isinstance(value[field], str) or not Path(value[field]).is_absolute()):
            blockers.append(blocked("manifest.invalid-root", f"{side}: {field} must be absolute", [path]))
    command = value.get("test_command")
    if not ((isinstance(command, str) and command.strip()) or
            (isinstance(command, list) and command and all(isinstance(arg, str) and arg for arg in command))):
        blockers.append(blocked("manifest.invalid-command", f"{side}: no complete test_command", [path]))
    execution = value.get("execution")
    if not isinstance(execution, dict):
        blockers.append(blocked("execution.missing", f"{side}: execution results are missing", [path]))
    else:
        for stage in STAGES:
            code = execution.get(stage)
            if type(code) is not int or code not in (0, 1):
                blockers.append(blocked("execution.incomplete", f"{side}: {stage} exit={code!r}; only a complete exit 0/1 is comparable", [path]))
        for stage, code in execution.items():
            if stage not in STAGES and (type(code) is not int or code not in (0, 1)):
                blockers.append(blocked("execution.incomplete", f"{side}: additional stage {stage} exit={code!r} did not complete", [path]))
    expected = value.get("expected_reports")
    if (not isinstance(expected, list) or not expected or
            not all(isinstance(name, str) and name.endswith(".xml") and Path(name).name == name
                    and "/" not in name and "\\" not in name for name in expected) or
            len(expected) != len(set(expected)) or "unit-e2e.xml" not in expected or len(expected) < 2):
        blockers.append(blocked("manifest.invalid-inventory", f"{side}: expected_reports must list unit-e2e.xml and every selected YAML report as unique top-level XML filenames", [path]))
    return value


def parse_case(case, report, normalise, blockers, side):
    classname, name = case.get("classname", ""), case.get("name", "")
    case_id = "::".join((report, classname, name))
    if not name:
        blockers.append(blocked("inventory.unnamed-case", f"{side}: testcase has no name", [report]))
    problems = [child for child in case if child.tag in OUTCOME_TAGS]
    if len(problems) > 1:
        blockers.append(blocked("junit.ambiguous-outcome", f"{side}: one case has multiple outcomes", [report], case_id))
    outcome = problems[0].tag if problems else "pass"
    findings = []
    for prop in case.findall("properties/property"):
        if prop.get("name") != "qa_finding":
            continue
        try:
            finding = json.loads(prop.get("value", prop.text or ""))
            canonical(finding)
            invalid = metadata_errors(finding)
            if invalid:
                raise ValueError("Invalid fields: " + ", ".join(invalid))
            findings.append(normalise(finding))
        except (ValueError, TypeError) as error:
            blockers.append(blocked("finding.invalid", f"{side}: invalid qa_finding: {error}", [report], case_id))
    if findings and outcome == "pass":
        blockers.append(blocked("finding.contradiction", f"{side}: passing JUnit case contains a failure finding", [report], case_id))
    diagnostics = [{"outcome": item.tag, "type": normalise(item.get("type", "")),
                    "message": normalise(item.get("message", "")),
                    "text": normalise(item.text or "")} for item in problems]
    # Reproduction locations are evidence, not the failure identity itself.
    contract = [{field: finding[field] for field in
                 ("issue_id", "suite", "mode", "stage", "status", "expected", "actual")}
                for finding in findings]
    signature = digest({"outcome": outcome, "findings": sorted(contract, key=canonical), "diagnostics": diagnostics})
    detail = "\n".join(filter(None, [item["message"] + "\n" + item["text"] for item in diagnostics]))
    structured_blocked = any(item["status"] == "BLOCKED" for item in findings)
    infrastructure = bool(INFRA_ERROR.search(detail)) or any(
        phrase in detail.lower() for phrase in
        ("required parser dependency unavailable", "dependency installation failed", "error collecting ",
         "no tests ran", "interrupted: ", "pytest: error:"))
    if outcome == "skipped":
        infrastructure = True
    production_error = bool(PRODUCTION_FILE.search(detail + "\n" + canonical(contract)))
    return {"case_id": case_id, "report": report, "classname": classname, "name": name,
            "outcome": outcome, "signature": signature, "findings": findings,
            "diagnostic": detail, "unverified": outcome == "error" or structured_blocked,
            "infrastructure": infrastructure, "production_error": production_error}


def read_reports(directory, manifest, normalise, side, blockers):
    directory = Path(directory)
    if not directory.is_dir():
        blockers.append(blocked("reports.missing-directory", f"{side}: reports directory is missing", [directory]))
        return {}, []
    paths = sorted(directory.glob("*.xml"))
    names = [path.name for path in paths]
    if not paths:
        blockers.append(blocked("reports.none", f"{side}: no top-level JUnit reports", [directory]))
    expected = manifest.get("expected_reports")
    if isinstance(expected, list) and all(isinstance(name, str) for name in expected) and set(expected) != set(names):
        blockers.append(blocked("reports.incomplete", f"{side}: expected {sorted(expected)!r}, found {names!r}", [directory]))
    cases = {}
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as error:
            blockers.append(blocked("reports.invalid-xml", f"{side}: {error}", [path]))
            continue
        if root.tag not in ("testsuite", "testsuites"):
            blockers.append(blocked("reports.not-junit", f"{side}: {path.name} is not JUnit XML", [path]))
            continue
        entries = list(root.iter("testcase"))
        if not entries:
            blockers.append(blocked("reports.no-cases", f"{side}: {path.name} has zero testcases", [path]))
        for suite in root.iter("testsuite"):
            descendants = list(suite.iter("testcase"))
            for attribute, tag in (("tests", None), ("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
                if attribute not in suite.attrib:
                    continue
                count = len(descendants) if tag is None else sum(case.find(tag) is not None for case in descendants)
                try:
                    declared = int(suite.attrib[attribute])
                except ValueError:
                    declared = -1
                if declared != count:
                    blockers.append(blocked("reports.invalid-counts", f"{side}: {path.name} declares {attribute}={suite.attrib[attribute]}, observed {count}", [path]))
        for entry in entries:
            value = parse_case(entry, path.name, normalise, blockers, side)
            if value["case_id"] in cases:
                blockers.append(blocked("inventory.duplicate-case", f"{side}: duplicate testcase {value['case_id']}", [path], value["case_id"]))
            cases[value["case_id"]] = value
    return cases, names


def issue(base, candidate, classification):
    item = candidate or base
    return {"case_id": item["case_id"], "report": item["report"], "classname": item["classname"], "name": item["name"],
            "classification": classification, "base_outcome": base["outcome"] if base else None,
            "candidate_outcome": candidate["outcome"] if candidate else None,
            "base_signature": base["signature"] if base else None,
            "candidate_signature": candidate["signature"] if candidate else None,
            "base_findings": base["findings"] if base else [],
            "candidate_findings": candidate["findings"] if candidate else [],
            "base_diagnostic": base["diagnostic"] if base else None,
            "candidate_diagnostic": candidate["diagnostic"] if candidate else None,
            "diagnostic": item["diagnostic"], "evidence": [item["report"]]}


def compare(base_reports, candidate_reports, base_manifest, candidate_manifest,
            base_roots=(), candidate_roots=()):
    """Classify reproduced old defects separately from new or unverified results."""
    blockers = []
    base = read_manifest(base_manifest, "base", blockers)
    candidate = read_manifest(candidate_manifest, "candidate", blockers)
    try:
        base_normalise, candidate_normalise = Normalizer(base, base_roots), Normalizer(candidate, candidate_roots)
    except ValueError as error:
        blockers.append(blocked("normalisation.invalid", str(error)))
        base_normalise = candidate_normalise = Normalizer({})
    for field in ("harness_digest", "fixtures_digest", "python_version", "test_command", "expected_reports"):
        if base_normalise(base.get(field)) != candidate_normalise(candidate.get(field)):
            blockers.append(blocked("baseline.incomparable", f"Base and candidate {field} differ", [base_manifest, candidate_manifest]))
    base_cases, base_names = read_reports(base_reports, base, base_normalise, "base", blockers)
    candidate_cases, candidate_names = read_reports(candidate_reports, candidate, candidate_normalise, "candidate", blockers)
    if base_names != candidate_names:
        blockers.append(blocked("inventory.report-mismatch", f"Base reports {base_names!r} differ from candidate reports {candidate_names!r}"))
    if set(base_cases) != set(candidate_cases):
        blockers.append(blocked("inventory.case-mismatch", "Base and candidate testcase inventories differ",
                                sorted(set(base_cases) ^ set(candidate_cases))))
    existing, resolved, regressions, coverage = [], [], [], []
    passed = 0
    for case_id in sorted(set(base_cases) | set(candidate_cases)):
        old, new = base_cases.get(case_id), candidate_cases.get(case_id)
        if old is None or new is None:
            continue  # The inventory blocker explains why this check is unverified.
        unsafe = [side for side, item in (("base", old), ("candidate", new)) if item["infrastructure"]]
        if unsafe:
            blockers.append(blocked("case.infrastructure", f"{', '.join(unsafe)}: skipped, timed-out, or infrastructure-dependent check", [new["report"]], case_id))
            continue
        if new["outcome"] == "pass":
            if old["outcome"] == "pass":
                passed += 1
            elif old["unverified"] and not old["production_error"]:
                blockers.append(blocked("case.unclassified-base-error", "The base setup error was not established as a parser defect", [old["report"]], case_id))
            else:
                resolved.append(issue(old, new, "RESOLVED"))
            continue
        same = old["signature"] == new["signature"]
        if old["unverified"] or new["unverified"]:
            complete_code_one = all(isinstance(manifest.get("execution"), dict) and
                                    1 in manifest["execution"].values() for manifest in (base, candidate))
            if same and old["production_error"] and new["production_error"] and complete_code_one:
                existing.append(issue(old, new, "EXISTING_ISSUE"))
                coverage.append({"case_id": case_id, "reason": "The same production parser error was reproduced; downstream assertions in this case were not verified.", "evidence": [new["report"]]})
            else:
                blockers.append(blocked("case.unverified", "New, changed, or unclassified setup/control error; this check did not verify its assertions", [new["report"]], case_id))
            continue
        if same:
            existing.append(issue(old, new, "EXISTING_ISSUE"))
        else:
            regressions.append(issue(old, new, "NEW_FAILURE" if old["outcome"] == "pass" else "WORSENED_FAILURE"))
    for side, manifest, cases in (("base", base, base_cases), ("candidate", candidate, candidate_cases)):
        execution = manifest.get("execution", {})
        if cases and any(item["outcome"] != "pass" for item in cases.values()) and isinstance(execution, dict) and execution and all(code == 0 for code in execution.values()):
            blockers.append(blocked("execution.contradiction", f"{side}: all stages report exit 0 but JUnit contains non-passing cases"))
        if cases and all(item["outcome"] == "pass" for item in cases.values()) and isinstance(execution, dict) and 1 in execution.values():
            blockers.append(blocked("execution.unexplained-failure", f"{side}: stage exit 1 has no corresponding JUnit failure"))
    counts = {"base_cases": len(base_cases), "candidate_cases": len(candidate_cases), "passed": passed,
              "existing": len(existing), "resolved": len(resolved),
              "new": sum(item["classification"] == "NEW_FAILURE" for item in regressions),
              "worsened": sum(item["classification"] == "WORSENED_FAILURE" for item in regressions), "blocked": len(blockers)}
    status = "BLOCKED" if blockers else "REGRESSION" if regressions else "NO_NEW_REGRESSIONS"
    return {"schema_version": 1, "status": status,
            "base": {field: base.get(field) for field in ("parser_revision", "parser_root", "work_root")},
            "candidate": {field: candidate.get(field) for field in ("parser_revision", "parser_root", "work_root")},
            "counts": counts, "existing_issues": existing, "resolved_issues": resolved,
            "regressions": regressions, "blockers": blockers, "coverage_limits": coverage,
            "inventory_digest": digest(sorted(candidate_cases))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for side in ("base", "candidate"):
        parser.add_argument(f"--{side}-reports", type=Path, required=True)
        parser.add_argument(f"--{side}-manifest", type=Path, required=True)
        parser.add_argument(f"--{side}-root", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = compare(args.base_reports, args.candidate_reports, args.base_manifest,
                     args.candidate_manifest, args.base_root, args.candidate_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(f"{result['status']}: {canonical(result['counts'])}")
    return {"NO_NEW_REGRESSIONS": 0, "REGRESSION": 1, "BLOCKED": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
