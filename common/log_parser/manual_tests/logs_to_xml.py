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
    Creates a subtest dictionary object. The data structure matches
    the JSON version but we'll later build XML from it.
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
    Increments the relevant total_* counter in suite_summary.
    """
    if status in ["PASSED", "FAILED", "SKIPPED", "ABORTED", "WARNINGS"]:
        suite_summary[f"total_{status}"] += 1

def parse_ethtool_test_log(log_data, os_name):
    """
    Parses the ethtool log data and returns a dictionary of
    { "test_results": [...], "suite_summary": {...} }
    """
    test_suite_key = f"ethtool_test_{os_name}"  # e.g. "ethtool_test_linux-opensuse-leap-15.5-version"

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
                result_index = i + 2  # guess result is two lines after
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

    # If ping tests were not found, add them as SKIPPED
    for intf in detected_interfaces:
        # Check if ping tests for this interface are present
        ping_to_router_present = any(
            subtest["sub_Test_Description"] == f"Ping to router/gateway on {intf}"
            for subtest in current_test["subtests"]
        )
        ping_to_arm_present = any(
            subtest["sub_Test_Description"] == f"Ping to www.arm.com on {intf}"
            for subtest in current_test["subtests"]
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
            update_suite_summary(current_test["test_suite_summary"], status)
            current_test["subtests"].append(subtest)
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

def dict_to_xml(data_dict):
    """
    Convert the parsed data_dict to XML. The structure is:
    {
      "test_results": [
         {
            "Test_suite_name": ...,
            "Test_suite_description": ...,
            "Test_case": ...,
            "Test_case_description": ...,
            "subtests": [...],
            "test_suite_summary": {...}
         }
      ],
      "suite_summary": {...}
    }
    """
    root = ET.Element("ethtool_test_result")

    # <test_results>
    test_results_elem = ET.SubElement(root, "test_results")
    for test_obj in data_dict["test_results"]:
        test_elem = ET.SubElement(test_results_elem, "test")

        ET.SubElement(test_elem, "Test_suite_name").text = test_obj.get("Test_suite_name", "")
        ET.SubElement(test_elem, "Test_suite_description").text = test_obj.get("Test_suite_description", "")
        ET.SubElement(test_elem, "Test_case").text = test_obj.get("Test_case", "")
        ET.SubElement(test_elem, "Test_case_description").text = test_obj.get("Test_case_description", "")

        # subtests
        subs_elem = ET.SubElement(test_elem, "subtests")
        for sub in test_obj["subtests"]:
            sub_elem = ET.SubElement(subs_elem, "subtest")
            ET.SubElement(sub_elem, "sub_Test_Number").text = sub.get("sub_Test_Number", "")
            ET.SubElement(sub_elem, "sub_Test_Description").text = sub.get("sub_Test_Description", "")

            # sub_test_result
            result_elem = ET.SubElement(sub_elem, "sub_test_result")
            res_dict = sub["sub_test_result"]
            for key in ["PASSED","FAILED","ABORTED","SKIPPED","WARNINGS"]:
                ET.SubElement(result_elem, key).text = str(res_dict[key])

            # reason lists
            pass_reasons_elem = ET.SubElement(result_elem, "pass_reasons")
            for r in res_dict["pass_reasons"]:
                reason_el = ET.SubElement(pass_reasons_elem, "reason")
                reason_el.text = r

            fail_reasons_elem = ET.SubElement(result_elem, "fail_reasons")
            for r in res_dict["fail_reasons"]:
                reason_el = ET.SubElement(fail_reasons_elem, "reason")
                reason_el.text = r

            abort_reasons_elem = ET.SubElement(result_elem, "abort_reasons")
            for r in res_dict["abort_reasons"]:
                reason_el = ET.SubElement(abort_reasons_elem, "reason")
                reason_el.text = r

            skip_reasons_elem = ET.SubElement(result_elem, "skip_reasons")
            for r in res_dict["skip_reasons"]:
                reason_el = ET.SubElement(skip_reasons_elem, "reason")
                reason_el.text = r

            warning_reasons_elem = ET.SubElement(result_elem, "warning_reasons")
            for r in res_dict["warning_reasons"]:
                reason_el = ET.SubElement(warning_reasons_elem, "reason")
                reason_el.text = r

        # test_suite_summary
        summary_elem = ET.SubElement(test_elem, "test_suite_summary")
        for k in ["total_PASSED","total_FAILED","total_SKIPPED","total_ABORTED","total_WARNINGS"]:
            ET.SubElement(summary_elem, k).text = str(test_obj["test_suite_summary"][k])

    # <suite_summary>
    suite_sum_elem = ET.SubElement(root, "suite_summary")
    for k in ["total_PASSED","total_FAILED","total_SKIPPED","total_ABORTED","total_WARNINGS"]:
        ET.SubElement(suite_sum_elem, k).text = str(data_dict["suite_summary"][k])

    xml_bytes = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 logs_to_xml.py <ethtool_test.log> <output XML file> <os_name>")
        sys.exit(1)

    log_file_path = sys.argv[1]
    output_file_path = sys.argv[2]
    os_name = sys.argv[3]

    # 1) Parse the log
    data_dict = parse_log(log_file_path, os_name)
    # 2) Convert to XML
    xml_output = dict_to_xml(data_dict)
    # 3) Write XML
    with open(output_file_path, 'wb') as outfile:
        outfile.write(xml_output)

    print(f"Log parsed successfully. XML output saved to {output_file_path}")
