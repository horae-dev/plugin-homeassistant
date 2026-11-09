# SPDX-License-Identifier: Apache-2.0
"""The reminders source is read-only towards Home Assistant, structurally: the HTTP layer
sends only a GET of `/api/states` or `/api/states/<entity_id>`, whatever the configuration."""

from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest

from fakes import TOKEN, URL, Recorder
from horae_homeassistant import http
from horae_homeassistant.http import (
    ForbiddenRequest,
    Reader,
    ReadError,
    ReadOnlyTransport,
    read_allowed,
)
from horae_homeassistant.settings import parse_server

SERVER = parse_server(URL, False)


@pytest.mark.parametrize(
    "target", ["/api/states", "/api/states/sensor.door_battery", "/api/states/todo.a_1"]
)
def test_the_read_paths(target: str) -> None:
    assert read_allowed("GET", target)


@pytest.mark.parametrize(
    "target",
    [
        "/api/services/light/turn_on",
        "/api/services/notify/mobile_app_pixel",
        "/api/states/",
        "/api/states/sensor",
        "/api/states/sensor.a/extra",
        "/api/states/sensor.a?x=1",
        "/api/states?x=1",
        "/api/states/../services/light/turn_on",
        "/api/states/sensor.%2e%2e",
        "/api/states/Sensor.A",
        "/api/states/sensor.a b",
        "/api/config",
        "/api/history/period",
        "/api/template",
        "/api/events/x",
        "/api/calendars",
        "/api/",
        "//api/states",
        "/api/statesx",
        "api/states",
        "",
    ],
)
def test_nothing_else_is_a_read_path(target: str) -> None:
    assert not read_allowed("GET", target)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "get"])
def test_only_get_is_allowed(method: str) -> None:
    assert not read_allowed(method, "/api/states")
    assert not read_allowed(method, "/api/states/sensor.a")


def send(transport: httpx2.AsyncBaseTransport, request: httpx2.Request) -> httpx2.Response:
    return anyio.run(transport.handle_async_request, request)


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", f"{URL}/api/services/light/turn_on", b"{}"),
        ("POST", f"{URL}/api/states/sensor.a", b"{}"),
        ("POST", f"{URL}/api/states", None),
        ("PUT", f"{URL}/api/states/sensor.a", None),
        ("DELETE", f"{URL}/api/states/sensor.a", None),
        ("GET", f"{URL}/api/services/light/turn_on", None),
        ("GET", f"{URL}/api/states/sensor.a?x=1", None),
        ("GET", f"{URL}/api/config", None),
        ("GET", f"{URL}/api/states/sensor.a", b"{}"),
    ],
)
def test_the_transport_refuses_everything_else(method: str, url: str, body: bytes | None) -> None:
    recorder = Recorder()
    guard = ReadOnlyTransport(recorder(SERVER))
    with pytest.raises(ForbiddenRequest):
        send(guard, httpx2.Request(method, url, content=body))
    assert recorder.requests == []


def test_the_transport_passes_a_read() -> None:
    recorder = Recorder()
    guard = ReadOnlyTransport(recorder(SERVER))
    assert send(guard, httpx2.Request("GET", f"{URL}/api/states")).status_code == 200
    assert [r.method for r in recorder.requests] == ["GET"]


def test_the_session_refuses_a_path_before_building_a_request() -> None:
    recorder = Recorder()

    async def run() -> None:
        async with Reader(transport=recorder).open(SERVER, TOKEN) as session:
            await session.get("/api/services/light/turn_on")

    with pytest.raises(ForbiddenRequest):
        anyio.run(run)
    assert recorder.requests == []


def test_a_forbidden_request_is_not_a_read_failure() -> None:
    """It is a bug, never reported as a degraded result with a reason."""
    assert not issubclass(ForbiddenRequest, ReadError)


def test_the_source_never_touches_the_notifier_client() -> None:
    """The only POST of the package is the notifier's `Caller`; the source cannot reach it."""
    root = Path(http.__file__).parent
    assert "Caller" not in (root / "reminders.py").read_text("utf-8")
    reading = Path(http.__file__).read_text("utf-8").split("# --- the reminders source")[1]
    assert '"POST"' not in reading
    assert "notify" not in reading


async def _reads(recorder: Recorder, *paths: str) -> list[Any]:
    async with Reader(transport=recorder).open(SERVER, TOKEN) as session:
        return [await session.get(path) for path in paths]


def test_reads_send_the_token_and_only_gets() -> None:
    recorder = Recorder(lambda _: httpx2.Response(200, json=[]))
    anyio.run(_reads, recorder, "/api/states", "/api/states/sensor.a")
    assert [(r.method, r.url.path) for r in recorder.requests] == [
        ("GET", "/api/states"),
        ("GET", "/api/states/sensor.a"),
    ]
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in recorder.requests)
