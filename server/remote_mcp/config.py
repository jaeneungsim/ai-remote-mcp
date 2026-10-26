"""Settings the server reads once when it starts.

On Lambda they come from SSM parameters under CONFIG_PREFIX. The server URL and the app client ID
exist only after the function URL and the Cognito client are created, so passing them as
environment variables would make the stack depend on itself. In tests they come from the
environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

# Settings field -> SSM parameter name under CONFIG_PREFIX (and upper-case environment variable in tests)
PARAMETERS = {"server_url": "server-url", "user_pool_id": "user-pool-id", "client_id": "client-id"}


@dataclass(frozen=True)
class Settings:
    server_url: str  # the MCP endpoint, https://<function URL host>/mcp; also the token audience
    user_pool_id: str
    client_id: str  # the pre-registered public app client that Claude Code signs in with
    region: str
    search_function: str
    version: str  # the git commit the server was built from

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"

    @property
    def scope(self) -> str:
        """Cognito names a custom scope `<resource server identifier>/<scope>`; the identifier is the server URL."""
        return f"{self.server_url}/search"

    @property
    def host(self) -> str:
        return urlparse(self.server_url).netloc


def load() -> Settings:
    prefix = os.environ.get("CONFIG_PREFIX")
    values = _from_ssm(prefix) if prefix else {key: os.environ[key.upper()] for key in PARAMETERS}
    return Settings(
        **values,
        region=os.environ.get("AWS_REGION", "ap-southeast-2"),
        search_function=os.environ.get("SEARCH_FUNCTION", "team-docs-search"),
        version=os.environ.get("SERVER_VERSION", "dev"),
    )


def _from_ssm(prefix: str) -> dict[str, str]:
    import boto3  # in the Lambda runtime; tests never get here

    names = {key: prefix + name for key, name in PARAMETERS.items()}
    answer = boto3.client("ssm").get_parameters(Names=list(names.values()))
    found = {parameter["Name"]: parameter["Value"] for parameter in answer["Parameters"]}
    missing = sorted(set(names.values()) - set(found))
    if missing:
        raise RuntimeError(f"Missing SSM parameters: {', '.join(missing)}")
    return {key: found[name] for key, name in names.items()}
