# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep parser CI opt-in by changed path and safe for untrusted pull requests."""

from pathlib import Path
import re

import yaml


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/log-parser-qa.yml"


def workflow():
    # BaseLoader keeps the YAML key `on` as text, not the YAML 1.1 boolean True.
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_only_parser_changes_trigger_push_and_pr_jobs():
    events = workflow()["on"]
    assert set(events) == {"push", "pull_request"}
    for event in events.values():
        assert event == {"paths": ["common/log_parser/**"]}


def test_untrusted_code_has_no_write_token_or_privileged_trigger():
    config = workflow()
    assert config["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in config["on"]
    for job in config["jobs"].values():
        assert not job.get("secrets")
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
                if step["uses"].startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] == "false"


def test_new_suite_contracts_run_once_and_evidence_survives_failures():
    steps = workflow()["jobs"]["parser-qa"]["steps"]
    assert not any(step.get("id") == "onboarding" for step in steps)
    contracts = [step for step in steps if step.get("id") == "regression"]
    assert len(contracts) == 1
    assert "qa_run.py" in contracts[0]["run"]
    assert "--base" in contracts[0]["run"]
    selftests = next(step for step in steps if step.get("id") == "selftests")
    assert "--ignore=common/acs_test_framework_runner/tests/log_parser" in selftests["run"]
    summary = next(step for step in steps if step["name"] == "Summarize checks and verify JUnit reports")
    assert "--baseline-aware" in summary["run"]
    for name in ("Summarize checks and verify JUnit reports", "Upload test reports and full logs"):
        assert next(step for step in steps if step["name"] == name)["if"] == "${{ always() }}"


def test_replay_uses_base_revision_and_candidate_on_identical_inputs():
    step = next(step for step in workflow()["jobs"]["parser-qa"]["steps"]
                if step.get("id") == "replay")
    assert 'git archive "$base"' in step["run"]
    assert "--base-parser" in step["run"]
    assert "--candidate-parser common/log_parser" in step["run"]
