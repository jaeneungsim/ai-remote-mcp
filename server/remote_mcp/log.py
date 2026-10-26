"""One JSON line per event on stdout. Lambda sends stdout to CloudWatch Logs.

Never log a token, an email or the question. `user` is the Cognito `sub`, and a question is
logged only as its length.
"""

from __future__ import annotations

import json
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# The guard in app.py puts a fresh dict here for each HTTP request. The token check writes the
# reason it refused a token into it, and the guard logs that reason with the 401.
rejection: ContextVar[dict[str, Any] | None] = ContextVar("rejection", default=None)


def emit(event: str, **fields: Any) -> None:
    record = {
        "event": event,
        "at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        **fields,
        "lambda_version": os.environ.get("AWS_LAMBDA_FUNCTION_VERSION"),
    }
    record = {key: value for key, value in record.items() if value is not None}
    sys.stdout.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def refuse(reason: str, **detail: Any) -> None:
    """Note why a token was refused, for the guard's `auth_rejected` line."""
    holder = rejection.get()
    if holder is not None:
        holder.update(reason=reason, **detail)
