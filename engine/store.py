"""Windows DPAPI-encrypted face embedding and credential profile store."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import secrets
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
from engine.tpm import (
    MAX_PLAINTEXT_BYTES as TPM_MAX_PLAINTEXT_BYTES,
    TPM_BLOB_PREFIX,
    TpmKeyProtector,
    TpmUnavailableError,
    tpm_disabled_by_env,
)

logger = logging.getLogger(__name__)

CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04

MIN_SERVICE_THRESHOLD = 0.60
MAX_SERVICE_THRESHOLD = 0.80
# Failed face scans allowed before Face ID locks until another sign-in method is used.
# 0 disables the lockout entirely.
MAX_FAILED_ATTEMPTS_LIMIT = 10
DEFAULT_MAX_FAILED_ATTEMPTS = 5

_default_tpm_protector = TpmKeyProtector()


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


#: Samples captured per enrollment pass; used to split legacy profiles into looks.
LEGACY_LOOK_SIZE = 8


def _new_look_id() -> str:
    return secrets.token_hex(4)


def _new_look(count: int, added_at: str | None = None) -> dict[str, Any]:
    return {"id": _new_look_id(), "added_at": added_at or _utc_now_iso(), "count": int(count)}


def normalize_looks(looks: Any, total: int, fallback_added_at: str) -> list[dict[str, Any]]:
    """Return a valid look list partitioning ``total`` embeddings.

    Each look is one enrollment pass (``{"id", "added_at", "count"}``) covering a
    contiguous slice of the flat embedding list. Profiles written before looks existed
    (or with an inconsistent list) are split into chunks of :data:`LEGACY_LOOK_SIZE`.
    """
    out: list[dict[str, Any]] = []
    if isinstance(looks, list):
        seen: set[str] = set()
        for item in looks:
            if not isinstance(item, dict):
                out = []
                break
            look_id = str(item.get("id", "") or "")[:64]
            try:
                count = int(item.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if not look_id or look_id in seen or count <= 0:
                out = []
                break
            seen.add(look_id)
            entry: dict[str, Any] = {
                "id": look_id,
                "added_at": str(item.get("added_at") or fallback_added_at),
                "count": count,
            }
            raw_label = item.get("label")
            if isinstance(raw_label, str):
                clean_label = raw_label.strip()[:32]
                if clean_label:
                    entry["label"] = clean_label
            out.append(entry)
    if out and sum(l["count"] for l in out) == total:
        return out
    legacy: list[dict[str, Any]] = []
    for i, start in enumerate(range(0, total, LEGACY_LOOK_SIZE)):
        count = min(LEGACY_LOOK_SIZE, total - start)
        legacy.append({"id": f"legacy-{i}", "added_at": fallback_added_at, "count": count})
    return legacy


@dataclass
class UserProfile:
    """Enrolled user face embeddings and optional protected logon credentials.

    ``password`` holds a plaintext password only while it is not TPM-wrapped (legacy
    stores, machines without a TPM). ``password_blob`` holds the TPM-wrapped form
    (``tpm1:<base64>``); use :meth:`resolve_password` to obtain the plaintext.
    """

    username: str
    domain: str = "."
    embeddings: list[list[float]] = field(default_factory=list)
    password: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    password_blob: str = ""
    sid: str = ""
    looks: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.looks = normalize_looks(self.looks, len(self.embeddings), self.created_at)

    @property
    def has_password(self) -> bool:
        """True if a logon password (plaintext or TPM-wrapped) is stored."""
        return bool(self.password or self.password_blob)

    def resolve_password(self, protector: TpmKeyProtector | None = None) -> str:
        """Return the plaintext password, unwrapping it inside the TPM when needed."""
        if self.password or not self.password_blob:
            return self.password
        if not self.password_blob.startswith(TPM_BLOB_PREFIX):
            raise TpmUnavailableError("Unknown password blob format")
        raw = base64.b64decode(self.password_blob[len(TPM_BLOB_PREFIX):])
        return (protector or _default_tpm_protector).unwrap(raw).decode("utf-8")

    def embedding_arrays(self) -> list[np.ndarray]:
        """Return stored embeddings as 1-D float32 numpy arrays."""
        return [np.asarray(e, dtype=np.float32) for e in self.embeddings]

    def to_dict(self) -> dict[str, Any]:
        """Serialize profile to JSON-compatible dictionary."""
        data: dict[str, Any] = {
            "username": self.username,
            "domain": self.domain,
            "embeddings": self.embeddings,
            "password": self.password,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "looks": normalize_looks(self.looks, len(self.embeddings), self.created_at),
        }
        if self.password_blob:
            data["password_tpm"] = self.password_blob
        if self.sid:
            data["sid"] = self.sid
        return data

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
            password_blob=str(data.get("password_tpm", "") or ""),
            sid=str(data.get("sid", "") or ""),
            looks=data.get("looks") or [],
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


def _current_token_is_elevated() -> bool:
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return bool(win32security.GetTokenInformation(token, win32security.TokenElevation))
    finally:
        win32api.CloseHandle(token)


def harden_file_acl(path: Path | str) -> bool:
    """Set a protected DACL granting FILE_ALL_ACCESS only to SYSTEM and Administrators.

    The writing user is added only when the process is neither SYSTEM nor elevated
    (developer runs and tests) so the writer can still read back its own file. Files
    written by the WinIRFaceID service (SYSTEM) or the elevated installer are therefore
    unreadable by any standard, non-elevated process.
    """
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
        if sid_user != sid_system and sid_user != sid_admins and not _current_token_is_elevated():
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
    max_failed_attempts: int = DEFAULT_MAX_FAILED_ATTEMPTS


def clamp_max_failed_attempts(value: Any) -> int:
    """Clamp the failed-scan limit into 0 (disabled) .. ``MAX_FAILED_ATTEMPTS_LIMIT``."""
    if isinstance(value, bool):
        return DEFAULT_MAX_FAILED_ATTEMPTS
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_MAX_FAILED_ATTEMPTS
    return min(MAX_FAILED_ATTEMPTS_LIMIT, max(0, number))


def clamp_service_threshold(value: float) -> float:
    """Clamp a match threshold into the supported, secure 0.60-0.80 range."""
    if value != value:  # NaN
        return DEFAULT_MATCH_THRESHOLD
    return round(min(MAX_SERVICE_THRESHOLD, max(MIN_SERVICE_THRESHOLD, float(value))), 2)


def load_service_config(path: Path | str | None = None) -> ServiceConfig:
    """Load ``ServiceConfig`` from JSON, falling back to defaults if absent or invalid."""
    target = Path(path) if path is not None else get_default_service_config_path()
    if not target.is_file():
        return ServiceConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return ServiceConfig()
        threshold = clamp_service_threshold(float(raw.get("threshold", DEFAULT_MATCH_THRESHOLD)))
        require_motion = bool(raw.get("require_motion", False))
        require_open_eyes = bool(raw.get("require_open_eyes", False))
        max_failed_attempts = clamp_max_failed_attempts(
            raw.get("max_failed_attempts", DEFAULT_MAX_FAILED_ATTEMPTS)
        )
        return ServiceConfig(
            threshold=threshold,
            require_motion=require_motion,
            require_open_eyes=require_open_eyes,
            max_failed_attempts=max_failed_attempts,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load service config '%s', using defaults: %s", target, exc)
        return ServiceConfig()


def save_service_config(cfg: ServiceConfig, path: Path | str | None = None) -> Path:
    """Persist ``ServiceConfig`` as JSON at ``path`` (default ``data/service_config.json``)."""
    target = Path(path) if path is not None else get_default_service_config_path()
    payload = {
        "threshold": clamp_service_threshold(float(cfg.threshold)),
        "require_motion": bool(cfg.require_motion),
        "require_open_eyes": bool(cfg.require_open_eyes),
        "max_failed_attempts": clamp_max_failed_attempts(cfg.max_failed_attempts),
    }
    raw = (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    _atomic_write_bytes(target, raw)
    return target


class ProfileStore:
    """Encrypted on-disk profile store: DPAPI for the file, TPM for logon passwords.

    The whole file is DPAPI-encrypted. In machine scope, logon passwords are additionally
    wrapped with a non-exportable TPM key (see ``engine.tpm``) whenever a TPM is available.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        local_machine: bool = True,
        use_tpm: bool | None = None,
        tpm_protector: TpmKeyProtector | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else get_default_store_path()
        self.local_machine = local_machine
        if use_tpm is None:
            use_tpm = local_machine and not tpm_disabled_by_env()
        self.use_tpm = bool(use_tpm)
        self._tpm = tpm_protector or _default_tpm_protector
        self._tpm_failed = False

    def _wrap_passwords(self, users: dict[str, UserProfile]) -> None:
        """Move plaintext passwords into TPM-wrapped blobs (best effort, DPAPI-only fallback)."""
        if not self.use_tpm or self._tpm_failed:
            return
        for profile in users.values():
            if not profile.password:
                continue
            secret = profile.password.encode("utf-8")
            if len(secret) > TPM_MAX_PLAINTEXT_BYTES:
                logger.warning("Password for '%s' too long for TPM wrapping; DPAPI only", profile.username)
                continue
            try:
                blob = self._tpm.wrap(secret)
            except (TpmUnavailableError, OSError) as exc:
                logger.warning("TPM password wrapping unavailable, using DPAPI only: %s", exc)
                self._tpm_failed = True
                return
            profile.password_blob = TPM_BLOB_PREFIX + base64.b64encode(blob).decode("ascii")
            profile.password = ""

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
        self._wrap_passwords(users)
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
        sid: str | None = None,
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
                sid=sid or "",
                looks=[_new_look(len(normalized), now)],
            )
        else:
            existing.username = clean
            if sid:
                existing.sid = sid
            if domain is not None:
                existing.domain = domain.strip() or "."
            if append:
                existing.looks = normalize_looks(
                    existing.looks, len(existing.embeddings), existing.created_at
                ) + [_new_look(len(normalized), now)]
                existing.embeddings.extend(normalized)
            else:
                existing.embeddings = normalized
                existing.looks = [_new_look(len(normalized), now)]
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
        sid: str | None = None,
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
                sid=sid or "",
            )
        else:
            existing.username = clean
            if sid:
                existing.sid = sid
            existing.domain = clean_domain
            existing.password = password
            existing.password_blob = ""
            existing.updated_at = now
            profile = existing

        users[key] = profile
        self._save_all(users)
        return profile

    def delete_look(self, username: str, look_id: str) -> UserProfile:
        """Remove one enrollment pass (look) and its embeddings from a profile.

        Raises ``KeyError`` if the user or look does not exist and ``ValueError`` when
        asked to remove the last remaining look (delete the whole profile instead).
        """
        clean = username.strip()
        users = self._load_all()
        key = clean.lower()
        profile = users.get(key)
        if profile is None:
            raise KeyError("not_found")
        looks = normalize_looks(profile.looks, len(profile.embeddings), profile.created_at)
        start = 0
        for idx, look in enumerate(looks):
            if look["id"] == look_id:
                break
            start += look["count"]
        else:
            raise KeyError("not_found")
        if len(looks) <= 1:
            raise ValueError("last_look")
        count = looks[idx]["count"]
        del profile.embeddings[start:start + count]
        del looks[idx]
        profile.looks = looks
        profile.updated_at = _utc_now_iso()
        users[key] = profile
        self._save_all(users)
        return profile

    def rename_look(self, username: str, look_id: str, label: str) -> UserProfile:
        """Assign a custom display label (1..32 chars) to one look on a profile.

        Raises ``ValueError("invalid_label")`` if ``label`` is empty after stripping
        or longer than 32 characters, and ``KeyError("not_found")`` if the user or
        look does not exist.
        """
        if not isinstance(label, str):
            raise ValueError("invalid_label")
        clean_label = label.strip()
        if not clean_label or len(clean_label) > 32:
            raise ValueError("invalid_label")
        clean = username.strip()
        users = self._load_all()
        key = clean.lower()
        profile = users.get(key)
        if profile is None:
            raise KeyError("not_found")
        looks = normalize_looks(profile.looks, len(profile.embeddings), profile.created_at)
        for look in looks:
            if look["id"] == look_id:
                look["label"] = clean_label
                break
        else:
            raise KeyError("not_found")
        profile.looks = looks
        profile.updated_at = _utc_now_iso()
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
