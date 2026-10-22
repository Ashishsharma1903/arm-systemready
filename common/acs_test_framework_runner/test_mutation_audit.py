#!/usr/bin/env python3
# Copyright (c) 2026, Arm Limited or its affiliates. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check mutation audit isolation, anchors and conservative detection verdicts."""

from pathlib import Path

import pytest

try:
    from . import mutation_audit as audit
except ImportError:
    import mutation_audit as audit


@pytest.mark.parametrize(
    "base,mutant,text,status",
    [
        ({"exit_code": 0}, {"exit_code": 1}, "Expected mutation signal", "DETECTED"),
        ({"exit_code": 1}, {"exit_code": 1}, "Expected mutation signal", "BLOCKED"),
        ({"exit_code": 0}, {"exit_code": 0}, "", "MISSED"),
        ({"exit_code": 0}, {"exit_code": 1}, "Unrelated infrastructure error", "BLOCKED"),
        ({"exit_code": 0}, {"exit_code": None, "timed_out": True}, "", "BLOCKED"),
        ({"exit_code": 0}, {"exit_code": 5, "test_count": 0}, "Expected mutation signal", "BLOCKED"),
        ({"exit_code": 0, "valid_evidence": False}, {"exit_code": 1}, "Expected mutation signal", "BLOCKED"),
        ({"exit_code": 0, "failures": [{"message": "hidden failure"}]}, {"exit_code": 1},
         "Expected mutation signal", "BLOCKED"),
    ],
)
def test_detection_requires_passing_control_and_specific_failure(base, mutant, text, status):
    assert audit.classify({"valid_evidence": True, **base},
                          {"valid_evidence": True, **mutant},
                          ("Expected mutation signal",), text)[0] == status


@pytest.mark.parametrize("copies", [0, 2])
def test_ambiguous_source_anchor_is_rejected(tmp_path, copies):
    mutation = audit.MUTATIONS[0]
    path = tmp_path / audit.PARSER / mutation.path
    path.parent.mkdir(parents=True)
    original = mutation.before * copies
    path.write_text(original)
    with pytest.raises(ValueError, match="expected one source anchor"):
        audit.apply_mutation(tmp_path, mutation)
    assert path.read_text() == original


def test_mutation_changes_only_exact_file(tmp_path):
    mutation = audit.MUTATIONS[0]
    path = tmp_path / audit.PARSER / mutation.path
    path.parent.mkdir(parents=True)
    path.write_text("# original\n" + mutation.before)
    neighbour = path.with_name("neighbour.py")
    neighbour.write_text("unchanged")
    patch = audit.apply_mutation(tmp_path, mutation)
    assert mutation.after in path.read_text()
    assert neighbour.read_text() == "unchanged"
    assert mutation.path in patch


def test_mutation_definitions_are_safe_and_unique():
    """The optional audit owns stale-anchor checks, not the normal PR gate."""
    assert len({case.name for case in audit.MUTATIONS}) == len(audit.MUTATIONS)
    for mutation in audit.MUTATIONS:
        path = Path(mutation.path)
        assert not path.is_absolute() and ".." not in path.parts
        assert mutation.before and mutation.before != mutation.after
        assert mutation.signals


def test_audit_preserves_source_and_refuses_existing_evidence(tmp_path, monkeypatch):
    source = tmp_path / "source"
    mutation = audit.MUTATIONS[0]
    original = mutation.before
    path = source / audit.PARSER / mutation.path
    path.parent.mkdir(parents=True)
    path.write_text(original)
    output = tmp_path / "evidence"

    def copy_minimal(src, dst):
        target = dst / audit.PARSER / mutation.path
        target.parent.mkdir(parents=True)
        target.write_text((src / audit.PARSER / mutation.path).read_text())

    def fake_check(checkout, case, evidence):
        evidence.mkdir(parents=True)
        changed = case.after in (checkout / audit.PARSER / case.path).read_text()
        (evidence / "check.log").write_text("SyntaxError" if changed else "PASS")
        return {"valid_evidence": True, "exit_code": int(changed)}

    monkeypatch.setattr(audit, "copy_checkout", copy_minimal)
    monkeypatch.setattr(audit, "run_check", fake_check)
    report = audit.audit(source, output, [mutation])
    assert report["counts"] == {"DETECTED": 1}
    assert report["source_unchanged"]
    assert path.read_text() == original
    with pytest.raises(ValueError, match="existing evidence"):
        audit.audit(source, output, [mutation])


@pytest.mark.parametrize("relative", [audit.PARSER, audit.RUNNER, Path("common/acs_test_framework_manifests")])
def test_audit_evidence_cannot_be_written_into_source_trees(tmp_path, relative):
    with pytest.raises(ValueError, match="outside parser"):
        audit.audit(tmp_path, tmp_path / relative / "evidence", [])
