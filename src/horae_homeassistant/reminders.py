# SPDX-License-Identifier: Apache-2.0
"""`homeassistant.reminders`: household state read from Home Assistant, proposed as reminders.

The source is **read-only towards HA**: it can only `GET /api/states` and
`GET /api/states/<entity_id>` (`horae_homeassistant.http.Reader`); no service is ever called.
Home Assistant's REST API has no GET that lists a to-do list's items (that is the `todo.get_items`
service, which this plugin never calls), so a `todo.*` entity is read as any other: its state is
the number of items still to do. Calendar entities expose their next event as attributes
(`message`, `start_time`), so "bins tonight" is a rule on a calendar's `state` and `attributes`.

A call is configured by its parameters (trusted): the server, and `rules`:

```yaml
- collect:
    - homeassistant.reminders:
        url: https://ha.example.org
        rules:
          - id: low-battery
            entity: sensor.*_battery          # an entity id, or a glob (see below)
            when:                             # every condition must hold
              - {below: 20}                   # the state, as a number
            intent: task.create               # task.create (default) | notify.push
            title: "Replace the battery of {name} ({state}{unit})"
            due_in_days: 2                    # task.create: days after the run's day (0 default)
            repeat: once                      # once (default) | daily: see the key below
```

**Rules.** At most `MAX_RULES`. `id` (`[a-z0-9-]{1,32}`, unique) names the rule in keys and in the
result. `entity` is an entity id (`binary_sensor.washer_done`, read with
`GET /api/states/<id>`) or a glob over entity ids (`*` and `?`, one `/api/states` read shared by
all globs of the run), which must keep a literal domain (`sensor.*_battery`; never `*.x`) and
matches at most `max_matches` entities (default 20, at most `MAX_MATCHES`; the first by id, and
`truncated` says more matched).

**Conditions** (`when`, at most `MAX_CONDITIONS`, all must hold; required): each is an object with
exactly one operator, on the entity's `state` or, with `attribute: <name>`, on that attribute:
`equals: <text|number|bool>`, `not_equals`, `in: [<text>...]`, `below: <number>`,
`above: <number>`, `present: <bool>`. A number is read from a JSON number or a plain decimal text
(`18`, `-3.5`); anything else (`unavailable`, `unknown`, `1e5`, `nan`) never satisfies `below` or
`above`.

**Reminder.** `title` (and, for `notify.push`, `body`; default the title) is a template over
`{name}` (the friendly name or the entity id), `{state}`, `{unit}` (the unit of measurement or
nothing), `{entity_id}` and `{attr.<name>}`. Anything HA returns is **untrusted**: it is cleaned
of controls and format characters, cut to `MAX_VALUE` characters, and a `task.create` title is
escaped by the SDK so it cannot set a date, a tag or a link (threat model T02).

**Key** (the core's dedupe): `homeassistant:<rule id>:<entity id>`, plus `:<run day>` when the
rule has `repeat: daily` (a new reminder each day while the condition holds). It is the task's
fingerprint, or the push's `key` (sent once per local day).

**Failures.** A rule whose read fails for a reason about that entity or answer (`entity_not_found`,
`response_too_large`, `response_invalid`) is reported in `degraded` (`rule`, `reason`) and the
other rules still run. Any other failure (token refused, timeout, network, rate limit, 5xx,
redirect) stops the call with a tool error carrying a fixed reason. Neither the token, the URL
nor HA's answer is ever in a result, an error or a log.
"""

import fnmatch
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from horae_sdk import Call, Output, PluginError, SinkError, intents, tasks

from horae_homeassistant.http import (
    ENTITY_ID,
    REASONS,
    STATES_PATH,
    Reader,
    ReadError,
    ReadSession,
)
from horae_homeassistant.settings import TOKEN_SECRET, Server, parse_server

NAME = "homeassistant"
TOOL = f"{NAME}.reminders"
NAMESPACE = "homeassistant"

MAX_RULES = 20
MAX_MATCHES = 50
DEFAULT_MATCHES = 20
MAX_CONDITIONS = 5
MAX_IN = 20
MAX_ITEMS = 200
MAX_VALUE = 80
MAX_TEMPLATE = 200
MAX_DUE_DAYS = 365
_TOTAL_DEADLINE_S = 30.0

_RULE_KEYS = {"id", "entity", "when", "intent", "title", "body", "due_in_days", "repeat"}
_RULE_ID = re.compile(r"[a-z0-9-]{1,32}")
_EXACT = re.compile(ENTITY_ID)
_GLOB = re.compile(r"[a-z0-9_]{1,64}\.[a-z0-9_*?]{1,128}")
_ATTRIBUTE = re.compile(r"[a-z0-9_]{1,64}")
_NUMBER = re.compile(r"-?[0-9]{1,15}(\.[0-9]{1,10})?")
_PLACEHOLDER = re.compile(r"\{(name|state|unit|entity_id|attr\.[a-z0-9_]{1,64})\}")
_OPERATORS = ("equals", "not_equals", "in", "below", "above", "present")
_LOCAL_FAILURES = frozenset({"entity_not_found", "response_too_large", "response_invalid"})
_TZ = re.compile(r"[A-Za-z0-9_+/-]{1,64}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

Clock = Callable[[], datetime]


def _bad(where: str, why: str) -> PluginError:
    return PluginError(f"{TOOL} {where}: {why}")


@dataclass(frozen=True)
class Condition:
    attribute: str | None
    operator: str
    value: Any


@dataclass(frozen=True)
class Rule:
    id: str
    entity: str
    glob: bool
    when: tuple[Condition, ...]
    intent: str
    title: str
    body: str | None
    due_in_days: int | None
    repeat: str
    max_matches: int = DEFAULT_MATCHES


def _condition(where: str, raw: object) -> Condition:
    if not isinstance(raw, dict):
        raise _bad(where, "a condition must be an object")
    operators = [key for key in raw if key in _OPERATORS]
    others = sorted(str(key)[:40] for key in raw if key not in (*_OPERATORS, "attribute"))
    if len(operators) != 1 or others:
        raise _bad(
            where, f"a condition has attribute (optional) and one of {', '.join(_OPERATORS)}"
        )
    attribute = raw.get("attribute")
    if attribute is not None and not (
        isinstance(attribute, str) and _ATTRIBUTE.fullmatch(attribute)
    ):
        raise _bad(where, "attribute must be 1 to 64 of a-z 0-9 _")
    operator = operators[0]
    value = raw[operator]
    if operator in ("below", "above"):
        if type(value) not in (int, float) or not -1e12 < value < 1e12:
            raise _bad(where, f"{operator} must be a number")
    elif operator == "present":
        if not isinstance(value, bool):
            raise _bad(where, "present must be true or false")
        if attribute is None:
            raise _bad(where, "present needs an attribute")
    elif operator == "in":
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= MAX_IN
            or not all(isinstance(item, str) and len(item) <= 255 for item in value)
        ):
            raise _bad(where, f"in must be 1 to {MAX_IN} texts")
        value = tuple(value)
    elif not (
        isinstance(value, bool | int | float) or (isinstance(value, str) and len(value) <= 255)
    ):
        raise _bad(where, f"{operator} must be a text, a number or true/false")
    return Condition(attribute, operator, value)


def _template(where: str, name: str, value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_TEMPLATE:
        raise _bad(where, f"{name} must be a text of 1 to {MAX_TEMPLATE} characters")
    if "\n" in value or "\r" in value:
        raise _bad(where, f"{name} must be one line")
    return value


def parse_rules(raw: object, max_matches: int) -> list[Rule]:
    """The `rules` parameter as `Rule`s, or `PluginError` naming the rule and the key (never
    echoing a value)."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_RULES:
        raise _bad("rules", f"must be a list of 1 to {MAX_RULES} rules")
    rules: list[Rule] = []
    for position, item in enumerate(raw, 1):
        where = f"rule {position}"
        if not isinstance(item, dict):
            raise _bad(where, "must be an object")
        unknown = sorted(str(key)[:40] for key in set(item) - _RULE_KEYS)
        if unknown:
            raise _bad(where, f"unknown key {unknown[0]!r}")
        rule_id = item.get("id")
        if not isinstance(rule_id, str) or not _RULE_ID.fullmatch(rule_id):
            raise _bad(where, "id is required, 1 to 32 of a-z 0-9 -")
        if any(rule.id == rule_id for rule in rules):
            raise _bad(where, "id is used twice")
        where = f"rule {rule_id}"
        entity = item.get("entity")
        if not isinstance(entity, str) or not (_EXACT.fullmatch(entity) or _GLOB.fullmatch(entity)):
            raise _bad(where, "entity must be an entity id or a glob with a literal domain")
        glob = _EXACT.fullmatch(entity) is None
        conditions = item.get("when")
        if not isinstance(conditions, list) or not 1 <= len(conditions) <= MAX_CONDITIONS:
            raise _bad(where, f"when is required: 1 to {MAX_CONDITIONS} conditions")
        intent = item.get("intent", "task.create")
        if intent not in ("task.create", "notify.push"):
            raise _bad(where, "intent must be task.create or notify.push")
        repeat = item.get("repeat", "once")
        if repeat not in ("once", "daily"):
            raise _bad(where, "repeat must be once or daily")
        title = _template(where, "title", item.get("title"))
        body = item.get("body")
        if body is not None:
            if intent != "notify.push":
                raise _bad(where, "body is for notify.push only")
            body = _template(where, "body", body)
        due = item.get("due_in_days", 0)
        if due is not None and (type(due) is not int or not 0 <= due <= MAX_DUE_DAYS):
            raise _bad(where, f"due_in_days is 0 to {MAX_DUE_DAYS}, or null")
        if intent == "notify.push" and "due_in_days" in item:
            raise _bad(where, "due_in_days is for task.create only")
        rules.append(
            Rule(
                id=rule_id,
                entity=entity,
                glob=glob,
                when=tuple(_condition(where, condition) for condition in conditions),
                intent=intent,
                title=title,
                body=body,
                due_in_days=None if intent == "notify.push" else due,
                repeat=repeat,
                max_matches=max_matches,
            )
        )
    return rules


# --- reading values ------------------------------------------------------------------------


def number(value: object) -> float | None:
    """A JSON number or a plain decimal text as a float; anything else (bool, `nan`, `1e5`,
    `1_0`, a huge text) is `None`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if -1e12 < value < 1e12 else None
    if isinstance(value, str) and _NUMBER.fullmatch(value):
        return float(value)
    return None


def _field(state: Mapping[str, Any], attribute: str | None) -> tuple[bool, Any]:
    if attribute is None:
        return True, state["state"]
    attributes = state["attributes"]
    value = attributes.get(attribute)
    return value is not None, value


def _equal(actual: object, wanted: object) -> bool:
    if isinstance(wanted, bool) or isinstance(actual, bool):
        return isinstance(wanted, bool) and isinstance(actual, bool) and actual == wanted
    if isinstance(wanted, int | float):
        have = number(actual)
        return have is not None and have == float(wanted)
    return isinstance(actual, str) and actual == wanted


def holds(condition: Condition, state: Mapping[str, Any]) -> bool:
    present, value = _field(state, condition.attribute)
    wanted = condition.value
    match condition.operator:
        case "present":
            return bool(present == wanted)
        case _ if not present:
            return False
        case "equals":
            return _equal(value, wanted)
        case "not_equals":
            return not _equal(value, wanted)
        case "in":
            return isinstance(value, str) and value in wanted
        case "below":
            have = number(value)
            return have is not None and have < wanted
        case "above":
            have = number(value)
            return have is not None and have > wanted
    return False  # pragma: no cover (every operator is handled above)


def clean_state(raw: object, entity_id: str | None = None) -> dict[str, Any] | None:
    """One state object of HA's answer reduced to what is used (entity id, state, attributes),
    or `None` when it is not a well-formed one (or, given `entity_id`, not that entity)."""
    if not isinstance(raw, dict):
        return None
    found, state, attributes = raw.get("entity_id"), raw.get("state"), raw.get("attributes", {})
    if (
        not isinstance(found, str)
        or not _EXACT.fullmatch(found)
        or (entity_id is not None and found != entity_id)
        or not isinstance(state, str)
        or len(state) > 255
        or not isinstance(attributes, dict)
    ):
        return None
    return {"entity_id": found, "state": state, "attributes": attributes}


def safe_text(value: object, limit: int = MAX_VALUE) -> str:
    """Untrusted text as plain one-line text of at most `limit` characters: controls, format
    characters and line breaks removed, `{{`-style template starts neutralised, cut with `…`.
    Scalars only (anything else is empty)."""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, str | int | float):
        text = str(value)
    else:
        return ""
    plain = tasks.plain_text(tasks.escape_title(text[: limit * 4], max_length=limit * 4))
    plain = re.sub(r"\{(?=[{%#])", "{ ", plain)
    return plain if len(plain) <= limit else plain[: limit - 1] + "…"


def render(template: str, state: Mapping[str, Any]) -> str:
    """The template with its placeholders replaced by cleaned values."""
    attributes: Mapping[str, Any] = state["attributes"]

    def value(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "state":
            return safe_text(state["state"])
        if key == "entity_id":
            return str(state["entity_id"])
        if key == "name":
            return safe_text(attributes.get("friendly_name")) or str(state["entity_id"])
        if key == "unit":
            unit = safe_text(attributes.get("unit_of_measurement"), 16)
            return unit
        return safe_text(attributes.get(key[len("attr.") :]))

    return _PLACEHOLDER.sub(value, template)


# --- the source ----------------------------------------------------------------------------


def run_day(today: str | None, timezone: str | None, clock: Clock) -> date:
    """The run's day: `today` when given, else the clock's date in `timezone` (default UTC)."""
    zone: ZoneInfo | None = None
    if timezone is not None:
        if not _TZ.fullmatch(timezone):
            raise _bad("timezone", "must be an IANA zone name")
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise _bad("timezone", "is not a known zone") from None
    if today is not None:
        if not _DATE.fullmatch(today):
            raise _bad("today", "must be YYYY-MM-DD")
        try:
            return date.fromisoformat(today)
        except ValueError:
            raise _bad("today", "must be a real date") from None
    return clock().astimezone(zone or UTC).date()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _key(rule: Rule, entity_id: str, day: date) -> str:
    parts = [NAMESPACE, rule.id, entity_id]
    if rule.repeat == "daily":
        parts.append(day.isoformat())
    return intents.fingerprint(*parts)


def _intent(rule: Rule, state: Mapping[str, Any], day: date) -> dict[str, Any]:
    key = _key(rule, state["entity_id"], day)
    title = render(rule.title, state)
    if rule.intent == "task.create":
        due = None if rule.due_in_days is None else day + timedelta(days=rule.due_in_days)
        return intents.task_create(title or "(untitled)", key=key, due=due)
    body = render(rule.body, state) if rule.body is not None else title
    return intents.notify_push(body or "(no text)", title=title or None, key=key)


@dataclass
class Run:
    """What one call found."""

    matched: list[dict[str, Any]] = field(default_factory=list)
    intents: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False
    seen: int = 0


class _Reads:
    """The run's reads, each path once."""

    def __init__(self, session: ReadSession) -> None:
        self.session = session
        self.cache: dict[str, Any] = {}

    async def get(self, path: str) -> Any:
        if path not in self.cache:
            self.cache[path] = await self.session.get(path)
        return self.cache[path]


async def _states_for(rule: Rule, reads: _Reads) -> tuple[list[dict[str, Any]], bool]:
    """The cleaned states a rule applies to, and whether the glob matched more than allowed."""
    if not rule.glob:
        state = clean_state(await reads.get(f"{STATES_PATH}/{rule.entity}"), rule.entity)
        if state is None:
            raise ReadError("response_invalid")
        return [state], False
    everything = await reads.get(STATES_PATH)
    if not isinstance(everything, list):
        raise ReadError("response_invalid")
    found: dict[str, dict[str, Any]] = {}
    for raw in everything:
        state = clean_state(raw)
        if state is not None and fnmatch.fnmatchcase(state["entity_id"], rule.entity):
            found.setdefault(state["entity_id"], state)
    ordered = [found[entity] for entity in sorted(found)]
    return ordered[: rule.max_matches], len(ordered) > rule.max_matches


async def collect(
    reader: Reader,
    server: Server,
    token: str,
    rules: list[Rule],
    day: date,
    max_items: int,
) -> Run:
    run = Run()
    async with reader.open(server, token) as session:
        reads = _Reads(session)
        for rule in rules:
            try:
                states, more = await _states_for(rule, reads)
            except ReadError as error:
                if error.reason not in _LOCAL_FAILURES:
                    raise
                run.degraded.append({"rule": rule.id, "reason": error.reason})
                continue
            run.truncated = run.truncated or more
            for state in states:
                run.seen += 1
                if not all(holds(condition, state) for condition in rule.when):
                    continue
                if len(run.intents) >= max_items:
                    run.truncated = True
                    continue
                intent = _intent(rule, state, day)
                run.intents.append(intent)
                run.matched.append(
                    {
                        "rule": rule.id,
                        "entity_id": state["entity_id"],
                        "name": safe_text(state["attributes"].get("friendly_name")) or None,
                        "state": safe_text(state["state"]),
                        "intent": intent["type"],
                        "key": _key(rule, state["entity_id"], day),
                    }
                )
    return run


def build_source(
    reader: Reader | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    clock: Clock = _utc_now,
) -> Callable[..., Any]:
    """The tool function of `homeassistant.reminders`. `reader`, `environ` and `clock` are
    replaceable for tests."""
    the_reader = reader or Reader()

    async def reminders(
        call: Call,
        url: str,
        rules: list[dict[str, Any]],
        allow_http: bool = False,
        max_matches: int = DEFAULT_MATCHES,
        max_items: int = 50,
        today: str | None = None,
        timezone: str | None = None,
    ) -> Output:
        """Reminders from Home Assistant state (read only: GET /api/states). Each rule
        proposes a task.create or notify.push for every matching entity; see the README."""
        try:
            server = parse_server(url, allow_http, name=TOOL)
        except SinkError as error:
            raise PluginError(error.message) from None
        if type(max_matches) is not int or not 1 <= max_matches <= MAX_MATCHES:
            raise _bad("max_matches", f"is 1 to {MAX_MATCHES}")
        if type(max_items) is not int or not 1 <= max_items <= MAX_ITEMS:
            raise _bad("max_items", f"is 1 to {MAX_ITEMS}")
        parsed = parse_rules(rules, max_matches)
        day = run_day(today, timezone, clock)
        if call.host_allowed(f"https://{server.host}") is False:
            raise _bad("url", f"host {server.host} is not approved in horae.lock")
        env = os.environ if environ is None else environ
        token = env.get(TOKEN_SECRET)
        if not token:
            raise PluginError(f"{TOOL}: {TOKEN_SECRET} is not set")
        if len(token) > 4096 or not all(0x21 <= ord(char) < 0x7F for char in token):
            raise PluginError(f"{TOOL}: {TOKEN_SECRET} is not a valid access token")
        failure: str | None = None
        run = Run()
        try:
            with anyio.fail_after(_TOTAL_DEADLINE_S):
                run = await collect(the_reader, server, token, parsed, day, max_items)
        except ReadError as error:
            failure = error.reason
        except TimeoutError:
            failure = "server_timeout"
        if failure is not None:
            raise PluginError(f"{TOOL}: {REASONS[failure]} ({failure})")
        return Output(
            {
                "day": day.isoformat(),
                "rules": len(parsed),
                "entities_read": run.seen,
                "matched": run.matched,
                "truncated": run.truncated,
                "degraded": run.degraded,
            },
            intents=run.intents,
        )

    return reminders
