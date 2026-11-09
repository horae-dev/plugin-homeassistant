<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-homeassistant

Horae plugin: Home Assistant notifications. Version 0.1.0.

## What it does

`homeassistant.push` is a **notifier**: a pipeline's `notify` step names it, the core routes each `notify.push` intent to it, and after the run the core applies the intent through the plugin's `homeassistant.apply` sink operation. The plugin calls your Home Assistant's `notify` service (`POST /api/services/notify/<service>`) with the intent's title, body and link, and the access token. It proposes nothing and writes no file. It can call **only `notify.*` services**.

```yaml
# pipelines/remind.yaml (excerpt)
- notify:
    - homeassistant.push:
        url: https://ha.example.org          # required: the HA base URL (no path)
        service: mobile_app_pixel            # required: notify.<service>, [a-z0-9_]+
        allow_http: false                    # true: accept an http:// url (LAN installs)
        priority: normal                     # min | low | normal (default) | high | critical
        link_prefix: https://notes.example.org/daily/   # links routed here (core)
        actions: view                        # view (default): buttons open their page | off
```

Every key except `url` and `service` is optional; an unknown key or an invalid value fails the apply with `config_invalid` (the message names the parameter, never its value).

| Parameter | Meaning |
|---|---|
| `url` | `https://<host>[:port]`, no path, query, fragment or user info. The host is a lowercase DNS name (`homeassistant.local` works; IP literals and `localhost` are refused, as the lock cannot approve them). |
| `service` | The part after `notify.`: `mobile_app_pixel` for `notify.mobile_app_pixel`. Must match `[a-z0-9_]+` (1 to 64), so the plugin cannot reach any other HA service. |
| `allow_http` | `false` (default): only `https://`. `true`: an `http://` URL is accepted too. The token then travels in clear on that network: use it for a LAN instance only. |
| `priority` | `min`, `low`, `normal`, `high`, `critical`. See below. |
| `link_prefix` | The core routes a `link` only under it; the plugin checks the link against it again. `https://` or `obsidian://`. |
| `actions` | `view` (default) sends the core's minted buttons; `off` sends none. |

### Home Assistant setup

1. **Create a long-lived access token.** In HA, open your profile (bottom of the sidebar), tab **Security**, section **Long-lived access tokens**, **Create token**. Name it `horae` and copy the token now: HA shows it once. Prefer a dedicated, non-admin user for it.
2. **Find the notify service name.** In **Developer tools > Actions** (older versions: Services) type `notify.`: the list shows every service, such as `notify.mobile_app_pixel_8` (one per phone with the companion app), `notify.notify` or `notify.persistent_notification`. The `service` parameter is what follows `notify.`. You can also run a test there with a message to check it arrives.
3. **Give the token to Horae** as the `HA_TOKEN` secret of the instance (an environment variable the host injects into the plugin).
4. **Approve the host** in `horae.lock` (below).

```yaml
# horae.lock (excerpt)
plugins:
  homeassistant:
    permissions:
      network: [ha.example.org]
      secrets: [HA_TOKEN]
      intents: [notify.push]
```

`plugin.yaml` declares no host: yours is your own, so it is approved **per instance** in `horae.lock`. The core sends the approved `permissions.network` with every apply and the plugin refuses (`config_invalid`, naming the host, before reading the token or contacting anything) a `url` whose host is not in it; an `http://` URL is matched by its host name too, and any port is accepted. An older core sends no list, and the plugin then trusts the configured `url`.

### What reaches the phone

- **Text.** `title` and `body` are shown as plain text: control and invisible formatting characters are removed, the title is one line, lengths are capped, and `{{`, `{%` and `{#` are neutralised so no template can run on proposer text.
- **Link.** `link` becomes `data.url` (iOS) and `data.clickAction` (Android): tapping the notification opens it. It must be under `link_prefix`, or the push is refused (`intent_invalid`). An `obsidian://` link is sent only when `link_prefix` is an `obsidian://` prefix (otherwise `details.click` is `dropped`).
- **Priority.** Android: `high` and `critical` set `priority: high` and `ttl: 0` (deliver at once); `min` and `low` set `priority: normal`. iOS: `push.interruption-level` is `passive` (`min`, `low`), `active` (`normal`), `time-sensitive` (`high`) or `critical` (`critical`, with the critical sound; the companion app needs the critical-alerts permission).
- **Buttons.** The buttons the core mints for an intent's `actions` (Done, Snooze) become actionable-notification `URI` actions (at most 3): tapping one opens its confirmation page in the phone's browser. Each URL is a single-use credential, so it is sent to HA only, and never logged, put in an error or returned in the result. Event-firing actions are not used: they need automations on the HA side.
- **Replacing, not duplicating.** The core's `dedupe_key` becomes HA's `tag`: a push sent again replaces the earlier notification.

### Failures

All are `SinkError`s with fixed texts: the token, HA's answer and the button URLs are never in them.

| Kind | When |
|---|---|
| `config_invalid` | A bad or unknown parameter, `http://` without `allow_http`, or a host the lock does not approve |
| `secret_missing` / `secret_invalid` | `HA_TOKEN` unset, or not a plausible token |
| `intent_invalid` | A link outside `link_prefix`, or a bad button |
| `auth_failed` | 401 or 403: the token is wrong, revoked or its user cannot call the service |
| `service_not_found` | 404: no such notify service (check it in Developer tools) |
| `rate_limited`, `server_unavailable`, `publish_refused` | 429, 5xx, another 4xx |
| `redirect_refused` | 3xx: redirects are never followed (give the final URL) |
| `server_timeout`, `network_error` | Connect 5 s, read and write 10 s, 20 s in all; the request is never retried |

### What `apply` returns

The target `<url>/notify.<service>` and `details`: `delivery: "sent"`, `priority`, `actions` (the number of buttons sent) and `click: "dropped"` where it applies.

## Permissions

| Kind | Value | Why |
|---|---|---|
| intents | `notify.push` | applied, never proposed |
| secrets | `HA_TOKEN` | the long-lived access token, sent as a Bearer header to the configured host only |
| network | none in the manifest; your HA host approved in `horae.lock` | see above |
| paths | none | |

## Install

Until M4's `horae plugin add`, pin the commit in `horae.lock` with the permissions above, install the package into an interpreter, and set `runtime.command` to that absolute interpreter (`["/opt/horae-plugins/homeassistant/bin/python", "-m", "horae_homeassistant"]`, horae-sdk README, "The command").

## Not in 0.1.0

Quiet hours and once-a-day reminder state (the core's `key` dedupe covers repeated pushes), event-firing companion actions, notification channels, images and TTS. See the [CHANGELOG](CHANGELOG.md).

## Development

The repository was scaffolded from [`template-plugin`](https://github.com/horae-dev/template-plugin): an MCP stdio server built on [`horae-sdk`](https://github.com/horae-dev/horae/tree/main/sdk), a `plugin.yaml` generated from the code, and CI (lockfile, ruff, mypy strict, pytest with an 85% coverage gate, gitleaks, and the aggregate **CI OK** check). `horae-sdk` is not on PyPI yet: `pyproject.toml` takes it from the core repository's `main` and `uv.lock` pins the commit.

```
uv sync
uv lock --check && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest --cov
uv run python -m horae_homeassistant --manifest > plugin.yaml   # after changing tools or permissions
```

Tests replay a recorded Home Assistant API (`tests/fixtures/ha_api.json`) through an in-memory HTTP transport: no real HA instance, no network. `HORAE_SDK_RECORD=1 uv run pytest` rewrites the expected requests (review the diff; refused in CI).

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
