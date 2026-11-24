"""Tests for the persistent Face ID lockout after failed scans."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from engine.ipc_server import ClientIdentity, handle_request
from engine.lockout import LOCKED_OUT_REASON, BiometricLockout, SessionUnlockWatcher
from engine.store import (
    DEFAULT_MAX_FAILED_ATTEMPTS,
    ProfileStore,
    ServiceConfig,
    clamp_max_failed_attempts,
    load_service_config,
    save_service_config,
)


@pytest.fixture(autouse=True)
def _no_acl_changes():
    # Hardening ACLs on tmp files would make them unreadable for the test user.
    with patch("engine.lockout.harden_file_acl"):
        yield


def _lockout(tmp_path: Path) -> BiometricLockout:
    return BiometricLockout(tmp_path / "biometric_lockout.json")


# ---- BiometricLockout -------------------------------------------------------------------


def test_lockout_locks_at_limit_and_persists_across_instances(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    assert lock.is_locked() is False
    assert lock.record_failure(3) is False
    assert lock.record_failure(3) is False
    assert lock.record_failure(3) is True
    assert lock.is_locked() is True

    # Simulates a service restart / reboot.
    again = _lockout(tmp_path)
    assert again.is_locked() is True
    assert again.status()["failed_attempts"] == 3
    assert again.status()["locked_at"]


def test_lockout_limit_one_locks_on_first_failure(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    assert lock.record_failure(1) is True
    assert lock.is_locked() is True


def test_lockout_success_resets_counter_but_not_lock(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    lock.record_failure(5)
    lock.record_failure(5)
    lock.record_success()
    assert lock.status()["failed_attempts"] == 0
    assert _lockout(tmp_path).status()["failed_attempts"] == 0

    lock.record_failure(1)
    lock.record_success()
    assert lock.is_locked() is True


def test_lockout_reset_clears_lock_and_persists(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    lock.record_failure(1)
    lock.reset("test")
    assert lock.is_locked() is False
    assert lock.status() == {"locked": False, "failed_attempts": 0, "locked_at": None}
    assert _lockout(tmp_path).is_locked() is False


def test_lockout_disabled_limit_does_not_count(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    assert lock.record_failure(0) is False
    assert lock.status()["failed_attempts"] == 0
    assert not (tmp_path / "biometric_lockout.json").exists()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"failed_attempts": -1, "locked": False}),
        json.dumps({"failed_attempts": "2", "locked": False}),
        json.dumps({"failed_attempts": 0, "locked": "no"}),
    ],
)
def test_lockout_corrupt_state_fails_closed(tmp_path: Path, content: str) -> None:
    (tmp_path / "biometric_lockout.json").write_text(content, encoding="utf-8")
    assert _lockout(tmp_path).is_locked() is True


# ---- SessionUnlockWatcher ------------------------------------------------------------


def test_unlock_watcher_resets_only_on_locked_to_unlocked_transition(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    sessions: list[dict[int, bool]] = [{}, {1: False}, {1: True}, {1: True}, {1: False}, {1: True}]
    watcher = SessionUnlockWatcher(lock, session_lister=lambda: sessions.pop(0))

    watcher.poll_once()  # nobody signed in
    lock.record_failure(1)
    watcher.poll_once()  # session 1 at the lock screen
    assert lock.is_locked() is True

    watcher.poll_once()  # session 1 unlocked with PIN / fingerprint
    assert lock.is_locked() is False

    lock.record_failure(1)
    watcher.poll_once()  # still the same unlocked session: no new unlock
    assert lock.is_locked() is True
    watcher.poll_once()  # Win+L
    watcher.poll_once()  # unlocked again
    assert lock.is_locked() is False


def test_unlock_watcher_ignores_sessions_already_unlocked_at_start(tmp_path: Path) -> None:
    """Regression: Automatic Restart Sign-On must not clear a persisted lockout.

    After a reboot Windows can sign the user in by itself (session appears unlocked)
    and lock it right after. Only a later unlock of that locked session counts.
    """
    lock = _lockout(tmp_path)
    lock.record_failure(1)
    sessions: list[dict[int, bool]] = [{1: True}, {1: True}, {1: False}, {1: True}]
    watcher = SessionUnlockWatcher(lock, session_lister=lambda: sessions.pop(0))

    watcher.poll_once()  # service starts while ARSO session is briefly unlocked
    watcher.poll_once()
    assert lock.is_locked() is True
    watcher.poll_once()  # Windows locks the ARSO session
    assert lock.is_locked() is True
    watcher.poll_once()  # user unlocks with PIN / fingerprint
    assert lock.is_locked() is False


def test_unlock_watcher_forgets_signed_out_sessions(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    sessions: list[dict[int, bool]] = [{2: False}, {}, {2: True}]
    watcher = SessionUnlockWatcher(lock, session_lister=lambda: sessions.pop(0))
    watcher.poll_once()  # session 2 locked
    lock.record_failure(1)
    watcher.poll_once()  # user signed out
    watcher.poll_once()  # new sign-in reusing session ID 2 (could be ARSO)
    assert lock.is_locked() is True


def test_unlock_watcher_ignores_enumeration_errors(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    lock.record_failure(1)

    def boom() -> dict[int, bool]:
        raise OSError("WTS unavailable")

    SessionUnlockWatcher(lock, session_lister=boom).poll_once()
    assert lock.is_locked() is True


# ---- handle_request integration --------------------------------------------------------


def _verify(tmp_path: Path, lock: BiometricLockout, verifier: Any, limit: int) -> dict[str, Any]:
    config_path = tmp_path / "service_config.json"
    save_service_config(ServiceConfig(max_failed_attempts=limit), config_path)
    return handle_request(
        {"cmd": "verify", "username": "DELL", "timeout_ms": 5000},
        store=ProfileStore(tmp_path / "profiles.enc"),
        verifier=verifier,
        config_path=config_path,
        lockout=lock,
    )


def _failing_verifier(reason: str = "timeout", face_detected: bool = True) -> MagicMock:
    verifier = MagicMock()
    verifier.verify.side_effect = lambda **_kw: {
        "status": "failed",
        "reason": reason,
        "face_detected": face_detected,
    }
    return verifier


def test_verify_counts_failed_scans_and_locks_at_limit(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    verifier = _failing_verifier()

    first = _verify(tmp_path, lock, verifier, limit=2)
    assert first == {"status": "failed", "reason": "timeout"}  # internal flag stripped
    second = _verify(tmp_path, lock, verifier, limit=2)
    assert second == {"status": "failed", "reason": LOCKED_OUT_REASON}

    verifier.verify.reset_mock()
    third = _verify(tmp_path, lock, verifier, limit=2)
    assert third == {"status": "failed", "reason": LOCKED_OUT_REASON}
    verifier.verify.assert_not_called()  # camera is never touched while locked


def test_verify_cancel_with_face_counts(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    resp = _verify(tmp_path, lock, _failing_verifier("cancelled"), limit=1)
    assert resp["reason"] == LOCKED_OUT_REASON
    assert lock.is_locked() is True


@pytest.mark.parametrize(
    "reason,face_detected",
    [
        ("timeout", False),
        ("cancelled", False),
        ("camera_error", True),
        ("credential_error", True),
        ("no_enrolled_faces", True),
    ],
)
def test_verify_failures_that_do_not_count(tmp_path: Path, reason: str, face_detected: bool) -> None:
    lock = _lockout(tmp_path)
    resp = _verify(tmp_path, lock, _failing_verifier(reason, face_detected), limit=1)
    assert resp == {"status": "failed", "reason": reason}
    assert lock.is_locked() is False
    assert lock.status()["failed_attempts"] == 0


def test_verify_match_resets_counter(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    _verify(tmp_path, lock, _failing_verifier(), limit=3)
    matcher = MagicMock()
    matcher.verify.return_value = {"status": "matched", "username": "DELL", "password": "x"}
    assert _verify(tmp_path, lock, matcher, limit=3)["status"] == "matched"
    assert lock.status()["failed_attempts"] == 0


def test_verify_lockout_disabled_ignores_existing_lock(tmp_path: Path) -> None:
    lock = _lockout(tmp_path)
    lock.record_failure(1)
    matcher = MagicMock()
    matcher.verify.return_value = {"status": "matched", "username": "DELL", "password": "x"}
    assert _verify(tmp_path, lock, matcher, limit=0)["status"] == "matched"
    matcher.verify.assert_called_once()


# ---- config ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1, 1),
        (10, 10),
        (0, 0),
        (-3, 0),
        (99, 10),
        ("4", 4),
        (True, DEFAULT_MAX_FAILED_ATTEMPTS),
        ("abc", DEFAULT_MAX_FAILED_ATTEMPTS),
        (None, DEFAULT_MAX_FAILED_ATTEMPTS),
    ],
)
def test_clamp_max_failed_attempts(raw: Any, expected: int) -> None:
    assert clamp_max_failed_attempts(raw) == expected


def test_max_failed_attempts_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "service_config.json"
    assert load_service_config(path).max_failed_attempts == DEFAULT_MAX_FAILED_ATTEMPTS
    save_service_config(ServiceConfig(max_failed_attempts=1), path)
    assert load_service_config(path).max_failed_attempts == 1


def test_config_set_validates_max_failed_attempts(tmp_path: Path) -> None:
    path = tmp_path / "service_config.json"
    store = ProfileStore(tmp_path / "profiles.enc")
    system = ClientIdentity(sid="S-1-5-18", username="SYSTEM", domain="NT AUTHORITY", is_system=True, privileged=True)

    resp = handle_request({"cmd": "config_set", "max_failed_attempts": 1}, store=store, config_path=path, client=system)
    assert resp["config"]["max_failed_attempts"] == 1
    assert load_service_config(path).max_failed_attempts == 1

    resp = handle_request({"cmd": "config_set", "max_failed_attempts": 50}, store=store, config_path=path, client=system)
    assert resp["config"]["max_failed_attempts"] == 10

    for bad in (True, "3", 2.5):
        resp = handle_request({"cmd": "config_set", "max_failed_attempts": bad}, store=store, config_path=path, client=system)
        assert resp == {"status": "failed", "reason": "bad_request"}
