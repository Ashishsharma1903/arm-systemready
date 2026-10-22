# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep parser CI opt-in by changed path and safe for untrusted pull requests."""

from pathlib import Path
import os
import re
import subprocess

import pytest
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


@pytest.mark.parametrize("available", [True, False])
def test_force_push_base_is_fetched_or_fails_without_changing_the_base(tmp_path, available):
    """A full clone still lacks an old tip removed by a force push."""
    def git(directory, *arguments, check=True):
        return subprocess.run(["git", "-C", str(directory), *arguments],
                              text=True, capture_output=True, check=check)

    remote, author, fresh = (tmp_path / name for name in ("remote.git", "author", "fresh"))
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True,
                   capture_output=True)
    # The local test server models GitHub's retained-commit SHA fetch support.
    git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    subprocess.run(["git", "init", "--initial-branch=main", str(author)], check=True, capture_output=True)
    git(author, "config", "user.name", "Parser CI test")
    git(author, "config", "user.email", "parser-ci@example.invalid")
    source = author / "common/log_parser/input.txt"
    source.parent.mkdir(parents=True)
    source.write_text("old parser revision\n")
    git(author, "add", ".")
    git(author, "commit", "-m", "old tip")
    old_tip = git(author, "rev-parse", "HEAD").stdout.strip()
    git(author, "remote", "add", "origin", str(remote))
    git(author, "push", "origin", "main")
    git(author, "checkout", "--orphan", "replacement")
    source.write_text("replacement parser revision\n")
    git(author, "add", ".")
    git(author, "commit", "-m", "replacement root")
    git(author, "push", "--force", "origin", "HEAD:main")
    # --no-local avoids copying unreachable loose objects from the bare repo.
    subprocess.run(["git", "clone", "--no-local", str(remote), str(fresh)],
                   check=True, capture_output=True)
    assert git(fresh, "cat-file", "-e", old_tip + "^{commit}", check=False).returncode != 0
    requested = old_tip if available else "1" * 40
    step = next(item for item in workflow()["jobs"]["parser-qa"]["steps"] if item.get("id") == "impact")
    prefix = step["run"].split("python common/acs_test_framework_runner/impact.py", 1)[0]
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c",
                             prefix + '\ngit rev-parse --verify "$BASE_SHA^{commit}"\n'],
                            cwd=fresh, env={**os.environ, "BASE_SHA": requested},
                            text=True, capture_output=True, check=False)
    if available:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == old_tip
        assert git(fresh, "cat-file", "-e", old_tip + "^{commit}").returncode == 0
    else:
        assert result.returncode != 0
        assert not result.stdout.strip(), "An unavailable nonzero base must not fall back to main"
