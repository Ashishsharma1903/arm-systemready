# Log-parser pull-request QA

## When it runs

`.github/workflows/log-parser-qa.yml` runs on GitHub pushes and pull requests
only when a changed path matches `common/log_parser/**`. This includes additions,
modifications and deletions. Documentation-only and unrelated changes do not
start parser QA. Doctor remains optional and is not invoked by this workflow.

The workflow tests Python 3.10 and 3.12 independently. All actions are pinned,
checkout credentials are disabled, and the token has read-only contents access.
There is no privileged `pull_request_target` execution and no partner logs or
secrets are required.

GitHub leaves a path-filtered required workflow pending on unrelated PRs.
Do not configure this workflow as an unconditional repository-wide required
check without an agreed path-aware branch protection policy.

## What is checked

- Every parser Python, shell and JSON file is checked individually. Invalid JSON,
  duplicate keys, non-finite values and missing file inventories fail.
- Registry routes, modes, schemas and independent expected suite requirements
  must agree. New suites must supply fixtures and expected results.
- Existing YAML regression groups run with zero-test, warning and skip failures.
- Frozen producer-format fixtures exercise log-to-JSON, waivers, selected/full
  merge behavior, detailed HTML, summary HTML and PDF.
- Mandatory, Recommended, Extension and conditional applicability are checked
  separately from the displayed suite result. Recommended failures do not
  become an overall mandatory failure.
- Empty/introductory-only logs must be Not Run with zero outcomes. A partly
  executed log is tested separately; it must not be mistaken for a clean run.
- Raw results, counters, identities, merged compliance and rendered results
  are cross-checked. Corruption tests prove that validators reject mismatches,
  malformed result shapes, missing files and incorrect links.
- The real standalone archive is built, checksum-checked, safely extracted and
  executed outside the source checkout. Copied-folder execution alone is not
  treated as package qualification.
- Normal reruns check that previous output cannot hide missing current input.
- Desktop/mobile browser checks and PDF content checks protect navigation,
  filtering, collapsed rows, identities, counts and compliance presentation.
- The same frozen DT/SR inputs are replayed through the base and candidate parser.
  Semantic comparison preserves individual identities/statuses, counts and
  compliance, rather than comparing volatile timestamps byte-for-byte.

Registration/compliance onboarding tests run inside the contracts stage once;
there is no second onboarding job repeating the same tests.

## Reading the result

A parser CI failure is not a hardware test failure. A fixture may deliberately
contain failed ACS tests; CI passes that assertion only when the parser reports
those failures correctly.

Inspect the GitHub job summary first. Download its artifact for:

- `qa-findings.json`: every failed assertion, suite, mode, expected and actual
  result, reproduction command and supporting files;
- JUnit XML and complete stage logs;
- `replay/result.json` and each base/candidate replay output.

Repeated assertions are grouped for readability, not counted as separate bugs.
Missing dependencies, stages, reports, zero checks or skipped checks never count
as successful validation.

The same candidate QA checks and fixtures run against both the base revision and
the proposed parser, in separate runtime checkouts. An unchanged, reproduced
failure is reported as an **existing issue**, not a reason to reject the push.
New failures, changed failure evidence, lost checks and incomplete CI execution
block the change. Comparing test names alone is not enough: expected/actual
values and diagnostics are compared too, so an already-failing test cannot hide
a different regression.

`NO_NEW_REGRESSIONS` means the proposed change did not introduce a detected
regression. It does **not** mean all parser correctness checks passed. Existing
issues and any resulting downstream coverage limits remain visible. No tests
are changed to xfail and no manually maintained allowlist silently accepts bugs.

An intended parser improvement can change the semantic replay. Review the exact
difference and update independently reviewed fixture expectations; never replace
expected output automatically with whatever the candidate produces.

## Local execution

From the repository root, use a fresh virtual environment:

```bash
python3 -m venv /tmp/systemready-parser-qa
. /tmp/systemready-parser-qa/bin/activate
python -m pip install -r common/acs_test_framework_runner/requirements-qa.txt

python common/acs_test_framework_runner/qa_run.py \
  --base origin/main --reports /tmp/parser-regression-fresh

python -m pytest -q common/acs_test_framework_runner \
  --ignore=common/acs_test_framework_runner/tests/log_parser \
  --junitxml=/tmp/parser-qa-selftests.xml
```

PDF checks need the operating-system libraries used by WeasyPrint.
Browser checks need Chromium (or a Chromium-compatible browser exposed as
`chromium`). A missing dependency is a blocked check, not a pass.

Use a fresh temporary output directory on each run. Never reuse old JUnit
reports as proof of a new run.

For an optional audit of CI's ability to catch mistakes, run:

```bash
python common/acs_test_framework_runner/mutation_audit.py \
  --source . --output /tmp/parser-mutation-audit-fresh
```

This runs intentional source mistakes only in disposable copies. A mutation is
counted as detected only when its unmodified targeted control passed first and
the expected error was found. It is not repeated in every normal CI job.

## Scope and limits

Controlled fixtures and intentional mutations provide parser regression evidence,
not hardware certification or a guarantee that every possible future bug is
detectable. Current scope does not include real release/partner log collections.
A full-release DT/SR corpus can be added later, with explicit publication approval,
immutable version/checksums and independently reviewed expectations.

Production parser behavior is not changed by this QA implementation. Its
artifact validator lives in the QA runner, not the shipped parser API.
