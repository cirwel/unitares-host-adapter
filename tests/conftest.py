"""Shared test isolation.

The Hermes binding persists a small session-link file under HERMES_HOME and
reads Hermes's session DB for lineage. Tests must never touch a real home, so
both defaults are pointed at a per-test temp dir / a no-op lookup.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    from unitares_host_adapter.bindings import hermes

    monkeypatch.setattr(
        hermes, "_default_links_path", lambda: tmp_path / "plugin-data" / "unitares" / "sessions.json"
    )
    monkeypatch.setattr(hermes, "_default_session_lookup", lambda session_id: None)
    yield
