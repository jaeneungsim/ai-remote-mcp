"""Measurements for the write-up. Signs in once in the browser (scripts/login.py), then:

  latency  Sends the same questions to the local server (stdio, ai-team-docs) and to the remote
           server, one warm-up call each first, and reports the median and the slowest call.
  watch    Asks the remote server for its version every second while a deploy or a rollback runs,
           and records each answer. `changes` then reads the record against the time the
           deploy finished or the alias moved.
  run      latency, then watch, with one sign-in (an access token lasts an hour).

Results go to results/. They hold timings, versions and question numbers, never a token.

  .venv/bin/python scripts/measure.py latency
  .venv/bin/python scripts/measure.py watch --label deploy-1 --minutes 10
  .venv/bin/python scripts/measure.py run --label session-1 --minutes 15
  .venv/bin/python scripts/measure.py changes results/watch-deploy-1.jsonl --since 2026-10-26T05:01:21Z
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
import login

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
TEAM_DOCS = Path.home() / "Development" / "projects" / "ai-team-docs" / "mcp"
STABLE = 30  # answers in a row from the new version before it counts as served to everyone

# Ten questions from ai-team-docs evals/questions.yaml (q1-q10), the same for both servers.
QUESTIONS = [
    "dev broke right after something got merged. What is the quickest way to get the previous working version "
    "running again?",
    "If I redeploy an older commit to fix dev, is that the end of it, or is there something else I have to do?",
    "The AI code review stopped running and every PR now waits for a human. How do I get the reviews going again?",
    "Someone sent me the id of a request that failed. Where can I see what the API did with it?",
    "Why does an item come back with a yes/no flag for whether it can be bought, instead of clients working that "
    "out from the stock number?",
    "Why is every endpoint handled by a single function rather than one function per endpoint?",
    "When I create a new item through the API, how many units does it have, and will the shop show it as available?",
    "Can a client set how many units are on hand when it creates an item?",
    "What happens if someone sends a DELETE request for an item?",
    "If I fetch an item with an id full of odd characters, do I get a bad request error?",
]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def remote_client(remote: login.Remote, token: str) -> tuple[Any, Client]:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    return http, Client(streamable_http_client(remote.url, http_client=http))


def local_client() -> Client:
    command = StdioServerParameters(
        command="uv",
        args=["run", "--directory", str(TEAM_DOCS), "python", "server.py"],
        env={"AWS_PROFILE": "team-docs-search"},  # merged over the SDK's default environment (HOME, PATH, ...)
    )
    return Client(command)


async def timed_calls(client: Client) -> tuple[list[dict[str, Any]], str | None]:
    async with client:
        await client.call_tool("search_team_docs", {"query": "warm-up: how do I deploy to dev?"})
        calls = []
        for number, question in enumerate(QUESTIONS, start=1):
            started = time.perf_counter()
            result = await client.call_tool("search_team_docs", {"query": question})
            calls.append({"q": number, "ms": round((time.perf_counter() - started) * 1000), "error": result.is_error})
        return calls, client.server_info.version if client.server_info else None


def summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    times = [c["ms"] for c in calls if not c["error"]]
    return {
        "calls": len(times),
        "median_ms": statistics.median(times),
        "max_ms": max(times),
        "errors": len(calls) - len(times),
    }


async def latency(remote: login.Remote, token: str) -> dict[str, Any]:
    http, client = remote_client(remote, token)
    async with http:
        remote_calls, version = await timed_calls(client)
    local_calls, _ = await timed_calls(local_client())
    return {
        "at": now(),
        "remote_version": version,
        "remote": summary(remote_calls),
        "local": summary(local_calls),
        "remote_calls": remote_calls,
        "local_calls": local_calls,
    }


async def watch(remote: login.Remote, token: str, minutes: float, interval: float, out: Path) -> None:
    deadline = time.monotonic() + minutes * 60
    last = None
    with out.open("a") as record:
        while time.monotonic() < deadline:
            started = time.perf_counter()
            entry: dict[str, Any] = {"t": now()}
            try:
                http, client = remote_client(remote, token)
                async with http, client:
                    entry["version"] = client.server_info.version if client.server_info else None
            except Exception as err:  # a deploy or a cold start may refuse a request; keep watching
                entry["error"] = type(err).__name__
            entry["ms"] = round((time.perf_counter() - started) * 1000)
            record.write(json.dumps(entry) + "\n")
            record.flush()
            if entry.get("version") != last:
                print(f"{entry['t']} version {entry.get('version')} ({entry['ms']} ms)", file=sys.stderr)
                last = entry.get("version")
            await anyio.sleep(max(interval - (time.perf_counter() - started), 0))


def changes(path: Path, since: str) -> dict[str, Any]:
    """From `since` (deploy finished or alias moved): first answer from a new version, and when that
    version had answered STABLE times in a row."""
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    before = [e for e in entries if datetime.fromisoformat(e["t"].replace("Z", "+00:00")) < start and e.get("version")]
    old = before[-1]["version"] if before else None
    after = [e for e in entries if datetime.fromisoformat(e["t"].replace("Z", "+00:00")) >= start]
    first = next((e for e in after if e.get("version") and e["version"] != old), None)
    if not first:
        return {"old": old, "new": None}
    new = first["version"]
    streak, stable_at = 0, None
    for entry in after[after.index(first) :]:
        streak = streak + 1 if entry.get("version") == new else 0
        if streak == STABLE:
            stable_at = entry["t"]
            break

    def seconds(t: str | None) -> float | None:
        return round((datetime.fromisoformat(t.replace("Z", "+00:00")) - start).total_seconds(), 1) if t else None

    old_after = [e for e in after[after.index(first) :] if e.get("version") == old]
    return {
        "old": old,
        "new": new,
        "first_new_after_s": seconds(first["t"]),
        "stable_after_s": seconds(stable_at),
        "old_answers_after_first_new": len(old_after),
        "errors_after_start": sum(1 for e in after if "error" in e),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("latency")
    for name in ("watch", "run"):
        watching = commands.add_parser(name)
        watching.add_argument("--label", required=True)
        watching.add_argument("--minutes", type=float, default=10)
        watching.add_argument("--interval", type=float, default=1.0)
    reading = commands.add_parser("changes")
    reading.add_argument("path", type=Path)
    reading.add_argument("--since", required=True, help="UTC time the deploy finished or the alias moved")
    args = parser.parse_args()

    RESULTS.mkdir(exist_ok=True)
    if args.command == "changes":
        print(json.dumps(changes(args.path, args.since), indent=2))
        return
    remote = login.remote_from_mcp_json()
    token = login.sign_in(remote)
    if args.command in ("latency", "run"):
        result = anyio.run(latency, remote, token)
        (RESULTS / "latency.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({k: result[k] for k in ("at", "remote_version", "remote", "local")}, indent=2), flush=True)
    if args.command in ("watch", "run"):
        print(f"{now()} watching for {args.minutes} min", file=sys.stderr, flush=True)
        anyio.run(watch, remote, token, args.minutes, args.interval, RESULTS / f"watch-{args.label}.jsonl")


if __name__ == "__main__":
    main()
