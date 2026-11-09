# SPDX-License-Identifier: Apache-2.0
"""The plugin: `homeassistant.push`, a notifier that applies `notify.push` intents through a
Home Assistant `notify` service, and `homeassistant.reminders`, a read-only source that proposes
reminders from HA state (`horae_homeassistant.reminders`).

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_homeassistant`). A pipeline's `notify` step names `homeassistant.push: {...}` (its trusted
parameters: `horae_homeassistant.settings`); the core routes each `notify.push` intent to it and
applies it through the sink operation `homeassistant.apply` (the SDK's `Notifier`). The core
journals the notification and skips a `dedupe_key` already sent, so a keyed intent (`key`) is
pushed once; the notifier adds HA's `tag` (from that key) so a re-send replaces rather than
duplicates.

**Network and auth.** `POST <url>/api/services/notify/<service>` with the long-lived access token
from the `HA_TOKEN` secret as a Bearer header (`horae_homeassistant.http`): https by default,
`http://` only with `allow_http: true`; the host is checked against the network permissions
approved in `horae.lock` (`ApplyRequest.host_allowed`: `False` refuses, `None` (an older core)
falls back to the configuration); no redirects; timeouts; the token and HA's answer are never
logged or echoed. The sink can reach no HA service other than `notify.*`.

**What `apply` returns** (`Applied`): the target `<url>/notify.<service>` and `details`:
`delivery: "sent"`, `priority`, `actions` (the number of buttons sent, never their URLs) and
`click: "dropped"` when an `obsidian://` link was not sent. Nothing in it comes from the
proposer's text, and the token is never in it.

**Not implemented** (left out on purpose): quiet hours and once-a-day reminder state (the core's
`key` dedupe covers repeats), the companion apps' event-firing actions, notification channels,
images and TTS, and `http://` LAN-only address checks (the operator's `allow_http` opt-in is the
control).
"""

import os
from collections.abc import Mapping
from typing import Any

from horae_sdk import Applied, ApplyRequest, Notifier, Paths, Permissions, Plugin, SinkError

from horae_homeassistant.http import Caller, Reader
from horae_homeassistant.message import is_obsidian, payload
from horae_homeassistant.reminders import TOOL as REMINDERS_TOOL
from horae_homeassistant.reminders import build_source
from horae_homeassistant.settings import TOKEN_SECRET, Settings, parse

NAME = "homeassistant"
"""The plugin's name, also the namespace of its refs (`homeassistant.push`)."""
VERSION = "0.2.0"
TOOL = f"{NAME}.push"
COMMAND = ["python3", "-m", "horae_homeassistant"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
PERMISSIONS = Permissions(
    intents=["notify.push", "task.create"], network=[], secrets=[TOKEN_SECRET]
)
"""`notify.push` (applied, never proposed) and the access token. There is no default host: the
operator's HA host is approved in `horae.lock` (README, "Setup"); `apply` refuses a host that
is not in the approved list the core sends (horae-sdk 0.5)."""
_TOKEN_CHARS = frozenset(chr(code) for code in range(0x21, 0x7F))


class HomeAssistantNotifier(Notifier):
    """`homeassistant.push`. `caller` and `env` are seams for tests (a mock transport, a fake
    environment)."""

    def __init__(
        self, caller: Caller | None = None, *, env: Mapping[str, str] | None = None
    ) -> None:
        super().__init__([TOOL])
        self.caller = caller or Caller()
        self._env = env

    def identity(self, paths: Paths) -> str:
        return NAME

    def _token(self) -> str:
        environ = os.environ if self._env is None else self._env
        token = environ.get(TOKEN_SECRET)
        if not token:
            raise SinkError("secret_missing", f"{TOKEN_SECRET} is not set")
        if len(token) > 4096 or not set(token) <= _TOKEN_CHARS:
            raise SinkError("secret_invalid", f"{TOKEN_SECRET} is not a valid access token")
        return token

    @staticmethod
    def _check_approved(request: ApplyRequest, settings: Settings) -> None:
        """Refuses a host `horae.lock` does not approve, before anything is read or sent. The
        lock matches `https` URLs only, so the check is on the host in its https form (it also
        covers the `http://` form of that server). `None` is an older core that sent no list:
        the configured URL stays trusted pipeline configuration."""
        if request.host_allowed(f"https://{settings.host}") is False:
            raise SinkError(
                "config_invalid",
                f"homeassistant.push parameter url: host {settings.host} is not approved in "
                "horae.lock (permissions.network); nothing was sent",
            )

    async def apply(self, request: ApplyRequest) -> Applied:
        if request.intent.get("type") != "notify.push":
            raise SinkError("not_supported", f"{TOOL} applies notify.push intents only")
        settings = parse(request.params)
        self._check_approved(request, settings)
        token = self._token()
        message = payload(
            request.intent, settings, dedupe_key=request.dedupe_key, actions=request.actions
        )
        details: dict[str, Any] = {"delivery": "sent", "priority": settings.priority}
        if is_obsidian(request.intent.get("link")) and "url" not in message["data"]:
            details["click"] = "dropped"
        if "actions" in message["data"]:
            details["actions"] = len(message["data"]["actions"])  # the count only: URLs are secrets
        await self.caller.call(settings, message, token)
        return Applied(settings.target, details=details)


def build(
    notifier: HomeAssistantNotifier | None = None,
    reader: Reader | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Plugin:
    """The plugin, applying pushes through `notifier` and reading HA with `reader` (defaults
    when omitted)."""
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Home Assistant notifications and household reminders.",
    )
    plugin.add(notifier or HomeAssistantNotifier())
    plugin.source(REMINDERS_TOOL)(build_source(reader, environ=env))
    return plugin


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_homeassistant --manifest`)."""
    return build().manifest(
        command=COMMAND,
        horae=">=0,<0.2",  # the core is 0.0.x until v0.1; nothing checks it yet
        license="Apache-2.0",
        homepage="https://github.com/horae-dev/plugin-homeassistant",
    )
