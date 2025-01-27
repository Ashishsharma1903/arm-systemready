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

#!/usr/bin/env python3
import os
import re
import xml.etree.ElementTree as ET

def parse_capsule_update_log(lines):
    """
    EXACTLY THE SAME FUNCTION (unchanged)
    Parse 'capsule-update.log' lines and return list of dicts.
    """
    tests = []
    i = 0
    total_lines = len(lines)
    while i < total_lines:
        line = lines[i].strip()
        match = re.match(r"Testing\s+(unauth\.bin|tampered\.bin)\s+update", line, re.IGNORECASE)
        if match:
            test_description = line
            test_info = ''
            test_result = 'FAILED'  # Default
            i += 1

            # Look for 'Test_Info'
            while i < total_lines:
                current_line = lines[i].strip()
                if re.match(r"Testing\s+", current_line, re.IGNORECASE):
                    # Another test begins
                    break
                elif re.match(r"Test[_\s]Info", current_line, re.IGNORECASE):
                    # Start collecting lines
                    i += 1
                    info_lines = []
                    while i < total_lines:
                        info_line = lines[i].strip()
                        if re.match(r"Testing\s+", info_line, re.IGNORECASE):
                            i -= 1
                            break
                        info_lines.append(info_line)
                        i += 1
                    test_info = '\n'.join(info_lines)

                    # Decide final result
                    if "failed to update capsule" in test_info.lower():
                        test_result = 'PASSED'
                    elif "not present" in test_info.lower():
                        test_result = 'FAILED'
                    elif "succeed to write" in test_info.lower():
                        test_result = 'PASSED'
                    else:
                        test_result = 'FAILED'
                    break
                else:
                    i += 1

            tests.append({
                'Test_Description': test_description,
                'Test_Info': test_info,
                'Test_Result': test_result
            })
        else:
            i += 1
    return tests

def parse_capsule_on_disk_log(lines):
    """
    EXACTLY THE SAME FUNCTION (unchanged)
    Parse 'capsule-on-disk.log' lines for "Testing signed_capsule.bin OD update".
    """
    tests = []
    i = 0
    total_lines = len(lines)
    while i < total_lines:
        line = lines[i].strip()
        match = re.match(r"Testing\s+signed_capsule\.bin\s+OD\s+update", line, re.IGNORECASE)
        if match:
            test_description = line
            test_info = ''
            test_result = 'FAILED'
            i += 1

            while i < total_lines:
                current_line = lines[i].strip()
                if re.match(r"Testing\s+", current_line, re.IGNORECASE):
                    break
                elif re.match(r"Test[_\s]Info", current_line, re.IGNORECASE):
                    i += 1
                    info_lines = []
                    while i < total_lines:
                        info_line = lines[i].strip()
                        if re.match(r"Testing\s+", info_line, re.IGNORECASE):
                            i -= 1
                            break
                        info_lines.append(info_line)
                        i += 1
                    test_info = '\n'.join(info_lines)

                    # Decide result
                    if "signed_capsule.bin not present" in test_info.lower():
                        test_result = 'FAILED'
                    elif "succeed to write signed_capsule.bin" in test_info.lower():
                        if "uefi capsule update has failed" in test_info.lower():
                            test_result = 'FAILED'
                        else:
                            test_result = 'PASSED'
                    else:
                        test_result = 'FAILED'
                    break
                else:
                    i += 1
            tests.append({
                'Test_Description': test_description,
                'Test_Info': test_info,
                'Test_Result': test_result
            })
        else:
            i += 1
    return tests

def parse_capsule_test_results_log(lines):
    """
    EXACTLY THE SAME FUNCTION (unchanged)
    Parse 'capsule_test_results.log' lines for:
    - 'Testing signed_capsule.bin sanity'
    - 'Testing ESRT FW version update'
    """
    tests = []
    i = 0
    total_lines = len(lines)
    while i < total_lines:
        line = lines[i].strip()
        sanity_match = re.match(r"Testing\s+signed_capsule\.bin\s+sanity", line, re.IGNORECASE)
        esrt_match = re.match(r"(Testing|Test:\s+Testing)\s+ESRT\s+FW\s+version\s+update", line, re.IGNORECASE)

        if sanity_match:
            test_description = line
            test_info = ''
            test_result = 'PASSED'
            i += 1
            while i < total_lines:
                current_line = lines[i].strip()
                if re.match(r"Testing\s+", current_line, re.IGNORECASE) or re.match(r"Test:\s+", current_line, re.IGNORECASE):
                    break
                elif "error sanity_check_capsule" in current_line.lower():
                    test_info = current_line
                    test_result = 'FAILED'
                    break
                elif "warning" in current_line.lower():
                    test_info = current_line
                    test_result = 'PASSED'
                    break
                else:
                    i += 1
            tests.append({
                'Test_Description': test_description,
                'Test_Info': test_info,
                'Test_Result': test_result
            })
        elif esrt_match:
            test_description = "Testing ESRT FW version update"
            test_info = ''
            test_result = 'FAILED'
            i += 1
            while i < total_lines:
                current_line = lines[i].strip()
                if re.match(r"Testing\s+", current_line, re.IGNORECASE) or re.match(r"Test:\s+", current_line, re.IGNORECASE):
                    break
                elif current_line.lower().startswith("info:"):
                    test_info = current_line[len("info:"):].strip()
                    i += 1
                elif current_line.lower().startswith("results:"):
                    result_line = current_line[len("results:"):].strip()
                    if result_line.upper() == "PASSED":
                        test_result = 'PASSED'
                    else:
                        test_result = 'FAILED'
                    break
                else:
                    i += 1
            tests.append({
                'Test_Description': test_description,
                'Test_Info': test_info,
                'Test_Result': test_result
            })
        else:
            i += 1
    return tests

def read_log_file(path, encoding='utf-8'):
    """
    EXACTLY THE SAME FUNCTION (unchanged)
    Utility to read a file with a given encoding, ignoring errors.
    """
    try:
        with open(path, 'r', encoding=encoding, errors='ignore') as file:
            return file.readlines()
    except Exception as e:
        print(f"Error reading {path} with encoding {encoding}: {e}")
        return []

def build_junit_xml(tests, summary):
    """
    NEW FUNCTION:
    Build a JUnit XML document from the parsed test list + summary.
    We treat everything as one <testsuite> named "Capsule Update Tests."
    
    Mapping:
      - PASSED -> no <failure|error|skipped> child => considered passed
      - FAILED -> <failure>
      - SKIPPED -> <skipped>
      - Test_Info -> stored in <system-out>
    """

    # We'll create a single <testsuites> root with one <testsuite>
    root = ET.Element("testsuites")
    testsuite_elem = ET.SubElement(root, "testsuite")

    # Name the test suite and set basic JUnit attributes
    testsuite_elem.set("name", "Capsule Update Tests")
    total_tests = len(tests)
    testsuite_elem.set("tests", str(total_tests))

    # We treat "failed" as <failure>, "skipped" as <skipped>, "errors" as 0 by default
    testsuite_elem.set("failures", str(summary["total_FAILED"]))
    testsuite_elem.set("errors", "0")  # no concept of 'error' in current script
    testsuite_elem.set("skipped", str(summary["total_SKIPPED"]))
    # We do not have a direct measure of time or warnings, so ignore them.

    for test_obj in tests:
        # For each test, create a <testcase>
        testcase_elem = ET.SubElement(testsuite_elem, "testcase")
        description = test_obj.get("Test_Description", "No Description")
        info = test_obj.get("Test_Info", "")
        result = test_obj.get("Test_Result", "").upper()

        # "classname" can be set to the same as suite name or something else
        testcase_elem.set("classname", "CapsuleUpdate")
        # "name" could be the test description
        testcase_elem.set("name", description)

        if result == "FAILED":
            failure_elem = ET.SubElement(testcase_elem, "failure")
            failure_elem.set("message", "Test Failed")
            failure_elem.set("type", "AssertionError")
            # If you want more detail, you can embed test_info text:
            if info:
                failure_elem.text = f"Details:\n{info}"
            else:
                failure_elem.text = "No additional info provided."
        elif result == "SKIPPED":
            skipped_elem = ET.SubElement(testcase_elem, "skipped")
            skipped_elem.set("message", "Test Skipped")
            if info:
                skipped_elem.text = info
        elif result == "PASSED":
            # Passing => no child elements => JUnit sees it as pass
            pass
        else:
            # Unknown or custom result => store in system-out
            system_out_elem = ET.SubElement(testcase_elem, "system-out")
            system_out_elem.text = f"Unknown result type: {result}\nInfo:\n{info}"

        # If there's extra test info for any case, we can store it in <system-out>
        # (But this is optional. Up to you.)
        # system_out_elem = ET.SubElement(testcase_elem, "system-out")
        # system_out_elem.text = info

    xml_bytes = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes

def main():
    # EXACT SAME SETUP, just replace the part where we build/write XML.

    # Paths
    capsule_update_log_path = '/mnt/acs_results_template/fw/capsule-update.log'
    capsule_on_disk_log_path = '/mnt/acs_results_template/fw/capsule-on-disk.log'
    capsule_test_results_log_path = '/mnt/acs_results/app_output/capsule_test_results.log'

    # Output path for the JUnit XML now (instead of custom XML)
    output_file = '/mnt/acs_results/acs_summary/acs_xmls/capsule_update_junit.xml'

    tests = []

    # 1) capsule-update.log (UTF-16)
    if os.path.exists(capsule_update_log_path):
        lines = read_log_file(capsule_update_log_path, encoding='utf-16')
        if lines:
            parsed_update_tests = parse_capsule_update_log(lines)
            tests.extend(parsed_update_tests)
            print(f"Parsed {len(parsed_update_tests)} tests from {capsule_update_log_path}")
        else:
            print(f"No content found in {capsule_update_log_path}")
    else:
        print(f"Error: {capsule_update_log_path} not found.")

    # 2) capsule-on-disk.log (UTF-8)
    if os.path.exists(capsule_on_disk_log_path):
        lines = read_log_file(capsule_on_disk_log_path, encoding='utf-8')
        if lines:
            parsed_on_disk_tests = parse_capsule_on_disk_log(lines)
            tests.extend(parsed_on_disk_tests)
            print(f"Parsed {len(parsed_on_disk_tests)} tests from {capsule_on_disk_log_path}")
        else:
            print(f"No content found in {capsule_on_disk_log_path}")
    else:
        print(f"Error: {capsule_on_disk_log_path} not found.")

    # 3) capsule_test_results.log (UTF-8)
    if os.path.exists(capsule_test_results_log_path):
        lines = read_log_file(capsule_test_results_log_path, encoding='utf-8')
        if lines:
            parsed_test_results = parse_capsule_test_results_log(lines)
            tests.extend(parsed_test_results)
            print(f"Parsed {len(parsed_test_results)} tests from {capsule_test_results_log_path}")
        else:
            print(f"No content found in {capsule_test_results_log_path}")
    else:
        print(f"Error: {capsule_test_results_log_path} not found.")

    # Create summary
    summary = {
        'total_PASSED': sum(1 for t in tests if t['Test_Result'] == 'PASSED'),
        'total_FAILED': sum(1 for t in tests if t['Test_Result'] == 'FAILED'),
        'total_SKIPPED': sum(1 for t in tests if t['Test_Result'] == 'SKIPPED'),
    }

    # Build JUnit XML
    junit_xml_output = build_junit_xml(tests, summary)

    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    try:
        with open(output_file, 'wb') as f:
            f.write(junit_xml_output)
        print(f"JUnit XML generation complete. Results saved to {output_file}")
    except Exception as e:
        print(f"Error writing to {output_file}: {e}")

if __name__ == "__main__":
    main()

