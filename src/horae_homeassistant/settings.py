# SPDX-License-Identifier: Apache-2.0
"""The `homeassistant.push` entry's trusted parameters, checked on every apply.

A pipeline's `notify` entry configures the notifier:

```yaml
- notify:
    - homeassistant.push:
        url: https://ha.example.org        # required: the HA base URL, no path
        service: mobile_app_pixel          # required: notify.<service>, [a-z0-9_]+
        allow_http: false                  # only for a core older than horae-sdk 0.6 (below)
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

**URL.** `https://<host>[:port]` with no path, query, fragment or user info. The host is a
lowercase DNS name (`homeassistant.local` and single-label names work, as the lock's
`permissions.network` writes it), or a LAN address: a canonical IPv4 literal in 127.0.0.0/8,
10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 or 169.254.0.0/16, or a bracketed IPv6 literal (`::1`,
fc00::/7, fe80::/10). Cloud metadata addresses and names, a public IP literal and `localhost`
are refused. A LAN address or a `.local`, `.lan`, `.internal` or `.home.arpa` name is meant to be
approved as `host:port` in `permissions.network_local` (horae-sdk 0.6, decision 66), which also
allows plain `http://` on exactly that host and port (`approval`). A core older than 0.6 sends
no list: `http://` then needs the operator's `allow_http: true` (the token travels in clear on
that network). `http://` on a public host is never approved by a core that sends a list.
"""

import ipaddress
import re
from collections.abc import Callable
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
_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa")
_METADATA_LABELS = frozenset({"metadata", "instance-data"})
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(a)
    for a in ("169.254.169.254", "169.254.170.2", "169.254.170.23", "fd00:ec2::254", "fd00:ec2::23")
)
_LAN_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _lan_address(host: str) -> bool | None:
    """`True` for an allowed LAN IP literal in canonical form, `False` for any other IP
    literal (public, metadata, non-canonical), `None` when `host` is not an IP literal."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if str(address) != host or address in _METADATA_ADDRESSES:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    return any(
        address in network for network in _LAN_NETWORKS if network.version == address.version
    )


def _local_name(host: str) -> bool:
    return any(host.endswith(suffix) and len(host) > len(suffix) for suffix in _LOCAL_SUFFIXES)


def _invalid(name: str, why: str, entry: str = "homeassistant.push") -> SinkError:
    return SinkError("config_invalid", f"{entry} parameter {name}: {why}")


@dataclass(frozen=True)
class Server:
    """The validated HA base URL, shared by the notifier and the reminders source."""

    base: str
    """`scheme://host[:port]`, no trailing slash."""
    scheme: Literal["https", "http"]
    host: str
    allow_http: bool
    """The operator's opt-in, honoured only when the core sends no approved list."""
    local: bool
    """A LAN address or a `.local`/`.lan`/`.internal`/`.home.arpa` name: asked of the lock with
    its explicit port, as a `network_local` endpoint."""


@dataclass(frozen=True)
class Settings(Server):
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


def parse_server(url: object, allow_http: bool, *, name: str = "homeassistant.push") -> Server:
    """The `url` / `allow_http` pair of an entry or a source call as a `Server`; a bad value is
    `SinkError("config_invalid")` (the reminders source maps it to its own error)."""
    if not isinstance(allow_http, bool):
        raise _invalid("allow_http", "must be true or false", name)
    base, scheme, host, local = _base(url, name)
    return Server(base, scheme, host, allow_http, local)


def approval(allowed: Callable[[str], bool | None], server: Server) -> str | None:
    """Why `server` may not be contacted, or `None` when it may (`allowed` is the core's
    `host_allowed`). A LAN address or name, and every `http://` URL, is asked with its explicit
    port, so only an exact `network_local` grant approves it; a public `https` host is asked as
    before (any port). `False` refuses. `None` (a core older than horae-sdk 0.6) leaves the
    configuration in charge, `http://` still needing `allow_http: true`."""
    url = server.base if server.local or server.scheme == "http" else f"https://{server.host}"
    verdict = allowed(url)
    if verdict is False:
        if server.scheme == "http" and not server.local:
            return f"host {server.host} is not approved for http (https is required here)"
        grant = "permissions.network_local" if server.local else "permissions.network"
        return f"host {server.host} is not approved in horae.lock ({grant})"
    if verdict is None and server.scheme == "http" and not server.allow_http:
        return "is http://; set allow_http: true to accept it (an older core, LAN installs)"
    return None


def _base(
    value: object, name: str = "homeassistant.push"
) -> tuple[str, Literal["https", "http"], str, bool]:
    if not isinstance(value, str) or len(value) > 300:
        raise _invalid("url", "must be an https:// URL", name)
    if not value.isascii() or any(ch <= " " or ch == "\x7f" for ch in value):
        raise _invalid("url", "must be an https:// URL without spaces or controls", name)
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise _invalid("url", "is not a valid URL", name) from None
    if parts.scheme not in ("http", "https"):
        raise _invalid("url", "must be https:// (or http:// on a network_local host)", name)
    host = parts.hostname or ""
    if "@" in parts.netloc or parts.query or parts.fragment or "#" in value or "?" in value:
        raise _invalid("url", "has user info, a query or a fragment", name)
    if parts.path not in ("", "/"):
        raise _invalid("url", "has a path (give the base URL only)", name)
    lan = _lan_address(host)
    if lan is False:
        raise _invalid("url", "host is an IP address that is not a canonical LAN address", name)
    local = lan is True
    if lan is None:
        if parts.netloc != parts.netloc.lower() or not _HOST.fullmatch(host):
            raise _invalid("url", "host must be a lowercase DNS name or a LAN address", name)
        if _LOCALHOST.search(host) or len(host) > 253:
            raise _invalid("url", "host must not be localhost", name)
        local = _local_name(host)
        if local and host.split(".")[0] in _METADATA_LABELS:
            raise _invalid("url", "host is a cloud metadata name", name)
    if port is not None and not 1 <= port <= 65535:
        raise _invalid("url", "has an invalid port", name)
    shown = f"[{host}]" if ":" in host else host
    netloc = shown if port is None else f"{shown}:{port}"
    if parts.netloc != netloc:
        raise _invalid("url", "has an invalid port", name)
    scheme: Literal["https", "http"] = "https" if parts.scheme == "https" else "http"
    return f"{scheme}://{netloc}", scheme, host, local


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
    base, scheme, host, local = _base(params["url"])
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
        allow_http=allow_http,
        local=local,
        service=service,
        priority=priority,
        link_prefix=prefix,
        actions="view" if actions == "view" else "off",
    )
