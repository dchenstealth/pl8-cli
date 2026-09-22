# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""Invokes pl8-interface and hands back its response envelope.

Every outcome is an envelope, {"ok": true, "data": ...} or {"ok": false,
"error": {"type", "message"}}, so the CLI has one shape to print. Failures
that never reach pl8-interface's own validation are reported under the
CLI-side types below rather than raised.
"""

import json

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# The call never got a usable answer from Lambda: credentials, region,
# network, throttling, permissions, or a response that isn't an envelope.
INVOKE_ERROR = "InvokeError"
# The function ran but raised instead of returning an envelope; pl8-interface
# treats that as a server-side bug.
FUNCTION_ERROR = "FunctionError"

# pl8-interface's timeout is 30s. Reading for longer means Lambda reports a
# timed-out function as a FunctionError before this client gives up on it.
READ_TIMEOUT_SECONDS = 65
CONNECT_TIMEOUT_SECONDS = 10

# botocore retries read timeouts as transient, but a write that timed out on
# the read may already have been applied, and retrying it could, say, create
# a second Issue. Writes get one attempt and the caller decides; reads can
# retry freely.
READ_RETRIES = {"mode": "standard"}
WRITE_RETRIES = {"mode": "standard", "total_max_attempts": 1}


def error(error_type, message):
    return {"ok": False, "error": {"type": error_type, "message": message}}


def is_read(operation):
    return operation.startswith("get_")


def is_envelope(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return False
    if payload["ok"]:
        return "data" in payload
    err = payload.get("error")
    return (isinstance(err, dict) and isinstance(err.get("type"), str)
            and isinstance(err.get("message"), str))


class Invoker:
    """Calls one pl8-interface function, synchronously, through Lambda Invoke."""

    def __init__(self, function_name, *, profile=None, region=None):
        """Raises ProfileNotFound for an unknown profile, before any call."""
        self.function_name = function_name
        self._session = boto3.Session(profile_name=profile, region_name=region)
        self._clients = {}

    def _client(self, *, read):
        if read not in self._clients:
            self._clients[read] = self._session.client("lambda", config=Config(
                read_timeout=READ_TIMEOUT_SECONDS,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                retries=READ_RETRIES if read else WRITE_RETRIES,
            ))
        return self._clients[read]

    def invoke(self, operation, params):
        request = json.dumps({"operation": operation, "params": params})
        try:
            response = self._client(read=is_read(operation)).invoke(
                FunctionName=self.function_name,
                InvocationType="RequestResponse",
                Payload=request.encode(),
            )
            raw = response["Payload"].read()
        except (BotoCoreError, ClientError) as exc:
            return error(INVOKE_ERROR, str(exc))

        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None

        if "FunctionError" in response:
            # Lambda's error payload is {"errorType", "errorMessage", ...}.
            if isinstance(payload, dict) and "errorMessage" in payload:
                message = f"{payload.get('errorType', 'Error')}: {payload['errorMessage']}"
            else:
                message = f"Function failed ({response['FunctionError']})"
            return error(FUNCTION_ERROR, message)

        if not is_envelope(payload):
            return error(INVOKE_ERROR, "Response is not a pl8-interface envelope")
        return payload
