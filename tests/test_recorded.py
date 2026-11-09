# SPDX-License-Identifier: Apache-2.0
"""A recorded Home Assistant API, replayed through MCP as the core's host calls
`homeassistant.apply`.

`horae_sdk.testing.replay` replays source and transform calls only; a notifier is driven
through its sink operations, so each case of `tests/fixtures/ha_api.json` (synthetic
parameters and intent, and HA's recorded `response`) is sent with `horae_sdk.testing.operation`
and its `expect` holds what the fake HA must receive (`request`, `null` when nothing is sent)
and what the core gets back (`result`, or the `error` kind). `HORAE_SDK_RECORD=1 uv run pytest`
rewrites `expect` (review the diff); record mode is refused in CI, like the SDK's own."""

import json
import os
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from horae_sdk.testing import CI_ENVS, RECORD_ENV, operation

from fakes import HOST, TOKEN, Recorder, notifier
from horae_homeassistant import TOOL, build

FIXTURES = Path(__file__).parent / "fixtures" / "ha_api.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURES.read_text("utf-8"))


def recording() -> bool:
    if not os.environ.get(RECORD_ENV):
        return False
    if any(os.environ.get(name) for name in CI_ENVS):
        raise RuntimeError("record mode is refused in CI")
    return True


def replay(case: dict[str, Any]) -> dict[str, Any]:
    recorded = case["response"]
    recorder = Recorder(lambda _: httpx2.Response(recorded["status"], json=recorded["body"]))
    plugin = build(notifier(recorder))
    arguments = {
        "paths": {},
        "intent": case["intent"],
        "tool": TOOL,
        "params": case["params"],
        "run_id": "run-synthetic",
        "index": 0,
        "dedupe_key": "0" * 64,
        "actions": case.get("actions", []),
    }
    result = anyio.run(
        partial(
            operation,
            network=case.get("network", [HOST]),
            network_local=case.get("network_local"),
        ),
        plugin,
        f"{TOOL.split('.')[0]}.apply",
        arguments,
    )
    request: dict[str, Any] | None = None
    if recorder.requests:
        [sent] = recorder.requests
        assert sent.headers["authorization"] == f"Bearer {TOKEN}"
        assert sent.method == "POST"
        request = {"url": str(sent.url), "json": recorder.json()}
    return {"request": request, "result": result}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_recorded_push(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_TOKEN", TOKEN)
    actual = replay(case)
    assert TOKEN not in json.dumps(actual)
    assert "synthetic-button" not in json.dumps(actual["result"])  # button URLs: request only
    if recording():
        case["expect"] = actual
        return
    assert actual == case["expect"]


def test_write_recordings() -> None:
    """Runs last (file order): writes the rewritten expectations in record mode."""
    if recording():
        FIXTURES.write_text(json.dumps(CASES, indent=2) + "\n", "utf-8")
    assert len(CASES) >= 4
