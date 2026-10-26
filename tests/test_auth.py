from __future__ import annotations

import time

import anyio
import pytest

from conftest import CLIENT_ID, OTHER_KEY, POOL_ID, SERVER_URL, FakeKeys, make_token
from remote_mcp import log
from remote_mcp.auth import CognitoVerifier, Refused
from remote_mcp.config import Settings

SETTINGS = Settings(
    server_url=SERVER_URL,
    user_pool_id=POOL_ID,
    client_id=CLIENT_ID,
    region="ap-southeast-2",
    search_function="team-docs-search",
    version="abc1234",
)


@pytest.fixture
def verifier() -> CognitoVerifier:
    return CognitoVerifier(SETTINGS, keys=FakeKeys())  # type: ignore[arg-type]


def refusal(verifier: CognitoVerifier, token: str) -> str:
    with pytest.raises(Refused) as caught:
        verifier.check(token)
    return caught.value.reason


def test_a_token_from_our_pool_for_this_server_passes(verifier: CognitoVerifier) -> None:
    claims = verifier.check(make_token())
    assert claims["sub"] == "11111111-2222-3333-4444-555555555555"


def test_aud_may_be_a_list_and_differ_in_case_or_trailing_slash(verifier: CognitoVerifier) -> None:
    verifier.check(make_token(aud=["https://other.example/api", SERVER_URL.upper() + "/"]))


@pytest.mark.parametrize(
    ("token_args", "reason"),
    [
        ({"exp": int(time.time()) - 120}, "expired"),
        ({"iss": "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_OtherPool"}, "bad_issuer"),
        ({"token_use": "id"}, "not_access_token"),
        ({"client_id": "another-app"}, "bad_client"),
        ({"aud": "https://another-server.example/mcp"}, "bad_audience"),
        ({"drop": ("aud",)}, "bad_audience"),
        ({"drop": ("scope",)}, "missing_claim"),
        ({"key": OTHER_KEY}, "bad_signature"),
        ({"kid": "unknown-key"}, "unknown_key"),
    ],
)
def test_refuses_with_a_reason(verifier: CognitoVerifier, token_args: dict, reason: str) -> None:
    assert refusal(verifier, make_token(**token_args)) == reason


def test_refuses_text_that_is_not_a_token(verifier: CognitoVerifier) -> None:
    assert refusal(verifier, "not-a-jwt") == "malformed"


def test_verify_token_returns_none_and_notes_the_reason(verifier: CognitoVerifier) -> None:
    reasons: dict = {}
    reset = log.rejection.set(reasons)
    try:
        assert anyio.run(verifier.verify_token, make_token(aud="https://another-server.example/mcp")) is None
    finally:
        log.rejection.reset(reset)
    assert reasons == {"reason": "bad_audience", "audience": ["https://another-server.example/mcp"]}


def test_verify_token_gives_the_sdk_what_it_checks(verifier: CognitoVerifier) -> None:
    token = anyio.run(verifier.verify_token, make_token(scope=f"openid {SERVER_URL}/search"))
    assert token is not None
    assert token.client_id == CLIENT_ID
    assert token.scopes == ["openid", f"{SERVER_URL}/search"]
    assert token.resource == SERVER_URL
    assert token.subject == "11111111-2222-3333-4444-555555555555"
