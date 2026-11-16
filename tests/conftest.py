"""Shared test isolation.

The developer machine has a real OS keyring (and, after local setup, real provider
tokens). Tests must never read it: a missing-token assertion would otherwise pass or
fail depending on the machine. Individual keyring tests monkeypatch a fake backend
explicitly.
"""

from __future__ import annotations

import pytest

from lv24_seo.core import secrets as secrets_module


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets_module, "_keyring_backend", lambda: None)
