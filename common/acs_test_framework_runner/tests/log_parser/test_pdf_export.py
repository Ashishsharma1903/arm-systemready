"""Public PDF export preserves suite identity, counts, and compliance text."""

import json
import re

from pypdf import PdfReader
import pytest

from test_end_to_end import CASES, SCENARIOS, portable_parser, run


OUTCOMES = [outcome for outcome in SCENARIOS["outcomes"] if outcome["suite"] == "FWTS"]


@pytest.mark.parametrize("outcome", OUTCOMES, ids=lambda outcome: outcome["name"])
def test_standalone_pdf_export(outcome, portable_parser, tmp_path):
    source = tmp_path / "fwts.log"
    log = CASES[0]["files"]["results/fwts/FWTSResults.log"]
    if outcome["status"] == "passed":
        log = log.replace("FAILED [HIGH] QA failed", "PASSED: Test 2, QA passed")
    source.write_text(log)
    category = tmp_path / "category.json"
    category.write_text(json.dumps({"qa": [{
        "Suite": "FWTS", "Test Suite": "uefirtmisc", "Waivable": "yes",
        "SRS scope": "core", "Main Readiness Grouping": "QA",
    }]}))
    output = tmp_path / "output"
    command = ["bash", portable_parser / "main_log_parser.sh", "--standalone", "--mode", "SR",
               "--suite", "FWTS", "--input-log", source, "--output", output,
               "--outputs", "pdf", "--schema", "--test-category", category]
    if outcome["status"] == "waived":
        waiver = tmp_path / "waiver.json"
        waiver.write_text(json.dumps({"Suites": [{"Suite": "FWTS", "Reason": "QA approved waiver"}]}))
        command.extend(["--waiver", waiver])
    run(command, tmp_path)

    pdf = output / "acs_summary.pdf"
    assert pdf.is_file() and pdf.stat().st_size > 1000
    assert pdf.read_bytes().startswith(b"%PDF-")
    document = PdfReader(pdf, strict=True)
    assert not document.is_encrypted and len(document.pages) > 0
    text = " ".join(" ".join(page.extract_text() for page in document.pages).split())
    assert "SBBR-FWTS" in text, text
    expected_compliance = {
        "passed": "Compliant", "failed": "Not Compliant", "waived": "Compliant with waivers"
    }[outcome["status"]]
    match = re.search(
        r"SRS requirements compliance results\s+(Not Compliant|Compliant with waivers|Compliant)\b",
        text,
        flags=re.IGNORECASE,
    )
    assert match and match.group(1).casefold() == expected_compliance.casefold(), text
    for label, count in (("Total Tests", 2), ("Passed", outcome["passed"]),
                         ("Failed", outcome["failed"]), ("Failed with Waiver", outcome["waived"])):
        assert re.search(rf"\b{label}\s+{count}\b", text), (label, count, text)
