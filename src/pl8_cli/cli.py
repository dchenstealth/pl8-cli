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
cross-space references need no extra flags.
"""

import argparse
import json
import os
import sys
from importlib.metadata import version
from typing import NamedTuple

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

USAGE_ERROR = "UsageError"
# Error types that mean the server, not the request, is at fault.
# DDBCorruptedError subclasses DDBInternalError but is reported by name.
FAULT_ERRORS = frozenset({
    INVOKE_ERROR,
    FUNCTION_ERROR,
    "DDBInternalError",
    "DDBCorruptedError",
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
        return sys.stdin.read()
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

def add_description(parser):
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--description", help="description text")
    group.add_argument("--description-file", metavar="PATH",
                       help='read the description from PATH ("-" for stdin); '
                            "avoids shell quoting for long or multi-line text")


def description(args):
    if args.description_file is not None:
        return read_text(args.description_file)
    return args.description


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


def add_issue_ref(parser, name="issue"):
    parser.add_argument(name, metavar="SPACE/ISSUE_ID",
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
    add_description(parser)
    parser.set_defaults(build=lambda args: Request("create_space", {
        "space_id": args.space_id, "name": args.name,
        "description": description(args)}))

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
    add_description(parser)
    add_version(parser)
    parser.set_defaults(build=lambda args: Request("update_space", versioned(args, {
        "space_id": args.space_id, "name": args.name,
        "description": description(args)})))

    parser = verbs.add_parser("delete", parents=[common], help="delete a Space",
                              description="Delete a Space. Its Issues are kept, and "
                                          "a Space recreated with the same id takes "
                                          "them up again.")
    parser.add_argument("space_id")
    parser.set_defaults(build=lambda args: Request("delete_space", {"space_id": args.space_id}))


# pl8 issue

def add_issue(subparsers, common):
    issue = subparsers.add_parser("issue", help="create, read, update, transition and "
                                                "delete Issues")
    verbs = issue.add_subparsers(title="commands", metavar="COMMAND", required=True)

    parser = verbs.add_parser("create", parents=[common], help="create an Issue",
                              description="Create an Issue in --space (or $PL8_SPACE). "
                                          "The Space need not exist.")
    add_space_option(parser)
    parser.add_argument("--title", required=True)
    add_description(parser)
    parser.add_argument("--status", choices=STATUSES, default="TODO",
                        help="initial status (default: TODO)")
    parser.set_defaults(build=lambda args: Request("create_issue", {
        "space_id": default_space(args), "title": args.title,
        "description": description(args), "status": args.status}))

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
    add_description(parser)
    add_version(parser)
    parser.set_defaults(build=lambda args: Request("update_issue", versioned(args, {
        **issue_params(args), "title": args.title,
        "description": description(args)})))

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


def build_parser():
    common = common_options()
    parser = ArgumentParser(
        prog="pl8", parents=[common],
        description="Agent-friendly CLI for PL8. Prints one JSON envelope on "
                    "stdout; exit status 0 ok, 1 rejected by PL8, 2 usage "
                    "error, 3 transport or server fault.")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {version('pl8-cli')}")
    subparsers = parser.add_subparsers(title="commands", metavar="COMMAND",
                                       required=True)
    add_space(subparsers, common)
    add_issue(subparsers, common)
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
        items.extend(envelope["data"]["items"])
        if envelope["data"]["cursor"] is None:
            return {"ok": True, "data": {"items": items, "cursor": None}}
        params = {**params, "cursor": envelope["data"]["cursor"]}


def emit(envelope, *, pretty=False):
    print(json.dumps(envelope, indent=2 if pretty else None))


def main(argv=None, *, make_invoker=Invoker):
    try:
        args, request = parse(argv)
        function_name = resolve_function_name(args)
    except UsageError as exc:
        emit(error(USAGE_ERROR, str(exc)))
        return EXIT_USAGE

    invoker = make_invoker(function_name,
                           profile=getattr(args, "profile", None),
                           region=getattr(args, "region", None))
    if request.all_pages:
        envelope = collect_pages(invoker, request)
    else:
        envelope = invoker.invoke(request.operation, request.params)
    emit(envelope, pretty=getattr(args, "pretty", False))
    return exit_code(envelope)
