# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""Envelope output, exit codes, configuration, and the raw invoke command."""

import io
import json

import pytest

from pl8_cli import cli


def usage_error(result):
    code, out, invoker = result
    assert code == cli.EXIT_USAGE
    assert out["ok"] is False
    assert out["error"]["type"] == cli.USAGE_ERROR
    assert invoker.calls == []
    return out["error"]["message"]


@pytest.mark.parametrize(("error_type", "expected"), [
    ("DDBMissingError", cli.EXIT_REJECTED),
    ("DDBVersionConflictError", cli.EXIT_REJECTED),
    ("InvalidParams", cli.EXIT_REJECTED),
    ("UnknownOperation", cli.EXIT_REJECTED),
    ("DDBInternalError", cli.EXIT_FAULT),
    ("DDBCorruptedError", cli.EXIT_FAULT),
    ("InvokeError", cli.EXIT_FAULT),
    ("FunctionError", cli.EXIT_FAULT),
])
def test_failure_exit_codes(run, error_type, expected):
    envelope = {"ok": False, "error": {"type": error_type, "message": "m"}}
    code, out, _ = run(["--env", "dev", "invoke", "get_space"], [envelope])

    assert code == expected
    assert out == envelope


def test_success_prints_envelope(run):
    envelope = {"ok": True, "data": {"space_id": "ENG"}}
    code, out, _ = run(["--env", "dev", "invoke", "get_space"], [envelope])

    assert code == cli.EXIT_OK
    assert out == envelope


def test_output_is_one_line_unless_pretty(run):
    envelope = {"ok": True, "data": {"a": 1}}

    _, out, _ = run(["--env", "dev", "invoke", "get_space"], [envelope], raw=True)
    assert out == json.dumps(envelope) + "\n"

    _, out, _ = run(["--env", "dev", "invoke", "get_space", "--pretty"], [envelope], raw=True)
    assert out == json.dumps(envelope, indent=2) + "\n"


# Configuration

@pytest.mark.parametrize(("argv", "environ", "expected"), [
    (["--env", "dev"], {}, "dev-pl8-interface"),
    (["--function-name", "fn"], {}, "fn"),
    (["--env", "dev", "--function-name", "fn"], {}, "fn"),
    ([], {"PL8_ENV": "prod"}, "prod-pl8-interface"),
    ([], {"PL8_FUNCTION_NAME": "fn"}, "fn"),
    ([], {"PL8_ENV": "prod", "PL8_FUNCTION_NAME": "fn"}, "fn"),
    # A flag beats either variable.
    (["--env", "dev"], {"PL8_FUNCTION_NAME": "fn"}, "dev-pl8-interface"),
])
def test_function_name_resolution(run, monkeypatch, argv, environ, expected):
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    _, _, invoker = run([*argv, "invoke", "get_space"])

    assert invoker.config["function_name"] == expected


def test_no_function_configured_is_usage_error(run):
    assert "PL8_ENV" in usage_error(run(["invoke", "get_space"]))


def test_common_options_before_or_after_the_command(run):
    for argv in (["--env", "dev", "--profile", "p", "--region", "r", "invoke", "get_space"],
                 ["invoke", "get_space", "--env", "dev", "--profile", "p", "--region", "r"]):
        _, _, invoker = run(argv)
        assert invoker.config == {"function_name": "dev-pl8-interface",
                                  "profile": "p", "region": "r"}


def test_missing_command_is_usage_error(run):
    usage_error(run(["--env", "dev"]))


def test_unknown_flag_is_usage_error(run):
    usage_error(run(["--env", "dev", "invoke", "get_space", "--nope"]))


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("pl8 ")


# pl8 invoke

def test_invoke_defaults_to_empty_params(run):
    _, _, invoker = run(["--env", "dev", "invoke", "get_spaces"])
    assert invoker.calls == [("get_spaces", {})]


def test_invoke_params(run):
    _, _, invoker = run(["--env", "dev", "invoke", "get_space",
                         "--params", '{"space_id": "ENG"}'])
    assert invoker.calls == [("get_space", {"space_id": "ENG"})]


def test_invoke_params_file(run, tmp_path):
    path = tmp_path / "params.json"
    path.write_text('{"space_id": "ENG"}')
    _, _, invoker = run(["--env", "dev", "invoke", "get_space", "--params-file", str(path)])
    assert invoker.calls == [("get_space", {"space_id": "ENG"})]


def test_invoke_params_from_stdin(run, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"space_id": "ENG"}'))
    _, _, invoker = run(["--env", "dev", "invoke", "get_space", "--params-file", "-"])
    assert invoker.calls == [("get_space", {"space_id": "ENG"})]


def test_invoke_passes_unknown_operations_through(run):
    # pl8-interface owns the allow-list; the CLI doesn't second-guess it.
    _, _, invoker = run(["--env", "dev", "invoke", "brand_new_op"])
    assert invoker.calls == [("brand_new_op", {})]


@pytest.mark.parametrize("params", ["{", "[1]", '"s"', "null"])
def test_invoke_rejects_params_that_are_not_an_object(run, params):
    usage_error(run(["--env", "dev", "invoke", "get_space", "--params", params]))


def test_invoke_missing_params_file(run, tmp_path):
    message = usage_error(run(["--env", "dev", "invoke", "get_space",
                               "--params-file", str(tmp_path / "missing.json")]))
    assert "missing.json" in message


def test_invoke_params_and_params_file_are_exclusive(run, tmp_path):
    usage_error(run(["--env", "dev", "invoke", "get_space",
                     "--params", "{}", "--params-file", "-"]))
