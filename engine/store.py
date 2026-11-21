"""Windows DPAPI-encrypted face embedding and credential profile store."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import ntsecuritycon
import numpy as np
import pywintypes
import win32api
import win32con
import win32security

from engine.recognizer import DEFAULT_MATCH_THRESHOLD

logger = logging.getLogger(__name__)

CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),
    wintypes.LPCWSTR,
    ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_DATA_BLOB),
]
_crypt32.CryptProtectData.restype = wintypes.BOOL

_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),
    ctypes.POINTER(wintypes.LPWSTR),
    ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_DATA_BLOB),
]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL

_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def dpapi_protect(plaintext: bytes, *, local_machine: bool = False) -> bytes:
    """Encrypt bytes with Windows DPAPI (CryptProtectData)."""
    if not plaintext:
        buf = (ctypes.c_ubyte * 1)()
        in_blob = _DATA_BLOB(0, buf)
    else:
        buf = (ctypes.c_ubyte * len(plaintext)).from_buffer_copy(plaintext)
        in_blob = _DATA_BLOB(len(plaintext), buf)

    out_blob = _DATA_BLOB()
    flags = CRYPTPROTECT_UI_FORBIDDEN | (CRYPTPROTECT_LOCAL_MACHINE if local_machine else 0)
    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "WinIRFaceIDProfileStore",
        None,
        None,
        None,
        flags,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            _kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(ciphertext: bytes) -> bytes:
    """Decrypt bytes previously encrypted with Windows DPAPI (CryptUnprotectData)."""
    if not ciphertext:
        raise ValueError("Ciphertext is empty")
    buf = (ctypes.c_ubyte * len(ciphertext)).from_buffer_copy(ciphertext)
    in_blob = _DATA_BLOB(len(ciphertext), buf)
    out_blob = _DATA_BLOB()

    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            _kernel32.LocalFree(out_blob.pbData)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class UserProfile:
    """Enrolled user face embeddings and optional DPAPI-protected logon credentials."""

    username: str
    domain: str = "."
    embeddings: list[list[float]] = field(default_factory=list)
    password: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)

    def embedding_arrays(self) -> list[np.ndarray]:
        """Return stored embeddings as 1-D float32 numpy arrays."""
        return [np.asarray(e, dtype=np.float32) for e in self.embeddings]

    def to_dict(self) -> dict[str, Any]:
        """Serialize profile to JSON-compatible dictionary."""
        return {
            "username": self.username,
            "domain": self.domain,
            "embeddings": self.embeddings,
            "password": self.password,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserProfile:
        """Deserialize profile from dictionary."""
        return cls(
            username=str(data["username"]),
            domain=str(data.get("domain", ".") or "."),
            embeddings=[[float(v) for v in row] for row in data.get("embeddings", [])],
            password=str(data.get("password", "") or ""),
            created_at=str(data.get("created_at") or _utc_now_iso()),
            updated_at=str(data.get("updated_at") or _utc_now_iso()),
        )


def get_default_store_path() -> Path:
    """Return the default ``<repo>/data/profiles.enc`` file path."""
    return Path(__file__).resolve().parent.parent / "data" / "profiles.enc"


def get_default_service_config_path() -> Path:
    """Return the default ``<repo>/data/service_config.json`` file path."""
    return Path(__file__).resolve().parent.parent / "data" / "service_config.json"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` via a sibling temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def harden_file_acl(path: Path | str) -> bool:
    """Set a protected DACL granting FILE_ALL_ACCESS only to SYSTEM, Admins, and current user."""
    target = Path(path)
    try:
        sid_system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        sid_admins = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32con.TOKEN_QUERY,
        )
        try:
            sid_user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            win32api.CloseHandle(token)

        acl = win32security.ACL()
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid_system)
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid_admins)
        if sid_user != sid_system and sid_user != sid_admins:
            acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid_user)

        win32security.SetNamedSecurityInfo(
            str(target),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )
        return True
    except (pywintypes.error, OSError) as exc:
        logger.warning("Best-effort NTFS ACL hardening skipped for '%s': %s", target, exc)
        return False


@dataclass
class ServiceConfig:
    """Persisted verification settings shared between the GUI and Windows Service."""

    threshold: float = DEFAULT_MATCH_THRESHOLD
    require_motion: bool = False
    require_open_eyes: bool = False


def load_service_config(path: Path | str | None = None) -> ServiceConfig:
    """Load ``ServiceConfig`` from JSON, falling back to defaults if absent or invalid."""
    target = Path(path) if path is not None else get_default_service_config_path()
    if not target.is_file():
        return ServiceConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return ServiceConfig()
        threshold = float(raw.get("threshold", DEFAULT_MATCH_THRESHOLD))
        require_motion = bool(raw.get("require_motion", False))
        require_open_eyes = bool(raw.get("require_open_eyes", False))
        return ServiceConfig(
            threshold=threshold,
            require_motion=require_motion,
            require_open_eyes=require_open_eyes,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load service config '%s', using defaults: %s", target, exc)
        return ServiceConfig()


def save_service_config(cfg: ServiceConfig, path: Path | str | None = None) -> Path:
    """Persist ``ServiceConfig`` as JSON at ``path`` (default ``data/service_config.json``)."""
    target = Path(path) if path is not None else get_default_service_config_path()
    payload = {
        "threshold": float(cfg.threshold),
        "require_motion": bool(cfg.require_motion),
        "require_open_eyes": bool(cfg.require_open_eyes),
    }
    raw = (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    _atomic_write_bytes(target, raw)
    return target


class ProfileStore:
    """Encrypted on-disk profile store backed by Windows DPAPI."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        local_machine: bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else get_default_store_path()
        self.local_machine = local_machine

    def _load_all(self) -> dict[str, UserProfile]:
        if not self.path.is_file():
            return {}
        ciphertext = self.path.read_bytes()
        if not ciphertext:
            return {}
        plaintext = dpapi_unprotect(ciphertext)
        payload = json.loads(plaintext.decode("utf-8"))
        raw_users = payload.get("users", {})
        result: dict[str, UserProfile] = {}
        for key, item in raw_users.items():
            profile = UserProfile.from_dict(item)
            result[key.lower()] = profile
        return result

    def _save_all(self, users: dict[str, UserProfile]) -> None:
        payload = {
            "version": 1,
            "users": {
                profile.username: profile.to_dict()
                for profile in users.values()
            },
        }
        plaintext = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ciphertext = dpapi_protect(plaintext, local_machine=self.local_machine)
        _atomic_write_bytes(self.path, ciphertext)
        if self.local_machine:
            harden_file_acl(self.path)

    def migrate_to_local_machine(self) -> bool:
        """Re-encrypt an existing store with machine-scoped DPAPI and hardened NTFS ACL."""
        if not self.path.is_file():
            return False
        users = self._load_all()
        prev_local_machine = self.local_machine
        self.local_machine = True
        try:
            self._save_all(users)
        except Exception:
            self.local_machine = prev_local_machine
            raise
        return True

    def list_users(self) -> list[UserProfile]:
        """Return all stored user profiles sorted by username."""
        users = self._load_all()
        return sorted(users.values(), key=lambda p: p.username.lower())

    def get_user(self, username: str) -> UserProfile | None:
        """Fetch a user profile by case-insensitive username."""
        clean = username.strip()
        if not clean:
            return None
        return self._load_all().get(clean.lower())

    def upsert_embeddings(
        self,
        username: str,
        embeddings: Sequence[Sequence[float] | np.ndarray],
        *,
        append: bool = True,
        domain: str | None = None,
    ) -> UserProfile:
        """Create or update a user's face embeddings."""
        clean = username.strip()
        if not clean:
            raise ValueError("Username cannot be empty")
        normalized = [
            [float(x) for x in np.asarray(emb, dtype=np.float32).reshape(-1)]
            for emb in embeddings
        ]
        if not normalized:
            raise ValueError("At least one embedding is required")

        users = self._load_all()
        key = clean.lower()
        now = _utc_now_iso()
        existing = users.get(key)
        if existing is None:
            profile = UserProfile(
                username=clean,
                domain=(domain or ".").strip() or ".",
                embeddings=normalized,
                password="",
                created_at=now,
                updated_at=now,
            )
        else:
            existing.username = clean
            if domain is not None:
                existing.domain = domain.strip() or "."
            if append:
                existing.embeddings.extend(normalized)
            else:
                existing.embeddings = normalized
            existing.updated_at = now
            profile = existing

        users[key] = profile
        self._save_all(users)
        return profile

    def set_credentials(
        self,
        username: str,
        password: str,
        domain: str = ".",
    ) -> UserProfile:
        """Store or update a user's DPAPI-encrypted Windows logon credentials."""
        clean = username.strip()
        if not clean:
            raise ValueError("Username cannot be empty")
        clean_domain = domain.strip() or "."

        users = self._load_all()
        key = clean.lower()
        now = _utc_now_iso()
        existing = users.get(key)
        if existing is None:
            profile = UserProfile(
                username=clean,
                domain=clean_domain,
                embeddings=[],
                password=password,
                created_at=now,
                updated_at=now,
            )
        else:
            existing.username = clean
            existing.domain = clean_domain
            existing.password = password
            existing.updated_at = now
            profile = existing

        users[key] = profile
        self._save_all(users)
        return profile

    def delete_user(self, username: str) -> bool:
        """Delete a user profile if present, returning True if removed."""
        clean = username.strip()
        if not clean:
            return False
        users = self._load_all()
        key = clean.lower()
        if key not in users:
            return False
        del users[key]
        self._save_all(users)
        return True
