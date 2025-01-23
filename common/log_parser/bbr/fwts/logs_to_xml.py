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

import sys
import re
import xml.etree.ElementTree as ET

def parse_fwts_log(log_path):
    """
    Parses the FWTS log and returns a Python dictionary with the same structure
    as in the JSON version. The only difference is that we'll later convert
    this dictionary to XML instead of JSON.
    """
    with open(log_path, 'r') as f:
        log_data = f.readlines()

    results = []
    main_tests = []
    current_test = None
    current_subtest = None
    Test_suite_Description = None

    # Summary variables
    suite_summary = {
        "total_PASSED": 0,
        "total_FAILED": 0,
        "total_ABORTED": 0,
        "total_SKIPPED": 0,
        "total_WARNINGS": 0
    }

    # First, identify all main tests from the "Running tests:" lines
    running_tests_started = False
    for line in log_data:
        if "Running tests:" in line:
            running_tests_started = True
            main_tests += re.findall(r'\b(\w+)\b', line.split(':', 1)[1].strip())
        elif running_tests_started and not re.match(r'^[=\-]+$', line.strip()):  # Continuation
            main_tests += re.findall(r'\b(\w+)\b', line.strip())
        elif running_tests_started and re.match(r'^[=\-]+$', line.strip()):  # Separator line
            break

    # Process the log data
    for line in log_data:
        # Detect the start of a new main test
        for main_test in main_tests:
            if line.startswith(main_test + ":"):
                if current_test:  # Save the previous test
                    if current_subtest:
                        current_test["subtests"].append(current_subtest)
                        current_subtest = None
                    # Update the test_suite_summary based on subtests
                    for sub in current_test["subtests"]:
                        for key in ["PASSED", "FAILED", "ABORTED", "SKIPPED", "WARNINGS"]:
                            current_test["test_suite_summary"][f"total_{key}"] += sub["sub_test_result"][key]
                    results.append(current_test)

                # Start a new main test
                Test_suite_Description = line.split(':', 1)[1].strip() if ':' in line else "No description"
                current_test = {
                    "Test_suite": main_test,
                    "Test_suite_Description": Test_suite_Description,
                    "subtests": [],
                    "test_suite_summary": {
                        "total_PASSED": 0,
                        "total_FAILED": 0,
                        "total_ABORTED": 0,
                        "total_SKIPPED": 0,
                        "total_WARNINGS": 0
                    }
                }
                current_subtest = None
                break

        # Detect subtest start, number, and description
        subtest_match = re.match(r"Test (\d+) of (\d+): (.+)", line)
        if subtest_match:
            if current_subtest:  # save the previous subtest
                current_test["subtests"].append(current_subtest)

            subtest_number = f'{subtest_match.group(1)} of {subtest_match.group(2)}'
            sub_Test_Description = subtest_match.group(3).strip()

            current_subtest = {
                "sub_Test_Number": subtest_number,
                "sub_Test_Description": sub_Test_Description,
                "sub_test_result": {
                    "PASSED": 0,
                    "FAILED": 0,
                    "ABORTED": 0,
                    "SKIPPED": 0,
                    "WARNINGS": 0,
                    "pass_reasons": [],
                    "fail_reasons": [],
                    "abort_reasons": [],
                    "skip_reasons": [],
                    "warning_reasons": []
                }
            }
            continue

        # Check for test abortion
        if "Aborted" in line or "ABORTED" in line:
            if not current_subtest:
                current_subtest = {
                    "sub_Test_Number": "Test 1 of 1",
                    "sub_Test_Description": "Aborted test",
                    "sub_test_result": {
                        "PASSED": 0,
                        "FAILED": 0,
                        "ABORTED": 1,
                        "SKIPPED": 0,
                        "WARNINGS": 0,
                        "pass_reasons": [],
                        "fail_reasons": [],
                        "abort_reasons": [],
                        "skip_reasons": [],
                        "warning_reasons": []
                    }
                }
            abort_reason = line.strip()
            current_subtest["sub_test_result"]["abort_reasons"].append(abort_reason)
            continue

        # Capture pass/fail/abort/skip/warning info
        if current_subtest:
            if "PASSED" in line:
                current_subtest["sub_test_result"]["PASSED"] += 1
                reason_text = line.split("PASSED:")[1].strip() if "PASSED:" in line else "No specific reason"
                current_subtest["sub_test_result"]["pass_reasons"].append(reason_text)
            elif "FAILED" in line:
                current_subtest["sub_test_result"]["FAILED"] += 1
                reason_text = line.split("FAILED:")[1].strip() if "FAILED:" in line else "No specific reason"
                current_subtest["sub_test_result"]["fail_reasons"].append(reason_text)
            elif "SKIPPED" in line:
                current_subtest["sub_test_result"]["SKIPPED"] += 1
                reason_text = line.split("SKIPPED:")[1].strip() if "SKIPPED:" in line else "No specific reason"
                current_subtest["sub_test_result"]["skip_reasons"].append(reason_text)
            elif "WARNING" in line:
                current_subtest["sub_test_result"]["WARNINGS"] += 1
                reason_text = line.split("WARNING:")[1].strip() if "WARNING:" in line else "No specific reason"
                current_subtest["sub_test_result"]["warning_reasons"].append(reason_text)
        else:
            # Handle SKIPPED when no current_subtest exists
            if "SKIPPED" in line:
                current_subtest = {
                    "sub_Test_Number": "Test 1 of 1",
                    "sub_Test_Description": "Skipped test",
                    "sub_test_result": {
                        "PASSED": 0,
                        "FAILED": 0,
                        "ABORTED": 0,
                        "SKIPPED": 1,
                        "WARNINGS": 0,
                        "pass_reasons": [],
                        "fail_reasons": [],
                        "abort_reasons": [],
                        "skip_reasons": [],
                        "warning_reasons": []
                    }
                }
                reason_text = line.split("SKIPPED:")[1].strip() if "SKIPPED:" in line else "No specific reason"
                current_subtest["sub_test_result"]["skip_reasons"].append(reason_text)
                current_test["subtests"].append(current_subtest)
                current_subtest = None
                continue

        # We won't update per-test summary lines (passed/failed/warning/etc.)
        # because we aggregate from subtests.

    # Save the final test/subtest after processing all lines
    if current_subtest:
        current_test["subtests"].append(current_subtest)
    if current_test:
        for sub in current_test["subtests"]:
            for key in ["PASSED", "FAILED", "ABORTED", "SKIPPED", "WARNINGS"]:
                current_test["test_suite_summary"][f"total_{key}"] += sub["sub_test_result"][key]
        results.append(current_test)

    # Build overall suite_summary
    for test in results:
        for key in ["PASSED", "FAILED", "ABORTED", "SKIPPED", "WARNINGS"]:
            suite_summary[f"total_{key}"] += test["test_suite_summary"][f"total_{key}"]

    # Return the dictionary structure
    return {
        "test_results": results,
        "suite_summary": suite_summary
    }

def dict_to_xml(data_dict):
    """
    Convert the parsed FWTS log dictionary into an XML string.
    """
    root = ET.Element("fwts_result")

    # ----- test_results -----
    test_results_elem = ET.SubElement(root, "test_results")
    for test in data_dict["test_results"]:
        test_elem = ET.SubElement(test_results_elem, "test")

        # Test_suite
        suite_name_elem = ET.SubElement(test_elem, "Test_suite")
        suite_name_elem.text = str(test.get("Test_suite", ""))

        # Test_suite_Description
        suite_desc_elem = ET.SubElement(test_elem, "Test_suite_Description")
        suite_desc_elem.text = str(test.get("Test_suite_Description", ""))

        # subtests
        subtests_elem = ET.SubElement(test_elem, "subtests")
        for sub in test["subtests"]:
            subtest_elem = ET.SubElement(subtests_elem, "subtest")

            # sub_Test_Number
            subtest_number_elem = ET.SubElement(subtest_elem, "sub_Test_Number")
            subtest_number_elem.text = str(sub.get("sub_Test_Number", ""))

            # sub_Test_Description
            subtest_desc_elem = ET.SubElement(subtest_elem, "sub_Test_Description")
            subtest_desc_elem.text = str(sub.get("sub_Test_Description", ""))

            # sub_test_result
            result_elem = ET.SubElement(subtest_elem, "sub_test_result")
            res = sub["sub_test_result"]

            # Numeric counts
            for key in ["PASSED", "FAILED", "ABORTED", "SKIPPED", "WARNINGS"]:
                child = ET.SubElement(result_elem, key)
                child.text = str(res[key])

            # Reason lists
            # pass_reasons
            pass_reasons_elem = ET.SubElement(result_elem, "pass_reasons")
            for r in res["pass_reasons"]:
                reason = ET.SubElement(pass_reasons_elem, "reason")
                reason.text = r

            # fail_reasons
            fail_reasons_elem = ET.SubElement(result_elem, "fail_reasons")
            for r in res["fail_reasons"]:
                reason = ET.SubElement(fail_reasons_elem, "reason")
                reason.text = r

            # abort_reasons
            abort_reasons_elem = ET.SubElement(result_elem, "abort_reasons")
            for r in res["abort_reasons"]:
                reason = ET.SubElement(abort_reasons_elem, "reason")
                reason.text = r

            # skip_reasons
            skip_reasons_elem = ET.SubElement(result_elem, "skip_reasons")
            for r in res["skip_reasons"]:
                reason = ET.SubElement(skip_reasons_elem, "reason")
                reason.text = r

            # warning_reasons
            warning_reasons_elem = ET.SubElement(result_elem, "warning_reasons")
            for r in res["warning_reasons"]:
                reason = ET.SubElement(warning_reasons_elem, "reason")
                reason.text = r

        # test_suite_summary
        summary_elem = ET.SubElement(test_elem, "test_suite_summary")
        suite_summary_dict = test["test_suite_summary"]
        for key in ["total_PASSED", "total_FAILED", "total_ABORTED", "total_SKIPPED", "total_WARNINGS"]:
            child = ET.SubElement(summary_elem, key)
            child.text = str(suite_summary_dict[key])

    # ----- suite_summary -----
    overall_summary_elem = ET.SubElement(root, "suite_summary")
    suite_summary = data_dict["suite_summary"]
    for key in ["total_PASSED", "total_FAILED", "total_ABORTED", "total_SKIPPED", "total_WARNINGS"]:
        child = ET.SubElement(overall_summary_elem, key)
        child.text = str(suite_summary[key])

    # Convert the ElementTree to a string with an XML declaration
    xml_string = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_string

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 logs_to_xml.py <path to FWTS log> <output XML file path>")
        sys.exit(1)

    log_file_path = sys.argv[1]
    output_file_path = sys.argv[2]

    # Parse the log into a dictionary
    data_dict = parse_fwts_log(log_file_path)
    # Convert dictionary to XML string
    xml_output = dict_to_xml(data_dict)

    # Write XML output to the specified file
    with open(output_file_path, 'wb') as outfile:
        outfile.write(xml_output)

    print(f"XML report generated at: {output_file_path}")
