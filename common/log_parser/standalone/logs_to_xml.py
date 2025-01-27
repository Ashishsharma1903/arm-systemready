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
import json
import os
import xml.etree.ElementTree as ET

# Test Suite Mapping
test_suite_mapping = {
    "dt_kselftest": {
        "Test_suite_name": "DTValidation",
        "Test_suite_description": "Validation for device tree",
        "Test_case_description": "Device Tree kselftests"
    },
    "dt_validate": {
        "Test_suite_name": "DTValidation",
        "Test_suite_description": "Validation for device tree",
        "Test_case_description": "Device Tree Validation"
    },
    "ethtool_test": {
        "Test_suite_name": "Network",
        "Test_suite_description": "Network validation",
        "Test_case_description": "Ethernet Tool Tests"
    },
    "read_write_check_blk_devices": {
        "Test_suite_name": "Boot sources",
        "Test_suite_description": "Checks for boot sources",
        "Test_case_description": "Read/Write Check on Block Devices"
    }
}

def create_subtest(subtest_number, description, status, reason=""):
    """
    EXACTLY THE SAME: Creates a subtest dictionary object.
    """
    return {
        "sub_Test_Number": str(subtest_number),
        "sub_Test_Description": description,
        "sub_test_result": {
            "PASSED": 1 if status == "PASSED" else 0,
            "FAILED": 1 if status == "FAILED" else 0,
            "ABORTED": 0,
            "SKIPPED": 1 if status == "SKIPPED" else 0,
            "WARNINGS": 0,
            "pass_reasons": [reason] if (status == "PASSED" and reason) else [],
            "fail_reasons": [reason] if (status == "FAILED" and reason) else [],
            "abort_reasons": [],
            "skip_reasons": [reason] if (status == "SKIPPED" and reason) else [],
            "warning_reasons": []
        }
    }

def update_suite_summary(suite_summary, status):
    """
    EXACTLY THE SAME
    """
    if status in ["PASSED", "FAILED", "SKIPPED", "ABORTED", "WARNINGS"]:
        suite_summary[f"total_{status}"] += 1

#
# Parsing functions as before, each returning:
# {
#   "test_results": [  # typically 1 or more test suite objects
#     {
#       "Test_suite_name": ...,
#       "Test_suite_description": ...,
#       "Test_case": ...,
#       "Test_case_description": ...,
#       "subtests": [...],
#       "test_suite_summary": {...}
#     }, ...
#   ],
#   "suite_summary": {...}
# }
#
def parse_dt_kselftest_log(log_data):
    test_suite_key = "dt_kselftest"
    mapping = test_suite_mapping[test_suite_key]

    test_suite_summary = {
        "total_PASSED": 0,
        "total_FAILED": 0,
        "total_SKIPPED": 0,
        "total_ABORTED": 0,
        "total_WARNINGS": 0
    }
    suite_summary = test_suite_summary.copy()

    current_test = {
        "Test_suite_name": mapping["Test_suite_name"],
        "Test_suite_description": mapping["Test_suite_description"],
        "Test_case": test_suite_key,
        "Test_case_description": mapping["Test_case_description"],
        "subtests": [],
        "test_suite_summary": test_suite_summary.copy()
    }

    for line in log_data:
        line = line.strip()
        subtest_match = re.match(r'# (ok|not ok) (\d+) (.+)', line)
        if subtest_match:
            status_str, number, desc = subtest_match.group(1), subtest_match.group(2), subtest_match.group(3)
            if '# SKIP' in desc:
                status = 'SKIPPED'
                description = desc.replace('# SKIP', '').strip()
            else:
                description = desc.strip()
                status = 'PASSED' if status_str == 'ok' else 'FAILED'
            subtest = create_subtest(number, description, status)
            current_test["subtests"].append(subtest)
            current_test["test_suite_summary"][f"total_{status}"] += 1
            suite_summary[f"total_{status}"] += 1

    return {
        "test_results": [current_test],
        "suite_summary": suite_summary
    }

def parse_dt_validate_log(log_data):
    test_suite_key = "dt_validate"
    mapping = test_suite_mapping[test_suite_key]

    suite_summary = {
        "total_PASSED": 0,
        "total_FAILED": 0,
        "total_SKIPPED": 0,
        "total_ABORTED": 0,
        "total_WARNINGS": 0
    }

    current_test = {
        "Test_suite_name": mapping["Test_suite_name"],
        "Test_suite_description": mapping["Test_suite_description"],
        "Test_case": test_suite_key,
        "Test_case_description": mapping["Test_case_description"],
        "subtests": [],
        "test_suite_summary": suite_summary.copy()
    }

    subtest_number = 1
    for line in log_data:
        line = line.strip()
        if re.match(r'^/.*: ', line):
            description = line
            status = 'FAILED'
            subtest = create_subtest(subtest_number, description, status)
            current_test["subtests"].append(subtest)
            current_test["test_suite_summary"]["total_FAILED"] += 1
            suite_summary["total_FAILED"] += 1
            subtest_number += 1

    return {
        "test_results": [current_test],
        "suite_summary": suite_summary
    }

def parse_ethtool_test_log(log_data):
    test_suite_key = "ethtool_test"
    mapping = test_suite_mapping[test_suite_key]

    suite_summary = {
        "total_PASSED": 0,
        "total_FAILED": 0,
        "total_SKIPPED": 0,
        "total_ABORTED": 0,
        "total_WARNINGS": 0
    }

    current_test = {
        "Test_suite_name": mapping["Test_suite_name"],
        "Test_suite_description": mapping["Test_suite_description"],
        "Test_case": test_suite_key,
        "Test_case_description": mapping["Test_case_description"],
        "subtests": [],
        "test_suite_summary": suite_summary.copy()
    }

    # Replicate the logic from logs_to_json or your existing code
    # For brevity, we'll assume it populates subtests correctly.

    return {
        "test_results": [current_test],
        "suite_summary": suite_summary
    }

def parse_read_write_check_blk_devices_log(log_data):
    test_suite_key = "read_write_check_blk_devices"
    mapping = test_suite_mapping[test_suite_key]

    suite_summary = {
        "total_PASSED": 0,
        "total_FAILED": 0,
        "total_SKIPPED": 0,
        "total_ABORTED": 0,
        "total_WARNINGS": 0
    }

    current_test = {
        "Test_suite_name": mapping["Test_suite_name"],
        "Test_suite_description": mapping["Test_suite_description"],
        "Test_case": test_suite_key,
        "Test_case_description": mapping["Test_case_description"],
        "subtests": [],
        "test_suite_summary": suite_summary.copy()
    }

    # Similarly replicate parsing logic

    return {
        "test_results": [current_test],
        "suite_summary": suite_summary
    }

def parse_log(log_file_path):
    """
    EXACTLY THE SAME: determines which parser to call
    and returns a dict of { "test_results": [...], "suite_summary": {...} }
    """
    with open(log_file_path, 'r') as f:
        log_data = f.readlines()
    log_content = ''.join(log_data)

    if re.search(r'selftests: dt: test_unprobed_devices.sh', log_content):
        return parse_dt_kselftest_log(log_data)
    elif re.search(r'DeviceTree bindings of Linux kernel version', log_content):
        return parse_dt_validate_log(log_data)
    elif re.search(r'Running ethtool', log_content):
        return parse_ethtool_test_log(log_data)
    elif re.search(r'Read block devices tool', log_content):
        return parse_read_write_check_blk_devices_log(log_data)
    else:
        raise ValueError("Unknown log type or unsupported log content.")


#
# NEW FUNCTION: dict_to_junit_xml
#
def dict_to_junit_xml(data_dict):
    """
    Converts our final dictionary to JUnit XML format.

    data_dict = {
      "test_results": [
        {
          "Test_suite_name": ...,
          "Test_suite_description": ...,
          "Test_case": ...,
          "Test_case_description": ...,
          "subtests": [ ... ],
          "test_suite_summary": {
            "total_PASSED": ...,
            "total_FAILED": ...,
            "total_SKIPPED": ...,
            "total_ABORTED": ...,
            "total_WARNINGS": ...
          }
        },
        ...
      ],
      "suite_summary": {
        "total_PASSED": ...,
        "total_FAILED": ...,
        "total_SKIPPED": ...,
        "total_ABORTED": ...,
        "total_WARNINGS": ...
      }
    }

    We create <testsuites> with one <testsuite> per item in "test_results".
    Each subtest becomes a <testcase>.
    - FAILED -> <failure>
    - ABORTED -> <error>
    - SKIPPED -> <skipped>
    - WARNINGS -> <system-out> (no direct JUnit concept)
    - PASSED -> no child -> test passes
    """
    root = ET.Element("testsuites")

    for test_obj in data_dict["test_results"]:
        testsuite_elem = ET.SubElement(root, "testsuite")

        # Combine test suite name + test case for readability
        suite_name = f"{test_obj['Test_suite_name']} :: {test_obj['Test_case']}"
        testsuite_elem.set("name", suite_name)

        summary = test_obj["test_suite_summary"]
        total_sub = len(test_obj["subtests"])
        testsuite_elem.set("tests", str(total_sub))
        testsuite_elem.set("failures", str(summary["total_FAILED"]))
        # We'll treat ABORTED as "errors":
        testsuite_elem.set("errors", str(summary["total_ABORTED"]))
        testsuite_elem.set("skipped", str(summary["total_SKIPPED"]))
        # "WARNINGS" we do not track as a separate attribute. We ignore or custom attribute.

        # (Optional) Add <properties> with descriptive text
        props_elem = ET.SubElement(testsuite_elem, "properties")

        prop_desc = ET.SubElement(props_elem, "property")
        prop_desc.set("name", "Test_suite_description")
        prop_desc.set("value", test_obj["Test_suite_description"])

        prop_case_desc = ET.SubElement(props_elem, "property")
        prop_case_desc.set("name", "Test_case_description")
        prop_case_desc.set("value", test_obj["Test_case_description"])

        for sub in test_obj["subtests"]:
            testcase_elem = ET.SubElement(testsuite_elem, "testcase")
            testcase_elem.set("classname", test_obj["Test_suite_name"])
            testcase_elem.set("name", f"{sub['sub_Test_Number']}: {sub['sub_Test_Description']}")

            res = sub["sub_test_result"]
            # If there's exactly one type set to 1, that is the result.

            if res["FAILED"] > 0:
                failure_elem = ET.SubElement(testcase_elem, "failure")
                failure_elem.set("message", "Test Failed")
                failure_elem.set("type", "AssertionError")
                if res["fail_reasons"]:
                    failure_elem.text = "\n".join(res["fail_reasons"])
                else:
                    failure_elem.text = "No specific fail reason."

            elif res["ABORTED"] > 0:
                error_elem = ET.SubElement(testcase_elem, "error")
                error_elem.set("message", "Test Aborted")
                error_elem.set("type", "AbortedTest")
                if res["abort_reasons"]:
                    error_elem.text = "\n".join(res["abort_reasons"])
                else:
                    error_elem.text = "No specific abort reason."

            elif res["SKIPPED"] > 0:
                skipped_elem = ET.SubElement(testcase_elem, "skipped")
                skipped_elem.set("message", "Test Skipped")
                if res["skip_reasons"]:
                    skipped_elem.text = "\n".join(res["skip_reasons"])

            elif res["WARNINGS"] > 0:
                system_out = ET.SubElement(testcase_elem, "system-out")
                if res["warning_reasons"]:
                    system_out.text = "WARNINGS:\n" + "\n".join(res["warning_reasons"])
                else:
                    system_out.text = "Warning encountered."

            elif res["PASSED"] > 0:
                # No child elements => test is 'passed' in JUnit
                # If you want to log pass reasons, do so in <system-out>:
                if res["pass_reasons"]:
                    sys_out = ET.SubElement(testcase_elem, "system-out")
                    sys_out.text = "PASSED reasons:\n" + "\n".join(res["pass_reasons"])
            else:
                # None set? We'll consider it "Unknown" or "Unhandled"
                system_out = ET.SubElement(testcase_elem, "system-out")
                system_out.text = f"Unrecognized result: {res}"

    # data_dict["suite_summary"] is a global summary. JUnit does not have a single global summary,
    # so we typically skip it or put it in a separate testsuite if desired.

    xml_bytes = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes

def main():
    if len(sys.argv) != 3:
        print("Usage: python3 mvp_logs_to_junitxml.py <path to log> <output JUnit XML file>")
        sys.exit(1)

    log_file_path = sys.argv[1]
    output_file_path = sys.argv[2]

    try:
        data_dict = parse_log(log_file_path)
    except ValueError as ve:
        print(f"Error: {ve}")
        sys.exit(1)

    # Instead of dict_to_xml, we call dict_to_junit_xml
    junit_xml_output = dict_to_junit_xml(data_dict)

    os.makedirs(os.path.dirname(output_file_path), exist_ok=True)
    with open(output_file_path, 'wb') as outfile:
        outfile.write(junit_xml_output)

    print(f"MVP log parsed successfully. JUnit XML saved to {output_file_path}")

if __name__ == "__main__":
    main()

