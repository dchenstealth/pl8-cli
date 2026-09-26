# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""The CLI against pl8-interface's operations.yaml (see contract/README.md).

Each subcommand is exercised twice: with only its required arguments, and
with every optional argument too. Both must produce params that pass the
operation's schema, compiled with the validator pl8-interface itself uses,
and the full form must reach every property the schema has. A command that
sends more than one operation is driven through all of its legs, with canned
responses, so each leg is checked the same way. Together with the coverage
checks, a new operation, a new schema property, or a new subcommand fails
here until the CLI and this table account for it.
"""

import argparse
import json
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
ATTACHMENT_ID = "0199f3a2-0000-7000-8000-000000000000"
PAGING = ["--limit", "10", "--cursor", "c1"]
# A real file to upload, since its size is read from the filesystem, and a
# path to download to that does not exist: the transfer never runs here.
UPLOAD = str(Path(__file__))
OUTPUT = str(Path(__file__).parent / "contract-download")

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
    ("comment", "list"): ([REF], [REF, "--desc", *PAGING]),
    ("comment", "wait"): (
        [REF],
        [REF, "--after", COMMENT_ID, "--interval", "10", "--max-wait", "30", *PAGING]),
    ("comment", "update"): (
        [REF, COMMENT_ID, "--body", "B"],
        [REF, COMMENT_ID, "--body", "B", "--if-version", "2"]),
    ("comment", "delete"): ([REF, COMMENT_ID], [REF, COMMENT_ID]),
    ("attachment", "add"): (
        [REF, "--file", UPLOAD, "--creator", "alice"],
        [REF, "--file", UPLOAD, "--creator", "alice", "--comment", COMMENT_ID,
         "--name", "notes.md", "--content-type", "text/markdown"]),
    ("attachment", "get"): (
        [REF, ATTACHMENT_ID, "--output", OUTPUT],
        [REF, ATTACHMENT_ID, "--output", OUTPUT, "--force"]),
    ("attachment", "list"): ([REF], [REF, "--desc", *PAGING]),
    # One subcommand, two operations: one case per side.
    ("attachment", "list", "--comment"): (
        [REF, "--comment", COMMENT_ID],
        [REF, "--comment", COMMENT_ID, "--desc", *PAGING]),
    ("attachment", "delete"): ([REF, ATTACHMENT_ID], [REF, ATTACHMENT_ID]),
    ("blocker", "add"): (
        ["--blocking", REF, "--blocked", "OPS/def456"],
        ["--blocking", REF, "--blocked", "OPS/def456"]),
    ("blocker", "remove"): (
        ["--blocking", REF, "--blocked", "OPS/def456"],
        ["--blocking", REF, "--blocked", "OPS/def456"]),
    ("blocker", "list", "--blocked"): (["--blocked", REF], ["--blocked", REF, *PAGING]),
    ("blocker", "list", "--blocking"): (["--blocking", REF], ["--blocking", REF, *PAGING]),
}

# Operations with no subcommand of their own, reached through pl8 invoke,
# which passes params through unchanged: re-signing an upload is a recovery
# step for `attachment add`, not a command anyone drives on its own. Covered
# the same way, so such an operation still has to be named here and still has
# to have every one of its properties sent.
RAW_CASES = {
    "resign_issue_attachment_upload": (
        {"space_id": "ENG", "issue_id": "abc123", "attachment_id": ATTACHMENT_ID},
        {"space_id": "ENG", "issue_id": "abc123", "attachment_id": ATTACHMENT_ID}),
}

# Takes any operation by design, so it has no schema of its own to meet.
RAW_COMMANDS = {("invoke",)}

# What the multi-step commands read out of their earlier legs' responses.
RESPONSES = {
    "initiate_issue_attachment_upload": {
        "attachment": {"attachment_id": ATTACHMENT_ID, "status": "PENDING"},
        "upload": {"url": "https://s3.example/pl8", "fields": {"key": "k"}}},
    "get_issue_attachment": {
        "attachment": {"attachment_id": ATTACHMENT_ID, "status": "UPLOADED"},
        "download_url": "https://s3.example/pl8/k"},
}


def leaf_commands(parser, path=()):
    """Every runnable subcommand path, e.g. ("issue", "create")."""
    subparsers = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subparsers:
        return {path}
    return {leaf for name, child in subparsers[0].choices.items()
            for leaf in leaf_commands(child, (*path, name))}


def canned(request):
    """A response that lets a command get to its next leg.

    Pages come back with an item in them, so `comment wait` returns on its
    first poll rather than sleeping through its wait.
    """
    if OPERATIONS[request.operation].get("paginated"):
        return {"ok": True, "data": {"items": [{}], "cursor": None}}
    return {"ok": True, "data": RESPONSES.get(request.operation)}


def legs(case, argv):
    """Every operation the command sends, driven with canned responses.

    A command that sends one operation has one leg. An attachment upload has
    its initiate and its confirm, with the S3 transfer between them answered
    here instead of performed: nothing in this file reaches AWS or S3.
    """
    command = [part for part in case if not part.startswith("--")]
    _, plan = cli.parse(["--env", "dev", *command, *argv])
    sent = []

    def perform(step):
        if not isinstance(step, cli.Request):
            return {"ok": True, "data": None}
        sent.append(step)
        return canned(step)

    envelope = cli.drive(plan, perform)
    assert envelope["ok"], envelope
    return sent


def raw_legs(operation, params):
    return legs(("invoke",), [operation, "--params", json.dumps(params)])


def all_requests():
    for case, forms in CASES.items():
        for form, argv in zip(("required", "full"), forms):
            for request in legs(case, argv):
                yield case, form, request
    for operation, forms in RAW_CASES.items():
        for form, params in zip(("required", "full"), forms):
            for request in raw_legs(operation, params):
                yield ("invoke", operation), form, request


def test_every_subcommand_has_a_case():
    covered = {tuple(p for p in case if not p.startswith("--")) for case in CASES}
    assert leaf_commands(cli.build_parser()) == covered | RAW_COMMANDS


def test_every_operation_is_reachable():
    assert {request.operation for _, _, request in all_requests()} == set(OPERATIONS)


@pytest.mark.parametrize("request_", [
    pytest.param(request, id=f"{' '.join(case)} ({form}: {request.operation})")
    for case, form, request in all_requests()
])
def test_params_pass_the_schema(request_):
    validate = fastjsonschema.compile(OPERATIONS[request_.operation]["schema"])
    validate(request_.params)


@pytest.mark.parametrize("request_", [
    pytest.param(request, id=f"{' '.join(case)} ({request.operation})")
    for case, form, request in all_requests() if form == "full"
])
def test_full_form_reaches_every_property(request_):
    assert set(request_.params) == set(OPERATIONS[request_.operation]["schema"]["properties"])


@pytest.mark.parametrize("case", list(CASES), ids=" ".join)
def test_all_pages_only_for_paginated_operations(case):
    command = [part for part in case if not part.startswith("--")]
    required = CASES[case][0]

    def paginated(requests):
        return [r for r in requests if OPERATIONS[r.operation].get("paginated", False)]

    if paginated(legs(case, required)):
        assert all(r.all_pages for r in paginated(legs(case, [*required, "--all"])))
    else:
        with pytest.raises(cli.UsageError):
            cli.parse(["--env", "dev", *command, *required, "--all"])


def test_statuses_match_schema():
    enum = OPERATIONS["create_issue"]["schema"]["properties"]["status"]["enum"]
    assert list(cli.STATUSES) == enum


def test_the_size_limit_matches_the_schema():
    size = OPERATIONS["initiate_issue_attachment_upload"]["schema"]["properties"]["size"]
    assert cli.MAX_ATTACHMENT_BYTES == size["maximum"]
