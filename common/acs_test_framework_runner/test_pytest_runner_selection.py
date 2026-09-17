from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest


HARNESS_DIR = Path(__file__).resolve().parent
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))


def load_harness_module(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, HARNESS_DIR / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pytest_runner = load_harness_module("yaml_harness_pytest_runner", "pytest_runner.py")
report = load_harness_module("yaml_harness_report", "report.py")
runner_reporting = load_harness_module(
    "yaml_harness_runner_reporting",
    "runner_reporting.py",
)
runner_check_execution = load_harness_module(
    "yaml_harness_runner_check_execution",
    "runner_check_execution.py",
)


def test_harness_sources_changed_ignores_pycache() -> None:
    changed_paths = {
        pytest_runner.HARNESS_DIR / "__pycache__" / "pytest_runner.cpython-313.pyc",
    }

    assert pytest_runner.harness_sources_changed(changed_paths) is False


def test_select_yaml_runs_runs_all_groups_when_harness_changes(monkeypatch) -> None:
    yaml_files = [
        pytest_runner.PROJECT_ROOT / "common" / "acs_test_framework_manifests" / "group_one.yaml",
        pytest_runner.PROJECT_ROOT / "common" / "acs_test_framework_manifests" / "group_two.yaml",
    ]
    changed_paths = {pytest_runner.HARNESS_DIR / "mock_loader.py"}

    monkeypatch.setattr(
        pytest_runner,
        "get_recently_changed_paths",
        lambda: changed_paths,
    )
    monkeypatch.setattr(
        pytest_runner,
        "yaml_targets_changed_files",
        lambda yaml_file, _changed: ({f"{yaml_file.stem}.py"}, None),
    )
    monkeypatch.setattr(
        pytest_runner,
        "get_yaml_target_entries",
        lambda yaml_file: ({f"{yaml_file.stem}.py", f"{yaml_file.stem}_extra.py"}, None),
    )

    selected_runs, warnings = pytest_runner.select_yaml_runs(yaml_files)

    assert warnings == []
    assert selected_runs == [
        (yaml_files[0], {"group_one.py", "group_one_extra.py"}),
        (yaml_files[1], {"group_two.py", "group_two_extra.py"}),
    ]


def test_select_yaml_runs_preserves_target_only_selection(monkeypatch) -> None:
    yaml_files = [
        pytest_runner.PROJECT_ROOT
        / "common"
        / "acs_test_framework_manifests"
        / "group_one.yaml"
    ]

    monkeypatch.setattr(
        pytest_runner,
        "get_recently_changed_paths",
        lambda: {pytest_runner.PROJECT_ROOT / "common" / "linux_scripts" / "target.py"},
    )
    monkeypatch.setattr(
        pytest_runner,
        "yaml_targets_changed_files",
        lambda _yaml_file, _changed: ({"target.py"}, None),
    )
    get_yaml_target_entries_called = False

    def fake_get_yaml_target_entries(_yaml_file: Path) -> tuple[set[str], None]:
        nonlocal get_yaml_target_entries_called
        get_yaml_target_entries_called = True
        return {"target.py", "unrelated.py"}, None

    monkeypatch.setattr(
        pytest_runner,
        "get_yaml_target_entries",
        fake_get_yaml_target_entries,
    )

    selected_runs, warnings = pytest_runner.select_yaml_runs(yaml_files)

    assert warnings == []
    assert get_yaml_target_entries_called is False
    assert selected_runs == [(yaml_files[0], {"target.py"})]


def test_select_yaml_runs_for_directory_includes_only_descendants(monkeypatch) -> None:
    yaml_file = (
        pytest_runner.PROJECT_ROOT
        / "common"
        / "acs_test_framework_manifests"
        / "group_one.yaml"
    )

    monkeypatch.setattr(
        pytest_runner,
        "load_yaml_suites_for_selection",
        lambda _yaml_file: (
            [
                {
                    "files": [
                        "common/log_parser/bsa/logs_to_json.py",
                        "common/log_parser/scmi/json_to_html.py",
                        "common/linux_scripts/linux_dump.sh",
                    ]
                }
            ],
            None,
        ),
    )

    selected_runs, warnings = pytest_runner.select_yaml_runs_for_target(
        [yaml_file],
        "common/log_parser",
    )

    assert warnings == []
    assert selected_runs == [
        (
            yaml_file,
            {
                "common/log_parser/bsa/logs_to_json.py",
                "common/log_parser/scmi/json_to_html.py",
            },
        )
    ]


def test_select_yaml_runs_for_file_remains_exact(monkeypatch) -> None:
    yaml_file = (
        pytest_runner.PROJECT_ROOT
        / "common"
        / "acs_test_framework_manifests"
        / "group_one.yaml"
    )

    monkeypatch.setattr(
        pytest_runner,
        "load_yaml_suites_for_selection",
        lambda _yaml_file: (
            [
                {
                    "files": [
                        "common/log_parser/bsa/logs_to_json.py",
                        "common/log_parser/scmi/json_to_html.py",
                    ]
                }
            ],
            None,
        ),
    )

    selected_runs, warnings = pytest_runner.select_yaml_runs_for_target(
        [yaml_file],
        "common/log_parser/bsa/logs_to_json.py",
    )

    assert warnings == []
    assert selected_runs == [
        (yaml_file, {"common/log_parser/bsa/logs_to_json.py"})
    ]


def test_log_parser_catalog_covers_all_manifest_cases(tmp_path) -> None:
    groups, warnings = pytest_runner.discover_test_groups(
        pytest_runner.discover_yaml_files()
    )
    groups = pytest_runner.filter_test_groups_for_target(
        groups, "common/log_parser"
    )
    group_ids = {group.test_id for group in groups}

    assert warnings == []
    assert 20 <= len(groups) <= 25
    assert len(group_ids) == len(groups)
    expected_outcomes = sum(
        len(suite["cases"])
        * sum(target.startswith("common/log_parser/") for target in suite["files"])
        for manifest in pytest_runner.discover_yaml_files()
        for suite in pytest_runner.normalize_suites(
            pytest_runner.load_yaml_config(manifest)
        )
    )
    assert sum(group.outcome_count for group in groups) == expected_outcomes
    assert "log-parser-integrity::log_parser_integrity" in group_ids
    assert "logs-to-json::logs_to_json_common" in group_ids
    assert "json-to-html::json_to_html_common" in group_ids
    report_paths = pytest_runner.build_selected_report_paths(
        pytest_runner.selected_runs_from_groups(groups), tmp_path, []
    )
    assert {path.name for path in report_paths.values()} == {
        "acs-info.xml",
        "apply_waivers.xml",
        "generate_acs_summary.xml",
        "json-to-html.xml",
        "log-parser-integrity.xml",
        "logs-to-json.xml",
        "merge-jsons.xml",
        "merge-summary.xml",
        "report-ui.xml",
    }


def test_catalog_rejects_a_suite_without_cases(monkeypatch) -> None:
    yaml_file = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"
    monkeypatch.setattr(
        pytest_runner,
        "load_yaml_suites_for_selection",
        lambda _path: (
            [{"name": "broken", "files": ["parser.py"], "cases": []}],
            None,
        ),
    )

    groups, warnings = pytest_runner.discover_test_groups([yaml_file])

    assert groups == []
    assert warnings == [
        "common/acs_test_framework_manifests/logs-to-json.yaml "
        "suite 'broken' has no test cases"
    ]


def test_test_catalog_target_filter_distinguishes_same_basename() -> None:
    yaml_file = pytest_runner.TEST_YAML_DIR / "json-to-html.yaml"
    group = pytest_runner.TestGroup(
        test_id="json-to-html::renderers",
        yaml_file=yaml_file,
        suite_name="renderers",
        targets=(
            "common/log_parser/bsa/json_to_html.py",
            "common/log_parser/scmi/json_to_html.py",
        ),
        case_count=2,
    )

    selected = pytest_runner.filter_test_groups_for_target(
        [group],
        "common/log_parser/bsa/json_to_html.py",
    )

    assert len(selected) == 1
    assert selected[0].targets == ("common/log_parser/bsa/json_to_html.py",)
    assert selected[0].outcome_count == 2


def test_work_directories_include_the_full_target_path(tmp_path) -> None:
    first = pytest_runner.get_file_work_dir(
        "logs", "common/log_parser/bsa/logs_to_json.py", tmp_path
    )
    second = pytest_runner.get_file_work_dir(
        "logs", "common/log_parser/scmi/logs_to_json.py", tmp_path
    )

    assert first != second
    assert first.parent == second.parent


def test_selected_runs_from_groups_combines_manifest_targets() -> None:
    yaml_file = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"
    groups = [
        pytest_runner.TestGroup(
            test_id=f"logs-to-json::test-{index}",
            yaml_file=yaml_file,
            suite_name=f"test-{index}",
            targets=(target,),
            case_count=1,
        )
        for index, target in enumerate(
            (
                "common/log_parser/bsa/logs_to_json.py",
                "common/log_parser/scmi/logs_to_json.py",
            ),
            start=1,
        )
    ]

    assert pytest_runner.selected_runs_from_groups(groups) == [
        (
            yaml_file,
            {
                "common/log_parser/bsa/logs_to_json.py",
                "common/log_parser/scmi/logs_to_json.py",
            },
        )
    ]


def test_custom_report_name_and_default_name_are_both_supported(tmp_path) -> None:
    integrity = pytest_runner.TEST_YAML_DIR / "log-parser-integrity.yaml"
    logs = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"
    selected_runs = [
        (integrity, {"common/log_parser/standalone_runner.py"}),
        (logs, {"common/log_parser/bsa/logs_to_json.py"}),
    ]

    report_paths = pytest_runner.build_selected_report_paths(
        selected_runs,
        tmp_path,
        ["log-parser-integrity=methodology result.junit"],
    )

    assert report_paths[integrity] == tmp_path / "methodology result.junit"
    assert report_paths[logs] == tmp_path / "logs-to-json.xml"


@pytest.mark.parametrize(
    "options",
    (
        ["log-parser-integrity=../result.xml"],
        ["log-parser-integrity=folder/result.xml"],
        ["log-parser-integrity="],
        ["log-parser-integrity=_work"],
        ["same.xml", "other.xml"],
    ),
)
def test_invalid_custom_report_names_are_rejected(tmp_path, options) -> None:
    integrity = pytest_runner.TEST_YAML_DIR / "log-parser-integrity.yaml"
    selected_runs = [
        (integrity, {"common/log_parser/standalone_runner.py"}),
    ]

    with pytest.raises(pytest_runner.ConfigError):
        pytest_runner.build_selected_report_paths(
            selected_runs,
            tmp_path,
            options,
        )


def test_colliding_report_names_are_rejected(tmp_path) -> None:
    integrity = pytest_runner.TEST_YAML_DIR / "log-parser-integrity.yaml"
    logs = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"

    with pytest.raises(pytest_runner.ConfigError, match="collision"):
        pytest_runner.build_selected_report_paths(
            [
                (integrity, {"common/log_parser/standalone_runner.py"}),
                (logs, {"common/log_parser/bsa/logs_to_json.py"}),
            ],
            tmp_path,
            [
                "log-parser-integrity=result.xml",
                "logs-to-json=result.xml",
            ],
        )


def test_colliding_manifest_ids_are_rejected(tmp_path) -> None:
    first = pytest_runner.TEST_YAML_DIR / "nested" / "same.yaml"
    second = pytest_runner.TEST_YAML_DIR / "nested" / "same.yml"

    with pytest.raises(pytest_runner.ConfigError, match="Manifest ID collision"):
        pytest_runner.build_selected_report_paths(
            [(first, {"first.py"}), (second, {"second.py"})],
            tmp_path,
            [],
        )


@pytest.mark.parametrize("destination_type", ("directory", "symlink"))
def test_non_regular_report_destinations_are_rejected(
    tmp_path, destination_type
) -> None:
    integrity = pytest_runner.TEST_YAML_DIR / "log-parser-integrity.yaml"
    destination = tmp_path / "result.junit"
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    if destination_type == "directory":
        destination.mkdir()
    else:
        destination.symlink_to(victim)

    with pytest.raises(pytest_runner.ConfigError):
        pytest_runner.build_selected_report_paths(
            [(integrity, {"common/log_parser/standalone_runner.py"})],
            tmp_path,
            ["result.junit"],
        )

    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_reports_dir_must_be_a_directory(monkeypatch, tmp_path, capsys) -> None:
    output_file = tmp_path / "not-a-directory"
    output_file.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["pytest_runner.py", "--all-tests", "--reports-dir", str(output_file)],
    )

    assert pytest_runner.main() == 1
    assert "Report output path is not a directory" in capsys.readouterr().out
    assert output_file.read_text(encoding="utf-8") == "unchanged"


def test_run_yaml_executes_every_case_in_only_the_requested_group(
    monkeypatch,
    tmp_path,
) -> None:
    yaml_file = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"
    target = "common/log_parser/bsa/logs_to_json.py"
    selected_id = pytest_runner.build_test_id(yaml_file, "selected_parser")
    executed = []
    outcome = SimpleNamespace(
        passed=True,
        skipped=False,
        warning=False,
        error=False,
    )

    monkeypatch.setattr(pytest_runner, "load_yaml_config", lambda _path: {})
    monkeypatch.setattr(
        pytest_runner,
        "normalize_suites",
        lambda _config: [
            {
                "name": "selected_parser",
                "files": [target],
                "cases": [
                    {"name": "first_case", "type": "file_exists"},
                    {"name": "second_case", "type": "file_exists"},
                ],
            },
            {
                "name": "other_parser",
                "files": [target],
                "cases": [{"name": "must_not_run", "type": "file_exists"}],
            },
        ],
    )

    def fake_run_case(**kwargs):
        executed.append(kwargs["case_def"]["name"])
        return outcome

    monkeypatch.setattr(pytest_runner, "run_case", fake_run_case)
    monkeypatch.setattr(pytest_runner, "append_run_header", lambda **_kwargs: None)
    monkeypatch.setattr(pytest_runner, "write_junit_xml", lambda *_args: None)
    monkeypatch.setattr(pytest_runner, "print_group_summary", lambda *_args: None)

    exit_code = pytest_runner.run_yaml(
        yaml_file,
        selected_targets={target},
        options=pytest_runner.RunYamlOptions(
            selected_group_ids=frozenset({selected_id}),
            reports_dir=tmp_path,
            xml_report=tmp_path / "selected.xml",
            require_tests=True,
            jobs=1,
        ),
    )

    assert exit_code == 0
    assert executed == ["first_case", "second_case"]


def test_listing_does_not_clean_or_create_reports(
    monkeypatch,
    capsys,
) -> None:
    yaml_file = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"
    group = pytest_runner.TestGroup(
        test_id="logs-to-json::logs",
        yaml_file=yaml_file,
        suite_name="logs",
        targets=("common/log_parser/a.py",),
        case_count=2,
    )
    monkeypatch.setattr(pytest_runner, "discover_yaml_files", lambda: [yaml_file])
    monkeypatch.setattr(
        pytest_runner,
        "discover_test_groups",
        lambda _files: ([group], []),
    )
    monkeypatch.setattr(
        pytest_runner,
        "cleanup_old_pytest_xml_reports",
        lambda *_args: pytest.fail("listing must not clean reports"),
    )
    monkeypatch.setattr(sys, "argv", ["pytest_runner.py", "--list-tests"])

    assert pytest_runner.main() == 0
    output = capsys.readouterr().out
    assert group.test_id in output
    assert "1 groups, 2 checks" in output


def test_all_tests_selects_every_catalog_entry(monkeypatch, tmp_path) -> None:
    yaml_file = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"
    group = pytest_runner.TestGroup(
        test_id="logs-to-json::logs",
        yaml_file=yaml_file,
        suite_name="logs",
        targets=(
            "common/log_parser/bsa/logs_to_json.py",
            "common/log_parser/scmi/logs_to_json.py",
        ),
        case_count=1,
    )
    calls = []
    stale_report = tmp_path / "logs-to-json.xml"
    stale_report.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(pytest_runner, "discover_yaml_files", lambda: [yaml_file])
    monkeypatch.setattr(
        pytest_runner,
        "discover_test_groups",
        lambda _files: ([group], []),
    )

    def fake_run_yaml(yaml_path, **kwargs):
        calls.append((yaml_path, kwargs))
        return 0

    monkeypatch.setattr(pytest_runner, "run_yaml", fake_run_yaml)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pytest_runner.py",
            "--all-tests",
            "--reports-dir",
            str(tmp_path),
        ],
    )

    assert pytest_runner.main() == 0
    assert len(calls) == 1
    assert calls[0][0] == yaml_file
    assert calls[0][1]["selected_targets"] == {
        "common/log_parser/bsa/logs_to_json.py",
        "common/log_parser/scmi/logs_to_json.py",
    }
    assert calls[0][1]["options"].selected_group_ids is None
    assert not stale_report.exists()


def test_unknown_test_id_fails_before_report_cleanup(
    monkeypatch,
    capsys,
) -> None:
    yaml_file = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"
    group = pytest_runner.TestGroup(
        test_id="logs-to-json::logs",
        yaml_file=yaml_file,
        suite_name="logs",
        targets=("common/log_parser/a.py",),
        case_count=1,
    )
    monkeypatch.setattr(pytest_runner, "discover_yaml_files", lambda: [yaml_file])
    monkeypatch.setattr(
        pytest_runner,
        "discover_test_groups",
        lambda _files: ([group], []),
    )
    monkeypatch.setattr(
        pytest_runner,
        "cleanup_old_pytest_xml_reports",
        lambda *_args: pytest.fail("invalid selection must not clean reports"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pytest_runner.py", "--test", "unknown::test::id"],
    )

    assert pytest_runner.main() == 1
    assert "Unknown test group ID" in capsys.readouterr().out


def test_no_selected_tests_cleans_default_reports(monkeypatch, tmp_path) -> None:
    stale_report = tmp_path / "stale.xml"
    stale_work = tmp_path / "_work" / "stale" / "combined.log"
    stale_work.parent.mkdir(parents=True)
    stale_report.write_text("stale", encoding="utf-8")
    stale_work.write_text("stale", encoding="utf-8")
    yaml_file = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"

    monkeypatch.setattr(pytest_runner, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(pytest_runner, "discover_yaml_files", lambda: [yaml_file])
    monkeypatch.setattr(
        pytest_runner, "select_yaml_runs", lambda _files: ([], [])
    )
    monkeypatch.setattr(sys, "argv", ["pytest_runner.py"])

    assert pytest_runner.main() == 0
    assert not stale_report.exists()
    assert not stale_work.exists()
    assert (tmp_path / "pytest-placeholder.xml").is_file()


def test_invalid_report_name_preserves_existing_reports(
    monkeypatch, tmp_path
) -> None:
    yaml_file = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"
    group = pytest_runner.TestGroup(
        test_id="logs-to-json::logs",
        yaml_file=yaml_file,
        suite_name="logs",
        targets=("parser.py",),
        case_count=1,
    )
    stale_report = tmp_path / "stale.xml"
    stale_report.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(pytest_runner, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(pytest_runner, "discover_yaml_files", lambda: [yaml_file])
    monkeypatch.setattr(
        pytest_runner,
        "discover_test_groups",
        lambda _files: ([group], []),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pytest_runner.py", "--all-tests", "--report-name", "_work"],
    )

    assert pytest_runner.main() == 1
    assert stale_report.read_text(encoding="utf-8") == "stale"


def test_write_junit_xml_supports_external_custom_filename(tmp_path) -> None:
    report_path = tmp_path / "nested" / "custom methodology report.junit"
    outcome = SimpleNamespace(
        passed=True,
        skipped=False,
        warning=False,
        error=False,
        file_path="common/log_parser/example.py",
        testcase_name="example::passes",
        message="passed",
        details="",
        meta=SimpleNamespace(
            suite_name="example",
            phase="case",
            test_type="file_exists",
        ),
    )

    runner_reporting.write_junit_xml(
        report_path,
        "example",
        pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml",
        [outcome],
    )

    root = ET.parse(report_path).getroot()
    assert root.attrib["tests"] == "1"
    assert root.attrib["failures"] == "0"


def test_run_case_snapshots_declared_expectations(monkeypatch, tmp_path) -> None:
    case = {"name": "recorded-contract", "type": "cli", "expect_exit_code": 0,
            "expect_output": ["ready"], "post_checks": [{"type": "file_exists", "path": "result.json"}],
            "command": "python3"}

    def check(_file, effective_case, _directory):
        effective_case["post_checks"].append({"unexpected": "mutation"})
        return False, "Unexpected exit code", "Expected 0; actual 2", False

    monkeypatch.setattr(pytest_runner, "run_single_check", check)
    outcome = pytest_runner.run_case("parser_contract", "common/log_parser/bsa/logs_to_json.py", 0, case,
                                     pytest_runner.RunCaseOptions(reports_dir=tmp_path))
    assert not outcome.passed
    assert outcome.meta.expectations == {
        "expect_exit_code": 0, "expect_output": ["ready"],
        "post_checks": [{"type": "file_exists", "path": "result.json"}],
    }


@pytest.mark.parametrize("kind", ["passed", "warning", "failure", "error", "skipped"])
def test_junit_finding_metadata_preserves_outcomes_and_nested_selector(tmp_path, kind) -> None:
    meta = pytest_runner.TestMeta("parser_contract", "case", "cli", expectations={
        "expect_exit_code": 0, "post_checks": [{"type": "file_exists", "path": Path("result.json")}],
    })
    target = "common/log_parser/bsa/logs_to_json.py"
    outcome = pytest_runner.create_outcome(
        testcase_name="parser_contract::logs_to_json.py::checks", file_path=target,
        passed=kind in ("passed", "warning", "skipped"), message="Expected zero exit", meta=meta,
        details="Command failed with exit 2", error=kind == "error", skipped=kind == "skipped",
        warning=kind == "warning",
    )
    manifest = pytest_runner.TEST_YAML_DIR / "nested" / "parser.yaml"
    output = tmp_path / "custom-report.xml"
    runner_reporting.write_junit_xml(output, "parser", manifest, [outcome])
    suite = ET.parse(output).getroot()
    assert suite.attrib["tests"] == "1"
    assert suite.attrib["failures"] == str(int(kind == "failure"))
    assert suite.attrib["errors"] == str(int(kind == "error"))
    assert suite.attrib["skipped"] == str(int(kind == "skipped"))
    property_node = suite.find("testcase/properties/property[@name='qa_finding']")
    if kind in ("passed", "warning"):
        assert property_node is None
        return
    finding = json.loads(property_node.attrib["value"])
    assert finding["status"] == ("BLOCKED" if kind in ("error", "skipped") else "FAIL")
    assert finding["suite"] == "parser_contract"
    assert finding["mode"] == "not selected by YAML runner"
    assert finding["stage"] == target + ":cli"
    assert finding["expected"]["selector"] == "nested/parser::parser_contract"
    assert finding["expected"]["conditions"]["expect_exit_code"] == 0
    assert finding["expected"]["conditions"]["post_checks"][0]["path"] == "result.json"
    assert finding["actual"] == {"message": "Expected zero exit", "details": "Command failed with exit 2"}
    assert finding["reproduce"] == {"argv": ["python3", "common/acs_test_framework_runner/pytest_runner.py",
        "--test", "nested/parser::parser_contract", "--target", target, "--require-tests",
        "--fail-on-warnings", "--fail-on-skips"], "cwd": "."}


@pytest.mark.parametrize("location", ["relative", "custom-root", "external"])
def test_junit_metadata_handles_relative_and_unselectable_manifests(monkeypatch, tmp_path, location) -> None:
    project = tmp_path / "project"
    monkeypatch.setattr(runner_reporting, "PROJECT_ROOT", project)
    paths = {
        "relative": Path("common/acs_test_framework_manifests/nested/checks.yaml"),
        "custom-root": project / "custom-manifests/checks.yaml",
        "external": tmp_path / "external/checks.yaml",
    }
    outcome = pytest_runner.create_outcome(
        testcase_name="inventory::parser.py::exists", file_path="parser.py", passed=False,
        message="Missing parser", meta=pytest_runner.TestMeta("inventory", "case", "file_exists"),
    )
    output = tmp_path / "report.xml"
    runner_reporting.write_junit_xml(output, "checks", paths[location], [outcome])
    finding = json.loads(ET.parse(output).find("testcase/properties/property").attrib["value"])
    assert finding["expected"]["check_type"] == "file_exists"
    assert finding["expected"]["conditions"] == {}
    if location == "relative":
        assert finding["expected"]["selector"] == "nested/checks::inventory"
        assert finding["reproduce"]["argv"][3] == "nested/checks::inventory"
    else:
        assert finding["reproduce"] is None
        assert finding["expected"]["selector"] is None


def test_configuration_failure_reproduction_does_not_invent_a_test_group(tmp_path) -> None:
    manifest = pytest_runner.TEST_YAML_DIR / "invalid.yaml"
    outcome = runner_reporting.build_config_error_outcome(manifest, "Invalid configuration", "Missing cases")
    output = tmp_path / "config.xml"
    runner_reporting.write_junit_xml(output, "invalid", manifest, [outcome])
    finding = json.loads(ET.parse(output).find("testcase/properties/property").attrib["value"])
    command = finding["reproduce"]["argv"]
    assert "--all-tests" in command
    assert "--test" not in command
    assert finding["expected"]["selector"] is None


def test_run_yaml_require_tests_rejects_zero_outcomes(monkeypatch, tmp_path) -> None:
    yaml_file = (
        pytest_runner.PROJECT_ROOT
        / "common"
        / "acs_test_framework_manifests"
        / "logs-to-json.yaml"
    )
    report_file = (
        pytest_runner.PROJECT_ROOT
        / "common"
        / "reports"
        / "log-parser-test.xml"
    )
    written_outcomes = []

    monkeypatch.setattr(pytest_runner, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(pytest_runner, "build_report_path", lambda *_args: report_file)
    monkeypatch.setattr(pytest_runner, "load_yaml_config", lambda _path: {})
    monkeypatch.setattr(
        pytest_runner,
        "normalize_suites",
        lambda _config: [
            {
                "name": "parser",
                "files": ["common/log_parser/bsa/logs_to_json.py"],
                "cases": [{"name": "file_exists", "type": "file_exists"}],
            }
        ],
    )
    monkeypatch.setattr(
        pytest_runner,
        "write_junit_xml",
        lambda _path, _group, _yaml, outcomes: written_outcomes.extend(outcomes),
    )
    monkeypatch.setattr(pytest_runner, "print_group_summary", lambda *_args: None)

    exit_code = pytest_runner.run_yaml(
        yaml_file,
        selected_targets={"common/log_parser/not-in-manifest.py"},
        require_tests=True,
    )

    assert exit_code == 1
    assert len(written_outcomes) == 1
    assert written_outcomes[0].passed is False


@pytest.mark.parametrize("kind", ["warning", "skipped"])
def test_run_yaml_strict_outcomes_return_failure(monkeypatch, tmp_path, kind) -> None:
    yaml_file = (
        pytest_runner.PROJECT_ROOT
        / "common"
        / "acs_test_framework_manifests"
        / "logs-to-json.yaml"
    )
    report_file = pytest_runner.PROJECT_ROOT / "common" / "reports" / "warning.xml"
    target = "common/log_parser/bsa/logs_to_json.py"
    warning_outcome = pytest_runner.create_outcome(
        testcase_name="parser::warning",
        file_path=target,
        passed=True,
        message="Original warning",
        meta=pytest_runner.TestMeta(suite_name="parser", phase="case", test_type="file_exists"),
        skipped=kind == "skipped",
        warning=kind == "warning",
    )

    monkeypatch.setattr(pytest_runner, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(pytest_runner, "build_report_path", lambda *_args: report_file)
    monkeypatch.setattr(pytest_runner, "load_yaml_config", lambda _path: {})
    monkeypatch.setattr(
        pytest_runner,
        "normalize_suites",
        lambda _config: [
            {
                "name": "parser",
                "files": [target],
                "cases": [{"name": "warning", "type": "file_exists"}],
            }
        ],
    )
    monkeypatch.setattr(pytest_runner, "run_case", lambda **_kwargs: warning_outcome)
    monkeypatch.setattr(pytest_runner, "append_run_header", lambda **_kwargs: None)
    monkeypatch.setattr(pytest_runner, "write_junit_xml", lambda *_args: None)
    monkeypatch.setattr(pytest_runner, "print_group_summary", lambda *_args: None)

    assert (
        pytest_runner.run_yaml(
            yaml_file,
            selected_targets={target},
            options=pytest_runner.RunYamlOptions(
                jobs=1, fail_on_warnings=True, fail_on_skips=True
            ),
        )
        == 1
    )
    assert (
        pytest_runner.run_yaml(
            yaml_file,
            selected_targets={target},
            options=pytest_runner.RunYamlOptions(jobs=1),
        )
        == 0
    )


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("jobs", [1, 4])
def test_strict_warnings_have_failure_xml_and_console_details(
    monkeypatch, tmp_path, capsys, strict, jobs,
) -> None:
    yaml_file = pytest_runner.TEST_YAML_DIR / "logs-to-json.yaml"
    target = "common/log_parser/bsa/logs_to_json.py"
    report_file = tmp_path / "warning.xml"
    message = "Expected Failed: 1 but got Failed: 0"
    details = "Original command output and count mismatch details"
    monkeypatch.setattr(pytest_runner, "load_yaml_config", lambda _path: {})
    monkeypatch.setattr(pytest_runner, "normalize_suites", lambda _config: [{
        "name": "parser", "files": [target], "cases": [
            {"name": "passing", "type": "file_exists"},
            {"name": "warning", "type": "file_exists", "warn_only": True},
        ],
    }])
    monkeypatch.setattr(
        pytest_runner, "run_single_check",
        lambda _path, case, _work: (case["name"] == "passing", message, details, False),
    )

    result = pytest_runner.run_yaml(
        yaml_file, selected_targets={target},
        options=pytest_runner.RunYamlOptions(
            jobs=jobs, fail_on_warnings=strict, reports_dir=tmp_path, xml_report=report_file,
        ),
    )

    assert result == int(strict)
    report = ET.parse(report_file).getroot()
    assert report.attrib["tests"] == "2"
    assert report.attrib["failures"] == str(int(strict))
    assert report.attrib["errors"] == report.attrib["skipped"] == "0"
    properties = {item.attrib["name"]: item.attrib["value"] for item in report.findall("properties/property")}
    assert properties["passed"] == "1"
    assert properties["warnings"] == str(int(not strict))
    warning = report.findall("testcase")[1]
    failure = warning.find("failure")
    assert details in warning.findtext("system-out")
    if strict:
        assert failure is not None
        assert failure.attrib["message"] == f"Warning treated as failure: {message}"
        assert failure.text == details
    else:
        assert failure is None
        assert "warning=True" in warning.findtext("system-out")
    console = capsys.readouterr().out
    assert f"Failed  : {int(strict)}" in console
    assert f"Warnings: {int(not strict)}" in console
    assert ("Warning treated as failure:" in console) is strict
    if strict:
        assert message in console


def test_passed_post_check_for_failed_text_is_not_a_failure() -> None:
    messages = [
        "post_check file_contains: detail.html contains 'FAILED' -> PASS",
        "post_check exists: summary.html -> FAIL",
    ]

    assert runner_check_execution.failed_post_check_messages(messages) == [
        "post_check exists: summary.html -> FAIL"
    ]


def test_report_changed_yaml_adds_python_targets_for_static_checks(monkeypatch) -> None:
    yaml_path = report.PROJECT_ROOT / "common" / "acs_test_framework_manifests" / "group_one.yaml"
    yaml_target = report.PROJECT_ROOT / "common" / "linux_scripts" / "target.py"
    direct_python_change = report.PROJECT_ROOT / "common" / "linux_scripts" / "direct.py"

    monkeypatch.setattr(
        report,
        "get_recently_changed_paths",
        lambda: [yaml_path, direct_python_change],
    )
    monkeypatch.setattr(
        report,
        "is_yaml_suite_file",
        lambda changed_path: changed_path == yaml_path,
    )
    monkeypatch.setattr(
        report,
        "get_python_targets_from_yaml_file",
        lambda changed_yaml: {yaml_target} if changed_yaml == yaml_path else set(),
    )

    assert report.get_recently_changed_python_files() == [
        direct_python_change,
        yaml_target,
    ]


def test_report_yaml_target_filter_keeps_only_existing_python_files(monkeypatch) -> None:
    yaml_path = report.PROJECT_ROOT / "common" / "acs_test_framework_manifests" / "group_one.yaml"
    py_target = report.PROJECT_ROOT / "common" / "linux_scripts" / "target.py"
    sh_target = report.PROJECT_ROOT / "common" / "linux_scripts" / "target.sh"

    monkeypatch.setattr(report, "load_yaml_config", lambda _yaml_file: {"suites": []})
    monkeypatch.setattr(
        report,
        "normalize_suites",
        lambda _config: [
            {
                "files": [
                    "common/linux_scripts/target.py",
                    "common/linux_scripts/target.sh",
                ]
            }
        ],
    )

    def fake_exists(self) -> bool:
        return self in {yaml_path, py_target, sh_target}

    def fake_is_file(self) -> bool:
        return self in {yaml_path, py_target, sh_target}

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "is_file", fake_is_file)

    assert report.get_python_targets_from_yaml_file(yaml_path) == {py_target}


def test_collect_pytest_case_logs_reads_persisted_case_description(
    monkeypatch,
) -> None:
    temp_path = HARNESS_DIR / "_case_log_test_reports"
    shutil.rmtree(temp_path, ignore_errors=True)
    try:
        runner_reporting.append_combined_case_log(
            file_work_dir=temp_path / "_work" / "suite" / "target",
            testcase_name="suite::target.py::case_with_description",
            status="PASS",
            message="case passed",
            details="",
            description="First line of description\nSecond line of description",
        )
        monkeypatch.setattr(report, "REPORTS_DIR", temp_path)

        assert report.collect_pytest_case_logs() == [
            {
                "name": "suite::target.py::case_with_description",
                "description": "First line of description Second line of description",
                "status": "passed",
                "details": "case passed",
            }
        ]
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)


def test_collect_pytest_case_logs_finds_full_target_work_name(
    monkeypatch,
) -> None:
    temp_path = HARNESS_DIR / "_target_case_log_test_reports"
    target = report.PROJECT_ROOT / "common/log_parser/bsa/logs_to_json.py"
    shutil.rmtree(temp_path, ignore_errors=True)
    try:
        runner_reporting.append_combined_case_log(
            file_work_dir=(
                temp_path
                / "_work"
                / "suite"
                / report.target_work_name(target)
            ),
            testcase_name="suite::logs_to_json.py::file_exists",
            status="PASS",
            message="case passed",
            details="",
        )
        monkeypatch.setattr(report, "REPORTS_DIR", temp_path)

        results = report.collect_pytest_case_logs(str(target))

        assert [item["name"] for item in results] == [
            "suite::logs_to_json.py::file_exists"
        ]
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)


def test_collect_pytest_case_logs_skips_missing_case_description(
    monkeypatch,
) -> None:
    temp_path = HARNESS_DIR / "_case_log_test_reports"
    shutil.rmtree(temp_path, ignore_errors=True)
    try:
        runner_reporting.append_combined_case_log(
            file_work_dir=temp_path / "_work" / "suite" / "target",
            testcase_name="suite::target.py::case_without_description",
            status="PASS",
            message="case passed",
            details="",
        )
        monkeypatch.setattr(report, "REPORTS_DIR", temp_path)

        assert report.collect_pytest_case_logs() == [
            {
                "name": "suite::target.py::case_without_description",
                "description": "",
                "status": "passed",
                "details": "case passed",
            }
        ]
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)
