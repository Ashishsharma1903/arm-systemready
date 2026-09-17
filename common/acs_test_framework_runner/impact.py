#!/usr/bin/env python3
"""Describe parser change scope without reducing the mandatory QA gate."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import subprocess


PARSER = "common/log_parser/"
REGISTRY = PARSER + "suite_registry.json"
QA_PATHS = ("common/acs_test_framework_", ".github/workflows/", ".githooks/")


def _suite_directories(registry):
    directories = {}
    for suite in registry.get("suites", []):
        for key, value in suite.items():
            if "logs_to_json" not in key and key != "json_to_html":
                continue
            for script in value if isinstance(value, list) else [value]:
                parent = str(PurePosixPath(PARSER + script).parent) + "/"
                directories.setdefault(parent, set()).add(suite["canonical"])
    return directories


def classify_changes(paths, old_registry, new_registry, old_files):
    old_names = {item["canonical"] for item in old_registry.get("suites", [])}
    new_names = {item["canonical"] for item in new_registry.get("suites", [])}
    old_execution = old_registry.get("standalone", {}).get("suite_execution", {})
    new_execution = new_registry.get("standalone", {}).get("suite_execution", {})
    added = (new_names - old_names) | (set(new_execution) - set(old_execution))
    directories = _suite_directories(old_registry)
    for directory, suites in _suite_directories(new_registry).items():
        directories.setdefault(directory, set()).update(suites)
    existing, shared = set(), []
    for path in paths:
        source = PurePosixPath(path)
        if path.startswith(PARSER) and source.name in ("logs_to_json.py", "json_to_html.py"):
            directory = str(source.parent) + "/"
            if not any(old.startswith(directory) for old in old_files):
                added.add("directory: " + str(source.parent.relative_to(PARSER)))
        matched = {suite for directory, suites in directories.items()
                   if path.startswith(directory) for suite in suites}
        existing.update(matched & old_names)
        if path.startswith(QA_PATHS) or (path.startswith(PARSER) and not matched):
            shared.append(path)
    kinds = []
    if added:
        kinds.append("new-suite")
    if existing:
        kinds.append("existing-suite")
    if shared:
        kinds.append("shared-schema-qa")
    return {"kinds": kinds or ["no-parser-change"], "new_suites": sorted(added),
            "existing_suites": sorted(existing), "shared_changes": sorted(shared)}


def _git(root, *arguments):
    return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def inspect_change(root, base, head="HEAD"):
    head = _git(root, "rev-parse", "--verify", "--end-of-options", head + "^{commit}").decode().strip()
    if not base or set(base) == {"0"}:
        parents = _git(root, "rev-list", "--parents", "-n", "1", head).decode().split()
        base = parents[1] if len(parents) > 1 else ""
    elif base:
        base = _git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()

    def files(revision):
        return set(_git(root, "ls-tree", "-r", "--name-only", "-z", revision).decode().split("\0")) - {""}

    old_files = files(base) if base else set()
    new_files = files(head)
    paths = (_git(root, "diff", "--name-only", "--no-renames", "-z", base, head, "--").decode().split("\0")
             if base else sorted(new_files))
    old_registry = json.loads(_git(root, "show", base + ":" + REGISTRY)) if REGISTRY in old_files else {}
    new_registry = json.loads(_git(root, "show", head + ":" + REGISTRY)) if REGISTRY in new_files else {}
    result = classify_changes([path for path in paths if path], old_registry, new_registry, old_files)
    return {"base": base, "head": head, **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="Base commit; an empty or all-zero value uses HEAD's parent")
    parser.add_argument("--head", default="HEAD", help="Commit being tested")
    args = parser.parse_args()
    result = inspect_change(Path(__file__).resolve().parents[2], args.base, args.head)
    print(json.dumps(result, indent=2))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"new_suite={str(bool(result['new_suites'])).lower()}\n")
            label = ("All-suite regression (shared/schema/QA)" if result["shared_changes"]
                     else "Existing-suite regression" if result["existing_suites"]
                     else "All-suite regression")
            output.write(f"regression_label={label}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("## Change classification\n\n" + ", ".join(result["kinds"]) + "\n\n")
            summary.write("All suites, schema contracts, and browser checks remain required.\n\n")
            summary.write("```json\n" + json.dumps(result, indent=2) + "\n```\n")


if __name__ == "__main__":
    main()
