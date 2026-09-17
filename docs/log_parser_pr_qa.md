# Log-parser PR checks

The parser's compliance results are partner-facing. A green check must mean
more than "the scripts ran" or "JSON and HTML agree". The tests also compare
results with independent expectations from small input logs and an explicit
policy table. Otherwise, two outputs could agree and both be wrong.

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
| Failure cleanup | Invalid arguments or input must fail without replacing an existing report with partial output. |

The compliance matrix covers Mandatory, Recommended, Conditional-Mandatory
and Extension requirements; present and missing suites; selected and full
runs; failures, waivers and their combinations; and nested result formats.
Mixed-suite tests verify that non-blocking or waived results cannot erase
another suite's blocking failure.

## When a Check Fails

Start with the failed scenario in the Actions summary. Its name includes the
suite, mode or policy combination. The assertion shows expected and actual
results. Download the QA artifact for the complete command output and XML.

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

Fix the smallest responsible stage, rerun the failed scenario, then run the
whole gate. A shared helper can affect suites that the original change did
not mention. Add the reproducing input to the tests before fixing a new bug.

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

# Run the existing shared-UI browser regressions.
python common/acs_test_framework_runner/report_ui_browser_smoke.py
```

Use `--test MANIFEST::GROUP` from the YAML listing to select a group.
`--reports-dir DIR` and `--report-name MANIFEST=filename.xml` control YAML
report output. Pytest supports `--collect-only` and `--junitxml=filename.xml`.

## Adding Coverage

Put sanitized logs and explicit JSON/compliance expectations in
`common/acs_test_framework_runner/tests/log_parser/scenarios.yaml`.
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

One existing policy needs explicit owner review: a missing Recommended suite
blocks a full DT run, while a present failing Recommended suite does not.
The tests preserve that behavior. The SR OS Recommended-group exception is
also explicit; it must not leak into BSA or other suites.

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
