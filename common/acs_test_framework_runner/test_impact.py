"""Change classification must not mistake new execution paths for existing suites."""

from copy import deepcopy
import json
import subprocess

import pytest

from .impact import REGISTRY, classify_changes, inspect_change


@pytest.fixture
def registry():
    return {"suites": [{"canonical": "BSA", "logs_to_json": "bsa/logs_to_json.py"}],
            "standalone": {"suite_execution": {"BSA": {"handler": "multi_log"}}}}


@pytest.mark.parametrize("path,kind", [
    ("common/log_parser/bsa/logs_to_json.py", "existing-suite"),
    ("common/log_parser/acs-results-schema.json", "shared-schema-qa"),
    ("common/log_parser/merge_jsons.py", "shared-schema-qa"),
    ("common/acs_test_framework_manifests/logs-to-json.yaml", "shared-schema-qa"),
    ("common/acs_test_framework_runner/impact.py", "shared-schema-qa"),
    (".github/workflows/log-parser-qa.yml", "shared-schema-qa"),
    ("README.md", "no-parser-change"),
])
def test_existing_shared_and_unrelated_changes(registry, path, kind):
    result = classify_changes([path], registry, registry, {"common/log_parser/bsa/logs_to_json.py"})
    assert result["kinds"] == [kind]
    assert result["new_suites"] == []


@pytest.mark.parametrize("entry", ["canonical", "execution"])
def test_new_registry_entry_in_existing_directory(registry, entry):
    updated = deepcopy(registry)
    if entry == "canonical":
        updated["suites"].append({"canonical": "NEW", "logs_to_json": "bsa/logs_to_json.py"})
    else:
        updated["standalone"]["suite_execution"]["NEW"] = {"handler": "multi_log"}
    result = classify_changes([REGISTRY], registry, updated, {REGISTRY})
    assert result["new_suites"] == ["NEW"]
    assert result["kinds"] == ["new-suite", "shared-schema-qa"]


def test_existing_metadata_is_not_a_new_suite(registry):
    updated = deepcopy(registry)
    updated["suites"][0]["requirements"] = {"DT": "R"}
    updated["standalone"]["suite_execution"]["BSA"]["handler"] = "single_log"
    result = classify_changes([REGISTRY], registry, updated, {REGISTRY})
    assert result["kinds"] == ["shared-schema-qa"]
    assert result["new_suites"] == []


def test_new_directory_is_detected_without_registry_entry(registry):
    result = classify_changes(["common/log_parser/new_suite/logs_to_json.py"],
                              registry, registry, {"common/log_parser/bsa/logs_to_json.py"})
    assert result["new_suites"] == ["directory: new_suite"]


def test_git_comparison_uses_committed_data_and_zero_base_fallback(tmp_path, registry):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "qa@example.invalid")
    git("config", "user.name", "Parser QA")
    path = tmp_path / REGISTRY
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(registry))
    git("add", ".")
    git("commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD")
    first_push = inspect_change(tmp_path, "0" * 40)
    assert first_push["base"] == ""
    assert first_push["new_suites"] == ["BSA"]
    registry["standalone"]["suite_execution"]["NEW"] = {}
    path.write_text(json.dumps(registry))
    git("add", ".")
    git("commit", "-qm", "add execution")
    path.write_text("uncommitted invalid JSON")
    result = inspect_change(tmp_path, "0" * 40)
    assert result["base"] == baseline
    assert result["new_suites"] == ["NEW"]
    with pytest.raises(subprocess.CalledProcessError):
        inspect_change(tmp_path, "--output=untrusted")
    assert not (tmp_path / "untrusted").exists()
