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
import re
import xml.etree.ElementTree as ET
import chardet

test_mapping = {
    "GenericTest": {
        "EFICompliantTest": [
            "PlatformSpecificElements",
            "RequiredElements"
        ],
        "SbbrEfiSpecVerLvl": [
            "TestEfiSpecVerLvl"
        ],
        "SbbrSysEnvConfig": [
            "BootExcLevel"
        ]
    },
    "BootServicesTest": {
        "EventTimerandPriorityServicesTest": [
            "CheckEvent_Conf",
            "CheckEvent_Func",
            "CloseEvent_Func",
            "CreateEventEx_Conf",
            "CreateEventEx_Func",
            "CreateEvent_Conf",
            "CreateEvent_Func",
            "RaiseTPL_Func",
            "RestoreTPL_Func",
            "SetTimer_Conf",
            "SetTimer_Func",
            "SignalEvent_Func",
            "WaitForEvent_Conf",
            "WaitForEvent_Func"
        ],
        "MemoryAllocationServicesTest": [
            "AllocatePages_Conf",
            "AllocatePages_Func",
            "AllocatePool_Conf",
            "AllocatePool_Func",
            "FreePages_Conf",
            "FreePages_Func",
            "GetMemoryMap_Conf",
            "GetMemoryMap_Func"
        ],
        "ProtocolHandlerServicesTest": [
            "CloseProtocol_Conf",
            "CloseProtocol_Func",
            "ConnectController_Conf",
            "ConnectController_Func",
            "DisconnectController_Conf",
            "DisconnectController_Func",
            "HandleProtocol_Conf",
            "HandleProtocol_Func",
            "InstallMultipleProtocolInterfaces_Conf",
            "InstallMultipleProtocolInterfaces_Func",
            "InstallProtocolInterface_Conf",
            "InstallProtocolInterface_Func",
            "LocateDevicePath_Conf",
            "LocateDevicePath_Func",
            "LocateHandleBuffer_Conf",
            "LocateHandleBuffer_Func",
            "LocateHandle_Conf",
            "LocateHandle_Func",
            "LocateProtocol_Conf",
            "LocateProtocol_Func",
            "OpenProtocolInformation_Conf",
            "OpenProtocolInformation_Func",
            "OpenProtocol_Conf",
            "OpenProtocol_Func_1",
            "OpenProtocol_Func_2",
            "OpenProtocol_Func_3",
            "ProtocolsPerHandle_Conf",
            "ProtocolsPerHandle_Func",
            "RegisterProtocolNotify_Conf",
            "RegisterProtocolNotify_Func",
            "ReinstallProtocolInterface_Conf",
            "ReinstallProtocolInterface_Func",
            "UninstallMultipleProtocolInterfaces_Conf",
            "UninstallMultipleProtocolInterfaces_Func",
            "UninstallProtocolInterface_Conf",
            "UninstallProtocolInterface_Func"
        ],
        "ImageServicesTest": [
            "ExitBootServices_Conf",
            "Exit_Conf",
            "Exit_Func",
            "LoadImage_Conf",
            "LoadImage_Func",
            "StartImage_Conf",
            "StartImage_Func",
            "UnloadImage_Conf",
            "UnloadImage_Func"
        ],
        "MiscBootServicesTest": [
            "CalculateCrc32_Conf",
            "CalculateCrc32_Func",
            "CopyMem_Func",
            "GetNextMonotonicCount_Conf",
            "GetNextMonotonicCount_Func",
            "InstallConfigurationTable_Conf",
            "InstallConfigurationTable_Func",
            "SetMem_Func",
            "SetWatchdogTimer_Conf",
            "SetWatchdogTimer_Func",
            "Stall_Func"
        ]
    },
    "RuntimeServicesTest": {
        "VariableServicesTest": [
            "GetNextVariableName_Conf",
            "GetNextVariableName_Func",
            "GetVariable_Conf",
            "GetVariable_Func",
            "HardwareErrorRecord_Conf",
            "HardwareErrorRecord_Func",
            "QueryVariableInfo_Conf",
            "QueryVariableInfo_Func",
            "SetVariable_Conf",
            "SetVariable_Func",
            "AuthVar_Conf",
            "AuthVar_Func"
        ],
        "TimeServicesTest": [
            "GetTime_Conf",
            "GetTime_Func",
            "GetWakeupTime_Conf",
            "GetWakeupTime_Func",
            "SetTime_Conf",
            "SetTime_Func",
            "SetWakeupTime_Conf",
            "SetWakeupTime_Func"
        ],
        "MiscRuntimeServicesTest": [
            "QueryCapsuleCapabilities_Conf",
            "QueryCapsuleCapabilities_Func",
            "UpdateCapsule_Conf"
        ],
        "SBBRRuntimeServicesTest": [
            "Non-volatile Variable Reset Test",
            "Runtime Services Test"
        ],
        "SecureBootTest":[
            "ImageLoading",
            "VariableAttributes",
            "VariableUpdates"
        ],
        "BBSRVariableSizeTest":[
            "BBSRVariableSizeTest_func"
        ],
        "TCGMemoryOverwriteRequestTest":[
            "Test MOR and MORLOCK"
        ]
    },
    "TCG2ProtocolTest":{
        "GetActivePcrBanks_Conf":[
            "GetActivePcrBanks_Conf"
        ],
        "GetCapability_Conf":[
            "GetCapability_Conf"
        ],
        "HashLogExtendEvent_Conf":[
            "HashLogExtendEvent_Conf"
        ],
        "SubmitCommand_Conf":[
            "SubmitCommand_Conf"
        ]
    },
    "PlatformResetAttackMitigationPsciTest":{
        "PlatformResetAttackMitigationPsciTest_func":[
            "PlatformResetAttackMitigationPsciTest_func"
        ]
    },
    "LoadedImageProtocolTest": {
        "LoadedImageProtocolTest1": [
            "LoadedImageProtocolTest1"
        ],
        "LoadedImageProtocolTest2": [
            "LoadedImageProtocolTest2"
        ]
    },
    "DevicePathProcotols": {
        "DevicePathProcotolTest": [
            "PathNode_Conf"
        ],
        "DevicePathUtilitiesProcotolTest": [
            "AppendDeviceNode_Conformance",
            "AppendDeviceNode_Functionality",
            "AppendDevicePathInstance_Conformance",
            "AppendDevicePathInstance_Functionality",
            "AppendDevicePath_Conformance",
            "AppendDevicePath_Functionality",
            "CreatDeviceNode_Functionality",
            "CreateDeviceNode_Conformance",
            "DuplicateDevicePath_Conformance",
            "DuplicateDevicePath_Functionality",
            "GetDevicePathSize_Conformance",
            "GetDevicePathSize_Functionality",
            "GetNextDevicePathInstance_Conformance",
            "GetNextDevicePathInstance_Functionality",
            "IsDevicePathMultiInstance_Functionality"
        ]
    },
    "HIITest": {
        "HIIDatabaseProtocolTest": [
            "ExportPackageListsConformance",
            "ExportPackageListsFunction",
            "FindKeyboardLayoutsConformance",
            "FindKeyboardLayoutsFunction",
            "GetKeyboardLayoutConformance",
            "GetKeyboardLayoutFunction",
            "GetPackageListHandleConformance",
            "GetPackageListHandleFunction",
            "ListPackageListsConformance",
            "ListPackageListsFunction",
            "NewPackageListConformance",
            "NewPackageListFunction",
            "RegisterPackageNotifyConformance",
            "RemovePackageListConformance",
            "RemovePackageListFunction",
            "SetKeyboardLayoutConformance",
            "SetKeyboardLayoutFunction",
            "UnregisterPackageNotifyConformance",
            "UpdatePackageListConformance",
            "UpdatePackageListFunction"
        ]
    },
    "NetworkSupportTest": {
        "SimpleNetworkProtocolTest": [
            "GetStatus_Conf",
            "GetStatus_Func",
            "Initialize_Conf",
            "Initialize_Func",
            "MCastIpToMac_Conf",
            "MCastIpToMac_Func",
            "Receive_Conf",
            "Reset_Conf",
            "Reset_Func",
            "Shutdown_Conf",
            "Shutdown_Func",
            "Start_Conf",
            "Start_Func",
            "Stop_Conf",
            "Stop_Func",
            "Transmit_Conf"
        ]
    },
    "SecureTechTest": {
        "RNGProtocolTest": [
            "GetInfo_Conf",
            "GetInfo_Func",
            "GetRNG_Conf",
            "GetRNG_Func"
        ]
    },
    "ConsoleSupportTest": {
        "SimpleTextInputExProtocolTest": [
            "ReadKeyStrokeExConformance",
            "ReadKeyStrokeExFunctionAuto",
            "RegisterKeyNotifyConformance",
            "ResetFunctionAuto",
            "SetStateConformance",
            "UnregisterKeyNotifyConformance"
        ],
        "SimpleInputProtocolTest": [
            "Reset_Func"
        ],
        "SimpleOutputProtocolTest": [
            "ClearScreen_Func",
            "EnableCursor_Func",
            "OutputString_Func",
            "QueryMode_Conf",
            "QueryMode_Func",
            "Reset_Func",
            "SetAttribute_Func",
            "SetCursorPosition_Conf",
            "SetCursorPosition_Func",
            "SetMode_Conf",
            "SetMode_Func",
            "TestString_Func"
        ]
    }
}

def detect_file_encoding(file_path):
    with open(file_path, 'rb') as file:
        raw_data = file.read()
        result = chardet.detect(raw_data)
        return result['encoding']

def clean_test_description(description):
    """
    Cleans the test description by removing any file paths at the start.
    This is specific to cases where the description begins with '/home/...'.
    """
    if description.startswith("/"):
        # Extract the last part after the last comma or period
        cleaned_desc = re.split(r"[,.]", description)[-1].strip()
        return cleaned_desc
    return description

def find_test_suite_and_subsuite(test_case_name):
    """
    Finds the Test Suite and Sub Test Suite for a given Test Case name from the mapping.
    """
    for test_suite, sub_suites in test_mapping.items():
        for sub_suite, test_cases in sub_suites.items():
            if test_case_name in test_cases:
                return test_suite, sub_suite
    return None, None  # If not found

def parse_sct_log(input_file):
    """
    IDENTICAL to your existing parse_sct_log in logs_to_xml.py.

    Returns a dictionary:
    {
      "test_results": [  # List of "test entries"
        {
          "Test_suite": str,
          "Sub_test_suite": str,
          "Test_case": str,
          "Test_case_description": str,
          "Test Entry Point GUID": str,
          "Returned Status Code": str,
          "subtests": [
            {
              "sub_Test_Number": str,
              "sub_Test_Description": str,
              "sub_Test_GUID": str,
              "sub_test_result": str,  # e.g. PASS/FAIL/ABORTED/SKIPPED/WARNING
              "sub_Test_Path": str
            },
            ...
          ],
          "test_case_summary": {
            "total_passed": int,
            "total_failed": int,
            "total_aborted": int,
            "total_skipped": int,
            "total_warnings": int
          }
        },
        ...
      ],
      "suite_summary": {
        "total_passed": int,
        "total_failed": int,
        "total_aborted": int,
        "total_skipped": int,
        "total_warnings": int
      }
    }
    """
    file_encoding = detect_file_encoding(input_file)
    
    results = []
    test_entry = None
    sub_test_number = 0
    capture_description = False

    suite_summary = {
        "total_passed": 0,
        "total_failed": 0,
        "total_aborted": 0,
        "total_skipped": 0,
        "total_warnings": 0
    }

    with open(input_file, "r", encoding=file_encoding, errors="ignore") as file:
        lines = file.readlines()

        for i, line in enumerate(lines):
            line = line.strip()

            # Detect new test entry
            if "BBR ACS" in line:
                if test_entry:
                    results.append(test_entry)
                test_entry = {
                    "Test_suite": "",
                    "Sub_test_suite": "",
                    "Test_case": "",
                    "Test_case_description": "",
                    "Test Entry Point GUID": "",
                    "Returned Status Code": "",
                    "subtests": [],
                    "test_case_summary": {
                        "total_passed": 0,
                        "total_failed": 0,
                        "total_aborted": 0,
                        "total_skipped": 0,
                        "total_warnings": 0
                    }
                }
                # Next line for the Test_case name
                if i + 1 < len(lines):
                    test_entry["Test_case"] = lines[i + 1].strip()
                sub_test_number = 0

                # Identify suite & sub-suite
                t_suite, sub_suite = find_test_suite_and_subsuite(test_entry["Test_case"])
                test_entry["Test_suite"] = t_suite if t_suite else "Unknown"
                test_entry["Sub_test_suite"] = sub_suite if sub_suite else "Unknown"

            # Start capturing description after "Test Configuration #0"
            if "Test Configuration #0" in line:
                capture_description = True
                continue

            if capture_description and line and not re.match(r'-+', line):
                test_entry["Test_case_description"] = line
                capture_description = False

            if "Test Entry Point GUID" in line:
                test_entry["Test Entry Point GUID"] = line.split(':', 1)[1].strip()

            if "Returned Status Code" in line:
                test_entry["Returned Status Code"] = line.split(':', 1)[1].strip()

            # Sub-test lines (PASS/FAIL/WARNING/etc.)
            if re.search(r'--\s*(PASS|FAIL|FAILURE|WARNING|NOT SUPPORTED)', line, re.IGNORECASE):
                parts = line.rsplit(' -- ', 1)
                test_desc = parts[0]
                result_str = parts[1]

                test_desc = clean_test_description(test_desc)

                if "PASS" in result_str.upper():
                    test_entry["test_case_summary"]["total_passed"] += 1
                    suite_summary["total_passed"] += 1
                    final_status = "PASS"
                elif "FAIL" in result_str.upper():
                    test_entry["test_case_summary"]["total_failed"] += 1
                    suite_summary["total_failed"] += 1
                    final_status = "FAIL"
                elif "ABORTED" in result_str.upper():
                    test_entry["test_case_summary"]["total_aborted"] += 1
                    suite_summary["total_aborted"] += 1
                    final_status = "ABORTED"
                elif "SKIPPED" in result_str.upper() or "NOT SUPPORTED" in result_str.upper():
                    test_entry["test_case_summary"]["total_skipped"] += 1
                    suite_summary["total_skipped"] += 1
                    final_status = "SKIPPED"
                elif "WARNING" in result_str.upper():
                    test_entry["test_case_summary"]["total_warnings"] += 1
                    suite_summary["total_warnings"] += 1
                    final_status = "WARNING"
                else:
                    final_status = result_str.strip()

                # Next lines for test GUID & file path
                test_guid = lines[i + 1].strip() if i + 1 < len(lines) else ""
                file_path = lines[i + 2].strip() if i + 2 < len(lines) else ""

                sub_test_number += 1
                sub_test = {
                    "sub_Test_Number": str(sub_test_number),
                    "sub_Test_Description": test_desc,
                    "sub_Test_GUID": test_guid,
                    "sub_test_result": final_status,
                    "sub_Test_Path": file_path
                }
                test_entry["subtests"].append(sub_test)

        # Add the last test_entry
        if test_entry:
            results.append(test_entry)

    return {
        "test_results": results,
        "suite_summary": suite_summary
    }

#
# NEW FUNCTION: Convert dictionary to JUnit-style XML
#
def dict_to_junit_xml(data_dict):
    """
    Convert the parsed SCT log data (from parse_sct_log) into JUnit XML format.

    - Each item in data_dict["test_results"] is treated as a <testsuite>.
    - Each subtest is treated as a <testcase> within that <testsuite>.
    - We map:
        FAIL -> <failure>
        ABORTED -> <error>
        SKIPPED -> <skipped>
        WARNING -> written to <system-out>
        PASS -> no child tags (means success)
    """
    # Create the <testsuites> root
    root = ET.Element("testsuites")

    # For each "test_result" we create one <testsuite>
    for test_item in data_dict["test_results"]:
        testsuite_elem = ET.SubElement(root, "testsuite")

        # We can combine some identifying info in the 'name'
        suite_name = f"{test_item['Test_suite']} :: {test_item['Test_case']}"
        testsuite_elem.set("name", suite_name)

        # Count how many subtests
        total_subtests = len(test_item["subtests"])
        testsuite_elem.set("tests", str(total_subtests))

        # JUnit standard attributes:
        # - failures, errors, skipped
        # NOTE: "ABORTED" -> "errors"
        summary = test_item["test_case_summary"]
        testsuite_elem.set("failures", str(summary["total_failed"]))
        testsuite_elem.set("errors", str(summary["total_aborted"]))  # treat aborted as "errors"
        testsuite_elem.set("skipped", str(summary["total_skipped"]))

        # If you want, you can store "total_warnings" in a custom attribute or ignore it.
        # There's no official place for warnings. We'll just omit or store as custom:
        # testsuite_elem.set("warnings", str(summary["total_warnings"]))

        # Optional: Add <properties> for additional metadata
        props_elem = ET.SubElement(testsuite_elem, "properties")

        # Sub_test_suite
        prop_sts = ET.SubElement(props_elem, "property")
        prop_sts.set("name", "Sub_test_suite")
        prop_sts.set("value", test_item["Sub_test_suite"])

        # Test_case_description
        prop_desc = ET.SubElement(props_elem, "property")
        prop_desc.set("name", "Test_case_description")
        prop_desc.set("value", test_item["Test_case_description"])

        # Test Entry Point GUID
        prop_guid = ET.SubElement(props_elem, "property")
        prop_guid.set("name", "Test Entry Point GUID")
        prop_guid.set("value", test_item["Test Entry Point GUID"])

        # Returned Status Code
        prop_rsc = ET.SubElement(props_elem, "property")
        prop_rsc.set("name", "Returned Status Code")
        prop_rsc.set("value", test_item["Returned Status Code"])

        # Now create <testcase> for each subtest
        for sub in test_item["subtests"]:
            testcase_elem = ET.SubElement(testsuite_elem, "testcase")
            # For "classname", we can use the main test_suite or sub_test_suite
            testcase_elem.set("classname", test_item["Test_suite"])
            # The "name" can be the sub_test_description (with sub_Test_Number for clarity)
            testcase_elem.set("name", f"[{sub['sub_Test_Number']}] {sub['sub_Test_Description']}")

            # If we had time info, we'd set testcase_elem.set("time", "0.0") but we don't.
            
            # Figure out pass/fail/abort/etc.
            result_status = sub["sub_test_result"].upper()
            
            if result_status == "FAIL":
                failure_elem = ET.SubElement(testcase_elem, "failure")
                failure_elem.set("message", "Test Failed")
                failure_elem.set("type", "AssertionError")
                # We could embed some detail:
                failure_elem.text = (
                    f"GUID: {sub['sub_Test_GUID']}\n"
                    f"File: {sub['sub_Test_Path']}"
                )
            elif result_status == "ABORTED":
                error_elem = ET.SubElement(testcase_elem, "error")
                error_elem.set("message", "Test Aborted")
                error_elem.set("type", "AbortedTest")
                error_elem.text = (
                    f"GUID: {sub['sub_Test_GUID']}\n"
                    f"File: {sub['sub_Test_Path']}"
                )
            elif result_status == "SKIPPED":
                skipped_elem = ET.SubElement(testcase_elem, "skipped")
                skipped_elem.set("message", "Test Skipped or Not Supported")
            elif result_status == "WARNING":
                # There's no native <warning> in JUnit, so let's log it in <system-out>
                system_out = ET.SubElement(testcase_elem, "system-out")
                system_out.text = (
                    "WARNING:\n"
                    f"GUID: {sub['sub_Test_GUID']}\n"
                    f"File: {sub['sub_Test_Path']}"
                )
            elif result_status == "PASS":
                # No child elements => test passed
                pass
            else:
                # Some unknown or custom result, let's treat it as <system-out> note
                system_out = ET.SubElement(testcase_elem, "system-out")
                system_out.text = (
                    f"Unknown result: {sub['sub_test_result']}\n"
                    f"GUID: {sub['sub_Test_GUID']}\n"
                    f"File: {sub['sub_Test_Path']}"
                )

    # Convert the ElementTree to a string with XML declaration
    xml_bytes = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes


def main():
    parser = argparse.ArgumentParser(description="Parse an SCT Log file and save results as JUnit XML.")
    parser.add_argument("input_file", help="Input SCT Log file")
    parser.add_argument("output_file", help="Output JUnit XML file")

    args = parser.parse_args()

    # 1) Parse the SCT log (same as your original code)
    data_dict = parse_sct_log(args.input_file)

    # 2) Convert to JUnit XML
    junit_xml_output = dict_to_junit_xml(data_dict)

    # 3) Write JUnit XML to output
    with open(args.output_file, 'wb') as xml_file:
        xml_file.write(junit_xml_output)

    print(f"SCT log parsed successfully. JUnit XML saved to {args.output_file}")


if __name__ == "__main__":
    main()

