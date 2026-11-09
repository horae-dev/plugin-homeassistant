# SPDX-License-Identifier: Apache-2.0
"""The entry's parameters: strict, and the service name cannot leave `notify.*`."""

from typing import Any

import pytest
from horae_sdk import SinkError

from horae_homeassistant.settings import approval, parse

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


def test_http_parses_and_the_approval_decides() -> None:
    settings = parse({**OK, "url": "http://homeassistant.local:8123"})
    assert (settings.scheme, settings.base) == ("http", "http://homeassistant.local:8123")
    assert (settings.local, settings.allow_http) == (True, False)
    bad(url="http://homeassistant.local", allow_http="yes")


@pytest.mark.parametrize(
    ("url", "host", "base"),
    [
        ("http://192.168.1.10:8123", "192.168.1.10", "http://192.168.1.10:8123"),
        ("https://10.0.0.5", "10.0.0.5", "https://10.0.0.5"),
        ("http://127.0.0.1:8123", "127.0.0.1", "http://127.0.0.1:8123"),
        ("http://[::1]:8123", "::1", "http://[::1]:8123"),
        ("http://[fd12:3456::1]:8123", "fd12:3456::1", "http://[fd12:3456::1]:8123"),
        ("http://ha.lan:8123", "ha.lan", "http://ha.lan:8123"),
        ("http://ha.home.arpa", "ha.home.arpa", "http://ha.home.arpa"),
    ],
)
def test_lan_hosts_parse_as_local(url: str, host: str, base: str) -> None:
    settings = parse({**OK, "url": url})
    assert (settings.host, settings.base, settings.local) == (host, base, True)


def test_a_public_name_is_not_local() -> None:
    assert parse(OK).local is False
    assert parse({**OK, "url": "https://ha.lan.evil.example.org"}).local is False


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8",
        "http://192.168.001.010",
        "http://169.254.169.254",
        "http://169.254.170.2",
        "http://[fd00:ec2::254]",
        "http://[::ffff:192.168.1.1]",
        "http://[2001:db8::1]",
        "http://metadata.internal",
        "http://metadata.google.internal",
        "http://instance-data.ec2.internal",
    ],
)
def test_public_and_metadata_addresses_are_refused(url: str) -> None:
    bad(url=url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://ha.example.org",
        "https://ha.example.org/api",
        "https://user:pw@ha.example.org",
        "https://ha.example.org?x=1",
        "https://ha.example.org#x",
        "https://HA.example.org",
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


def server(url: str, **extra: Any) -> Any:
    from horae_homeassistant.settings import parse_server

    return parse_server(url, extra.get("allow_http", False))


def asks(answer: bool | None) -> tuple[Any, list[str]]:
    asked: list[str] = []

    def allowed(url: str) -> bool | None:
        asked.append(url)
        return answer

    return allowed, asked


@pytest.mark.parametrize("url", ["http://homeassistant.local:8123", "http://192.168.1.10:8123"])
def test_approval_true_allows_http_on_a_local_grant(url: str) -> None:
    allowed, asked = asks(True)
    assert approval(allowed, server(url)) is None
    assert asked == [url]  # the explicit port is asked


@pytest.mark.parametrize("url", ["http://homeassistant.local:8123", "https://ha.example.org"])
def test_approval_false_refuses(url: str) -> None:
    allowed, _ = asks(False)
    assert approval(allowed, server(url)) is not None


def test_approval_false_refuses_http_on_a_public_host() -> None:
    allowed, asked = asks(False)
    why = approval(allowed, server("http://ha.example.org:8123", allow_http=True))
    assert why is not None
    assert "https is required" in why
    assert asked == ["http://ha.example.org:8123"]


def test_approval_none_falls_back_to_the_configuration() -> None:
    allowed, _ = asks(None)
    assert approval(allowed, server("https://ha.example.org")) is None
    assert approval(allowed, server("https://192.168.1.10:8123")) is None
    assert "allow_http" in str(approval(allowed, server("http://homeassistant.local:8123")))
    assert approval(allowed, server("http://homeassistant.local:8123", allow_http=True)) is None


def test_a_public_https_host_is_still_asked_without_its_port() -> None:
    allowed, asked = asks(True)
    assert approval(allowed, server("https://ha.example.org:8123")) is None
    assert asked == ["https://ha.example.org"]
