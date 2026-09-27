# SPDX-FileCopyrightText: 2026 Daniel Chen
#
# SPDX-License-Identifier: MIT

"""Invokes pl8-interface, and moves attachment bytes to and from S3.

Every outcome of an invoke is an envelope, {"ok": true, "data": ...} or
{"ok": false, "error": {"type", "message"}}, so the CLI has one shape to
print. Failures that never reach pl8-interface's own validation are reported
under the CLI-side types below rather than raised.

Attachment bytes never go through pl8-interface: it signs a POST for an
upload and a GET for a download, and the CLI transfers the object itself.
Those legs raise TransferError instead of returning an envelope, since the
caller has to decide what a half-done transfer means for the row it belongs
to.
"""

import http.client
import io
import json
import shutil
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# The call never got a usable answer from Lambda: credentials, region,
# network, throttling, permissions, or a response that isn't an envelope.
INVOKE_ERROR = "InvokeError"
# The function ran but raised instead of returning an envelope; pl8-interface
# treats that as a server-side bug.
FUNCTION_ERROR = "FunctionError"
# A presigned S3 transfer failed part-way: the upload POST or the download
# GET. It gets its own type because it is neither pl8-interface refusing a
# request nor an answer about the object: a transfer that fails says nothing
# trustworthy about whether the bytes landed.
TRANSFER_ERROR = "TransferError"

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


def error(error_type, message, **details):
    """An error envelope. details carry machine-readable context beside the
    message, e.g. the attachment_id a failed upload left behind."""
    return {"ok": False,
            "error": {"type": error_type, "message": message, **details}}


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


# Presigned S3 transfers

# A cap on each socket operation, not on the whole transfer: a 100MB upload
# over a slow link is fine, a stalled connection is not.
TRANSFER_TIMEOUT_SECONDS = 60
# Enough of S3's XML to say which policy condition it was that failed.
ERROR_DETAIL_LIMIT = 2000
# What the presigned POST calls the part holding the bytes. S3 requires it.
FILE_FIELD = "file"


class TransferError(Exception):
    """A presigned S3 transfer failed; reported as TRANSFER_ERROR."""


class MultipartForm:
    """A presigned POST body as a stream: the policy fields, then the file.

    S3's POST policy requires every field to precede the file part and the
    file to be last, so the body is assembled in that order. It is a stream
    rather than one bytes object because an attachment may be 100MB, and
    holding that in memory to satisfy a library's API is not a trade worth
    making: this is why the upload uses urllib.request rather than requests,
    whose multipart support builds the whole body first.

    Content-Length is known in advance, from the size the upload was signed
    for, which keeps the request out of chunked encoding. S3's POST doesn't
    accept chunked.
    """

    def __init__(self, fields, content, *, size, name, content_type):
        self.boundary = uuid.uuid4().hex
        self.content_type = f"multipart/form-data; boundary={self.boundary}"
        head = b"".join(self._field(key, value) for key, value in fields.items())
        head += self._file_header(name, content_type)
        tail = f"\r\n--{self.boundary}--\r\n".encode()
        self._length = len(head) + size + len(tail)
        # The file sits between the two, read from as the socket takes it.
        self._parts = [io.BytesIO(head), content, io.BytesIO(tail)]

    def _field(self, name, value):
        return (f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="{_quoted(name)}"\r\n\r\n'
                f"{value}\r\n").encode()

    def _file_header(self, name, content_type):
        return (f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="{FILE_FIELD}"; '
                f'filename="{_quoted(name)}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n").encode()

    def __len__(self):
        return self._length

    def read(self, size=-1):
        """Bytes from the part being read, moving on as each runs out.

        A short read is fine: the HTTP client reads until read() gives back
        nothing, and stopping at each part's end keeps this from having to
        join anything.
        """
        if size is None or size < 0:
            return b"".join(iter(lambda: self.read(io.DEFAULT_BUFFER_SIZE), b""))
        while self._parts:
            chunk = self._parts[0].read(size)
            if chunk:
                return chunk
            self._parts.pop(0)
        return b""


def _quoted(value):
    """A value fit for a Content-Disposition parameter.

    Form metadata only: the object's S3 key comes from the signed policy's
    own `key` field, not from this filename, so escaping the characters that
    would end the quoted string early is all this needs to do.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def upload_file(target, path, *, size, name, content_type):
    """POST a file to a presigned S3 form, streaming it.

    Args:
        target (dict): the presigned POST, {"url", "fields"}
        path (str): the file to send
        size (int): its size, the one the upload was signed for
        name (str): filename to put in the form
        content_type (str): content type to put in the form

    Raises:
        TransferError: if the POST fails, or fails part-way through
    """
    try:
        with open(path, "rb") as content:
            body = MultipartForm(target["fields"], content, size=size, name=name,
                                 content_type=content_type)
            request = urllib.request.Request(target["url"], data=body, method="POST",
                                             headers={"Content-Type": body.content_type,
                                                      "Content-Length": str(len(body))})
            with urllib.request.urlopen(request, timeout=TRANSFER_TIMEOUT_SECONDS) as response:
                response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise TransferError(f"Upload to S3 failed: {_detail(exc)}") from exc


def download_file(url, out):
    """Stream a presigned URL into an open binary file.

    Args:
        url (str): the presigned GET
        out (io.BufferedWriter): where the bytes go

    Raises:
        TransferError: if the GET fails, or fails part-way through
    """
    try:
        with urllib.request.urlopen(url, timeout=TRANSFER_TIMEOUT_SECONDS) as response:
            shutil.copyfileobj(response, out)
    except (OSError, http.client.HTTPException) as exc:
        raise TransferError(f"Download from S3 failed: {_detail(exc)}") from exc


def _detail(exc):
    """What went wrong, with S3's own explanation when it gave one."""
    if not isinstance(exc, urllib.error.HTTPError):
        return str(exc)
    try:
        body = exc.read(ERROR_DETAIL_LIMIT).decode("utf-8", "replace").strip()
    except (OSError, http.client.HTTPException):
        body = ""
    return f"HTTP {exc.code} {exc.reason}" + (f": {body}" if body else "")
