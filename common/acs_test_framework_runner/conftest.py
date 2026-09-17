"""Retain structured evidence without changing pytest verdicts or assertions."""

from contextvars import ContextVar
import sys

import pytest

try:
    from .qa_evidence import ContractEvidence
except ImportError:
    from qa_evidence import ContractEvidence


_ACTIVE = ContextVar("parser_qa_evidence", default=None)
_STAGES = {
    "test_compliance": "merge", "test_schema_contract": "schema",
    "test_end_to_end": "end-to-end", "test_incomplete_bsa": "log-to-json",
    "test_artifact_validation": "artifact-validation", "test_pdf_export": "pdf",
    "test_fixture_contract": "log-expectations", "test_impact": "qa-classification",
    "test_workflow_reporting": "qa-reporting", "test_pytest_runner_selection": "qa-runner",
}


def pytest_configure(config):
    config.addinivalue_line("markers", "qa_context(suite, mode, stage): explicit failure evidence context")


@pytest.fixture(autouse=True)
def qa(request):
    evidence = ContractEvidence(request.node)
    token = _ACTIVE.set(evidence)
    yield evidence
    _ACTIVE.reset(token)


def pytest_assertrepr_compare(op, left, right):
    evidence = _ACTIVE.get()
    if evidence is not None:
        frame = sys._getframe(1)
        while frame and frame.f_code.co_filename != str(evidence.node.path):
            frame = frame.f_back
        location = (frame.f_code.co_filename, frame.f_lineno) if frame else None
        evidence.comparison = (left, right, op, location)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
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
    statement = str(call.excinfo.traceback[-1].statement) if call.excinfo else "Required check completes"
    actual = str(call.excinfo.value) if call.excinfo else report.longreprtext
    expected = statement
    if evidence.comparison is not None and call.excinfo:
        left, right, operator, location = evidence.comparison
        last = call.excinfo.traceback[-1]
        if location == (str(last.path), last.lineno + 1):
            actual = left
            expected = right if operator == "==" else {"operator": operator, "right": right}
    stage = _STAGES.get(item.path.stem, "qa-contract")
    stage = stage + "-setup" if report.when == "setup" else context.get("stage", stage)
    evidence.record(
        actual, expected, issue_id=item.originalname or item.name,
        suite=str(suite or ("BSA" if item.path.stem == "test_incomplete_bsa" else "shared")),
        mode=str(mode or "mode-independent"),
        stage=stage,
        status="BLOCKED" if report.when == "setup" else "FAIL",
    )
    report.user_properties = list(item.user_properties)
    return report
