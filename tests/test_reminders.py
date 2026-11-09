# SPDX-License-Identifier: Apache-2.0
"""The reminders source: rule validation, conditions, text cleaning, keys, caps and failures."""

import copy
import json
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx2
import pytest
from horae_sdk import tasks
from horae_sdk.testing import call

from fakes import HOST, TOKEN, URL, Recorder
from horae_homeassistant import build
from horae_homeassistant.http import REASONS, Reader
from horae_homeassistant.reminders import (
    Condition,
    holds,
    number,
    parse_rules,
    run_day,
    safe_text,
)

RULE: dict[str, Any] = {
    "id": "low-battery",
    "entity": "sensor.door_battery",
    "when": [{"below": 20}],
    "title": "Battery {name}",
}


def state(entity: str, value: str, **attributes: Any) -> dict[str, Any]:
    return {"entity_id": entity, "state": value, "attributes": attributes}


def run(
    rules: list[dict[str, Any]],
    answers: dict[str, httpx2.Response] | None = None,
    *,
    environ: dict[str, str] | None = None,
    network: list[str] | None = None,
    **params: Any,
) -> tuple[Any, Recorder]:
    answers = answers or {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return answers.get(request.url.path, httpx2.Response(404, json={}))

    recorder = Recorder(handler)
    plugin = build(
        reader=Reader(transport=recorder),
        env={"HA_TOKEN": TOKEN} if environ is None else environ,
    )
    arguments = {"url": URL, "rules": rules, "today": "2026-11-09", **params}
    replayed = anyio.run(
        lambda: call(
            plugin,
            "homeassistant.reminders",
            arguments,
            network=[HOST] if network is None else network,
        )
    )
    return replayed, recorder


def one(entity: str, value: str, **attributes: Any) -> dict[str, httpx2.Response]:
    return {f"/api/states/{entity}": httpx2.Response(200, json=state(entity, value, **attributes))}


# --- configuration ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"id": "Bad Id"},
        {"id": ""},
        {"entity": "sensor"},
        {"entity": "*.battery"},
        {"entity": "sensor.a/b"},
        {"entity": "Sensor.A"},
        {"entity": "sensor.[ab]"},
        {"when": []},
        {"when": [{"below": 1}] * 6},
        {"when": [{}]},
        {"when": [{"below": 1, "above": 0}]},
        {"when": [{"below": "9"}]},
        {"when": [{"below": True}]},
        {"when": [{"below": 1, "x": 1}]},
        {"when": [{"in": []}]},
        {"when": [{"in": [1]}]},
        {"when": [{"present": True}]},
        {"when": [{"attribute": "A b", "below": 1}]},
        {"when": [{"equals": ["x"]}]},
        {"intent": "http.post"},
        {"intent": "notify.push", "due_in_days": 1},
        {"body": "x"},
        {"title": ""},
        {"title": "a\nb"},
        {"title": "x" * 201},
        {"due_in_days": -1},
        {"due_in_days": 366},
        {"due_in_days": True},
        {"repeat": "weekly"},
        {"surprise": 1},
    ],
)
def test_bad_rules_are_refused_without_echoing_values(change: dict[str, Any]) -> None:
    rule = {**RULE, **change}
    with pytest.raises(Exception) as raised:  # noqa: PT011 - PluginError, checked below
        parse_rules([rule], 20)
    assert type(raised.value).__name__ == "PluginError"
    for value in change.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in str(raised.value)


def test_rules_need_a_list_of_at_most_20_with_unique_ids() -> None:
    bads: list[Any] = [
        None,
        [],
        "x",
        {},
        [RULE] * 2,
        [{**RULE, "id": f"r{i}"} for i in range(21)],
        [1],
    ]
    for bad in bads:
        with pytest.raises(Exception):  # noqa: B017, PT011 - PluginError
            parse_rules(bad, 20)
    assert len(parse_rules([{**RULE, "id": f"r{i}"} for i in range(20)], 20)) == 20


def test_defaults() -> None:
    [rule] = parse_rules([RULE], 20)
    assert (rule.intent, rule.repeat, rule.due_in_days, rule.glob) == (
        "task.create",
        "once",
        0,
        False,
    )
    [glob] = parse_rules([{**RULE, "entity": "sensor.*_battery"}], 7)
    assert (glob.glob, glob.max_matches) == (True, 7)


@pytest.mark.parametrize(
    "params",
    [
        {"max_matches": 0},
        {"max_matches": 51},
        {"max_items": 0},
        {"max_items": 201},
        {"today": "2026-13-01"},
        {"today": "tomorrow"},
        {"timezone": "Mars/Base"},
        {"timezone": "../etc"},
        {"url": "http://ha.example.org"},
        {"url": "https://user@ha.example.org"},
    ],
)
def test_bad_call_parameters_are_a_tool_error(params: dict[str, Any]) -> None:
    replayed, recorder = run([RULE], one("sensor.door_battery", "3"), **params)
    assert replayed.error is not None
    assert recorder.requests == []


def test_run_day_uses_the_timezone() -> None:
    late = datetime(2026, 11, 9, 23, 30, tzinfo=UTC)
    assert run_day(None, "Europe/Paris", lambda: late).isoformat() == "2026-11-10"
    assert run_day(None, None, lambda: late).isoformat() == "2026-11-09"


# --- conditions ---------------------------------------------------------------------------


def holds_for(condition: dict[str, Any], value: str, **attributes: Any) -> bool:
    [rule] = parse_rules([{**RULE, "when": [condition]}], 20)
    condition_ = rule.when[0]
    assert isinstance(condition_, Condition)
    return holds(condition_, state("sensor.a", value, **attributes))


@pytest.mark.parametrize(
    ("condition", "value", "expected"),
    [
        ({"equals": "on"}, "on", True),
        ({"equals": "on"}, "ON", False),
        ({"not_equals": "on"}, "off", True),
        ({"not_equals": "on"}, "on", False),
        ({"in": ["on", "paused"]}, "paused", True),
        ({"in": ["on"]}, "off", False),
        ({"below": 20}, "12", True),
        ({"below": 20}, "20", False),
        ({"below": 20}, "7.5", True),
        ({"below": 20}, "-3", True),
        ({"below": 20}, "unavailable", False),
        ({"below": 20}, "unknown", False),
        ({"below": 20}, "1e1", False),
        ({"below": 20}, "nan", False),
        ({"below": 20}, "1_0", False),
        ({"below": 20}, "", False),
        ({"above": 0}, "3", True),
        ({"above": 0}, "0", False),
        ({"equals": 3}, "3.0", True),
        ({"equals": 3}, "three", False),
    ],
)
def test_state_conditions(condition: dict[str, Any], value: str, expected: bool) -> None:
    assert holds_for(condition, value) is expected


@pytest.mark.parametrize(
    ("condition", "attributes", "expected"),
    [
        ({"attribute": "level", "below": 20}, {"level": 12}, True),
        ({"attribute": "level", "below": 20}, {"level": "12"}, True),
        ({"attribute": "level", "below": 20}, {"level": True}, False),
        ({"attribute": "level", "below": 20}, {"level": [1]}, False),
        ({"attribute": "level", "below": 20}, {}, False),
        ({"attribute": "level", "present": True}, {"level": 0}, True),
        ({"attribute": "level", "present": True}, {"level": None}, False),
        ({"attribute": "level", "present": False}, {}, True),
        ({"attribute": "mode", "equals": "eco"}, {"mode": "eco"}, True),
        ({"attribute": "mode", "equals": "eco"}, {"mode": 1}, False),
        ({"attribute": "armed", "equals": True}, {"armed": True}, True),
        ({"attribute": "armed", "equals": True}, {"armed": "true"}, False),
        ({"attribute": "armed", "equals": 1}, {"armed": True}, False),
        ({"attribute": "mode", "in": ["a", "b"]}, {"mode": "b"}, True),
        ({"attribute": "mode", "not_equals": "a"}, {}, False),
    ],
)
def test_attribute_conditions(
    condition: dict[str, Any], attributes: dict[str, Any], expected: bool
) -> None:
    assert holds_for(condition, "x", **attributes) is expected


def test_numbers() -> None:
    assert number("12") == 12.0
    assert number(3) == 3.0
    assert number(True) is None
    assert number("9" * 16) is None
    assert number(10**13) is None
    assert number(None) is None


# --- untrusted text -----------------------------------------------------------------------


def test_safe_text_is_one_clean_capped_line() -> None:
    hostile = "A‮B​C\x00D\nE\tF {{ x }} {% y %} {# z #}" + "!" * 500
    text = safe_text(hostile)
    assert len(text) <= 80
    assert text.endswith("…")
    assert all(ch.isprintable() for ch in text)
    assert "{{" not in text
    assert "{%" not in text
    assert "{#" not in text
    assert safe_text({"a": 1}) == ""
    assert safe_text(None) == ""
    assert safe_text(True) == "true"
    assert safe_text(12) == "12"


def test_names_and_attributes_cannot_set_task_fields() -> None:
    name = "Door \U0001f4c5 2020-01-01 \U0001f501 every day ⏫ #tag [[x]] https://evil.example"
    answers = one("sensor.door_battery", "3", friendly_name=name)
    replayed, _ = run([RULE], answers)
    [task] = replayed.intents
    for signifier in ("\U0001f4c5", "\U0001f501", "⏫"):
        assert signifier not in task["title"]
    assert "[[" not in task["title"]
    assert "\\#tag" in task["title"]  # escaped, so not a tag
    assert "https\\:" in task["title"]  # escaped, so not a link
    assert task["due"] == "2026-11-09"  # the rule's, not the name's


def test_placeholders() -> None:
    attrs = {"friendly_name": "Door", "unit_of_measurement": "%", "mode": "eco", "n": 7}
    rule = {**RULE, "title": "{name}|{state}|{unit}|{entity_id}|{attr.mode}|{attr.n}|{attr.none}"}
    replayed, _ = run([rule], one("sensor.door_battery", "3", **attrs))
    [task] = replayed.intents
    assert tasks.plain_text(task["title"]) == "Door|3|%|sensor.door_battery|eco|7|"


def test_an_unknown_placeholder_stays_literal() -> None:
    replayed, _ = run([{**RULE, "title": "{secret} {name!r}"}], one("sensor.door_battery", "3"))
    assert replayed.error is None
    assert tasks.plain_text(replayed.intents[0]["title"]) == "{secret} {name!r}"


def test_the_name_falls_back_to_the_entity_id() -> None:
    replayed, _ = run([RULE], one("sensor.door_battery", "3"))
    assert tasks.plain_text(replayed.intents[0]["title"]) == "Battery sensor.door_battery"


# --- keys and intents ---------------------------------------------------------------------


def test_keys_are_stable_and_daily_rules_add_the_day() -> None:
    answers = one("sensor.door_battery", "3")
    once, _ = run([RULE], answers)
    again, _ = run([RULE], answers, today="2026-11-10")
    assert once.intents[0]["fingerprint"] == again.intents[0]["fingerprint"]
    assert once.intents[0]["fingerprint"] == "homeassistant:low-battery:sensor.door_battery"
    daily = {**RULE, "repeat": "daily"}
    first, _ = run([daily], answers)
    second, _ = run([daily], answers, today="2026-11-10")
    assert first.intents[0]["fingerprint"].endswith(":2026-11-09")
    assert second.intents[0]["fingerprint"].endswith(":2026-11-10")


def test_a_push_carries_the_key_and_no_due_date() -> None:
    rule = {**RULE, "intent": "notify.push", "body": "Low: {state}", "repeat": "daily"}
    replayed, _ = run([rule], one("sensor.door_battery", "3"))
    [push] = replayed.intents
    assert push["type"] == "notify.push"
    assert push["key"] == "homeassistant:low-battery:sensor.door_battery:2026-11-09"
    assert push["body"] == "Low: 3"
    assert "due" not in push


def test_a_rule_without_due_has_none() -> None:
    replayed, _ = run([{**RULE, "due_in_days": None}], one("sensor.door_battery", "3"))
    assert "due" not in replayed.intents[0]


def test_no_match_no_intent() -> None:
    replayed, _ = run([RULE], one("sensor.door_battery", "80"))
    assert replayed.intents == []
    assert replayed.data["matched"] == []


def test_a_glob_is_one_read_shared_by_rules() -> None:
    states = [
        state("sensor.a_battery", "5"),
        state("sensor.b_battery", "9"),
        state("light.x", "on"),
    ]
    answers = {"/api/states": httpx2.Response(200, json=states)}
    rules = [
        {**RULE, "id": "r1", "entity": "sensor.*_battery"},
        {**RULE, "id": "r2", "entity": "sensor.?_battery", "when": [{"below": 6}]},
    ]
    replayed, recorder = run(rules, answers)
    assert len(recorder.requests) == 1
    assert [i["fingerprint"] for i in replayed.intents] == [
        "homeassistant:r1:sensor.a_battery",
        "homeassistant:r1:sensor.b_battery",
        "homeassistant:r2:sensor.a_battery",
    ]


def test_max_items_caps_the_intents() -> None:
    states = [state(f"sensor.s{i:02d}_battery", "1") for i in range(10)]
    answers = {"/api/states": httpx2.Response(200, json=states)}
    replayed, _ = run(
        [{**RULE, "entity": "sensor.*_battery"}], answers, max_items=3, max_matches=50
    )
    assert len(replayed.intents) == 3
    assert replayed.data["truncated"] is True


def test_malformed_states_are_skipped() -> None:
    states: list[Any] = [
        "x",
        {"entity_id": 1, "state": "1"},
        {"entity_id": "sensor.Bad_battery", "state": "1", "attributes": {}},
        {"entity_id": "sensor.a_battery", "state": 3, "attributes": {}},
        {"entity_id": "sensor.b_battery", "state": "1", "attributes": []},
        {"entity_id": "sensor.ok_battery", "state": "1"},
    ]
    answers = {"/api/states": httpx2.Response(200, json=states)}
    replayed, _ = run([{**RULE, "entity": "sensor.*_battery"}], answers)
    assert [i["fingerprint"] for i in replayed.intents] == [
        "homeassistant:low-battery:sensor.ok_battery"
    ]


# --- failures ----------------------------------------------------------------------------


def test_the_host_must_be_approved() -> None:
    replayed, recorder = run([RULE], one("sensor.door_battery", "3"), network=["other.example.org"])
    assert replayed.error is not None
    assert "not approved" in replayed.error
    assert recorder.requests == []


def test_an_older_core_without_a_list_trusts_the_configuration() -> None:
    plugin = build(
        reader=Reader(
            transport=Recorder(
                lambda _: httpx2.Response(200, json=state("sensor.door_battery", "3"))
            )
        ),
        env={"HA_TOKEN": TOKEN},
    )
    replayed = anyio.run(
        lambda: call(
            plugin,
            "homeassistant.reminders",
            {"url": URL, "rules": [RULE], "today": "2026-11-09"},
            network=None,
        )
    )
    assert replayed.error is None
    assert len(replayed.intents) == 1


@pytest.mark.parametrize(
    "environ", [{}, {"HA_TOKEN": ""}, {"HA_TOKEN": "a b"}, {"HA_TOKEN": "x" * 5000}]
)
def test_the_token_must_be_set_and_plausible(environ: dict[str, str]) -> None:
    replayed, recorder = run([RULE], one("sensor.door_battery", "3"), environ=environ)
    assert replayed.error is not None
    assert "HA_TOKEN" in replayed.error
    assert recorder.requests == []
    assert "a b" not in replayed.error


BODY = {"message": "secret-body-text " + TOKEN}


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx2.Response(401, json=BODY), "auth_failed"),
        (httpx2.Response(403, json=BODY), "auth_failed"),
        (httpx2.Response(429, json=BODY), "rate_limited"),
        (httpx2.Response(500, json=BODY), "server_unavailable"),
        (httpx2.Response(503, json=BODY), "server_unavailable"),
        (httpx2.Response(302, headers={"Location": "https://evil.example/x"}), "redirect_refused"),
        (httpx2.Response(400, json=BODY), "request_refused"),
    ],
)
def test_global_failures_stop_the_call_with_a_fixed_reason(
    response: httpx2.Response, reason: str
) -> None:
    replayed, recorder = run(
        [RULE, {**RULE, "id": "second"}], {"/api/states/sensor.door_battery": response}
    )
    assert replayed.error is not None
    assert replayed.error == f"homeassistant.reminders: {REASONS[reason]} ({reason})"
    assert len(recorder.requests) == 1  # a redirect is never followed, nothing else is tried
    for secret in (TOKEN, "secret-body-text", "evil.example", HOST):
        assert secret not in replayed.error


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx2.Response(404, json=BODY), "entity_not_found"),
        (httpx2.Response(200, content=b"{not json" + b"secret-body-text"), "response_invalid"),
        (httpx2.Response(200, json=[1]), "response_invalid"),
        (httpx2.Response(200, json=state("sensor.other", "3")), "response_invalid"),
        (httpx2.Response(200, content=b" " * (64 * 1024 + 1)), "response_too_large"),
    ],
)
def test_local_failures_degrade_one_rule_and_the_others_run(
    response: httpx2.Response, reason: str
) -> None:
    answers = {
        "/api/states/sensor.bad": response,
        **one("sensor.door_battery", "3"),
    }
    replayed, _ = run([{**RULE, "id": "bad", "entity": "sensor.bad"}, RULE], answers)
    assert replayed.error is None
    assert replayed.data["degraded"] == [{"rule": "bad", "reason": reason}]
    assert [i["fingerprint"] for i in replayed.intents] == [
        "homeassistant:low-battery:sensor.door_battery"
    ]
    assert "secret-body-text" not in json.dumps(replayed.data)


def test_a_glob_answer_that_is_not_a_list_degrades() -> None:
    answers = {"/api/states": httpx2.Response(200, json={"a": 1})}
    replayed, _ = run([{**RULE, "entity": "sensor.*_battery"}], answers)
    assert replayed.data["degraded"] == [{"rule": "low-battery", "reason": "response_invalid"}]


def test_an_oversize_states_answer_degrades() -> None:
    big = httpx2.Response(200, content=b"[" + b" " * (4 * 1024 * 1024))
    replayed, _ = run([{**RULE, "entity": "sensor.*_battery"}], {"/api/states": big})
    assert replayed.data["degraded"] == [{"rule": "low-battery", "reason": "response_too_large"}]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx2.ConnectError("boom token=" + TOKEN), "network_error"),
        (httpx2.ReadTimeout("slow"), "server_timeout"),
    ],
)
def test_network_failures_are_fixed_reasons(error: Exception, reason: str) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise error

    plugin = build(
        reader=Reader(transport=lambda _: httpx2.MockTransport(fail)), env={"HA_TOKEN": TOKEN}
    )
    replayed = anyio.run(
        lambda: call(
            plugin,
            "homeassistant.reminders",
            {"url": URL, "rules": [RULE], "today": "2026-11-09"},
            network=[HOST],
        )
    )
    assert replayed.error == f"homeassistant.reminders: {REASONS[reason]} ({reason})"
    assert TOKEN not in replayed.error


def test_the_result_never_holds_the_token_or_the_url() -> None:
    replayed, _ = run([RULE], one("sensor.door_battery", "3"))
    text = json.dumps([replayed.data, replayed.intents])
    assert TOKEN not in text
    assert HOST not in text


def test_the_rules_are_not_mutated() -> None:
    rules = [copy.deepcopy(RULE)]
    run(rules, one("sensor.door_battery", "3"))
    assert rules == [RULE]
