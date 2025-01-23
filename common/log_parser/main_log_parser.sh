#!/bin/bash
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

# Determine the base directory of the script
BASE_DIR=$(dirname "$(realpath "$0")")

# Determine paths
SCRIPTS_PATH="$BASE_DIR"

# Check for required arguments
if [ $# -lt 1 ]; then
    echo "Usage: $0 <acs_results_directory> [acs_config.txt] [system_config.txt] [waiver.json]"
    exit 1
fi

# Add the YOCTO_FLAG variable
YOCTO_FLAG="/mnt/yocto_image.flag"

# Check if the YOCTO_FLAG exists
if [ -f "$YOCTO_FLAG" ]; then
    YOCTO_FLAG_PRESENT=1
else
    YOCTO_FLAG_PRESENT=0
fi

LOGS_PATH=$1
ACS_CONFIG_PATH=$2
SYSTEM_CONFIG_PATH=$3
WAIVER_JSON=$4

if [ $YOCTO_FLAG_PRESENT -eq 1 ]; then 
  test_category="/usr/bin/log_parser/test_categoryDT.json"
else
  test_category="/usr/bin/log_parser/test_category.json"
fi

# Check if ACS_CONFIG_PATH is provided
if [ -z "$ACS_CONFIG_PATH" ]; then
    echo "WARNING: ACS information will be affected on summary page as acs_config.txt is not provided"
    echo ""
    echo "If you want ACS information, please use this format: $0 <acs_results_directory> [acs_config.txt] [system_config.txt] [waiver.json]"
    echo ""
fi

# Check if SYSTEM_CONFIG_PATH is provided
if [ -z "$SYSTEM_CONFIG_PATH" ]; then
    echo "WARNING: System information may be incomplete as system_config.txt is not provided"
    echo ""
    echo "If you want complete system information, please use this format: $0 <acs_results_directory> [acs_config.txt] [system_config.txt] [waiver.json]"
    echo ""
fi

# Initialize waiver-related variables
WAIVERS_APPLIED=0

# Check if waiver.json and test_category.json are provided
if [ -n "$WAIVER_JSON" ];  then
    if [ -f "$WAIVER_JSON" ]; then
        WAIVERS_APPLIED=1
        echo "Waivers will be applied using:"
        echo "  Waiver File        : $WAIVER_JSON"
#        echo "  Output JSON File   : $test_category"
        echo ""
    else
        echo "WARNING: waiver.json ('$WAIVER_JSON') must be provided to apply waivers."
        echo "Waivers will not be applied."
        echo ""
        WAIVER_JSON=""
    fi
else
    echo "WARNING: waiver.json not provided. Waivers will not be applied."
    echo ""
    WAIVER_JSON=""
fi


# Function to check if a file exists
check_file() {
    if [ ! -f "$1" ]; then
        echo "WARNING: Log file '$(basename "$1")' is not present at the given directory."
        return 1
    fi
    return 0
}

# Function to apply waivers
apply_waivers() {
    local suite_name="$1"
    local json_file="$2"

    if [ "$WAIVERS_APPLIED" -eq 1 ]; then
        python3 "$SCRIPTS_PATH/apply_waivers.py" "$suite_name" "$json_file" "$WAIVER_JSON" "$test_category" --quiet
 #   else
 #       echo "Waivers not applied for suite '$suite_name' as waiver files are not provided."
    fi
}

# Create directories for JSONs and HTMLs inside acs_summary
ACS_SUMMARY_DIR="$LOGS_PATH/acs_summary"
JSONS_DIR="$ACS_SUMMARY_DIR/acs_jsons"
HTMLS_DIR="$ACS_SUMMARY_DIR/html_detailed_summaries"
mkdir -p "$JSONS_DIR"
mkdir -p "$HTMLS_DIR"

# Initialize processing flags
BSA_PROCESSED=0
SBSA_PROCESSED=0
FWTS_PROCESSED=0
SCT_PROCESSED=0
standalone_PROCESSED=0
BBSR_FWTS_PROCESSED=0
BBSR_SCT_PROCESSED=0
MANUAL_TESTS_PROCESSED=0
CAPSULE_PROCESSED=0

###############################################################################
#                      BSA UEFI and Kernel Log Parsing                        #
###############################################################################
BSA_LOG="$LOGS_PATH/uefi/BsaResults.log"
BSA_KERNEL_LOG="$LOGS_PATH/linux_acs/bsa_acs_app/BsaResultsKernel.log"
if [ ! -f "$BSA_KERNEL_LOG" ]; then
    BSA_KERNEL_LOG="$LOGS_PATH/linux/BsaResultsKernel.log"
fi

BSA_JSON="$JSONS_DIR/BSA.json"
BSA_XML="$JSONS_DIR/BSA.xml"

BSA_LOGS=()

if check_file "$BSA_LOG"; then
    BSA_LOGS+=("$BSA_LOG")
fi

if check_file "$BSA_KERNEL_LOG"; then
    BSA_LOGS+=("$BSA_KERNEL_LOG")
fi

if [ ${#BSA_LOGS[@]} -gt 0 ]; then
    BSA_PROCESSED=1
    python3 "$SCRIPTS_PATH/bsa/logs_to_json.py" "${BSA_LOGS[@]}" "$BSA_JSON"
    # Apply waivers
    apply_waivers "BSA" "$BSA_JSON"
    python3 "$SCRIPTS_PATH/bsa/json_to_html.py" "$BSA_JSON" "$HTMLS_DIR/BSA_detailed.html" "$HTMLS_DIR/BSA_summary.html"
    if [ -f "$SCRIPTS_PATH/bsa/logs_to_xml.py" ]; then
        python3 "$SCRIPTS_PATH/bsa/logs_to_xml.py" "${BSA_LOGS[@]}" "$BSA_XML"
        echo "BSA XML output created at: $BSA_XML"
    else
        echo "WARNING: logs_to_xml.py not found for BSA"
    fi
else
    echo "WARNING: Skipping BSA log parsing as the log files are missing."
    echo ""
fi

###############################################################################
#                      SBSA UEFI and Kernel Log Parsing                       #
###############################################################################
SBSA_LOG="$LOGS_PATH/uefi/SbsaResults.log"
SBSA_KERNEL_LOG="$LOGS_PATH/linux/SbsaResultsKernel.log"

SBSA_JSON="$JSONS_DIR/SBSA.json"
SBSA_XML="$JSONS_DIR/SBSA.xml"

SBSA_LOGS=()

if [ $YOCTO_FLAG_PRESENT -eq 0 ]; then
    if check_file "$SBSA_LOG"; then
        SBSA_LOGS+=("$SBSA_LOG")
    fi

    if check_file "$SBSA_KERNEL_LOG"; then
        SBSA_LOGS+=("$SBSA_KERNEL_LOG")
    fi

    if [ ${#SBSA_LOGS[@]} -gt 0 ]; then
        SBSA_PROCESSED=1
        python3 "$SCRIPTS_PATH/bsa/logs_to_json.py" "${SBSA_LOGS[@]}" "$SBSA_JSON"
        # Apply waivers
        apply_waivers "SBSA" "$SBSA_JSON"
        python3 "$SCRIPTS_PATH/bsa/json_to_html.py" "$SBSA_JSON" "$HTMLS_DIR/SBSA_detailed.html" "$HTMLS_DIR/SBSA_summary.html"

        if [ -f "$SCRIPTS_PATH/bsa/logs_to_xml.py" ]; then
            python3 "$SCRIPTS_PATH/bsa/logs_to_xml.py" "${SBSA_LOGS[@]}" "$SBSA_XML"
            echo "SBSA XML output created at: $SBSA_XML"
        else
            echo "WARNING: logs_to_xml.py not found for SBSA"
        fi

    else
        echo "WARNING: Skipping SBSA log parsing as the log files are missing."
        echo ""
    fi
fi

###############################################################################
#                        FWTS UEFI Log Parsing                                #
###############################################################################
FWTS_LOG="$LOGS_PATH/fwts/FWTSResults.log"
FWTS_JSON="$JSONS_DIR/FWTSResults.json"
FWTS_XML="$JSONS_DIR/FWTSResults.xml"

if check_file "$FWTS_LOG"; then
    FWTS_PROCESSED=1
    python3 "$SCRIPTS_PATH/bbr/fwts/logs_to_json.py" "$FWTS_LOG" "$FWTS_JSON"
    apply_waivers "FWTS" "$FWTS_JSON"
    python3 "$SCRIPTS_PATH/bbr/fwts/json_to_html.py" "$FWTS_JSON" "$HTMLS_DIR/fwts_detailed.html" "$HTMLS_DIR/fwts_summary.html"

    if [ -f "$SCRIPTS_PATH/bbr/fwts/logs_to_xml.py" ]; then
        python3 "$SCRIPTS_PATH/bbr/fwts/logs_to_xml.py" "$FWTS_LOG" "$FWTS_XML"
        echo "FWTS XML output created at: $FWTS_XML"
    else
        echo "WARNING: logs_to_xml.py not found for FWTS"
    fi
else
    echo "WARNING: Skipping FWTS log parsing as the log file is missing."
    echo ""
fi

###############################################################################
#                           SCT Log Parsing                                   #
###############################################################################
SCT_LOG="$LOGS_PATH/sct_results/Overall/Summary.log"
SCT_JSON="$JSONS_DIR/SCT.json"
SCT_XML="$JSONS_DIR/SCT.xml"

if check_file "$SCT_LOG"; then
    SCT_PROCESSED=1
    python3 "$SCRIPTS_PATH/bbr/sct/logs_to_json.py" "$SCT_LOG" "$SCT_JSON"
    apply_waivers "SCT" "$SCT_JSON"
    python3 "$SCRIPTS_PATH/bbr/sct/json_to_html.py" "$SCT_JSON" "$HTMLS_DIR/SCT_detailed.html" "$HTMLS_DIR/SCT_summary.html"
    if [ -f "$SCRIPTS_PATH/bbr/sct/logs_to_xml.py" ]; then
        python3 "$SCRIPTS_PATH/bbr/sct/logs_to_xml.py" "$SCT_LOG" "$SCT_XML"
        echo "SCT XML output created at: $SCT_XML"
    else
        echo "WARNING: logs_to_xml.py not found for SCT"
    fi
else
    echo "WARNING: Skipping SCT log parsing as the log file is missing."
    echo ""
fi

###############################################################################
#                        BBSR FWTS Log Parsing                                #
###############################################################################
BBSR_FWTS_LOG="$LOGS_PATH/BBSR/fwts/FWTSResults.log"
BBSR_FWTS_JSON="$JSONS_DIR/bbsr-fwts.json"
BBSR_FWTS_XML="$JSONS_DIR/bbsr-fwts.xml"

if check_file "$BBSR_FWTS_LOG"; then
    BBSR_FWTS_PROCESSED=1
    python3 "$SCRIPTS_PATH/bbr/fwts/logs_to_json.py" "$BBSR_FWTS_LOG" "$BBSR_FWTS_JSON"
    apply_waivers "BBSR-FWTS" "$BBSR_FWTS_JSON"
    python3 "$SCRIPTS_PATH/bbr/fwts/json_to_html.py" "$BBSR_FWTS_JSON" "${HTMLS_DIR}/bbsr-fwts_detailed.html" "${HTMLS_DIR}/bbsr-fwts_summary.html"
    if [ -f "$SCRIPTS_PATH/bbr/fwts/logs_to_xml.py" ]; then
        python3 "$SCRIPTS_PATH/bbr/fwts/logs_to_xml.py" "$BBSR_FWTS_LOG" "$BBSR_FWTS_XML"
        echo "BBSR FWTS XML output created at: $BBSR_FWTS_XML"
    else
        echo "WARNING: logs_to_xml.py not found for BBSR FWTS"
    fi
else
    echo "WARNING: Skipping BBSR FWTS log parsing as the log file is missing."
    echo ""
fi

###############################################################################
#                         BBSR SCT Log Parsing                                #
###############################################################################
BBSR_SCT_LOG="$LOGS_PATH/BBSR/sct_results/Overall/Summary.log"
BBSR_SCT_JSON="$JSONS_DIR/bbsr-sct.json"
BBSR_SCT_XML="$JSONS_DIR/bbsr-sct.xml"

if check_file "$BBSR_SCT_LOG"; then
    BBSR_SCT_PROCESSED=1
    python3 "$SCRIPTS_PATH/bbr/sct/logs_to_json.py" "$BBSR_SCT_LOG" "$BBSR_SCT_JSON"
    apply_waivers "BBSR-SCT" "$BBSR_SCT_JSON"
    python3 "$SCRIPTS_PATH/bbr/sct/json_to_html.py" "$BBSR_SCT_JSON" "${HTMLS_DIR}/bbsr-sct_detailed.html" "${HTMLS_DIR}/bbsr-sct_summary.html"
    if [ -f "$SCRIPTS_PATH/bbr/sct/logs_to_xml.py" ]; then
        python3 "$SCRIPTS_PATH/bbr/sct/logs_to_xml.py" "$BBSR_SCT_LOG" "$BBSR_SCT_XML"
        echo "BBSR SCT XML output created at: $BBSR_SCT_XML"
    else
        echo "WARNING: logs_to_xml.py not found for BBSR SCT"
    fi
else
    echo "WARNING: Skipping BBSR SCT log parsing as the log file is missing."
    echo ""
fi

###############################################################################
#                     standalone Logs Parsing (Yocto)                         #
###############################################################################
if [ $YOCTO_FLAG_PRESENT -eq 1 ]; then
    LINUX_TOOLS_LOGS_PATH="$LOGS_PATH/linux_tools"

    DT_KSELFTEST_LOG="$LINUX_TOOLS_LOGS_PATH/dt_kselftest.log"
    DT_VALIDATE_LOG="$LINUX_TOOLS_LOGS_PATH/dt-validate.log"
    ETHTOOL_TEST_LOG="$LINUX_TOOLS_LOGS_PATH/ethtool-test.log"
    READ_WRITE_CHECK_LOG="$LINUX_TOOLS_LOGS_PATH/read_write_check_blk_devices.log"

    DT_KSELFTEST_JSON="$JSONS_DIR/dt_kselftest.json"
    DT_KSELFTEST_XML="$JSONS_DIR/dt_kselftest.xml"
    DT_VALIDATE_JSON="$JSONS_DIR/dt_validate.json"
    DT_VALIDATE_XML="$JSONS_DIR/dt_validate.xml"
    ETHTOOL_TEST_JSON="$JSONS_DIR/ethtool_test.json"
    ETHTOOL_TEST_XML="$JSONS_DIR/ethtool_test.xml"
    READ_WRITE_CHECK_JSON="$JSONS_DIR/read_write_check_blk_devices.json"
    READ_WRITE_CHECK_XML="$JSONS_DIR/read_write_check_blk_devices.xml"

    standalone_JSONS=()

    # dt_kselftest
    if check_file "$DT_KSELFTEST_LOG"; then
        python3 "$SCRIPTS_PATH/standalone/logs_to_json.py" "$DT_KSELFTEST_LOG" "$DT_KSELFTEST_JSON"
        standalone_JSONS+=("$DT_KSELFTEST_JSON")
        apply_waivers "standalone" "$DT_KSELFTEST_JSON"

        # logs_to_xml
        if [ -f "$SCRIPTS_PATH/standalone/logs_to_xml.py" ]; then
            python3 "$SCRIPTS_PATH/standalone/logs_to_xml.py" "$DT_KSELFTEST_LOG" "$DT_KSELFTEST_XML"
            echo "dt_kselftest XML output: $DT_KSELFTEST_XML"
        fi
    fi

    # dt-validate
    if check_file "$DT_VALIDATE_LOG"; then
        python3 "$SCRIPTS_PATH/standalone/logs_to_json.py" "$DT_VALIDATE_LOG" "$DT_VALIDATE_JSON"
        standalone_JSONS+=("$DT_VALIDATE_JSON")
        apply_waivers "standalone" "$DT_VALIDATE_JSON"

        # logs_to_xml
        if [ -f "$SCRIPTS_PATH/standalone/logs_to_xml.py" ]; then
            python3 "$SCRIPTS_PATH/standalone/logs_to_xml.py" "$DT_VALIDATE_LOG" "$DT_VALIDATE_XML"
            echo "dt_validate XML output: $DT_VALIDATE_XML"
        fi
    fi

    # ethtool-test
    if check_file "$ETHTOOL_TEST_LOG"; then
        python3 "$SCRIPTS_PATH/standalone/logs_to_json.py" "$ETHTOOL_TEST_LOG" "$ETHTOOL_TEST_JSON"
        standalone_JSONS+=("$ETHTOOL_TEST_JSON")
        apply_waivers "standalone" "$ETHTOOL_TEST_JSON"

        # logs_to_xml
        if [ -f "$SCRIPTS_PATH/standalone/logs_to_xml.py" ]; then
            python3 "$SCRIPTS_PATH/standalone/logs_to_xml.py" "$ETHTOOL_TEST_LOG" "$ETHTOOL_TEST_XML"
            echo "ethtool_test XML output: $ETHTOOL_TEST_XML"
        fi
    fi

    # read_write_check_blk_devices
    if check_file "$READ_WRITE_CHECK_LOG"; then
        python3 "$SCRIPTS_PATH/standalone/logs_to_json.py" "$READ_WRITE_CHECK_LOG" "$READ_WRITE_CHECK_JSON"
        standalone_JSONS+=("$READ_WRITE_CHECK_JSON")
        apply_waivers "standalone" "$READ_WRITE_CHECK_JSON"

        # logs_to_xml
        if [ -f "$SCRIPTS_PATH/standalone/logs_to_xml.py" ]; then
            python3 "$SCRIPTS_PATH/standalone/logs_to_xml.py" "$READ_WRITE_CHECK_LOG" "$READ_WRITE_CHECK_XML"
            echo "read_write_check_blk_devices XML: $READ_WRITE_CHECK_XML"
        fi
    fi

    # Generate combined standalone detailed and summary HTML reports
    standalone_DETAILED_HTML="$HTMLS_DIR/standalone_detailed.html"
    standalone_SUMMARY_HTML="$HTMLS_DIR/standalone_summary.html"

    if [ ${#standalone_JSONS[@]} -gt 0 ]; then
        standalone_PROCESSED=1
        python3 "$SCRIPTS_PATH/standalone/json_to_html.py" "${standalone_JSONS[@]}" "$standalone_DETAILED_HTML" "$standalone_SUMMARY_HTML" --include-drop-down
    fi
fi

###############################################################################
#           Manual Tests Logs Parsing (ethtool_test under os-logs)           #
###############################################################################
OS_LOGS_PATH="/mnt/acs_results_template/os-logs"

MANUAL_JSONS_DIR="$JSONS_DIR"
mkdir -p "$MANUAL_JSONS_DIR"

MANUAL_JSONS=()
BOOT_SOURCES_PATHS=()

if [ -d "$OS_LOGS_PATH" ]; then
    for OS_DIR in "$OS_LOGS_PATH"/linux*; do
        if [ -d "$OS_DIR" ]; then
            OS_NAME=$(basename "$OS_DIR")
            ETH_TOOL_LOG="$OS_DIR/ethtool_test.log"
            BOOT_SOURCES_LOG="$OS_DIR/boot_sources.log"

            if [ -f "$ETH_TOOL_LOG" ]; then
                OUTPUT_JSON="$MANUAL_JSONS_DIR/ethtool_test_${OS_NAME}.json"
                OUTPUT_XML="$MANUAL_JSONS_DIR/ethtool_test_${OS_NAME}.xml"  # <----- ADDED

                # logs_to_json (original)
                python3 "$SCRIPTS_PATH/manual_tests/logs_to_json.py" "$ETH_TOOL_LOG" "$OUTPUT_JSON" "$OS_NAME"
                apply_waivers "Manual Tests" "$OUTPUT_JSON"
                MANUAL_JSONS+=("$OUTPUT_JSON")
                MANUAL_TESTS_PROCESSED=1

                # logs_to_xml (added)
                if [ -f "$SCRIPTS_PATH/manual_tests/logs_to_xml.py" ]; then
                    python3 "$SCRIPTS_PATH/manual_tests/logs_to_xml.py" "$ETH_TOOL_LOG" "$OUTPUT_XML" "$OS_NAME"
                    echo "Manual ethtool_test XML output: $OUTPUT_XML"
                fi

                if [ -f "$BOOT_SOURCES_LOG" ]; then
                    BOOT_SOURCES_PATHS+=("$BOOT_SOURCES_LOG")
                else
                    BOOT_SOURCES_PATHS+=("Unknown")
                fi
            else
                echo "WARNING: ethtool_test.log not found in $OS_DIR"
            fi
        fi
    done
else
    echo "WARNING: os-logs directory not found at $OS_LOGS_PATH"
fi

# Generate combined OS tests detailed and summary HTML reports
if [ ${#MANUAL_JSONS[@]} -gt 0 ]; then
    MANUAL_DETAILED_HTML="$HTMLS_DIR/manual_tests_detailed.html"
    MANUAL_SUMMARY_HTML="$HTMLS_DIR/manual_tests_summary.html"
    python3 "$SCRIPTS_PATH/manual_tests/json_to_html.py" "${MANUAL_JSONS[@]}" "$MANUAL_DETAILED_HTML" "$MANUAL_SUMMARY_HTML" --include-drop-down --boot-sources-paths "${BOOT_SOURCES_PATHS[@]}"
fi

###############################################################################
#                   Capsule Update Logs Parsing                               #
###############################################################################
CAPSULE_JSON="$JSONS_DIR/capsule_update.json"
CAPSULE_XML="$JSONS_DIR/capsule_update.xml"  # <----- ADDED

python3 "$SCRIPTS_PATH/capsule_update/logs_to_json.py"

if [ -f "$CAPSULE_JSON" ]; then
    CAPSULE_PROCESSED=1
    apply_waivers "Capsule Update" "$CAPSULE_JSON"
    python3 "$SCRIPTS_PATH/capsule_update/json_to_html.py" "$CAPSULE_JSON" "$HTMLS_DIR/capsule_update_detailed.html" "$HTMLS_DIR/capsule_update_summary.html"
    echo ""

    # ----------------- XML -----------------
    if [ -f "$SCRIPTS_PATH/capsule_update/logs_to_xml.py" ]; then
        python3 "$SCRIPTS_PATH/capsule_update/logs_to_xml.py"
        echo "Capsule XML output might be at: $CAPSULE_XML"
    else
        echo "WARNING: logs_to_xml.py not found for Capsule Update"
    fi
else
    echo "WARNING: Capsule Update JSON file not created. Skipping Capsule Update log parsing."
    echo ""
fi

###############################################################################
#                          Generate ACS Summary                               #
###############################################################################
ACS_SUMMARY_HTML="$HTMLS_DIR/acs_summary.html"

GENERATE_ACS_SUMMARY_CMD="python3 \"$SCRIPTS_PATH/generate_acs_summary.py\""

# Always BSA
GENERATE_ACS_SUMMARY_CMD+=" \"$HTMLS_DIR/BSA_summary.html\""

# SBSA only if processed
if [ $SBSA_PROCESSED -eq 1 ]; then
    GENERATE_ACS_SUMMARY_CMD+=" \"$HTMLS_DIR/SBSA_summary.html\""
else
    GENERATE_ACS_SUMMARY_CMD+=" \"\""
fi

# FWTS always
GENERATE_ACS_SUMMARY_CMD+=" \"$HTMLS_DIR/fwts_summary.html\""

# SCT always
GENERATE_ACS_SUMMARY_CMD+=" \"$HTMLS_DIR/SCT_summary.html\""

# standalone only if processed
if [ $standalone_PROCESSED -eq 1 ]; then
    GENERATE_ACS_SUMMARY_CMD+=" \"$HTMLS_DIR/standalone_summary.html\""
else
    GENERATE_ACS_SUMMARY_CMD+=" \"\""
fi

# OS Tests only if processed
if [ $MANUAL_TESTS_PROCESSED -eq 1 ]; then
    GENERATE_ACS_SUMMARY_CMD+=" \"$HTMLS_DIR/manual_tests_summary.html\""
else
    GENERATE_ACS_SUMMARY_CMD+=" \"\""
fi

# Capsule only if processed
if [ $CAPSULE_PROCESSED -eq 1 ]; then
    GENERATE_ACS_SUMMARY_CMD+=" \"$HTMLS_DIR/capsule_update_summary.html\""
else
    GENERATE_ACS_SUMMARY_CMD+=" \"\""
fi

# Output HTML path
GENERATE_ACS_SUMMARY_CMD+=" \"$ACS_SUMMARY_HTML\""

# Additional arguments
if [ -n "$ACS_CONFIG_PATH" ]; then
    GENERATE_ACS_SUMMARY_CMD+=" --acs_config_path \"$ACS_CONFIG_PATH\""
fi

if [ -n "$SYSTEM_CONFIG_PATH" ]; then
    GENERATE_ACS_SUMMARY_CMD+=" --system_config_path \"$SYSTEM_CONFIG_PATH\""
fi

UEFI_VERSION_LOG="$LOGS_PATH/uefi_dump/uefi_version.log"
DEVICE_TREE_DTS="$LOGS_PATH/linux_tools/device_tree.dts"
if [ ! -f "$UEFI_VERSION_LOG" ]; then
    echo "WARNING: UEFI version log '$(basename "$UEFI_VERSION_LOG")' not found."
    UEFI_VERSION_LOG=""
fi

if [ $YOCTO_FLAG_PRESENT -eq 1 ]; then
    if [ ! -f "$DEVICE_TREE_DTS" ]; then
        echo "WARNING: Device Tree DTS file '$(basename "$DEVICE_TREE_DTS")' not found."
        DEVICE_TREE_DTS=""
    fi
fi

if [ -n "$UEFI_VERSION_LOG" ]; then
    GENERATE_ACS_SUMMARY_CMD+=" --uefi_version_log \"$UEFI_VERSION_LOG\""
fi

if [ -n "$DEVICE_TREE_DTS" ]; then
    GENERATE_ACS_SUMMARY_CMD+=" --device_tree_dts \"$DEVICE_TREE_DTS\""
fi

eval $GENERATE_ACS_SUMMARY_CMD

# Summary Prints
if [ $BSA_PROCESSED -eq 1 ]; then
    echo "BSA UEFI Log              : $BSA_LOG"
    echo "BSA JSON                  : $BSA_JSON"
    echo "BSA Detailed Summary      : $HTMLS_DIR/BSA_detailed.html"
    echo "BSA Summary               : $HTMLS_DIR/BSA_summary.html"
    echo "BSA XML                   : $BSA_XML"
    echo ""
fi

if [ $SBSA_PROCESSED -eq 1 ]; then
    echo "SBSA UEFI Log             : $SBSA_LOG"
    echo "SBSA JSON                 : $SBSA_JSON"
    echo "SBSA Detailed Summary     : $HTMLS_DIR/SBSA_detailed.html"
    echo "SBSA Summary              : $HTMLS_DIR/SBSA_summary.html"
    echo "SBSA XML                  : $SBSA_XML"
    echo ""
fi

if [ $FWTS_PROCESSED -eq 1 ]; then
    echo "FWTS Log                  : $FWTS_LOG"
    echo "FWTS JSON                 : $FWTS_JSON"
    echo "FWTS Detailed Summary     : $HTMLS_DIR/fwts_detailed.html"
    echo "FWTS Summary              : $HTMLS_DIR/fwts_summary.html"
    echo "FWTS XML                  : $FWTS_XML"
    echo ""
fi

if [ $SCT_PROCESSED -eq 1 ]; then
    echo "SCT Log                   : $SCT_LOG"
    echo "SCT JSON                  : $SCT_JSON"
    echo "SCT Detailed Summary      : $HTMLS_DIR/SCT_detailed.html"
    echo "SCT Summary               : $HTMLS_DIR/SCT_summary.html"
    echo "SCT XML                   : $SCT_XML"
    echo ""
fi

if [ $BBSR_FWTS_PROCESSED -eq 1 ]; then
    echo "BBSR FWTS Log              : $BBSR_FWTS_LOG"
    echo "BBSR FWTS JSON             : $BBSR_FWTS_JSON"
    echo "BBSR FWTS Detailed Summary : $HTMLS_DIR/bbsr-fwts_detailed.html"
    echo "BBSR FWTS Summary          : $HTMLS_DIR/bbsr-fwts_summary.html"
    echo "BBSR FWTS XML              : $BBSR_FWTS_XML"
    echo ""
fi

if [ $BBSR_SCT_PROCESSED -eq 1 ]; then
    echo "BBSR SCT Log               : $BBSR_SCT_LOG"
    echo "BBSR SCT JSON              : $BBSR_SCT_JSON"
    echo "BBSR SCT Detailed Summary  : $HTMLS_DIR/bbsr-sct_detailed.html"
    echo "BBSR SCT Summary           : $HTMLS_DIR/bbsr-sct_summary.html"
    echo "BBSR SCT XML               : $BBSR_SCT_XML"
    echo ""
fi

if [ $standalone_PROCESSED -eq 1 ]; then
    echo "standalone Logs Processed"
    echo "standalone Detailed Summary: $standalone_DETAILED_HTML"
    echo "standalone Summary         : $standalone_SUMMARY_HTML"
    echo ""
fi

if [ $MANUAL_TESTS_PROCESSED -eq 1 ]; then
    echo "Manual Tests Logs Processed"
    echo "Manual Tests Detailed Summary : $MANUAL_DETAILED_HTML"
    echo "Manual Tests Summary          : $MANUAL_SUMMARY_HTML"
    echo ""
fi

if [ $CAPSULE_PROCESSED -eq 1 ]; then
    echo "Capsule Update Logs Processed"
    echo "Capsule Update JSON             : $CAPSULE_JSON"
    echo "Capsule Update Detailed Summary : $HTMLS_DIR/capsule_update_detailed.html"
    echo "Capsule Update Summary          : $HTMLS_DIR/capsule_update_summary.html"
    echo "Capsule Update XML (potential)  : $CAPSULE_XML"
    echo ""
fi

ACS_SUMMARY_HTML="$HTMLS_DIR/acs_summary.html"
echo "ACS Summary               : $ACS_SUMMARY_HTML"
echo ""

MERGED_JSON="$JSONS_DIR/merged_results.json"
JSON_FILES=()

# BSA
if [ -f "$BSA_JSON" ]; then
    JSON_FILES+=("$BSA_JSON")
else
    echo "WARNING: $(basename "$BSA_JSON") not found. Skipping this file."
fi

# SBSA
if [ $SBSA_PROCESSED -eq 1 ] && [ -f "$SBSA_JSON" ]; then
    JSON_FILES+=("$SBSA_JSON")
elif [ $SBSA_PROCESSED -eq 1 ]; then
    echo "WARNING: $(basename "$SBSA_JSON") not found. Skipping this file."
fi

# FWTS
if [ -f "$FWTS_JSON" ]; then
    JSON_FILES+=("$FWTS_JSON")
else
    echo "WARNING: $(basename "$FWTS_JSON") not found. Skipping this file."
fi

# SCT
if [ -f "$SCT_JSON" ]; then
    JSON_FILES+=("$SCT_JSON")
else
    echo "WARNING: $(basename "$SCT_JSON") not found. Skipping this file."
fi

# BBSR FWTS
if [ $BBSR_FWTS_PROCESSED -eq 1 ] && [ -f "$BBSR_FWTS_JSON" ]; then
    JSON_FILES+=("$BBSR_FWTS_JSON")
else
    echo "WARNING: $(basename "$BBSR_FWTS_JSON") not found. Skipping this file."
fi

# BBSR SCT
if [ $BBSR_SCT_PROCESSED -eq 1 ] && [ -f "$BBSR_SCT_JSON" ]; then
    JSON_FILES+=("$BBSR_SCT_JSON")
else
    echo "WARNING: $(basename "$BBSR_SCT_JSON") not found. Skipping this file."
fi

# standalone
if [ $standalone_PROCESSED -eq 1 ] && [ ${#standalone_JSONS[@]} -gt 0 ]; then
    JSON_FILES+=("${standalone_JSONS[@]}")
elif [ $standalone_PROCESSED -eq 1 ]; then
    echo "WARNING: No standalone JSON files found. Skipping standalone files."
fi

# manual logs
if [ $MANUAL_TESTS_PROCESSED -eq 1 ] && [ ${#MANUAL_JSONS[@]} -gt 0 ]; then
    JSON_FILES+=("${MANUAL_JSONS[@]}")
elif [ $MANUAL_TESTS_PROCESSED -eq 1 ]; then
    echo "WARNING: No Manual Tests JSON files found. Skipping Manual Tests files."
fi

# capsule
if [ $CAPSULE_PROCESSED -eq 1 ] && [ -f "$CAPSULE_JSON" ]; then
    JSON_FILES+=("$CAPSULE_JSON")
else
    echo "WARNING: $(basename "$CAPSULE_JSON") not found. Skipping this file."
fi

if [ ${#JSON_FILES[@]} -gt 0 ]; then
    python3 "$SCRIPTS_PATH/merge_jsons.py" "$MERGED_JSON" "${JSON_FILES[@]}"
    echo "Merged JSON created at: $MERGED_JSON"
else
    echo "No JSON files to merge."
fi

echo ""
