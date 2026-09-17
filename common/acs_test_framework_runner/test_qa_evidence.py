"""Failure evidence must preserve the original test outcome and reproduction."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from .qa_evidence import ContractEvidence, json_value, run_command


def test_explicit_failure_is_structured_and_blocking():
    node = SimpleNamespace(nodeid="test_probe.py::test_case[SR]", user_properties=[])
    evidence = ContractEvidence(node)
    context = dict(issue_id="missing-control", suite="SCT", mode="SR", stage="log-to-json")
    evidence.check(1, 1, **context)
    assert node.user_properties == []
    with pytest.raises(AssertionError, match="BLOCKED missing-control"):
        evidence.check(0, 1, status="BLOCKED", evidence=[Path("input.log")], **context)
    finding = json.loads(node.user_properties[0][1])
    assert finding["expected"] == 1 and finding["actual"] == 0
    assert finding["status"] == "BLOCKED"
    assert finding["reproduce"]["argv"][-1] == node.nodeid
    assert finding["evidence"] == ["input.log"]


def test_pytest_hooks_preserve_failures_errors_and_exact_scenarios(tmp_path):
    root = Path(__file__).resolve().parent
    for name in ("conftest.py", "qa_evidence.py"):
        shutil.copy2(root / name, tmp_path / name)
    (tmp_path / "test_probe.py").write_text('''import pytest
@pytest.mark.parametrize("suite,mode", [("BSA", "DT"), ("SBSA", "SR")])
def test_counts(suite, mode):
    actual = 0
    assert actual == 1
@pytest.fixture
def unavailable():
    raise RuntimeError("Required parser dependency unavailable")
def test_setup(unavailable):
    assert False, "must not execute"
def test_pass(qa):
    assert 1 == 1
@pytest.mark.qa_context(suite="BSA", mode="DT", stage="merge")
def test_caught_comparison_is_not_evidence_for_later_failure():
    with pytest.raises(AssertionError):
        assert 0 == 999
    assert False, "unrelated final failure"
@pytest.fixture(params=[{"suites": ["SCT", "BBSR-SCT"], "mode": "SR"}])
def generated_run(request):
    raise RuntimeError("Native control could not generate reports")
def test_generated_setup(generated_run):
    assert False, "must not execute"
@pytest.mark.qa_context(mode="SR", stage="end-to-end")
@pytest.mark.parametrize("outcome", [{"suite": "FWTS", "status": "failed"}])
def test_selected(outcome):
    assert "Compliant" == "Not Compliant"
''')
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--junitxml=result.xml"],
        cwd=tmp_path, text=True, capture_output=True, check=False, timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    cases = list(ET.parse(tmp_path / "result.xml").iter("testcase"))
    assert len(cases) == 7
    for case in cases:
        properties = {entry.get("name"): entry.get("value") for entry in case.findall("properties/property")}
        if case.get("name") == "test_pass":
            assert "qa_finding" not in properties
            continue
        finding = json.loads(properties["qa_finding"])
        assert case.get("name") in finding["reproduce"]["argv"][-1]
        if case.get("name").startswith("test_counts"):
            assert case.find("failure") is not None
            assert finding["actual"] == 0 and finding["expected"] == 1
            assert (finding["suite"], finding["mode"]) in (("BSA", "DT"), ("SBSA", "SR"))
        elif case.get("name") == "test_setup":
            assert case.find("error") is not None
            assert finding["status"] == "BLOCKED"
            assert finding["stage"] == "qa-contract-setup"
            assert "dependency unavailable" in finding["actual"]
        elif case.get("name").startswith("test_generated_setup"):
            assert case.find("error") is not None
            assert (finding["suite"], finding["mode"], finding["status"]) == ("SCT,BBSR-SCT", "SR", "BLOCKED")
            assert finding["stage"] == "qa-contract-setup"
        elif case.get("name").startswith("test_selected"):
            assert case.find("failure") is not None
            assert (finding["suite"], finding["mode"], finding["stage"]) == ("FWTS", "SR", "end-to-end")
        else:
            assert finding["status"] == "FAIL"
            assert (finding["suite"], finding["mode"], finding["stage"]) == ("BSA", "DT", "merge")
            assert "unrelated final failure" in finding["actual"]
            assert "unrelated final failure" in finding["expected"]


def test_evidence_json_handles_invalid_data_without_hiding_it():
    assert json_value(float("nan")) == "NaN"
    assert json_value({("invalid", "json-key"): 1}) == "{('invalid', 'json-key'): 1}"


@pytest.mark.parametrize("code", [0, 1])
def test_command_evidence_keeps_success_and_failure_output(tmp_path, code):
    argv = [sys.executable, "-c", f"print('fixture evidence'); raise SystemExit({code})"]
    result = run_command(argv, tmp_path, tmp_path, name="probe")
    assert result.returncode == code
    assert (tmp_path / "probe.log").read_text() == "fixture evidence\n"
    assert json.loads((tmp_path / "probe.command.json").read_text()) == {"argv": argv, "cwd": str(tmp_path)}


def test_command_setup_failure_keeps_reproduction(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_command([tmp_path / "missing-python"], tmp_path, tmp_path)
    assert "missing-python" in (tmp_path / "command.log").read_text()
    assert (tmp_path / "command.command.json").is_file()


def test_command_timeout_keeps_partial_output(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, "-u", "-c", "import time; print('started'); time.sleep(30)"],
                    tmp_path, tmp_path, timeout=1)
    assert "started" in (tmp_path / "command.log").read_text()
    assert (tmp_path / "command.command.json").is_file()
