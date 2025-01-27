#!/usr/bin/env python3
# Copyright (c) 2024, Arm Limited or its affiliates. All rights reserved.
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
import xml.etree.ElementTree as ET
from collections import defaultdict

def detect_file_encoding(file_path):
    with open(file_path, 'rb') as file:
        raw_data = file.read()
        result = chardet.detect(raw_data)
        return result['encoding']

def parse_logs_to_dict(input_files):
    """
    IDENTICAL to your existing parser. Returns a list of dictionaries of the form:
    [
      {
        "Test_suite": str,
        "subtests": [
          {
            "sub_Test_Number": str,
            "sub_Test_Description": str,
            "sub_test_result": str,  # e.g. "PASSED", "FAILED", "ABORTED", "SKIPPED", "WARNING"
            "RULES FAILED": optional str,
            "RULES SKIPPED": optional str
          },
          ...
        ],
        "test_suite_summary": {
          "total_PASSED": 0,
          "total_FAILED": 0,
          "total_ABORTED": 0,
          "total_SKIPPED": 0,
          "total_WARNINGS": 0
        }
      },
      ...
      {
        "Suite_summary": {
          "total_PASSED": ...,
          "total_FAILED": ...,
          "total_ABORTED": ...,
          "total_SKIPPED": ...,
          "total_WARNINGS": ...
        }
      }
    ]
    """
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
        "total_PASSED": 0,
        "total_FAILED": 0,
        "total_ABORTED": 0,
        "total_SKIPPED": 0,
        "total_WARNINGS": 0
    }

    # Track test numbers per suite to avoid duplicates
    test_numbers_per_suite = defaultdict(set)

    for input_file in input_files:
        file_encoding = detect_file_encoding(input_file)
        with open(input_file, "r", encoding=file_encoding, errors="ignore") as file:
            lines = file.read().splitlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                # Remove leading timestamps in square brackets, e.g. "[2025-01-01 12:00:00]"
                line = re.sub(r'^\[.*?\]\s*', '', line)

                if "*** Starting" in line:
                    match = re.search(r'\*\*\* Starting (.*) tests \*\*\*', line)
                    if match:
                        suite_name = match.group(1).strip()
                    else:
                        # fallback if the above pattern doesn't match
                        suite_name = line.split("*** Starting")[-1].split("tests")[0].strip()
                    processing = True
                    in_test = False
                    i += 1
                    continue

                elif processing:
                    if not line.strip():
                        i += 1
                        continue

                    # Try to match "1 : SomeTest : Result: PASS"
                    result_line_match = re.match(r'^\s*(\d+)\s*:\s*(.*?)\s*: Result:\s*(\w+)$', line)
                    if result_line_match:
                        test_number = result_line_match.group(1).strip()
                        test_name = result_line_match.group(2).strip()
                        raw_result = result_line_match.group(3).strip()
                        result = result_mapping.get(raw_result, raw_result)

                        # Check for duplicates
                        if test_number in test_numbers_per_suite[suite_name]:
                            i += 1
                            continue  # skip duplicates

                        subtest_entry = {
                            "sub_Test_Number": test_number,
                            "sub_Test_Description": test_name,
                            "sub_test_result": result
                        }
                        result_data[suite_name].append(subtest_entry)
                        test_numbers_per_suite[suite_name].add(test_number)

                        # Update overall suite_summary
                        if result == "PASSED":
                            suite_summary["total_PASSED"] += 1
                        elif result == "FAILED":
                            suite_summary["total_FAILED"] += 1
                        elif result == "ABORTED":
                            suite_summary["total_ABORTED"] += 1
                        elif result == "SKIPPED":
                            suite_summary["total_SKIPPED"] += 1
                        elif result == "WARNING":
                            suite_summary["total_WARNINGS"] += 1

                        in_test = False
                        test_number, test_name, test_description, result, rules = "","","","",""
                        i += 1
                        continue

                    # Try to match just "1 : SomeTest"
                    test_line_match = re.match(r'^\s*(\d+)\s*:\s*(.*)$', line)
                    if test_line_match:
                        test_number = test_line_match.group(1).strip()
                        test_name = test_line_match.group(2).strip()
                        in_test = True
                        test_description, result, rules = "","",""
                        i += 1
                        continue

                    elif in_test:
                        # Possibly lines with ": Result:"
                        if ': Result:' in line:
                            match_res = re.search(r': Result:\s*(\w+)', line)
                            if match_res:
                                raw_result = match_res.group(1).strip()
                                result = result_mapping.get(raw_result, raw_result)
                            else:
                                result = "UNKNOWN"

                            if test_number in test_numbers_per_suite[suite_name]:
                                i += 1
                                in_test = False
                                continue

                            subtest_entry = {
                                "sub_Test_Number": test_number,
                                "sub_Test_Description": test_name,
                                "sub_test_result": result
                            }
                            if result == "FAILED" and rules:
                                subtest_entry["RULES FAILED"] = rules.strip()
                            elif result == "SKIPPED" and rules:
                                subtest_entry["RULES SKIPPED"] = rules.strip()

                            result_data[suite_name].append(subtest_entry)
                            test_numbers_per_suite[suite_name].add(test_number)

                            # Update suite summary
                            if result == "PASSED":
                                suite_summary["total_PASSED"] += 1
                            elif result == "FAILED":
                                suite_summary["total_FAILED"] += 1
                            elif result == "ABORTED":
                                suite_summary["total_ABORTED"] += 1
                            elif result == "SKIPPED":
                                suite_summary["total_SKIPPED"] += 1
                            elif result == "WARNING":
                                suite_summary["total_WARNINGS"] += 1

                            in_test = False
                            test_number, test_name, test_description, result, rules = "","","","",""
                            i += 1
                            continue
                        else:
                            # Possibly 'rules' or additional description lines
                            if re.match(r'^[A-Z0-9_ ,]+$', line.strip()) or line.strip().startswith('Appendix'):
                                rules = rules + ' ' + line.strip() if rules else line.strip()
                            else:
                                test_description = test_description + ' ' + line.strip() if test_description else line.strip()
                            i += 1
                            continue
                    else:
                        i += 1
                        continue
                else:
                    i += 1
                    continue

    # Build final output structure
    formatted_result = []
    for suite, subtests in result_data.items():
        # Summaries for each suite
        test_suite_summary = {
            "total_PASSED": 0,
            "total_FAILED": 0,
            "total_ABORTED": 0,
            "total_SKIPPED": 0,
            "total_WARNINGS": 0
        }
        for sub in subtests:
            res = sub["sub_test_result"]
            if res == "PASSED":
                test_suite_summary["total_PASSED"] += 1
            elif res == "FAILED":
                test_suite_summary["total_FAILED"] += 1
            elif res == "ABORTED":
                test_suite_summary["total_ABORTED"] += 1
            elif res == "SKIPPED":
                test_suite_summary["total_SKIPPED"] += 1
            elif res == "WARNING":
                test_suite_summary["total_WARNINGS"] += 1

        formatted_result.append({
            "Test_suite": suite,
            "subtests": subtests,
            "test_suite_summary": test_suite_summary
        })

    # Add the overall summary as a separate item
    formatted_result.append({
        "Suite_summary": suite_summary
    })

    return formatted_result

#
# NEW FUNCTION: Convert to JUnit XML
#
def dict_to_junit_xml(data_list):
    """
    Takes the list-of-dicts structure from parse_logs_to_dict and produces JUnit XML.
    Each "Test_suite" item becomes one <testsuite>; subtests become <testcase>.

    For JUnit:
      - PASSED -> <testcase> with no <failure|error|skipped> child
      - FAILED -> <failure>
      - ABORTED -> <error>
      - SKIPPED -> <skipped>
      - WARNING -> placed in <system-out>
      - "RULES FAILED"/"RULES SKIPPED" appended to <failure> or <skipped> text
    """
    # Create <testsuites> root
    root = ET.Element("testsuites")

    for item in data_list:
        # Skip the final overall summary item (because JUnit doesn't have a global summary concept)
        if "Suite_summary" in item:
            # If you want to incorporate this global summary somewhere, do so here.
            # For now, we'll just ignore it.
            continue

        # Build a <testsuite>
        suite_name = item["Test_suite"]
        testsuite_elem = ET.SubElement(root, "testsuite")
        testsuite_elem.set("name", suite_name)

        # Basic JUnit attributes:
        total_sub = len(item["subtests"])
        testsuite_elem.set("tests", str(total_sub))

        # Summaries
        summary = item["test_suite_summary"]
        testsuite_elem.set("failures", str(summary["total_FAILED"]))
        # We'll consider "ABORTED" as "errors"
        testsuite_elem.set("errors", str(summary["total_ABORTED"]))
        testsuite_elem.set("skipped", str(summary["total_SKIPPED"]))
        # Warnings have no direct attribute; we might ignore or add a custom attribute.
        # testsuite_elem.set("warnings", str(summary["total_WARNINGS"]))

        # Now each subtest -> <testcase>
        for sub in item["subtests"]:
            testcase_elem = ET.SubElement(testsuite_elem, "testcase")
            testcase_elem.set("classname", suite_name)  # or some other classification
            # We'll combine sub_Test_Number + sub_Test_Description
            test_title = f"[{sub['sub_Test_Number']}] {sub['sub_Test_Description']}"
            testcase_elem.set("name", test_title)

            # Determine outcome
            result = sub["sub_test_result"].upper()

            if result == "FAILED":
                failure_elem = ET.SubElement(testcase_elem, "failure")
                failure_elem.set("message", "Test Failed")
                failure_elem.set("type", "AssertionError")

                # If "RULES FAILED" data is present, include it in the text
                if "RULES FAILED" in sub:
                    failure_elem.text = f"RULES FAILED: {sub['RULES FAILED']}"
                else:
                    failure_elem.text = "No further details."

            elif result == "ABORTED":
                error_elem = ET.SubElement(testcase_elem, "error")
                error_elem.set("message", "Test Aborted")
                error_elem.set("type", "AbortedTest")
                # If you have more details, add them here:
                # error_elem.text = "..."

            elif result == "SKIPPED":
                skipped_elem = ET.SubElement(testcase_elem, "skipped")
                skipped_elem.set("message", "Test Skipped")
                # If "RULES SKIPPED" is present, add more details:
                if "RULES SKIPPED" in sub:
                    skipped_elem.text = f"RULES SKIPPED: {sub['RULES SKIPPED']}"

            elif result == "WARNING":
                # No <warning> in JUnit, so we put it in system-out
                system_out = ET.SubElement(testcase_elem, "system-out")
                system_out.text = f"WARNING result encountered."

            elif result == "PASSED":
                # A passing test has no child elements
                pass
            else:
                # Unknown or custom result -> store in system-out
                system_out = ET.SubElement(testcase_elem, "system-out")
                system_out.text = f"Unknown result: {sub['sub_test_result']}"

    # Convert ET to string with XML declaration
    xml_bytes = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes

def main():
    parser = argparse.ArgumentParser(description="Parse BSA/SBSA log files and output JUnit XML.")
    parser.add_argument("input_files", nargs='+', help="Input log files (BSA/SBSA logs).")
    parser.add_argument("output_file", help="Output JUnit XML file.")

    args = parser.parse_args()

    # 1) Parse logs into data structure
    data_list = parse_logs_to_dict(args.input_files)

    # 2) Convert that structure to JUnit XML
    junit_xml_output = dict_to_junit_xml(data_list)

    # 3) Write to output file
    with open(args.output_file, 'wb') as xml_file:
        xml_file.write(junit_xml_output)

    print(f"BSA/SBSA logs parsed successfully. JUnit XML saved to {args.output_file}")

if __name__ == "__main__":
    main()

