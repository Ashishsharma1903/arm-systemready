"""Incomplete ACS rules must never disappear from a successful BSA parse."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SPEC = importlib.util.spec_from_file_location(
    "qa_bsa_logs", Path(__file__).resolve().parents[3] / "log_parser/bsa/logs_to_json.py"
)
BSA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BSA)


@pytest.mark.parametrize("text", [
    "*** Running PE tests ***\nB_PE_01 : 1 : Pass\nResult: PASSED\nB_PE_02 : 2 : Unfinished\n",
    "*** Running PE tests ***\nB_PE_01 : 1 : Unfinished\nB_PE_02 : 2 : Pass\nResult: PASSED\n",
    "START PE B_PE_01 1 : Unfinished\nSTART PE B_PE_02 2 : Pass\nEND B_PE_02 PASSED\n",
    "START PE PARENT 1 : Parent\n=== Start tests for rules referenced by PARENT ===\n"
    "START PE CHILD 2 : Child\nEND PARENT PASSED\nEND CHILD PASSED\n",
    "*** Running PE tests ***\nPARENT : 1 : Parent\n"
    "=== Start tests for rules referenced by PARENT ===\nCHILD : 2 : Unfinished\n"
    "SIBLING : 3 : Pass\nResult: PASSED\nResult: PASSED\nResult: PASSED\n",
], ids=["eof", "new-rule", "legacy-new-rule", "legacy-parent-before-child", "unfinished-sibling"])
@pytest.mark.qa_context(suite="BSA", mode="mode-independent", stage="log-to-json")
def test_incomplete_rule_preserves_existing_output(tmp_path, text):
    log, output = tmp_path / "BsaResults.log", tmp_path / "bsa.json"
    log.write_text(text)
    output.write_text("previous report")
    with pytest.raises(ValueError, match="incomplete ACS log.*missing Result/END"):
        BSA.main([str(log)], str(output))
    assert output.read_text() == "previous report"


@pytest.mark.qa_context(suite="BSA", mode="mode-independent", stage="log-to-json")
def test_unfinished_rule_cannot_continue_in_another_input_file(tmp_path):
    first, second = tmp_path / "first.log", tmp_path / "second.log"
    first.write_text("START PE B_PE_01 1 : Unfinished\n")
    second.write_text("START PE B_PE_02 2 : Pass\nEND B_PE_02 PASSED\n")
    output = tmp_path / "bsa.json"
    with pytest.raises(ValueError, match="B_PE_01"):
        BSA.main([str(first), str(second)], str(output))
    assert not output.exists()


@pytest.mark.parametrize("status", ["", "UNKNOWN", "not-a-result"],
                         ids=["empty", "unknown", "invalid"])
@pytest.mark.parametrize("template", [
    "*** Running PE tests ***\nB_PE_01 : 1 : Test\nResult: {status}\n",
    "START PE B_PE_01 1 : Test\nEND B_PE_01 {status}\n",
    "*** Running PE tests ***\nB_PE_01 : 1 : Test Result: {status}\n",
    "*** Running PE tests ***\nPARENT : 1 : Parent\n"
    "=== Start tests for rules referenced by PARENT ===\nB_PE_01 : 1 : Child\nResult: {status}\n"
    "=== End tests for rules referenced by PARENT ===\nResult: PASSED\n",
], ids=["modern", "legacy", "inline", "nested"])
@pytest.mark.qa_context(suite="BSA", mode="mode-independent", stage="log-to-json")
def test_unrecognized_results_preserve_existing_output(tmp_path, status, template):
    log, output = tmp_path / "BsaResults.log", tmp_path / "bsa.json"
    log.write_text(template.format(status=status))
    output.write_text("previous report")
    result = subprocess.run([sys.executable, str(BSA.__file__), str(log), str(output)],
                            capture_output=True, text=True, check=False, timeout=15)
    assert result.returncode != 0, "Unrecognized verdict was accepted: " + result.stdout + result.stderr
    assert output.read_text() == "previous report"


@pytest.mark.parametrize("text,expected", [
    ("*** Running PE tests ***\nB_PE_01 : 1 : Partial\nResult: PASSED (PARTIAL)\n", "PASSED(*PARTIAL)"),
    ("START PE B_PE_01 1 : Pass\nEND B_PE_01 PASSED\n", "PASSED"),
    ("*** Running PE tests ***\nB_PE_01 : 1 : Compact Result: PASSED\n", "PASSED"),
    ("*** Running PE tests ***\nPARENT : 1 : Parent\n"
     "=== Start tests for rules referenced by PARENT ===\nCHILD : 2 : Child\nResult: PASSED\n"
     "=== End tests for rules referenced by PARENT ===\nResult: PASSED\n", "PASSED"),
    *[(f"*** Running PE tests ***\nB_PE_01 : 1 : Test\nResult: {status}\n", result)
      for status, result in [
          ("FAILED", "FAILED"), ("FAILED WITH WAIVER", "FAILED (WITH WAIVER)"),
          ("SKIPPED", "SKIPPED"), ("PAL NOT SUPPORTED", "PAL NOT SUPPORTED"),
          ("TEST NOT IMPLEMENTED", "TEST NOT IMPLEMENTED"), ("WARNING", "WARNING"),
      ]],
], ids=["partial-result", "legacy", "inline", "nested", "failed", "waiver",
        "skipped", "unsupported", "unimplemented", "warning"])
@pytest.mark.qa_context(suite="BSA", mode="mode-independent", stage="log-to-json")
def test_completed_rules_remain_supported(tmp_path, text, expected):
    log, output = tmp_path / "BsaResults.log", tmp_path / "bsa.json"
    log.write_text(text)
    BSA.main([str(log)], str(output))
    data = json.loads(output.read_text())
    assert data["suite_summary"]["Total Rules Run"] == 1
    assert data["test_results"][0]["testcases"][0]["Test_result"] == expected
