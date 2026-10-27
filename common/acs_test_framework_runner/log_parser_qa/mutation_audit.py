#!/usr/bin/env python3
# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Optional developer audit: prove QA detects controlled parser-source mistakes.

Each mutation runs in its own temporary checkout copy. A passing unmodified
control is required before a failing mutant counts as detected. No source
checkout, Git ref or remote is changed by this command.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import yaml


RUNNER = Path("common/acs_test_framework_runner")
TESTS = RUNNER / "log_parser_qa/tests"
PARSER = Path("common/log_parser")


@dataclass(frozen=True)
class Mutation:
    """One exact source replacement and the existing check that must catch it."""

    name: str
    description: str
    path: str
    before: str
    after: str
    check: str
    signals: tuple[str, ...]


MUTATIONS = (
    Mutation("python-syntax", "Invalid Python syntax", "standalone_runner.py",
             "import argparse\n", "import (\n", "syntax", ("SyntaxError",)),
    Mutation("shell-syntax", "Unclosed shell conditional in a later script", "package_standalone.sh",
             "set -euo pipefail\n", "set -euo pipefail\nif true; then\n", "syntax",
             ("syntax error",)),
    Mutation("json-syntax", "Malformed registry JSON", "suite_registry.json",
             '"suites": [', '"suites": [}', "syntax", ("Expecting value",)),
    Mutation("runtime-import", "Missing BSA runtime dependency", "bsa/logs_to_json.py",
             "import json\n", "import qa_intentional_missing_dependency\n",
             "native:BSA-SR", ("qa_intentional_missing_dependency",)),
    Mutation("raw-counter", "Off-by-one FWTS passed counter", "bbr/fwts/logs_to_json.py",
             'current_subtest["sub_test_result"]["PASSED"] += 1',
             'current_subtest["sub_test_result"]["PASSED"] += 2',
             "native:FWTS-SR", ("AssertionError",)),
    Mutation("raw-status", "FWTS passed assertion incorrectly becomes failed", "bbr/fwts/logs_to_json.py",
             'current_subtest["sub_test_result"]["PASSED"] += 1',
             'current_subtest["sub_test_result"]["FAILED"] += 1',
             "native:FWTS-SR", ("AssertionError",)),
    Mutation("merged-counter", "Add one invented merged failure", "merge_jsons.py",
             "return (total_failed, total_failed_with_waiver)",
             "return (total_failed + 1, total_failed_with_waiver)",
             "counter", ("assert (2, 0) == (1, 0)",)),
    Mutation("mandatory-policy", "Change mandatory SR BSA to recommended", "suite_registry.json",
             '"requirements": {"SR": "M", "DT": "R"}',
             '"requirements": {"SR": "R", "DT": "R"}',
             "policy", ("AssertionError",)),
    Mutation("waiver-status", "Ignore FWTS whole-suite assertion waivers", "apply_waivers.py",
             "# For other structures: apply to subtests\n        for subtest in test_suite_entry.get('subtests', []):",
             "# For other structures: apply to subtests\n        for subtest in []:",
             "waiver", ("AssertionError",)),
    Mutation("html-overall", "Display a green overall verdict despite failed evidence", "generate_acs_summary.py",
             'overall_result = acs_summary.get("Overall Compliance Result", "Unknown")',
             'overall_result = "Compliant"', "integrity", ("compliance status mismatch",)),
    Mutation("html-link", "Break consolidated links to detailed reports", "generate_acs_summary.py",
             'href="{{ card.detail }}"', 'href="qa_missing_detailed.html"',
             "integrity", ("must link exactly once to standalone_tests_detailed.html",)),
    Mutation("schema-type", "Require an array where merged JSON is an object", "acs-results-schema.json",
             '"type": "object",\n  "required": [\n    "Suite_Name: acs_info"',
             '"type": "array",\n  "required": [\n    "Suite_Name: acs_info"',
             "integrity", ("merged JSON violates the complete schema", "is not of type 'array'")),
    Mutation("package-missing-module", "Omit shared report UI from shipped archive", "package_standalone.sh",
             'exclude_args=(\n', 'exclude_args=(\n    "--exclude=common/log_parser/report_ui.py"\n',
             "native:BSA-SR", ("Extracted runtime differs from the source parser",)),
)


def source_digest(source: Path) -> str:
    """Record source bytes so the audit can verify it left them untouched."""
    digest = hashlib.sha256()
    for path in sorted((source / PARSER).rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(path.relative_to(source).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def copy_checkout(source: Path, destination: Path) -> None:
    """Copy only the runtime, QA harness, manifests and package documentation."""
    for relative in (PARSER, RUNNER, Path("common/acs_test_framework_manifests")):
        shutil.copytree(source / relative, destination / relative,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    for relative in ("docs/acs_schema_guide.md", "docs/log_parser_guide.md", "LICENSE.md"):
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, path)


def apply_mutation(checkout: Path, mutation: Mutation) -> str:
    """Refuse stale or ambiguous anchors rather than altering the wrong code."""
    path = checkout / PARSER / mutation.path
    original = path.read_text(encoding="utf-8")
    matches = original.count(mutation.before)
    if matches != 1:
        raise ValueError(f"{mutation.name}: expected one source anchor, found {matches}: {mutation.path}")
    updated = original.replace(mutation.before, mutation.after, 1)
    path.write_text(updated, encoding="utf-8")
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile="a/" + str(PARSER / mutation.path), tofile="b/" + str(PARSER / mutation.path),
    ))


def check_command(checkout: Path, mutation: Mutation, evidence: Path) -> tuple[list[str], bool]:
    """Use the same narrow source, contract or integrity checks as the CI gate."""
    python = sys.executable
    if mutation.check == "syntax":
        manifest = yaml.safe_load((checkout / "common/acs_test_framework_manifests/log-parser-integrity.yaml").read_text())
        case = next(item for item in manifest["suites"][0]["cases"]
                    if item["name"] == "isolated_sr_registry_and_dependency_check")
        script = evidence / "check_syntax.py"
        script.write_text(case["scripts"]["check_syntax.py"], encoding="utf-8")
        return [python, str(script), str(checkout / PARSER)], False
    if mutation.check == "integrity":
        return [python, str(RUNNER / "pytest_runner.py"), "--test",
                "log-parser-integrity::log_parser_integrity", "--target", str(PARSER),
                "--require-tests", "--fail-on-warnings", "--fail-on-skips", "--jobs", "1",
                "--reports-dir", str(evidence / "yaml")], False
    if mutation.check.startswith("native:"):
        selector = f"test_end_to_end.py::test_native_positive_control[{mutation.check.split(':', 1)[1]}]"
    elif mutation.check == "counter":
        selector = "test_compliance.py::test_failure_counts[list-failed-1_waived-0-None-testcases]"
    elif mutation.check == "policy":
        selector = "test_compliance.py::test_registry_policy_matches_expected_contract[BSA-SR]"
    elif mutation.check == "waiver":
        selector = "test_end_to_end.py::test_native_waiver_contracts[idempotence]"
    else:
        raise ValueError(f"Unknown mutation check: {mutation.check}")
    return [python, "-m", "pytest", "-q", "--tb=short", str(TESTS / selector),
            "--junitxml", str(evidence / "junit.xml"), "--basetemp", str(evidence / "work")], True


def run_check(checkout: Path, mutation: Mutation, evidence: Path) -> dict:
    evidence.mkdir(parents=True)
    command, pytest_check = check_command(checkout, mutation, evidence)
    environment = {**os.environ, "LOG_PARSER_QA_PARSER_DIR": str(checkout / PARSER),
                   "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
                   "PYTHONPATH": "", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "MPLBACKEND": "Agg", "NO_COLOR": "1"}
    start = time.monotonic()
    try:
        result = subprocess.run(command, cwd=checkout, env=environment, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240, check=False)
        output, exit_code, timed_out = result.stdout, result.returncode, False
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        exit_code, timed_out = None, True
    (evidence / "check.log").write_text(output, encoding="utf-8")
    record = {"argv": command, "cwd": str(checkout), "exit_code": exit_code,
              "timed_out": timed_out, "seconds": round(time.monotonic() - start, 3),
              "log": str(evidence / "check.log"), "failures": [], "signal_evidence": []}
    # Some child parser errors are linked from JUnit rather than printed by
    # pytest. Retain the exact child log and line that explains the failure.
    for log in sorted(evidence.rglob("*.log")):
        for number, line in enumerate(log.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for signal in mutation.signals:
                if signal.lower() in line.lower():
                    record["signal_evidence"].append({"signal": signal, "file": str(log),
                                                       "line": number, "text": line[:500]})
    if pytest_check or mutation.check == "integrity":
        try:
            report = evidence / ("junit.xml" if pytest_check else "yaml/log-parser-integrity.xml")
            root = ET.parse(report).getroot()
            cases = root.findall(".//testcase")
            record["test_count"] = len(cases)
            record["skipped"] = sum(case.find("skipped") is not None for case in cases)
            record["failures"] = [{"test": case.get("name"), "message": node.get("message", "")}
                                   for case in cases for node in case
                                   if node.tag in ("failure", "error")]
            record["valid_evidence"] = bool(cases) and record["skipped"] == 0
        except (OSError, ET.ParseError):
            record["valid_evidence"] = False
    else:
        record["valid_evidence"] = not timed_out
    (evidence / "command.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def classify(base: dict, mutant: dict, signals: tuple[str, ...], output: str) -> tuple[str, str]:
    """An already failing control or unrelated error is not a caught mutation."""
    if not base.get("valid_evidence") or base.get("exit_code") != 0 or base.get("failures"):
        return "BLOCKED", "The unmodified targeted check does not pass; detection was not established."
    if not mutant.get("valid_evidence") or mutant.get("timed_out"):
        return "BLOCKED", "The mutant check did not produce complete execution evidence."
    if mutant.get("exit_code") == 0:
        return "MISSED", "The targeted check passed after the intentional source mistake."
    if mutant.get("exit_code") in (None, 2, 3, 4, 5) and mutant.get("test_count") == 0:
        return "BLOCKED", "Pytest did not execute the targeted check."
    if not any(signal.lower() in output.lower() for signal in signals):
        return "BLOCKED", "The check failed, but the expected mutation-specific signal was absent."
    return "DETECTED", "The unmodified control passed and the intentional mistake caused the expected failure."


def audit(source: Path, output: Path, mutations: list[Mutation]) -> dict:
    for copied_tree in (PARSER, RUNNER, Path("common/acs_test_framework_manifests")):
        if output == source / copied_tree or source / copied_tree in output.parents:
            raise ValueError("Place audit evidence outside parser, runner and manifest source folders.")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose a new or empty output directory; existing evidence is never overwritten.")
    output.mkdir(parents=True, exist_ok=True)
    before = source_digest(source)
    findings = []
    for mutation in mutations:
        evidence = output / mutation.name
        evidence.mkdir()
        finding = {"mutation": asdict(mutation)}
        print(f"Checking {mutation.name} ...", flush=True)
        with tempfile.TemporaryDirectory(prefix="parser-mutation-audit-") as temporary:
            checkout = Path(temporary) / "checkout"
            copy_checkout(source, checkout)
            try:
                # Confirm the anchor before spending time on baseline execution.
                original = (checkout / PARSER / mutation.path).read_text(encoding="utf-8")
                if original.count(mutation.before) != 1:
                    raise ValueError(f"{mutation.name}: source anchor is missing or ambiguous")
                base = run_check(checkout, mutation, evidence / "base")
                patch = apply_mutation(checkout, mutation)
                (evidence / "mutation.patch").write_text(patch, encoding="utf-8")
                mutant = run_check(checkout, mutation, evidence / "mutant")
                text = (evidence / "mutant/check.log").read_text(encoding="utf-8")
                text += "\n" + "\n".join(item["text"] for item in mutant.get("signal_evidence", []))
                status, explanation = classify(base, mutant, mutation.signals, text)
                finding.update(status=status, explanation=explanation, base=base, mutant=mutant)
            except (OSError, ValueError) as error:
                finding.update(status="BLOCKED", explanation=str(error))
        findings.append(finding)
        (evidence / "result.json").write_text(json.dumps(finding, indent=2), encoding="utf-8")
        print(f"  {finding['status']}: {finding['explanation']}", flush=True)
    unchanged = before == source_digest(source)
    counts = dict(Counter(item["status"] for item in findings))
    report = {"synthetic_only": True, "source": str(source), "source_sha256": before,
              "source_unchanged": unchanged, "counts": counts, "mutations": findings,
              "scope": "Targeted synthetic regression controls, not exhaustive proof or hardware qualification."}
    (output / "mutation-results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", action="append", default=[], choices=["all", *(case.name for case in MUTATIONS)])
    parser.add_argument("--list", action="store_true", help="Print exact mutation anchors for review")
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps([asdict(case) for case in MUTATIONS], indent=2))
        return 0
    if args.output is None:
        parser.error("--output is required unless --list is used")
    names = set(args.case or ["all"])
    selected = [case for case in MUTATIONS if "all" in names or case.name in names]
    try:
        report = audit(args.source.resolve(), args.output.resolve(), selected)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report["counts"], sort_keys=True))
    if not report["source_unchanged"] or report["counts"].get("BLOCKED"):
        return 2
    return int(bool(report["counts"].get("MISSED")))


if __name__ == "__main__":
    raise SystemExit(main())
