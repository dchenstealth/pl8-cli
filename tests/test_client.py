# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

import io
import json

import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from pl8_cli.client import FUNCTION_ERROR, INVOKE_ERROR, Invoker

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
