#!/usr/bin/env python3
# Copyright (c) 2024-2025, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier : Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import chardet
import json
import re
from collections import defaultdict

def detect_file_encoding(file_path):
    with open(file_path, 'rb') as file:
        raw_data = file.read()
        result = chardet.detect(raw_data)
        return result['encoding']

def main(input_files, output_file):
    processing = False
    in_test = False
    suite_name = ""
    test_number = ""
    test_name = ""
    test_description = ""
    result = ""
    rules = ""
    result_mapping = {"PASS": "PASSED", "FAIL": "FAILED", "SKIPPED": "SKIPPED"}

    result_data = defaultdict(list)
    suite_summary = {
        "Total Rules Run": 0,
        "Passed": 0,
        "Passed (Partial)": 0,
        "Warnings": 0,
        "Skipped": 0,
        "Failed": 0,
        "PAL Not Supported": 0,
        "Not Implemented": 0,
        "total_failed_with_waiver": 0
    }

    # Dictionary to keep track of test numbers per suite to avoid duplicates
    test_numbers_per_suite = defaultdict(set)

    # Minimal state for new-format START/END tracking (no hardcoded suite mapping)
    active_tests = {}

    for input_file in input_files:
        file_encoding = detect_file_encoding(input_file)

        with open(input_file, "r", encoding=file_encoding, errors="ignore") as file:
            lines = file.read().splitlines()
            i = 0
            while i < len(lines):
                line = lines[i]
                # Remove leading timestamp and square brackets
                line = line.strip()
                line = re.sub(r'^\[.*?\]\s*', '', line)

                # Allow new-format runs to trigger processing without old banners
                if not processing and (line.startswith("START ") or "---------------------- Running tests ------------------------" in line or "Selected rules:" in line):
                    processing = True

                # Handle new-format START lines (suite comes from token after START)
                start_match_new = re.match(r'^START\s+([^\s:]+)\s+([A-Z0-9_]+)\s*:\s*(.*)$', line)
                if start_match_new:
                    token_suite = (start_match_new.group(1) or "").strip()
                    current_test_id = start_match_new.group(2).strip()
                    current_desc = (start_match_new.group(3) or "").strip()
                    # If token_suite is "-", keep current suite_name; else use token_suite directly (no mapping)
                    if token_suite != "-":
                        suite_name = token_suite
                    active_tests[current_test_id] = {"suite": suite_name, "desc": current_desc}
                    i += 1
                    continue

                # Handle new-format END lines
                end_match_new = re.match(r'^END\s+([A-Z0-9_]+)\s+(.*)$', line)
                if end_match_new:
                    end_id = end_match_new.group(1).strip()
                    status_text = (end_match_new.group(2) or "").strip()
                    up = status_text.upper()

                    # Classify without coercing to SKIPPED
                    if "PASSED" in up and "PARTIAL" in up:
                        norm_res = "PASSED_PARTIAL"
                    elif "NOT TESTED" in up and "PAL NOT SUPPORTED" in up:
                        norm_res = "PAL_NOT_SUPPORTED"
                    elif "NOT TESTED" in up and "NOT IMPLEMENTED" in up:
                        norm_res = "NOT_IMPLEMENTED"
                    elif "PASSED" in up:
                        norm_res = "PASSED"
                    elif "FAILED" in up:
                        norm_res = "FAILED"
                    elif "SKIPPED" in up:
                        norm_res = "SKIPPED"
                    elif up.startswith("STATUS:"):
                        norm_res = "STATUS"
                    else:
                        norm_res = status_text if status_text else "UNKNOWN"

                    meta = active_tests.pop(end_id, {})
                    suite_for_test = meta.get("suite", suite_name)
                    desc_for_test = meta.get("desc", "")

                    # De-dup and append
                    if end_id not in test_numbers_per_suite[suite_for_test]:
                        # Keep sub_test_result human-readable:
                        # - for special classes, preserve original text when available
                        # - else use normalized simple labels
                        if norm_res in ("PAL_NOT_SUPPORTED", "NOT_IMPLEMENTED") and status_text:
                            pretty_res = status_text
                        elif norm_res == "PASSED_PARTIAL":
                            pretty_res = "PASSED(*PARTIAL)"
                        else:
                            pretty_res = "PASSED" if norm_res == "PASSED" else \
                                         "FAILED" if norm_res == "FAILED" else \
                                         "SKIPPED" if norm_res == "SKIPPED" else \
                                         "STATUS" if norm_res == "STATUS" else \
                                         (status_text if status_text else "UNKNOWN")

                        subtest_entry = {
                            "sub_Test_Number": end_id,
                            "sub_Test_Description": desc_for_test,
                            "sub_test_result": pretty_res
                        }
                        # Preserve raw status for summaries
                        if status_text:
                            subtest_entry["raw_status"] = status_text

                        result_data[suite_for_test].append(subtest_entry)
                        test_numbers_per_suite[suite_for_test].add(end_id)

                        # Update global summary buckets
                        if "FAILED" in subtest_entry["sub_test_result"] and "WAIVER" in subtest_entry["sub_test_result"]:
                            suite_summary["total_failed_with_waiver"] += 1
                        elif pretty_res == "PASSED":
                            suite_summary["Passed"] += 1
                        elif pretty_res == "FAILED":
                            suite_summary["Failed"] += 1
                        elif pretty_res == "SKIPPED":
                            suite_summary["Skipped"] += 1
                        elif pretty_res == "STATUS":
                            suite_summary["Warnings"] += 1
                        else:
                            # No direct bucket for UNKNOWN etc.
                            pass

                        # ACS-style tallies from raw text (does not affect above buckets)
                        raw_up = status_text.upper() if status_text else ""
                        if "PASSED" in raw_up and "PARTIAL" in raw_up:
                            suite_summary["Passed (Partial)"] += 1
                        if "PAL NOT SUPPORTED" in raw_up:
                            suite_summary["PAL Not Supported"] += 1
                        if "NOT IMPLEMENTED" in raw_up:
                            suite_summary["Not Implemented"] += 1

                    i += 1
                    continue

                if "*** Starting" in line:
                    suite_name_match = re.search(r'\*\*\* Starting (.*) tests \*\*\*', line)
                    if suite_name_match:
                        suite_name = suite_name_match.group(1).strip()
                    else:
                        suite_name = line.strip().split("*** Starting")[1].split("tests")[0].strip()
                    if suite_name == "GICv2m":
                         suite_name = "GIC"
                    processing = True
                    in_test = False
                    i += 1
                    continue
                elif processing:
                    if not line.strip():
                        i +=1
                        continue
                    # Try to match test line with result on same line
                    result_line_match = re.match(r'^\s*(\d+)\s*:\s*(.*?)\s*: Result:\s*(\w+)$', line)
                    if result_line_match:
                        test_number = result_line_match.group(1).strip()
                        test_name = result_line_match.group(2).strip()
                        result = result_mapping.get(result_line_match.group(3).strip(), result_line_match.group(3).strip())
                        test_description = ""
                        rules = ""
                        # Check for duplicates
                        if test_number in test_numbers_per_suite[suite_name]:
                            i +=1
                            continue  # Skip adding duplicate test
                        # Create subtest_entry
                        subtest_entry = {
                            "sub_Test_Number": test_number,
                            "sub_Test_Description": test_name,
                            "sub_test_result": result
                        }
                        # Append subtest_entry to result_data
                        result_data[suite_name].append(subtest_entry)
                        test_numbers_per_suite[suite_name].add(test_number)
                        # Update suite_summary
                        if "FAILED" in result and "WAIVER" in result:
                            suite_summary["total_failed_with_waiver"] += 1
                        elif result == "PASSED":
                            suite_summary["Passed"] += 1
                        elif result == "FAILED":
                            suite_summary["Failed"] += 1
                        elif result == "ABORTED":
                            suite_summary["Warnings"] += 1
                        elif result == "SKIPPED":
                            suite_summary["Skipped"] += 1
                        elif result == "WARNING":
                            suite_summary["Warnings"] += 1
                        # Reset variables
                        in_test = False
                        test_number = ""
                        test_name = ""
                        test_description = ""
                        result = ""
                        rules = ""
                        i +=1
                        continue
                    # Try to match test line without result
                    test_line_match = re.match(r'^\s*(\d+)\s*:\s*(.*)$', line)
                    if test_line_match:
                        test_number = test_line_match.group(1).strip()
                        test_name = test_line_match.group(2).strip()
                        in_test = True
                        test_description = ""
                        result = ""
                        rules = ""
                        i +=1
                        continue
                    elif in_test:
                        if ': Result:' in line:
                            result_match = re.search(r': Result:\s*(\w+)', line)
                            if result_match:
                                result = result_mapping.get(result_match.group(1).strip(), result_match.group(1).strip())
                            else:
                                result = "UNKNOWN"
                            # Check for duplicates
                            if test_number in test_numbers_per_suite[suite_name]:
                                i +=1
                                in_test = False  # Reset in_test flag
                                continue  # Skip adding duplicate test
                            # Create subtest_entry
                            subtest_entry = {
                                "sub_Test_Number": test_number,
                                "sub_Test_Description": test_name,
                                "sub_test_result": result
                            }
                            # Add rules if any
                            if result == "FAILED" and rules:
                                subtest_entry["RULES FAILED"] = rules.strip()
                            elif result == "SKIPPED" and rules:
                                subtest_entry["RULES SKIPPED"] = rules.strip()
                            # Append subtest_entry to result_data
                            result_data[suite_name].append(subtest_entry)
                            test_numbers_per_suite[suite_name].add(test_number)
                            # Update suite_summary
                            if "FAILED" in result and "WAIVER" in result:
                                suite_summary["total_failed_with_waiver"] += 1
                            elif result == "PASSED":
                                suite_summary["Passed"] += 1
                            elif result == "FAILED":
                                suite_summary["Failed"] += 1
                            elif result == "ABORTED":
                                suite_summary["Warnings"] += 1
                            elif result == "SKIPPED":
                                suite_summary["Skipped"] += 1
                            elif result == "WARNING":
                                suite_summary["Warnings"] += 1
                            # Reset variables
                            in_test = False
                            test_number = ""
                            test_name = ""
                            test_description = ""
                            result = ""
                            rules = ""
                            i +=1
                            continue
                        else:
                            # Check if line is rules
                            if re.match(r'^[A-Z0-9_ ,]+$', line.strip()) or line.strip().startswith('Appendix'):
                                if rules:
                                    rules += ' ' + line.strip()
                                else:
                                    rules = line.strip()
                            else:
                                # Append to test_description
                                if test_description:
                                    test_description += ' ' + line.strip()
                                else:
                                    test_description = line.strip()
                            i +=1
                            continue
                    else:
                        i +=1
                        continue
                else:
                    i +=1
                    continue

    # Prepare the final output structure
    formatted_result = {
         "test_results": [],
         "suite_summary": suite_summary
    }

    for test_suite, subtests in result_data.items():
        # Initialize test suite summary
        test_suite_summary = {
            "Total Rules Run": 0,
            "Passed": 0,
            "Passed (Partial)": 0,
            "Warnings": 0,
            "Skipped": 0,
            "Failed": 0,
            "PAL Not Supported": 0,
            "Not Implemented": 0,
            "total_failed_with_waiver": 0
        }

        # Count test results for the suite
        for subtest in subtests:
            result = subtest['sub_test_result']
            raw = subtest.get('raw_status', '')
            raw_up = raw.upper()

            if "FAILED" in result and "WAIVER" in result:
                test_suite_summary["total_failed_with_waiver"] += 1
            elif result == "PASSED":
                test_suite_summary["Passed"] += 1
            elif result == "FAILED":
                test_suite_summary["Failed"] += 1
            elif result == "ABORTED":
                test_suite_summary["Warnings"] += 1
            elif result == "SKIPPED":
                test_suite_summary["Skipped"] += 1
            elif result == "WARNING":
                test_suite_summary["Warnings"] += 1
            elif result == "STATUS":
                test_suite_summary["Warnings"] += 1

            # ACS-style derivation from raw status (does not change existing buckets)
            if "PASSED" in raw_up and "PARTIAL" in raw_up:
                test_suite_summary["Passed (Partial)"] += 1
            if "PAL NOT SUPPORTED" in raw_up:
                test_suite_summary["PAL Not Supported"] += 1
            if "NOT IMPLEMENTED" in raw_up:
                test_suite_summary["Not Implemented"] += 1

        # Compute Total Rules Run for the suite
        test_suite_summary["Total Rules Run"] = (
            test_suite_summary["Passed"]
            + test_suite_summary["Failed"]
            + test_suite_summary["Skipped"]
            + test_suite_summary["Warnings"]
            + test_suite_summary["PAL Not Supported"]
            + test_suite_summary["Not Implemented"]
            + test_suite_summary["Passed (Partial)"]
        )

        # Add the test suite and subtests to the result along with the test suite summary
        formatted_result["test_results"].append({
            "Test_suite": test_suite,
            "subtests": subtests,
            "test_suite_summary": test_suite_summary  # Nesting the summary within the test suite object
        })

    # Compute global Total Rules Run at the end
    suite_summary["Total Rules Run"] = (
        suite_summary["Passed"]
        + suite_summary["Failed"]
        + suite_summary["Skipped"]
        + suite_summary["Warnings"]
        + suite_summary["PAL Not Supported"]
        + suite_summary["Not Implemented"]
        + suite_summary["Passed (Partial)"]
    )

    # Write the result to the JSON file
    with open(output_file, 'w') as json_file:
        json.dump(formatted_result, json_file, indent=2)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parse Log files and save results to a JSON file.")
    parser.add_argument("input_files", nargs='+', help="Input Log files")
    parser.add_argument("output_file", help="Output JSON file")
    args = parser.parse_args()
    main(args.input_files, args.output_file)
