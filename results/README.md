# Measurements

All on 2026-10-26 (UTC), from a laptop in Sydney to `ap-southeast-2`, with Claude Code 2.1.310 and
the MCP Python SDK 2.3.0 client in `scripts/measure.py`. One user, one machine: these numbers show
how the pieces behave, not how a team would load them.

## Sign-in with Claude Code (04:42-04:57)

- Claude Code signed in through Cognito's managed login with the pre-registered public client and
  callback port 8976, then called the tool. No `authServerMetadataUrl` was needed, although
  Cognito's discovery document has no `code_challenge_methods_supported`.
- The access token passed the `aud` check: Claude Code sent `resource`, and Cognito put the server
  URL into `aud`.
- The 401's `WWW-Authenticate` header reached the client renamed to
  `x-amzn-Remapped-www-authenticate` (Lambda function URL). Claude Code found the metadata at the
  well-known path.
- Claude Code negotiated protocol `2026-07-28` and sent its client info (`claude-code/2.1.310`).
- After Claude Code was closed and opened again, it did not ask to sign in again.

## Response time (`latency.json`, 05:07)

The same ten questions (ai-team-docs `evals/questions.yaml` q1-q10) to each server, after one
warm-up call each. Both servers call the same search function.

| Server | Median | Slowest |
|---|---|---|
| Local (stdio on the laptop, ai-team-docs) | 143.5 ms | 159 ms |
| Remote (this repo, function URL) | 170.5 ms | 201 ms |

The server's own log for the warm remote calls: median 132 ms in total, of which 130 ms waiting for
the search function. The rest of the difference is the extra network hop from the laptop.

## Cold start

A new execution environment of the server started in 2.1-2.7 s (`Init Duration` of five cold
starts: 2,134, 2,679, 2,103, 2,600 and 2,712 ms). The first call to a newly deployed version took
3.1 s at the client.

## Deploy and rollback (`watch-session-1.jsonl`, 05:07-05:11)

The server version was asked every second (272 answers, 0 errors) while a deploy and two alias
moves ran in GitHub Actions.

| Event | From start | First answer after the alias moved | Old version afterwards |
|---|---|---|---|
| Deploy `2fdb6c4` (push to main) | 90 s from the push (tests 35 s) | 0.7 s | none |
| Rollback to version 3 (`c912093`) | 13 s from the workflow start | 1.1 s | none |
| Roll forward to version 4 (`2fdb6c4`) | 11 s from the workflow start | 1.2 s | none |

"Alias moved": CloudFormation's `UPDATE_COMPLETE` for the alias (deploy), or the time the rollback
workflow logged after `update-alias` returned. Each new version then answered 30 times in a row.

## Logs

The CloudWatch Logs Insights query in the top-level README shows the calls per user, tool and server
version, for example 11 calls by one user to `search_team_docs` on `c912093`.

## Cost

Sydney prices from the AWS Price List (2026-10-22): $0.20 per million requests and $0.0000133334
per GB-second (arm64), before the free tier.

- The whole measurement session: 298 invocations, 3.98 GB-seconds, $0.0001.
- A warm tool call bills about 135 ms at 512 MB (most of it waiting for the search function):
  about $0.0011 per 1,000 calls. Each call writes about 0.6 KB of log lines, under $0.001 of
  CloudWatch Logs per 1,000 calls ($0.67 per GB ingested).
- The search function's cost is the same with the local server, so it is not counted here.
