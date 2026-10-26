"""Signs in through Cognito's managed login the way Claude Code does, and returns an access token.

Reads the server and the client from .mcp.json, finds the authorization server in the server's
protected resource metadata, opens the browser with PKCE and `resource` set to the server URL, and
catches the redirect on the fixed callback port. The token stays in memory: it is never printed or
written to a file.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Remote:
    url: str
    client_id: str
    scope: str
    port: int


def remote_from_mcp_json(path: Path = ROOT / ".mcp.json", name: str = "team-docs-remote") -> Remote:
    server = json.loads(path.read_text())["mcpServers"][name]
    oauth = server["oauth"]
    return Remote(url=server["url"], client_id=oauth["clientId"], scope=oauth["scopes"], port=oauth["callbackPort"])


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


def sign_in(remote: Remote, timeout: float = 300) -> str:
    """An access token for `remote`, after the person signs in in the browser."""
    parts = urllib.parse.urlparse(remote.url)
    metadata = get_json(f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource{parts.path}")
    issuer = metadata["authorization_servers"][0]
    endpoints = get_json(f"{issuer}/.well-known/openid-configuration")  # Cognito has no RFC 8414 document

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://127.0.0.1:{remote.port}/callback"  # registered too; matches the listener below
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": remote.client_id,
            "redirect_uri": redirect_uri,
            "scope": remote.scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": remote.url,  # Cognito copies it into the access token's aud
        }
    )
    answer: dict[str, str] = {}
    done = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if urllib.parse.urlparse(self.path).path != "/callback":
                self.send_error(404)
                return
            answer.update(params)
            done.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab.\n")

        def log_message(self, *args: object) -> None:  # keep the code out of the terminal
            pass

    listener = http.server.HTTPServer(("127.0.0.1", remote.port), Callback)
    threading.Thread(target=listener.serve_forever, daemon=True).start()
    try:
        url = f"{endpoints['authorization_endpoint']}?{query}"
        print("Sign in in the browser window that just opened.", file=sys.stderr)
        if not webbrowser.open(url):
            print(f"Open this address to sign in: {url}", file=sys.stderr)
        if not done.wait(timeout):
            raise SystemExit("No sign-in within the time limit.")
    finally:
        listener.shutdown()
    if answer.get("state") != state or "code" not in answer:
        raise SystemExit(f"Sign-in failed: {answer.get('error', 'state mismatch')}")

    form = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": remote.client_id,
            "code": answer["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    ).encode()
    request = urllib.request.Request(
        endpoints["token_endpoint"], data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)["access_token"]
