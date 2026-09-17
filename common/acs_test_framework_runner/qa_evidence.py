"""Structured failure evidence shared by parser contract tests and CI reports."""

import json
import os
from pathlib import Path
import subprocess
import sys


def json_value(value):
    try:
        return json.loads(json.dumps(value, default=str), parse_constant=str)
    except (TypeError, ValueError):
        return repr(value)


def run_command(argv, cwd, evidence_dir, name="command", timeout=120):
    """Keep the exact command and output, including setup failures and timeouts."""
    argv = [str(part) for part in argv]
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / f"{name}.command.json").write_text(
        json.dumps({"argv": argv, "cwd": str(cwd)}, indent=2) + "\n"
    )
    log = evidence_dir / f"{name}.log"
    try:
        result = subprocess.run(
            argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, check=False, env={**os.environ, "MPLBACKEND": "Agg",
                "PYTHONNOUSERSITE": "1", "PYTHONPATH": "", "NO_COLOR": "1",
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")},
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        log.write_text(output.decode(errors="replace") if isinstance(output, bytes) else output)
        raise
    except OSError as error:
        log.write_text(str(error) + "\n")
        raise
    log.write_text(result.stdout)
    return result


class ContractEvidence:
    def __init__(self, node):
        self.node = node
        self.comparison = None

    def record(self, actual, expected, *, issue_id, suite, mode, stage,
               status="FAIL", reproduce=None, evidence=()):
        finding = {
            "issue_id": issue_id, "suite": suite, "mode": mode, "stage": stage,
            "status": status, "expected": json_value(expected), "actual": json_value(actual),
            "reproduce": reproduce or {
                "argv": ["python3", "-m", "pytest", "-q", self.node.nodeid], "cwd": ".",
            },
            "evidence": [str(path) for path in evidence],
        }
        self.node.user_properties.append(("qa_finding", json.dumps(finding, default=str)))
        return finding

    def check(self, actual, expected, **context):
        if actual != expected:
            finding = self.record(actual, expected, **context)
            raise AssertionError(
                f"{finding['status']} {finding['issue_id']} "
                f"[{finding['suite']} / {finding['mode']} / {finding['stage']}]\n"
                f"Expected: {finding['expected']!r}\nActual: {finding['actual']!r}"
            )
