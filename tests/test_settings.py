# SPDX-License-Identifier: Apache-2.0
"""The entry's parameters: strict, and the service name cannot leave `notify.*`."""

from typing import Any

import pytest
from horae_sdk import SinkError

from horae_homeassistant.settings import parse

OK: dict[str, Any] = {"url": "https://ha.example.org", "service": "mobile_app_pixel"}


def bad(**changes: Any) -> SinkError:
    with pytest.raises(SinkError) as raised:
        parse({**OK, **changes})
    assert raised.value.kind == "config_invalid"
    return raised.value


def test_minimal_entry_defaults() -> None:
    settings = parse(OK)
    assert settings.endpoint == "https://ha.example.org/api/services/notify/mobile_app_pixel"
    assert (settings.scheme, settings.priority, settings.actions) == ("https", "normal", "view")
    assert settings.target == "https://ha.example.org/notify.mobile_app_pixel"


@pytest.mark.parametrize(
    "service",
    [
        "light/turn_on",
        "../light/turn_on",
        "notify.mobile_app",
        "mobile-app",
        "Mobile",
        "a b",
        "svc\n",
        "svc?x=1",
        "svc#x",
        "%2e%2e",
        "",
        "a" * 65,
        "é",
        None,
        3,
    ],
)
def test_service_name_cannot_leave_notify(service: object) -> None:
    bad(service=service)


def test_service_is_required() -> None:
    with pytest.raises(SinkError, match="service"):
        parse({"url": "https://ha.example.org"})


def test_url_is_required() -> None:
    with pytest.raises(SinkError, match="url"):
        parse({"service": "x"})


def test_http_needs_the_explicit_opt_in() -> None:
    assert "allow_http" in str(bad(url="http://homeassistant.local:8123"))
    settings = parse({**OK, "url": "http://homeassistant.local:8123", "allow_http": True})
    assert (settings.scheme, settings.base) == ("http", "http://homeassistant.local:8123")
    bad(url="http://homeassistant.local", allow_http="yes")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://ha.example.org",
        "https://ha.example.org/api",
        "https://user:pw@ha.example.org",
        "https://ha.example.org?x=1",
        "https://ha.example.org#x",
        "https://HA.example.org",
        "https://192.168.1.5",
        "https://[::1]",
        "https://localhost",
        "https://ha.localhost",
        "https://ha.example.org:99999",
        "https://ha.example.org:",
        "https://ha.example.org\n",
        "https://ha example.org",
        "https://hä.example.org",
        "ha.example.org",
        "",
        None,
        42,
    ],
)
def test_url_is_a_plain_base_url(url: object) -> None:
    bad(url=url, allow_http=True)


def test_url_error_never_echoes_the_value() -> None:
    assert "secrettoken" not in str(bad(url="https://user:secrettoken@ha.example.org"))


def test_unknown_parameter_is_refused() -> None:
    bad(priorty="high")
    bad(**{"token": "x"})


@pytest.mark.parametrize("changes", [{"priority": "urgent"}, {"priority": 3}, {"actions": "http"}])
def test_invalid_values(changes: dict[str, Any]) -> None:
    bad(**changes)


@pytest.mark.parametrize(
    "prefix",
    [
        "http://notes.example.org/d/",
        "https://notes.example.org/d",
        "https://notes.example.org/",
        "https://notes.example.org/../d/",
        "https://u@notes.example.org/d/",
        "https://notes.example.org/d/?q=1",
        "https://notes.example.org/%2e%2e/",
        5,
    ],
)
def test_bad_link_prefix(prefix: object) -> None:
    bad(link_prefix=prefix)


def test_good_link_prefixes() -> None:
    assert parse({**OK, "link_prefix": "https://notes.example.org/daily/"}).link_prefix
    assert parse({**OK, "link_prefix": "obsidian://open/vault/"}).link_prefix
