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
    EXACT SAME FUNCTION AS IN logs_to_xml.py
    Parses the FWTS log and returns a Python dictionary with the structure:
        {
          "test_results": [
            {
              "Test_suite": str,
              "Test_suite_Description": str,
              "subtests": [
                {
                  "sub_Test_Number": str,
                  "sub_Test_Description": str,
                  "sub_test_result": {
                      "PASSED": int,
                      "FAILED": int,
                      "ABORTED": int,
                      "SKIPPED": int,
                      "WARNINGS": int,
                      "pass_reasons": [],
                      "fail_reasons": [],
                      "abort_reasons": [],
                      "skip_reasons": [],
                      "warning_reasons": []
                  }
                },
                ...
              ],
              "test_suite_summary": {
                  "total_PASSED": int,
                  "total_FAILED": int,
                  "total_ABORTED": int,
                  "total_SKIPPED": int,
                  "total_WARNINGS": int
              }
            },
            ...
          ],
          "suite_summary": {
              "total_PASSED": int,
              "total_FAILED": int,
              "total_ABORTED": int,
              "total_SKIPPED": int,
              "total_WARNINGS": int
          }
        }
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
                Test_suite_Description = (line.split(':', 1)[1].strip()
                                          if ':' in line else "No description")
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
                reason_text = (line.split("PASSED:")[1].strip()
                               if "PASSED:" in line else "No specific reason")
                current_subtest["sub_test_result"]["pass_reasons"].append(reason_text)
            elif "FAILED" in line:
                current_subtest["sub_test_result"]["FAILED"] += 1
                reason_text = (line.split("FAILED:")[1].strip()
                               if "FAILED:" in line else "No specific reason")
                current_subtest["sub_test_result"]["fail_reasons"].append(reason_text)
            elif "SKIPPED" in line:
                current_subtest["sub_test_result"]["SKIPPED"] += 1
                reason_text = (line.split("SKIPPED:")[1].strip()
                               if "SKIPPED:" in line else "No specific reason")
                current_subtest["sub_test_result"]["skip_reasons"].append(reason_text)
            elif "WARNING" in line:
                current_subtest["sub_test_result"]["WARNINGS"] += 1
                reason_text = (line.split("WARNING:")[1].strip()
                               if "WARNING:" in line else "No specific reason")
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
                reason_text = (line.split("SKIPPED:")[1].strip()
                               if "SKIPPED:" in line else "No specific reason")
                current_subtest["sub_test_result"]["skip_reasons"].append(reason_text)
                current_test["subtests"].append(current_subtest)
                current_subtest = None
                continue

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

    return {
        "test_results": results,
        "suite_summary": suite_summary
    }

def dict_to_junit_xml(data_dict):
    """
    Convert the parsed FWTS log dictionary into JUnit-style XML.
    
    JUnit XML structure (simplified) looks like this:
    
        <testsuites>
          <testsuite name="..." tests="..." failures="..." errors="..." skipped="..." time="...">
            <testcase classname="..." name="..." time="...">
              <failure message="..." type="...">Details</failure>
              <error message="..." type="...">Details</error>
              <skipped />
              <system-out>...</system-out>
            </testcase>
            ...
          </testsuite>
        </testsuites>
    
    Important Mappings:
    - 'FAILED' → <failure>
    - 'ABORTED' → <error>
    - 'SKIPPED' → <skipped>
    - 'WARNINGS' (no direct JUnit concept) → appended in <system-out>
    - 'PASSED' → no <failure>/<error>/<skipped> tag means PASS
    """
    # Create the <testsuites> root
    root = ET.Element("testsuites")

    # You can store an overall summary as an attribute in <testsuites> if desired:
    # e.g., root.set("name", "FWTS Overall Results")
    # but it's optional.

    # Loop over each "main test" in data_dict["test_results"]
    for test in data_dict["test_results"]:
        # Each "main test" becomes one <testsuite>
        testsuite_elem = ET.SubElement(root, "testsuite")

        # Basic testsuite-level attributes
        testsuite_elem.set("name", test["Test_suite"])
        total_subtests = len(test["subtests"])
        testsuite_elem.set("tests", str(total_subtests))

        # JUnit uses "failures", "errors", and "skipped" as core attributes
        testsuite_elem.set("failures", str(test["test_suite_summary"]["total_FAILED"]))
        testsuite_elem.set("errors", str(test["test_suite_summary"]["total_ABORTED"]))
        testsuite_elem.set("skipped", str(test["test_suite_summary"]["total_SKIPPED"]))

        # (JUnit has no "warnings" attribute. We ignore it at the testsuite level.)
        # Optionally you could include a time or timestamp attribute:
        # testsuite_elem.set("time", "0.0")
        # testsuite_elem.set("timestamp", "2025-01-01T00:00:00")

        # We can also store the Test_suite_Description in a <properties> block or system-out
        # For simplicity, let's add it as <properties><property name="suite_description" value="..."/></properties>
        properties_elem = ET.SubElement(testsuite_elem, "properties")
        prop_elem = ET.SubElement(properties_elem, "property")
        prop_elem.set("name", "suite_description")
        prop_elem.set("value", test.get("Test_suite_Description", "No Description"))

        # Now handle each subtest as a <testcase>
        for sub in test["subtests"]:
            testcase_elem = ET.SubElement(testsuite_elem, "testcase")

            # testCase attributes
            testcase_elem.set("classname", test["Test_suite"])  # the suite name
            testcase_elem.set("name", sub["sub_Test_Description"])
            # testcase_elem.set("time", "0.0")  # if you have timing info, set it here

            # Decide pass/fail/error/skip:
            res = sub["sub_test_result"]

            # If there's at least one 'FAILED', we add a <failure> block
            if res["FAILED"] > 0:
                failure_elem = ET.SubElement(testcase_elem, "failure")
                failure_elem.set("message", "Test Failed")
                failure_elem.set("type", "AssertionError")
                # Combine all fail_reasons into one text block
                if res["fail_reasons"]:
                    failure_elem.text = "\n".join(res["fail_reasons"])
                else:
                    failure_elem.text = "No specific failure reason given."

            # If there's at least one 'ABORTED', we add an <error> block
            if res["ABORTED"] > 0:
                error_elem = ET.SubElement(testcase_elem, "error")
                error_elem.set("message", "Test Aborted")
                error_elem.set("type", "AbortedTest")
                if res["abort_reasons"]:
                    error_elem.text = "\n".join(res["abort_reasons"])
                else:
                    error_elem.text = "No specific abort reason given."

            # If there's at least one 'SKIPPED', we add a <skipped /> block
            if res["SKIPPED"] > 0:
                skipped_elem = ET.SubElement(testcase_elem, "skipped")
                # If you want to store skip reasons:
                # <skipped message="..." />
                combined_skip = "\n".join(res["skip_reasons"]) if res["skip_reasons"] else ""
                if combined_skip:
                    skipped_elem.set("message", combined_skip)

            # If we have warnings or pass reasons, we can stick them into system-out
            has_warnings = (res["WARNINGS"] > 0)
            has_passes = (res["PASSED"] > 0)

            # Construct a system-out section if there's something to output
            systemout_lines = []

            if has_passes:
                pass_msgs = "\n".join(res["pass_reasons"]) if res["pass_reasons"] else ""
                if pass_msgs:
                    systemout_lines.append(f"PASSED Reasons:\n{pass_msgs}")

            if has_warnings:
                warning_msgs = "\n".join(res["warning_reasons"]) if res["warning_reasons"] else ""
                if warning_msgs:
                    systemout_lines.append(f"WARNINGS:\n{warning_msgs}")

            if systemout_lines:
                system_out_elem = ET.SubElement(testcase_elem, "system-out")
                system_out_elem.text = "\n\n".join(systemout_lines)

            # If it's purely passed (no failures/aborted/skipped), then no child elements
            # means testCase is 'passed' in JUnit terms.

    # Convert the ElementTree to a string with an XML declaration
    xml_string = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_string


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 logs_to_junitxml.py <path to FWTS log> <output JUnit XML file path>")
        sys.exit(1)

    log_file_path = sys.argv[1]
    output_file_path = sys.argv[2]

    # 1) Parse the log into a dictionary (same as logs_to_xml.py)
    data_dict = parse_fwts_log(log_file_path)
    
    # 2) Convert that dictionary to JUnit XML
    junit_xml_output = dict_to_junit_xml(data_dict)

    # 3) Write JUnit XML to the specified file
    with open(output_file_path, 'wb') as outfile:
        outfile.write(junit_xml_output)

    print(f"JUnit XML report generated at: {output_file_path}")
