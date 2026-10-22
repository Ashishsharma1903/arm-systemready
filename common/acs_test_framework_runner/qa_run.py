#!/usr/bin/env python3
# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run one shared QA harness against isolated base and proposed parser copies.

Existing parser failures stay visible. The separate regression comparator decides
whether this change adds or worsens a failure. Neither checkout nor input logs are
edited, and every stage runs even when an earlier stage found parser defects.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import time

try:
    from .qa_report import required_yaml_reports
except ImportError:
    from qa_report import required_yaml_reports


HARNESS = Path("common/acs_test_framework_runner")
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
        "contracts": [python, "-m", "pytest", "-q", "--tb=short", "common/acs_test_framework_runner/tests/log_parser",
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


def main(argv=None):
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
        fixtures_digest = digest_files(source, (HARNESS / "tests/log_parser/scenarios.yaml",
            HARNESS / "tests/log_parser/native_contracts.yaml", HARNESS / "tests/log_parser/compliance_cases.yaml", MANIFESTS))
        expected = required_yaml_reports(reports) + ["unit-e2e.xml"]
        common = {"harness_digest": harness_digest, "fixtures_digest": fixtures_digest,
                  "python_version": sys.version, "test_command": "strict YAML all-parser (including browser) + pytest parser contracts",
                  "expected_reports": sorted(expected)}
        for label, root in roots.items():
            manifest = {**common, "parser_revision": revision if label == "base" else _git(source, "rev-parse", "HEAD").decode().strip()}
            execute_revision(root, reports / label, manifest, args.stage_timeout, args.jobs)
        command = [sys.executable, source / HARNESS / "regression_gate.py",
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


if __name__ == "__main__":
    sys.exit(main())
