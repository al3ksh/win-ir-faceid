"""Shared pytest configuration.

Tests must never create or use the real machine TPM key, so TPM password wrapping is
disabled for the whole session (tests that exercise wrapping inject a fake protector).
"""

import os

os.environ["WINIRFACEID_DISABLE_TPM"] = "1"
# The GUI talks to the SYSTEM service by default; tests use a local, injected store.
os.environ.setdefault("WINIRFACEID_LOCAL_STORE", "1")

import pytest


@pytest.fixture(autouse=True)
def _skip_real_logon_check(monkeypatch):
    """Fake test accounts can't pass LogonUserW; tests that need the check patch it back."""
    import engine.ipc_server as ipc

    monkeypatch.setattr(ipc, "_validate_logon_password", lambda *_a, **_k: None)
