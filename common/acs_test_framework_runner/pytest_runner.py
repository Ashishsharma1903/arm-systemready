from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Support package imports and direct harness module loading.
    from .case_data_builders import CaseBuildError
    from .runner_checks import (
        ConfigError,
        RunCaseOptions,
        SkipCase,
        TestMeta,
        TestOutcome,
        create_outcome,
        detect_project_root,
        format_outcome_message,
        load_yaml_config,
        normalize_suites,
        resolve_target_path,
        run_single_check,
        sanitize_name,
        target_work_name,
    )
    from .runner_reporting import (
        append_combined_case_log,
        append_run_header,
        build_config_error_outcome,
        build_report_path,
        cleanup_old_pytest_xml_reports,
        create_placeholder_xml,
        print_group_summary,
        remove_placeholder_xml,
        sanitize_xml_text,
        write_case_log,
        write_junit_xml,
    )
except ImportError:  # pragma: no cover - exercised by flat-module harness imports.
    from case_data_builders import CaseBuildError
    from runner_checks import (
        ConfigError,
        RunCaseOptions,
        SkipCase,
        TestMeta,
        TestOutcome,
        create_outcome,
        detect_project_root,
        format_outcome_message,
        load_yaml_config,
        normalize_suites,
        resolve_target_path,
        run_single_check,
        sanitize_name,
        target_work_name,
    )
    from runner_reporting import (
        append_combined_case_log,
        append_run_header,
        build_config_error_outcome,
        build_report_path,
        cleanup_old_pytest_xml_reports,
        create_placeholder_xml,
        print_group_summary,
        remove_placeholder_xml,
        sanitize_xml_text,
        write_case_log,
        write_junit_xml,
    )

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = detect_project_root(SCRIPT_DIR)
HARNESS_DIR = PROJECT_ROOT / "common" / "acs_test_framework_runner"
TEST_YAML_DIR = PROJECT_ROOT / "common" / "acs_test_framework_manifests"
REPORTS_DIR = PROJECT_ROOT / "common" / "reports"
SUPPORTED_SUFFIXES = {".yaml", ".yml"}
IN_PROCESS_CASE_TYPES = {"py_function", "module_main_with_env", "module_cli"}
RESERVED_REPORT_FILENAMES = {
    "_runner_work",
    "_work",
    "pytest-placeholder.xml",
}


@dataclass(frozen=True)
class TestGroup:
    test_id: str
    yaml_file: Path
    suite_name: str
    targets: tuple[str, ...]
    case_count: int

    @property
    def outcome_count(self) -> int:
        return len(self.targets) * self.case_count


@dataclass(frozen=True)
class RunYamlOptions:
    jobs: int = 4
    require_tests: bool = False
    fail_on_warnings: bool = False
    fail_on_skips: bool = False
    selected_group_ids: frozenset[str] | None = None
    reports_dir: Path | None = None
    xml_report: Path | None = None


def discover_yaml_files() -> list[Path]:
    if not TEST_YAML_DIR.exists() or not TEST_YAML_DIR.is_dir():
        return []
    return sorted(
        path
        for path in TEST_YAML_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def get_group_name(yaml_file: Path) -> str:
    rel_path = yaml_file.relative_to(TEST_YAML_DIR)
    return rel_path.parts[0] if len(rel_path.parts) > 1 else yaml_file.stem


def get_manifest_id(yaml_file: Path) -> str:
    return yaml_file.relative_to(TEST_YAML_DIR).with_suffix("").as_posix()


def build_test_id(yaml_file: Path, suite_name: str) -> str:
    return f"{get_manifest_id(yaml_file)}::{suite_name}"


def display_path(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def resolve_reports_dir(value: str | None) -> Path:
    if not value:
        return REPORTS_DIR
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ConfigError(f"Report output path is not a directory: {path}")
    return path


def cleanup_default_reports(reports_dir: Path) -> None:
    if reports_dir != REPORTS_DIR.resolve():
        return
    reports_dir.mkdir(parents=True, exist_ok=True)
    cleanup_old_pytest_xml_reports(reports_dir)


def get_file_work_dir(
    suite_name: str,
    file_entry: str,
    reports_dir: Path | None = None,
) -> Path:
    reports_root = REPORTS_DIR if reports_dir is None else Path(reports_dir)
    return (
        reports_root
        / "_work"
        / sanitize_name(suite_name)
        / target_work_name(file_entry)
    )


def requires_serial_case_execution(suite_cases: list[dict[str, Any]]) -> bool:
    return any(
        str(case_def.get("type", "cli")) in IN_PROCESS_CASE_TYPES
        for case_def in suite_cases
    )


def run_case(
    suite_name: str,
    file_entry: str,
    case_index: int,
    case_def: dict[str, Any],
    options: RunCaseOptions | None = None,
) -> TestOutcome:
    if options is None:
        options = RunCaseOptions()

    file_path = resolve_target_path(file_entry)
    case_name = case_def["name"].strip()
    testcase_name = f"{suite_name}::{Path(file_entry).name}::{case_name}"
    case_type = str(case_def.get("type", "cli"))

    file_work_dir = get_file_work_dir(
        suite_name,
        file_entry,
        reports_dir=options.reports_dir,
    )
    work_dir = file_work_dir / sanitize_name(f"{case_index}_{case_name}")
    work_dir.mkdir(parents=True, exist_ok=True)

    effective_case = dict(case_def)
    if options.suite_command is not None and "command" not in effective_case:
        effective_case["command"] = options.suite_command

    meta = TestMeta(
        suite_name=suite_name,
        phase="case",
        test_type=case_type,
    )

    def persist_case_logs(status: str, outcome: TestOutcome) -> None:
        case_description = effective_case.get("description")
        append_combined_case_log(
            file_work_dir=file_work_dir,
            testcase_name=testcase_name,
            status=status,
            message=outcome.message,
            details=outcome.details,
            description=case_description,
        )
        write_case_log(
            case_work_dir=work_dir,
            testcase_name=testcase_name,
            status=status,
            message=outcome.message,
            details=outcome.details,
            description=case_description,
        )

    try:
        passed, message, details, is_error = run_single_check(
            file_path, effective_case, work_dir
        )

        warn_only = bool(effective_case.get("warn_only", False))
        final_passed = passed
        final_warning = False

        if warn_only and not passed and not is_error:
            final_passed = True
            final_warning = True

        outcome = create_outcome(
            testcase_name=testcase_name,
            file_path=file_entry,
            passed=final_passed,
            message=sanitize_xml_text(message),
            meta=meta,
            details=sanitize_xml_text(details),
            error=is_error,
            warning=final_warning,
        )

        status = (
            "ERROR"
            if is_error
            else (
                "WARNING"
                if outcome.warning
                else ("PASS" if outcome.passed else "FAIL")
            )
        )
        persist_case_logs(status, outcome)
        return outcome
    except SkipCase as exc:
        outcome = create_outcome(
            testcase_name=testcase_name,
            file_path=file_entry,
            passed=True,
            message=sanitize_xml_text(str(exc)),
            meta=meta,
            details=sanitize_xml_text(
                f"Case skipped in work directory: {work_dir}\nReason: {exc}"
            ),
            skipped=True,
        )
        persist_case_logs("SKIPPED", outcome)
        return outcome
    except (ConfigError, CaseBuildError) as exc:
        outcome = create_outcome(
            testcase_name=testcase_name,
            file_path=file_entry,
            passed=False,
            message=sanitize_xml_text(
                format_outcome_message("Configuration error", str(exc))
            ),
            meta=meta,
            details=sanitize_xml_text(traceback.format_exc()),
            error=True,
        )
        persist_case_logs("ERROR", outcome)
        return outcome
    except Exception as exc:  # pylint: disable=broad-exception-caught
        outcome = create_outcome(
            testcase_name=testcase_name,
            file_path=file_entry,
            passed=False,
            message=sanitize_xml_text(
                format_outcome_message("Unhandled exception", str(exc))
            ),
            meta=meta,
            details=sanitize_xml_text(traceback.format_exc()),
            error=True,
        )
        persist_case_logs("ERROR", outcome)
        return outcome


def git_changed_paths_for_query(args: list[str]) -> set[Path]:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return set()

    paths: set[Path] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        paths.add((PROJECT_ROOT / line).resolve())
    return paths


def get_recently_changed_paths() -> set[Path]:
    changed: set[Path] = set()
    changed.update(git_changed_paths_for_query(["diff", "--name-only"]))
    changed.update(git_changed_paths_for_query(["diff", "--cached", "--name-only"]))
    changed.update(
        git_changed_paths_for_query(["ls-files", "--others", "--exclude-standard"])
    )
    return changed


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def harness_sources_changed(changed_paths: set[Path]) -> bool:
    for changed_path in changed_paths:
        if not path_is_within(changed_path, HARNESS_DIR):
            continue
        if "__pycache__" in changed_path.parts:
            continue
        return True
    return False


def normalize_target_for_matching(file_entry: str) -> Path:
    return resolve_target_path(file_entry)


def format_yaml_config_warning(yaml_file: Path, message: str) -> str:
    rel_path = yaml_file.relative_to(PROJECT_ROOT).as_posix()
    return f"Skipping {rel_path} due to configuration error: {message}"


def load_yaml_suites_for_selection(
    yaml_file: Path,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        config = load_yaml_config(yaml_file)
        suites = normalize_suites(config)
    except ConfigError as exc:
        return None, format_yaml_config_warning(yaml_file, str(exc))
    return suites, None


def get_yaml_target_entries(yaml_file: Path) -> tuple[set[str], str | None]:
    suites, warning = load_yaml_suites_for_selection(yaml_file)
    if suites is None:
        return set(), warning

    targets: set[str] = set()
    for suite in suites:
        for file_entry in suite["files"]:
            targets.add(Path(file_entry).as_posix())
    return targets, None


def yaml_targets_changed_files(
    yaml_file: Path,
    changed_paths: set[Path],
) -> tuple[set[str], str | None]:
    suites, warning = load_yaml_suites_for_selection(yaml_file)
    if suites is None:
        return set(), warning

    matched: set[str] = set()
    for suite in suites:
        for file_entry in suite["files"]:
            target_path = normalize_target_for_matching(file_entry)
            if target_path in changed_paths:
                matched.add(Path(file_entry).as_posix())
    return matched, None


def select_yaml_runs(
    yaml_files: list[Path],
) -> tuple[list[tuple[Path, set[str]]], list[str]]:
    changed_paths = get_recently_changed_paths()

    if not changed_paths:
        return [], []

    run_all_yaml_groups = harness_sources_changed(changed_paths)
    selected_map: dict[Path, set[str]] = {}
    config_warnings: list[str] = []

    for yaml_file in yaml_files:
        yaml_abs = yaml_file.resolve()
        matched_targets, warning = yaml_targets_changed_files(yaml_file, changed_paths)
        if warning is not None:
            config_warnings.append(warning)

        if run_all_yaml_groups or yaml_abs in changed_paths:
            yaml_targets, target_warning = get_yaml_target_entries(yaml_file)
            if target_warning is not None and target_warning not in config_warnings:
                config_warnings.append(target_warning)
            matched_targets.update(yaml_targets)

        if matched_targets:
            selected_map[yaml_file] = matched_targets

    return list(selected_map.items()), config_warnings


def select_yaml_runs_for_target(
    yaml_files: list[Path],
    target: str,
) -> tuple[list[tuple[Path, set[str]]], list[str]]:
    target_path = resolve_target_path(target)
    target_is_directory = target_path.is_dir()
    selected_runs: list[tuple[Path, set[str]]] = []
    config_warnings: list[str] = []

    for yaml_file in yaml_files:
        suites, warning = load_yaml_suites_for_selection(yaml_file)
        if suites is None:
            if warning is not None:
                config_warnings.append(warning)
            continue

        matched_targets: set[str] = set()
        for suite in suites:
            for file_entry in suite["files"]:
                file_path = resolve_target_path(file_entry)
                if (
                    file_path == target_path
                    or (
                        target_is_directory
                        and path_is_within(file_path, target_path)
                    )
                ):
                    matched_targets.add(Path(file_entry).as_posix())

        if matched_targets:
            selected_runs.append((yaml_file, matched_targets))

    return selected_runs, config_warnings


def discover_test_groups(
    yaml_files: list[Path],
) -> tuple[list[TestGroup], list[str]]:
    groups: list[TestGroup] = []
    warnings: list[str] = []
    seen: dict[str, Path] = {}

    for yaml_file in yaml_files:
        suites, warning = load_yaml_suites_for_selection(yaml_file)
        if suites is None:
            if warning is not None:
                warnings.append(warning)
            continue

        for suite in suites:
            suite_name = suite["name"]
            if not suite["cases"]:
                warnings.append(
                    f"{display_path(yaml_file)} suite {suite_name!r} "
                    "has no test cases"
                )
                continue
            test_id = build_test_id(yaml_file, suite_name)
            if test_id in seen:
                warnings.append(
                    f"Duplicate test group ID {test_id!r} in "
                    f"{display_path(seen[test_id])} and "
                    f"{display_path(yaml_file)}"
                )
                continue
            seen[test_id] = yaml_file
            groups.append(
                TestGroup(
                    test_id=test_id,
                    yaml_file=yaml_file,
                    suite_name=suite_name,
                    targets=tuple(
                        Path(file_entry).as_posix()
                        for file_entry in suite["files"]
                    ),
                    case_count=len(suite["cases"]),
                )
            )

    return groups, warnings


def filter_test_groups_for_target(
    groups: list[TestGroup],
    target: str | None,
) -> list[TestGroup]:
    if not target:
        return groups

    target_path = resolve_target_path(target)
    target_is_directory = target_path.is_dir()
    scoped_groups: list[TestGroup] = []
    for group in groups:
        targets = tuple(
            file_entry
            for file_entry in group.targets
            if (
                resolve_target_path(file_entry) == target_path
                or (
                    target_is_directory
                    and path_is_within(
                        resolve_target_path(file_entry),
                        target_path,
                    )
                )
            )
        )
        if targets:
            scoped_groups.append(
                TestGroup(
                    test_id=group.test_id,
                    yaml_file=group.yaml_file,
                    suite_name=group.suite_name,
                    targets=targets,
                    case_count=group.case_count,
                )
            )
    return scoped_groups


def selected_runs_from_groups(
    groups: list[TestGroup],
) -> list[tuple[Path, set[str]]]:
    selected: dict[Path, set[str]] = {}
    for group in groups:
        selected.setdefault(group.yaml_file, set()).update(group.targets)
    return list(selected.items())


def print_test_catalog(groups: list[TestGroup]) -> None:
    outcome_count = sum(group.outcome_count for group in groups)
    print(f"Available YAML test groups ({len(groups)} groups, {outcome_count} checks):")
    for group in groups:
        print(
            f"  {group.test_id} (checks: {group.outcome_count}, "
            f"targets: {len(group.targets)}, cases: {group.case_count})"
        )


def _validate_report_filename(filename: str) -> str:
    filename = filename.strip()
    invalid_name = any(
        (
            not filename,
            filename in {".", ".."},
            "\0" in filename,
            Path(filename).name != filename,
            "/" in filename,
            "\\" in filename,
        )
    )
    if invalid_name:
        raise ConfigError(
            "Report names must be non-empty filenames; use --reports-dir "
            "to choose a directory"
        )
    return filename


def build_selected_report_paths(
    selected_runs: list[tuple[Path, set[str]]],
    reports_dir: Path,
    report_name_options: list[str],
) -> dict[Path, Path]:
    selected_manifests: dict[str, Path] = {}
    for yaml_file, _targets in selected_runs:
        manifest_id = get_manifest_id(yaml_file)
        previous = selected_manifests.get(manifest_id)
        if previous is not None and previous != yaml_file:
            raise ConfigError(
                f"Manifest ID collision between {display_path(previous)} and "
                f"{display_path(yaml_file)}: {manifest_id}"
            )
        selected_manifests[manifest_id] = yaml_file
    overrides: dict[str, str] = {}
    unnamed: list[str] = []

    for value in report_name_options:
        if "=" not in value:
            unnamed.append(value)
            continue
        manifest_id, filename = (part.strip() for part in value.split("=", 1))
        if not manifest_id:
            raise ConfigError("--report-name has an empty manifest ID")
        if manifest_id in overrides:
            raise ConfigError(f"Duplicate --report-name for {manifest_id}")
        overrides[manifest_id] = _validate_report_filename(filename)

    if unnamed:
        if len(unnamed) != 1 or len(selected_manifests) != 1:
            raise ConfigError(
                "An unqualified --report-name requires exactly one selected "
                "manifest; use MANIFEST=FILENAME for multiple manifests"
            )
        only_manifest = next(iter(selected_manifests))
        if only_manifest in overrides:
            raise ConfigError(f"Duplicate --report-name for {only_manifest}")
        overrides[only_manifest] = _validate_report_filename(unnamed[0])

    unknown = sorted(set(overrides) - set(selected_manifests))
    if unknown:
        raise ConfigError(
            "--report-name refers to unselected manifests: " + ", ".join(unknown)
        )

    report_paths: dict[Path, Path] = {}
    destinations: dict[Path, str] = {}
    for manifest_id, yaml_file in selected_manifests.items():
        filename = overrides.get(manifest_id)
        if filename is None:
            filename = build_report_path(
                get_group_name(yaml_file),
                yaml_file,
                reports_dir=reports_dir,
            ).name
        destination = reports_dir / filename
        if filename.casefold() in RESERVED_REPORT_FILENAMES:
            raise ConfigError(f"Report filename is reserved by the runner: {filename}")
        if (
            reports_dir == REPORTS_DIR.resolve()
            and filename.casefold() == "pylint-report.xml"
        ):
            raise ConfigError("Report filename is reserved for pylint: pylint-report.xml")
        if destination.is_symlink():
            raise ConfigError(f"Report destination must not be a symlink: {filename}")
        if destination.exists() and not destination.is_file():
            raise ConfigError(
                f"Report destination is not a regular file: {filename}"
            )
        resolved = destination.resolve()
        if resolved in destinations:
            raise ConfigError(
                "Report filename collision between manifests "
                f"{destinations[resolved]} and {manifest_id}: {filename}"
            )
        destinations[resolved] = manifest_id
        report_paths[yaml_file] = destination

    return report_paths


def run_yaml(  # pylint: disable=too-many-arguments
    yaml_file: Path,
    selected_targets: set[str],
    jobs: int | None = None,
    require_tests: bool | None = None,
    fail_on_warnings: bool | None = None,
    *,
    options: RunYamlOptions | None = None,
) -> int:
    if options is None:
        options = RunYamlOptions(
            jobs=4 if jobs is None else jobs,
            require_tests=False if require_tests is None else require_tests,
            fail_on_warnings=(
                False if fail_on_warnings is None else fail_on_warnings
            ),
        )
    elif any(
        value is not None for value in (jobs, require_tests, fail_on_warnings)
    ):
        raise TypeError("Use either legacy run_yaml options or RunYamlOptions, not both")
    reports_root = (
        REPORTS_DIR if options.reports_dir is None else Path(options.reports_dir)
    )
    reports_root.mkdir(parents=True, exist_ok=True)

    group_name = get_group_name(yaml_file)
    xml_report = options.xml_report
    if xml_report is None:
        xml_report = build_report_path(group_name, yaml_file, reports_root)

    print(f"\n[INFO] Running group : {group_name}")
    print(
        f"[INFO] YAML file     : "
        f"{yaml_file.relative_to(PROJECT_ROOT).as_posix()}"
    )
    print(
        f"[INFO] XML report    : "
        f"{display_path(xml_report)}"
    )

    try:
        config = load_yaml_config(yaml_file)
        suites = normalize_suites(config)
    except ConfigError as exc:
        outcome = build_config_error_outcome(
            yaml_file=yaml_file,
            message=format_outcome_message("Configuration error", str(exc)),
            details=traceback.format_exc(),
        )
        write_junit_xml(xml_report, group_name, yaml_file, [outcome])
        print_group_summary(group_name, [outcome], xml_report)
        return 1

    outcomes: list[TestOutcome] = []

    for suite_index, suite in enumerate(suites, start=1):
        suite_name = suite["name"]
        if (
            options.selected_group_ids is not None
            and build_test_id(yaml_file, suite_name)
            not in options.selected_group_ids
        ):
            continue
        run_case_options = RunCaseOptions(
            suite_command=suite.get("command"),
            reports_dir=reports_root,
        )
        suite_files = suite["files"]
        suite_cases = suite["cases"]

        filtered_files = []
        for file_entry in suite_files:
            normalized_file_entry = Path(file_entry).as_posix()
            if normalized_file_entry in selected_targets:
                filtered_files.append(file_entry)

        if not filtered_files:
            continue

        print(f"[INFO] Suite         : {suite_name}")

        if not suite_cases:
            outcomes.append(
                create_outcome(
                    testcase_name=f"{suite_name}::no_cases",
                    file_path="",
                    passed=False,
                    message="Suite has no cases defined",
                    meta=TestMeta(
                        suite_name=suite_name,
                        phase="suite",
                        test_type="config",
                    ),
                    details=f"suites[{suite_index}] has an empty 'cases' list.",
                    error=True,
                )
            )
            continue

        for file_entry in filtered_files:
            print(f"[INFO] Target file   : {file_entry}")

            case_jobs = list(enumerate(suite_cases, start=1))

            file_work_dir = get_file_work_dir(
                suite_name,
                file_entry,
                reports_dir=reports_root,
            )

            if file_work_dir.exists():
                shutil.rmtree(file_work_dir)
            file_work_dir.mkdir(parents=True, exist_ok=True)

            append_run_header(
                file_work_dir=file_work_dir,
                suite_name=suite_name,
                yaml_file=yaml_file,
                targets=[file_entry],
            )

            file_jobs = (
                1
                if requires_serial_case_execution(suite_cases)
                else options.jobs
            )

            if file_jobs <= 1:
                for case_index, case_def in case_jobs:
                    outcomes.append(
                        run_case(
                            suite_name=suite_name,
                            file_entry=file_entry,
                            case_index=case_index,
                            case_def=case_def,
                            options=run_case_options,
                        )
                    )
            else:
                case_results: list[tuple[int, TestOutcome]] = []
                with ThreadPoolExecutor(max_workers=file_jobs) as executor:
                    future_map = {
                        executor.submit(
                            run_case,
                            suite_name,
                            file_entry,
                            case_index,
                            case_def,
                            run_case_options,
                        ): case_index
                        for case_index, case_def in case_jobs
                    }
                    for future in as_completed(future_map):
                        case_index = future_map[future]
                        case_results.append((case_index, future.result()))

                for _case_index, outcome in sorted(
                    case_results,
                    key=lambda item: item[0],
                ):
                    outcomes.append(outcome)

    if not outcomes and options.require_tests:
        outcome = build_config_error_outcome(
            yaml_file=yaml_file,
            message="Selected YAML run produced no test outcomes",
            details=(
                "The selected targets did not execute any test cases. "
                "A required pre-check-in gate must never pass without running tests."
            ),
        )
        write_junit_xml(xml_report, group_name, yaml_file, [outcome])
        print_group_summary(group_name, [outcome], xml_report)
        return 1

    if not outcomes:
        return 0

    write_junit_xml(xml_report, group_name, yaml_file, outcomes)
    print_group_summary(group_name, outcomes, xml_report)

    if options.fail_on_warnings and any(item.warning for item in outcomes):
        return 1
    if options.fail_on_skips and any(item.skipped for item in outcomes):
        return 1

    return 0 if all(item.passed or item.skipped or item.warning for item in outcomes) else 1


def main() -> int:  # pylint: disable=too-many-return-statements
    parser = argparse.ArgumentParser(
        description="Discover and run YAML-driven repository tests."
    )
    parser.add_argument(
        "--target",
        help="Limit discovery or execution to a file or directory target",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--list-tests",
        action="store_true",
        help="List selectable test groups without running tests or changing reports",
    )
    selection.add_argument(
        "--all-tests",
        "--all",
        dest="all_tests",
        action="store_true",
        help="Run all discovered tests, optionally limited by --target",
    )
    selection.add_argument(
        "--test",
        action="append",
        default=[],
        metavar="TEST_ID",
        help="Run one test group printed by --list-tests; repeat as needed",
    )
    parser.add_argument(
        "--reports-dir",
        metavar="DIR",
        help="Write JUnit XML and case logs below DIR instead of common/reports",
    )
    parser.add_argument(
        "--report-name",
        action="append",
        default=[],
        metavar="[MANIFEST=]FILENAME",
        help=(
            "Override a JUnit filename; an unqualified name requires one "
            "selected manifest"
        ),
    )
    parser.add_argument(
        "--require-tests",
        action="store_true",
        help="Fail when no tests run or any selected YAML configuration is invalid",
    )
    parser.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help="Fail when any selected YAML test produces a warning outcome",
    )
    parser.add_argument(
        "--fail-on-skips",
        action="store_true",
        help="Fail when any selected YAML test is skipped",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="Number of YAML test cases to run in parallel (default: 4)",
    )
    args = parser.parse_args()

    if args.list_tests and (args.reports_dir or args.report_name):
        parser.error("--list-tests does not create reports; omit report options")

    jobs = max(1, args.jobs)
    try:
        reports_dir = resolve_reports_dir(args.reports_dir)
    except ConfigError as exc:
        print(f"[ERROR] Invalid report output: {exc}")
        return 1
    explicit_selection = args.all_tests or bool(args.test)
    default_reports_dir = reports_dir == REPORTS_DIR.resolve()
    yaml_files = discover_yaml_files()

    if not yaml_files:
        reason = (
            f"No YAML files found in "
            f"{TEST_YAML_DIR.relative_to(PROJECT_ROOT).as_posix()}"
        )
        print(f"[ERROR] {reason}")
        if args.list_tests:
            return 1
        if not args.test and not args.report_name:
            cleanup_default_reports(reports_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)
        create_placeholder_xml(reason, reports_dir)
        return 1 if args.require_tests or args.all_tests or args.test else 0

    selected_group_ids: set[str] | None = None
    if args.list_tests or explicit_selection:
        groups, config_warnings = discover_test_groups(yaml_files)
        scoped_groups = filter_test_groups_for_target(
            groups,
            args.target,
        )
        for warning in config_warnings:
            print(f"[ERROR] {warning}")
        if config_warnings:
            if args.all_tests and not args.report_name:
                cleanup_default_reports(reports_dir)
            return 1

        if args.list_tests:
            if not scoped_groups:
                scope = f" for target {args.target}" if args.target else ""
                print(f"[ERROR] No YAML tests found{scope}.")
                return 1
            print_test_catalog(scoped_groups)
            return 0

        if args.test:
            groups_by_id = {
                group.test_id: group
                for group in scoped_groups
            }
            requested_ids = set(args.test)
            unknown_ids = sorted(requested_ids - set(groups_by_id))
            if unknown_ids:
                print("[ERROR] Unknown test group ID(s) in the selected scope:")
                for test_id in unknown_ids:
                    print(f"  - {test_id}")
                print("[INFO] Use --list-tests with the same --target to copy an ID.")
                return 1
            selected_group_ids = requested_ids
            scoped_groups = [
                groups_by_id[test_id] for test_id in sorted(requested_ids)
            ]

        if not scoped_groups:
            scope = f" for target {args.target}" if args.target else ""
            print(f"[ERROR] No YAML tests found{scope}.")
            if args.all_tests and not args.report_name:
                cleanup_default_reports(reports_dir)
            return 1
        selected_runs = selected_runs_from_groups(scoped_groups)
    elif args.target:
        print(f"[INFO] Manual target override enabled: {args.target}")
        selected_runs, config_warnings = select_yaml_runs_for_target(
            yaml_files,
            args.target,
        )
    else:
        selected_runs, config_warnings = select_yaml_runs(yaml_files)

    for warning in config_warnings:
        print(f"[WARNING] {warning}")

    if config_warnings and args.require_tests:
        message = "Required test selection found invalid YAML configuration."
        print(f"[ERROR] {message}")
        if not args.report_name:
            cleanup_default_reports(reports_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)
        create_placeholder_xml(message, reports_dir)
        return 1

    if not selected_runs:
        if args.report_name:
            print("[ERROR] --report-name cannot be used when no tests are selected.")
            return 1
        if args.target:
            if config_warnings:
                message = (
                    f"No valid YAML test groups found for manual target: {args.target}"
                )
                print(f"[ERROR] {message}")
                cleanup_default_reports(reports_dir)
                reports_dir.mkdir(parents=True, exist_ok=True)
                create_placeholder_xml(message, reports_dir)
                return 1
            message = f"No YAML test groups found for manual target: {args.target}"
        else:
            message = "No impacted YAML test groups found for changed files."

        print(f"[INFO] {message}")
        print(f"[INFO] Reports written to: {display_path(reports_dir)}")
        cleanup_default_reports(reports_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)
        create_placeholder_xml(message, reports_dir)
        return 1 if args.require_tests else 0

    try:
        report_paths = build_selected_report_paths(
            selected_runs,
            reports_dir,
            args.report_name,
        )
    except ConfigError as exc:
        print(f"[ERROR] Invalid report output: {exc}")
        return 1

    reports_dir.mkdir(parents=True, exist_ok=True)
    if default_reports_dir and not args.test:
        cleanup_default_reports(reports_dir)
    for report_path in report_paths.values():
        report_path.unlink(missing_ok=True)
    remove_placeholder_xml(reports_dir)

    if args.test:
        count = len(selected_group_ids or ())
        noun = "group" if count == 1 else "groups"
        print(f"[INFO] Running {count} requested YAML test {noun}:")
    elif args.all_tests:
        print("[INFO] Running all discovered YAML tests:")
    elif args.target:
        print("[INFO] Running YAML test groups for manual target:")
    else:
        print("[INFO] Running only impacted YAML test groups:")

    for yaml_file, selected_targets in selected_runs:
        print(f"  - {display_path(yaml_file)}")
        for target in sorted(selected_targets):
            print(f"      * target: {target}")

    overall_exit_code = 0
    for yaml_file, selected_targets in selected_runs:
        exit_code = run_yaml(
            yaml_file,
            selected_targets=selected_targets,
            options=RunYamlOptions(
                jobs=jobs,
                require_tests=args.require_tests,
                fail_on_warnings=args.fail_on_warnings,
                fail_on_skips=args.fail_on_skips,
                selected_group_ids=(
                    None
                    if selected_group_ids is None
                    else frozenset(selected_group_ids)
                ),
                reports_dir=reports_dir,
                xml_report=report_paths[yaml_file],
            ),
        )
        if exit_code != 0:
            overall_exit_code = exit_code

    print("\n[INFO] Custom YAML test execution completed.")
    print(f"[INFO] Reports written to: {display_path(reports_dir)}")
    return overall_exit_code


if __name__ == "__main__":
    sys.exit(main())
