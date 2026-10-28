#!/usr/bin/env python3
# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Classify changes, run isolated parser copies, and compare their QA evidence.

Both revisions use the same checks. Existing failures stay visible, while new or
worsened failures and incomplete checks block the change. Source checkouts and
input logs are preserved throughout the run.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
import re
import xml.etree.ElementTree as ET
import shutil
import signal
import subprocess
import sys
import tarfile
import time

try:
    from .qa_report import metadata_errors, required_yaml_reports
except ImportError:
    from qa_report import metadata_errors, required_yaml_reports


HARNESS = Path("common/acs_test_framework_runner")
QA = HARNESS / "log_parser_qa"
MANIFESTS = Path("common/acs_test_framework_manifests")
PARSER = Path("common/log_parser")
WORKFLOW = Path(".github/workflows/log-parser-qa.yml")


def _ignored(directory, names):
    return {name for name in names if name in {"__pycache__", ".pytest_cache"} or name.endswith(".pyc")}


def _copy(source, destination):
    if source.is_dir():
        shutil.copytree(source, destination, ignore=_ignored, symlinks=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)


def digest_files(source, locations):
    """Fingerprint names and contents, not directory times or generated caches."""
    digest = hashlib.sha256()
    files = []
    for location in locations:
        path = source / location
        files.extend(path.rglob("*") if path.is_dir() else [path])
    for path in sorted(set(files)):
        if not path.is_file() or "__pycache__" in path.parts or ".pytest_cache" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"QA harness inputs cannot be symlinks: {path}")
        digest.update(str(path.relative_to(source)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _git(source, *arguments):
    result = subprocess.run(["git", "-C", str(source), *arguments], capture_output=True, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def prepare_snapshots(source, base, runtime):
    """Export base runtime files; overlay exactly the same candidate QA on both."""
    revision = _git(source, "rev-parse", "--verify", base + "^{commit}").decode().strip()
    base_root, candidate_root = runtime / "base", runtime / "candidate"
    base_root.mkdir(parents=True)
    candidate_root.mkdir(parents=True)
    archive = _git(source, "archive", revision, "--", "common/log_parser", "docs", "LICENSE.md")
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        for member in package.getmembers():
            target = base_root / member.name
            if (Path(member.name).is_absolute() or ".." in Path(member.name).parts
                    or not target.resolve().is_relative_to(base_root.resolve())
                    or not (member.isfile() or member.isdir())):
                raise ValueError("unsafe base archive entry: " + member.name)
        # Only regular files and directories are accepted. Avoid tar extraction
        # defaults changing between supported Python versions.
        for member in package.getmembers():
            target = base_root / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as archived, target.open("wb") as output:
                    shutil.copyfileobj(archived, output)
                target.chmod(member.mode & 0o777)
    # Keep working edits and new files, but not ignored leftovers from older
    # local runs. A deleted tracked file must remain absent from the candidate.
    names = _git(source, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                 "--", str(PARSER), "docs", "LICENSE.md").decode(errors="surrogateescape").split("\0")
    for name in sorted(set(names) - {""}):
        path = source / name
        if path.is_file() or path.is_symlink():
            _copy(path, candidate_root / name)
    for root in (base_root, candidate_root):
        for relative in (HARNESS, MANIFESTS):
            _copy(source / relative, root / relative)
        if (source / WORKFLOW).exists():
            _copy(source / WORKFLOW, root / WORKFLOW)
    return {"base": base_root, "candidate": candidate_root}, revision


def run_stage(argv, cwd, log, timeout):
    """Record complete output; infrastructure exceptions get a distinct code 2."""
    started = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"argv": [str(part) for part in argv], "cwd": str(cwd), "timeout_seconds": timeout}
    temporary = log.parent / "temporary"
    temporary.mkdir(exist_ok=True)
    environment = {**os.environ, "PYTHONPATH": "", "PYTHONNOUSERSITE": "1", "PYTHONHASHSEED": "0",
                   "MPLBACKEND": "Agg", "NO_COLOR": "1",
                   "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
                   "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    # Each snapshot must test its own parser, not an override from the caller.
    environment.pop("LOG_PARSER_QA_PARSER_DIR", None)
    with log.open("w", encoding="utf-8") as output:
        try:
            child = subprocess.Popen(metadata["argv"], cwd=cwd, env=environment, text=True,
                                     stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                metadata["exit_code"] = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
                metadata.update(exit_code=2, infrastructure_error="stage timed out")
                output.write("\nBLOCKED: stage timed out\n")
        except OSError as exc:
            metadata.update(exit_code=2, infrastructure_error=str(exc))
            output.write("BLOCKED: " + str(exc) + "\n")
    metadata["duration_seconds"] = round(time.monotonic() - started, 3)
    return metadata


def execute_revision(root, reports, manifest, timeout, jobs):
    reports.mkdir(parents=True)
    harness = root / HARNESS
    python = Path(sys.executable).absolute()
    commands = {
        "yaml": [python, harness / "pytest_runner.py", "--all-tests", "--target", "common/log_parser", "--require-tests",
                 "--fail-on-warnings", "--fail-on-skips", "--jobs", str(jobs), "--reports-dir", reports],
        "contracts": [python, "-m", "pytest", "-q", "--tb=short", str(QA / "tests"),
                      "--basetemp", reports / "work", "--junitxml", reports / "unit-e2e.xml"],
    }
    manifest.update(parser_root=str(root / PARSER), work_root=str(reports), repository_root=str(root),
                    python_executable=str(python), execution={}, stages={})
    for name, command in commands.items():
        log = reports / (name + ".log")
        stage = run_stage(command, root, log, timeout)
        manifest["execution"][name] = stage["exit_code"]
        manifest["stages"][name] = stage
        # A later stage failing must not destroy the earlier run evidence.
        (reports / "run-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def execute_revisions(roots, reports, common, revisions, timeout, jobs):
    """Run both isolated snapshots within one bounded test-worker budget.

    Both revisions always finish before an unexpected worker error is reported.
    Each worker keeps its own logs and partial manifest for diagnosis.
    """
    parallel = jobs >= 2
    budgets = ({"base": (jobs + 1) // 2, "candidate": jobs // 2}
               if parallel else {"base": 1, "candidate": 1})

    def execute(label):
        manifest = {**common, "parser_revision": revisions[label]}
        destination = reports / label
        try:
            return execute_revision(roots[label], destination, manifest, timeout, budgets[label])
        except Exception as exc:
            message = f"{label}: {type(exc).__name__}: {exc}"
            destination.mkdir(parents=True, exist_ok=True)
            manifest["infrastructure_error"] = message
            (destination / "run-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            (destination / "revision-error.log").write_text(message + "\n")
            return manifest

    if parallel:
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = {label: executor.submit(execute, label) for label in roots}
            outcomes = {label: future.result() for label, future in pending.items()}
    else:
        outcomes = {label: execute(label) for label in roots}
    errors = [manifest["infrastructure_error"] for manifest in outcomes.values()
              if "infrastructure_error" in manifest]
    if errors:
        raise ValueError("; ".join(errors))
    return outcomes


def run_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--stage-timeout", type=int, default=1800)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)
    source, reports = args.source.resolve(), args.reports.resolve()
    reports.mkdir(parents=True, exist_ok=True)
    try:
        if any((reports / name).exists() for name in ("runtime", "base", "candidate")):
            raise ValueError("use a fresh reports folder; base/candidate/runtime already exists")
        if args.stage_timeout <= 0 or args.jobs <= 0:
            raise ValueError("stage timeout and job count must be positive")
        roots, revision = prepare_snapshots(source, args.base, reports / "runtime")
        harness_digest = digest_files(source, (HARNESS, MANIFESTS, WORKFLOW))
        fixtures_digest = digest_files(source, (QA / "tests/fixtures.yaml", MANIFESTS))
        expected = required_yaml_reports(reports) + ["unit-e2e.xml"]
        common = {"harness_digest": harness_digest, "fixtures_digest": fixtures_digest,
                  "python_version": sys.version, "test_command": "strict YAML all-parser (including browser) + pytest parser contracts",
                  "expected_reports": sorted(expected)}
        revisions = {"base": revision, "candidate": _git(source, "rev-parse", "HEAD").decode().strip()}
        execute_revisions(roots, reports, common, revisions, args.stage_timeout, args.jobs)
        command = [sys.executable, source / QA / "qa_run.py", "--compare",
                   "--base-reports", reports / "base", "--candidate-reports", reports / "candidate",
                   "--base-manifest", reports / "base/run-manifest.json",
                   "--candidate-manifest", reports / "candidate/run-manifest.json",
                   "--output", reports / "regression.json"]
        comparison = run_stage(command, source, reports / "comparison.log", args.stage_timeout)
        if not (reports / "regression.json").is_file():
            raise ValueError("comparison did not produce regression.json; see comparison.log")
        return comparison["exit_code"]
    except Exception as exc:
        finding = {"status": "BLOCKED", "blockers": [f"{type(exc).__name__}: {exc}"],
                   "note": "CI comparison did not complete; no healthy result is implied."}
        (reports / "regression.json").write_text(json.dumps(finding, indent=2) + "\n")
        print(finding["blockers"][0], file=sys.stderr)
        return 2



# Identify the affected suites before running the same checks on both revisions.

PARSER_PREFIX = "common/log_parser/"
REGISTRY = PARSER_PREFIX + "suite_registry.json"
QA_PATHS = ("common/acs_test_framework_", ".github/workflows/", ".githooks/")


def _suite_directories(registry):
    directories = {}
    for suite in registry.get("suites", []):
        for key, value in suite.items():
            if "logs_to_json" not in key and key != "json_to_html":
                continue
            for script in value if isinstance(value, list) else [value]:
                parent = str(PurePosixPath(PARSER_PREFIX + script).parent) + "/"
                directories.setdefault(parent, set()).add(suite["canonical"])
    return directories


def classify_changes(paths, old_registry, new_registry, old_files):
    old_names = {item["canonical"] for item in old_registry.get("suites", [])}
    new_names = {item["canonical"] for item in new_registry.get("suites", [])}
    old_execution = old_registry.get("standalone", {}).get("suite_execution", {})
    new_execution = new_registry.get("standalone", {}).get("suite_execution", {})
    added = (new_names - old_names) | (set(new_execution) - set(old_execution))
    directories = _suite_directories(old_registry)
    for directory, suites in _suite_directories(new_registry).items():
        directories.setdefault(directory, set()).update(suites)
    existing, shared = set(), []
    for path in paths:
        source = PurePosixPath(path)
        if path.startswith(PARSER_PREFIX) and source.name in ("logs_to_json.py", "json_to_html.py"):
            directory = str(source.parent) + "/"
            if not any(old.startswith(directory) for old in old_files):
                added.add("directory: " + str(source.parent.relative_to(PARSER_PREFIX)))
        matched = {suite for directory, suites in directories.items()
                   if path.startswith(directory) for suite in suites}
        existing.update(matched & old_names)
        if path.startswith(QA_PATHS) or (path.startswith(PARSER_PREFIX) and not matched):
            shared.append(path)
    kinds = []
    if added:
        kinds.append("new-suite")
    if existing:
        kinds.append("existing-suite")
    if shared:
        kinds.append("shared-schema-qa")
    return {"kinds": kinds or ["no-parser-change"], "new_suites": sorted(added),
            "existing_suites": sorted(existing), "shared_changes": sorted(shared)}


def _impact_git(root, *arguments):
    return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def inspect_change(root, base, head="HEAD"):
    head = _impact_git(root, "rev-parse", "--verify", "--end-of-options", head + "^{commit}").decode().strip()
    if not base or set(base) == {"0"}:
        parents = _impact_git(root, "rev-list", "--parents", "-n", "1", head).decode().split()
        base = parents[1] if len(parents) > 1 else ""
    elif base:
        base = _impact_git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()

    def files(revision):
        return set(_impact_git(root, "ls-tree", "-r", "--name-only", "-z", revision).decode().split("\0")) - {""}

    old_files = files(base) if base else set()
    new_files = files(head)
    paths = (_impact_git(root, "diff", "--name-only", "--no-renames", "-z", base, head, "--").decode().split("\0")
             if base else sorted(new_files))
    def registry(revision, names):
        if REGISTRY not in names:
            return {}
        try:
            return json.loads(_impact_git(root, "show", revision + ":" + REGISTRY))
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Invalid JSON in {REGISTRY} at revision {revision}: "
                f"{error.msg} (line {error.lineno}, column {error.colno})"
            ) from error

    old_registry = registry(base, old_files)
    new_registry = registry(head, new_files)
    result = classify_changes([path for path in paths if path], old_registry, new_registry, old_files)
    return {"base": base, "head": head, **result}


def classify_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="Base commit; an empty or all-zero value uses HEAD's parent")
    parser.add_argument("--head", default="HEAD", help="Commit being tested")
    args = parser.parse_args(argv)
    try:
        result = inspect_change(Path(__file__).resolve().parents[3], args.base, args.head)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        # Keep stdout valid JSON on success and give failures a saved, readable
        # stderr diagnostic. The workflow must not approve skipped comparisons.
        print(f"Change classification failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            detail = error.stderr
            print(detail.decode(errors="replace") if isinstance(detail, bytes) else detail,
                  file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"new_suite={str(bool(result['new_suites'])).lower()}\n")
            label = ("All-suite regression (shared/schema/QA)" if result["shared_changes"]
                     else "Existing-suite regression" if result["existing_suites"]
                     else "All-suite regression")
            output.write(f"regression_label={label}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("## Change classification\n\n" + ", ".join(result["kinds"]) + "\n\n")
            summary.write("All suites, schema contracts, and browser checks remain required.\n\n")
            summary.write("```json\n" + json.dumps(result, indent=2) + "\n```\n")



# Compare complete result evidence so an already-failing check cannot hide a change.

STAGES = ("yaml", "contracts")
OUTCOME_TAGS = ("failure", "error", "skipped")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}| UTC)\b")
VOLATILE_KEYS = {"generated_at", "run_timestamp", "timestamp", "started_at", "finished_at"}
RUN_DATE_FIELD = re.compile(r"(['\"]Date['\"]\s*:\s*['\"])(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(['\"])")
WORK_STAGING = re.compile(r"(<WORK>/(?:(?:(?!\.\.\.)[^/\s'\"<>])+/)*)\.output\.tmp-[a-z0-9_]{8}(?=/)")
INFRA_ERROR = re.compile(
    r"(?:^|\n)(?:E\s+)?(?:[\w.]+\.)?"
    r"(?:ImportError|ModuleNotFoundError|PermissionError|TimeoutExpired|TimeoutError|KeyboardInterrupt|BrowserInfrastructureError|"
    r"Interrupted|UsageError|CollectionError)(?::|$)", re.MULTILINE)
RESOURCE_ERROR = re.compile(
    r"(?:^|\n)(?:E\s+)?(?:[\w.]+\.)?(?:OSError|IOError):\s*\[Errno (?:12|24|28|30)\]",
    re.MULTILINE)
PRODUCTION_FILE = re.compile(
    r"(?:<PARSER>(?:/|\b)|(?:/|\b)common/log_parser/|\b(?:main_log_parser\.sh|"
    r"merge_jsons\.py|logs_to_json\.py|json_to_html\.py|standalone_runner\.py|"
    r"apply_waivers\.py)\b)")
PARSER_OUTPUT_MISMATCH = re.compile(
    r"^(?:failed on setup with [\"'])?"
    r"(?:(?:common\.acs_test_framework_runner\.log_parser_qa\.)?qa_report\.)?"
    r"ParserOutputMismatch(?::|$)")
CHROMIUM_PREFIX = re.compile(r"\[\d+:\d+:\d{4}/\d{6}\.\d+:(?=(?:VERBOSE\d*|INFO|WARNING|ERROR|FATAL):)")
DEVTOOLS_BANNER = re.compile(
    r"(?m)^DevTools listening on ws://(?:127\.0\.0\.1|localhost|\[::1\]):\d+"
    r"/devtools/browser/[0-9a-fA-F-]{36}\s*$")


def canonical(value):
    """Keep the complete value: changing an already-red actual must be noticed."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def valid_time(value):
    """Do not hide a malformed date as though it were harmless run metadata."""
    try:
        datetime.fromisoformat(value.replace(" UTC", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


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
        text = TIMESTAMP.sub(lambda match: "<TIMESTAMP>" if valid_time(match[0]) else match[0], text)
        text = RUN_DATE_FIELD.sub(lambda match: match[1] + "<TIMESTAMP>" + match[3]
                                 if valid_time(match[2]) else match[0], text)
        if key == "Date" and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text) and valid_time(text):
            text = "<TIMESTAMP>"
        if key in VOLATILE_KEYS and re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ].+", text):
            try:
                datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                pass  # Malformed date values remain visible to the contract.
            else:
                text = "<TIMESTAMP>"
        # Standalone publication creates this random eight-character staging
        # suffix. Only this known basename below the recorded work root is noise.
        text = WORK_STAGING.sub(lambda match: match[1] + ".output.tmp-<STAGING>", text)
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
    # Missing permissions or host resources are not reproduced parser defects.
    # Other OSErrors can still describe a real parser handling bug.
    error_detail = detail + "\n" + "\n".join(item["type"] + ": " + item["message"] for item in diagnostics)
    infrastructure = bool(INFRA_ERROR.search(error_detail) or RESOURCE_ERROR.search(error_detail)) or any(
        phrase in detail.lower() for phrase in
        ("required parser dependency unavailable", "dependency installation failed", "error collecting ",
         "no tests ran", "interrupted: ", "pytest: error:"))
    if outcome == "skipped":
        infrastructure = True
    # A completed producer can return the wrong count without raising inside a
    # parser file. Its independent output assertion supplies explicit provenance;
    # ordinary fixture errors must not receive this attribution.
    # Read the outer exception identity, not a quoted line from a parser log or
    # an earlier exception in a traceback. Pytest omits type on some XML nodes.
    output_mismatch = any(PARSER_OUTPUT_MISMATCH.match(item["type"] or item["message"])
                          for item in diagnostics)
    production_error = bool(PRODUCTION_FILE.search(detail + "\n" + canonical(contract))
                            or output_mismatch)
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


def compare_main(argv=None):
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



def main(argv=None):
    """Keep classification, execution and comparison behind one QA entry point."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--classify":
        return classify_main(arguments[1:])
    if arguments and arguments[0] == "--compare":
        return compare_main(arguments[1:])
    return run_main(arguments)


if __name__ == "__main__":
    sys.exit(main())
