"""Persistent Face ID lockout after too many failed scans (phone-style).

After ``max_failed_attempts`` failed face scans the WinIRFaceID service refuses every
further ``verify`` request until a Windows session is unlocked (or signed in) with
another method: PIN, fingerprint, or password. The state lives in
``data/biometric_lockout.json`` (SYSTEM/Administrators only), so it survives service
restarts and reboots.

Only scans in which a face was actually detected count as failures, so a scan that
times out with nobody in front of the camera never locks anyone out.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from engine.store import _atomic_write_bytes, harden_file_acl

logger = logging.getLogger(__name__)

LOCKED_OUT_REASON = "locked_out"


def get_default_lockout_path() -> Path:
    """Return the default ``<repo>/data/biometric_lockout.json`` file path."""
    return Path(__file__).resolve().parent.parent / "data" / "biometric_lockout.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class BiometricLockout:
    """Thread-safe failed-scan counter with a lock flag persisted to disk."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else get_default_lockout_path()
        self._lock = threading.Lock()
        self._failed_attempts = 0
        self._locked = False
        self._locked_at: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("lockout state is not an object")
            attempts = raw.get("failed_attempts", 0)
            if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
                raise ValueError("invalid failed_attempts")
            locked = raw.get("locked", False)
            if not isinstance(locked, bool):
                raise ValueError("invalid locked flag")
            locked_at = raw.get("locked_at")
            self._failed_attempts = attempts
            self._locked = locked
            self._locked_at = str(locked_at) if locked_at else None
        except (OSError, ValueError, TypeError) as exc:
            # Fail closed: a damaged state file must not silently re-enable Face ID.
            # Unlocking Windows with PIN / fingerprint / password clears it again.
            logger.warning("Unreadable lockout state '%s' (%s); treating Face ID as locked.", self.path, exc)
            self._failed_attempts = 0
            self._locked = True
            self._locked_at = _utc_now_iso()

    def _save_locked(self) -> None:
        payload = {
            "failed_attempts": int(self._failed_attempts),
            "locked": bool(self._locked),
            "locked_at": self._locked_at,
        }
        try:
            _atomic_write_bytes(
                self.path,
                (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode("utf-8"),
            )
            harden_file_acl(self.path)
        except OSError as exc:
            logger.error("Failed to persist lockout state '%s': %s", self.path, exc)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "locked": self._locked,
                "failed_attempts": self._failed_attempts,
                "locked_at": self._locked_at,
            }

    def is_locked(self) -> bool:
        with self._lock:
            return self._locked

    def record_failure(self, max_attempts: int) -> bool:
        """Count one failed scan. Returns True when Face ID is (now) locked."""
        with self._lock:
            if max_attempts <= 0:
                return self._locked
            self._failed_attempts += 1
            if self._failed_attempts >= max_attempts and not self._locked:
                self._locked = True
                self._locked_at = _utc_now_iso()
                logger.warning(
                    "Face ID locked after %d failed scan(s); another sign-in method is required.",
                    self._failed_attempts,
                )
            self._save_locked()
            return self._locked

    def record_success(self) -> None:
        """A face match resets the failed-scan counter."""
        with self._lock:
            if self._failed_attempts == 0 and not self._locked:
                return
            previous_attempts = self._failed_attempts
            self._failed_attempts = 0
            self._save_locked()
        logger.info("Failed-scan counter reset from %d (face matched).", previous_attempts)

    def reset(self, reason: str = "") -> None:
        """Clear the counter and the lock (Windows was unlocked with another method)."""
        with self._lock:
            if self._failed_attempts == 0 and not self._locked:
                return
            was_locked = self._locked
            previous_attempts = self._failed_attempts
            self._failed_attempts = 0
            self._locked = False
            self._locked_at = None
            self._save_locked()
        if was_locked:
            logger.info("Face ID lockout cleared (%s).", reason or "reset")
        else:
            logger.info(
                "Failed-scan counter reset from %d (%s).", previous_attempts, reason or "reset"
            )


# ---- Windows session unlock detection --------------------------------------------------

_WTS_CURRENT_SERVER_HANDLE = 0
_WTS_ACTIVE = 0
_WTS_DISCONNECTED = 4
_WTS_SESSION_INFO_EX = 25
_WTS_SESSIONSTATE_UNLOCK = 1
# WTSINFOEXW = { DWORD Level; union { WTSINFOEX_LEVEL1_W } } and LEVEL1 holds LARGE_INTEGERs,
# so it starts 8-byte aligned at offset 8: SessionId (+0), SessionState (+4),
# SessionFlags (+8), WinStationName[33] (+12), UserName[21] (+78).
_INFOEX_LEVEL1_OFFSET = 8
_INFOEX_FLAGS_OFFSET = _INFOEX_LEVEL1_OFFSET + 8
_INFOEX_USERNAME_OFFSET = _INFOEX_LEVEL1_OFFSET + 12 + 33 * 2
_INFOEX_USERNAME_CHARS = 21


class _WTS_SESSION_INFOW(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.DWORD),
        ("pWinStationName", wintypes.LPWSTR),
        ("State", ctypes.c_int),
    ]


def list_user_sessions() -> dict[int, bool]:
    """Map session ID -> True if unlocked, for every session with a signed-in user.

    Disconnected sessions (fast user switching) are reported as locked: getting back
    into one requires authenticating again, exactly like unlocking a locked desktop.
    """
    wtsapi = ctypes.WinDLL("wtsapi32")
    p_info = ctypes.POINTER(_WTS_SESSION_INFOW)()
    count = wintypes.DWORD(0)
    if not wtsapi.WTSEnumerateSessionsW(
        wintypes.HANDLE(_WTS_CURRENT_SERVER_HANDLE), 0, 1, ctypes.byref(p_info), ctypes.byref(count)
    ):
        raise ctypes.WinError()
    sessions: dict[int, bool] = {}
    try:
        for i in range(count.value):
            info = p_info[i]
            if info.State not in (_WTS_ACTIVE, _WTS_DISCONNECTED):
                continue
            buf = ctypes.c_void_p()
            size = wintypes.DWORD(0)
            if not wtsapi.WTSQuerySessionInformationW(
                wintypes.HANDLE(_WTS_CURRENT_SERVER_HANDLE),
                wintypes.DWORD(info.SessionId),
                _WTS_SESSION_INFO_EX,
                ctypes.byref(buf),
                ctypes.byref(size),
            ):
                continue
            try:
                if not buf.value or size.value < _INFOEX_USERNAME_OFFSET + 2:
                    continue
                flags = ctypes.c_long.from_address(buf.value + _INFOEX_FLAGS_OFFSET).value
                username = ctypes.wstring_at(buf.value + _INFOEX_USERNAME_OFFSET, _INFOEX_USERNAME_CHARS)
                username = username.split("\x00", 1)[0]
                if username:
                    sessions[int(info.SessionId)] = (
                        info.State == _WTS_ACTIVE and flags == _WTS_SESSIONSTATE_UNLOCK
                    )
            finally:
                wtsapi.WTSFreeMemory(buf)
    finally:
        wtsapi.WTSFreeMemory(p_info)
    return sessions


class SessionUnlockWatcher:
    """Clears the lockout when a session this watcher saw *locked* becomes unlocked.

    While Face ID is locked, the WinIRFaceID tile cannot sign anyone in, so unlocking a
    locked desktop proves the user authenticated with PIN, fingerprint, or password.

    New sessions that appear already unlocked never count: after a reboot Windows may
    sign the user in by itself (Automatic Restart Sign-On, "use my sign-in info after a
    restart") and only then lock the desktop, which proves nothing about who is there.
    """

    def __init__(
        self,
        lockout: BiometricLockout,
        *,
        poll_interval: float = 1.5,
        session_lister: Callable[[], dict[int, bool]] | None = None,
    ) -> None:
        self._lockout = lockout
        self._poll_interval = poll_interval
        self._lister = session_lister or list_user_sessions
        self._seen_locked: set[int] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> None:
        try:
            current = self._lister()
        except OSError as exc:
            logger.debug("Session enumeration failed: %s", exc)
            return
        unlocked_again = False
        for session_id, unlocked in current.items():
            if not unlocked:
                self._seen_locked.add(session_id)
            elif session_id in self._seen_locked:
                self._seen_locked.discard(session_id)
                unlocked_again = True
        # Forget signed-out sessions: a reused session ID must be seen locked again.
        self._seen_locked.intersection_update(current)
        if unlocked_again:
            self._lockout.reset("Windows session unlocked")

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="WinIRFaceID-SessionWatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
