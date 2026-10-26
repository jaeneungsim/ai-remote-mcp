"""Checks a Cognito access token before the server does anything for the caller.

The MCP SDK calls `verify_token` for each request that carries `Authorization: Bearer ...` and
answers 401 when it returns None. A token passes only when it is signed by our user pool, not
expired, an access token (not an ID token), issued to our app client, and issued for this server:
Cognito puts the `resource` the client asked for into `aud` (RFC 8707). The SDK then checks the
scope and answers 403 when it is missing.
"""

from __future__ import annotations

from typing import Any

import anyio.to_thread
import jwt
from mcp.server.auth.provider import AccessToken

from remote_mcp import log
from remote_mcp.config import Settings

REQUIRED_CLAIMS = ["exp", "iat", "iss", "sub", "client_id", "token_use", "scope"]


class Refused(Exception):
    def __init__(self, reason: str, **detail: Any):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class CognitoVerifier:
    def __init__(self, settings: Settings, keys: jwt.PyJWKClient | None = None):
        self._settings = settings
        # Caches the user pool's public keys and fetches them again once when a token names an unknown key.
        self._keys = keys or jwt.PyJWKClient(settings.jwks_url, cache_jwk_set=True, lifespan=3600, timeout=5)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await anyio.to_thread.run_sync(self.check, token)
        except Refused as refused:
            log.refuse(refused.reason, **refused.detail)
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=claims["scope"].split(),
            expires_at=claims["exp"],
            resource=self._settings.server_url,
            subject=claims["sub"],
            claims={"iss": claims["iss"]},
        )

    def check(self, token: str) -> dict[str, Any]:
        """The token's claims, or Refused with a reason that is safe to log."""
        try:
            key = self._keys.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError:
            raise Refused("keys_unreachable") from None
        except jwt.PyJWKClientError:
            raise Refused("unknown_key") from None
        except jwt.DecodeError:
            raise Refused("malformed") from None
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self._settings.issuer,
                leeway=30,
                # Cognito access tokens carry `aud` only with a resource binding, so it is checked below.
                options={"require": REQUIRED_CLAIMS, "verify_aud": False},
            )
        except jwt.ExpiredSignatureError:
            raise Refused("expired") from None
        except jwt.InvalidIssuerError:
            raise Refused("bad_issuer") from None
        except jwt.MissingRequiredClaimError as err:
            raise Refused("missing_claim", claim=err.claim) from None
        except jwt.InvalidTokenError:
            raise Refused("bad_signature") from None
        if claims["token_use"] != "access":
            raise Refused("not_access_token")
        if claims["client_id"] != self._settings.client_id:
            raise Refused("bad_client")
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else list(audience or [])
        if not any(_same_url(value, self._settings.server_url) for value in audiences):
            raise Refused("bad_audience", audience=audiences)
        return claims


def _same_url(a: str, b: str) -> bool:
    return a.rstrip("/").lower() == b.rstrip("/").lower()
