# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""The CLI against pl8-interface's operations.yaml (see contract/README.md).

Each subcommand is exercised twice: with only its required arguments, and
with every optional argument too. Both must produce params that pass the
operation's schema, compiled with the validator pl8-interface itself uses,
and the full form must reach every property the schema has. Together with
the coverage checks, a new operation, a new schema property, or a new
subcommand fails here until the CLI and this table account for it.
"""

import argparse
from pathlib import Path

import fastjsonschema
import pytest
import yaml

from pl8_cli import cli

OPERATIONS = {
    entry["method"]: entry
    for entry in yaml.safe_load(
        (Path(__file__).parent / "contract" / "operations.yaml").read_text()
    )["operations"]
}

REF = "ENG/abc123"
COMMENT_ID = "0199f3a1-0000-7000-8000-000000000000"
PAGING = ["--limit", "10", "--cursor", "c1"]

# subcommand: (required-only argv, argv with every optional too)
CASES = {
    ("space", "create"): (
        ["ENG", "--name", "N", "--description", "D", "--creator", "alice"],
        ["ENG", "--name", "N", "--description", "D", "--creator", "alice"]),
    ("space", "get"): (["ENG"], ["ENG"]),
    ("space", "list"): ([], PAGING),
    ("space", "update"): (
        ["ENG", "--name", "N", "--description", "D"],
        ["ENG", "--name", "N", "--description", "D", "--if-version", "2"]),
    ("space", "delete"): (["ENG"], ["ENG"]),
    ("issue", "create"): (
        ["--space", "ENG", "--title", "T", "--description", "D", "--creator", "alice"],
        ["--space", "ENG", "--title", "T", "--description", "D", "--creator", "alice",
         "--status", "BLOCKED"]),
    ("issue", "get"): ([REF], [REF]),
    ("issue", "list"): (
        ["--space", "ENG", "--status", "TODO"],
        ["--space", "ENG", "--status", "TODO", *PAGING]),
    ("issue", "update"): (
        [REF, "--title", "T", "--description", "D"],
        [REF, "--title", "T", "--description", "D", "--if-version", "2"]),
    ("issue", "transition"): (
        [REF, "--status", "DONE"],
        [REF, "--status", "DONE", "--if-version", "2"]),
    ("issue", "delete"): ([REF], [REF]),
    ("comment", "add"): (
        [REF, "--body", "B", "--creator", "alice"],
        [REF, "--body", "B", "--creator", "alice"]),
    ("comment", "get"): ([REF, COMMENT_ID], [REF, COMMENT_ID]),
    ("comment", "list"): ([REF], [REF, *PAGING]),
    ("comment", "update"): (
        [REF, COMMENT_ID, "--body", "B"],
        [REF, COMMENT_ID, "--body", "B", "--if-version", "2"]),
    ("comment", "delete"): ([REF, COMMENT_ID], [REF, COMMENT_ID]),
    ("blocker", "add"): (
        ["--blocking", REF, "--blocked", "OPS/def456"],
        ["--blocking", REF, "--blocked", "OPS/def456"]),
    ("blocker", "remove"): (
        ["--blocking", REF, "--blocked", "OPS/def456"],
        ["--blocking", REF, "--blocked", "OPS/def456"]),
    # One subcommand, two operations: one case per side.
    ("blocker", "list", "--blocked"): (["--blocked", REF], ["--blocked", REF, *PAGING]),
    ("blocker", "list", "--blocking"): (["--blocking", REF], ["--blocking", REF, *PAGING]),
}

# Takes any operation by design, so it has no schema of its own to meet.
RAW_COMMANDS = {("invoke",)}


def leaf_commands(parser, path=()):
    """Every runnable subcommand path, e.g. ("issue", "create")."""
    subparsers = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subparsers:
        return {path}
    return {leaf for name, child in subparsers[0].choices.items()
            for leaf in leaf_commands(child, (*path, name))}


def build(case, argv):
    command = [part for part in case if not part.startswith("--")]
    _, request = cli.parse(["--env", "dev", *command, *argv])
    return request


def all_requests():
    for case, (required, full) in CASES.items():
        yield case, "required", build(case, required)
        yield case, "full", build(case, full)


def test_every_subcommand_has_a_case():
    covered = {tuple(p for p in case if not p.startswith("--")) for case in CASES}
    assert leaf_commands(cli.build_parser()) == covered | RAW_COMMANDS


def test_every_operation_is_reachable():
    assert {request.operation for _, _, request in all_requests()} == set(OPERATIONS)


@pytest.mark.parametrize("request_", [
    pytest.param(request, id=f"{' '.join(case)} ({form})")
    for case, form, request in all_requests()
])
def test_params_pass_the_schema(request_):
    validate = fastjsonschema.compile(OPERATIONS[request_.operation]["schema"])
    validate(request_.params)


@pytest.mark.parametrize("case", list(CASES), ids=" ".join)
def test_full_form_reaches_every_property(case):
    request = build(case, CASES[case][1])
    assert set(request.params) == set(OPERATIONS[request.operation]["schema"]["properties"])


@pytest.mark.parametrize("case", list(CASES), ids=" ".join)
def test_all_pages_only_for_paginated_operations(case):
    command = [part for part in case if not part.startswith("--")]
    required = CASES[case][0]
    paginated = OPERATIONS[build(case, required).operation].get("paginated", False)

    if paginated:
        assert build(case, [*required, "--all"]).all_pages
    else:
        with pytest.raises(cli.UsageError):
            cli.parse(["--env", "dev", *command, *required, "--all"])


def test_statuses_match_schema():
    enum = OPERATIONS["create_issue"]["schema"]["properties"]["status"]["enum"]
    assert list(cli.STATUSES) == enum
