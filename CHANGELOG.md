# Changelog

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
