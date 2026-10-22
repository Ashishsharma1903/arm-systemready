#!/usr/bin/env python3
# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read-only CI checks for agreement between raw JSON, merged JSON and HTML.

This tool belongs to the test framework, not the shipped parser. It adds strict
artifact checks without changing the public raw/merged validation commands.
"""

import argparse
from copy import deepcopy
from collections import Counter, defaultdict
import fnmatch
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "log_parser"
sys.path.insert(0, str(SCRIPT_DIR))
from suite_registry import (
    expand_selected_suites, get_suite, load_registry, normalize_suite_name,
    suite_supports_mode,
)

RED = GREEN = NC = ""


def _native_validator():
    """Load the parser's schema helpers without modifying their public behavior."""
    path = SCRIPT_DIR / "validate.py"
    spec = importlib.util.spec_from_file_location("qa_schema_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reject_json_constant(value):
    raise ValueError(f"non-standard JSON value: {value}")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path, *, strict=False):
    options = ({"object_pairs_hook": _reject_duplicate_keys,
                "parse_constant": _reject_json_constant} if strict else {})
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, **options)


def _load_schema(path, fragment, *, strict=False):
    if strict:
        _load_json(path, strict=True)
    return _native_validator()._load_schema(path, fragment)


def _validate_one(path, suite, *, strict=False, category_provided=False):
    # Strict decoding is CI-only. Legacy validate.py remains untouched.
    if strict:
        for candidate, tag in ((path, "JSON_FILE"), (suite["schema"], "SCHEMA_FILE")):
            try:
                _load_json(candidate, strict=True)
            except (OSError, ValueError) as exc:
                return {"canonical": suite["canonical"], "fatal": (tag, str(exc))}
    result = _native_validator()._validate_one(path, suite)
    if strict and "fatal" not in result:
        validator = _schema_for_run(suite["schema"], suite.get("schema_fragment", ""),
                                    category_provided=category_provided)
        result["errors"] = list(validator.iter_errors(result["data"]))
    return result


def _schema_for_run(path, fragment="", *, selected=None, registry=None, category_provided=False):
    """Apply only explicit run context; validate every field that is present.

    Selected-suite reports need not contain compliance keys for unselected suites.
    Missing Test_suite_info is required only when this run supplied a category.
    No source schema or generated result is modified.
    """
    native = _native_validator()
    schema = deepcopy(_load_json(path, strict=True))
    if not isinstance(schema, dict):
        raise ValueError("registered schemas must be schema objects")
    if not category_provided:
        def remove_info_required(node):
            if isinstance(node, dict):
                if isinstance(node.get("required"), list):
                    node["required"] = [key for key in node["required"] if key != "Test_suite_info"]
                for value in node.values():
                    remove_info_required(value)
            elif isinstance(node, list):
                for value in node:
                    remove_info_required(value)
        remove_info_required(schema)
    if selected is not None:
        def applies(key):
            if key == "BBSR compliance results":
                return any(name.startswith("BBSR-") for name in selected)
            match = re.fullmatch(r"Suite_Name:\s*[^:]+:\s*(.+)_compliance", key)
            if not match:
                return True
            name = match.group(1)
            # These are band-specific display labels, not registry suite names.
            # They still refer to the same FWTS or SCT parser route.
            band_label = re.fullmatch(r"(?:EBBR|SBBR)-(FWTS|SCT)", name)
            if band_label:
                name = band_label.group(1)
            canonical = "OS-TESTS" if name.startswith("OS_") else normalize_suite_name(name, registry)
            return not canonical or canonical in selected
        def restrict_summary(node):
            if isinstance(node, dict):
                if isinstance(node.get("required"), list):
                    node["required"] = [key for key in node["required"] if applies(key)]
                for value in node.values():
                    restrict_summary(value)
            elif isinstance(node, list):
                for value in node:
                    restrict_summary(value)
        summary = schema.get("definitions", {}).get("acs_results_summary", {})
        restrict_summary(summary)
    if fragment:
        schema = {"$schema": schema.get("$schema"), "$ref": fragment,
                  **{key: schema[key] for key in ("definitions", "$defs") if key in schema}}
    native.Draft202012Validator.check_schema(schema)
    return native.Draft202012Validator(schema)


def _run_merged_validation(path, schema, max_paths):
    return _native_validator()._run_merged_validation(path, schema, max_paths)


def _format_path(path):
    return _native_validator()._format_path(path)


def _schema_location(registry_path, suite):
    return _native_validator()._schema_location(registry_path, suite)


def _split_selected_suites(values):
    return [name.strip() for value in values for name in value.split(",") if name.strip()]


class ArtifactValidationError(Exception):
    """Raised when generated parser artifacts disagree."""


def _normal_token(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def _canonical_status(value):
    token = _normal_token(value)
    if token == "not tested pal not supported":
        return "pal_not_supported"
    if token == "not tested test not implemented":
        return "not_implemented"
    if token.startswith("not tested"):
        return "not_tested"
    if token.startswith("known ") and token.endswith(" limitation"):
        return "ignored"
    aliases = {
        "pass": "passed",
        "passed": "passed",
        "passed partial": "passed_partial",
        "fail": "failed",
        "failed": "failed",
        "failure": "failed",
        "fail with waiver": "failed_with_waiver",
        "failed with waiver": "failed_with_waiver",
        "waived": "failed_with_waiver",
        "warning": "warnings",
        "warnings": "warnings",
        "abort": "aborted",
        "aborted": "aborted",
        "skip": "skipped",
        "skipped": "skipped",
        "ignored": "ignored",
        "known limitation": "ignored",
        "known issue limitation": "ignored",
        "test not implemented": "not_implemented",
        "not implemented": "not_implemented",
        "pal not supported": "pal_not_supported",
        "not tested": "not_tested",
        "info": "info",
        "unknown": "unknown",
        "status": "unknown",
    }
    status = aliases.get(token)
    if not status:
        raise ArtifactValidationError(f"unknown result status: {value!r}")
    return status


def _result_status(value, renderer):
    if not isinstance(value, dict):
        return _canonical_status(value)

    positive = {status for status, count in _status_counts(value).items() if count > 0}

    if not positive:
        renderer = renderer.replace("\\", "/")
        return "info" if renderer.endswith("os_tests/json_to_html.py") else "unknown"

    renderer = renderer.replace("\\", "/")
    if renderer.endswith("bbr/fwts/json_to_html.py"):
        order = (
            "failed",
            "passed",
            "failed_with_waiver",
            "aborted",
            "skipped",
            "warnings",
        )
    elif renderer.endswith("post_script/json_to_html.py"):
        order = (
            "passed",
            "failed",
            "failed_with_waiver",
            "aborted",
            "skipped",
            "warnings",
        )
    elif renderer.endswith("standalone_tests/json_to_html.py"):
        order = (
            "passed",
            "failed_with_waiver",
            "failed",
            "aborted",
            "skipped",
            "warnings",
        )
    elif renderer.endswith("os_tests/json_to_html.py"):
        order = ("passed", "failed", "skipped", "aborted", "warnings", "info")
    else:
        order = (
            "passed",
            "failed_with_waiver",
            "failed",
            "aborted",
            "skipped",
            "warnings",
            "ignored",
            "passed_partial",
            "not_implemented",
            "pal_not_supported",
            "not_tested",
        )

    for status in order:
        if status in positive:
            return status
    raise ArtifactValidationError(f"unsupported active statuses: {sorted(positive)}")


class _ReportHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.body = {}
        self.ids = Counter()
        self.links = []
        self.sbmr_channels = Counter()
        self.tables = []
        self._tables = []
        self._containers = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("data-acs-sbmr-channel"):
            self.sbmr_channels[attributes["data-acs-sbmr-channel"]] += 1
        element_id = attributes.get("id")
        if element_id:
            self.ids[element_id] += 1
        if tag == "body":
            self.body = attributes
        elif tag == "a" and attributes.get("href"):
            summary_id = ""
            in_details_link = False
            for _, ancestor in self._containers:
                classes = _classes(ancestor)
                if "summary" in classes and ancestor.get("id"):
                    summary_id = ancestor["id"]
                if "details-link" in classes:
                    in_details_link = True
            self.links.append(
                {
                    "href": attributes["href"],
                    "class": attributes.get("class", ""),
                    "summary_id": summary_id,
                    "details_link": in_details_link,
                }
            )
        elif tag == "table":
            summary_id = ""
            for _, ancestor in self._containers:
                if "summary" in _classes(ancestor) and ancestor.get("id"):
                    summary_id = ancestor["id"]
            table = {
                "attrs": attributes,
                "rows": [],
                "row": None,
                "cell": None,
                "summary_id": summary_id,
            }
            if self._tables:
                if self._tables[-1]["cell"] is not None:
                    self._tables[-1]["cell"]["contains_nested"] = True
            self.tables.append(table)
            self._tables.append(table)
        elif tag == "tr" and self._tables:
            self._tables[-1]["row"] = []
        elif tag in ("th", "td") and self._tables:
            table = self._tables[-1]
            if table["row"] is not None:
                table["cell"] = {"tag": tag, "attrs": attributes, "text": []}
        if tag in ("body", "main", "section", "article", "div", "button"):
            self._containers.append((tag, attributes))

    def handle_data(self, data):
        if (self._tables and self._tables[-1]["cell"] is not None
                and not any(tag == "button" for tag, _ in self._containers)):
            self._tables[-1]["cell"]["text"].append(data)

    def handle_endtag(self, tag):
        if self._tables:
            table = self._tables[-1]
            if tag in ("th", "td") and table["cell"] is not None:
                cell = table["cell"]
                cell["text"] = " ".join("".join(cell["text"]).split())
                table["row"].append(cell)
                table["cell"] = None
            elif tag == "tr" and table["row"] is not None:
                if table["row"]:
                    table["rows"].append(table["row"])
                table["row"] = None
            elif tag == "table":
                self._tables.pop()
        if tag in ("body", "main", "section", "article", "div", "button"):
            for index in range(len(self._containers) - 1, -1, -1):
                if self._containers[index][0] == tag:
                    del self._containers[index:]
                    break


def _read_html(path, cache=None):
    path = Path(path)
    cache = cache if cache is not None else {}
    resolved = path.resolve()
    if resolved in cache:
        return cache[resolved]
    if not path.is_file() or path.stat().st_size == 0:
        raise ArtifactValidationError(f"HTML report is missing or empty: {path}")
    parser = _ReportHTMLParser()
    try:
        parser.feed(path.read_text(encoding="utf-8"))
        parser.close()
    except Exception as exc:
        raise ArtifactValidationError(f"cannot parse HTML report {path}: {exc}") from exc
    if not parser.body:
        raise ArtifactValidationError(f"HTML report has no body element: {path}")
    duplicate_ids = sorted(name for name, count in parser.ids.items() if count > 1)
    if duplicate_ids:
        raise ArtifactValidationError(
            f"HTML report has duplicate element IDs in {path}: "
            + ", ".join(duplicate_ids)
        )
    cache[resolved] = parser
    return parser


def _classes(attributes):
    return set(attributes.get("class", "").split())


def _renderer_suite_marker(renderer):
    family = Path(renderer).parent.name
    return {
        "standalone_tests": "standalone",
        "os_tests": "os",
        "post_script": "post-script",
    }.get(family, family)


def _combined_summary_id(summary_name):
    stem = Path(summary_name).stem
    return {
        "standalone_tests_summary": "standalone_summary",
        "os_tests_summary": "OS_tests_summary",
    }.get(stem, stem)


def _summary_label(value):
    token = _normal_token(value)
    if token.startswith("total ") and (
        "test" in token or "rule" in token or "result" in token
    ):
        return "total"
    labels = {
        "passed": "passed",
        "pass": "passed",
        "passed partial": "passed_partial",
        "failed": "failed",
        "fail": "failed",
        "failed with waiver": "failed_with_waiver",
        "failed with waivers": "failed_with_waiver",
        "warnings": "warnings",
        "warning": "warnings",
        "aborted": "aborted",
        "skipped": "skipped",
        "ignored": "ignored",
        "not implemented": "not_implemented",
        "pal not supported": "pal_not_supported",
        "not tested": "not_tested",
    }
    return labels.get(token)


def _summary_entries(document, report_path):
    summaries = []
    for table in document.tables:
        if "summary-table" not in _classes(table["attrs"]):
            continue
        summary = {}
        for row in table["rows"]:
            if any(cell["tag"] == "th" for cell in row):
                continue
            cells = [cell["text"] for cell in row]
            if len(cells) < 2:
                raise ArtifactValidationError(
                    f"malformed summary row in {report_path}: {cells}"
                )
            key = _summary_label(cells[0])
            if not key:
                raise ArtifactValidationError(
                    f"unknown summary label in {report_path}: {cells[0]!r}"
                )
            value = cells[1].replace(",", "").strip()
            if not re.fullmatch(r"[0-9]+", value):
                raise ArtifactValidationError(
                    f"non-integer {cells[0]!r} count in {report_path}: {cells[1]!r}"
                )
            if key in summary:
                raise ArtifactValidationError(
                    f"duplicate {cells[0]!r} summary row in {report_path}"
                )
            summary[key] = int(value)
        if "total" not in summary:
            raise ArtifactValidationError(
                f"summary table has no recognized total row: {report_path}"
            )
        summaries.append((table.get("summary_id", ""), summary))
    return summaries


def _summary_maps(document, report_path):
    return [summary for _, summary in _summary_entries(document, report_path)]


def _summary_values(value):
    result = {}
    if not isinstance(value, dict):
        return result
    for key, count in value.items():
        if type(count) is not int or count < 0:
            raise ArtifactValidationError(f"invalid summary count for {key!r}: {count!r}")
        token = _normal_token(key)
        if token.startswith("total "):
            token = token[6:]
        mapped = {
            "rules run": "total",
            "tests": "total",
            "passed": "passed",
            "failed": "failed",
            "failed with waiver": "failed_with_waiver",
            "failed with waivers": "failed_with_waiver",
            "warnings": "warnings",
            "aborted": "aborted",
            "skipped": "skipped",
            "ignored": "ignored",
            "passed partial": "passed_partial",
            "not implemented": "not_implemented",
            "pal not supported": "pal_not_supported",
            "not tested": "not_tested",
        }.get(token)
        if mapped:
            result[mapped] = result.get(mapped, 0) + count
    return result


def _nested_summaries(value):
    summaries = []
    if isinstance(value, dict):
        if isinstance(value.get("test_suite_summary"), dict):
            summaries.append(_summary_values(value["test_suite_summary"]))
        for child in value.values():
            summaries.extend(_nested_summaries(child))
    elif isinstance(value, list):
        for child in value:
            summaries.extend(_nested_summaries(child))
    return summaries


def _status_counts(value):
    counts = defaultdict(int)
    if isinstance(value, dict):
        for key, count in value.items():
            if _normal_token(key) in {
                "pass reasons", "fail reasons", "skip reasons", "abort reasons", "warning reasons",
                "waiver reason",
            }:
                continue
            if type(count) is not int or count < 0:
                raise ArtifactValidationError(f"invalid result count for {key!r}: {count!r}")
            status = _canonical_status(key)
            counts[status] += count
    else:
        counts[_canonical_status(value)] += 1
    return dict(counts)


def _leaf_status_counts(value):
    counts = defaultdict(int)

    def visit(item):
        if isinstance(item, list):
            found = False
            for child in item:
                found = visit(child) or found
            return found
        if not isinstance(item, dict):
            return False
        result_keys = [
            key for key in item if _normal_token(key) in {
                "sub test result", "test result", "result", "status", "outcome"
            }
        ]
        found_child = False
        for key, child in item.items():
            if key not in result_keys and visit(child):
                found_child = True
        if result_keys and not found_child:
            for status, count in _status_counts(item[result_keys[0]]).items():
                counts[status] += count
            return True
        return found_child

    visit(value)
    return dict(counts)


def _raw_test_counts(test, renderer):
    renderer = renderer.replace("\\", "/")
    if renderer.endswith(("bsa/json_to_html.py", "scmi/json_to_html.py")):
        testcases = test.get("testcases", []) if isinstance(test, dict) else []
        if testcases:
            return _merge_count_maps(
                _status_counts(_result_field(testcase)) for testcase in testcases
            )
        if isinstance(test, dict) and "Test_result" in test:
            return _status_counts(test["Test_result"])
        if isinstance(test, dict) and "test_results" in test:
            return _merge_count_maps(
                _raw_test_counts(group, renderer) for group in test["test_results"]
            )
    return _leaf_status_counts(test)


def _assert_raw_internal_counts(path, data, renderer):
    def visit(item, location):
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{location}[{index}]")
        elif isinstance(item, dict):
            for key, child in item.items():
                if _normal_token(key) not in {
                    "suite summary", "test suite summary", "test case summary"
                }:
                    visit(child, f"{location}.{key}")
                    continue
                declared = _summary_values(child)
                total = declared.pop("total", None)
                actual = _raw_test_counts(item, renderer)
                keys = set(declared) | set(actual)
                if any(declared.get(status, 0) != actual.get(status, 0) for status in keys):
                    level = "suite" if _normal_token(key) == "suite summary" else "test"
                    raise ArtifactValidationError(
                        f"raw {level} summary mismatch in {path.name} {location}.{key}; "
                        f"summary={declared}, results={actual}"
                    )
                if total is not None and total != sum(actual.values()):
                    raise ArtifactValidationError(
                        f"raw total mismatch in {path.name} {location}.{key}; "
                        f"total={total}, results={sum(actual.values())}"
                    )
    visit(data, "<root>")


def _merge_count_maps(maps):
    merged = defaultdict(int)
    for values in maps:
        for key, value in values.items():
            merged[key] += value
    return dict(merged)


def _result_field(item):
    names = {
        "sub test result",
        "test result",
        "result",
        "status",
        "outcome",
    }
    for key, value in item.items():
        if _normal_token(key) in names:
            return value
    return None


def _record_field(item, candidates):
    normalized = {_normal_token(key): value for key, value in item.items()}
    for candidate in candidates:
        value = normalized.get(candidate)
        if value not in (None, ""):
            return " ".join(str(value).split())
    return ""


def _leaf_result_records(value, renderer):
    records = []
    renderer = renderer.replace("\\", "/")

    def visit(item, position=None):
        if isinstance(item, list):
            for index, child in enumerate(item, 1):
                visit(child, index)
            return
        if not isinstance(item, dict):
            return

        before = len(records)
        result_keys = {
            key for key in item if _normal_token(key) in {
                "sub test result", "test result", "result", "status", "outcome"
            }
        }
        for key, child in item.items():
            if key not in result_keys:
                visit(child)
        include_parent = renderer.endswith("bsa/json_to_html.py")
        if (len(records) != before and not include_parent) or not result_keys:
            return

        result = _result_field(item)
        identifier_fields = (
            "sub test guid",
            "sub test number",
            "test case number",
            "test number",
            "test id",
            "test case",
            "guid",
            "number",
            "id",
        )
        identifier = _record_field(item, identifier_fields)
        if renderer.endswith("bbr/tpm/json_to_html.py"):
            identifier = str(position)
        description = _record_field(
            item,
            (
                "sub test description",
                "test case description",
                "test description",
                "description",
                "name",
            ),
        )
        if not identifier:
            raise ArtifactValidationError(
                "result entry is missing a stable identifier: "
                f"{sorted(item)}"
            )
        records.append((identifier, description, _result_status(result, renderer)))

    visit(value)
    return records


def _standalone_summary(raw_data, renderer):
    counts = Counter()
    tests = raw_data.get("test_results", []) if isinstance(raw_data, dict) else []
    if not isinstance(tests, list):
        raise ArtifactValidationError("test_results must be a list, not " + type(tests).__name__)
    for test in tests:
        if not isinstance(test, dict):
            raise ArtifactValidationError("test_results entries must be objects")
        statuses = {
            status for _, _, status in _leaf_result_records(test, renderer)
        }
        if "failed" in statuses:
            counts["failed"] += 1
        elif "warnings" in statuses:
            counts["warnings"] += 1
        elif "failed_with_waiver" in statuses:
            counts["failed_with_waiver"] += 1
        else:
            counts["passed"] += 1
    counts["total"] = len(tests)
    for status in ("passed", "failed", "warnings", "failed_with_waiver"):
        counts.setdefault(status, 0)
    return dict(counts)


def _expected_summary(raw_items, renderer):
    if renderer.replace("\\", "/").endswith("standalone_tests/json_to_html.py"):
        return _merge_count_maps(
            _standalone_summary(data, renderer) for _, _, data, _ in raw_items
        )

    summaries = []
    for _, _, data, _ in raw_items:
        top_summary = data.get("suite_summary") if isinstance(data, dict) else None
        values = _summary_values(top_summary)
        if not values:
            values = _merge_count_maps(_nested_summaries(data))
        if not values and _leaf_result_records(data, renderer):
            values = dict(Counter(
                status for _, _, status in _leaf_result_records(data, renderer)
            ))
        summaries.append(values)

    expected = _merge_count_maps(summaries)
    if "total" not in expected:
        statuses = (
            "passed",
            "failed",
            "aborted",
            "skipped",
            "warnings",
            "ignored",
            "passed_partial",
            "not_implemented",
            "pal_not_supported",
            "not_tested",
        )
        expected["total"] = sum(expected.get(status, 0) for status in statuses)
        expected["total"] += expected.get("failed_with_waiver", 0)
    renderer = renderer.replace("\\", "/")
    core = {
        "total",
        "passed",
        "failed",
        "failed_with_waiver",
        "aborted",
        "skipped",
        "warnings",
    }
    if renderer.endswith("standalone_tests/json_to_html.py"):
        required = {
            "total", "passed", "failed", "warnings", "failed_with_waiver"
        }
    elif renderer.endswith("os_tests/json_to_html.py") and not any(
        path.name == "os_test.json" for _, path, _, _ in raw_items
    ):
        required = {"total", "passed", "failed", "skipped"}
    elif renderer.endswith("bsa/json_to_html.py"):
        required = core | {
            "passed_partial", "not_implemented", "pal_not_supported"
        }
    elif renderer.endswith(("bbr/sct/json_to_html.py", "bbr/tpm/json_to_html.py")):
        required = core | {"ignored"}
    else:
        required = core
    return {key: expected.get(key, 0) for key in required}


def _assert_summary(expected, actual, report_path):
    if any(expected.get(key, 0) != actual.get(key, 0) for key in set(expected) | set(actual)):
        raise ArtifactValidationError(
            f"summary count mismatch in {report_path}; "
            f"JSON={expected}, HTML={actual}"
        )


def _detail_records(document, report_path):
    records = []
    result_tables = 0
    for table in document.tables:
        if "summary-table" in _classes(table["attrs"]):
            continue
        header_index = None
        status_index = None
        id_index = None
        description_index = None
        for index, row in enumerate(table["rows"]):
            if not any(cell["tag"] == "th" for cell in row):
                continue
            raw_headers = [cell["text"] for cell in row]
            headers = [_normal_token(value) for value in raw_headers]
            status_indexes = [
                cell_index
                for cell_index, header in enumerate(headers)
                if any(word in header for word in ("result", "status", "outcome"))
                and not any(
                    word in header for word in ("reason", "summary", "description")
                )
            ]
            id_indexes = [
                cell_index
                for cell_index, header in enumerate(headers)
                if "#" in raw_headers[cell_index]
                or "number" in header
                or "guid" in header
                or header == "test case"
                or header.endswith(" id")
            ]
            description_indexes = [
                cell_index
                for cell_index, header in enumerate(headers)
                if any(word in header for word in ("description", "name"))
            ]
            if status_indexes:
                if len(id_indexes) > 1:
                    guid_indexes = [index for index in id_indexes if "guid" in headers[index]]
                    if len(guid_indexes) == 1:
                        id_indexes = guid_indexes
                if (
                    len(status_indexes) != 1
                    or len(id_indexes) != 1
                    or len(description_indexes) != 1
                ):
                    raise ArtifactValidationError(
                        f"ambiguous result table headers in {report_path}: {raw_headers}"
                    )
                status_index = status_indexes[0]
                id_index = id_indexes[0]
                description_index = description_indexes[0]
                header_index = index
                break
        if header_index is None:
            continue
        if id_index is None or description_index is None:
            raise ArtifactValidationError(
                f"result table lacks ID/description columns in {report_path}"
            )
        result_tables += 1
        for row in table["rows"][header_index + 1 :]:
            if any(cell["tag"] != "td" for cell in row):
                continue
            needed = max(status_index, id_index, description_index)
            if len(row) <= needed:
                if (len(row) == 1 and row[0].get("contains_nested")
                        and int(row[0]["attrs"].get("colspan", "1")) > needed):
                    continue
                raise ArtifactValidationError(
                    f"short result row in {report_path}: {[cell['text'] for cell in row]}"
                )
            records.append(
                (
                    row[id_index]["text"],
                    row[description_index]["text"],
                    _canonical_status(row[status_index]["text"]),
                )
            )
    return records, result_tables


def _json_counter(values):
    return Counter(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        for value in values
    )


def _raw_preserved(expected, actual):
    """Merger may add documented category metadata, never alter source results."""
    enrichment = {"Test_suite_info", "Waivable", "SRS scope", "Main Readiness Grouping"}
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        if set(actual) - set(expected) - enrichment:
            return False
        return all(key in actual and _raw_preserved(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return False
        remaining = list(actual)
        for value in expected:
            match = next((index for index, entry in enumerate(remaining) if _raw_preserved(value, entry)), None)
            if match is None:
                return False
            remaining.pop(match)
        return True
    return expected == actual


def _assert_raw_merged_parity(raw_items, merged):
    by_destination = defaultdict(list)
    for _, path, data, destination in raw_items:
        by_destination[destination].append((path, data))

    actual_sections = set(merged) - {"Suite_Name: acs_info"}
    expected_sections = set(by_destination)
    if actual_sections != expected_sections:
        raise ArtifactValidationError(
            "merged JSON section set mismatch; "
            f"expected={sorted(expected_sections)}, actual={sorted(actual_sections)}"
        )

    for destination, items in by_destination.items():
        actual = merged[destination]
        if destination == "Suite_Name: Standalone":
            expected_entries = []
            for path, data in items:
                if not isinstance(data, dict) or not isinstance(
                    data.get("test_results"), list
                ):
                    raise ArtifactValidationError(
                        f"standalone raw JSON has no test_results list: {path.name}"
                    )
                expected_entries.extend(data["test_results"])
            if not _raw_preserved(expected_entries, actual):
                raise ArtifactValidationError(
                    "merged JSON does not preserve every standalone raw result"
                )
        else:
            if len(items) != 1:
                names = ", ".join(path.name for path, _ in items)
                raise ArtifactValidationError(
                    f"multiple raw files target non-aggregate section {destination}: {names}"
                )
            path, expected = items[0]
            if not _raw_preserved(expected, actual):
                raise ArtifactValidationError(
                    f"merged section {destination} does not equal raw JSON {path.name}"
                )


def _assert_local_links(report_path, document, html_dir, cache):
    html_root = html_dir.resolve()
    for link in document.links:
        href = link["href"]
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            continue
        relative_path = unquote(parsed.path)
        target = report_path if not relative_path else report_path.parent / relative_path
        target = target.resolve()
        try:
            target.relative_to(html_root)
        except ValueError as exc:
            # SBMR links to the original Robot report beside its input XML.
            source_report = (
                document.body.get("data-acs-report-kind") == "suite"
                and document.body.get("data-acs-suite") == "sbmr"
                and "report-card-btn" in _classes(link)
            )
            if not source_report:
                raise ArtifactValidationError(
                    f"local link escapes report directory in {report_path}: {href}"
                ) from exc
        if not target.is_file() or target.stat().st_size == 0:
            raise ArtifactValidationError(
                f"local link target is missing or empty in {report_path}: {href}"
            )
        if parsed.fragment:
            target_document = _read_html(target, cache)
            if target_document.ids[unquote(parsed.fragment)] != 1:
                raise ArtifactValidationError(
                    f"local link fragment does not exist in {report_path}: {href}"
                )


def _assert_compliance_parity(merged, combined, report_path, *, selected=None):
    summary = merged["Suite_Name: acs_info"]["ACS Results Summary"]
    expected = {
        "srs requirements compliance results": summary["Overall Compliance Result"],
    }
    if "BBSR compliance results" in summary:
        expected["bbsr compliance results"] = summary["BBSR compliance results"]
    if "SCMI compliance results" in summary:
        expected["scmi compliance results"] = summary["SCMI compliance results"]
    # The current renderer emits an Unknown BBSR placeholder even when a
    # selected-only run has no BBSR aggregate. This is not a green verdict.
    # Never accept a claimed result without matching JSON evidence.
    optional_bbsr_placeholder = (selected is not None
        and not any(name.startswith("BBSR-") for name in selected)
        and "BBSR compliance results" not in summary
        and "BBSR compliance results" not in merged["Suite_Name: acs_info"]
        and not any("BBSR" in str(name).upper() for name in merged if name != "Suite_Name: acs_info"))

    def display_text(value):
        return " ".join(re.sub(r"\s*([,:;])\s*", r"\1", str(value)).split()).casefold()

    visible = defaultdict(list)
    for table in combined.tables:
        if "summary-table" in _classes(table["attrs"]):
            continue
        if not any("compliance" in _normal_token(cell["text"])
                   for row in table["rows"] for cell in row if cell["tag"] == "th"):
            continue
        active = None
        for row in table["rows"]:
            headers = [cell["text"] for cell in row if cell["tag"] == "th"]
            values = [cell["text"] for cell in row if cell["tag"] == "td"]
            if len(values) != 1 or len(headers) > 1:
                raise ArtifactValidationError(f"invalid compliance row in {report_path}: {row}")
            if headers:
                label = _normal_token(headers[0])
                if label == "bbsr compliance results" and optional_bbsr_placeholder and label not in expected:
                    expected[label] = "Unknown"
                if label in expected:
                    active = [values[0]]
                    visible[label].append(active)
                elif label in ("band", "date"):
                    active = None
                else:
                    raise ArtifactValidationError(
                        f"unexpected compliance row in {report_path}: {headers[0]}"
                    )
            elif active is not None:
                active.append(values[0])
            else:
                raise ArtifactValidationError(f"orphan compliance detail in {report_path}: {values}")

    for label, expected_value in expected.items():
        entries = visible.get(label, [])
        primary = str(expected_value).split(":", 1)[0]
        if len(entries) != 1 or display_text(entries[0][0]) != display_text(primary):
            raise ArtifactValidationError(
                f"compliance status mismatch in {report_path}: {label}; "
                f"JSON={expected_value!r}, HTML={entries}"
            )
        details = [f"{scope}: {content}" for scope, content in
                   re.findall(r"\b(Mandatory|Recommended)\s*-\s*\(([^()]*)\)", str(expected_value))]
        # The SCMI aggregate names its failed suite without the usual 'failed:' prefix.
        if label == "scmi compliance results" and details == ["Mandatory: SCMI"]:
            details = ["Mandatory: failed: SCMI"]
        if Counter(map(display_text, entries[0][1:])) != Counter(map(display_text, details)):
            raise ArtifactValidationError(
                f"compliance detail mismatch in {report_path}: {label}; "
                f"JSON={details!r}, HTML={entries[0][1:]!r}"
            )


def _auxiliary_json_names(registry_path):
    document = _load_json(registry_path, strict=True)
    names = set()
    executions = document.get("standalone", {}).get("suite_execution", {})
    for execution in executions.values():
        for input_spec in execution.get("inputs", []):
            if input_spec.get("supporting") and input_spec.get("output"):
                names.add(input_spec["output"])
    return names


def _strict_suite_info_for_file(path, registry, registry_path):
    basename = Path(path).name
    exact_matches = []
    pattern_matches = []
    for suite in registry:
        if not suite.get("schema"):
            continue
        schema_path, schema_fragment, schema_ref = _schema_location(
            registry_path, suite
        )
        suite_info = {
            "canonical": suite["canonical"],
            "schema": schema_path,
            "schema_fragment": schema_fragment,
            "schema_ref": schema_ref,
        }
        if suite.get("json_output") == basename:
            exact_matches.append(suite_info)
        elif any(
            fnmatch.fnmatch(basename, pattern)
            for pattern in suite.get("json_output_patterns", [])
        ):
            pattern_matches.append(suite_info)

    matches = exact_matches or pattern_matches
    if len(matches) > 1:
        names = ", ".join(sorted(item["canonical"] for item in matches))
        raise ArtifactValidationError(
            f"generated JSON {basename} ambiguously matches suites: {names}"
        )
    return matches[0] if matches else None


def _assert_json_subset(expected, actual, path="<root>"):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise ArtifactValidationError(
                f"merged acs_info changed the type at {path}"
            )
        for key, value in expected.items():
            if key not in actual:
                raise ArtifactValidationError(
                    f"merged acs_info dropped {path}.{key}"
                )
            _assert_json_subset(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        if expected != actual:
            raise ArtifactValidationError(
                f"merged acs_info changed list data at {path}"
            )
    elif expected != actual:
        raise ArtifactValidationError(
            f"merged acs_info changed {path}: {expected!r} != {actual!r}"
        )


def _parse_expected_raw(specifications):
    expected = {}
    for specification in specifications:
        if "=" not in specification:
            raise ArtifactValidationError(
                "--expect-raw must use BASENAME=MERGED_SECTION format"
            )
        basename, destination = (
            value.strip() for value in specification.split("=", 1)
        )
        if Path(basename).name != basename or not basename.endswith(".json"):
            raise ArtifactValidationError(
                "--expect-raw basenames must be JSON filenames, not paths"
            )
        if not destination.startswith("Suite_Name: "):
            raise ArtifactValidationError(
                f"invalid merged section for {basename}: {destination!r}"
            )
        if basename in expected:
            raise ArtifactValidationError(
                f"duplicate --expect-raw output: {basename}"
            )
        expected[basename] = destination
    return expected


def _run_artifact_validation(args):
    output_dir = Path(args.output_dir)
    json_dir = output_dir / "acs_jsons"
    html_dir = output_dir / "html_detailed_summaries"
    registry_path = Path(args.registry)
    schema_path = Path(args.schema)
    selected_names = _split_selected_suites(args.selected_suites)
    errors = []

    try:
        registry = load_registry(str(registry_path))
        unknown = [name for name in selected_names if not normalize_suite_name(name, registry)]
        if unknown:
            raise ArtifactValidationError(
                "unknown selected suite names: " + ", ".join(sorted(unknown))
            )
        selected = expand_selected_suites(selected_names, registry)
        if not selected:
            raise ArtifactValidationError("at least one selected suite is required")
        selected_set = set(selected)
        selected_suites = [get_suite(name, registry) for name in selected]
        selected_suites = [suite for suite in selected_suites if suite]
        if not any(suite.get("schema") for suite in selected_suites):
            raise ArtifactValidationError("selected suites have no registered JSON outputs")
        unsupported = [
            suite["canonical"]
            for suite in selected_suites
            if not suite_supports_mode(suite["canonical"], args.mode, registry)
        ]
        if unsupported:
            raise ArtifactValidationError(
                f"suites do not support {args.mode} mode: " + ", ".join(unsupported)
            )
        if not json_dir.is_dir() or not html_dir.is_dir():
            raise ArtifactValidationError(
                f"missing generated artifact directories below {output_dir}"
            )
        for required_path in (
            json_dir / "acs_info.json",
            json_dir / "merged_results.json",
            html_dir / "acs_summary.html",
        ):
            if not required_path.is_file() or required_path.stat().st_size == 0:
                raise ArtifactValidationError(
                    f"required generated artifact is missing or empty: {required_path}"
                )

        expected_raw = _parse_expected_raw(args.expect_raw)
        if not expected_raw:
            raise ArtifactValidationError("at least one --expect-raw output is required")
        allowed_metadata = {"acs_info.json", "merged_results.json"}
        allowed_auxiliary = _auxiliary_json_names(registry_path)
        expected_auxiliary = set(args.expect_aux)
        invalid_auxiliary = expected_auxiliary - allowed_auxiliary
        if invalid_auxiliary:
            raise ArtifactValidationError(
                "unregistered --expect-aux outputs: "
                + ", ".join(sorted(invalid_auxiliary))
            )
        actual_auxiliary = set()
        actual_raw = []
        for path in sorted(json_dir.glob("*.json")):
            if path.name in allowed_metadata:
                _load_json(path, strict=True)
                continue
            if path.name in allowed_auxiliary:
                _load_json(path, strict=True)
                actual_auxiliary.add(path.name)
                continue
            suite_info = _strict_suite_info_for_file(path, registry, registry_path)
            if not isinstance(suite_info, dict):
                raise ArtifactValidationError(
                    f"unregistered generated JSON output: {path.name}"
                )
            if suite_info.get("canonical") not in selected_set:
                raise ArtifactValidationError(
                    f"stale or unselected generated JSON output: {path.name}"
                )
            actual_raw.append(path)

        if actual_auxiliary != expected_auxiliary:
            raise ArtifactValidationError(
                "auxiliary JSON output set mismatch; "
                f"expected={sorted(expected_auxiliary)}, "
                f"actual={sorted(actual_auxiliary)}"
            )

        actual_names = {path.name for path in actual_raw}
        expected_names = set(expected_raw)
        missing_raw = sorted(expected_names - actual_names)
        unexpected_raw = sorted(actual_names - expected_names)
        if missing_raw:
            raise ArtifactValidationError(
                "expected raw JSON outputs are missing: " + ", ".join(missing_raw)
            )
        if unexpected_raw:
            raise ArtifactValidationError(
                "unexpected raw JSON outputs were produced: "
                + ", ".join(unexpected_raw)
            )

        raw_items = []
        for json_file in actual_raw:
            suite_info = _strict_suite_info_for_file(
                json_file, registry, registry_path
            )
            if not suite_info:
                raise ArtifactValidationError(
                    f"no registered schema for raw JSON: {json_file.name}"
                )
            result = _validate_one(json_file, suite_info, strict=True,
                                   category_provided=getattr(args, "test_category_provided", False))
            if "fatal" in result:
                tag, message = result["fatal"]
                raise ArtifactValidationError(
                    f"raw JSON {json_file.name} failed {tag}: {message}"
                )
            if result["errors"]:
                first = result["errors"][0]
                errors.append(
                    f"raw JSON {json_file.name} violates schema at "
                    f"{_format_path(first.absolute_path)}: {first.message}"
                )
            suite = get_suite(result["canonical"], registry)
            _assert_raw_internal_counts(
                json_file, result["data"], suite.get("json_to_html", "")
            )
            raw_items.append(
                (
                    suite,
                    json_file,
                    result["data"],
                    expected_raw[json_file.name],
                )
            )

        merged_path = json_dir / "merged_results.json"
        merged = _load_json(merged_path, strict=True)
        merged_validator = _schema_for_run(schema_path, selected=selected_set, registry=registry,
                                          category_provided=getattr(args, "test_category_provided", False))
        merged_errors = sorted(
            merged_validator.iter_errors(merged),
            key=lambda item: list(item.absolute_path),
        )
        if merged_errors:
            first = merged_errors[0]
            errors.append(
                "merged JSON violates the complete schema at "
                f"{_format_path(first.absolute_path)}: {first.message}"
            )
        _assert_raw_merged_parity(raw_items, merged)

        acs_info_path = json_dir / "acs_info.json"
        acs_info = _load_json(acs_info_path, strict=True)
        merged_acs_info = merged.get("Suite_Name: acs_info")
        if not isinstance(merged_acs_info, dict):
            raise ArtifactValidationError("merged JSON has no acs_info section")
        _assert_json_subset(acs_info, merged_acs_info)
        expected_band = {
            "DT": "SystemReady Devicetree band",
            "SR": "SystemReady band",
        }[args.mode]
        summary_band = merged_acs_info.get("ACS Results Summary", {}).get("Band")
        system_band = merged_acs_info.get("System Info", {}).get("Band")
        if summary_band != expected_band or system_band != expected_band:
            raise ArtifactValidationError(
                "merged Band values do not match the parser mode; "
                f"System Info={system_band!r}, ACS Results Summary={summary_band!r}, "
                f"{args.mode} requires {expected_band!r}"
            )

        grouped = defaultdict(list)
        for suite, path, data, destination in raw_items:
            detail_name = suite.get("detailed_html")
            summary_name = suite.get("summary_html")
            renderer = suite.get("json_to_html")
            if not detail_name or not summary_name or not renderer:
                raise ArtifactValidationError(
                    f"suite {suite['canonical']} has incomplete HTML registration"
                )
            grouped[(detail_name, summary_name, renderer)].append(
                (suite, path, data, destination)
            )

        sbmr_items = [item for item in raw_items if item[0]["canonical"] in {"SBMR-IB", "SBMR-OOB"}]
        if sbmr_items:
            grouped[("sbmr_detailed.html", "sbmr_summary.html", "sbmr/json_to_html.py")] = sbmr_items

        expected_html = {"acs_summary.html"}
        for detail_name, summary_name, _ in grouped:
            expected_html.update((detail_name, summary_name))
        actual_html = {path.name for path in html_dir.glob("*.html")}
        missing_html = sorted(expected_html - actual_html)
        extra_html = sorted(actual_html - expected_html)
        if missing_html:
            raise ArtifactValidationError(
                "registered HTML outputs are missing: " + ", ".join(missing_html)
            )
        if extra_html:
            raise ArtifactValidationError(
                "stale or unregistered HTML outputs were produced: "
                + ", ".join(extra_html)
            )

        cache = {}
        combined_path = html_dir / "acs_summary.html"
        combined = _read_html(combined_path, cache)
        if combined.body.get("data-acs-report-kind") != "acs-summary":
            raise ArtifactValidationError(
                "acs_summary.html does not declare data-acs-report-kind=acs-summary"
            )
        ui_marker = combined.body.get("data-acs-report-ui")
        if "acs-report-ui" not in _classes(combined.body) or not ui_marker:
            raise ArtifactValidationError(
                "acs_summary.html is missing the shared report UI marker"
            )
        _assert_compliance_parity(merged, combined, combined_path, selected=selected_set)
        combined_entries = _summary_entries(combined, combined_path)
        combined_summaries = [summary for _, summary in combined_entries]
        summary_ids = [owner_id for owner_id, _ in combined_entries]
        if any(not owner_id for owner_id in summary_ids):
            raise ArtifactValidationError(
                "a consolidated summary table is outside a named summary card"
            )
        for summary_id in summary_ids:
            navigation_links = [
                link
                for link in combined.links
                if not link["details_link"] and link["href"] == f"#{summary_id}"
            ]
            if len(navigation_links) != 1:
                raise ArtifactValidationError(
                    f"acs_summary.html must navigate exactly once to #{summary_id}"
                )
        expected_combined = Counter()
        total_result_rows = 0

        for (detail_name, summary_name, renderer), group_items in grouped.items():
            detail_path = html_dir / detail_name
            summary_path = html_dir / summary_name
            detail = _read_html(detail_path, cache)
            summary = _read_html(summary_path, cache)
            expected_suite_marker = _renderer_suite_marker(renderer)
            for path, document in ((detail_path, detail), (summary_path, summary)):
                if document.body.get("data-acs-report-kind") != "suite":
                    raise ArtifactValidationError(
                        f"suite report has wrong data-acs-report-kind: {path}"
                    )
                if document.body.get("data-acs-suite") != expected_suite_marker:
                    raise ArtifactValidationError(
                        f"suite report has wrong data-acs-suite identity: {path}; "
                        f"expected {expected_suite_marker!r}"
                    )
                if (
                    "acs-report-ui" not in _classes(document.body)
                    or document.body.get("data-acs-report-ui") != ui_marker
                ):
                    raise ArtifactValidationError(
                        f"suite report UI marker disagrees with acs_summary.html: {path}"
                    )
            if detail.body.get("data-acs-main-page") != "acs_summary.html":
                raise ArtifactValidationError(
                    f"detailed report does not link back to acs_summary.html: {detail_path}"
                )

            detail_summaries = _summary_maps(detail, detail_path)
            summary_summaries = _summary_maps(summary, summary_path)
            if len(detail_summaries) != 1 or len(summary_summaries) != 1:
                raise ArtifactValidationError(
                    f"expected exactly one summary table in {detail_name} and {summary_name}"
                )
            if detail_summaries[0] != summary_summaries[0]:
                raise ArtifactValidationError(
                    f"detailed and summary HTML counts disagree for {detail_name}"
                )
            expected_summary = _expected_summary(group_items, renderer)
            _assert_summary(expected_summary, detail_summaries[0], detail_path)
            individual_sbmr = renderer == "sbmr/json_to_html.py" and detail_name != "sbmr_detailed.html"
            if not individual_sbmr:
                expected_combined[
                    json.dumps(detail_summaries[0], sort_keys=True, separators=(",", ":"))
                ] += 1
            if detail_name == "sbmr_detailed.html":
                channels = Counter({suite["canonical"]: len(data.get("test_results", []))
                                    for suite, _, data, _ in group_items})
                if detail.sbmr_channels != channels:
                    raise ArtifactValidationError(f"combined SBMR interface identity mismatch: expected={channels}, actual={detail.sbmr_channels}")

            expected_records = []
            for _, _, data, _ in group_items:
                expected_records.extend(_leaf_result_records(data, renderer))
            actual_records, result_tables = _detail_records(detail, detail_path)
            if expected_records and result_tables == 0:
                raise ArtifactValidationError(
                    f"detailed report has no result table: {detail_path}"
                )
            if Counter(expected_records) != Counter(actual_records):
                missing_rows = Counter(expected_records) - Counter(actual_records)
                extra_rows = Counter(actual_records) - Counter(expected_records)
                raise ArtifactValidationError(
                    f"detailed HTML result rows disagree with raw JSON in {detail_name}; "
                    f"missing={dict(missing_rows)}, extra={dict(extra_rows)}"
                )
            total_result_rows += len(expected_records)

            if individual_sbmr:
                continue

            detail_links = [
                link
                for link in combined.links
                if link["details_link"]
                and not urlsplit(link["href"]).scheme
                and Path(unquote(urlsplit(link["href"]).path)).name == detail_name
            ]
            if len(detail_links) != 1 or detail_links[0]["href"] != detail_name:
                raise ArtifactValidationError(
                    f"acs_summary.html must link exactly once to {detail_name}; "
                    f"found {detail_links}"
                )
            summary_id = detail_links[0]["summary_id"]
            expected_summary_id = _combined_summary_id(summary_name)
            if summary_id != expected_summary_id:
                raise ArtifactValidationError(
                    f"acs_summary.html links {detail_name} from the wrong "
                    f"summary card; expected #{expected_summary_id}, "
                    f"found #{summary_id or '<none>'}"
                )
            linked_summaries = [
                values for owner_id, values in combined_entries if owner_id == summary_id
            ]
            if not summary_id or linked_summaries != [detail_summaries[0]]:
                raise ArtifactValidationError(
                    f"acs_summary.html links {detail_name} from the wrong summary card"
                )

        actual_combined = Counter(
            json.dumps(summary, sort_keys=True, separators=(",", ":"))
            for summary in combined_summaries
        )
        if actual_combined != expected_combined:
            raise ArtifactValidationError(
                "consolidated HTML summary counts do not match suite summaries"
            )

        for report_path in sorted(html_dir.glob("*.html")):
            _assert_local_links(
                report_path,
                _read_html(report_path, cache),
                html_dir,
                cache,
            )

    except (ArtifactValidationError, OSError, ValueError, TypeError, KeyError) as exc:
        errors.append(str(exc))

    if errors:
        for error in errors:
            print(f"{RED}ERROR:{NC} {error}")
        print(f"Artifact consistency result: {RED}FAIL{NC}")
        return 1

    print(
        f"Artifact consistency result: {GREEN}PASS{NC} "
        f"({len(raw_items)} raw JSON, {len(grouped)} detailed HTML, "
        f"{len(grouped)} summary HTML, {total_result_rows} result rows)"
    )
    return 0



def main(argv=None):
    """Validate a generated report package using explicit fixture expectations."""
    global SCRIPT_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("artifacts", help="Check a complete generated report")
    check.add_argument("output_dir")
    check.add_argument("--parser-dir", type=Path, default=SCRIPT_DIR)
    check.add_argument("--mode", choices=("DT", "SR"), required=True)
    check.add_argument("--selected-suites", action="append", required=True)
    check.add_argument("--expect-raw", action="append", required=True)
    check.add_argument("--expect-aux", action="append", default=[])
    check.add_argument("--registry")
    check.add_argument("--schema")
    check.add_argument("--test-category-provided", action="store_true",
                       help="The generating run explicitly supplied a category file; require its suite information")
    args = parser.parse_args(argv)
    SCRIPT_DIR = args.parser_dir.resolve()
    args.registry = args.registry or str(SCRIPT_DIR / "suite_registry.json")
    args.schema = args.schema or str(SCRIPT_DIR / "acs-results-schema.json")
    return _run_artifact_validation(args)


if __name__ == "__main__":
    sys.exit(main())
