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
import os
import xml.etree.ElementTree as ET

def create_subtest(subtest_number, description, status, reason=""):
    """
    EXACTLY THE SAME AS ORIGINAL
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
    EXACTLY THE SAME AS ORIGINAL
    """
    if status in ["PASSED", "FAILED", "SKIPPED", "ABORTED", "WARNINGS"]:
        suite_summary[f"total_{status}"] += 1

def parse_ethtool_test_log(log_data, os_name):
    """
    EXACTLY THE SAME AS ORIGINAL:
    Returns a dictionary:
    {
      "test_results": [
        {
          "Test_suite_name": "Network",
          "Test_suite_description": "Network validation",
          "Test_case": "ethtool_test_<os_name>",
          "Test_case_description": "Ethernet Tool Tests",
          "subtests": [ {...}, ... ],
          "test_suite_summary": {
            "total_PASSED": ...,
            "total_FAILED": ...,
            "total_SKIPPED": ...,
            "total_ABORTED": ...,
            "total_WARNINGS": ...
          }
        }
      ],
      "suite_summary": {
        "total_PASSED": ...,
        "total_FAILED": ...,
        "total_SKIPPED": ...,
        "total_ABORTED": ...,
        "total_WARNINGS": ...
      }
    }
    """
    test_suite_key = f"ethtool_test_{os_name}"

    mapping = {
        "Test_suite_name": "Network",
        "Test_suite_description": "Network validation",
        "Test_case_description": "Ethernet Tool Tests"
    }

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
    interface = None
    detected_interfaces = []

    i = 0
    while i < len(log_data):
        line = log_data[i].strip()

        # (All the logic below is identical to your code)
        # ...
        # 1) Detection of Ethernet Interfaces
        if line.startswith("INFO: Detected following ethernet interfaces via ip command :"):
            interfaces = []
            i += 1
            while i < len(log_data) and log_data[i].strip() and not log_data[i].startswith("INFO"):
                match = re.match(r'\d+:\s+(\S+)', log_data[i].strip())
                if match:
                    interfaces.append(match.group(1))
                i += 1
            if interfaces:
                detected_interfaces = interfaces
                status = "PASSED"
                description = f"Detection of Ethernet Interfaces: {', '.join(interfaces)}"
            else:
                status = "FAILED"
                description = "No Ethernet Interfaces Detected"

            subtest = create_subtest(subtest_number, description, status)
            current_test["subtests"].append(subtest)
            update_suite_summary(current_test["test_suite_summary"], status)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1
            continue

        # 2) Bringing Down Ethernet Interfaces
        if "INFO: Bringing down all ethernet interfaces using ifconfig" in line:
            status = "PASSED"
            description = "Bringing down all Ethernet interfaces"
            for j in range(i + 1, len(log_data)):
                if "Unable to bring down ethernet interface" in log_data[j]:
                    status = "FAILED"
                    description = "Failed to bring down some Ethernet interfaces"
                    break
                if "****************************************************************" in log_data[j]:
                    break
            subtest = create_subtest(subtest_number, description, status)
            current_test["subtests"].append(subtest)
            update_suite_summary(current_test["test_suite_summary"], status)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1

        # 3) Bringing up interface
        if "INFO: Bringing up ethernet interface:" in line:
            interface = line.split(":")[-1].strip()
            if i + 1 < len(log_data) and "Unable to bring up ethernet interface" in log_data[i + 1]:
                status = "FAILED"
                description = f"Bring up interface {interface}"
            else:
                status = "PASSED"
                description = f"Bring up interface {interface}"
            subtest = create_subtest(subtest_number, description, status)
            current_test["subtests"].append(subtest)
            update_suite_summary(current_test["test_suite_summary"], status)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1

        # 4) Running ethtool Command
        if f"INFO: Running \"ethtool {interface}\" :" in line:
            status = "PASSED"
            description = f"Running ethtool on {interface}"
            subtest = create_subtest(subtest_number, description, status)
            current_test["subtests"].append(subtest)
            update_suite_summary(current_test["test_suite_summary"], status)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1

        # 5) Ethernet interface self-test
        if "INFO: Ethernet interface" in line and "supports ethtool self test" in line:
            if "doesn't support ethtool self test" in line:
                status = "SKIPPED"
                description = f"Self-test on {interface} (Not supported)"
            else:
                result_index = i + 2
                if result_index < len(log_data) and "The test result is" in log_data[result_index]:
                    result_line = log_data[result_index].strip()
                    if "PASS" in result_line.upper():
                        status = "PASSED"
                    else:
                        status = "FAILED"
                    description = f"Self-test on {interface}"
                else:
                    status = "FAILED"
                    description = f"Self-test on {interface} (Result not found)"

            subtest = create_subtest(subtest_number, description, status)
            current_test["subtests"].append(subtest)
            update_suite_summary(current_test["test_suite_summary"], status)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1

        # 6) Link detection
        if "Link detected:" in line:
            if "yes" in line.lower():
                status = "PASSED"
                description = f"Link detected on {interface}"
            else:
                status = "FAILED"
                description = f"Link not detected on {interface}"
            subtest = create_subtest(subtest_number, description, status)
            current_test["subtests"].append(subtest)
            update_suite_summary(current_test["test_suite_summary"], status)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1

        # 7) DHCP support
        if "doesn't support DHCP" in line or "supports DHCP" in line:
            if "doesn't support DHCP" in line:
                status = "FAILED"
                description = f"DHCP support on {interface}"
            else:
                status = "PASSED"
                description = f"DHCP support on {interface}"
            subtest = create_subtest(subtest_number, description, status)
            current_test["subtests"].append(subtest)
            update_suite_summary(current_test["test_suite_summary"], status)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1

        # 8) Ping to router/gateway
        if "INFO: Ping to router/gateway" in line:
            if "is successful" in line:
                status = "PASSED"
                description = f"Ping to router/gateway on {interface}"
            else:
                status = "FAILED"
                description = f"Ping to router/gateway on {interface}"
            subtest = create_subtest(subtest_number, description, status)
            update_suite_summary(current_test["test_suite_summary"], status)
            current_test["subtests"].append(subtest)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1

        # 9) Ping to www.arm.com
        if "INFO: Ping to www.arm.com" in line:
            if "is successful" in line:
                status = "PASSED"
                description = f"Ping to www.arm.com on {interface}"
            else:
                status = "FAILED"
                description = f"Ping to www.arm.com on {interface}"
            subtest = create_subtest(subtest_number, description, status)
            update_suite_summary(current_test["test_suite_summary"], status)
            current_test["subtests"].append(subtest)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1

        i += 1

    # If ping tests were not found, mark them SKIPPED
    for intf in detected_interfaces:
        ping_to_router_present = any(
            st["sub_Test_Description"] == f"Ping to router/gateway on {intf}"
            for st in current_test["subtests"]
        )
        ping_to_arm_present = any(
            st["sub_Test_Description"] == f"Ping to www.arm.com on {intf}"
            for st in current_test["subtests"]
        )
        if not ping_to_router_present:
            description = f"Ping to router/gateway on {intf}"
            status = "SKIPPED"
            subtest = create_subtest(subtest_number, description, status)
            current_test["subtests"].append(subtest)
            update_suite_summary(current_test["test_suite_summary"], status)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1
        if not ping_to_arm_present:
            description = f"Ping to www.arm.com on {intf}"
            status = "SKIPPED"
            subtest = create_subtest(subtest_number, description, status)
            current_test["subtests"].append(subtest)
            update_suite_summary(current_test["test_suite_summary"], status)
            suite_summary[f"total_{status}"] += 1
            subtest_number += 1

    return {
        "test_results": [current_test],
        "suite_summary": suite_summary
    }

def parse_log(log_file_path, os_name):
    with open(log_file_path, 'r') as f:
        log_data = f.readlines()
    return parse_ethtool_test_log(log_data, os_name)

#
# NEW FUNCTION: dict_to_junit_xml
#
def dict_to_junit_xml(data_dict):
    """
    Convert the data structure to JUnit XML:
    {
      "test_results": [
        {
          "Test_suite_name": "Network",
          "Test_suite_description": "Network validation",
          "Test_case": "ethtool_test_<os_name>",
          "Test_case_description": "Ethernet Tool Tests",
          "subtests": [...],
          "test_suite_summary": {...}
        }
      ],
      "suite_summary": {...}
    }

    We'll create one <testsuite> for each item in "test_results".
    Each "subtest" becomes a <testcase>.
    We'll map:
      - FAILED -> <failure>
      - ABORTED -> <error>
      - SKIPPED -> <skipped>
      - WARNINGS -> stored in <system-out> (no direct JUnit concept)
      - PASSED -> no child elements => test passed
    """
    # Create <testsuites> root
    root = ET.Element("testsuites")

    # test_results is typically a list of 1 item here, but we'll loop in case it's more
    for test_obj in data_dict["test_results"]:
        # Create a <testsuite>
        testsuite_elem = ET.SubElement(root, "testsuite")

        # Name the suite using "Test_case" or "Test_suite_name" or both
        # For example: "Network :: ethtool_test_os_name"
        suite_name = f"{test_obj['Test_suite_name']} :: {test_obj['Test_case']}"
        testsuite_elem.set("name", suite_name)

        # Gather summary counts
        summary = test_obj["test_suite_summary"]
        total_subtests = len(test_obj["subtests"])
        testsuite_elem.set("tests", str(total_subtests))
        testsuite_elem.set("failures", str(summary["total_FAILED"]))
        # We'll treat ABORTED as "errors" in JUnit
        testsuite_elem.set("errors", str(summary["total_ABORTED"]))
        testsuite_elem.set("skipped", str(summary["total_SKIPPED"]))
        # We ignore "WARNINGS" for suite-level attributes

        # Optionally store "Test_suite_description" or "Test_case_description" in <properties>
        props_elem = ET.SubElement(testsuite_elem, "properties")

        p_desc = ET.SubElement(props_elem, "property")
        p_desc.set("name", "Test_suite_description")
        p_desc.set("value", test_obj["Test_suite_description"])

        p_case_desc = ET.SubElement(props_elem, "property")
        p_case_desc.set("name", "Test_case_description")
        p_case_desc.set("value", test_obj["Test_case_description"])

        # Now create <testcase> for each subtest
        for sub in test_obj["subtests"]:
            testcase_elem = ET.SubElement(testsuite_elem, "testcase")
            # "classname" can be the suite name or something else
            testcase_elem.set("classname", test_obj["Test_suite_name"])

            # Combine sub_Test_Number + sub_Test_Description for the "name"
            title = f"{sub['sub_Test_Number']}: {sub['sub_Test_Description']}"
            testcase_elem.set("name", title)

            res = sub["sub_test_result"]
            # Decide pass/fail/skip/abort/warnings
            if res["FAILED"] > 0:
                failure_elem = ET.SubElement(testcase_elem, "failure")
                failure_elem.set("message", "Test Failed")
                failure_elem.set("type", "AssertionError")

                # If there's fail_reasons, combine them
                if res["fail_reasons"]:
                    failure_elem.text = "\n".join(res["fail_reasons"])
                else:
                    failure_elem.text = "No specific failure reason given."

            elif res["ABORTED"] > 0:
                error_elem = ET.SubElement(testcase_elem, "error")
                error_elem.set("message", "Test Aborted")
                error_elem.set("type", "AbortedTest")
                # If you had "abort_reasons", you could put them in error_elem.text
                if res["abort_reasons"]:
                    error_elem.text = "\n".join(res["abort_reasons"])
                else:
                    error_elem.text = "No specific abort reason given."

            elif res["SKIPPED"] > 0:
                skipped_elem = ET.SubElement(testcase_elem, "skipped")
                skipped_elem.set("message", "Test Skipped")
                if res["skip_reasons"]:
                    skipped_elem.text = "\n".join(res["skip_reasons"])

            elif res["WARNINGS"] > 0:
                # No official "warning" tag in JUnit, so let's put it in <system-out>
                system_out = ET.SubElement(testcase_elem, "system-out")
                reasons = "\n".join(res["warning_reasons"]) if res["warning_reasons"] else ""
                system_out.text = f"WARNINGS:\n{reasons}" if reasons else "Warning encountered"

            elif res["PASSED"] > 0:
                # No child elements => test is 'passed'
                # If you want to store pass_reasons, you can add them in <system-out>:
                if res["pass_reasons"]:
                    system_out = ET.SubElement(testcase_elem, "system-out")
                    system_out.text = "\n".join(res["pass_reasons"])

            else:
                # If none of the counters are > 0 (very unlikely in your code), treat as "unknown"
                system_out = ET.SubElement(testcase_elem, "system-out")
                system_out.text = (
                    "Unrecognized result. Possibly 0 in all counters.\n"
                    f"Data: {res}"
                )

    # (Optionally) data_dict["suite_summary"] is global. JUnit doesn't have a direct "global" summary.
    # Typically, we'd skip it or add it as a separate testsuite. For now, let's skip.

    # Convert tree to bytes with XML declaration
    xml_bytes = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 logs_to_junitxml.py <ethtool_test.log> <output JUnit XML file> <os_name>")
        sys.exit(1)

    log_file_path = sys.argv[1]
    output_file_path = sys.argv[2]
    os_name = sys.argv[3]

    # 1) Parse the log (identical to original)
    data_dict = parse_log(log_file_path, os_name)

    # 2) Convert to JUnit XML instead of custom XML
    junit_xml_output = dict_to_junit_xml(data_dict)

    # 3) Write JUnit XML to output
    os.makedirs(os.path.dirname(output_file_path), exist_ok=True)
    with open(output_file_path, 'wb') as outfile:
        outfile.write(junit_xml_output)

    print(f"Log parsed successfully. JUnit XML saved to {output_file_path}")

