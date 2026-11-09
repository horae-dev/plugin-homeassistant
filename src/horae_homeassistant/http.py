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

**The reminders source** reads HA with `Reader`, which can only `GET` `/api/states` and
`/api/states/<entity_id>` (`READ_PATHS`): the path is checked before the request and again by
`ReadOnlyTransport`, which refuses any other method, path, query or body, so no service can be
called through it, whatever the configuration says. Its failures are `ReadError`s with a fixed
`reason` (`REASONS`); HA's answer, the URL and the token are never in them.

Failures of the notifier are `SinkError` kinds: `auth_failed` (401, 403), `service_not_found` (404),
`rate_limited` (429), `server_unavailable` (5xx), `redirect_refused` (3xx),
`publish_refused` (another 4xx), `server_timeout`, `network_error`, `internal`.
"""

import json
import logging
import re
from collections.abc import Callable
from typing import Any, Self

import anyio
import httpx2
from horae_sdk import SinkError

from horae_homeassistant.settings import Server, Settings

DEADLINE_S = 20.0
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT = httpx2.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
LIMITS = httpx2.Limits(max_connections=2, max_keepalive_connections=0)
HTTP_LOGGERS = ("httpx2", "httpcore2")
USER_AGENT = "horae-plugin-homeassistant"
NOTIFY_PATH = "/api/services/notify/"

TransportFactory = Callable[[Server], httpx2.AsyncBaseTransport]
"""Builds the transport for a call (tests inject a mock one)."""


def quiet_loggers() -> None:
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def default_transport(settings: Server) -> httpx2.AsyncBaseTransport:
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


# --- the reminders source: GET only -------------------------------------------------------

STATES_PATH = "/api/states"
ENTITY_ID = r"[a-z0-9_]{1,64}\.[a-z0-9_]{1,128}"
READ_PATHS = (
    re.compile(re.escape(STATES_PATH)),
    re.compile(re.escape(STATES_PATH + "/") + ENTITY_ID),
)
"""The only paths the source can reach: all the states, or one entity's state."""
MAX_STATES_BYTES = 4 * 1024 * 1024
MAX_ENTITY_BYTES = 64 * 1024
REASONS = {
    "auth_failed": "Home Assistant refused the access token",
    "entity_not_found": "Home Assistant has no such entity",
    "rate_limited": "Home Assistant is rate limiting",
    "server_unavailable": "Home Assistant failed",
    "redirect_refused": "Home Assistant answered a redirect",
    "request_refused": "Home Assistant refused the request",
    "response_too_large": "Home Assistant's answer is too large",
    "response_invalid": "Home Assistant's answer is not what was expected",
    "server_timeout": "Home Assistant did not answer in time",
    "network_error": "the connection to Home Assistant failed",
}
"""Every `ReadError.reason` and its fixed text."""


class ReadError(Exception):
    """A read that failed; `reason` is one of `REASONS` (the only text that leaves this module)."""

    def __init__(self, reason: str) -> None:
        super().__init__(REASONS[reason])
        self.reason = reason


class ForbiddenRequest(Exception):
    """A request that is not a GET of `READ_PATHS`: never sent."""


def read_allowed(method: str, target: str) -> bool:
    """Whether `method` and the request target (path and query, as sent) are a GET of
    `READ_PATHS`, exactly: no query, no fragment, no encoding, no trailing slash."""
    return method == "GET" and any(pattern.fullmatch(target) for pattern in READ_PATHS)


class ReadOnlyTransport(httpx2.AsyncBaseTransport):
    """Wraps a transport and refuses (`ForbiddenRequest`) anything but a GET of `READ_PATHS`
    with no body, before it reaches the network."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        target = request.url.raw_path.decode("ascii", "replace")
        if not read_allowed(request.method, target) or request.content:
            raise ForbiddenRequest("only GET of /api/states[/<entity_id>] is allowed")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _read_status(status: int) -> str | None:
    if 200 <= status < 300:
        return None
    if 300 <= status < 400:
        return "redirect_refused"
    if status in (401, 403):
        return "auth_failed"
    if status == 404:
        return "entity_not_found"
    if status == 429:
        return "rate_limited"
    return "server_unavailable" if status >= 500 else "request_refused"


class Reader:
    """Opens read-only sessions with an HA server (`transport` is the seam for tests)."""

    def __init__(self, *, transport: TransportFactory | None = None) -> None:
        self._transport = transport or default_transport

    def open(self, server: Server, token: str) -> "ReadSession":
        """`async with reader.open(server, token) as session:` then `await session.get(path)`."""
        quiet_loggers()
        client = httpx2.AsyncClient(
            transport=ReadOnlyTransport(self._transport(server)),
            trust_env=False,
            follow_redirects=False,
            timeout=TIMEOUT,
            limits=LIMITS,
        )
        return ReadSession(client, server.base, token)


class ReadSession:
    """One run's connection: `get(path)` is the parsed JSON of a GET of `READ_PATHS`."""

    def __init__(self, client: httpx2.AsyncClient, base: str, token: str) -> None:
        self._client = client
        self._base = base
        self._token = token

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _fetch(self, path: str, limit: int) -> tuple[int, bytes | None]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }
        async with self._client.stream("GET", self._base + path, headers=headers) as r:
            chunks: list[bytes] = []
            read = 0
            async for chunk in r.aiter_raw():
                read += len(chunk)
                if read > limit:
                    return r.status_code, None
                chunks.append(chunk)
            return r.status_code, b"".join(chunks)

    async def get(self, path: str) -> Any:
        """The JSON answer of `GET <base><path>`, or `ReadError` with a fixed reason."""
        if not read_allowed("GET", path):
            raise ForbiddenRequest("only GET of /api/states[/<entity_id>] is allowed")
        limit = MAX_STATES_BYTES if path == STATES_PATH else MAX_ENTITY_BYTES
        failure: str | None = None
        status = 0
        raw: bytes | None = None
        try:
            status, raw = await self._fetch(path, limit)
        except (TimeoutError, httpx2.TimeoutException):
            failure = "server_timeout"
        except httpx2.HTTPError:
            failure = "network_error"
        if failure is None:
            failure = _read_status(status)
        if failure is None and raw is None:
            failure = "response_too_large"
        data: Any = None
        if failure is None and raw is not None:
            try:
                data = json.loads(raw)
            except ValueError:
                failure = "response_invalid"
        if failure is not None:
            raise ReadError(failure)
        return data
