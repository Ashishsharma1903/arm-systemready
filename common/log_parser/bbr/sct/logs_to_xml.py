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
import chardet
import xml.etree.ElementTree as ET

# JSON mapping of Test Suites, Sub Test Suites, and Test Cases
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
    Parses the SCT log and returns a dictionary with the same structure
    as the JSON version: { "test_results": [...], "suite_summary": {...} }
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

            # Detect the start of the Test_case after "BBR ACS"
            if "BBR ACS" in line:
                if test_entry:
                    # Add the previous test entry to results
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

                # Identify the test suite & sub suite
                t_suite, sub_suite = find_test_suite_and_subsuite(test_entry["Test_case"])
                test_entry["Test_suite"] = t_suite if t_suite else "Unknown"
                test_entry["Sub_test_suite"] = sub_suite if sub_suite else "Unknown"

            # Start capturing the description after "Test Configuration #0"
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

            # Capture sub-test lines
            if re.search(r'--\s*(PASS|FAIL|FAILURE|WARNING|NOT SUPPORTED)', line, re.IGNORECASE):
                parts = line.rsplit(' -- ', 1)
                test_desc = parts[0]
                result = parts[1]

                test_desc = clean_test_description(test_desc)
                # Update summary counters
                if "PASS" in result.upper():
                    test_entry["test_case_summary"]["total_passed"] += 1
                    suite_summary["total_passed"] += 1
                    res_str = "PASS"
                elif "FAIL" in result.upper():
                    test_entry["test_case_summary"]["total_failed"] += 1
                    suite_summary["total_failed"] += 1
                    res_str = "FAIL"
                elif "ABORTED" in result.upper():
                    test_entry["test_case_summary"]["total_aborted"] += 1
                    suite_summary["total_aborted"] += 1
                    res_str = "ABORTED"
                elif "SKIPPED" in result.upper() or "NOT SUPPORTED" in result.upper():
                    test_entry["test_case_summary"]["total_skipped"] += 1
                    suite_summary["total_skipped"] += 1
                    res_str = "SKIPPED"
                elif "WARNING" in result.upper():
                    test_entry["test_case_summary"]["total_warnings"] += 1
                    suite_summary["total_warnings"] += 1
                    res_str = "WARNING"
                else:
                    res_str = result.strip()

                # Next lines for test GUID & file path
                test_guid = lines[i + 1].strip() if i + 1 < len(lines) else ""
                file_path = lines[i + 2].strip() if i + 2 < len(lines) else ""

                sub_test_number += 1
                sub_test = {
                    "sub_Test_Number": str(sub_test_number),
                    "sub_Test_Description": test_desc,
                    "sub_Test_GUID": test_guid,
                    "sub_test_result": res_str,
                    "sub_Test_Path": file_path
                }
                test_entry["subtests"].append(sub_test)

        # Add the last test entry
        if test_entry:
            results.append(test_entry)

    return {
        "test_results": results,
        "suite_summary": suite_summary
    }

def dict_to_xml(data_dict):
    """
    Converts the parsed SCT dictionary to an XML string.
    """
    root = ET.Element("sct_result")

    # test_results
    test_results_elem = ET.SubElement(root, "test_results")
    for test_item in data_dict["test_results"]:
        test_elem = ET.SubElement(test_results_elem, "test")

        # Basic fields
        ET.SubElement(test_elem, "Test_suite").text = test_item.get("Test_suite", "")
        ET.SubElement(test_elem, "Sub_test_suite").text = test_item.get("Sub_test_suite", "")
        ET.SubElement(test_elem, "Test_case").text = test_item.get("Test_case", "")
        ET.SubElement(test_elem, "Test_case_description").text = test_item.get("Test_case_description", "")
        ET.SubElement(test_elem, "Test_Entry_Point_GUID").text = test_item.get("Test Entry Point GUID", "")
        ET.SubElement(test_elem, "Returned_Status_Code").text = test_item.get("Returned Status Code", "")

        # subtests
        subtests_elem = ET.SubElement(test_elem, "subtests")
        for sub in test_item["subtests"]:
            subtest_elem = ET.SubElement(subtests_elem, "subtest")
            ET.SubElement(subtest_elem, "sub_Test_Number").text = sub.get("sub_Test_Number", "")
            ET.SubElement(subtest_elem, "sub_Test_Description").text = sub.get("sub_Test_Description", "")
            ET.SubElement(subtest_elem, "sub_Test_GUID").text = sub.get("sub_Test_GUID", "")
            ET.SubElement(subtest_elem, "sub_test_result").text = sub.get("sub_test_result", "")
            ET.SubElement(subtest_elem, "sub_Test_Path").text = sub.get("sub_Test_Path", "")

        # test_case_summary
        summary_elem = ET.SubElement(test_elem, "test_case_summary")
        tcs = test_item["test_case_summary"]
        for key in ["total_passed","total_failed","total_aborted","total_skipped","total_warnings"]:
            ET.SubElement(summary_elem, key).text = str(tcs[key])

    # suite_summary
    suite_summary_elem = ET.SubElement(root, "suite_summary")
    ss = data_dict["suite_summary"]
    for key in ["total_passed","total_failed","total_aborted","total_skipped","total_warnings"]:
        ET.SubElement(suite_summary_elem, key).text = str(ss[key])

    # Convert to XML string (with declaration)
    xml_bytes = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes

def main():
    parser = argparse.ArgumentParser(description="Parse an SCT Log file and save results to an XML file.")
    parser.add_argument("input_file", help="Input SCT Log file")
    parser.add_argument("output_file", help="Output XML file")

    args = parser.parse_args()

    data_dict = parse_sct_log(args.input_file)
    xml_output = dict_to_xml(data_dict)

    with open(args.output_file, 'wb') as xml_file:
        xml_file.write(xml_output)

    print(f"SCT log parsed successfully. XML output saved to {args.output_file}")

if __name__ == "__main__":
    main()
