# SPDX-License-Identifier: Apache-2.0
"""`plugin.yaml` is valid for the SDK's manifest helpers and matches the plugin's code."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from horae_sdk import Permissions, dump_manifest
from horae_sdk import manifest as build_manifest

from horae_homeassistant import manifest
from horae_homeassistant.__main__ import HEADER, main

PLUGIN_YAML = Path(__file__).parent.parent / "plugin.yaml"


def on_disk() -> dict[str, Any]:
    document = yaml.safe_load(PLUGIN_YAML.read_text("utf-8"))
    assert isinstance(document, dict)
    return document


def test_plugin_yaml_passes_the_sdk_checks() -> None:
    """Rebuilt from the file alone, so a hand edit is checked too (`ManifestError`)."""
    document = on_disk()
    paths = document["permissions"].get("paths", {})
    permissions = Permissions(
        intents=document["permissions"].get("intents", []),
        network=document["permissions"].get("network", []),
        secrets=document["permissions"].get("secrets", []),
        read=paths.get("read", []),
        write=paths.get("write", []),
    )
    rebuilt = build_manifest(
        name=document["name"],
        version=document["version"],
        provides=document["provides"],
        permissions=permissions,
        command=document["runtime"]["command"],
        horae=document["horae"],
        description=document.get("description"),
        license=document.get("license"),
        homepage=document.get("homepage"),
    )
    assert rebuilt == document


def test_plugin_yaml_is_what_the_code_declares() -> None:
    """Regenerate with `uv run python -m horae_homeassistant --manifest > plugin.yaml` after
    changing `PERMISSIONS` or the registered tools."""
    assert on_disk() == manifest()
    assert PLUGIN_YAML.read_text("utf-8") == HEADER + dump_manifest(manifest())


def test_main_prints_the_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--manifest"])
    assert capsys.readouterr().out == PLUGIN_YAML.read_text("utf-8")


def test_main_refuses_unknown_arguments() -> None:
    with pytest.raises(SystemExit, match="usage"):
        main(["--nope"])


def test_version_is_0_2_0() -> None:
    assert on_disk()["version"] == "0.2.0"
    assert 'version = "0.2.0"' in (Path(__file__).parent.parent / "pyproject.toml").read_text()


def test_permissions_are_the_least_the_notifier_needs() -> None:
    """The notifier applies `notify.push`; the source proposes `task.create` and `notify.push`.
    One secret (the same token), no file; the HA host is operator specific, so it is approved in
    horae.lock rather than declared here."""
    document = on_disk()
    assert document["provides"] == {
        "sources": ["homeassistant.reminders"],
        "notifiers": ["homeassistant.push"],
    }
    assert document["permissions"] == {
        "intents": ["notify.push", "task.create"],
        "secrets": ["HA_TOKEN"],
    }
