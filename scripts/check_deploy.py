"""Checks a deployed server from outside, without a token. Runs after every deploy and rollback.

- POST /mcp without a token gets 401, and the server's challenge names the metadata address and the scope.
  Lambda function URLs rename a 401's WWW-Authenticate header to x-amzn-Remapped-WWW-Authenticate
  (seen 2026-10-26), so clients do not see the challenge and find the metadata at the well-known path.
- Both well-known paths serve the same metadata, naming this server and the Cognito issuer.
- GET /mcp gets 405, so no event stream is opened.

Usage: python scripts/check_deploy.py https://<function-url-host>/mcp
Standard library only, so it runs anywhere. Exits 1 on the first failed check.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse


def request(method: str, url: str, body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def check(condition: bool, message: str) -> None:
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        sys.exit(1)


def main(server_url: str) -> None:
    origin = "{0.scheme}://{0.netloc}".format(urlparse(server_url))
    path_form = f"{origin}/.well-known/oauth-protected-resource{urlparse(server_url).path}"
    root_form = f"{origin}/.well-known/oauth-protected-resource"

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    status, headers, _ = request("POST", server_url, body)
    check(status == 401, f"POST without a token -> {status}")
    name = "www-authenticate" if "www-authenticate" in headers else "x-amzn-remapped-www-authenticate"
    challenge = headers.get(name, "")
    check(f'resource_metadata="{path_form}"' in challenge, f"401 names the metadata address (in {name})")
    check(f'scope="{server_url}/search"' in challenge, f"401 names the scope (in {name})")

    documents = []
    for url in (path_form, root_form):
        status, _, raw = request("GET", url)
        check(status == 200, f"GET {urlparse(url).path} -> {status}")
        documents.append(json.loads(raw))
    check(documents[0] == documents[1], "both metadata paths serve the same document")
    check(documents[0].get("resource") == server_url, "metadata names this server as the resource")
    issuers = documents[0].get("authorization_servers") or []
    check(len(issuers) == 1 and issuers[0].startswith("https://cognito-idp."), "metadata names the Cognito issuer")

    status, headers, _ = request("GET", server_url)
    check(status == 405 and headers.get("allow") == "POST", f"GET /mcp -> {status}, Allow: {headers.get('allow')}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://") or not sys.argv[1].endswith("/mcp"):
        sys.exit(__doc__)
    main(sys.argv[1])
