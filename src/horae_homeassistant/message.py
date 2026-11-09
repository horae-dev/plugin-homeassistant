# SPDX-License-Identifier: Apache-2.0
"""The HA `notify` service payload, built from an intent (untrusted text) and the settings.

```json
{"title": "...", "message": "...",
 "data": {"url": "...", "clickAction": "...", "tag": "...", "actions": [...], ...}}
```

**Untrusted text** (`title`, `body`) is data. C0/C1 controls and Unicode format characters
(bidi overrides, zero-width spaces, the BOM) are removed, except a tab and line break in the
body; the title is one line; both are cut (`MAX_TITLE`, `MAX_BODY`) with `…`. HA templates are
only rendered by HA for template-typed fields: the notify service does not render `message`
as a template when called through the REST API, but `{{`/`{%` are neutralised anyway (a space
is put between the braces) so no template engine on the way can evaluate proposer text.

**Link.** `data.url` (iOS and Android companion apps) and `data.clickAction` (Android) are the
intent's `link`, which the core routed only under the entry's `link_prefix`; it is checked
again here (same scheme and host, path under the prefix's, clean rest, no user info, spaces or
controls) and the push is refused (`intent_invalid`) otherwise. An `obsidian://` link is sent
only if `link_prefix` is an `obsidian://` prefix; else it is dropped.

**Priority** (entry parameter) maps to the companion apps' options: Android `priority` /
`ttl` (high: immediate delivery), iOS `push.interruption-level` (`passive`, `active`,
`time-sensitive`, `critical` with the critical sound).

**Buttons** (`ApplyRequest.actions`, minted by the core, each `url` a single-use credential)
become companion-app actionable notifications of the `URI` kind (`data.actions`): tapping opens
the confirmation page. They are checked again (`http`, `POST`, `https://` URL, plain label), at
most `MAX_ACTIONS`; a bad one refuses the push with a fixed text, and **the URL is never put in
an error, a log or the result**. Not mapped: the app's other action kinds, which fire HA events
that need HA-side automations.

**`tag`** is derived from the core's `dedupe_key`: HA replaces a notification with the same
tag, so a re-sent push replaces rather than duplicates.
"""

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from horae_sdk import SinkError

from horae_homeassistant.settings import Settings, clean_path

MAX_BODY = 4096
MAX_TITLE = 250
MAX_LINK = 2048
MAX_LABEL = 40
MAX_ACTIONS = 3
TAG_LENGTH = 32
EMPTY_BODY = "(no text)"
_ELLIPSIS = "…"
_KEEP_FORMAT = {"‌", "‍"}  # ZWNJ, ZWJ: emoji sequences and some scripts need them
_LINE_BREAKS = {"\n", "\u2028", "\u2029", "\x85"}
_TEMPLATE = re.compile(r"\{(?=[{%#])")


def _clean(text: str, *, multiline: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for char in text:
        if char in _LINE_BREAKS or char == "\t":
            kept.append(("\n" if char != "\t" else "\t") if multiline else " ")
            continue
        category = unicodedata.category(char)
        if category == "Cc" or (category == "Cf" and char not in _KEEP_FORMAT):
            continue
        kept.append("�" if category == "Cs" else char)
    return _TEMPLATE.sub("{ ", "".join(kept))


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    while cut and unicodedata.combining(text[len(cut)]):  # not before a combining mark
        cut = cut[:-1]
    return cut.rstrip() + _ELLIPSIS


def body_text(body: str) -> str:
    cleaned = _clean(body, multiline=True).strip()
    return _cut(cleaned, MAX_BODY) if cleaned else EMPTY_BODY


def title_text(title: str | None) -> str | None:
    if title is None:
        return None
    cleaned = " ".join(_clean(title, multiline=False).split())
    return _cut(cleaned, MAX_TITLE) if cleaned else None


def is_obsidian(link: object) -> bool:
    return isinstance(link, str) and link.startswith("obsidian://")


def _under(link: str, prefix: str) -> bool:
    try:
        got, want = urlsplit(link), urlsplit(prefix)
    except ValueError:
        return False
    if got.scheme != want.scheme or got.netloc != want.netloc:
        return False
    return got.path.startswith(want.path) and clean_path(got.path[len(want.path) :], prefix=False)


def click_url(link: object, settings: Settings) -> str | None:
    """The intent's link checked again against `link_prefix`; `None` for no link, or for an
    `obsidian://` one when `link_prefix` is not an `obsidian://` prefix."""
    if link is None:
        return None
    prefix = settings.link_prefix
    scheme = "obsidian" if is_obsidian(link) else "https"
    if scheme == "obsidian" and (prefix is None or not prefix.startswith("obsidian://")):
        return None
    if not isinstance(link, str) or len(link) > MAX_LINK:
        raise SinkError("intent_invalid", "the notification link is not a string URL")
    if any(ch <= " " or ch == "\x7f" for ch in link) or not link.startswith(f"{scheme}://"):
        raise SinkError("intent_invalid", "the notification link is not an https:// URL")
    try:
        parts = urlsplit(link)
    except ValueError:
        raise SinkError("intent_invalid", "the notification link is not a valid URL") from None
    if not parts.netloc or "@" in parts.netloc or "\\" in link:
        raise SinkError("intent_invalid", "the notification link has no host or has user info")
    if prefix is None or not prefix.startswith(f"{scheme}://") or not _under(link, prefix):
        raise SinkError("intent_invalid", "the notification link is not under link_prefix")
    return link


def _button_error(why: str) -> SinkError:
    # Fixed texts: the button's URL is a credential and must not leak into an error.
    return SinkError("intent_invalid", f"an action button {why}")


def _button(item: object) -> dict[str, str]:
    if not isinstance(item, dict):
        raise _button_error("is not an object")
    label, url = item.get("label"), item.get("url")
    if item.get("action") != "http" or item.get("method") != "POST":
        raise _button_error("is not an http POST action")
    if (
        not isinstance(label, str)
        or not 1 <= len(label) <= MAX_LABEL
        or any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") for ch in label)
    ):
        raise _button_error("has an invalid label")
    if (
        not isinstance(url, str)
        or len(url) > MAX_LINK
        or not url.startswith("https://")
        or any(ch <= " " or ch == "\x7f" or ch == "\\" for ch in url)
    ):
        raise _button_error("has an invalid URL")
    try:
        parts = urlsplit(url)
        valid = bool(parts.hostname) and "@" not in parts.netloc
    except ValueError:
        valid = False
    if not valid:
        raise _button_error("has an invalid URL")
    return {"action": "URI", "title": label, "uri": url}


def action_buttons(requested: Sequence[object], settings: Settings) -> list[dict[str, str]]:
    if settings.actions == "off":
        return []
    return [_button(item) for item in list(requested)[:MAX_ACTIONS]]


def tag(dedupe_key: str | None) -> str | None:
    """HA's notification `tag`: the first 32 hex characters of the key's SHA-256."""
    if dedupe_key is None:
        return None
    return hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:TAG_LENGTH]


def _priority_data(priority: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if priority in ("high", "critical"):
        data.update({"priority": "high", "ttl": 0})  # Android: deliver now
    elif priority in ("min", "low"):
        data["priority"] = "normal"
    level = {
        "min": "passive",
        "low": "passive",
        "normal": "active",
        "high": "time-sensitive",
        "critical": "critical",
    }[priority]
    push: dict[str, Any] = {"interruption-level": level}
    if priority == "critical":
        push["sound"] = {"name": "default", "critical": 1, "volume": 1.0}
    data["push"] = push
    return data


def payload(
    intent: dict[str, Any],
    settings: Settings,
    *,
    dedupe_key: str | None = None,
    actions: Sequence[object] = (),
) -> dict[str, Any]:
    """The JSON body for `POST /api/services/notify/<service>`."""
    body, title = intent.get("body"), intent.get("title")
    if not isinstance(body, str) or not (title is None or isinstance(title, str)):
        raise SinkError("intent_invalid", "a notify.push needs a text body")
    data: dict[str, Any] = _priority_data(settings.priority)
    message: dict[str, Any] = {"message": body_text(body), "data": data}
    shown = title_text(title)
    if shown is not None:
        message["title"] = shown
    click = click_url(intent.get("link"), settings)
    if click is not None:
        data["url"] = click
        data["clickAction"] = click
    buttons = action_buttons(actions, settings)
    if buttons:
        data["actions"] = buttons
    group = tag(dedupe_key)
    if group is not None:
        data["tag"] = group
    return message
