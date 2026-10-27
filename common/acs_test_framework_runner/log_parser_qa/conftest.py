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

"""Retain structured evidence without changing pytest verdicts or assertions."""

from contextvars import ContextVar
import sys

import pytest

try:
    from .qa_report import ContractEvidence, json_value, run_command
except ImportError:
    # Pytest also loads this file directly, with its directory on the import path.
    from qa_report import (  # pylint: disable=import-error
        ContractEvidence,
        json_value,
        run_command,
    )


_ACTIVE = ContextVar("parser_qa_evidence", default=None)
_STAGES = {
    "test_compliance": "merge", "test_end_to_end": "end-to-end",
    "test_artifact_validation": "artifact-validation", "test_qa_gate": "qa-gate",
    "test_qa_workflow": "qa-reporting", "test_pytest_runner_selection": "qa-runner",
}


def pytest_configure(config):
    """Register the marker that records a check's suite, mode and stage."""
    config.addinivalue_line(
        "markers", "qa_context(suite, mode, stage): explicit failure evidence context"
    )


@pytest.fixture(autouse=True)
def qa(request):
    """Keep each test's evidence separate and clear it when the test finishes."""
    evidence = ContractEvidence(request.node)
    token = _ACTIVE.set(evidence)
    yield evidence
    _ACTIVE.reset(token)


def pytest_assertrepr_compare(op, left, right):
    """Save the compared values and source location for a failed assertion."""
    evidence = _ACTIVE.get()
    if evidence is not None:
        frame = sys._getframe(1)
        while frame and frame.f_code.co_filename != str(evidence.node.path):
            frame = frame.f_back
        location = (frame.f_code.co_filename, frame.f_lineno) if frame else None
        evidence.comparison = (left, right, op, location)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Attach readable failure evidence without changing the test verdict."""
    report = yield
    if not report.failed or any(name == "qa_finding" for name, _ in item.user_properties):
        return report
    evidence = item.funcargs.get("qa") or ContractEvidence(item)
    params = getattr(getattr(item, "callspec", None), "params", {})
    case = next((params[key] for key in ("case", "generated_run", "outcome")
                 if isinstance(params.get(key), dict)), {})
    marker = item.get_closest_marker("qa_context")
    context = marker.kwargs if marker else {}
    suite = (context.get("suite") or params.get("suite") or case.get("suite")
             or ",".join(case.get("suites", [])))
    mode = context.get("mode") or params.get("mode") or case.get("mode")
    if isinstance(suite, dict):
        suite = suite.get("canonical") or suite.get("suite")
    statement = (
        str(call.excinfo.traceback[-1].statement) if call.excinfo else "Required check completes"
    )
    actual = str(call.excinfo.value) if call.excinfo else report.longreprtext
    expected = statement
    if evidence.comparison is not None and call.excinfo:
        left, right, operator, location = evidence.comparison
        last = call.excinfo.traceback[-1]
        if location == (str(last.path), last.lineno + 1):
            actual = left
            expected = right if operator == "==" else {"operator": operator, "right": right}
    stage = context.get("stage", _STAGES.get(item.path.stem, "qa-contract"))
    stage = stage + "-setup" if report.when == "setup" else stage
    evidence.record(
        actual, expected, issue_id=item.originalname or item.name,
        suite=str(suite or "shared"),
        mode=str(mode or "mode-independent"),
        stage=stage,
        status="BLOCKED" if report.when == "setup" else "FAIL",
    )
    report.user_properties = list(item.user_properties)
    return report
