# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The two-revision CI runner must preserve isolation and incomplete-run evidence."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from . import qa_run as run


def test_command_failure_preserves_log_and_exit_code(tmp_path):
    log = tmp_path / "failure.log"
    result = run.run_stage([sys.executable, "-c", "print('controlled failure'); raise SystemExit(1)"], tmp_path, log, 10)
    assert result["exit_code"] == 1
    assert "controlled failure" in log.read_text()


def test_timeout_is_infrastructure_not_a_baseline_failure(tmp_path):
    result = run.run_stage([sys.executable, "-c", "import time; time.sleep(20)"], tmp_path, tmp_path / "timeout.log", 0.05)
    assert result["exit_code"] == 2
    assert "timed out" in result["infrastructure_error"]


def test_missing_command_is_infrastructure(tmp_path):
    result = run.run_stage([str(tmp_path / "no-such-program")], tmp_path, tmp_path / "missing.log", 1)
    assert result["exit_code"] == 2
    assert result["infrastructure_error"]


def test_child_cannot_inherit_a_different_parser_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PARSER_QA_PARSER_DIR", "/outside/old-parser")
    monkeypatch.setenv("PARSER_QA_TEST_MARKER", "preserved")
    log = tmp_path / "environment.log"
    command = [sys.executable, "-c", "import json, os; print(json.dumps(["
               "os.environ.get('LOG_PARSER_QA_PARSER_DIR'), os.environ.get('PARSER_QA_TEST_MARKER')]))"]
    result = run.run_stage(command, tmp_path, log, 10)
    assert result["exit_code"] == 0
    assert json.loads(log.read_text()) == [None, "preserved"]
    assert os.environ["LOG_PARSER_QA_PARSER_DIR"] == "/outside/old-parser"


def test_each_stage_runs_even_after_parser_contract_failures(tmp_path, monkeypatch):
    stages = []
    def execute(argv, cwd, log, timeout):
        stages.append(log.stem)
        log.write_text("existing parser defect\n")
        return {"exit_code": 1, "argv": [str(part) for part in argv]}
    monkeypatch.setattr(run, "run_stage", execute)
    manifest = run.execute_revision(tmp_path / "runtime", tmp_path / "reports", {}, 10, 2)
    assert stages == ["yaml", "contracts"]
    assert manifest["execution"] == {"yaml": 1, "contracts": 1}
    assert json.loads((tmp_path / "reports/run-manifest.json").read_text())["execution"] == manifest["execution"]
    assert not (tmp_path / "reports/browser.xml").exists()
    assert manifest["stages"]["yaml"]["argv"][0] == str(Path(sys.executable).absolute())


def test_browser_is_not_executed_twice_outside_the_yaml_manifest(tmp_path, monkeypatch):
    stages = []
    def execute(argv, cwd, log, timeout):
        stages.append(log.stem)
        log.write_text("ok\n")
        return {"exit_code": 0}
    monkeypatch.setattr(run, "run_stage", execute)
    manifest = run.execute_revision(tmp_path / "runtime", tmp_path / "reports", {}, 10, 1)
    assert stages == ["yaml", "contracts"]
    assert set(manifest["execution"]) == {"yaml", "contracts"}


def test_digest_includes_file_names_and_ignores_runtime_caches(tmp_path):
    folder = tmp_path / "harness"
    folder.mkdir()
    (folder / "test.py").write_text("test")
    before = run.digest_files(tmp_path, [Path("harness")])
    cache = folder / "__pycache__"
    cache.mkdir()
    (cache / "test.pyc").write_text("generated")
    assert run.digest_files(tmp_path, [Path("harness")]) == before
    (folder / "test.py").rename(folder / "different.py")
    assert run.digest_files(tmp_path, [Path("harness")]) != before


def fake_archive(member_name, kind=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(member_name)
        if kind:
            entry.type = kind
            entry.linkname = "../../outside"
            archive.addfile(entry)
        else:
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b"x"))
    return buffer.getvalue()


@pytest.mark.parametrize("name,kind", [("../outside", None), ("/absolute", None), ("link", tarfile.SYMTYPE)])
def test_base_archive_cannot_escape_the_isolated_runtime(tmp_path, monkeypatch, name, kind):
    def git(source, *args):
        return b"a" * 40 if args[0] == "rev-parse" else fake_archive(name, kind)
    monkeypatch.setattr(run, "_git", git)
    with pytest.raises(ValueError, match="unsafe base archive"):
        run.prepare_snapshots(tmp_path, "base", tmp_path / "runtime")


def test_base_and_candidate_use_same_harness_but_different_parser_sources(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    for relative in ("common/log_parser/parser.py", "common/log_parser/deleted.py",
                     "docs/log_parser_guide.md", "LICENSE.md"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("base")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=QA", "-c", "user.email=qa@example.invalid",
                    "-c", "core.hooksPath=/dev/null", "commit", "-qm", "isolated test fixture"], check=True)
    (source / "common/log_parser/parser.py").write_text("candidate")
    (source / "common/log_parser/deleted.py").unlink()
    (source / "common/log_parser/new.py").write_text("new candidate file")
    (source / ".gitignore").write_text("common/log_parser/ignored/\n")
    ignored = source / "common/log_parser/ignored/stale.py"
    ignored.parent.mkdir()
    ignored.write_text("stale private local file")
    for relative in (run.HARNESS / "test_probe.py", run.MANIFESTS / "probe.yaml", run.WORKFLOW):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("shared current QA")
    roots, _ = run.prepare_snapshots(source, "HEAD", tmp_path / "runtime")
    assert (roots["base"] / "common/log_parser/parser.py").read_text() == "base"
    assert (roots["candidate"] / "common/log_parser/parser.py").read_text() == "candidate"
    assert (roots["base"] / "common/log_parser/deleted.py").read_text() == "base"
    assert not (roots["candidate"] / "common/log_parser/deleted.py").exists()
    assert (roots["candidate"] / "common/log_parser/new.py").read_text() == "new candidate file"
    assert not (roots["candidate"] / "common/log_parser/ignored").exists()
    for root in roots.values():
        assert (root / run.HARNESS / "test_probe.py").read_text() == "shared current QA"
    assert (source / "common/log_parser/parser.py").read_text() == "candidate"


def test_orchestration_setup_failure_produces_blocked_report(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "runtime").mkdir()
    assert run.main(["--source", str(tmp_path), "--base", "HEAD", "--reports", str(reports)]) == 2
    assert json.loads((reports / "regression.json").read_text())["status"] == "BLOCKED"
