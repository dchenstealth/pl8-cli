# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""How each subcommand's arguments become an operation and its params."""

import io

import pytest

from pl8_cli import cli


def page(items, cursor=None):
    return {"ok": True, "data": {"items": items, "cursor": cursor}}


@pytest.fixture
def call(run):
    """Run a subcommand against dev and return the one (operation, params) sent."""
    def call(*argv):
        code, out, invoker = run(["--env", "dev", *argv])
        assert code == cli.EXIT_OK, out
        [sent] = invoker.calls
        return sent
    return call


@pytest.fixture
def usage_error(run):
    def usage_error(*argv):
        code, out, invoker = run(["--env", "dev", *argv])
        assert code == cli.EXIT_USAGE
        assert out["error"]["type"] == cli.USAGE_ERROR
        assert invoker.calls == []
        return out["error"]["message"]
    return usage_error


# pl8 space

def test_space_create(call):
    assert call("space", "create", "ENG", "--name", "Engineering",
                "--description", "Eng work") == (
        "create_space", {"space_id": "ENG", "name": "Engineering",
                         "description": "Eng work"})


def test_space_get(call):
    assert call("space", "get", "ENG") == ("get_space", {"space_id": "ENG"})


def test_space_list(call):
    assert call("space", "list") == ("get_spaces", {})
    assert call("space", "list", "--limit", "10", "--cursor", "c1") == (
        "get_spaces", {"limit": 10, "cursor": "c1"})


def test_space_update(call):
    assert call("space", "update", "ENG", "--name", "N", "--description", "D") == (
        "update_space", {"space_id": "ENG", "name": "N", "description": "D"})
    assert call("space", "update", "ENG", "--name", "N", "--description", "D",
                "--if-version", "3") == (
        "update_space", {"space_id": "ENG", "name": "N", "description": "D",
                         "version": 3})


def test_space_delete(call):
    assert call("space", "delete", "ENG") == ("delete_space", {"space_id": "ENG"})


def test_space_commands_ignore_default_space(call, monkeypatch):
    monkeypatch.setenv("PL8_SPACE", "OPS")
    assert call("space", "get", "ENG") == ("get_space", {"space_id": "ENG"})


# pl8 issue

def test_issue_create(call):
    assert call("issue", "create", "--space", "ENG", "--title", "T",
                "--description", "D") == (
        "create_issue", {"space_id": "ENG", "title": "T", "description": "D",
                         "status": "TODO"})
    assert call("issue", "create", "--space", "ENG", "--title", "T",
                "--description", "D", "--status", "IN_PROGRESS")[1]["status"] == "IN_PROGRESS"


def test_issue_create_uses_default_space(call, monkeypatch):
    monkeypatch.setenv("PL8_SPACE", "OPS")
    assert call("issue", "create", "--title", "T", "--description", "D")[1]["space_id"] == "OPS"
    # The flag beats the variable.
    assert call("issue", "create", "--space", "ENG", "--title", "T",
                "--description", "D")[1]["space_id"] == "ENG"


def test_issue_create_needs_a_space(usage_error):
    assert "PL8_SPACE" in usage_error("issue", "create", "--title", "T", "--description", "D")


def test_issue_get(call):
    assert call("issue", "get", "ENG/abc123") == (
        "get_issue", {"space_id": "ENG", "issue_id": "abc123"})


def test_issue_list(call):
    assert call("issue", "list", "--space", "ENG", "--status", "TODO") == (
        "get_issues_by_status", {"space_id": "ENG", "status": "TODO"})
    assert call("issue", "list", "--space", "ENG", "--status", "DONE",
                "--limit", "5", "--cursor", "c1") == (
        "get_issues_by_status", {"space_id": "ENG", "status": "DONE",
                                 "limit": 5, "cursor": "c1"})


def test_issue_list_needs_a_status(usage_error):
    usage_error("issue", "list", "--space", "ENG")


def test_issue_status_must_be_known(usage_error):
    usage_error("issue", "list", "--space", "ENG", "--status", "done")


def test_issue_update(call):
    assert call("issue", "update", "ENG/abc123", "--title", "T", "--description", "D",
                "--if-version", "2") == (
        "update_issue", {"space_id": "ENG", "issue_id": "abc123", "title": "T",
                         "description": "D", "version": 2})


def test_issue_update_needs_both_fields(usage_error):
    usage_error("issue", "update", "ENG/abc123", "--title", "T")


def test_issue_transition(call):
    assert call("issue", "transition", "ENG/abc123", "--status", "DONE") == (
        "transition_issue", {"space_id": "ENG", "issue_id": "abc123", "status": "DONE"})
    assert call("issue", "transition", "ENG/abc123", "--status", "DONE",
                "--if-version", "4")[1]["version"] == 4


def test_issue_delete(call):
    assert call("issue", "delete", "ENG/abc123") == (
        "delete_issue", {"space_id": "ENG", "issue_id": "abc123"})


# pl8 blocker

BLOCKER_PARAMS = {"blocking_issue_space_id": "ENG", "blocking_issue_id": "aaa111",
                  "blocked_issue_space_id": "OPS", "blocked_issue_id": "bbb222"}


def test_blocker_add(call):
    assert call("blocker", "add", "--blocking", "ENG/aaa111", "--blocked", "OPS/bbb222") == (
        "add_issue_blocker", BLOCKER_PARAMS)


def test_blocker_remove(call):
    assert call("blocker", "remove", "--blocking", "ENG/aaa111",
                "--blocked", "OPS/bbb222") == ("delete_issue_blocker", BLOCKER_PARAMS)


def test_blocker_pair_mixes_references_and_default_space(call):
    # The explicit OPS reference doesn't conflict with --space ENG.
    assert call("blocker", "add", "--space", "ENG", "--blocking", "aaa111",
                "--blocked", "OPS/bbb222")[1] == BLOCKER_PARAMS


def test_blocker_add_needs_both_sides(usage_error):
    usage_error("blocker", "add", "--blocking", "ENG/aaa111")


def test_blocker_list_blocked(call):
    assert call("blocker", "list", "--blocked", "OPS/bbb222", "--limit", "5") == (
        "get_issue_blockers", {"space_id": "OPS", "blocked_issue_id": "bbb222", "limit": 5})


def test_blocker_list_blocking(call):
    assert call("blocker", "list", "--blocking", "aaa111", "--space", "ENG",
                "--cursor", "c1") == (
        "get_issue_blocking", {"space_id": "ENG", "blocking_issue_id": "aaa111",
                               "cursor": "c1"})


def test_blocker_list_needs_exactly_one_side(usage_error):
    usage_error("blocker", "list")
    usage_error("blocker", "list", "--blocked", "OPS/b", "--blocking", "ENG/a")


def test_blocker_list_all(run):
    _, out, invoker = run(["--env", "dev", "blocker", "list", "--blocked", "OPS/bbb222",
                           "--all"], [page([1], "c1"), page([2])])
    assert out == page([1, 2])
    assert len(invoker.calls) == 2


# Issue references

def test_bare_issue_id_takes_space_flag(call):
    assert call("issue", "get", "abc123", "--space", "ENG")[1] == {
        "space_id": "ENG", "issue_id": "abc123"}


def test_bare_issue_id_takes_space_variable(call, monkeypatch):
    monkeypatch.setenv("PL8_SPACE", "OPS")
    assert call("issue", "get", "abc123")[1] == {"space_id": "OPS", "issue_id": "abc123"}


def test_reference_space_beats_defaults(call, monkeypatch):
    monkeypatch.setenv("PL8_SPACE", "OPS")
    assert call("issue", "get", "ENG/abc123", "--space", "SEC")[1] == {
        "space_id": "ENG", "issue_id": "abc123"}


def test_bare_issue_id_without_space(usage_error):
    assert "SPACE/ISSUE_ID" in usage_error("issue", "get", "abc123")


# Descriptions

def test_description_file(call, tmp_path):
    path = tmp_path / "desc.md"
    path.write_text("Line one\n\n`quoted` $text\n", encoding="utf-8")
    assert call("space", "create", "ENG", "--name", "N", "--description-file",
                str(path))[1]["description"] == "Line one\n\n`quoted` $text\n"


def test_description_from_stdin(call, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert call("issue", "create", "--space", "ENG", "--title", "T",
                "--description-file", "-")[1]["description"] == "from stdin"


def test_description_is_required(usage_error):
    usage_error("space", "create", "ENG", "--name", "N")


def test_description_and_file_are_exclusive(usage_error):
    usage_error("space", "create", "ENG", "--name", "N", "--description", "D",
                "--description-file", "-")


def test_missing_description_file(usage_error, tmp_path):
    assert "nope.md" in usage_error("space", "create", "ENG", "--name", "N",
                                    "--description-file", str(tmp_path / "nope.md"))


# --all

def test_all_follows_cursors(run):
    code, out, invoker = run(
        ["--env", "dev", "space", "list", "--all", "--limit", "2"],
        [page([1, 2], "c1"), page([3, 4], "c2"), page([5])])

    assert code == cli.EXIT_OK
    assert out == page([1, 2, 3, 4, 5])
    assert invoker.calls == [
        ("get_spaces", {"limit": 2}),
        ("get_spaces", {"limit": 2, "cursor": "c1"}),
        ("get_spaces", {"limit": 2, "cursor": "c2"}),
    ]


def test_all_starts_from_given_cursor(run):
    _, _, invoker = run(["--env", "dev", "space", "list", "--all", "--cursor", "c0"],
                        [page([1])])
    assert invoker.calls == [("get_spaces", {"cursor": "c0"})]


def test_all_stops_at_failed_page(run):
    failure = {"ok": False, "error": {"type": "InvokeError", "message": "throttled"}}
    code, out, invoker = run(
        ["--env", "dev", "issue", "list", "--space", "ENG", "--status", "TODO", "--all"],
        [page([1], "c1"), failure])

    assert code == cli.EXIT_FAULT
    assert out == failure
    assert len(invoker.calls) == 2


def test_without_all_returns_one_page(run):
    _, out, invoker = run(["--env", "dev", "space", "list"], [page([1], "c1")])
    assert out == page([1], "c1")
    assert len(invoker.calls) == 1
