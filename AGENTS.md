# Rules for AI coding agents working on ai-remote-mcp

This repo puts the ai-team-docs search behind a remote MCP server on a public Lambda function URL.
Anyone on the internet can reach the URL, so every change keeps the login checks intact.

## Non-negotiables

- The server has one tool, and it only reads. It calls the `team-docs-search` function and nothing
  else. Do not add tools that write or that call other services.
- Never pass the caller's token on. The server calls the search function with its own IAM role.
- Never log a token, an email or the question text. Log lines keep the Cognito `sub`, the question's
  length and timings (`server/remote_mcp/log.py`).
- The tool name, parameters, description, server instructions and result text stay the same as the
  local server in ai-team-docs (commit 63f1b07), so the two can be compared. `tests/test_render.py`
  checks the result text.
- Keep the Lambda guards: GET and DELETE on `/mcp` answer 405 before the SDK, and
  `subscriptions=False`. Without them an open event stream runs the function until it times out.
- Every token check stays: signature, issuer, expiry, `token_use`, app client, `aud` (this server's
  URL) and the scope. A change that loosens one needs a test that shows why.
- Do not commit AWS account ids, the function URL, the Cognito client id, a real `.mcp.json` or
  `infra/cdk.context.json`.

## How to work

- One purpose per PR. Run before pushing:
  `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/python -m pytest`,
  and `npx tsc && npx jest` in `infra/` when the stack changes.
- The MCP Python SDK is pinned to 2.3.x. Before raising it, read the SDK's release notes for
  changes to auth, `streamable_http_app` and stateless HTTP, and run the tests in `tests/test_http.py`.
- `requirements.txt` is built for Lambda (Linux arm64):
  `uv pip compile requirements.in --python-version 3.13 --python-platform aarch64-manylinux_2_28 -o requirements.txt`.
