# SPDX-License-Identifier: Apache-2.0
"""Shared fakes: an in-memory Home Assistant API (httpx mock transport). Nothing touches the
network. The token and the host are synthetic."""

import json
from collections.abc import Callable
from typing import Any

import httpx2

from horae_homeassistant.http import Caller
from horae_homeassistant.plugin import HomeAssistantNotifier
from horae_homeassistant.settings import Server

TOKEN = "eyJ.synthetic-ha-token_0123456789"  # noqa: S105 - a fake token, for tests only
HOST = "ha.example.org"
URL = f"https://{HOST}"

Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    """A transport that records requests and answers `handler` (HA's `[]` by default)."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.handler = handler or (lambda request: httpx2.Response(200, json=[]))

    def __call__(self, settings: Server) -> httpx2.AsyncBaseTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            answer = self.handler(request)
            # A streamed answer, as a real transport gives (the caller streams it).
            stream = httpx2.ByteStream(answer.content)
            return httpx2.Response(answer.status_code, headers=answer.headers, stream=stream)

        return httpx2.MockTransport(handle)

    def json(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content.decode("utf-8"))


def notifier(recorder: Recorder, *, environ: dict[str, str] | None = None) -> HomeAssistantNotifier:
    return HomeAssistantNotifier(
        Caller(transport=recorder), env={"HA_TOKEN": TOKEN} if environ is None else environ
    )
