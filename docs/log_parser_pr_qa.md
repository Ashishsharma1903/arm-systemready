# Log-parser PR checks

The parser's compliance results are partner-facing. A green check must mean
more than "the scripts ran" or "JSON and HTML agree". The tests also compare
results with independent expectations from small input logs and an explicit
policy table. Otherwise, two outputs could agree and both be wrong.

This change is CI-only: it adds checks, validation and reporting, not parser,
merger, waiver, renderer or schema fixes. Existing defects must remain visible
as failing checks. Fixes belong in separately reviewed changes; do not skip
tests, weaken assertions or change expected results just to make CI green.
Strict JSON decoding is opt-in through `validate.py artifacts`; the existing
`raw` and `merged` commands retain their previous JSON-loading behavior.

## What Runs

`.github/workflows/log-parser-qa.yml` runs on every GitHub pull request, on
manual dispatch, and on the `log-parser-pr-qa` fork-testing branches.
It does not change the existing SystemReady workflows or Methodology CI.

| Change | Additional checks |
| --- | --- |
| New suite or parser directory | Registration, executable paths, schema, YAML targets, compliance policy and a log fixture with expected output. Adding a suite name without actual produced results is not coverage. |
| Existing suite | Existing parser regressions plus the complete cross-suite suite. A local change can still affect the merger or shared reports. |
| Schema, category, registry, shared parser or QA code | All-suite validation and tests of the validator and test runner themselves. |
| Other files | The mandatory gate still runs, so required GitHub checks are never left pending by a path filter. |

Classification is shown in the Actions summary. It never disables the
mandatory compliance, artifact or browser checks. Python 3.10 and 3.12 run
the same checks. Missing dependencies, skipped tests, empty reports and test
failures fail the job. Full logs and JUnit results are uploaded as artifacts.

## Coverage

| Check | Example failure it catches |
| --- | --- |
| Portable execution | A parser imports a file outside `common/log_parser`, works in a developer checkout, then fails in isolation. |
| Log to raw JSON | The log contains a failed test but JSON records it as passed or loses it. Expected assertions are written in the fixture YAML. |
| Test identity | Two named tests exchange pass/fail results while totals stay unchanged. Counts alone would miss this. |
| Raw JSON schema and counts | Negative, fractional, boolean or string counters; unknown fields or statuses; disagreement between test results and summaries. |
| Raw to merged JSON | A suite or test disappears, is duplicated, changes status, or gains inconsistent category metadata during merging. |
| Compliance policy | A failed Recommended BSA group becomes Compliant; a waiver hides a separate failure; input ordering changes overall compliance. |
| JSON to detailed and summary HTML | Wrong counts or statuses, missing/duplicated result rows, broken local links, missing reports or duplicate anchors. |
| Consolidated report | A suite's counts, aggregate compliance status, or displayed failed/not-run suite lists differ from merged JSON. |
| Browser | Generated reports fail to initialize, visible counts change, filtering fails, or a report overflows a mobile viewport. |
| PDF export | A selected-suite passing, failing or waived result fails to export, or the compliance text is lost. This is a data smoke test, not a full visual PDF review. |
| Negative controls | Intentionally corrupting generated JSON or HTML must fail validation. A validator that always returns success cannot pass. |
| Incomplete input | A BSA log ends after starting a rule; the unfinished test must not disappear into a Compliant report. |
| Native positive controls | A known valid log must produce its expected test identities, counts and HTML before malformed-input checks can count as evidence. |
| Native false-green regressions | SCT parent failures without subtests, empty/header-only logs, missing or UNKNOWN SBMR statuses, and missing Capsule completion results must not invent success. |
| Actual waiver reproductions | FWTS whole-suite waivers cover both failed assertions; passed or unmatched tests remain unchanged; Runtime Device Map waivers do not crash; repeated waivers do not grow counts. |
| Normal reruns | Parse passing BSA logs, remove those fixture logs, rerun, and compare with a fresh missing-input run. Old Compliant JSON/HTML must not survive as current output. |
| Mixed-suite isolation | Swap passing/failing BSA and SBSA, or present/missing SBMR interfaces. Check each requirement/status, combined result, summary badge and detailed rows separately. |
| Failure cleanup | Invalid arguments or input must fail without replacing an existing report with partial output. |

The compliance matrix covers Mandatory, Recommended, Conditional-Mandatory
and Extension requirements; present and missing suites; selected and full
runs; failures, waivers and their combinations; and nested result formats.
Mixed-suite tests verify that non-blocking or waived results cannot erase
another suite's blocking failure.

## Known Failing Contracts

The following were reproduced with the application files and schema from
public main `4bc29f3c`. They are not fixed by this CI change. The failing tests
are retained so separate fixes can demonstrate that the problem is resolved.

| Area | Observed result | Reproducing check |
| --- | --- | --- |
| Recommended BSA results | A failed DT BSA group is shown as Compliant, while failed Post-Script results remain visible. A Recommended suite's own failure must remain visible without blocking overall compliance. | `test_bsa_and_post_script_recommended_failures_remain_visible` |
| SCT waiver totals | One passed and one waived result produce an HTML total of 1 instead of 2. | `test_sct_waivers_included_in_report_total` |
| BSA waiver summaries | A waived testcase still has `Failed: 1` in its testcase summary. | `test_bsa_waiver_updates_case_summary` |
| Incomplete or invalid BSA input | Unfinished rules and unsupported verdicts are not consistently rejected. An unfinished test can disappear from the reported results. | `test_incomplete_bsa.py` and the public CLI cases in `test_end_to_end.py` |
| Schema contracts | Some emitted SR OS and waiver data are rejected, while malformed compliance labels and some missing classification fields are accepted. | `test_schema_contract.py` |
| BBSR raw metadata | The raw BBSR-TPM enrichment does not find the category alias used by the merger. | `test_tpm_raw_enrichment_matches_merger_category_alias` |
| Stale normal results | After removing a passing BSA fixture log, a normal rerun still reports Compliant and leaves the old BSA JSON/HTML; a fresh missing-input run reports Not Run. | `test_normal_missing_input_matches_fresh_run` |

A failed assertion still needs triage: distinguish wrong report data from an
overly specific test contract. For example, rejection by a different exception
is not the same as accepting bad input. Keep the original failure evidence and
get policy or interface expectations reviewed before changing either side.

## When a Check Fails

Start with the failed scenario in the Actions summary. Its name includes the
suite, mode or policy combination. The assertion shows expected and actual
results. Download the QA artifact for the complete command output and XML.
It also contains `pytest-work/` (and `onboarding-work/` when used), including
fixture inputs and generated JSON/HTML that remain on disk. Failed standalone
runs clean up their temporary output; use the retained inputs and logs to
reproduce those failures.

`qa-findings.json` retains all affected scenarios, grouped by issue and stage.
Doctor-derived contracts record suite, mode, expected and actual results,
reproduction command and evidence paths. `BLOCKED` means a required control or
setup did not complete; it is not a passing malformed-input test or a confirmed
parser defect. Missing dependencies, missing stage outcomes and zero executed
required checks prevent green CI, even if another report contains passing tests.
Generic YAML findings identify the test group and source file; their mode is
`not selected by YAML runner`. Use the explicit SR/DT scenarios for mode-specific
compliance evidence.

The native tests reuse Doctor's synthetic scenarios, not its unmerged code or
private rendering helpers. Schema checks run separately: a known schema failure
must not prevent a valid native parser control from exercising a waiver or
incomplete-log regression. Missing required compliance badges/rows are reported
as separate HTML contract failures, not mistaken for incorrect raw counts.

| Failure | First place to inspect | What to verify |
| --- | --- | --- |
| New-suite onboarding | `suite_registry.json`, suite manifests, `scenarios.yaml` | A real log produces the declared output, with known testcase identities and results. |
| Log expectation | The suite's `logs_to_json.py` | Did the input format change, or did parsing lose or misclassify a test? |
| Schema | `acs-results-schema.json` and the JSON-producing code | Fix the producer or model a valid format explicitly. Do not make result fields optional just to pass. |
| Raw/merged mismatch | `enrich_suite_json.py`, `apply_waivers.py`, `merge_jsons.py` | Preserve every result and update all affected counts together. |
| Compliance | `merge_jsons.py`, `compliance_cases.yaml`, category files | Separate visible suite results from their effect on overall compliance. Confirm policy changes with the owner. |
| HTML data or links | The suite's `json_to_html.py`, `merge_summary.py` | Keep displayed statuses, totals and report destinations consistent with JSON. |
| Browser | `report_ui.py` and the suite renderer | Check visible data after JavaScript runs, including filters and mobile layout. |
| Portable execution | `standalone_runner.py`, registry paths, requirements | Reproduce from an isolated parser directory, not just the repository root. |
| Test discovery or skipped checks | `pytest_runner.py`, manifests, workflow | Required checks must actually execute and report failures. |

Report the smallest responsible stage and keep its reproducing input. Make
the application fix in a separate reviewed change, rerun the failed scenario,
then run the whole gate. A shared helper can affect suites that the original
change did not mention. A red baseline is not permission to disable the check.

## Run Locally

Use a virtual environment with the QA requirements and Chromium on `PATH`.
WeasyPrint also needs its platform libraries; GitHub installs these explicitly.

```bash
python -m pip install -r common/acs_test_framework_runner/requirements-qa.txt

# List the selectable YAML groups.
python common/acs_test_framework_runner/pytest_runner.py --list-tests --target common/log_parser

# Run all YAML groups, with no warning or skip allowed.
python common/acs_test_framework_runner/pytest_runner.py \
  --all-tests --target common/log_parser --require-tests \
  --fail-on-warnings --fail-on-skips --jobs 4

# Run contracts, compliance matrices and end-to-end tests.
python -m pytest -q common/acs_test_framework_runner

# Run one area or one scenario.
python -m pytest -q common/acs_test_framework_runner/tests/log_parser/test_compliance.py
python -m pytest -q common/acs_test_framework_runner/tests/log_parser/test_end_to_end.py -k dt

# Doctor-derived native cases, mixed reports, and the normal rerun regression.
python -m pytest -q common/acs_test_framework_runner/tests/log_parser/test_native_contracts.py
python -m pytest -q common/acs_test_framework_runner/tests/log_parser/test_mixed_compliance_reports.py
python -m pytest -q common/acs_test_framework_runner/tests/log_parser/test_normal_rerun.py

# Run the existing shared-UI browser regressions.
python common/acs_test_framework_runner/report_ui_browser_smoke.py
```

Use `--test MANIFEST::GROUP` from the YAML listing to select a group.
`--reports-dir DIR` and `--report-name MANIFEST=filename.xml` control YAML
report output. Pytest supports `--collect-only` and `--junitxml=filename.xml`.

## Adding Coverage

Put sanitized logs and explicit JSON/compliance expectations in
`common/acs_test_framework_runner/tests/log_parser/scenarios.yaml`.
Native failure/waiver fixtures are in the adjacent `native_contracts.yaml`.
Register the suite, schema and YAML targets too. Do not generate expected
results by running the implementation being tested.

For a new input format, include a passing case, a failing case, an incomplete
or malformed input, and supported waiver cases. Assert which individual tests
passed or failed, not only totals. Include valid non-failure statuses such as
Skipped or Not Run where the suite supports them. Check both detailed and
consolidated results before considering the suite covered.

Keep requirement expectations in `compliance_cases.yaml` separate from the
registry. Every runnable suite needs a requirement for each supported mode;
umbrella selectors use their included suites' requirements. Changing expected
compliance to match a failed test needs policy review, not just a new golden file.

The owner-approved contract is now explicit: Recommended failures and missing
results do not block overall compliance in either full or selected mode. Their
individual results remain Not Compliant or Not Run. Mandatory results still
determine overall compliance. The SR OS Recommended-group exception remains
separate; it must not leak into BSA or other suites.

Every confirmed parser fix needs a retained regression that fails on the broken
implementation and passes after the fix, using the same fixture and expected
answer. Include both revisions, the exact test command and both results in the
fix review. A passing new fixture alone is not evidence that the regression
detects the old bug. Keep a valid control beside malformed-input cases, and
never turn a failed control into a skip or an accepted rejection.

Whole-parent BSA waivers retain the documented behavior: waive the failed
parent and failed descendants, even when a SubTests entry is also supplied;
already-passed descendants remain passed. The normal-rerun test uses the host's
actual mode and never changes `/mnt/yocto_image.flag`. GitHub's Ubuntu job tests
the normal SR path; it does not claim normal DT host-mode coverage.

These checks cannot certify every possible device log or replace approval of
the compliance policy. Full merged-schema validation currently describes a
complete-band report, not every possible partial-suite report. Add partner
log regressions when new formats or policy decisions arrive.

A log cut off between two completed rules needs a producer completion marker
or an independently expected test inventory to prove that later rules are
missing. Rejecting an unfinished rule is not proof that the entire intended
run finished. Keep new real-world regressions as sanitized fixtures so that
each resolved issue remains covered after ownership changes.

To prevent merging a failing PR, make both `Parser QA (Python 3.10)` and
`Parser QA (Python 3.12)` required checks in the target repository's branch
rules. Committing the workflow starts checks; it does not enable branch rules.
