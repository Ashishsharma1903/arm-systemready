# Log-parser pull-request QA

## When it runs

`.github/workflows/log-parser-qa.yml` runs on GitHub pushes and pull requests
when a changed path matches any of these:

- `common/log_parser/**`: production parser code, configuration and schemas.
- `common/acs_test_framework_runner/**`: all parser QA checks, fixtures,
  dependencies and shared test-runner helpers.
- `common/acs_test_framework_manifests/**`: test definitions, including a case
  added to an existing YAML file or a new nested `.yaml`/`.yml` manifest.
- `.github/workflows/log-parser-qa.yml`: this workflow itself.

Additions, modifications, renames and deletions are included. Changes limited to
`docs/`, top-level documentation, generated reports or unrelated workflows do
not start parser QA. Any file inside the watched directories starts CI. A
QA-only or manifest-only change still runs every parser check; the workflow does
not skip stages because production parser files are unchanged. New parser YAML
cases are discovered automatically and run against both parser revisions using
the same proposed test definitions.

The enhanced repository-health Doctor remains optional and is not the CI
engine. A separate smoke check still exercises the standalone `--doctor`
dependency preflight. The strict self-test stage runs the CI machinery and
shared manifest-selection tests, not Doctor's separate unit tests or a full
Doctor health run. Parser behavior is checked independently against both
revisions in the comparison and replay stages.

The workflow runs all checks once on Python 3.12. All actions are pinned,
checkout credentials are disabled, and the token has read-only contents access.
There is no privileged `pull_request_target` execution and no partner logs or
secrets are required.

A push to a branch with an open PR can start two workflow runs: push compares
the branch change, while PR checks GitHub's proposed merge with its target.
These are separate event checks, not two Python versions. Neither is skipped
based only on an open-PR lookup, which could miss a conflicted or untested merge.

Checkout fetches the proposed revision and the exact comparison commit, not the
repository's complete history. A first push on a new branch fetches `main` as
its comparison base. Setup and test stages have separate time limits, with a
larger overall budget that leaves time to generate and upload evidence. A stage
that times out still blocks the change; increasing the budget does not skip it.

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

Base and candidate checks run concurrently in separate workspaces when at least
two workers are available. Their YAML workers split the requested `--jobs`
budget; `--jobs 1` runs the revisions serially. Both revisions retain every
stage, timeout, report and log, including when the other revision fails.

Artifact validation reuses schema preparation only within one read-only check.
Every generated JSON and HTML file is still read and checked, and each corruption
case still invokes the full validator. The setup cache is discarded after the
check, including on error; later checks cannot reuse old artifact results or
schemas. This reduces repeated setup without removing scenarios or assertions.

Parser-specific QA lives in `common/acs_test_framework_runner/log_parser_qa/`.
`qa_run.py` classifies changes, runs both revisions and compares their evidence;
`qa_report.py` captures findings and builds the summary. Artifact validation,
semantic replay and the optional mutation audit each retain a separate module.
Two files test the QA machinery. Its `tests/` folder contains three parser
contract test files and one shared `fixtures.yaml`, grouped by execution,
compliance and artifact checks. Shared execution and reporting helpers remain
in `common/acs_test_framework_runner/`; the production parser stays separate in
`common/log_parser/`.

## Reading the result

A parser CI failure is not a hardware test failure. A fixture may deliberately
contain failed ACS tests; CI passes that assertion only when the parser reports
those failures correctly.

Inspect the GitHub job summary first. Download its artifact for:

- `qa-findings.json`: every failed assertion, suite, mode, expected and actual
  result, reproduction command and supporting files;
- `dependencies.log` and `change-impact.stderr.log`: setup output and change
  classification errors, including the registry path, revision and JSON line;
- JUnit XML and complete stage logs;
- `replay/result.json` and each base/candidate replay output.

Package mismatches list missing, extra and changed paths with sizes and hashes.
The full path inventory is saved in `package-integrity.json` within the contract
work directory; failure reports do not repeat the contents of every source file.

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

A completed parser's output mismatch is distinguished from an unavailable test
fixture. If the same wrong output is reproduced on both revisions, it remains
an existing issue, with any dependent assertions explicitly marked unverified.
New or changed mismatches still block the change, as do host/setup failures.

An intended parser improvement can change the semantic replay. Review the exact
difference and update independently reviewed fixture expectations; never replace
expected output automatically with whatever the candidate produces.

## Local execution

From the repository root, use a fresh virtual environment:

```bash
python3 -m venv /tmp/systemready-parser-qa
. /tmp/systemready-parser-qa/bin/activate
python -m pip install -r common/acs_test_framework_runner/log_parser_qa/requirements-qa.txt

python common/acs_test_framework_runner/log_parser_qa/qa_run.py \
  --base origin/main --reports /tmp/parser-regression-fresh

python -m pytest -q common/acs_test_framework_runner/log_parser_qa \
  common/acs_test_framework_runner/test_pytest_runner_selection.py \
  --ignore=common/acs_test_framework_runner/log_parser_qa/tests \
  --junitxml=/tmp/parser-qa-selftests.xml
```

PDF checks need the operating-system libraries used by WeasyPrint.
Browser checks need Chromium (or a Chromium-compatible browser exposed as
`chromium`). A missing dependency is a blocked check, not a pass.

Use a fresh temporary output directory on each run. Never reuse old JUnit
reports as proof of a new run.

For an optional audit of CI's ability to catch mistakes, run:

```bash
python common/acs_test_framework_runner/log_parser_qa/mutation_audit.py \
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

<!-- Disposable docs-only no-trigger control. -->
