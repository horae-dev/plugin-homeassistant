# SPDX-License-Identifier: Apache-2.0
"""One bounded call of an HA `notify` service, with the posture of the core's HTTP clients.

- **Only `notify.*`**: the URL is built by `Settings.endpoint` from the validated service name
  (`[a-z0-9_]+`) and `call` refuses (`internal`) any URL not under `/api/services/notify/`.
- **Explicit client**: `trust_env=False` (no ambient proxy, `.netrc` or `SSL_CERT_*`), TLS
  verified on the default trust store, **no redirect followed** (a 3xx is `redirect_refused`),
  the `httpx2` / `httpcore2` loggers pinned at `WARNING`.
- **Bounded**: connect 5 s, read and write 10 s, one deadline of `DEADLINE_S` for the whole
  call. At most `MAX_RESPONSE_BYTES` of the answer is read, and it is never used or echoed
  (HA answers a JSON list of changed states).
- **No retry**: a notification that may have reached HA is never sent twice by this plugin.
- **The token** is sent only as `Authorization: Bearer <token>`. It is never logged, never in an
  error (they are fixed texts written here) and never in the result; errors are raised outside
  any exception handler, so no `__context__` leads back to the request.

Failures are `SinkError` kinds: `auth_failed` (401, 403), `service_not_found` (404),
`rate_limited` (429), `server_unavailable` (5xx), `redirect_refused` (3xx),
`publish_refused` (another 4xx), `server_timeout`, `network_error`, `internal`.
"""

import json
import logging
import re
from collections.abc import Callable
from typing import Any

import anyio
import httpx2
from horae_sdk import SinkError

from horae_homeassistant.settings import Settings

DEADLINE_S = 20.0
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT = httpx2.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
LIMITS = httpx2.Limits(max_connections=2, max_keepalive_connections=0)
HTTP_LOGGERS = ("httpx2", "httpcore2")
USER_AGENT = "horae-plugin-homeassistant"
NOTIFY_PATH = "/api/services/notify/"

TransportFactory = Callable[[Settings], httpx2.AsyncBaseTransport]
"""Builds the transport for a call (tests inject a mock one)."""


def quiet_loggers() -> None:
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def default_transport(settings: Settings) -> httpx2.AsyncBaseTransport:
    return httpx2.AsyncHTTPTransport(verify=True, trust_env=False, limits=LIMITS, retries=0)


def _status_error(status: int) -> SinkError:
    if 300 <= status < 400:
        return SinkError("redirect_refused", f"Home Assistant answered {status} (a redirect)")
    if status in (401, 403):
        return SinkError("auth_failed", f"Home Assistant refused the access token ({status})")
    if status == 404:
        return SinkError("service_not_found", "Home Assistant has no such notify service (404)")
    if status == 429:
        return SinkError("rate_limited", "Home Assistant is rate limiting (429)")
    if status >= 500:
        return SinkError("server_unavailable", f"Home Assistant failed ({status})")
    return SinkError("publish_refused", f"Home Assistant refused the notification ({status})")


class Caller:
    """Calls one notify service (`call`); `transport` is the seam for tests."""

    def __init__(
        self, *, transport: TransportFactory | None = None, deadline_s: float = DEADLINE_S
    ) -> None:
        self._transport = transport or default_transport
        self.deadline_s = deadline_s

    async def _send(
        self, client: httpx2.AsyncClient, url: str, body: bytes, headers: dict[str, str]
    ) -> int:
        async with client.stream("POST", url, content=body, headers=headers) as r:
            read = 0
            async for chunk in r.aiter_raw():  # drained (bounded) and dropped, never used
                read += len(chunk)
                if read > MAX_RESPONSE_BYTES:
                    break
            return r.status_code

    async def call(self, settings: Settings, message: dict[str, Any], token: str) -> None:
        """POST `message` to `settings.endpoint`; returns when HA accepted it (2xx)."""
        url = settings.endpoint
        if not re.fullmatch(re.escape(settings.base + NOTIFY_PATH) + r"[a-z0-9_]{1,64}", url):
            raise SinkError("internal", "refusing a URL outside HA's notify services")
        quiet_loggers()
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }
        failure: SinkError | None = None
        status = 0
        try:
            with anyio.fail_after(self.deadline_s):
                client = httpx2.AsyncClient(
                    transport=self._transport(settings),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=TIMEOUT,
                    limits=LIMITS,
                )
                async with client:
                    status = await self._send(client, url, body, headers)
        except (TimeoutError, httpx2.TimeoutException):
            failure = SinkError("server_timeout", "Home Assistant did not answer in time")
        except httpx2.HTTPError:
            failure = SinkError("network_error", "the connection to Home Assistant failed")
        if failure is not None:
            raise failure
        if not 200 <= status < 300:
            raise _status_error(status)
