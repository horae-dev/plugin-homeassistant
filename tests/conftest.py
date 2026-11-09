# SPDX-License-Identifier: Apache-2.0
"""Fixtures over the fakes of `fakes.py` (an in-memory HA API; nothing reaches the network)."""

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_TOKEN", raising=False)
