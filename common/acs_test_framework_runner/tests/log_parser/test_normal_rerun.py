"""A normal rerun must not publish earlier BSA success after its log disappears."""

from functools import cache
import json
from pathlib import Path
import shutil

import pytest

from common.acs_test_framework_runner.qa_evidence import run_command
from test_artifact_validation import validator
from test_end_to_end import CASES, portable_parser


def observe(folder, validator):
    output = folder / "acs_summary"
    merged_path = output / "acs_jsons/merged_results.json"
    merged = json.loads(merged_path.read_text()) if merged_path.is_file() else {}
    summary = merged.get("Suite_Name: acs_info", {}).get("ACS Results Summary", {})
    statuses = [value for key, value in summary.items() if key.endswith("  : BSA_compliance")]
    raw_path = output / "acs_jsons/bsa.json"
    raw = json.loads(raw_path.read_text()) if raw_path.is_file() else {}
    detail = output / "html_detailed_summaries/bsa_detailed.html"
    html_counts = validator._summary_maps(validator._read_html(detail), detail) if detail.is_file() else []
    return {
        "status": statuses, "counts": validator._summary_values(raw.get("suite_summary", {})),
        "html_counts": html_counts, "raw_present": raw_path.is_file(),
        "detail_present": detail.is_file(),
    }


@pytest.fixture(scope="module")
def normal_runs(tmp_path_factory, portable_parser, validator):
    folder = tmp_path_factory.mktemp("normal-stale-results")
    mode = "DT" if Path("/mnt/yocto_image.flag").is_file() else "SR"
    band = "SystemReady Devicetree band" if mode == "DT" else "SystemReady band"
    acs, system = folder / "acs_config.txt", folder / "system_config.txt"
    acs.write_text(f"ACS version: QA\nSRS version: SRS 3.1.1\nBSA version: BSA v1.2\nBand: {band}\n")
    system.write_text("FW source code: QA fixture\n")
    inputs = folder / "reused/acs_results"
    log = inputs / "uefi/BsaResults.log"
    log.parent.mkdir(parents=True)
    log.write_text(CASES[0]["files"]["results/uefi/BsaResults.log"].replace("FAILED", "PASSED"))

    def invoke(source, name):
        process = run_command(["bash", portable_parser / "main_log_parser.sh", source, acs, system, ""],
                              folder, folder, name=name)
        return {"exit": process.returncode, **observe(source, validator)}

    first = invoke(inputs, "first")
    if (inputs / "acs_summary").is_dir():
        shutil.copytree(inputs / "acs_summary", folder / "first-report")
    @cache
    def missing_runs():
        # Only reached after a valid positive control; never alter the host mode flag.
        log.rename(folder / "saved-BsaResults.log")
        rerun = invoke(inputs, "missing-rerun")
        fresh_inputs = folder / "fresh/acs_results"
        fresh_inputs.mkdir(parents=True)
        fresh = invoke(fresh_inputs, "fresh-missing")
        (folder / "observations.json").write_text(json.dumps({"first": first, "rerun": rerun, "fresh": fresh}, indent=2))
        return rerun, fresh

    return mode, folder, first, missing_runs


@pytest.mark.parametrize("aspect", ["merged-status", "raw-json", "detailed-html"])
def test_normal_missing_input_matches_fresh_run(normal_runs, qa, aspect):
    mode, folder, first, missing_runs = normal_runs
    context = dict(issue_id="normal-stale-bsa-results", suite="BSA", mode=mode,
                   stage="normal-execution", evidence=[folder])
    qa.check(first["exit"], 0, status="BLOCKED", **context)
    qa.check(first["status"], ["Compliant"], status="BLOCKED", **context)
    qa.check({key: value for key, value in first["counts"].items() if value},
             {"total": 2, "passed": 2}, status="BLOCKED", **context)
    qa.check([{key: value for key, value in counts.items() if value} for counts in first["html_counts"]],
             [{"total": 2, "passed": 2}], status="BLOCKED", **context)
    rerun, fresh = missing_runs()
    qa.check(fresh["exit"], 0, status="BLOCKED", **context)
    qa.check(len(fresh["status"]) == 1 and "not run" in fresh["status"][0].lower(),
             True, status="BLOCKED", **context)
    qa.check(rerun["exit"], fresh["exit"], **context)
    key = {"merged-status": "status", "raw-json": "raw_present", "detailed-html": "detail_present"}[aspect]
    qa.check(rerun[key], fresh[key], **context)
