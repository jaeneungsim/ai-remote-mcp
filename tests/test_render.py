"""render.py must give the agent the same text as ai-team-docs mcp/render.py (commit 63f1b07).

The expected texts in fixtures/render-cases.json were produced by that file.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from remote_mcp import render

CASES = json.loads((Path(__file__).parent / "fixtures" / "render-cases.json").read_text())


@pytest.mark.parametrize("name", sorted(CASES))
def test_same_text_as_the_local_server(name: str) -> None:
    case = CASES[name]
    assert render.answer(case["response"], datetime.fromisoformat(case["now"])) == case["expected"]
