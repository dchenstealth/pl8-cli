# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import http.client
import io
import json
import re
import urllib.error
import urllib.request

import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from pl8_cli.client import (
    FUNCTION_ERROR,
    INVOKE_ERROR,
    Invoker,
    MultipartForm,
    TransferError,
    download_file,
    is_read,
    upload_file,
)

FUNCTION = "dev-pl8-interface"


def payload(obj):
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return StreamingBody(io.BytesIO(raw), len(raw))


@pytest.fixture
def invoker():
    return Invoker(FUNCTION)


def stub(invoker, *, read):
    stubber = Stubber(invoker._client(read=read))
    stubber.activate()
    return stubber


def expect(stubber, operation, params, response):
    stubber.add_response("invoke", {"StatusCode": 200, **response}, {
        "FunctionName": FUNCTION,
        "InvocationType": "RequestResponse",
        "Payload": json.dumps({"operation": operation, "params": params}).encode(),
    })


def test_success_envelope_passes_through(invoker):
    stubber = stub(invoker, read=True)
    envelope = {"ok": True, "data": {"space_id": "ENG"}}
    expect(stubber, "get_space", {"space_id": "ENG"}, {"Payload": payload(envelope)})

    assert invoker.invoke("get_space", {"space_id": "ENG"}) == envelope
    stubber.assert_no_pending_responses()


def test_failure_envelope_passes_through(invoker):
    stubber = stub(invoker, read=False)
    envelope = {"ok": False, "error": {"type": "DDBExistsError", "message": "exists"}}
    expect(stubber, "create_space", {}, {"Payload": payload(envelope)})

    assert invoker.invoke("create_space", {}) == envelope


def test_function_error_reports_lambda_error(invoker):
    stubber = stub(invoker, read=True)
    expect(stubber, "get_space", {}, {
        "FunctionError": "Unhandled",
        "Payload": payload({"errorType": "KeyError", "errorMessage": "'x'"}),
    })

    assert invoker.invoke("get_space", {}) == {
        "ok": False, "error": {"type": FUNCTION_ERROR, "message": "KeyError: 'x'"}}


def test_function_error_with_unreadable_payload(invoker):
    stubber = stub(invoker, read=True)
    expect(stubber, "get_space", {}, {"FunctionError": "Unhandled",
                                      "Payload": payload(b"not json")})

    result = invoker.invoke("get_space", {})
    assert result["error"] == {"type": FUNCTION_ERROR,
                               "message": "Function failed (Unhandled)"}


@pytest.mark.parametrize("body", [
    b"not json",
    {"statusCode": 200},
    {"ok": True},
    {"ok": False, "error": "nope"},
    {"ok": "true", "data": None},
])
def test_non_envelope_response_is_invoke_error(invoker, body):
    stubber = stub(invoker, read=True)
    expect(stubber, "get_space", {}, {"Payload": payload(body)})

    assert invoker.invoke("get_space", {})["error"]["type"] == INVOKE_ERROR


def test_client_error_is_invoke_error(invoker):
    stubber = stub(invoker, read=True)
    stubber.add_client_error("invoke", service_error_code="ResourceNotFoundException",
                             service_message="Function not found")

    result = invoker.invoke("get_space", {})
    assert result["ok"] is False
    assert result["error"]["type"] == INVOKE_ERROR
    assert "Function not found" in result["error"]["message"]


def test_connection_error_is_invoke_error(invoker, monkeypatch):
    def fail(**kwargs):
        raise EndpointConnectionError(endpoint_url="https://lambda")
    monkeypatch.setattr(invoker._client(read=False), "invoke", fail)

    assert invoker.invoke("create_space", {})["error"]["type"] == INVOKE_ERROR


def test_writes_are_not_retried(invoker):
    # A write that timed out on the read may already have been applied.
    assert invoker._client(read=False).meta.config.retries["total_max_attempts"] == 1
    assert "total_max_attempts" not in invoker._client(read=True).meta.config.retries


def test_reads_and_writes_use_their_own_clients(invoker):
    read_stubber = stub(invoker, read=True)
    write_stubber = stub(invoker, read=False)
    ok = {"Payload": payload({"ok": True, "data": None})}
    expect(read_stubber, "get_issue", {}, ok)
    expect(write_stubber, "delete_issue", {}, {"Payload": payload({"ok": True, "data": None})})

    invoker.invoke("get_issue", {})
    invoker.invoke("delete_issue", {})
    read_stubber.assert_no_pending_responses()
    write_stubber.assert_no_pending_responses()


@pytest.mark.parametrize(("operation", "read"), [
    # Reads may be retried; each of these is one call to DynamoDB's view of
    # the world and asking twice costs nothing.
    ("get_issue_attachment", True),
    ("get_issue_attachments", True),
    ("get_issue_comment_attachments", True),
    ("get_issue_comments_after", True),
    # Writes get the single-attempt policy: initiating reserves a row,
    # re-signing hands out another upload, confirming claims the bytes
    # landed, and deleting is a delete. None of them is safe to repeat
    # because the read of the response timed out.
    ("initiate_issue_attachment_upload", False),
    ("resign_issue_attachment_upload", False),
    ("confirm_issue_attachment_uploaded", False),
    ("delete_issue_attachment", False),
])
def test_attachment_operations_land_on_the_right_retry_policy(operation, read):
    assert is_read(operation) is read


# Presigned S3 transfers

TARGET = {"url": "https://s3.example/pl8",
          "fields": {"key": "ENG/abc123/k", "policy": "eyJ...",
                     "x-amz-signature": "sig"}}


def multipart(content=b"PAYLOAD", *, name="notes.txt"):
    return MultipartForm(TARGET["fields"], io.BytesIO(content), size=len(content),
                         name=name, content_type="text/plain")


def test_multipart_form_puts_every_policy_field_before_the_file():
    form = multipart()
    body = form.read()

    # "; name=", so the file part's filename= is not one of them.
    assert re.findall(rb'; name="([^"]+)"', body) == [
        b"key", b"policy", b"x-amz-signature", b"file"]
    assert body.index(b"PAYLOAD") > body.rindex(b'name="file"')
    assert body.endswith(f"\r\n--{form.boundary}--\r\n".encode())


def test_multipart_form_knows_its_length_before_it_is_read():
    """Content-Length comes from the size the upload was signed for."""
    form = multipart()
    assert len(form) == len(form.read())


def test_multipart_form_is_read_in_chunks():
    """The file is never held whole: read(n) hands back at most n bytes."""
    form = multipart(b"x" * 4096)
    chunks = list(iter(lambda: form.read(64), b""))

    assert max(len(chunk) for chunk in chunks) <= 64
    assert sum(len(chunk) for chunk in chunks) == len(form)


def test_multipart_form_escapes_a_name_that_would_end_the_header():
    form = multipart(name='in"verted.txt')
    body = form.read()

    assert rb'filename="in\"verted.txt"' in body
    assert len(body) == len(form)


def test_upload_posts_a_streamed_multipart_form(monkeypatch, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"PAYLOAD")
    sent = {}

    def urlopen(request, timeout=None):
        sent.update(url=request.full_url, method=request.get_method(),
                    headers=dict(request.header_items()), body=request.data.read(),
                    timeout=timeout)
        return io.BytesIO(b"")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    upload_file(TARGET, str(path), size=7, name="notes.txt",
                content_type="text/plain")

    assert (sent["url"], sent["method"]) == (TARGET["url"], "POST")
    assert sent["headers"]["Content-type"].startswith("multipart/form-data; boundary=")
    # Content-Length, not chunked encoding: S3's POST doesn't take chunked.
    assert int(sent["headers"]["Content-length"]) == len(sent["body"])
    assert b"PAYLOAD" in sent["body"]
    assert sent["timeout"] is not None


def test_upload_failure_reports_what_s3_said(monkeypatch, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"PAYLOAD")

    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", {},
            io.BytesIO(b"<Error><Code>AccessDenied</Code></Error>"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(TransferError) as raised:
        upload_file(TARGET, str(path), size=7, name="notes.txt",
                    content_type="text/plain")

    assert "403" in str(raised.value)
    assert "AccessDenied" in str(raised.value)


def test_upload_connection_failure_is_a_transfer_error(monkeypatch, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"PAYLOAD")

    def urlopen(request, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(TransferError, match="connection reset"):
        upload_file(TARGET, str(path), size=7, name="notes.txt",
                    content_type="text/plain")


def test_download_streams_into_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: io.BytesIO(b"the object"))
    destination = tmp_path / "out"

    with open(destination, "wb") as out:
        download_file("https://s3.example/pl8/k", out)

    assert destination.read_bytes() == b"the object"


def test_download_cut_off_part_way_is_a_transfer_error(monkeypatch, tmp_path):
    class Stalls(io.BytesIO):
        """Gives up after the first read, the way a dropped response does."""

        def read(self, size=-1):
            if self.tell():
                raise http.client.IncompleteRead(b"")
            return super().read(size)

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: Stalls(b"half an object"))

    with pytest.raises(TransferError), open(tmp_path / "out", "wb") as out:
        download_file("https://s3.example/pl8/k", out)
