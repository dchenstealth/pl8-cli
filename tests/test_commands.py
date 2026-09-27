# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""How each subcommand's arguments become an operation and its params."""

import json
import os
import time

import pytest

from pl8_cli import cli
from pl8_cli.client import TransferError


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
                "--description", "Eng work", "--creator", "alice") == (
        "create_space", {"space_id": "ENG", "name": "Engineering",
                         "description": "Eng work", "creator": "alice"})


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
                "--description", "D", "--creator", "alice") == (
        "create_issue", {"space_id": "ENG", "title": "T", "description": "D",
                         "status": "TODO", "creator": "alice"})
    assert call("issue", "create", "--space", "ENG", "--title", "T", "--description", "D",
                "--creator", "alice", "--status", "IN_PROGRESS")[1]["status"] == "IN_PROGRESS"


def test_issue_create_uses_default_space(call, monkeypatch):
    monkeypatch.setenv("PL8_SPACE", "OPS")
    monkeypatch.setenv("PL8_CREATOR", "alice")
    assert call("issue", "create", "--title", "T", "--description", "D")[1]["space_id"] == "OPS"
    # The flag beats the variable.
    assert call("issue", "create", "--space", "ENG", "--title", "T",
                "--description", "D")[1]["space_id"] == "ENG"


def test_issue_create_needs_a_space(usage_error, monkeypatch):
    monkeypatch.setenv("PL8_CREATOR", "alice")
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


def test_issue_update_sends_only_the_fields_given(call):
    assert call("issue", "update", "ENG/abc123", "--title", "T") == (
        "update_issue", {"space_id": "ENG", "issue_id": "abc123", "title": "T"})
    assert call("issue", "update", "ENG/abc123", "--description", "D") == (
        "update_issue", {"space_id": "ENG", "issue_id": "abc123",
                         "description": "D"})


def test_issue_update_needs_a_field(usage_error):
    assert "Nothing to update" in usage_error("issue", "update", "ENG/abc123",
                                              "--if-version", "2")


def test_issue_transition(call):
    assert call("issue", "transition", "ENG/abc123", "--status", "DONE") == (
        "transition_issue", {"space_id": "ENG", "issue_id": "abc123", "status": "DONE"})
    assert call("issue", "transition", "ENG/abc123", "--status", "DONE",
                "--if-version", "4")[1]["version"] == 4


def test_issue_delete(call):
    assert call("issue", "delete", "ENG/abc123") == (
        "delete_issue", {"space_id": "ENG", "issue_id": "abc123"})


# Creators

def test_creator_from_the_environment(call, monkeypatch):
    monkeypatch.setenv("PL8_CREATOR", "agent-7")
    assert call("space", "create", "ENG", "--name", "N",
                "--description", "D")[1]["creator"] == "agent-7"
    # The flag beats the variable.
    assert call("space", "create", "ENG", "--name", "N", "--description", "D",
                "--creator", "alice")[1]["creator"] == "alice"


def test_creator_is_required(usage_error):
    assert "PL8_CREATOR" in usage_error("space", "create", "ENG", "--name", "N",
                                        "--description", "D")


@pytest.mark.parametrize("argv", [
    ("space", "get", "ENG"),
    ("issue", "get", "ENG/abc123"),
    ("issue", "update", "ENG/abc123", "--title", "T", "--description", "D"),
    ("comment", "update", "ENG/abc123", "c1", "--body", "B"),
])
def test_only_creates_take_a_creator(call, usage_error, argv):
    """An update never changes the creator, so no command but a create has one."""
    assert "creator" not in call(*argv)[1]
    usage_error(*argv, "--creator", "alice")


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


# pl8 comment

COMMENT_ID = "0199f3a1-0000-7000-8000-000000000000"
ISSUE_PARAMS = {"space_id": "ENG", "issue_id": "abc123"}
COMMENT_PARAMS = {**ISSUE_PARAMS, "comment_id": COMMENT_ID}


def test_comment_add(call):
    assert call("comment", "add", "ENG/abc123", "--body", "Reproduced on Safari",
                "--creator", "alice") == (
        "create_issue_comment", {**ISSUE_PARAMS, "body": "Reproduced on Safari",
                                 "creator": "alice"})


def test_comment_add_takes_a_bare_issue_id(call, monkeypatch):
    monkeypatch.setenv("PL8_SPACE", "OPS")
    monkeypatch.setenv("PL8_CREATOR", "alice")
    assert call("comment", "add", "abc123", "--body", "B")[1]["space_id"] == "OPS"


def test_comment_add_needs_a_body(usage_error):
    usage_error("comment", "add", "ENG/abc123", "--creator", "alice")


def test_comment_body_from_a_file(call, tmp_path):
    path = tmp_path / "body.md"
    path.write_text("Line one\n\n`quoted` $text\n", encoding="utf-8")
    assert call("comment", "add", "ENG/abc123", "--body-file", str(path),
                "--creator", "alice")[1]["body"] == "Line one\n\n`quoted` $text\n"


def test_comment_body_from_stdin(call, stdin):
    stdin("from stdin")
    assert call("comment", "add", "ENG/abc123", "--body-file", "-",
                "--creator", "alice")[1]["body"] == "from stdin"


def test_comment_body_and_file_are_exclusive(usage_error):
    usage_error("comment", "add", "ENG/abc123", "--body", "B", "--body-file", "-",
                "--creator", "alice")


def test_comment_get(call):
    assert call("comment", "get", "ENG/abc123", COMMENT_ID) == (
        "get_issue_comment", COMMENT_PARAMS)


def test_comment_list(call):
    assert call("comment", "list", "ENG/abc123") == ("get_issue_comments", ISSUE_PARAMS)
    assert call("comment", "list", "ENG/abc123", "--limit", "5", "--cursor", "c1") == (
        "get_issue_comments", {**ISSUE_PARAMS, "limit": 5, "cursor": "c1"})


def test_comment_list_all(run):
    _, out, invoker = run(["--env", "dev", "comment", "list", "ENG/abc123", "--all"],
                          [page([1], "c1"), page([2])])
    assert out == page([1, 2])
    assert len(invoker.calls) == 2


def test_comment_update(call):
    assert call("comment", "update", "ENG/abc123", COMMENT_ID, "--body", "B") == (
        "update_issue_comment", {**COMMENT_PARAMS, "body": "B"})
    assert call("comment", "update", "ENG/abc123", COMMENT_ID, "--body", "B",
                "--if-version", "3")[1]["version"] == 3


def test_comment_delete(call):
    assert call("comment", "delete", "ENG/abc123", COMMENT_ID) == (
        "delete_issue_comment", COMMENT_PARAMS)


def test_comment_id_is_not_slash_joined(usage_error):
    """A comment is named by two arguments, not one reference."""
    usage_error("comment", "get", f"ENG/abc123/{COMMENT_ID}")


# pl8 comment wait

@pytest.fixture
def clock(monkeypatch):
    """A clock that only moves when the CLI sleeps, and the sleeps it took."""
    sleeps = []
    now = [0.0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", sleep)
    return sleeps


def wait_argv(*extra):
    return ["--env", "dev", "comment", "wait", "ENG/abc123", *extra]


def test_comment_wait_params(run):
    _, _, invoker = run(wait_argv(), [page([1])])
    assert invoker.calls == [("get_issue_comments_after", ISSUE_PARAMS)]

    _, _, invoker = run(wait_argv("--after", COMMENT_ID, "--limit", "5"), [page([1])])
    assert invoker.calls == [("get_issue_comments_after", {
        **ISSUE_PARAMS, "last_comment_id": COMMENT_ID, "limit": 5})]


def test_comment_wait_returns_the_first_non_empty_batch(run, clock):
    arrived = page([{"comment_id": COMMENT_ID}])
    code, out, invoker = run(wait_argv(), [page([]), page([]), arrived])

    assert (code, out) == (cli.EXIT_OK, arrived)
    assert len(invoker.calls) == 3
    assert len(clock) == 2


def test_comment_wait_emits_one_json_document(run, clock):
    """Comments are buffered, not streamed: stdout is one document."""
    arrived = page([{"comment_id": COMMENT_ID}])
    _, out, _ = run(wait_argv(), [page([]), page([]), arrived], raw=True)

    assert out == json.dumps(arrived) + "\n"


def test_comment_wait_that_waited_for_nothing_succeeds(run, clock):
    """Nothing arriving is an answer, so a caller can loop on this command."""
    empty = page([])
    # One poll, one full interval's sleep, one more poll, out of time.
    code, out, invoker = run(wait_argv("--interval", "60", "--max-wait", "60"),
                             [empty, empty])

    assert (code, out) == (cli.EXIT_OK, empty)
    assert len(invoker.calls) == 2
    assert clock == [60.0]


def test_comment_wait_polls_once_when_it_cannot_wait(run, clock):
    _, out, invoker = run(wait_argv("--max-wait", "0"), [page([])])

    assert out == page([])
    assert len(invoker.calls) == 1
    assert clock == []


@pytest.mark.parametrize(("asked", "clamped"), [
    ("0.1", cli.MIN_POLL_INTERVAL),
    ("1", cli.MIN_POLL_INTERVAL),
    ("600", cli.MAX_POLL_INTERVAL),
])
def test_comment_wait_clamps_the_interval(run, clock, asked, clamped):
    run(wait_argv("--interval", asked, "--max-wait", "600"), [page([]), page([1])])

    [slept] = clock
    # The clamped interval is the floor; jitter only ever adds to it.
    assert clamped <= slept <= clamped * (1 + cli.POLL_JITTER)


def test_comment_wait_jitters_each_sleep(run, clock, monkeypatch):
    run(wait_argv("--interval", "30", "--max-wait", "600"),
        [page([]), page([]), page([]), page([1])])

    assert len(set(clock)) == len(clock) == 3
    assert all(30.0 <= slept <= 30.0 * (1 + cli.POLL_JITTER) for slept in clock)


def test_comment_wait_never_sleeps_past_the_deadline(run, clock, monkeypatch):
    # No jitter, so the arithmetic is the only thing under test here.
    monkeypatch.setattr(cli.random, "uniform", lambda low, high: low)
    code, out, invoker = run(wait_argv("--interval", "60", "--max-wait", "70"),
                             [page([])] * 3)

    # Three polls, and the last sleep cut down to what was left of the wait.
    assert len(invoker.calls) == 3
    assert (code, out) == (cli.EXIT_OK, page([]))
    assert clock == [60.0, 10.0]


def test_poll_interval_is_clamped_not_rejected():
    assert cli.poll_interval(0.0) == cli.MIN_POLL_INTERVAL
    assert cli.poll_interval(3600.0) == cli.MAX_POLL_INTERVAL
    assert cli.poll_interval(20.0) == 20.0


def test_comment_wait_stops_at_a_failure(run, clock):
    failure = {"ok": False, "error": {"type": "DDBMissingError", "message": "gone"}}
    code, out, invoker = run(wait_argv(), [failure])

    assert (code, out) == (cli.EXIT_REJECTED, failure)
    assert len(invoker.calls) == 1


def test_comment_wait_rejects_a_response_that_is_not_a_page(run, clock):
    code, out, _ = run(wait_argv(), [{"ok": True, "data": None}])

    assert code == cli.EXIT_FAULT
    assert out["error"]["type"] == "InvokeError"


def test_comment_wait_all_follows_cursors(run, clock):
    _, out, invoker = run(wait_argv("--all"), [page([1], "c1"), page([2])])

    assert out == page([1, 2])
    assert len(invoker.calls) == 2


# pl8 attachment

ATTACHMENT_ID = "0199f3a2-0000-7000-8000-000000000000"
ATTACHMENT_PARAMS = {**ISSUE_PARAMS, "attachment_id": ATTACHMENT_ID}
UPLOAD_URL = "https://s3.example/pl8"
FIELDS = {"key": "ENG/abc123/k", "policy": "eyJ...", "x-amz-signature": "sig"}
DOWNLOAD_URL = "https://s3.example/pl8/k?X-Amz-Signature=deadbeef"
CONTENT = b"# notes\n"
INITIATE_PARAMS = {**ISSUE_PARAMS, "name": "notes.txt", "content_type": "text/plain",
                   "size": len(CONTENT), "creator": "alice"}


@pytest.fixture
def upload(tmp_path):
    """A small file to attach."""
    path = tmp_path / "notes.txt"
    path.write_bytes(CONTENT)
    return path


def initiated():
    return {"ok": True, "data": {
        "attachment": {"attachment_id": ATTACHMENT_ID, "status": "PENDING"},
        "upload": {"url": UPLOAD_URL, "fields": FIELDS}}}


def attached(status="UPLOADED", url=DOWNLOAD_URL):
    return {"ok": True, "data": {
        "attachment": {"attachment_id": ATTACHMENT_ID, "name": "notes.txt",
                       "status": status},
        "download_url": url}}


def add_argv(upload, *extra):
    return ["--env", "dev", "attachment", "add", "ENG/abc123", "--file", str(upload),
            "--creator", "alice", *extra]


def get_argv(destination, *extra):
    return ["--env", "dev", "attachment", "get", "ENG/abc123", ATTACHMENT_ID,
            "--output", str(destination), *extra]


def test_attachment_add_initiates_uploads_then_confirms(run, transfers, upload):
    code, out, invoker = run(add_argv(upload), [initiated(), {"ok": True, "data": None}])

    assert code == cli.EXIT_OK
    assert invoker.calls == [
        ("initiate_issue_attachment_upload", INITIATE_PARAMS),
        ("confirm_issue_attachment_uploaded", ATTACHMENT_PARAMS)]
    assert transfers.uploads == [{
        "target": {"url": UPLOAD_URL, "fields": FIELDS}, "path": str(upload),
        "size": len(CONTENT), "name": "notes.txt", "content_type": "text/plain"}]
    # Confirming reports nothing of its own, so the id comes from here.
    assert out == {"ok": True, "data": {"attachment_id": ATTACHMENT_ID}}


def test_attachment_add_reports_what_confirming_returned(run, transfers, upload):
    row = {"attachment_id": ATTACHMENT_ID, "status": "UPLOADED"}
    _, out, _ = run(add_argv(upload), [initiated(), {"ok": True, "data": row}])

    assert out == {"ok": True, "data": row}


def test_attachment_add_name_and_content_type_override_the_file(run, transfers, upload):
    _, _, invoker = run(add_argv(upload, "--name", "report.pdf",
                                 "--content-type", "application/pdf"),
                        [initiated(), {"ok": True, "data": None}])

    params = invoker.calls[0][1]
    assert (params["name"], params["content_type"]) == ("report.pdf", "application/pdf")
    # The size is still the file's, whatever it is called.
    assert params["size"] == len(CONTENT)
    assert transfers.uploads[0]["name"] == "report.pdf"


def test_attachment_add_content_type_falls_back_to_octet_stream(run, transfers, tmp_path):
    path = tmp_path / "blob.unknown-suffix"
    path.write_bytes(CONTENT)
    _, _, invoker = run(add_argv(path), [initiated(), {"ok": True, "data": None}])

    assert invoker.calls[0][1]["content_type"] == "application/octet-stream"


def test_attachment_add_attaches_to_a_comment(run, transfers, upload):
    _, _, invoker = run(add_argv(upload, "--comment", COMMENT_ID),
                        [initiated(), {"ok": True, "data": None}])

    assert invoker.calls[0][1] == {**INITIATE_PARAMS, "comment_id": COMMENT_ID}


def test_attachment_add_failed_upload_names_the_attachment(run, transfers, upload):
    transfers.fail = TransferError("Upload to S3 failed: connection reset")
    code, out, invoker = run(add_argv(upload), [initiated()])

    assert code == cli.EXIT_FAULT
    assert out["error"]["type"] == "TransferError"
    assert out["error"]["attachment_id"] == ATTACHMENT_ID
    assert "connection reset" in out["error"]["message"]
    # Never confirmed: confirming is the claim that the bytes are there.
    assert [operation for operation, _ in invoker.calls] == [
        "initiate_issue_attachment_upload"]


def test_attachment_add_failed_confirm_names_the_attachment(run, transfers, upload):
    failure = {"ok": False, "error": {"type": "DDBMissingError", "message": "gone"}}
    code, out, _ = run(add_argv(upload), [initiated(), failure])

    assert code == cli.EXIT_REJECTED
    assert out["error"] == {"type": "DDBMissingError", "message": "gone",
                            "attachment_id": ATTACHMENT_ID}
    assert len(transfers.uploads) == 1


def test_attachment_add_failed_initiate_has_no_attachment_to_name(run, transfers, upload):
    failure = {"ok": False, "error": {"type": "DDBMissingError", "message": "no Issue"}}
    code, out, _ = run(add_argv(upload), [failure])

    assert (code, out) == (cli.EXIT_REJECTED, failure)
    assert transfers.uploads == []


@pytest.mark.parametrize("data", [
    None,
    {"attachment": {"attachment_id": ATTACHMENT_ID}},
    {"attachment": {}, "upload": {"url": UPLOAD_URL, "fields": FIELDS}},
    {"attachment": {"attachment_id": ATTACHMENT_ID}, "upload": {"url": UPLOAD_URL}},
])
def test_attachment_add_needs_an_attachment_and_an_upload(run, transfers, upload, data):
    code, out, _ = run(add_argv(upload), [{"ok": True, "data": data}])

    assert code == cli.EXIT_FAULT
    assert out["error"]["type"] == "InvokeError"
    assert transfers.uploads == []


def test_attachment_add_refuses_a_file_over_the_limit(usage_error, tmp_path):
    big = tmp_path / "big.bin"
    big.touch()
    # Sparse, so this costs nothing: only its size is ever read.
    os.truncate(big, cli.MAX_ATTACHMENT_BYTES + 1)

    message = usage_error("attachment", "add", "ENG/abc123", "--file", str(big),
                          "--creator", "alice")
    assert str(cli.MAX_ATTACHMENT_BYTES) in message


def test_attachment_add_refuses_stdin(usage_error):
    assert "stdin" in usage_error("attachment", "add", "ENG/abc123", "--file", "-",
                                  "--creator", "alice")


def test_attachment_add_refuses_a_missing_file(usage_error, tmp_path):
    assert "nope.bin" in usage_error("attachment", "add", "ENG/abc123", "--creator",
                                     "alice", "--file", str(tmp_path / "nope.bin"))


def test_attachment_add_refuses_a_directory(usage_error, tmp_path):
    usage_error("attachment", "add", "ENG/abc123", "--file", str(tmp_path),
                "--creator", "alice")


def test_attachment_get_writes_the_file_and_reports_it(run, transfers, tmp_path):
    destination = tmp_path / "notes.txt"
    code, out, invoker = run(get_argv(destination), [attached()])

    assert code == cli.EXIT_OK
    assert invoker.calls == [("get_issue_attachment", ATTACHMENT_PARAMS)]
    assert transfers.downloads == [DOWNLOAD_URL]
    assert destination.read_bytes() == transfers.content
    assert out == {"ok": True, "data": {
        "attachment_id": ATTACHMENT_ID, "name": "notes.txt", "status": "UPLOADED",
        "path": str(destination)}}


def test_attachment_get_never_prints_the_url(run, transfers, tmp_path):
    """A presigned URL is a live bearer token; stdout is not where it goes."""
    _, out, _ = run(get_argv(tmp_path / "notes.txt"), [attached()], raw=True)

    assert DOWNLOAD_URL not in out


def test_attachment_get_refuses_stdout(usage_error):
    message = usage_error("attachment", "get", "ENG/abc123", ATTACHMENT_ID,
                          "--output", "-")
    assert "envelope" in message


def test_attachment_get_refuses_an_existing_path(usage_error, tmp_path):
    destination = tmp_path / "notes.txt"
    destination.write_bytes(b"do not lose me")

    assert "--force" in usage_error("attachment", "get", "ENG/abc123", ATTACHMENT_ID,
                                   "--output", str(destination))
    assert destination.read_bytes() == b"do not lose me"


def test_attachment_get_overwrites_with_force(run, transfers, tmp_path):
    destination = tmp_path / "notes.txt"
    destination.write_bytes(b"stale")
    code, _, _ = run(get_argv(destination, "--force"), [attached()])

    assert code == cli.EXIT_OK
    assert destination.read_bytes() == transfers.content


def test_attachment_get_refuses_a_missing_directory(usage_error, tmp_path):
    usage_error("attachment", "get", "ENG/abc123", ATTACHMENT_ID,
                "--output", str(tmp_path / "nowhere" / "notes.txt"))


def test_attachment_get_refuses_an_attachment_with_no_object(run, transfers, tmp_path):
    destination = tmp_path / "notes.txt"
    code, out, _ = run(get_argv(destination), [attached(status="PENDING", url=None)])

    assert code == cli.EXIT_REJECTED
    assert out["error"]["type"] == cli.ATTACHMENT_PENDING
    assert "PENDING" in out["error"]["message"]
    assert transfers.downloads == []
    assert list(tmp_path.iterdir()) == []


def test_attachment_get_needs_a_download_url(run, transfers, tmp_path):
    code, out, _ = run(get_argv(tmp_path / "notes.txt"),
                       [{"ok": True, "data": {"attachment": {"status": "UPLOADED"}}}])

    assert code == cli.EXIT_FAULT
    assert out["error"]["type"] == "InvokeError"
    assert transfers.downloads == []


def test_attachment_get_leaves_nothing_behind_when_the_download_fails(run, transfers,
                                                                      tmp_path):
    destination = tmp_path / "notes.txt"
    transfers.content = b"half an obj"
    transfers.fail = TransferError("Download from S3 failed: connection reset")
    code, out, _ = run(get_argv(destination), [attached()])

    assert code == cli.EXIT_FAULT
    assert out["error"]["type"] == "TransferError"
    # Neither the destination nor the temporary file it was written through.
    assert list(tmp_path.iterdir()) == []


def test_attachment_get_keeps_the_old_file_when_a_forced_download_fails(run, transfers,
                                                                       tmp_path):
    destination = tmp_path / "notes.txt"
    destination.write_bytes(b"the old object")
    transfers.fail = TransferError("Download from S3 failed: connection reset")
    run(get_argv(destination, "--force"), [attached()])

    assert destination.read_bytes() == b"the old object"
    assert list(tmp_path.iterdir()) == [destination]


def test_attachment_list(call):
    assert call("attachment", "list", "ENG/abc123") == (
        "get_issue_attachments", ISSUE_PARAMS)
    assert call("attachment", "list", "ENG/abc123", "--limit", "5", "--cursor", "c1") == (
        "get_issue_attachments", {**ISSUE_PARAMS, "limit": 5, "cursor": "c1"})


def test_attachment_list_for_one_comment(call):
    assert call("attachment", "list", "ENG/abc123", "--comment", COMMENT_ID) == (
        "get_issue_comment_attachments", {**ISSUE_PARAMS, "comment_id": COMMENT_ID})


def test_attachment_list_all(run):
    _, out, invoker = run(["--env", "dev", "attachment", "list", "ENG/abc123", "--all"],
                          [page([1], "c1"), page([2])])

    assert out == page([1, 2])
    assert len(invoker.calls) == 2


def test_attachment_delete(call):
    assert call("attachment", "delete", "ENG/abc123", ATTACHMENT_ID) == (
        "delete_issue_attachment", ATTACHMENT_PARAMS)


def test_attachment_id_is_not_slash_joined(usage_error):
    """An attachment is named by its Issue and then its own id."""
    usage_error("attachment", "delete", f"ENG/abc123/{ATTACHMENT_ID}")


# Order

@pytest.mark.parametrize(("argv", "operation"), [
    (("comment", "list", "ENG/abc123"), "get_issue_comments"),
    (("attachment", "list", "ENG/abc123"), "get_issue_attachments"),
    (("attachment", "list", "ENG/abc123", "--comment", COMMENT_ID),
     "get_issue_comment_attachments"),
])
def test_desc_asks_for_the_reverse_order(call, argv, operation):
    # Left out otherwise, so the default order stays PL8's to state.
    assert "ascending" not in call(*argv)[1]
    assert call(*argv, "--desc") == (operation, {**call(*argv)[1], "ascending": False})


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
    assert call("space", "create", "ENG", "--name", "N", "--creator", "alice",
                "--description-file", str(path)
                )[1]["description"] == "Line one\n\n`quoted` $text\n"


def test_description_from_stdin(call, stdin):
    stdin("from stdin")
    assert call("issue", "create", "--space", "ENG", "--title", "T", "--creator", "alice",
                "--description-file", "-")[1]["description"] == "from stdin"


def test_stdin_is_utf8_whatever_the_locale(call, stdin):
    stdin("café ✓", locale_encoding="latin-1")
    assert call("issue", "create", "--space", "ENG", "--title", "T", "--creator", "alice",
                "--description-file", "-")[1]["description"] == "café ✓"


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


@pytest.mark.parametrize("data", [
    None,
    {"cursor": None},
    {"items": {}, "cursor": None},
    {"items": [], "cursor": 1},
])
def test_all_rejects_a_malformed_page(run, data):
    code, out, _ = run(["--env", "dev", "space", "list", "--all"],
                       [{"ok": True, "data": data}])

    assert code == cli.EXIT_FAULT
    assert out["error"]["type"] == "InvokeError"


def test_without_all_returns_one_page(run):
    _, out, invoker = run(["--env", "dev", "space", "list"], [page([1], "c1")])
    assert out == page([1], "c1")
    assert len(invoker.calls) == 1
