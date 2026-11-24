"""Unprivileged client for the WinIRFaceID service's self-service pipe commands.

The profile store (``data/profiles.enc``) is readable only by LocalSystem and
Administrators. The GUI runs as the interactive user, so it manages *its own* profile
through the service instead of touching the file. The service binds every operation to
the caller's Windows identity, so one user can never read or overwrite another user's
face data or password, and no password is ever sent back over the pipe.

``ServiceProfileClient`` is duck-type compatible with the subset of ``ProfileStore`` the
GUI uses (``list_users``, ``get_user``, ``upsert_embeddings``, ``set_credentials``,
``delete_user``) plus ``load_config`` / ``save_config``.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Sequence

import numpy as np
import pywintypes
import win32api
import win32file
import win32pipe
import win32security
import ntsecuritycon

from engine.store import (
    DEFAULT_MAX_FAILED_ATTEMPTS,
    ServiceConfig,
    UserProfile,
    clamp_max_failed_attempts,
)

PIPE_NAME = r"\\.\pipe\WinIRFaceID"
_SECURITY_SQOS_PRESENT = 0x00100000
_SECURITY_IDENTIFICATION = 0x00010000
_PIPE_CLIENT_ACCESS = ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_WRITE_DATA
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_REDACTED_BLOB = "service:redacted"


class ServiceUnavailableError(RuntimeError):
    """The WinIRFaceID service pipe is not reachable (service stopped / not installed)."""


class ServiceRequestError(RuntimeError):
    """The service rejected a request."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _profile_from_remote(data: dict[str, Any] | None) -> UserProfile | None:
    if not data:
        return None
    profile = UserProfile(
        username=str(data.get("username", "")),
        domain=str(data.get("domain", ".") or "."),
        embeddings=[[float(v) for v in row] for row in data.get("embeddings", [])],
        password="",
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
        looks=data.get("looks") or [],
    )
    if data.get("has_password"):
        # The real password never leaves the service; mark it as present.
        profile.password_blob = _REDACTED_BLOB
    return profile


class ServiceProfileClient:
    """Manage the calling user's own profile through the SYSTEM service."""

    def __init__(
        self,
        pipe_name: str = PIPE_NAME,
        *,
        timeout_ms: int = 3000,
        cache_ttl: float = 2.0,
        require_system_server: bool = True,
    ) -> None:
        self.pipe_name = pipe_name
        self.timeout_ms = int(timeout_ms)
        self.cache_ttl = float(cache_ttl)
        self.require_system_server = require_system_server
        self._lock = threading.Lock()
        self._cache: tuple[float, UserProfile | None] | None = None
        try:
            self._owner = win32api.GetUserName()
        except Exception:
            self._owner = ""

    @property
    def owner_username(self) -> str:
        return self._owner

    def is_owner(self, username: str) -> bool:
        return bool(self._owner) and username.strip().lower() == self._owner.lower()

    # ---- transport -------------------------------------------------------------------

    def _verify_server(self, handle: Any) -> None:
        """Refuse to talk to a pipe server that is not running in session 0 (services)."""
        if not self.require_system_server:
            return
        try:
            # Ask the pipe itself; a standard user cannot open the SYSTEM process by PID.
            session = win32pipe.GetNamedPipeServerSessionId(handle)
        except Exception as exc:
            raise ServiceUnavailableError(f"Cannot verify pipe server: {exc}") from exc
        if session != 0:
            raise ServiceUnavailableError("Pipe server is not the WinIRFaceID service (not in session 0)")

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
        try:
            win32pipe.WaitNamedPipe(self.pipe_name, self.timeout_ms)
            handle = win32file.CreateFile(
                self.pipe_name,
                _PIPE_CLIENT_ACCESS,
                0,
                None,
                win32file.OPEN_EXISTING,
                _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION,
                None,
            )
        except pywintypes.error as exc:
            raise ServiceUnavailableError(f"WinIRFaceID service is not reachable: {exc.strerror}") from exc
        try:
            self._verify_server(handle)
            win32file.WriteFile(handle, data)
            buf = bytearray()
            while b"\n" not in buf and len(buf) < _MAX_RESPONSE_BYTES:
                try:
                    _hr, chunk = win32file.ReadFile(handle, 65536)
                except pywintypes.error as exc:
                    if exc.winerror in (109, 232, 234):  # broken / closing / more data
                        if exc.winerror == 234:
                            continue
                        break
                    raise
                if not chunk:
                    break
                buf.extend(chunk)
        except pywintypes.error as exc:
            raise ServiceUnavailableError(f"Pipe I/O failed: {exc.strerror}") from exc
        finally:
            try:
                win32file.CloseHandle(handle)
            except Exception:
                pass
        line = bytes(buf).split(b"\n", 1)[0]
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceUnavailableError("Malformed response from service") from exc
        if not isinstance(response, dict):
            raise ServiceUnavailableError("Malformed response from service")
        if response.get("status") != "ok":
            raise ServiceRequestError(str(response.get("reason", "unknown_error")))
        return response

    # ---- profile API (ProfileStore-compatible subset) -------------------------------

    def _check_owner(self, username: str) -> None:
        if not self.is_owner(username):
            raise PermissionError(
                f"You can only manage your own profile ('{self._owner}'), not '{username}'."
            )

    def _set_cache(self, profile: UserProfile | None) -> None:
        self._cache = (time.monotonic(), profile)

    def _own_profile(self) -> UserProfile | None:
        with self._lock:
            if self._cache is not None and (time.monotonic() - self._cache[0]) < self.cache_ttl:
                return self._cache[1]
            profile = _profile_from_remote(self._request({"cmd": "profile_get"}).get("profile"))
            self._set_cache(profile)
            return profile

    def list_users(self) -> list[UserProfile]:
        profile = self._own_profile()
        return [profile] if profile is not None else []

    def get_user(self, username: str) -> UserProfile | None:
        if not self.is_owner(username):
            return None
        return self._own_profile()

    def upsert_embeddings(
        self,
        username: str,
        embeddings: Sequence[Sequence[float] | np.ndarray],
        *,
        append: bool = True,
        domain: str | None = None,
        sid: str | None = None,
    ) -> UserProfile:
        self._check_owner(username)
        rows = [
            [float(x) for x in np.asarray(e, dtype=np.float32).reshape(-1)] for e in embeddings
        ]
        with self._lock:
            self._cache = None
            resp = self._request({"cmd": "profile_enroll", "embeddings": rows, "append": bool(append)})
            profile = _profile_from_remote(resp.get("profile"))
            self._set_cache(profile)
        if profile is None:
            raise ServiceRequestError("enroll_failed")
        return profile

    def set_credentials(
        self,
        username: str,
        password: str,
        domain: str = ".",
        sid: str | None = None,
    ) -> UserProfile:
        self._check_owner(username)
        with self._lock:
            self._cache = None
            resp = self._request({"cmd": "profile_set_password", "password": password, "domain": domain})
            profile = _profile_from_remote(resp.get("profile"))
            self._set_cache(profile)
        if profile is None:
            raise ServiceRequestError("set_password_failed")
        return profile

    def delete_look(self, username: str, look_id: str) -> UserProfile:
        self._check_owner(username)
        with self._lock:
            self._cache = None
            resp = self._request({"cmd": "profile_delete_look", "look_id": str(look_id)})
            profile = _profile_from_remote(resp.get("profile"))
            self._set_cache(profile)
        if profile is None:
            raise ServiceRequestError("not_found")
        return profile

    def rename_look(self, username: str, look_id: str, label: str) -> UserProfile:
        self._check_owner(username)
        clean_label = str(label or "").strip()
        if not clean_label or len(clean_label) > 32:
            raise ValueError("invalid_label")
        with self._lock:
            self._cache = None
            resp = self._request(
                {"cmd": "profile_rename_look", "look_id": str(look_id), "label": clean_label}
            )
            profile = _profile_from_remote(resp.get("profile"))
            self._set_cache(profile)
        if profile is None:
            raise ServiceRequestError("not_found")
        return profile

    def delete_user(self, username: str) -> bool:
        self._check_owner(username)
        with self._lock:
            self._cache = None
            resp = self._request({"cmd": "profile_delete"})
            self._set_cache(None)
        return bool(resp.get("deleted"))

    # ---- config ---------------------------------------------------------------------

    def load_config(self) -> ServiceConfig:
        cfg = self._request({"cmd": "config_get"}).get("config") or {}
        return ServiceConfig(
            threshold=float(cfg.get("threshold", ServiceConfig().threshold)),
            require_motion=bool(cfg.get("require_motion", False)),
            require_open_eyes=bool(cfg.get("require_open_eyes", False)),
            max_failed_attempts=clamp_max_failed_attempts(
                cfg.get("max_failed_attempts", DEFAULT_MAX_FAILED_ATTEMPTS)
            ),
        )

    def save_config(self, cfg: ServiceConfig) -> ServiceConfig:
        resp = self._request(
            {
                "cmd": "config_set",
                "threshold": float(cfg.threshold),
                "require_motion": bool(cfg.require_motion),
                "require_open_eyes": bool(cfg.require_open_eyes),
                "max_failed_attempts": clamp_max_failed_attempts(cfg.max_failed_attempts),
            }
        )
        out = resp.get("config") or {}
        return ServiceConfig(
            threshold=float(out.get("threshold", cfg.threshold)),
            require_motion=bool(out.get("require_motion", cfg.require_motion)),
            require_open_eyes=bool(out.get("require_open_eyes", cfg.require_open_eyes)),
            max_failed_attempts=clamp_max_failed_attempts(
                out.get("max_failed_attempts", cfg.max_failed_attempts)
            ),
        )
