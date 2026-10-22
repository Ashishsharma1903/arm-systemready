# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check browser isolation, cleanup and useful startup-error diagnostics."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
from threading import Barrier

import pytest

from common.acs_test_framework_runner import report_ui_browser_smoke as browser


PASS_DOM = '<body data-browser-smoke="PASS"></body>'


def test_browser_uses_short_private_temp_without_changing_parent(monkeypatch, tmp_path):
    inherited = str(tmp_path / ("long-" + "a" * 160))
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, inherited)
    observed = []

    def run(command, **kwargs):
        environment = kwargs["env"]
        temporary = Path(environment["TMPDIR"])
        observed.append(temporary)
        assert temporary.parent == Path("/tmp")
        assert len(str(temporary)) < 70
        assert temporary.is_dir()
        assert all(environment[key] == str(temporary) for key in ("TMPDIR", "TMP", "TEMP"))
        assert f"--user-data-dir={temporary}/profile" in command
        assert environment["PATH"] == os.environ["PATH"]
        return subprocess.CompletedProcess(command, 0, PASS_DOM, "")

    monkeypatch.setattr(browser.subprocess, "run", run)
    browser._run_page("fake-chromium", tmp_path, "short-temp", "<html></html>")
    assert observed and all(not path.exists() for path in observed)
    assert all(os.environ[key] == inherited for key in ("TMPDIR", "TMP", "TEMP"))


def test_concurrent_browsers_do_not_share_temp_or_profile(monkeypatch, tmp_path):
    barrier = Barrier(2)
    observed = []

    def run(command, **kwargs):
        temporary = Path(kwargs["env"]["TMPDIR"])
        observed.append(temporary)
        barrier.wait(timeout=5)
        assert temporary.is_dir()
        assert f"--user-data-dir={temporary}/profile" in command
        return subprocess.CompletedProcess(command, 0, PASS_DOM, "")

    monkeypatch.setattr(browser.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(browser._run_page, "fake-chromium", tmp_path,
                               f"parallel-{index}", "<html></html>") for index in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert len(set(observed)) == 2
    assert all(not path.exists() for path in observed)


def test_browser_timeout_also_cleans_private_temp(monkeypatch, tmp_path):
    observed = []

    def run(command, **kwargs):
        observed.append(Path(kwargs["env"]["TMPDIR"]))
        raise subprocess.TimeoutExpired(command, 20)

    monkeypatch.setattr(browser.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        browser._run_page("fake-chromium", tmp_path, "timeout", "<html></html>")
    assert observed and all(not path.exists() for path in observed)


def test_browser_crash_is_unverified_and_keeps_first_and_last_errors(monkeypatch, tmp_path):
    stderr = "FATAL: Socket path too long\n" + "stack trace\n" * 500 + "FINAL STACK FRAME"
    monkeypatch.setattr(browser.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, -6, "", stderr))
    with pytest.raises(browser.BrowserInfrastructureError) as error:
        browser._run_page("fake-chromium", tmp_path, "startup", "<html></html>")
    assert "FATAL: Socket path too long" in str(error.value)
    assert "FINAL STACK FRAME" in str(error.value)
    assert "output truncated" in str(error.value)
    assert "exit=-6" in str(error.value)


def test_page_assertion_failure_is_not_a_browser_startup_failure(monkeypatch, tmp_path):
    dom = '<pre id="browser-smoke-result">Expected Passed &amp; Failed filters</pre>'
    monkeypatch.setattr(browser.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, dom, ""))
    with pytest.raises(RuntimeError, match="Expected Passed & Failed filters") as error:
        browser._run_page("fake-chromium", tmp_path, "page", "<html></html>")
    assert not isinstance(error.value, browser.BrowserInfrastructureError)


@pytest.mark.parametrize("length", [0, 10, 2400])
def test_short_diagnostic_is_not_truncated(length):
    value = "x" * length
    assert browser._diagnostic_excerpt(value, 2400) == value
