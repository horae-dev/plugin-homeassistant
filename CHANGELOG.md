# Changelog

## 0.2.0

- New source `homeassistant.reminders`: reads household state from Home Assistant and proposes
  `task.create` or `notify.push` reminders from configured rules (an entity id or a capped glob,
  conditions on the state or an attribute: equals, not equals, in, below, above, present).
  Examples: low batteries, a to-do list's count, a filter-change counter, a finished appliance
  cycle, a calendar's next event.
- Read only towards HA: only `GET /api/states` and `GET /api/states/<entity_id>`, enforced by the
  HTTP layer (`ReadOnlyTransport`) and tested; no service is ever called.
- Each reminder has a stable key (`homeassistant:<rule>:<entity>`, plus the day with
  `repeat: daily`) used as the task fingerprint or the push `key`, so the core dedupes it.
- Names and attributes from HA are untrusted: cleaned, capped and escaped (SDK helpers).
- A failed read gives a fixed reason: per-rule in `degraded`, or a tool error for token, network,
  rate-limit, server and redirect failures. HA's answer, the URL and the token are never echoed.
- Same `HA_TOKEN` secret, `url` validation, approved-host check, no redirects and timeouts as the
  notifier. `plugin.yaml` adds the source and the `task.create` intent.
- Tested against a recorded HA API fixture (`tests/fixtures/ha_states.json`).

## 0.1.0

- First release: `homeassistant.push`, a `notify.push` notifier that calls
  `POST /api/services/notify/<service>` on your Home Assistant, authenticated with a long-lived
  access token from the `HA_TOKEN` secret.
- Maps `title`, `body`, `priority` and the intent's `link` (`data.url` and `data.clickAction`) to
  the companion apps' payload; a keyed push's `dedupe_key` becomes HA's `tag`, so a re-send
  replaces the notification instead of duplicating it.
- The buttons the core mints (`ApplyRequest.actions`) become companion-app `URI` actions that open
  the confirmation page (`actions: view`, the default; `actions: off` sends none).
- Only `notify.*` is reachable: `service` must match `[a-z0-9_]+` and the request URL is checked
  again before it is sent.
- https by default; `http://` only with `allow_http: true`. The host must be approved in
  `horae.lock` (`ApplyRequest.host_allowed`); no redirects, timeouts, no retry; the token and HA's
  answer are never logged or echoed.
- Tested against a recorded HA API fixture (`tests/fixtures/ha_api.json`), no real HA instance.
- Left out: quiet hours and once-a-day reminder state, event-firing companion actions (they need
  HA-side automations), channels, images, TTS.
