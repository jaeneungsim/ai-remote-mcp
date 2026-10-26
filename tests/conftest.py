"""Shared test setup: settings in the environment, a signing key that stands in for the user pool, a live server.

The server reads its settings when remote_mcp.app is imported, so they are set here, before any test
imports it. The server runs in uvicorn on a free local port, as Lambda Web Adapter runs it.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = _free_port()
SERVER_URL = f"http://127.0.0.1:{PORT}/mcp"
POOL_ID = "ap-southeast-2_TestPool1"
CLIENT_ID = "test-client-id"
ISSUER = f"https://cognito-idp.ap-southeast-2.amazonaws.com/{POOL_ID}"
SCOPE = f"{SERVER_URL}/search"
KID = "test-key"

os.environ.update(
    SERVER_URL=SERVER_URL,
    USER_POOL_ID=POOL_ID,
    CLIENT_ID=CLIENT_ID,
    AWS_REGION="ap-southeast-2",
    SERVER_VERSION="abc1234",
    SEARCH_FUNCTION="team-docs-search",
)
os.environ.pop("CONFIG_PREFIX", None)

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeKeys:
    """Stands in for jwt.PyJWKClient: knows one key id, like a user pool's JWKS."""

    def __init__(self) -> None:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
        self._key = jwt.PyJWK({**jwk, "kid": KID, "alg": "RS256", "use": "sig"})

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
        header = jwt.get_unverified_header(token)
        if header.get("kid") != KID:
            raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{header.get("kid")}"')
        return self._key


def make_token(*, key: Any = None, kid: str = KID, drop: tuple[str, ...] = (), **overrides: Any) -> str:
    """A Cognito-shaped access token for the test client and server; `overrides` replace claims."""
    now = int(time.time())
    claims = {
        "sub": "11111111-2222-3333-4444-555555555555",
        "iss": ISSUER,
        "client_id": CLIENT_ID,
        "origin_jti": "o-1",
        "event_id": "e-1",
        "token_use": "access",
        "scope": SCOPE,
        "auth_time": now,
        "exp": now + 3600,
        "iat": now,
        "jti": "j-1",
        "username": "11111111-2222-3333-4444-555555555555",
        "aud": SERVER_URL,
        **overrides,
    }
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, key or PRIVATE_KEY, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(scope="session")
def app_module() -> Any:
    from remote_mcp import app

    app.verifier._keys = FakeKeys()
    return app


@pytest.fixture(scope="session")
def live_server(app_module: Any) -> Iterator[str]:
    config = uvicorn.Config(app_module.app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield SERVER_URL
    server.should_exit = True
    thread.join(timeout=5)


def log_lines(text: str) -> list[dict[str, Any]]:
    """The JSON log lines in captured stdout."""
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]
