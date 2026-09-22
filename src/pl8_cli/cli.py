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


class UsageError(Exception):
    pass


class Request(NamedTuple):
    operation: str
    params: dict


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
    envelope = invoker.invoke(request.operation, request.params)
    emit(envelope, pretty=getattr(args, "pretty", False))
    return exit_code(envelope)
