# SPDX-License-Identifier: Apache-2.0
"""A recorded Home Assistant API, replayed through MCP as the core's host calls
`homeassistant.reminders`.

`tests/fixtures/ha_states.json` holds the recorded `/api/states` answer (synthetic entities) and
cases: the call's parameters, optional `api` overrides (a path and the status and body HA
answered) and `expect`: the GETs HA must receive, and what the core gets back (`data` and
`intents`, or the tool `error`). `HORAE_SDK_RECORD=1 uv run pytest` rewrites `expect` (review the
diff); record mode is refused in CI, like the SDK's own."""

import json
import os
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from horae_sdk.testing import CI_ENVS, RECORD_ENV, call

from fakes import HOST, TOKEN, Recorder
from horae_homeassistant import build
from horae_homeassistant.http import Reader

FIXTURES = Path(__file__).parent / "fixtures" / "ha_states.json"
RECORDED: dict[str, Any] = json.loads(FIXTURES.read_text("utf-8"))
STATES: list[dict[str, Any]] = RECORDED["states"]
CASES: list[dict[str, Any]] = RECORDED["cases"]


def recording() -> bool:
    if not os.environ.get(RECORD_ENV):
        return False
    if any(os.environ.get(name) for name in CI_ENVS):
        raise RuntimeError("record mode is refused in CI")
    return True


def ha_answer(case: dict[str, Any], request: httpx2.Request) -> httpx2.Response:
    path = request.url.path
    if path in case["api"]:
        recorded = case["api"][path]
        return httpx2.Response(recorded["status"], json=recorded["body"])
    if path == "/api/states":
        return httpx2.Response(200, json=STATES)
    for state in STATES:
        if path == f"/api/states/{state['entity_id']}":
            return httpx2.Response(200, json=state)
    return httpx2.Response(404, json={"message": "Entity not found."})


def replay(case: dict[str, Any]) -> tuple[Recorder, dict[str, Any]]:
    recorder = Recorder(lambda request: ha_answer(case, request))
    plugin = build(reader=Reader(transport=recorder), env={"HA_TOKEN": TOKEN})
    params = {**case["params"], "today": case["today"]}
    replayed = anyio.run(lambda: call(plugin, "homeassistant.reminders", params, network=[HOST]))
    gets = [
        {"method": sent.method, "path": sent.url.path, "query": sent.url.query.decode()}
        for sent in recorder.requests
    ]
    expect: dict[str, Any] = {"requests": gets}
    if replayed.error is not None:
        expect["error"] = replayed.error
    else:
        expect.update(data=replayed.data, intents=replayed.intents)
    return recorder, expect


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_recorded_reminders(case: dict[str, Any]) -> None:
    recorder, actual = replay(case)
    for sent in recorder.requests:
        assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    text = json.dumps(actual)
    assert TOKEN not in text
    assert "secret-body-text" not in text
    assert "ha.example.org" not in text
    if recording():
        case["expect"] = actual
        return
    assert actual == case["expect"]


def test_write_recordings() -> None:
    """Runs last (file order): writes the rewritten expectations in record mode."""
    if recording():
        FIXTURES.write_text(json.dumps(RECORDED, indent=2, ensure_ascii=False) + "\n", "utf-8")
    assert len(CASES) >= 6
