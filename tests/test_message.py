# SPDX-License-Identifier: Apache-2.0
"""The HA notify payload: untrusted text, links, priorities, buttons."""

from typing import Any

import pytest
from horae_sdk import SinkError

from horae_homeassistant.message import MAX_BODY, MAX_TITLE, body_text, payload, tag, title_text
from horae_homeassistant.settings import Settings, parse

PREFIX = "https://notes.example.org/daily/"


def settings(**params: Any) -> Settings:
    return parse({"url": "https://ha.example.org", "service": "phone", **params})


def intent(**fields: Any) -> dict[str, Any]:
    return {"type": "notify.push", "body": "hello", **fields}


def button(label: str = "Done", url: str = "https://h.example.org/act/x1") -> dict[str, str]:
    return {"action": "http", "label": label, "url": url, "method": "POST"}


def test_title_body_and_tag() -> None:
    message = payload(intent(title="T"), settings(), dedupe_key="k")
    assert message["title"] == "T"
    assert message["message"] == "hello"
    assert message["data"]["tag"] == tag("k")
    assert len(message["data"]["tag"]) == 32
    assert "tag" not in payload(intent(), settings())["data"]


def test_text_is_cleaned() -> None:
    assert body_text("a‮b\x00c\r\nd\te") == "abc\nd\te"
    assert body_text("   ") == "(no text)"
    assert title_text("a\nb\tc​d") == "a b cd"
    assert title_text("​ ") is None
    assert title_text(None) is None


def test_templates_are_neutralised() -> None:
    text = body_text("{{ states('x') }} {% if 1 %} {# c #}")
    assert "{{" not in text
    assert "{%" not in text
    assert "{#" not in text


def test_long_text_is_cut_on_a_boundary() -> None:
    assert len(body_text("x" * 10_000)) == MAX_BODY
    assert body_text("x" * 10_000).endswith("…")
    assert len(title_text("y" * 1000) or "") == MAX_TITLE
    cut = title_text("y" * (MAX_TITLE - 2) + "é" + "z" * 10) or ""
    assert not cut.endswith("́")


@pytest.mark.parametrize(
    ("priority", "level", "android"),
    [
        ("min", "passive", {"priority": "normal"}),
        ("low", "passive", {"priority": "normal"}),
        ("normal", "active", {}),
        ("high", "time-sensitive", {"priority": "high", "ttl": 0}),
        ("critical", "critical", {"priority": "high", "ttl": 0}),
    ],
)
def test_priority_mapping(priority: str, level: str, android: dict[str, Any]) -> None:
    data = payload(intent(), settings(priority=priority))["data"]
    assert data["push"]["interruption-level"] == level
    assert {k: data[k] for k in ("priority", "ttl") if k in data} == android
    assert ("sound" in data["push"]) is (priority == "critical")


def test_link_under_prefix_is_the_click_target() -> None:
    link = f"{PREFIX}2026-10-29"
    data = payload(intent(link=link), settings(link_prefix=PREFIX))["data"]
    assert data["url"] == data["clickAction"] == link


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example.org/daily/x",
        "https://notes.example.org/other/x",
        f"{PREFIX}../x",
        f"{PREFIX}%2e%2e/x",
        "https://notes.example.org@evil.example.org/daily/x",
        f"{PREFIX}a b",
        "http://notes.example.org/daily/x",
        "javascript:alert(1)",
        "intent://x",
        7,
    ],
)
def test_link_outside_prefix_refuses(link: object) -> None:
    with pytest.raises(SinkError) as raised:
        payload(intent(link=link), settings(link_prefix=PREFIX))
    assert raised.value.kind == "intent_invalid"


def test_link_without_prefix_refuses() -> None:
    with pytest.raises(SinkError):
        payload(intent(link=f"{PREFIX}x"), settings())


def test_obsidian_link_needs_an_obsidian_prefix() -> None:
    link = "obsidian://open/vault/note"
    assert "url" not in payload(intent(link=link), settings(link_prefix=PREFIX))["data"]
    data = payload(intent(link=link), settings(link_prefix="obsidian://open/vault/"))["data"]
    assert data["url"] == link


def test_buttons_become_uri_actions() -> None:
    data = payload(intent(), settings(), actions=[button("Done"), button("Snooze")])["data"]
    assert data["actions"] == [
        {"action": "URI", "title": "Done", "uri": "https://h.example.org/act/x1"},
        {"action": "URI", "title": "Snooze", "uri": "https://h.example.org/act/x1"},
    ]


def test_buttons_are_capped_and_can_be_turned_off() -> None:
    many = [button(f"b{i}") for i in range(5)]
    assert len(payload(intent(), settings(), actions=many)["data"]["actions"]) == 3
    assert "actions" not in payload(intent(), settings(actions="off"), actions=many)["data"]


@pytest.mark.parametrize(
    "bad",
    [
        "nope",
        {**button(), "action": "view"},
        {**button(), "method": "GET"},
        button(label=""),
        button(label="x" * 41),
        button(label="a‮b"),
        button(url="http://h.example.org/act/x1"),
        button(url="https://u:p@h.example.org/act/x1"),
        button(url="https://h.example.org/a b"),
        button(url="https://h.example.org/a\\b"),
        button(url="https://"),
        {"action": "http", "method": "POST", "label": "x", "url": 3},
    ],
)
def test_bad_button_refuses_without_leaking_its_url(bad: Any) -> None:
    secret = "https://h.example.org/act/SECRETCRED"  # noqa: S105 - a synthetic URL
    with pytest.raises(SinkError) as raised:
        payload(intent(), settings(), actions=[button(url=secret), bad])
    assert raised.value.kind == "intent_invalid"
    assert "SECRETCRED" not in str(raised.value)
    assert "SECRETCRED" not in str(bad)


def test_intent_needs_a_text_body() -> None:
    for broken in ({"type": "notify.push"}, intent(body=3), intent(title=3)):
        with pytest.raises(SinkError) as raised:
            payload(broken, settings())
        assert raised.value.kind == "intent_invalid"
