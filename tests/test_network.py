# SPDX-License-Identifier: Apache-2.0
"""Network and auth posture: only `notify.*`, https unless opted in, approved hosts, no
redirects, timeouts, and the token never leaks."""

import json
import logging
from typing import Any

import anyio
import httpx2
import pytest
from horae_sdk import ApplyRequest, Network, Paths, SinkError

from fakes import HOST, TOKEN, URL, Recorder, notifier
from horae_homeassistant.http import HTTP_LOGGERS, NOTIFY_PATH, Caller
from horae_homeassistant.settings import parse

PARAMS: dict[str, Any] = {"url": URL, "service": "mobile_app_pixel"}


def request(
    params: dict[str, Any] | None = None,
    *,
    hosts: tuple[str, ...] | None = (HOST,),
    local: tuple[str, ...] = (),
    **extra: Any,
) -> ApplyRequest:
    return ApplyRequest(
        Paths(),
        {"type": "notify.push", "title": "T", "body": "hello"},
        tool="homeassistant.push",
        params=PARAMS if params is None else params,
        dedupe_key="a" * 64,
        network=Network(hosts, local),
        **extra,
    )


def apply(recorder: Recorder, req: ApplyRequest, **kw: Any) -> Any:
    return anyio.run(notifier(recorder, **kw).apply, req)


def failure(recorder: Recorder, req: ApplyRequest, **kw: Any) -> SinkError:
    with pytest.raises(SinkError) as raised:
        apply(recorder, req, **kw)
    return raised.value


def test_posts_to_the_notify_service_with_a_bearer_token() -> None:
    recorder = Recorder()
    applied = apply(recorder, request())
    [sent] = recorder.requests
    assert (sent.method, str(sent.url)) == ("POST", f"{URL}/api/services/notify/mobile_app_pixel")
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["content-type"] == "application/json"
    assert applied.target == f"{URL}/notify.mobile_app_pixel"
    assert applied.details == {"delivery": "sent", "priority": "normal"}


@pytest.mark.parametrize("service", ["phone", "mobile_app_a1", "persistent_notification", "x_1_y"])
def test_every_request_stays_under_notify(service: str) -> None:
    recorder = Recorder()
    apply(recorder, request({**PARAMS, "service": service}))
    [sent] = recorder.requests
    assert sent.url.path == f"{NOTIFY_PATH}{service}"
    assert sent.url.query == b""


@pytest.mark.parametrize(
    "service", ["light/turn_on", "../script/turn_on", "notify/../light", "a.b", "x?y", "x%2fy"]
)
def test_no_other_ha_service_is_reachable(service: str) -> None:
    recorder = Recorder()
    assert failure(recorder, request({**PARAMS, "service": service})).kind == "config_invalid"
    assert recorder.requests == []


def test_the_caller_itself_refuses_a_url_outside_notify() -> None:
    settings = parse(PARAMS)
    object.__setattr__(settings, "service", "../../light/turn_on")  # bypass parse on purpose
    recorder = Recorder()
    with pytest.raises(SinkError) as raised:
        anyio.run(Caller(transport=recorder).call, settings, {"message": "x"}, TOKEN)
    assert raised.value.kind == "internal"
    assert recorder.requests == []


def test_http_needs_a_local_grant_or_the_opt_in_of_an_older_core() -> None:
    recorder = Recorder()
    params = {**PARAMS, "url": "http://homeassistant.local:8123"}
    # an older core (no list): the operator's opt-in is the approval
    assert failure(recorder, request(params, hosts=None)).kind == "config_invalid"
    assert recorder.requests == []
    apply(recorder, request({**params, "allow_http": True}, hosts=None))
    assert str(recorder.requests[0].url).startswith(
        "http://homeassistant.local:8123/api/services/notify/"
    )


@pytest.mark.parametrize(
    "url", ["http://homeassistant.local:8123", "http://192.168.1.10:8123", "http://[::1]:8123"]
)
def test_a_network_local_grant_allows_http_without_the_opt_in(url: str) -> None:
    recorder = Recorder()
    entry = url.removeprefix("http://")
    apply(recorder, request({**PARAMS, "url": url}, hosts=(), local=(entry,)))
    assert str(recorder.requests[0].url).startswith(f"{url}/api/services/notify/")


def test_a_local_grant_also_allows_https_on_the_same_endpoint() -> None:
    recorder = Recorder()
    params = {**PARAMS, "url": "https://192.168.1.10:8123"}
    apply(recorder, request(params, hosts=(), local=("192.168.1.10:8123",)))
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://homeassistant.local:8124", ("homeassistant.local:8123",)),  # another port
        ("http://homeassistant.local", ("homeassistant.local:8123",)),  # default port 80
        ("http://192.168.1.11:8123", ("192.168.1.10:8123",)),  # another address
        ("http://ha.lan:8123", ()),  # no grant
    ],
)
def test_a_local_url_without_an_exact_grant_is_refused(url: str, local: tuple[str, ...]) -> None:
    recorder = Recorder()
    req = request({**PARAMS, "url": url, "allow_http": True}, hosts=(), local=local)
    assert failure(recorder, req).kind == "config_invalid"
    assert recorder.requests == []


def test_http_on_a_public_host_is_refused_even_when_approved_for_https() -> None:
    recorder = Recorder()
    params = {**PARAMS, "url": f"http://{HOST}", "allow_http": True}
    error = failure(recorder, request(params, local=("192.168.1.10:8123",)))
    assert error.kind == "config_invalid"
    assert "https is required" in error.message
    assert recorder.requests == []


def test_a_local_grant_does_not_approve_a_public_name() -> None:
    recorder = Recorder()
    params = {**PARAMS, "url": "http://homeassistant.local.evil.example.org:8123"}
    req = request({**params, "allow_http": True}, hosts=(), local=("homeassistant.local:8123",))
    assert failure(recorder, req).kind == "config_invalid"
    assert recorder.requests == []


def test_unapproved_host_refuses_before_anything_is_sent() -> None:
    recorder = Recorder()
    assert failure(recorder, request(hosts=("other.example.org",))).kind == "config_invalid"
    assert (
        failure(recorder, request(hosts=())).kind == "config_invalid"
    )  # an empty list: fail closed
    assert recorder.requests == []


def test_unknown_approval_list_falls_back_to_the_configuration() -> None:
    recorder = Recorder()
    apply(recorder, request(hosts=None))
    assert len(recorder.requests) == 1


def test_approved_host_covers_any_port() -> None:
    recorder = Recorder()
    apply(recorder, request({**PARAMS, "url": f"{URL}:8123"}))
    assert ":8123/" in str(recorder.requests[0].url)


def test_redirects_are_not_followed() -> None:
    recorder = Recorder(
        lambda _: httpx2.Response(302, headers={"Location": "https://evil.example.org/x"})
    )
    error = failure(recorder, request())
    assert error.kind == "redirect_refused"
    assert len(recorder.requests) == 1
    assert "evil" not in str(error)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "auth_failed"),
        (403, "auth_failed"),
        (404, "service_not_found"),
        (400, "publish_refused"),
        (422, "publish_refused"),
        (429, "rate_limited"),
        (500, "server_unavailable"),
        (503, "server_unavailable"),
        (301, "redirect_refused"),
    ],
)
def test_statuses_map_to_fixed_reasons_without_the_body(status: int, kind: str) -> None:
    leak = "LEAKED-BODY-TEXT"
    recorder = Recorder(lambda _: httpx2.Response(status, json={"message": leak}))
    error = failure(recorder, request())
    assert error.kind == kind
    assert leak not in str(error)
    assert TOKEN not in str(error)
    assert len(recorder.requests) == 1  # never retried


def test_connection_errors_and_timeouts_are_fixed_texts() -> None:
    def refuse(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"boom {TOKEN}")

    error = failure(Recorder(refuse), request())
    assert error.kind == "network_error"
    assert TOKEN not in str(error)
    assert error.__cause__ is None
    assert not error.__suppress_context__

    def slow(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    assert failure(Recorder(slow), request()).kind == "server_timeout"


def test_the_deadline_bounds_a_hanging_server() -> None:
    class Hang(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            await anyio.sleep(30)
            raise AssertionError("unreachable")

    notif = notifier(Recorder())
    notif.caller = Caller(transport=lambda _: Hang(), deadline_s=0.05)
    with pytest.raises(SinkError) as raised:
        anyio.run(notif.apply, request())
    assert raised.value.kind == "server_timeout"


def test_a_huge_answer_is_bounded_and_ignored() -> None:
    recorder = Recorder(lambda _: httpx2.Response(200, content=b"x" * 1_000_000))
    assert apply(recorder, request()).details["delivery"] == "sent"


@pytest.mark.parametrize(
    ("environ", "kind"),
    [({}, "secret_missing"), ({"HA_TOKEN": ""}, "secret_missing")]
    + [({"HA_TOKEN": bad}, "secret_invalid") for bad in ("a b", "a\nb", "x" * 5000, "é")],
)
def test_token_problems_are_fixed_errors(environ: dict[str, str], kind: str) -> None:
    recorder = Recorder()
    error = failure(recorder, request(), environ=environ)
    assert error.kind == kind
    assert recorder.requests == []
    for value in environ.values():
        assert not value or value not in str(error)


def test_the_token_is_not_in_the_result_logs_or_payload(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder()
    with caplog.at_level(logging.DEBUG):
        applied = apply(recorder, request())
    out = capsys.readouterr()
    assert TOKEN not in json.dumps(applied.details) + applied.target
    assert TOKEN not in caplog.text
    assert TOKEN not in out.out + out.err
    assert TOKEN not in recorder.requests[0].content.decode()
    assert all(logging.getLogger(name).level == logging.WARNING for name in HTTP_LOGGERS)


def test_button_urls_are_sent_but_never_reported() -> None:
    recorder = Recorder()
    buttons = (
        {
            "action": "http",
            "label": "Done",
            "url": "https://h.example.org/act/SECRET9",
            "method": "POST",
        },
    )
    applied = apply(recorder, request(actions=buttons))
    assert applied.details["actions"] == 1
    assert "SECRET9" in recorder.requests[0].content.decode()
    assert "SECRET9" not in json.dumps(applied.details) + applied.target


def test_non_push_intents_are_refused() -> None:
    req = ApplyRequest(Paths(), {"type": "task.create"}, tool="homeassistant.push", params=PARAMS)
    assert failure(Recorder(), req).kind == "not_supported"


def test_obsidian_link_without_obsidian_prefix_is_reported_dropped() -> None:
    req = ApplyRequest(
        Paths(),
        {"type": "notify.push", "body": "x", "link": "obsidian://open/n"},
        params={**PARAMS, "link_prefix": "https://notes.example.org/d/"},
    )
    assert apply(Recorder(), req).details["click"] == "dropped"
