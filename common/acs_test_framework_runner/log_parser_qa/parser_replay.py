#!/usr/bin/env python3
# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier : Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Replay the same frozen inputs through the base and proposed parser.

Hardware FAIL results are valid input. CI fails when parser results differ without
reviewed expectations, or when the proposed output violates independent fixture
expectations. Unchanged baseline defects remain visible but do not block the PR;
an infrastructure failure never counts as a verified baseline defect.
"""

import argparse
from collections import Counter
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import yaml

try:
    from . import parser_artifacts as artifacts
    from .qa_report import run_command
except ImportError:
    import parser_artifacts as artifacts
    from qa_report import run_command


DEFAULT_FIXTURES = Path(__file__).parent / "tests/fixtures.yaml"


def _relative(root, value):
    """Reject absolute paths, escaping paths and symlinks in optional corpora."""
    value = Path(value)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError(f"expected a safe relative path: {value}")
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path escapes corpus: {value}")
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
        raise ValueError(f"symlink is not allowed in corpus: {value}")
    return path


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_cases(fixtures=DEFAULT_FIXTURES, corpus=None):
    """Use handwritten fixtures; add only explicitly approved hash-pinned logs.

    The optional JSON manifest has version=1, public_sanitized=true and cases.
    Every case has name, mode, suites, raw expectations and files mapping target
    names to {path, sha256}. Paths are local to that manifest; no network fetches.
    """
    cases = yaml.safe_load(Path(fixtures).read_text())["scenarios"]
    cases = [dict(case, source="synthetic") for case in cases]
    if corpus:
        manifest_path = Path(corpus).resolve()
        manifest = artifacts._load_json(manifest_path, strict=True)
        if manifest.get("version") != 1 or manifest.get("public_sanitized") is not True:
            raise ValueError("corpus must be version 1 and explicitly public_sanitized=true")
        for original in manifest.get("cases", []):
            case = dict(original, source="approved-corpus", files={})
            hashes = {}
            for target, file_spec in original["files"].items():
                _relative(Path("."), target)
                path = _relative(manifest_path.parent, file_spec["path"])
                if not path.is_file() or _sha(path) != file_spec["sha256"]:
                    raise ValueError(f"corpus SHA256 mismatch or missing file: {path}")
                case["files"][target] = path.read_bytes()
                hashes[target] = file_spec["sha256"]
            case["input_sha256"] = hashes
            cases.append(case)
    names = set()
    for case in cases:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", case["name"]) or case["name"] in names:
            raise ValueError(f"invalid or duplicate case name: {case['name']}")
        names.add(case["name"])
        if case["mode"] not in ("DT", "SR") or not case["suites"] or not case.get("raw"):
            raise ValueError(
                f"case needs mode, selected suites and independent raw expectations: {case['name']}")
        for name in case["files"]:
            _relative(Path("."), name)
    if not cases:
        raise ValueError("replay must execute at least one scenario")
    return cases


def _write_inputs(case, destination):
    """Materialize identical fixtures for both revisions; never touch real logs."""
    destination.mkdir(parents=True)
    for relative, content in case["files"].items():
        path = _relative(destination, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    if case.get("source") == "synthetic":
        defaults = {
            "results/linux_dump/dmidecode.txt": (
                "BIOS Information\n    Version: QA-FW\nSystem Information\n"
                "    Manufacturer: QA Vendor\n    Product Name: QA Board\n    Family: QA SoC\n"),
            "results/uefi_dump/uefi_version.log": "UEFI v2.10\n".encode("utf-16"),
            "acs_config.txt": (
                "ACS version: ACS v26.03_3.1.2\nSRS version: SRS 3.1.1\n"
                "BSA version: BSA v1.2\nSBSA version: SBSA v8.0\n"
                "PFDI version: PFDI 1.0 BET0\nBBR version: BBR v2.1\n"
                "SBBR version: SBBR v1.2\nSBMR version: SBMR v1.0\n"
                "EBBR version: EBBR v2.2.0\nBBSR version: BBSR v1.3\n"
                "SCMI version: SCMI v3.2\nDevice Tree Version: v0.4\nBand: "
                + ("SystemReady Devicetree band" if case["mode"] == "DT" else "SystemReady band") + "\n"),
            "system_config.txt": (
                "FW source code: Unknown\nFlashing instructions: Unknown\n"
                "product website: Unknown\nTested operated Systems: Unknown\n"
                "Testlab assistance: Unknown\nTotal_number_of_network_controllers= 0\n"),
        }
        for relative, content in defaults.items():
            path = _relative(destination, relative)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return {str(path.relative_to(destination)): _sha(path)
            for path in sorted(destination.rglob("*")) if path.is_file()}


def _at(value, path):
    for part in path:
        value = value[part]
    return value


def _equal(actual, expected):
    """JSON counters are typed: true must never compare equal to the count 1."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _equal(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _equal(left, right) for left, right in zip(actual, expected))
    return actual == expected


def _result_paths(value, path=()):
    if isinstance(value, dict):
        if "Test_result" in value or "sub_test_result" in value:
            yield path
        for key, child in value.items():
            yield from _result_paths(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _result_paths(child, (*path, index))


def independent_expectations(output, case):
    """Check fixture authors' expectations, never values learned from base output."""
    failures = []
    for filename, spec in case["raw"].items():
        path = output / "acs_jsons" / filename
        try:
            data = artifacts._load_json(path, strict=True)
            observed_paths = set(_result_paths(data))
            expected_paths = {tuple(record["path"]) for record in spec["records"]}
            if observed_paths != expected_paths:
                failures.append(
                    f"{filename}: result inventory differs: expected {expected_paths}, "
                    f"got {observed_paths}")
            for assertion in spec["assertions"]:
                actual = _at(data, assertion["path"])
                if not _equal(actual, assertion["value"]):
                    failures.append(
                        f"{filename}:{assertion['path']}: "
                        f"expected {assertion['value']!r}, got {actual!r}")
            for record in spec["records"]:
                actual = _at(data, record["path"])
                for key, expected in record["fields"].items():
                    value = actual.get(key)
                    # Reason arrays may be additional evidence; all counters remain exact.
                    if isinstance(expected, dict) and isinstance(value, dict):
                        extras = {name: count for name, count in value.items()
                                  if name not in expected and type(count) in (int, float) and count}
                        if extras:
                            failures.append(f"{filename}: unexpected counters {extras}")
                        value = {name: value.get(name) for name in expected}
                    if not _equal(value, expected):
                        failures.append(
                            f"{filename}:{record['path']}:{key}: "
                            f"expected {expected!r}, got {value!r}")
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            failures.append(f"{filename}: cannot validate independent expectations: {exc}")
    try:
        merged = artifacts._load_json(output / "acs_jsons/merged_results.json", strict=True)
        summary = merged["Suite_Name: acs_info"]["ACS Results Summary"]
        for key, expected in case.get("compliance", {}).items():
            if not _equal(summary.get(key), expected):
                failures.append(f"{key}: expected {expected!r}, got {summary.get(key)!r}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        failures.append(f"cannot validate merged compliance expectations: {exc}")
    return failures


def semantic_snapshot(output, input_root):
    """Keep every JSON field except two documented generated timestamp fields.

    Test identifiers, descriptions, reasons, summaries, requirement classifications
    and compliance remain visible. Normalize only this disposable input root.
    """
    result = {}
    for path in sorted((output / "acs_jsons").glob("*.json")):
        data = artifacts._load_json(path, strict=True)
        info = data.get("Suite_Name: acs_info", data) if isinstance(data, dict) else None
        if path.name in ("merged_results.json", "acs_info.json") and isinstance(info, dict):
            for section, field in (("System Info", "Summary Generated On"), ("ACS Results Summary", "Date")):
                if isinstance(info.get(section), dict):
                    info[section].pop(field, None)
            # The producer uses sets for these explanatory suite lists. Preserve
            # categories, reasons and duplicate names, but ignore only list order.
            summary = info.get("ACS Results Summary", {})
            for key in ("Overall Compliance Result", "BBSR compliance results", "SCMI compliance results"):
                if isinstance(summary.get(key), str):
                    summary[key] = re.sub(r"((?:failed|not run):\s*)([^;()]*)",
                        lambda match: match.group(1) + ", ".join(sorted(part.strip() for part in match.group(2).split(","))),
                        summary[key])
        encoded = json.dumps(data, sort_keys=True, ensure_ascii=True)
        result[path.name] = json.loads(encoded.replace(str(input_root), "<REPLAY_INPUT>"))
    if "merged_results.json" not in result:
        raise ValueError("replay produced no merged_results.json")
    return result


def differences(before, after, path="", limit=100):
    """Report paths and old/new values; equal totals cannot hide swapped tests."""
    found = []
    def visit(left, right, location):
        if len(found) >= limit:
            return
        if type(left) is not type(right):
            found.append({"path": location, "base": left, "candidate": right})
        elif isinstance(left, dict):
            for key in sorted(set(left) | set(right)):
                if key not in left or key not in right:
                    found.append({"path": f"{location}/{key}", "base": left.get(key, "<missing>"),
                                  "candidate": right.get(key, "<missing>")})
                else:
                    visit(left[key], right[key], f"{location}/{key}")
                if len(found) >= limit:
                    break
        elif isinstance(left, list):
            if len(left) != len(right):
                found.append({"path": location + "/length", "base": len(left), "candidate": len(right)})
            for index, (old, new) in enumerate(zip(left, right)):
                visit(old, new, f"{location}/{index}")
        elif left != right:
            found.append({"path": location, "base": left, "candidate": right})
    visit(before, after, path)
    return found


def replay_revision(parser_dir, case, directory, timeout):
    """Run one parser revision on frozen inputs and save comparable evidence."""
    inputs = directory / "input"
    hashes = _write_inputs(case, inputs)
    output = directory / "output"
    selected = ",".join(case["suites"])
    command = ["bash", parser_dir / "main_log_parser.sh", "--standalone", "--mode", case["mode"],
               "--suite", selected, "--input-log", inputs / case.get("results_dir", "results"),
               "--output", output, "--outputs", "summary"]
    for key, option in (("acs_config", "--acs-config"), ("system_config", "--system-config")):
        command.extend([option, _relative(inputs, case.get(key, key + ".txt"))])
    for key, option in (("waivers", "--waiver"), ("test_category", "--test-category")):
        if case.get(key):
            command.extend([option, _relative(inputs, case[key])])
    result = {"input_sha256": hashes, "correctness_errors": []}
    try:
        process = run_command(command, inputs, directory, "parser", timeout)
        result["exit_code"] = process.returncode
        if process.returncode:
            result["correctness_errors"].append(f"parser exited {process.returncode}; see parser.log")
        result["correctness_errors"].extend(independent_expectations(output, case))
        result["snapshot"] = semantic_snapshot(output, inputs)
        arguments = SimpleNamespace(output_dir=output, mode=case["mode"],
            registry=parser_dir / "suite_registry.json", schema=parser_dir / "acs-results-schema.json",
            selected_suites=[selected],
            expect_raw=[f"{name}={spec['destination']}" for name, spec in case["raw"].items()],
            expect_aux=case.get("auxiliary", []), test_category_provided=bool(case.get("test_category")))
        stream = io.StringIO()
        original_parser = artifacts.SCRIPT_DIR
        try:
            artifacts.SCRIPT_DIR = parser_dir
            with redirect_stdout(stream):
                status = artifacts._run_artifact_validation(arguments)
        finally:
            artifacts.SCRIPT_DIR = original_parser
        (directory / "artifact-validation.log").write_text(stream.getvalue())
        if status:
            # Keep each diagnostic for attribution; one known schema error must
            # not hide a newly introduced HTML inconsistency in the same run.
            for line in stream.getvalue().splitlines():
                if line.startswith("ERROR:"):
                    normalized = line.removeprefix("ERROR:").strip().replace(str(directory), "<REVISION>")
                    normalized = normalized.replace(str(parser_dir), "<PARSER>")
                    result["correctness_errors"].append(normalized)
    except Exception as exc:
        # Preserve earlier findings and keep independent cases running.
        result["blocked"] = f"{type(exc).__name__}: {exc}"
    result["correctness_errors"] = [error.replace(str(directory), "<REVISION>").replace(str(parser_dir), "<PARSER>")
                                    for error in result["correctness_errors"]]
    (directory / "observed.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def snapshot_digest(snapshot):
    """Hash the complete normalized replay snapshot."""
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def classify(base, candidate, reviewed=None):
    """Separate new output changes from unchanged or improved baseline defects."""
    for name, observed in (("base", base), ("candidate", candidate)):
        if (not isinstance(observed.get("input_sha256"), dict) or not observed["input_sha256"]
                or "snapshot" not in observed or not isinstance(observed["snapshot"], dict)
                or type(observed.get("exit_code")) is not int or observed["exit_code"] != 0
                or not isinstance(observed.get("correctness_errors"), list)):
            return "BLOCKED", [{"reason": f"{name} replay did not complete with comparable inputs and outputs"}]
    if base.get("input_sha256") != candidate.get("input_sha256"):
        return "BLOCKED", [{"reason": "base and candidate input hashes differ"}]
    if base.get("blocked") or candidate.get("blocked"):
        return "BLOCKED", [{"base": base.get("blocked"), "candidate": candidate.get("blocked")}]
    changed = differences(base.get("snapshot"), candidate.get("snapshot"))
    if candidate.get("correctness_errors"):
        old_errors, new_errors = Counter(base.get("correctness_errors", [])), Counter(candidate["correctness_errors"])
        if not changed and new_errors == old_errors:
            status = "KNOWN_BASELINE_DEFECT"
        elif not changed and not new_errors - old_errors:
            status = "BASELINE_DEFECT_IMPROVED"
        else:
            status = "REGRESSION"
    elif changed and reviewed and reviewed.get("reason", "").strip() and (
        reviewed.get("base_sha256") == snapshot_digest(base.get("snapshot"))
        and reviewed.get("candidate_sha256") == snapshot_digest(candidate.get("snapshot"))
    ):
        status = "PASS_REVIEWED_CHANGE"
    elif changed:
        # A base defect disappearing is still a change requiring reviewed expectations.
        status = "RESULT_CHANGE_REQUIRES_REVIEW"
    elif base.get("correctness_errors"):
        status = "BASELINE_DEFECT_IMPROVED"
    else:
        status = "PASS"
    return status, changed


def main(argv=None):
    """Replay both revisions and write the combined comparison result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-parser", type=Path, required=True)
    parser.add_argument("--candidate-parser", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--reviewed-changes", type=Path,
                        help="Explicitly reviewed case/base_sha256/candidate_sha256/reason JSON records; never generated automatically")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args(argv)
    # Child parsers run inside their input folders; keep every generated path absolute.
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"status": "BLOCKED", "cases": [], "baseline_defects": 0, "baseline_findings": 0, "status_counts": {},
              "note": "A successful CI result means no new regressions, not that the parser has no bugs. Unchanged baseline defects are informational; incomplete checks block the result."}
    try:
        cases = load_cases(args.fixtures, args.corpus)
        reviewed = {}
        if args.reviewed_changes:
            entries = artifacts._load_json(args.reviewed_changes, strict=True)
            if entries.get("version") != 1:
                raise ValueError("reviewed changes must use version=1")
            for entry in entries["changes"]:
                if entry["case"] in reviewed:
                    raise ValueError("duplicate reviewed change: " + entry["case"])
                reviewed[entry["case"]] = entry
        for case in cases:
            directory = args.output / case["name"]
            if directory.exists():
                raise ValueError(f"use a fresh replay output directory: {directory}")
            observations = {name: replay_revision(location.resolve(), case, directory / name, args.timeout)
                            for name, location in (("base", args.base_parser), ("candidate", args.candidate_parser))}
            status, changed = classify(observations["base"], observations["candidate"], reviewed.get(case["name"]))
            report["cases"].append({"name": case["name"], "mode": case["mode"], "source": case["source"],
                "status": status, "differences": changed,
                "base_sha256": snapshot_digest(observations["base"].get("snapshot")),
                "candidate_sha256": snapshot_digest(observations["candidate"].get("snapshot")),
                "base_errors": observations["base"].get("correctness_errors", []),
                "candidate_errors": observations["candidate"].get("correctness_errors", [])})
        statuses = {case["status"] for case in report["cases"]}
        informative = {"PASS", "PASS_REVIEWED_CHANGE", "KNOWN_BASELINE_DEFECT", "BASELINE_DEFECT_IMPROVED"}
        report["status_counts"] = dict(Counter(case["status"] for case in report["cases"]))
        report["baseline_defects"] = sum(case["status"] == "KNOWN_BASELINE_DEFECT" or (
            case["status"] == "BASELINE_DEFECT_IMPROVED" and bool(case["candidate_errors"])) for case in report["cases"])
        report["baseline_findings"] = sum(len(case["candidate_errors"]) for case in report["cases"]
                                          if case["status"] in {"KNOWN_BASELINE_DEFECT", "BASELINE_DEFECT_IMPROVED"})
        if statuses <= informative:
            report["status"] = "NO_NEW_REGRESSIONS"
        elif statuses <= informative | {"BLOCKED"}:
            report["status"] = "BLOCKED"
        else:
            report["status"] = "FAIL"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "NO_NEW_REGRESSIONS" else 2 if report["status"] == "BLOCKED" else 1


if __name__ == "__main__":
    sys.exit(main())
