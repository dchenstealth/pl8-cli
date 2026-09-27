# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""The pl8 command line.

Output contract, so agents can drive it without scraping text:
* stdout is always exactly one JSON document, the pl8-interface envelope:
  {"ok": true, "data": ...} or {"ok": false, "error": {"type", "message"}}.
  Failures on the CLI side use the same shape. (--help and --version are the
  exceptions, and print text.)
* The exit code says which kind of failure it was; see exit_code().

Parameters are passed through for pl8-interface to validate, so its JSON
Schema stays the only statement of what a valid request is.

Most commands send one operation. Some send several: an upload is initiate,
POST to S3, confirm; a download is a read and then a GET; a wait is as many
polls as the wait needs. They still print exactly one JSON document, at the
end, never a line per step.

Issues are named SPACE/ISSUE_ID. A bare ISSUE_ID takes its space from
--space, then $PL8_SPACE; a space in the reference itself always wins, so
cross-space references need no extra flags. A comment is named by its Issue
and then its own id, as two arguments: a comment belongs to an Issue and is
not addressable without it. An attachment is named the same way, whether it
hangs off the Issue or off one of its comments.
"""

import argparse
import contextlib
import json
import mimetypes
import os
import random
import stat
import sys
import tempfile
import time
from importlib.metadata import version
from typing import NamedTuple

from botocore.exceptions import ProfileNotFound

from pl8_cli.client import (
    FUNCTION_ERROR,
    INVOKE_ERROR,
    TRANSFER_ERROR,
    Invoker,
    TransferError,
    download_file,
    error,
    upload_file,
)

EXIT_OK = 0
# pl8-interface refused the request (bad params, missing item, stale version,
# a lifecycle rule). Fix the request; retrying it unchanged won't help.
EXIT_REJECTED = 1
# The command line itself was wrong; nothing was sent.
EXIT_USAGE = 2
# No trustworthy answer: transport failure or a server-side fault. A write
# that fails this way may or may not have been applied.
EXIT_FAULT = 3
# Transient contention that pl8-interface already retried. Nothing was
# applied, so the same request may be sent again unchanged.
EXIT_TRANSIENT = 4

USAGE_ERROR = "UsageError"
# The attachment exists but its object doesn't: its upload never finished.
# Reported like any other refusal, since the request was well formed and the
# answer is that there is nothing to download yet.
ATTACHMENT_PENDING = "AttachmentPending"
# Error types that mean the server, not the request, is at fault.
# DDBCorruptedError subclasses DDBInternalError but is reported by name.
# TransferError is here because an S3 transfer that fails gives no
# trustworthy answer about whether the bytes landed, which is what EXIT_FAULT
# means: read the attachment back before deciding what to do about it.
FAULT_ERRORS = frozenset({
    INVOKE_ERROR,
    FUNCTION_ERROR,
    TRANSFER_ERROR,
    "DDBInternalError",
    "DDBCorruptedError",
})
# Error types that pl8-base documents as safe to retry unchanged.
TRANSIENT_ERRORS = frozenset({
    "DDBTransactionConflictError",
    "DDBIdCollisionError",
})

FUNCTION_SUFFIX = "-pl8-interface"

# pl8-base's IssueStatus. Listed here for --help; pl8-interface still
# validates.
STATUSES = ("TODO", "BLOCKED", "IN_PROGRESS", "DONE")


class UsageError(Exception):
    pass


class Request(NamedTuple):
    """One pl8-interface operation to send."""

    operation: str
    params: dict
    # Follow cursors and return every page's items as one page.
    all_pages: bool = False


class Upload(NamedTuple):
    """One file to stream to a presigned S3 POST. Not a pl8-interface call."""

    target: dict
    path: str
    size: int
    name: str
    content_type: str


class Download(NamedTuple):
    """One presigned S3 GET to stream into a path."""

    url: str
    path: str


class ArgumentParser(argparse.ArgumentParser):
    """Reports parse errors as a UsageError, so they print as an envelope."""

    def error(self, message):
        self.print_usage(sys.stderr)
        raise UsageError(message)


def read_text(path):
    """Read a file argument, with "-" meaning stdin."""
    if path == "-":
        # Decoded as UTF-8 like a file, whatever the locale's encoding.
        return sys.stdin.buffer.read().decode("utf-8")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise UsageError(f"Cannot read {path}: {exc.strerror}") from exc


def common_options():
    # Every option defaults to SUPPRESS so it can go before or after the
    # subcommand: a leaf parser that doesn't see it leaves the root's value
    # alone instead of overwriting it with a default.
    parser = ArgumentParser(add_help=False)
    group = parser.add_argument_group("connection and output")
    group.add_argument("--env", default=argparse.SUPPRESS,
                       help=f"PL8 environment; invokes <env>{FUNCTION_SUFFIX} "
                            "(default: $PL8_ENV)")
    group.add_argument("--function-name", default=argparse.SUPPRESS,
                       help="pl8-interface function name or ARN; overrides --env "
                            "(default: $PL8_FUNCTION_NAME)")
    group.add_argument("--profile", default=argparse.SUPPRESS,
                       help="AWS profile (default: the standard AWS credential chain)")
    group.add_argument("--region", default=argparse.SUPPRESS,
                       help="AWS region (default: the standard AWS config chain)")
    group.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS,
                       help="indent the JSON output")
    return parser


def add_invoke(subparsers, common):
    parser = subparsers.add_parser(
        "invoke", parents=[common],
        help="send any pl8-interface operation as raw JSON",
        description="Send an operation and its params unchanged. Use this for "
                    "operations without a subcommand of their own.")
    parser.add_argument("operation", help="operation name, e.g. get_issue")
    params = parser.add_mutually_exclusive_group()
    params.add_argument("--params", help="params as a JSON object (default: {})")
    params.add_argument("--params-file", metavar="PATH",
                        help='read the params JSON object from PATH ("-" for stdin)')
    parser.set_defaults(build=build_invoke)


def build_invoke(args):
    raw = args.params
    if args.params_file is not None:
        raw = read_text(args.params_file)
    if raw is None:
        return Request(args.operation, {})
    try:
        params = json.loads(raw)
    except ValueError as exc:
        raise UsageError(f"params are not valid JSON: {exc}") from exc
    if not isinstance(params, dict):
        raise UsageError("params must be a JSON object")
    return Request(args.operation, params)


# Shared arguments

def add_long_text(parser, name, *, required=True):
    """--NAME or --NAME-file, at most one, and exactly one if required.

    PL8's long text fields (an Issue's description, a comment's body) all take
    this pair, so the flag a caller learns for one works for the others.
    """
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(f"--{name}", help=f"{name} text")
    group.add_argument(f"--{name}-file", metavar="PATH",
                       help=f'read the {name} from PATH ("-" for stdin); '
                            "avoids shell quoting for long or multi-line text")


def long_text(args, name):
    if (path := getattr(args, f"{name}_file")) is not None:
        return read_text(path)
    return getattr(args, name)


def add_version(parser):
    # Not --version, which reads as the program's version.
    parser.add_argument("--if-version", type=int, metavar="VERSION",
                        help="only write if the item is still at this version "
                             "(from its last read); fails with "
                             "DDBVersionConflictError otherwise")


def update_fields(args, field):
    """The fields an update was given: FIELD, the description, or both.

    An update leaves out whichever it is not given, and must be given one.
    """
    fields = {field: getattr(args, field),
              "description": long_text(args, "description")}
    fields = {name: value for name, value in fields.items() if value is not None}
    if not fields:
        raise UsageError(f"Nothing to update: pass --{field}, --description or "
                         "--description-file")
    return fields


def versioned(args, params):
    if args.if_version is not None:
        params["version"] = args.if_version
    return params


def add_creator(parser):
    parser.add_argument("--creator", help="who or what to record as the creator, "
                                          "e.g. your name or an agent's; a label PL8 "
                                          "records but never checks "
                                          "(default: $PL8_CREATOR)")


def default_creator(args):
    """The creator to record. PL8 stores the label and never verifies it.

    Required, like the space for a bare Issue id: a row with no creator is
    not a row PL8 accepts, and guessing one from the AWS identity would read
    as an authenticated claim, which it isn't.
    """
    if creator := args.creator or os.environ.get("PL8_CREATOR"):
        return creator
    raise UsageError("No creator: pass --creator or set PL8_CREATOR")


def add_paging(parser):
    parser.add_argument("--limit", type=int, help="page size, 1-100 (default: 50)")
    parser.add_argument("--cursor", help="resume from the cursor a previous page returned")
    parser.add_argument("--all", action="store_true",
                        help="follow cursors and return every item as one page")


def paged(args, operation, params):
    if args.limit is not None:
        params["limit"] = args.limit
    if args.cursor is not None:
        params["cursor"] = args.cursor
    return Request(operation, params, all_pages=args.all)


def add_desc(parser):
    """Reverse a listing. The default order is the one PL8 documents for it."""
    parser.add_argument("--desc", action="store_true",
                        help="reverse the order, newest or highest first")


def descending(args, params):
    # Left out unless asked for, so PL8's default order is PL8's to state.
    if args.desc:
        params["ascending"] = False
    return params


def add_space_option(parser):
    parser.add_argument("--space", help="space for bare issue ids (default: $PL8_SPACE)")


def default_space(args):
    if space := args.space or os.environ.get("PL8_SPACE"):
        return space
    raise UsageError("No space: pass --space, set PL8_SPACE, or name the Issue "
                     "as SPACE/ISSUE_ID")


def issue_ref(args, ref):
    """Split SPACE/ISSUE_ID, or pair a bare id with the default space."""
    space, sep, issue_id = ref.partition("/")
    if sep:
        return space, issue_id
    return default_space(args), ref


def add_issue_ref(parser):
    parser.add_argument("issue", metavar="SPACE/ISSUE_ID",
                        help="the Issue, as SPACE/ISSUE_ID or a bare ISSUE_ID")


# pl8 space

def add_space(subparsers, common):
    space = subparsers.add_parser("space", help="create, read, update and delete Spaces")
    verbs = space.add_subparsers(title="commands", metavar="COMMAND", required=True)

    parser = verbs.add_parser("create", parents=[common], help="create a Space",
                              description="Create a Space. Fails with DDBExistsError "
                                          "if the id is taken.")
    parser.add_argument("space_id", help="1-64 characters from [A-Za-z0-9_-]")
    parser.add_argument("--name", required=True)
    add_long_text(parser, "description")
    add_creator(parser)
    parser.set_defaults(build=lambda args: Request("create_space", {
        "space_id": args.space_id, "name": args.name,
        "description": long_text(args, "description"),
        "creator": default_creator(args)}))

    parser = verbs.add_parser("get", parents=[common], help="get a Space")
    parser.add_argument("space_id")
    parser.set_defaults(build=lambda args: Request("get_space", {"space_id": args.space_id}))

    parser = verbs.add_parser("list", parents=[common], help="list Spaces, by id")
    add_paging(parser)
    parser.set_defaults(build=lambda args: paged(args, "get_spaces", {}))

    parser = verbs.add_parser("update", parents=[common], help="update a Space",
                              description="Replace a Space's name, description, "
                                          "or both; whichever is left out is "
                                          "unchanged.")
    parser.add_argument("space_id")
    parser.add_argument("--name")
    add_long_text(parser, "description", required=False)
    add_version(parser)
    parser.set_defaults(build=lambda args: Request("update_space", versioned(args, {
        "space_id": args.space_id, **update_fields(args, "name")})))

    parser = verbs.add_parser("delete", parents=[common], help="delete a Space",
                              description="Delete a Space. Fails with "
                                          "DDBSpaceNotEmptyError while it has any "
                                          "Issues; delete them first.")
    parser.add_argument("space_id")
    parser.set_defaults(build=lambda args: Request("delete_space", {"space_id": args.space_id}))


# pl8 issue

def add_issue(subparsers, common):
    issue = subparsers.add_parser("issue", help="create, read, update, transition and "
                                                "delete Issues")
    verbs = issue.add_subparsers(title="commands", metavar="COMMAND", required=True)

    parser = verbs.add_parser("create", parents=[common], help="create an Issue",
                              description="Create an Issue in --space (or $PL8_SPACE). "
                                          "Fails with DDBMissingError if the Space "
                                          "doesn't exist; create it first.")
    add_space_option(parser)
    parser.add_argument("--title", required=True)
    add_long_text(parser, "description")
    parser.add_argument("--status", choices=STATUSES, default="TODO",
                        help="initial status (default: TODO)")
    add_creator(parser)
    parser.set_defaults(build=lambda args: Request("create_issue", {
        "space_id": default_space(args), "title": args.title,
        "description": long_text(args, "description"), "status": args.status,
        "creator": default_creator(args)}))

    parser = verbs.add_parser("get", parents=[common], help="get an Issue")
    add_issue_ref(parser)
    add_space_option(parser)
    parser.set_defaults(build=lambda args: Request("get_issue", issue_params(args)))

    parser = verbs.add_parser("list", parents=[common],
                              help="list a space's Issues in one status",
                              description="List the Issues in --space (or $PL8_SPACE) "
                                          "with a status, longest in that status "
                                          "first.")
    add_space_option(parser)
    parser.add_argument("--status", choices=STATUSES, required=True)
    add_paging(parser)
    parser.set_defaults(build=lambda args: paged(args, "get_issues_by_status", {
        "space_id": default_space(args), "status": args.status}))

    parser = verbs.add_parser("update", parents=[common], help="update an Issue",
                              description="Replace an Issue's title, description, "
                                          "or both; whichever is left out is "
                                          "unchanged.")
    add_issue_ref(parser)
    add_space_option(parser)
    parser.add_argument("--title")
    add_long_text(parser, "description", required=False)
    add_version(parser)
    parser.set_defaults(build=build_issue_update)

    parser = verbs.add_parser("transition", parents=[common],
                              help="move an Issue to a status",
                              description="Move an Issue to a status. DONE is final, "
                                          "and an Issue can't leave BLOCKED while it "
                                          "has active blockers.")
    add_issue_ref(parser)
    add_space_option(parser)
    parser.add_argument("--status", choices=STATUSES, required=True)
    add_version(parser)
    parser.set_defaults(build=lambda args: Request("transition_issue", versioned(args, {
        **issue_params(args), "status": args.status})))

    parser = verbs.add_parser("delete", parents=[common], help="delete an Issue",
                              description="Delete an Issue. Blockers naming it are "
                                          "removed in the background.")
    add_issue_ref(parser)
    add_space_option(parser)
    parser.set_defaults(build=lambda args: Request("delete_issue", issue_params(args)))


def issue_params(args):
    space_id, issue_id = issue_ref(args, args.issue)
    return {"space_id": space_id, "issue_id": issue_id}


# pl8 comment

def add_comment(subparsers, common):
    comment = subparsers.add_parser("comment", help="add, read, update and delete "
                                                    "comments on an Issue")
    verbs = comment.add_subparsers(title="commands", metavar="COMMAND", required=True)

    parser = verbs.add_parser("add", parents=[common], help="comment on an Issue",
                              description="Comment on an Issue, whatever its status: "
                                          "DONE stops an Issue moving to another "
                                          "status, not the discussion. PL8 generates "
                                          "the comment's id.")
    add_issue_ref(parser)
    add_space_option(parser)
    add_long_text(parser, "body")
    add_creator(parser)
    parser.set_defaults(build=lambda args: Request("create_issue_comment", {
        **issue_params(args), "body": long_text(args, "body"),
        "creator": default_creator(args)}))

    parser = verbs.add_parser("get", parents=[common], help="get one comment")
    add_comment_ref(parser)
    add_space_option(parser)
    parser.set_defaults(build=lambda args: Request("get_issue_comment",
                                                   comment_params(args)))

    parser = verbs.add_parser("list", parents=[common],
                              help="list an Issue's comments, oldest first")
    add_issue_ref(parser)
    add_space_option(parser)
    add_desc(parser)
    add_paging(parser)
    parser.set_defaults(build=lambda args: paged(args, "get_issue_comments",
                                                 descending(args, issue_params(args))))

    parser = verbs.add_parser(
        "wait", parents=[common], help="wait for new comments on an Issue",
        description="Poll for comments after --after until some arrive or "
                    "--max-wait runs out. The waiting happens here and not in "
                    "PL8: a Lambda that blocked would bill the wall clock for "
                    "doing nothing. Nothing arriving is not a failure, so this "
                    "exits 0 with an empty items list and a loop can simply "
                    "call it again. One JSON document is printed when the wait "
                    "ends, never a comment at a time.")
    add_issue_ref(parser)
    add_space_option(parser)
    parser.add_argument("--after", metavar="COMMENT_ID",
                        help="return the comments after this one (default: from "
                             "the start of the thread)")
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL,
                        metavar="SECONDS",
                        help="seconds between polls, clamped to "
                             f"{MIN_POLL_INTERVAL:g}-{MAX_POLL_INTERVAL:g} and "
                             f"jittered (default: {DEFAULT_POLL_INTERVAL:g})")
    parser.add_argument("--max-wait", type=float, default=DEFAULT_MAX_WAIT,
                        metavar="SECONDS",
                        help="stop waiting after this long, and report what "
                             f"arrived (default: {DEFAULT_MAX_WAIT:g})")
    add_paging(parser)
    parser.set_defaults(build=build_comment_wait)

    parser = verbs.add_parser("update", parents=[common], help="update a comment",
                              description="Replace a comment's body. Its id, creator "
                                          "and place in the thread are fixed at "
                                          "creation.")
    add_comment_ref(parser)
    add_space_option(parser)
    add_long_text(parser, "body")
    add_version(parser)
    parser.set_defaults(build=lambda args: Request("update_issue_comment", versioned(
        args, {**comment_params(args), "body": long_text(args, "body")})))

    parser = verbs.add_parser("delete", parents=[common], help="delete a comment",
                              description="Delete one comment. Deleting the Issue "
                                          "deletes all of them, in the background.")
    add_comment_ref(parser)
    add_space_option(parser)
    parser.set_defaults(build=lambda args: Request("delete_issue_comment",
                                                   comment_params(args)))


def add_comment_ref(parser):
    add_issue_ref(parser)
    parser.add_argument("comment_id", metavar="COMMENT_ID",
                        help="the comment's id, as PL8 generated it")


def comment_params(args):
    return {**issue_params(args), "comment_id": args.comment_id}


# Polling for comments happens on this side: pl8-interface is a Lambda, and a
# function that sat waiting would bill the wall clock to do it.
MIN_POLL_INTERVAL = 5.0
MAX_POLL_INTERVAL = 60.0
DEFAULT_POLL_INTERVAL = 15.0
DEFAULT_MAX_WAIT = 300.0
# How much of the interval the jitter may add on top of it.
POLL_JITTER = 0.25


def poll_interval(seconds):
    """The interval to poll at: clamped, not rejected.

    Either bound is a fine answer to "as fast as you can" or "hardly ever",
    and failing the command over a number that has a sensible nearest value
    helps nobody.
    """
    return min(max(seconds, MIN_POLL_INTERVAL), MAX_POLL_INTERVAL)


def poll_sleep(interval):
    """How long to sleep before the next poll.

    Jitter is added to the interval, never subtracted, so the clamped floor
    stays a floor: what it buys is that agents which started polling one
    Issue together stop re-colliding in lockstep, the same reason pl8-base's
    retry_on_transaction_conflict jitters its backoff. Like that one, this is
    not security sensitive, so random and not secrets.
    """
    return interval + random.uniform(0, POLL_JITTER * interval)


def build_issue_update(args):
    return Request("update_issue", versioned(args, {
        **issue_params(args), **update_fields(args, "title")}))


def build_comment_wait(args):
    params = issue_params(args)
    if args.after is not None:
        params["last_comment_id"] = args.after
    return comment_wait(paged(args, "get_issue_comments_after", params),
                        poll_interval(args.interval), max(args.max_wait, 0.0))


def comment_wait(request, interval, max_wait):
    """Poll until comments arrive or the wait runs out; one envelope either way.

    Every poll's answer is buffered rather than printed, because stdout is one
    JSON document and a comment printed as it arrived would end that.
    """
    deadline = time.monotonic() + max_wait
    while True:
        envelope = yield request
        if not envelope["ok"]:
            return envelope
        if not is_page(envelope["data"]):
            return error(INVOKE_ERROR, "Response is not a page of items")
        if envelope["data"]["items"]:
            return envelope
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Waited, nothing came. That is an answer, not a failure: the
            # empty page exits 0 so a caller can loop on this command.
            return envelope
        time.sleep(min(remaining, poll_sleep(interval)))


# pl8 attachment

# What pl8-interface will sign an upload for. A bigger file is refused here,
# before sending a request that could only come back rejected.
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
# What PL8 calls an attachment whose row exists but whose object does not,
# yet. Only ever reported, never compared against: a download URL PL8 left
# null is what says there is nothing to download.
PENDING = "PENDING"


def add_attachment(subparsers, common):
    attachment = subparsers.add_parser(
        "attachment", help="upload, download, list and delete the files "
                           "attached to an Issue or a comment")
    verbs = attachment.add_subparsers(title="commands", metavar="COMMAND",
                                      required=True)

    parser = verbs.add_parser(
        "add", parents=[common], help="upload a file as an attachment",
        description="Upload a file, in one command: PL8 reserves the "
                    "attachment and signs an upload, the CLI streams the file "
                    "to S3, then PL8 marks it uploaded. One envelope is "
                    "printed at the end, and its data names the attachment. "
                    "The size comes from the file itself, never from you, "
                    "since it is what the upload is signed for. If a step "
                    "after the first fails, the error carries the "
                    "attachment_id: the row exists from then on, and being "
                    "told which one it is means you can retry the upload "
                    "(`pl8 invoke resign_issue_attachment_upload`) or delete "
                    "it, instead of leaving behind a PENDING attachment that "
                    "nobody can name.")
    add_issue_ref(parser)
    add_space_option(parser)
    parser.add_argument("--file", metavar="PATH", required=True,
                        help="the file to upload, at most "
                             f"{MAX_ATTACHMENT_BYTES // 1024 // 1024}MB")
    parser.add_argument("--comment", metavar="COMMENT_ID",
                        help="attach to this comment on the Issue, rather than "
                             "to the Issue itself")
    parser.add_argument("--name",
                        help="name to record (default: the file's basename)")
    parser.add_argument("--content-type", metavar="TYPE",
                        help="content type to record (default: guessed from the "
                             "name, else application/octet-stream)")
    add_creator(parser)
    parser.set_defaults(build=build_attachment_add)

    parser = verbs.add_parser(
        "get", parents=[common], help="download an attachment to a file",
        description="Download an attachment. --output is required and the "
                    "presigned URL is never printed: stdout carries the "
                    "envelope, and that URL is a live five-minute bearer token "
                    "for the object, which is not a thing to leave in terminal "
                    "scrollback or an agent transcript. `pl8 invoke "
                    "get_issue_attachment` is there for anyone who does want "
                    "the URL. The file is written through a temporary file in "
                    "PATH's own directory and renamed into place, so a failed "
                    "download never leaves a truncated file at PATH.")
    add_attachment_ref(parser)
    add_space_option(parser)
    parser.add_argument("--output", metavar="PATH", required=True,
                        help='where to write the file; "-" is refused, since '
                             "stdout carries the envelope")
    parser.add_argument("--force", action="store_true",
                        help="overwrite PATH if it already exists")
    parser.set_defaults(build=build_attachment_get)

    parser = verbs.add_parser(
        "list", parents=[common],
        help="list an Issue's attachments, or one comment's",
        description="List the attachments on an Issue, or on one of its "
                    "comments with --comment, oldest first.")
    add_issue_ref(parser)
    add_space_option(parser)
    parser.add_argument("--comment", metavar="COMMENT_ID",
                        help="list this comment's attachments rather than the "
                             "Issue's")
    add_desc(parser)
    add_paging(parser)
    parser.set_defaults(build=build_attachment_list)

    parser = verbs.add_parser(
        "delete", parents=[common], help="delete an attachment",
        description="Delete an attachment. Its S3 object is removed in the "
                    "background.")
    add_attachment_ref(parser)
    add_space_option(parser)
    parser.set_defaults(build=lambda args: Request("delete_issue_attachment",
                                                   attachment_params(args)))


def add_attachment_ref(parser):
    add_issue_ref(parser)
    parser.add_argument("attachment_id", metavar="ATTACHMENT_ID",
                        help="the attachment's id, as PL8 generated it")


def attachment_params(args):
    return {**issue_params(args), "attachment_id": args.attachment_id}


def build_attachment_list(args):
    params = issue_params(args)
    if args.comment is not None:
        return paged(args, "get_issue_comment_attachments",
                     descending(args, {**params, "comment_id": args.comment}))
    return paged(args, "get_issue_attachments", descending(args, params))


def build_attachment_add(args):
    size = attachment_size(args.file)
    name = args.name or os.path.basename(args.file)
    params = {**issue_params(args), "name": name,
              "content_type": args.content_type or guess_content_type(name),
              "size": size, "creator": default_creator(args)}
    if args.comment is not None:
        params["comment_id"] = args.comment
    return attachment_add(params, args.file, size)


def attachment_size(path):
    """The file's size, read from the filesystem.

    A size the caller typed could disagree with the bytes that follow, and the
    one S3 holds the upload to is the one the request asked to sign, so this
    is not a number to take on trust. Stdin has no size at all.
    """
    if path == "-":
        raise UsageError("--file needs a real file, not stdin: an upload is "
                         "signed for the size the file is")
    try:
        info = os.stat(path)
    except OSError as exc:
        raise UsageError(f"Cannot read {path}: {exc.strerror}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise UsageError(f"Not a regular file: {path}")
    if info.st_size > MAX_ATTACHMENT_BYTES:
        raise UsageError(f"{path} is {info.st_size} bytes; an attachment is at "
                         f"most {MAX_ATTACHMENT_BYTES}")
    return info.st_size


def guess_content_type(name):
    """What the name says the file is, or the type that says nothing."""
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def attachment_add(params, path, size):
    """Reserve the attachment, stream the file to S3, then confirm it."""
    envelope = yield Request("initiate_issue_attachment_upload", params)
    if not envelope["ok"]:
        return envelope
    initiate = initiated(envelope["data"])
    if initiate is None:
        return error(INVOKE_ERROR, "initiate_issue_attachment_upload did not "
                                   "return an attachment and a presigned "
                                   "upload")
    row, target = initiate
    attachment_id = row.get("attachment_id")
    if not isinstance(attachment_id, str):
        return error(INVOKE_ERROR, "The attachment "
                                   "initiate_issue_attachment_upload returned "
                                   "has no attachment_id")

    envelope = yield Upload(target, path, size, params["name"],
                            params["content_type"])
    if not envelope["ok"]:
        return stranded(envelope, attachment_id)

    envelope = yield Request("confirm_issue_attachment_uploaded", {
        "space_id": params["space_id"], "issue_id": params["issue_id"],
        "attachment_id": attachment_id})
    if not envelope["ok"]:
        return stranded(envelope, attachment_id)
    # Confirming reports nothing of its own, and the id this command just
    # created is what names the attachment from here on, so say it either way.
    reported = envelope["data"] if isinstance(envelope["data"], dict) else {}
    return {"ok": True, "data": {"attachment_id": attachment_id, **reported}}


def stranded(envelope, attachment_id):
    """A failure with the attachment it left behind named in it.

    Everything after the first step has a row already, so an agent that is
    told the id can re-upload to it or delete it. Without the id it cannot do
    either, and a PENDING attachment sits there unreferenced.
    """
    return {"ok": False,
            "error": {**envelope["error"], "attachment_id": attachment_id}}


def build_attachment_get(args):
    path = args.output
    if path == "-":
        raise UsageError("--output cannot be -: stdout carries the JSON "
                         "envelope, so the file needs a path of its own")
    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        raise UsageError(f"No such directory: {directory}")
    if os.path.isdir(path):
        raise UsageError(f"{path} is a directory")
    if os.path.lexists(path) and not args.force:
        raise UsageError(f"{path} already exists; pass --force to overwrite it")
    return attachment_get(attachment_params(args), path)


def attachment_get(params, path):
    """Read the attachment, then fetch its object here rather than print a URL."""
    envelope = yield Request("get_issue_attachment", params)
    if not envelope["ok"]:
        return envelope
    read = attached(envelope["data"])
    if read is None:
        return error(INVOKE_ERROR, "get_issue_attachment did not return an "
                                   "attachment and a download URL")
    row, url = read
    if url is None:
        # PL8 signs a download only once there is an object to download, so a
        # null URL is its answer about this attachment, not a broken response.
        # The status is what says why, which is worth more than "no URL".
        return error(ATTACHMENT_PENDING,
                     f"Attachment {params['attachment_id']} is "
                     f"{row.get('status', PENDING)}: its upload has not "
                     "completed, so it has no object to download")

    envelope = yield Download(url, path)
    if not envelope["ok"]:
        return envelope
    return {"ok": True, "data": {**row, "path": path}}


# What the attachment operations return. pl8-base hands pl8-interface a pair
# and pl8-interface unpacks it into named keys, so each of these reads exactly
# the keys that operation sends and nothing else: a response shaped some other
# way is a mismatch between the CLI and pl8-interface, which is worth
# reporting as a fault rather than working around.

def initiated(data):
    """(attachment, upload) from an initiated upload, or None if it isn't one.

    The upload is the presigned POST, {"url", "fields"}.
    """
    if not isinstance(data, dict):
        return None
    row, target = data.get("attachment"), data.get("upload")
    if not isinstance(row, dict) or not isinstance(target, dict):
        return None
    if (not isinstance(target.get("url"), str)
            or not isinstance(target.get("fields"), dict)):
        return None
    return row, target


def attached(data):
    """(attachment, download_url) from get_issue_attachment, or None.

    The URL is null for an attachment whose upload has not completed. That is
    an answer about the attachment rather than a malformed response, so it
    comes back as a None inside the pair; a response with no download_url at
    all comes back as no pair.
    """
    if not isinstance(data, dict) or "download_url" not in data:
        return None
    row, url = data.get("attachment"), data["download_url"]
    if not isinstance(row, dict) or not isinstance(url, (str, type(None))):
        return None
    return row, url


# pl8 blocker

def add_blocker(subparsers, common):
    blocker = subparsers.add_parser("blocker", help="add, remove and list blocking "
                                                    "relationships between Issues")
    verbs = blocker.add_subparsers(title="commands", metavar="COMMAND", required=True)

    parser = verbs.add_parser("add", parents=[common], help="make one Issue block another",
                              description="Make --blocking block --blocked, which moves "
                                          "the blocked Issue to BLOCKED. It returns to "
                                          "TODO, in the background, once every Issue "
                                          "blocking it is DONE or deleted. The blocking "
                                          "Issue can't be DONE.")
    add_blocker_pair(parser)
    parser.set_defaults(build=lambda args: Request("add_issue_blocker",
                                                   blocker_params(args)))

    parser = verbs.add_parser("remove", parents=[common],
                              help="remove a blocking relationship")
    add_blocker_pair(parser)
    parser.set_defaults(build=lambda args: Request("delete_issue_blocker",
                                                   blocker_params(args)))

    parser = verbs.add_parser("list", parents=[common],
                              help="list what blocks an Issue, or what it blocks")
    add_space_option(parser)
    side = parser.add_mutually_exclusive_group(required=True)
    side.add_argument("--blocked", metavar="SPACE/ISSUE_ID",
                      help="list the IssueBlockers blocking this Issue")
    side.add_argument("--blocking", metavar="SPACE/ISSUE_ID",
                      help="list the IssueBlockers where this Issue is the blocker")
    add_paging(parser)
    parser.set_defaults(build=build_blocker_list)


def add_blocker_pair(parser):
    add_space_option(parser)
    parser.add_argument("--blocking", metavar="SPACE/ISSUE_ID", required=True,
                        help="the Issue that blocks")
    parser.add_argument("--blocked", metavar="SPACE/ISSUE_ID", required=True,
                        help="the Issue that is blocked")


def blocker_params(args):
    blocking_space, blocking_id = issue_ref(args, args.blocking)
    blocked_space, blocked_id = issue_ref(args, args.blocked)
    return {"blocking_issue_space_id": blocking_space, "blocking_issue_id": blocking_id,
            "blocked_issue_space_id": blocked_space, "blocked_issue_id": blocked_id}


def build_blocker_list(args):
    if args.blocked is not None:
        space_id, issue_id = issue_ref(args, args.blocked)
        return paged(args, "get_issue_blockers",
                     {"space_id": space_id, "blocked_issue_id": issue_id})
    space_id, issue_id = issue_ref(args, args.blocking)
    return paged(args, "get_issue_blocking",
                 {"space_id": space_id, "blocking_issue_id": issue_id})


def build_parser():
    common = common_options()
    parser = ArgumentParser(
        prog="pl8", parents=[common],
        description="Agent-friendly CLI for PL8. Prints one JSON envelope on "
                    "stdout; exit status 0 ok, 1 rejected by PL8, 2 usage "
                    "error, 3 transport or server fault, 4 transient conflict "
                    "(retry unchanged).")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {version('pl8-cli')}")
    subparsers = parser.add_subparsers(title="commands", metavar="COMMAND",
                                       required=True)
    add_space(subparsers, common)
    add_issue(subparsers, common)
    add_comment(subparsers, common)
    add_attachment(subparsers, common)
    add_blocker(subparsers, common)
    add_invoke(subparsers, common)
    return parser


def resolve_function_name(args):
    """Flags win over environment variables; a function name wins over an env."""
    if name := getattr(args, "function_name", None):
        return name
    if env := getattr(args, "env", None):
        return env + FUNCTION_SUFFIX
    if name := os.environ.get("PL8_FUNCTION_NAME"):
        return name
    if env := os.environ.get("PL8_ENV"):
        return env + FUNCTION_SUFFIX
    raise UsageError("No function to invoke: pass --env or --function-name, "
                     "or set PL8_ENV or PL8_FUNCTION_NAME")


def parse(argv):
    """Parse argv into (args, plan) without touching AWS or the network.

    A plan is one Request, or a generator of the steps a multi-step command
    takes; see drive(). Anything wrong with the command line, including a file
    that can't be read or a path that can't be written, is a UsageError raised
    here, before anything has been sent.
    """
    args = build_parser().parse_args(argv)
    return args, args.build(args)


def exit_code(envelope):
    if envelope["ok"]:
        return EXIT_OK
    if envelope["error"]["type"] in FAULT_ERRORS:
        return EXIT_FAULT
    if envelope["error"]["type"] in TRANSIENT_ERRORS:
        return EXIT_TRANSIENT
    return EXIT_REJECTED


def collect_pages(invoker, request):
    """Follow cursors to the end; one envelope holding every page's items.

    Stops at the first failed page and returns that failure, since a partial
    list would read as a complete one.
    """
    params = request.params
    items = []
    while True:
        envelope = invoker.invoke(request.operation, params)
        if not envelope["ok"]:
            return envelope
        if not is_page(envelope["data"]):
            return error(INVOKE_ERROR, "Response is not a page of items")
        items.extend(envelope["data"]["items"])
        if envelope["data"]["cursor"] is None:
            return {"ok": True, "data": {"items": items, "cursor": None}}
        params = {**params, "cursor": envelope["data"]["cursor"]}


def is_page(data):
    return (isinstance(data, dict) and isinstance(data.get("items"), list)
            and isinstance(data.get("cursor"), (str, type(None))))


def drive(plan, perform_step):
    """Carry out what parse() produced and return the envelope to print.

    A multi-step command is a generator: it yields a step and gets that step's
    envelope back, and the envelope it returns is the one to print. Keeping
    the steps as data, and the doing of them in perform_step, is what lets the
    tests watch every operation such a command sends without reaching AWS or
    S3.
    """
    if isinstance(plan, Request):
        return perform_step(plan)
    envelope = None
    while True:
        try:
            step = plan.send(envelope)
        except StopIteration as done:
            return done.value
        envelope = perform_step(step)


def perform(invoker, step):
    """Do one step, and report how it went as an envelope.

    An S3 transfer answers in the same shape as an operation, so a command
    that mixes the two is still a sequence of steps with one kind of answer.
    """
    if isinstance(step, Request):
        if step.all_pages:
            return collect_pages(invoker, step)
        return invoker.invoke(step.operation, step.params)
    try:
        if isinstance(step, Upload):
            upload(step)
        else:
            download(step)
    except TransferError as exc:
        return error(TRANSFER_ERROR, str(exc))
    return {"ok": True, "data": None}


def upload(step):
    upload_file(step.target, step.path, size=step.size, name=step.name,
                content_type=step.content_type)


def download(step):
    """Fetch the object through a temporary file beside its destination.

    A download that fails, or is cut off part-way, leaves nothing at the
    destination: the bytes go to a temporary file in the same directory and
    are renamed over it only once the transfer is done, and the temporary file
    is removed either way. Same directory, so the rename is atomic rather than
    a copy. The file keeps the owner-only permissions mkstemp gives it.
    """
    directory = os.path.dirname(step.path) or "."
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".pl8-",
                                         suffix=".part")
    try:
        with open(handle, "wb") as out:
            download_file(step.url, out)
        os.replace(temporary, step.path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def emit(envelope, *, pretty=False):
    print(json.dumps(envelope, indent=2 if pretty else None))


def main(argv=None, *, make_invoker=Invoker):
    try:
        args, plan = parse(argv)
        invoker = make_invoker(resolve_function_name(args),
                               profile=getattr(args, "profile", None),
                               region=getattr(args, "region", None))
    except (UsageError, ProfileNotFound) as exc:
        emit(error(USAGE_ERROR, str(exc)))
        return EXIT_USAGE

    envelope = drive(plan, lambda step: perform(invoker, step))
    emit(envelope, pretty=getattr(args, "pretty", False))
    return exit_code(envelope)
