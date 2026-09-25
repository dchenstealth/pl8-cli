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

Issues are named SPACE/ISSUE_ID. A bare ISSUE_ID takes its space from
--space, then $PL8_SPACE; a space in the reference itself always wins, so
cross-space references need no extra flags. A comment is named by its Issue
and then its own id, as two arguments: a comment belongs to an Issue and is
not addressable without it.
"""

import argparse
import json
import os
import sys
from importlib.metadata import version
from typing import NamedTuple

from botocore.exceptions import ProfileNotFound

from pl8_cli.client import FUNCTION_ERROR, INVOKE_ERROR, Invoker, error

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
# Error types that mean the server, not the request, is at fault.
# DDBCorruptedError subclasses DDBInternalError but is reported by name.
FAULT_ERRORS = frozenset({
    INVOKE_ERROR,
    FUNCTION_ERROR,
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
    operation: str
    params: dict
    # Follow cursors and return every page's items as one page.
    all_pages: bool = False


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

def add_long_text(parser, name):
    """--NAME or --NAME-file, exactly one required.

    PL8's long text fields (an Issue's description, a comment's body) all take
    this pair, so the flag a caller learns for one works for the others.
    """
    group = parser.add_mutually_exclusive_group(required=True)
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
                              description="Replace a Space's name and description; "
                                          "pass both.")
    parser.add_argument("space_id")
    parser.add_argument("--name", required=True)
    add_long_text(parser, "description")
    add_version(parser)
    parser.set_defaults(build=lambda args: Request("update_space", versioned(args, {
        "space_id": args.space_id, "name": args.name,
        "description": long_text(args, "description")})))

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
                              description="Replace an Issue's title and description; "
                                          "pass both.")
    add_issue_ref(parser)
    add_space_option(parser)
    parser.add_argument("--title", required=True)
    add_long_text(parser, "description")
    add_version(parser)
    parser.set_defaults(build=lambda args: Request("update_issue", versioned(args, {
        **issue_params(args), "title": args.title,
        "description": long_text(args, "description")})))

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
    add_paging(parser)
    parser.set_defaults(build=lambda args: paged(args, "get_issue_comments",
                                                 issue_params(args)))

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
    """Parse argv into (args, Request) without touching AWS."""
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


def emit(envelope, *, pretty=False):
    print(json.dumps(envelope, indent=2 if pretty else None))


def main(argv=None, *, make_invoker=Invoker):
    try:
        args, request = parse(argv)
        invoker = make_invoker(resolve_function_name(args),
                               profile=getattr(args, "profile", None),
                               region=getattr(args, "region", None))
    except (UsageError, ProfileNotFound) as exc:
        emit(error(USAGE_ERROR, str(exc)))
        return EXIT_USAGE

    if request.all_pages:
        envelope = collect_pages(invoker, request)
    else:
        envelope = invoker.invoke(request.operation, request.params)
    emit(envelope, pretty=getattr(args, "pretty", False))
    return exit_code(envelope)
