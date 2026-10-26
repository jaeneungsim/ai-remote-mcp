"""Calls the search function from ai-team-docs with this server's own role.

The caller's token never leaves the server: the function is invoked with the Lambda execution
role, which may invoke this one function and nothing else.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# The search function may run up to 30 s and this function stops at 30 s, so give up a little
# earlier and answer with an error instead of timing out. No retry: a retry would not fit.
CONFIG = Config(connect_timeout=3, read_timeout=26, retries={"mode": "standard", "max_attempts": 1})

_clients: dict[str, Any] = {}


class SearchFailed(Exception):
    """The message is safe to show to the agent and to log."""


def invoke(function: str, payload: dict[str, Any]) -> dict[str, Any]:
    if "lambda" not in _clients:
        _clients["lambda"] = boto3.client("lambda", config=CONFIG)
    try:
        answer = _clients["lambda"].invoke(FunctionName=function, Payload=json.dumps(payload).encode())
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        raise SearchFailed(f"The search function could not be called ({code or 'error'}).") from err
    except BotoCoreError as err:
        raise SearchFailed(f"The search function did not answer ({type(err).__name__}).") from err
    body = json.loads(answer["Payload"].read() or b"null")
    if answer.get("FunctionError"):
        message = body.get("errorMessage") if isinstance(body, dict) else body
        raise SearchFailed(f"The search failed: {message}")
    return body
