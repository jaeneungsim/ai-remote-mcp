# ai-remote-mcp

A remote MCP server for the team docs search from [ai-team-docs](https://github.com/jaeneungsim/ai-team-docs).
Instead of every developer running the MCP server on their own machine, one server runs on AWS
Lambda. Developers approve it once in Claude Code and sign in with their own account.

```
Claude Code ── HTTPS + access token ──> Lambda function URL (live alias)
                                          │  remote MCP server (Python, MCP SDK 2.3, Lambda Web Adapter)
                                          │  checks the token, logs one line per call
                                          └─> team-docs-search (ai-team-docs), with the server's own role
Cognito (managed login) ── access token with aud = this server ──> Claude Code
```

- **One place to change:** a deploy publishes a new Lambda version and moves the `live` alias to it.
  The next request from every developer is answered by the new version. A rollback moves the alias back.
- **No company credentials on laptops:** the server calls the search function with its own IAM
  role. A developer's machine holds only the token for this server.
- **One log for everyone:** each tool call writes one JSON line with the user (Cognito `sub`), the
  client and protocol version, the server version and timings. Never the token or the question.

## How sign-in works

The server follows the MCP authorization spec as a resource server, and Cognito is the
authorization server.

1. Claude Code calls `/mcp` without a token and gets 401.
2. It reads the protected resource metadata at `/.well-known/oauth-protected-resource/mcp`, which
   names the Cognito user pool as the authorization server.
3. It opens Cognito's managed login in the browser, with PKCE, the fixed callback port from
   `.mcp.json` and `resource` set to the server URL.
4. Cognito puts the server URL into the access token's `aud` (RFC 8707 resource indicators,
   Essentials tier, managed login only).
5. The server checks each token: signature, issuer, expiry, `token_use`, app client, `aud` and the
   `search` scope (`server/remote_mcp/auth.py`).

Cognito has no dynamic client registration and no client ID metadata documents, so the Claude Code
client is registered in advance as a public client (no secret). Cognito matches callback URLs
exactly, so the port is fixed in both the app client and `.mcp.json`.

## What did not work as planned

- **Lambda function URLs rename `WWW-Authenticate`.** The server sends the 401 challenge with
  `resource_metadata` and `scope`, but the function URL returns it as
  `x-amzn-Remapped-www-authenticate`. Clients find the metadata at the well-known path instead,
  which the MCP spec allows (a server needs either the header or the well-known URI), and the scope
  is fixed in `.mcp.json`.
- **A Cognito resource server name cannot be a URL.** CDK uses the identifier as the name by
  default, and names may not contain `:` or `/`. The identifier is the server URL, so the name is set
  separately.
- **The SDK's 401 has no `scope`.** A thin wrapper around the MCP app adds it, answers GET and DELETE
  with 405 (an open event stream would keep the function running until it times out) and logs
  refused calls.

## Setup

Needs: the ai-team-docs stack deployed in the same account and Region (its `team-docs-search`
function), Node 22, [uv](https://docs.astral.sh/uv/), and the AWS CLI.

1. Deploy: `cd infra && npm ci && npx cdk deploy --outputs-file cdk-outputs.json`.
   Without the `github` context in `infra/cdk.json`, no CI roles are created.
2. Create a user (self sign-up is off):
   ```
   aws cognito-idp admin-create-user --user-pool-id <UserPoolId> --username <name> --message-action SUPPRESS
   aws cognito-idp admin-set-user-password --user-pool-id <UserPoolId> --username <name> --password '<password>' --permanent
   ```
3. Copy `.mcp.json.example` to `.mcp.json` and fill in `ServerUrl`, `ClientId` and `Scope` from
   the deploy outputs. In a team repo this file is committed, so everyone gets the same server.
4. Start Claude Code in the repo, approve `team-docs-remote`, then sign in from `/mcp`.

Check a deployed server from outside, without a token: `python3 scripts/check_deploy.py <ServerUrl>`.

## Deploy and rollback

- `.github/workflows/deploy.yml`: tests every pull request; on `main` it deploys and runs the
  outside check. It assumes `DEPLOY_ROLE_ARN`, which may only assume the CDK bootstrap roles.
- `.github/workflows/rollback.yml`: run by hand with a Lambda version number. It moves the `live`
  alias with `ROLLBACK_ROLE_ARN`, which may only read and move the alias.
- Both roles trust only the `main` branch of this repo, matched by GitHub's numeric owner and repo ids.

## Logs

One JSON line per tool call or refused call in `/aws/lambda/ai-remote-mcp`. A CloudWatch Logs
Insights query for calls per user, tool and version:

```
fields @timestamp, user, tool, ms, server_version
| filter event = "tool_call"
| stats count(*) as calls, avg(ms) as avg_ms by user, tool, server_version
```

## Limits

- The account's Lambda concurrency limit is shared with every other function in it, and requests
  without a token still start the function. A team would add per-user rate limits.
- One user, made by hand. A company would connect its own identity provider to Cognito, or use
  that provider directly.
