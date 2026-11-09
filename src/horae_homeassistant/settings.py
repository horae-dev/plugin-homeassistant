# SPDX-License-Identifier: Apache-2.0
"""The `homeassistant.push` entry's trusted parameters, checked on every apply.

A pipeline's `notify` entry configures the notifier:

```yaml
- notify:
    - homeassistant.push:
        url: https://ha.example.org        # required: the HA base URL, no path
        service: mobile_app_pixel          # required: notify.<service>, [a-z0-9_]+
        allow_http: false                  # true: accept an http:// url (LAN installs)
        priority: normal                   # min | low | normal (default) | high | critical
        link_prefix: https://notes.example.org/daily/   # read by the core
        actions: view                      # view (default): minted buttons open their page | off
```

These parameters are **trusted** (the pipeline author's), but a typo must not silently turn a
feature off, so any unknown key or invalid value is `SinkError("config_invalid")`, naming the
parameter and never echoing its value.

**Service.** `service` is the part after `notify.` and must match `[a-z0-9_]+` in full: no dot,
slash, dash, space, case or line break, so the request path can only ever be
`/api/services/notify/<service>`; no other HA domain (`light`, `script`, `homeassistant`...)
can be reached through this plugin.

**URL.** `https://<host>[:port]` with no path, query, fragment or user info; the host is a
lowercase DNS name (`homeassistant.local` and single-label names work), as the lock's
`permissions.network` writes it (no IP literal, no `localhost`). `http://` is refused unless the
operator sets `allow_http: true`: the token then travels in clear on that network.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from horae_sdk import SinkError

TOKEN_SECRET = "HA_TOKEN"  # noqa: S105 - the name of the secret, not its value
"""The declared secret holding the HA long-lived access token (`permissions.secrets`)."""

PRIORITIES = ("min", "low", "normal", "high", "critical")
LINK_SCHEMES = frozenset({"https", "obsidian"})
MAX_PREFIX = 2048

_KEYS = {"url", "service", "allow_http", "priority", "link_prefix", "actions"}
_SERVICE = re.compile(r"[a-z0-9_]{1,64}")
_HOST = re.compile(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
_LOCALHOST = re.compile(r"(^|\.)localhost$")
_UNSAFE_ESCAPES = ("%2e", "%2f", "%5c", "%25")


def _invalid(name: str, why: str) -> SinkError:
    return SinkError("config_invalid", f"homeassistant.push parameter {name}: {why}")


@dataclass(frozen=True)
class Settings:
    base: str
    """`scheme://host[:port]`, no trailing slash."""
    scheme: Literal["https", "http"]
    host: str
    service: str
    priority: str
    link_prefix: str | None
    actions: Literal["view", "off"] = "view"

    @property
    def endpoint(self) -> str:
        """The only URL this plugin ever posts to."""
        return f"{self.base}/api/services/notify/{self.service}"

    @property
    def target(self) -> str:
        return f"{self.base}/notify.{self.service}"


def _base(value: object, allow_http: bool) -> tuple[str, Literal["https", "http"], str]:
    if not isinstance(value, str) or len(value) > 300:
        raise _invalid("url", "must be an https:// URL")
    if not value.isascii() or any(ch <= " " or ch == "\x7f" for ch in value):
        raise _invalid("url", "must be an https:// URL without spaces or controls")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise _invalid("url", "is not a valid URL") from None
    if parts.scheme == "http":
        if not allow_http:
            raise _invalid("url", "is http://; set allow_http: true to accept it (LAN installs)")
    elif parts.scheme != "https":
        raise _invalid("url", "must be https:// (or http:// with allow_http: true)")
    host = parts.hostname or ""
    if "@" in parts.netloc or parts.query or parts.fragment or "#" in value or "?" in value:
        raise _invalid("url", "has user info, a query or a fragment")
    if parts.path not in ("", "/"):
        raise _invalid("url", "has a path (give the base URL only)")
    if parts.netloc != parts.netloc.lower() or not _HOST.fullmatch(host):
        raise _invalid("url", "host must be a lowercase DNS name (no IP literal)")
    if _LOCALHOST.search(host) or len(host) > 253:
        raise _invalid("url", "host must not be localhost")
    if port is not None and not 1 <= port <= 65535:
        raise _invalid("url", "has an invalid port")
    netloc = host if port is None else f"{host}:{port}"
    if parts.netloc != netloc:
        raise _invalid("url", "has an invalid port")
    scheme: Literal["https", "http"] = "https" if parts.scheme == "https" else "http"
    return f"{scheme}://{netloc}", scheme, host


def clean_path(path: str, *, prefix: bool) -> bool:
    """The core's rule (`horae.engine.executor._clean_path`). A `link_prefix` path
    (`prefix=True`: absolute, at least one segment, ending in `/`), or the rest of a link's
    path after it: no empty (but a trailing), `.` or `..` segment, no backslash, no escaped
    dot, slash, backslash or percent sign."""
    lowered = path.lower()
    if "\\" in path or any(escape in lowered for escape in _UNSAFE_ESCAPES):
        return False
    segments = path.split("/")
    if prefix:
        if len(segments) < 3 or segments[0] != "" or segments[-1] != "":
            return False
        segments = segments[1:-1]
    elif segments[-1] == "":
        segments = segments[:-1]
    return all(segment not in ("", ".", "..") for segment in segments)


def check_link_prefix(prefix: str) -> bool:
    """Whether `prefix` is a safe `link_prefix`, by the core's rule: an `https://` or
    `obsidian://` URL with a host, no user info, query or fragment, and a clean absolute path
    ending in `/`."""
    if len(prefix) > MAX_PREFIX or any(ch <= " " or ch == "\x7f" for ch in prefix):
        return False
    try:
        parts = urlsplit(prefix)
    except ValueError:
        return False
    return (
        parts.scheme in LINK_SCHEMES
        and bool(parts.netloc)
        and "@" not in parts.netloc
        and "?" not in prefix
        and "#" not in prefix
        and prefix.endswith("/")
        and clean_path(parts.path, prefix=True)
    )


def parse(params: dict[str, Any]) -> Settings:
    """The entry's parameters as `Settings`, or `SinkError("config_invalid")`."""
    unknown = sorted(str(key) for key in set(params) - _KEYS)
    if unknown:
        raise _invalid(f"{unknown[0]!r:.40}", "unknown parameter")
    allow_http = params.get("allow_http", False)
    if not isinstance(allow_http, bool):
        raise _invalid("allow_http", "must be true or false")
    if "url" not in params:
        raise _invalid("url", "required (the Home Assistant base URL)")
    base, scheme, host = _base(params["url"], allow_http)
    service = params.get("service")
    if not isinstance(service, str) or not _SERVICE.fullmatch(service):
        raise _invalid("service", "required, 1 to 64 of a-z 0-9 _ (the part after notify.)")
    priority = params.get("priority", "normal")
    if not isinstance(priority, str) or priority not in PRIORITIES:
        raise _invalid("priority", f"must be one of {', '.join(PRIORITIES)}")
    prefix = params.get("link_prefix")
    if prefix is not None and not (isinstance(prefix, str) and check_link_prefix(prefix)):
        raise _invalid(
            "link_prefix", "must be an https:// or obsidian:// URL with a clean path ending in /"
        )
    actions = params.get("actions", "view")
    if actions not in ("view", "off"):
        raise _invalid("actions", "must be view or off")
    return Settings(
        base=base,
        scheme=scheme,
        host=host,
        service=service,
        priority=priority,
        link_prefix=prefix,
        actions="view" if actions == "view" else "off",
    )
