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
    Parses the BSA/SBSA logs exactly as the JSON script does, returning
    the final list-of-dicts structure. The structure looks like:

    [
      {
        "Test_suite": "...",
        "subtests": [...],
        "test_suite_summary": {...}
      },
      ...
      {
        "Suite_summary": {...}
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
                # Remove leading timestamps in square brackets
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
                        i +=1
                        continue

                    # Try to match test line with result on same line
                    result_line_match = re.match(r'^\s*(\d+)\s*:\s*(.*?)\s*: Result:\s*(\w+)$', line)
                    if result_line_match:
                        test_number = result_line_match.group(1).strip()
                        test_name = result_line_match.group(2).strip()
                        raw_result = result_line_match.group(3).strip()
                        result = result_mapping.get(raw_result, raw_result)

                        # Check for duplicates
                        if test_number in test_numbers_per_suite[suite_name]:
                            i +=1
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
                        i +=1
                        continue

                    # Try to match test line without result
                    test_line_match = re.match(r'^\s*(\d+)\s*:\s*(.*)$', line)
                    if test_line_match:
                        test_number = test_line_match.group(1).strip()
                        test_name = test_line_match.group(2).strip()
                        in_test = True
                        test_description, result, rules = "","",""
                        i +=1
                        continue

                    elif in_test:
                        if ': Result:' in line:
                            # parse result
                            match_res = re.search(r': Result:\s*(\w+)', line)
                            if match_res:
                                raw_result = match_res.group(1).strip()
                                result = result_mapping.get(raw_result, raw_result)
                            else:
                                result = "UNKNOWN"

                            # Check duplicates
                            if test_number in test_numbers_per_suite[suite_name]:
                                i +=1
                                in_test = False
                                continue
                            # Build subtest entry
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
                            i +=1
                            continue
                        else:
                            # Possibly 'rules' or additional description lines
                            if re.match(r'^[A-Z0-9_ ,]+$', line.strip()) or line.strip().startswith('Appendix'):
                                rules = rules + ' ' + line.strip() if rules else line.strip()
                            else:
                                test_description = test_description + ' ' + line.strip() if test_description else line.strip()
                            i +=1
                            continue
                    else:
                        i +=1
                        continue
                else:
                    i +=1
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

    # Add the overall summary at the end
    formatted_result.append({
        "Suite_summary": suite_summary
    })

    return formatted_result

def dict_to_xml(data_list):
    """
    Converts the final list-of-dicts data structure into XML.
    The top-level structure is something like:
    [
      {
        "Test_suite": <name>,
        "subtests": [...],
        "test_suite_summary": {...}
      },
      ...
      {
        "Suite_summary": {...}
      }
    ]
    """
    root = ET.Element("bsa_sbsa_result")

    for item in data_list:
        # If this dictionary is the final "Suite_summary"
        if "Suite_summary" in item:
            # Put the overall summary at the end
            suite_summary_elem = ET.SubElement(root, "Suite_summary")
            for k,v in item["Suite_summary"].items():
                ET.SubElement(suite_summary_elem, k).text = str(v)
            continue

        # Otherwise it's a test suite entry
        suite_elem = ET.SubElement(root, "test_suite")

        # <Test_suite>
        ET.SubElement(suite_elem, "Test_suite").text = item.get("Test_suite", "Unknown")

        # <subtests>
        subtests_elem = ET.SubElement(suite_elem, "subtests")
        for sub in item["subtests"]:
            subtest_elem = ET.SubElement(subtests_elem, "subtest")
            ET.SubElement(subtest_elem, "sub_Test_Number").text = sub.get("sub_Test_Number", "")
            ET.SubElement(subtest_elem, "sub_Test_Description").text = sub.get("sub_Test_Description", "")
            ET.SubElement(subtest_elem, "sub_test_result").text = sub.get("sub_test_result", "")

            # If "RULES FAILED" or "RULES SKIPPED" exist
            if "RULES FAILED" in sub:
                ET.SubElement(subtest_elem, "RULES_FAILED").text = sub["RULES FAILED"]
            if "RULES SKIPPED" in sub:
                ET.SubElement(subtest_elem, "RULES_SKIPPED").text = sub["RULES SKIPPED"]

        # <test_suite_summary>
        summary_elem = ET.SubElement(suite_elem, "test_suite_summary")
        for k,v in item["test_suite_summary"].items():
            ET.SubElement(summary_elem, k).text = str(v)

    xml_bytes = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes

def main():
    parser = argparse.ArgumentParser(description="Parse BSA/SBSA log files and save results to an XML file.")
    parser.add_argument("input_files", nargs='+', help="Input log files")
    parser.add_argument("output_file", help="Output XML file")

    args = parser.parse_args()

    data_list = parse_logs_to_dict(args.input_files)
    xml_output = dict_to_xml(data_list)

    with open(args.output_file, 'wb') as xml_file:
        xml_file.write(xml_output)

    print(f"BSA/SBSA logs parsed successfully. XML output saved to {args.output_file}")

if __name__ == "__main__":
    main()
