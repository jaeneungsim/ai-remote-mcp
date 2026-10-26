"""The server as Claude Code sees it: real HTTP to uvicorn, the SDK's own client, a stand-in search function."""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import Implementation

from conftest import SCOPE, SERVER_URL, log_lines, make_token

ORIGIN = SERVER_URL.removesuffix("/mcp")
SEARCH_RESPONSE = {
    "warnings": [],
    "results": [
        {
            "rank": 1,
            "title": "Roll back dev",
            "heading": "Fast path",
            "url": "https://example.atlassian.net/wiki/spaces/INV/pages/101",
            "version": 3,
            "edited_at": "2026-10-20T09:24:00+00:00",
            "synced_at": "2026-10-20T09:30:05+00:00",
            "text": "Do not re-run old deploys.",
        }
    ],
    "index": {"last_success_at": "2026-10-20T09:30:05+00:00", "pages": 30},
}


@pytest.fixture
def searches(app_module: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_invoke(function: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append({"function": function, **payload})
        return SEARCH_RESPONSE

    monkeypatch.setattr(app_module.search, "invoke", fake_invoke)
    return calls


def post(token: str | None = None, **headers: str) -> httpx.Response:
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    return httpx.post(
        SERVER_URL, json=body, headers={"Accept": "application/json, text/event-stream", **headers}, timeout=10
    )


async def call(mode: str = "auto", **arguments: Any) -> tuple[Any, Any, Any]:
    headers = {"Authorization": f"Bearer {make_token()}", "x-amzn-lambda-context": json.dumps({"request_id": "req-1"})}
    async with (
        create_mcp_http_client(headers=headers) as http,
        Client(
            streamable_http_client(SERVER_URL, http_client=http),
            mode=mode,
            client_info=Implementation(name="pytest", version="1.0"),
        ) as client,
    ):
        tools = await client.list_tools()
        result = await client.call_tool("search_team_docs", arguments)
        return tools, result, client.server_info


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_get_and_delete_get_405_before_the_sdk(live_server: str, method: str) -> None:
    response = httpx.request(method, live_server, headers={"Accept": "text/event-stream"}, timeout=5)
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


def test_no_token_gets_401_that_points_to_the_metadata_and_scope(live_server: str, capsys: Any) -> None:
    response = post()
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert f'scope="{SCOPE}"' in challenge
    [line] = [line for line in log_lines(capsys.readouterr().out) if line["event"] == "auth_rejected"]
    assert line["reason"] == "missing_token"
    assert line["status"] == 401


def test_token_for_another_server_gets_401_and_the_reason_is_logged(live_server: str, capsys: Any) -> None:
    response = post(make_token(aud="https://another-server.example/mcp"))
    assert response.status_code == 401
    [line] = [line for line in log_lines(capsys.readouterr().out) if line["event"] == "auth_rejected"]
    assert line["reason"] == "bad_audience"
    assert "token" not in json.dumps(line).lower().replace("invalid_token", "")


def test_token_without_the_search_scope_gets_403(live_server: str, capsys: Any) -> None:
    response = post(make_token(scope="openid"))
    assert response.status_code == 403
    assert "insufficient_scope" in response.headers["www-authenticate"]
    [line] = [line for line in log_lines(capsys.readouterr().out) if line["event"] == "auth_rejected"]
    assert line["reason"] == "missing_scope"


def test_requests_from_a_web_page_are_refused(live_server: str) -> None:
    assert post(make_token(), Origin="https://evil.example").status_code == 403


def test_other_host_names_are_refused(live_server: str) -> None:
    assert post(make_token(), Host="attacker.example").status_code == 421


def test_metadata_is_the_same_at_both_well_known_paths(live_server: str) -> None:
    path_form = httpx.get(f"{ORIGIN}/.well-known/oauth-protected-resource/mcp", timeout=5)
    root_form = httpx.get(f"{ORIGIN}/.well-known/oauth-protected-resource", timeout=5)
    assert path_form.status_code == root_form.status_code == 200
    assert path_form.json() == root_form.json()
    assert path_form.json() == {
        "resource": SERVER_URL,
        "authorization_servers": ["https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_TestPool1"],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
    }


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_tool_call_returns_the_rendered_text_and_logs_one_line(
    live_server: str, searches: list, capsys: Any, mode: str
) -> None:
    tools, result, server_info = anyio.run(lambda: call(mode, query="how do I roll back dev?", top_k=2))
    [tool] = tools.tools
    assert tool.name == "search_team_docs"
    assert tool.annotations.read_only_hint is True
    assert set(tool.input_schema["properties"]) == {"query", "top_k"}
    assert server_info.version == "abc1234"
    assert searches == [{"function": "team-docs-search", "query": "how do I roll back dev?", "top_k": 2}]
    assert not result.is_error
    assert result.content[0].text.startswith("[1] Roll back dev > Fast path\n")

    [line] = [line for line in log_lines(capsys.readouterr().out) if line["event"] == "tool_call"]
    assert line["user"] == "11111111-2222-3333-4444-555555555555"
    assert line["request_id"] == "req-1"
    assert line["query_chars"] == len("how do I roll back dev?")
    assert "query" not in line
    assert line["results"] == 1
    assert line["server_version"] == "abc1234"
    assert line["ms"] >= line["search_ms"] >= 0
    # 2026-07-28 requests carry clientInfo; a legacy stateless request has only the User-Agent header.
    assert line["client"] == "pytest/1.0" if mode == "auto" else line["client"].startswith("python-httpx2/")
    assert line["protocol"] == ("2026-07-28" if mode == "auto" else "2025-11-25")


def test_a_failed_search_is_an_error_result_and_a_log_line(live_server: str, app_module: Any, monkeypatch, capsys):
    def broken(function: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise app_module.search.SearchFailed("The search failed: index missing")

    monkeypatch.setattr(app_module.search, "invoke", broken)
    _, result, _ = anyio.run(lambda: call(query="anything"))
    assert result.is_error
    assert "index missing" in result.content[0].text
    [line] = [line for line in log_lines(capsys.readouterr().out) if line["event"] == "tool_call"]
    assert line["error"] == "SearchFailed"


def test_the_post_deploy_check_passes_against_the_server(live_server: str, capsys: Any) -> None:
    import check_deploy

    check_deploy.main(live_server)
    assert "FAIL" not in capsys.readouterr().out
