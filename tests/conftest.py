# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import json

import pytest

from pl8_cli import cli

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def aws_environment(monkeypatch):
    """Fake credentials, and no ambient PL8 settings, so nothing reaches real
    AWS and every test starts from the same configuration."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    for name in ("AWS_PROFILE", "PL8_ENV", "PL8_FUNCTION_NAME", "PL8_SPACE"):
        monkeypatch.delenv(name, raising=False)


class FakeInvoker:
    """Stands in for client.Invoker: records calls, replays queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.config = None

    def __call__(self, function_name, *, profile=None, region=None):
        self.config = {"function_name": function_name, "profile": profile,
                       "region": region}
        return self

    def invoke(self, operation, params):
        self.calls.append((operation, params))
        return self.responses.pop(0)


@pytest.fixture
def run(capsys):
    """Run the CLI against a FakeInvoker.

    Returns (exit code, parsed stdout, invoker), or the stdout text itself
    with raw=True. Responses default to one empty success.
    """
    def run(argv, responses=({"ok": True, "data": None},), *, raw=False):
        invoker = FakeInvoker(responses)
        code = cli.main(argv, make_invoker=invoker)
        out = capsys.readouterr().out
        return code, out if raw else json.loads(out), invoker
    return run
