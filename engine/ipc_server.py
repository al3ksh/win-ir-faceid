"""Windows Named Pipe IPC server and protocol handler for WinIRFaceID."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import ntsecuritycon
import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security
import winerror

from engine import __version__
from engine.lockout import LOCKED_OUT_REASON, BiometricLockout
from engine.liveness import TemporalLivenessTracker, check_eyes_open_ir, check_ir_liveness
from engine.recognizer import DEFAULT_MATCH_THRESHOLD, FaceRecognizer, enhance_ir_bgr
from engine.store import (
    ProfileStore,
    ServiceConfig,
    UserProfile,
    clamp_max_failed_attempts,
    clamp_service_threshold,
    load_service_config,
    save_service_config,
)

logger = logging.getLogger(__name__)

PIPE_NAME = r"\\.\pipe\WinIRFaceID"
MAX_MESSAGE_BYTES = 65536
MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_MS = 8000
DEFAULT_THRESHOLD = DEFAULT_MATCH_THRESHOLD
DEFAULT_REQUIRED_CONSECUTIVE = 2
MAX_EMBEDDINGS_PER_USER = 32
MAX_EMBEDDING_DIM = 2048
MAX_PASSWORD_CHARS = 256

FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008

# Rights granted to ordinary interactive users on the pipe: read/write data, but NOT
# FILE_CREATE_PIPE_INSTANCE (== FILE_APPEND_DATA), so they cannot squat server instances.
PIPE_CLIENT_ACCESS = ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_WRITE_DATA

_SID_SYSTEM_STR = "S-1-5-18"


def _current_process_user_sid() -> Any:
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        win32api.CloseHandle(token)


def build_pipe_security_attributes(service_mode: bool = False) -> pywintypes.SECURITY_ATTRIBUTES:
    """Build the Named Pipe DACL.

    LocalSystem and Administrators get full control. Authenticated users may connect and
    exchange messages (the server authorizes each command against the caller's identity),
    but are never granted ``FILE_CREATE_PIPE_INSTANCE``. In developer (non-service) mode the
    current user additionally gets full control.
    """
    sd = win32security.SECURITY_DESCRIPTOR()
    acl = win32security.ACL()

    sid_system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    sid_admins = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
    sid_auth_users = win32security.CreateWellKnownSid(win32security.WinAuthenticatedUserSid, None)

    acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid_system)
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid_admins)
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, PIPE_CLIENT_ACCESS, sid_auth_users)

    if not service_mode:
        sid_user = _current_process_user_sid()
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid_user)

    sd.SetSecurityDescriptorDacl(1, acl, 0)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = False
    return sa


def _select_camera(cameras: list[Any]) -> Any | None:
    """Pick the best IR camera descriptor (real IR first, then any IR; never RGB)."""
    if not cameras:
        return None
    for cam in cameras:
        if getattr(cam, "is_ir", False) and not getattr(cam, "is_virtual", False):
            return cam
    for cam in cameras:
        if getattr(cam, "is_ir", False):
            return cam
    return None


class IRFaceVerifier:
    """Executes an active-IR liveness + SFace verification session."""

    def __init__(
        self,
        store: ProfileStore,
        *,
        recognizer: FaceRecognizer | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        required_consecutive: int = DEFAULT_REQUIRED_CONSECUTIVE,
        min_liveness_delta: float = 12.0,
        min_liveness_ratio: float = 1.30,
        require_motion: bool = False,
        require_open_eyes: bool = False,
        min_eye_open_score: float = 0.28,
        camera_discoverer: Callable[[], list[Any]] | None = None,
        worker_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.store = store
        self._recognizer = recognizer
        self.threshold = threshold
        self.required_consecutive = max(1, int(required_consecutive))
        self.min_liveness_delta = min_liveness_delta
        self.min_liveness_ratio = min_liveness_ratio
        self.require_motion = bool(require_motion)
        self.require_open_eyes = bool(require_open_eyes)
        self.min_eye_open_score = float(min_eye_open_score)
        self._camera_discoverer = camera_discoverer
        self._worker_factory = worker_factory

    def _get_recognizer(self) -> FaceRecognizer:
        if self._recognizer is None:
            self._recognizer = FaceRecognizer()
        return self._recognizer

    def _get_candidates(self, username: str) -> list[UserProfile]:
        clean = username.strip()
        if clean:
            user = self.store.get_user(clean)
            return [user] if (user is not None and user.embeddings) else []
        return [u for u in self.store.list_users() if u.embeddings]

    def _create_worker(self) -> tuple[Any, bool]:
        if self._camera_discoverer is not None:
            cameras = self._camera_discoverer()
        else:
            from engine.ir_camera import discover_cameras

            cameras = discover_cameras()

        cam = _select_camera(cameras)
        if cam is None:
            raise RuntimeError("No camera available")

        is_ir = bool(getattr(cam, "is_ir", False))
        if self._worker_factory is not None:
            worker = self._worker_factory(
                group=cam.group,
                source_id=cam.source_id,
                is_ir=is_ir,
                ir_led_enabled=is_ir,
            )
        else:
            from engine.ir_camera import WinRTCameraWorker

            worker = WinRTCameraWorker(
                group=cam.group,
                source_id=cam.source_id,
                is_ir=is_ir,
                ir_led_enabled=is_ir,
            )
        return worker, is_ir

    def verify(
        self,
        username: str = "",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Run active IR verification and return a protocol response dictionary."""
        candidates = self._get_candidates(username)
        if not candidates:
            return {"status": "failed", "reason": "no_enrolled_faces"}

        try:
            recognizer = self._get_recognizer()
            worker, is_ir = self._create_worker()
        except Exception as exc:
            logger.error("Failed to initialize recognizer or camera: %s", exc)
            return {"status": "failed", "reason": "camera_error"}

        galleries: list[tuple[UserProfile, list[Any]]] = [
            (profile, profile.embedding_arrays()) for profile in candidates
        ]

        deadline = time.monotonic() + (max(1, int(timeout_ms)) / 1000.0)
        last_timestamp: float | None = None
        consecutive_user: str | None = None
        consecutive_count = 0
        motion_tracker = TemporalLivenessTracker() if self.require_motion else None
        # Set once any face reaches liveness / matching: only such scans count as failed
        # attempts for the lockout, so an empty room never locks anyone out.
        face_detected = False

        try:
            worker.start()

            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    return {"status": "failed", "reason": "cancelled", "face_detected": face_detected}

                worker_err = getattr(worker, "error", None)
                if worker_err:
                    logger.error("Camera worker error: %s", worker_err)
                    return {"status": "failed", "reason": "camera_error"}

                if not is_ir:
                    # Non-IR fallback cameras cannot satisfy active IR liveness.
                    return {"status": "failed", "reason": "camera_error"}

                pair = worker.get_ir_pair()
                if pair is None:
                    time.sleep(0.02)
                    continue

                if last_timestamp is not None and pair.timestamp == last_timestamp:
                    time.sleep(0.02)
                    continue
                last_timestamp = pair.timestamp

                eval_frame = pair.diff_bgr if pair.dark_bgr is not None else pair.bright_bgr
                det = recognizer.detect_largest(eval_frame)
                if det is None:
                    enhanced = enhance_ir_bgr(eval_frame)
                    det = recognizer.detect_largest(enhanced)
                    if det is not None:
                        eval_frame = enhanced
                    elif pair.bright_bgr is not None and eval_frame is not pair.bright_bgr:
                        det = recognizer.detect_largest(pair.bright_bgr)
                        if det is not None:
                            eval_frame = pair.bright_bgr
                        else:
                            enhanced_bright = enhance_ir_bgr(pair.bright_bgr)
                            det = recognizer.detect_largest(enhanced_bright)
                            if det is not None:
                                eval_frame = enhanced_bright

                if det is None:
                    consecutive_user = None
                    consecutive_count = 0
                    continue

                face_detected = True
                liveness = check_ir_liveness(
                    pair,
                    det.bbox,
                    min_delta=self.min_liveness_delta,
                    min_ratio=self.min_liveness_ratio,
                )
                if not liveness.passed:
                    consecutive_user = None
                    consecutive_count = 0
                    continue

                motion_frame = pair.diff_bgr if pair.dark_bgr is not None else pair.bright_bgr
                if motion_tracker is not None:
                    motion_ok, _motion_reason = motion_tracker.update(
                        motion_frame,
                        det,
                        timestamp=pair.timestamp,
                    )
                    if not motion_ok:
                        consecutive_user = None
                        consecutive_count = 0
                        continue

                if self.require_open_eyes:
                    eye_res = check_eyes_open_ir(
                        motion_frame,
                        det,
                        min_open_score=self.min_eye_open_score,
                    )
                    if not eye_res.passed:
                        consecutive_user = None
                        consecutive_count = 0
                        continue

                emb = recognizer.extract_embedding(eval_frame, det)

                best_profile: UserProfile | None = None
                best_score = -1.0
                for profile, gallery in galleries:
                    _, score = recognizer.match_against(emb, gallery, threshold=self.threshold)
                    if score > best_score:
                        best_score = score
                        best_profile = profile

                if best_profile is not None and best_score >= self.threshold:
                    if consecutive_user == best_profile.username:
                        consecutive_count += 1
                    else:
                        consecutive_user = best_profile.username
                        consecutive_count = 1

                    if consecutive_count >= self.required_consecutive:
                        try:
                            password = best_profile.resolve_password()
                        except Exception as exc:
                            logger.error("Failed to unwrap stored password: %s", exc)
                            return {"status": "failed", "reason": "credential_error"}
                        return {
                            "status": "matched",
                            "username": best_profile.username,
                            "domain": best_profile.domain,
                            "password": password,
                            "score": round(float(best_score), 4),
                            "liveness_delta": round(float(liveness.delta), 2),
                        }
                else:
                    consecutive_user = None
                    consecutive_count = 0

            return {"status": "failed", "reason": "timeout", "face_detected": face_detected}
        except Exception as exc:
            logger.exception("Unexpected verification error: %s", exc)
            return {"status": "failed", "reason": "camera_error"}
        finally:
            try:
                worker.stop()
            except Exception as stop_exc:
                logger.warning("Error stopping camera worker: %s", stop_exc)


@dataclass(frozen=True)
class ClientIdentity:
    """Windows identity of a connected pipe client, captured via impersonation."""

    sid: str
    username: str
    domain: str
    is_system: bool = False
    privileged: bool = False
    is_admin_member: bool = False


_SID_ADMINS_STR = "S-1-5-32-544"


def _token_is_admin_member(token: Any) -> bool:
    """True if the token lists BUILTIN\\Administrators (enabled or UAC deny-only)."""
    try:
        groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
    except pywintypes.error:
        return False
    for sid, _attrs in groups:
        try:
            if win32security.ConvertSidToStringSid(sid) == _SID_ADMINS_STR:
                return True
        except pywintypes.error:
            continue
    return False


def identify_pipe_client(pipe_handle: Any, *, service_mode: bool) -> ClientIdentity | None:
    """Impersonate the pipe client briefly to learn who it is. Returns None on failure."""
    try:
        win32security.ImpersonateNamedPipeClient(pipe_handle)
    except Exception:
        return None
    try:
        token = win32security.OpenThreadToken(
            win32api.GetCurrentThread(), win32con.TOKEN_QUERY, True
        )
    except Exception:
        token = None
    finally:
        try:
            win32security.RevertToSelf()
        except Exception:
            logger.critical("RevertToSelf failed; terminating to avoid running as client")
            raise SystemExit(1)
    if token is None:
        return None
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        sid_str = win32security.ConvertSidToStringSid(sid)
        is_admin_member = _token_is_admin_member(token)
    except Exception:
        return None
    finally:
        try:
            win32api.CloseHandle(token)
        except Exception:
            pass

    try:
        username, domain, _type = win32security.LookupAccountSid(None, sid)
    except Exception:
        username, domain = "", ""

    is_system = sid_str == _SID_SYSTEM_STR
    privileged = is_system
    if not service_mode and not privileged:
        try:
            privileged = sid_str == win32security.ConvertSidToStringSid(_current_process_user_sid())
        except Exception:
            privileged = False
    return ClientIdentity(
        sid=sid_str,
        username=str(username or ""),
        domain=str(domain or ""),
        is_system=is_system,
        privileged=privileged,
        is_admin_member=is_admin_member or is_system,
    )


def _fail(reason: str) -> dict[str, Any]:
    return {"status": "failed", "reason": reason}


def _public_profile(profile: UserProfile | None) -> dict[str, Any] | None:
    """Profile view safe to hand to its owner: never includes the password."""
    if profile is None:
        return None
    return {
        "username": profile.username,
        "domain": profile.domain,
        "embeddings": profile.embeddings,
        "has_password": profile.has_password,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
        "looks": [dict(look) for look in profile.looks],
    }


def _validate_embeddings(raw: Any) -> list[list[float]] | None:
    if not isinstance(raw, list) or not (1 <= len(raw) <= MAX_EMBEDDINGS_PER_USER):
        return None
    out: list[list[float]] = []
    dim: int | None = None
    for row in raw:
        if not isinstance(row, list) or not (1 <= len(row) <= MAX_EMBEDDING_DIM):
            return None
        if dim is None:
            dim = len(row)
        elif len(row) != dim:
            return None
        vals: list[float] = []
        for v in row:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            fv = float(v)
            if not math.isfinite(fv):
                return None
            vals.append(fv)
        out.append(vals)
    return out


def _config_to_dict(cfg: ServiceConfig) -> dict[str, Any]:
    return {
        "threshold": float(cfg.threshold),
        "require_motion": bool(cfg.require_motion),
        "require_open_eyes": bool(cfg.require_open_eyes),
        "max_failed_attempts": int(cfg.max_failed_attempts),
    }


_MANAGEMENT_COMMANDS = frozenset(
    {
        "profile_get",
        "profile_enroll",
        "profile_set_password",
        "profile_delete",
        "profile_delete_look",
        "profile_rename_look",
        "config_get",
        "config_set",
    }
)


def _handle_management(
    cmd: str,
    msg: dict[str, Any],
    store: ProfileStore,
    verifier: Any | None,
    client: ClientIdentity,
    config_path: Path | str | None,
    store_lock: threading.Lock,
) -> dict[str, Any]:
    """Self-service commands. Every profile operation is bound to the caller's own account."""
    if cmd == "config_get":
        return {"status": "ok", "config": _config_to_dict(load_service_config(config_path))}

    if cmd == "config_set":
        # Global security settings: only members of BUILTIN\Administrators may change them.
        if not (client.privileged or client.is_admin_member):
            return _fail("forbidden")
        with store_lock:
            cfg = load_service_config(config_path)
            if "threshold" in msg:
                raw = msg["threshold"]
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    return _fail("bad_request")
                cfg.threshold = clamp_service_threshold(float(raw))
            for key in ("require_motion", "require_open_eyes"):
                if key in msg:
                    if not isinstance(msg[key], bool):
                        return _fail("bad_request")
                    setattr(cfg, key, msg[key])
            if "max_failed_attempts" in msg:
                raw_limit = msg["max_failed_attempts"]
                if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
                    return _fail("bad_request")
                cfg.max_failed_attempts = clamp_max_failed_attempts(raw_limit)
            save_service_config(cfg, config_path)
        if verifier is not None:
            for attr in ("threshold", "require_motion", "require_open_eyes"):
                if hasattr(verifier, attr):
                    setattr(verifier, attr, getattr(cfg, attr))
        return {"status": "ok", "config": _config_to_dict(cfg)}

    username = client.username.strip()
    if not username or client.is_system:
        return _fail("forbidden")

    with store_lock:
        existing = store.get_user(username)
        if existing is not None and existing.sid and existing.sid != client.sid:
            return _fail("forbidden")

        if cmd == "profile_get":
            return {"status": "ok", "username": username, "profile": _public_profile(existing)}

        if cmd == "profile_enroll":
            embeddings = _validate_embeddings(msg.get("embeddings"))
            if embeddings is None:
                return _fail("bad_request")
            append = msg.get("append", True)
            if not isinstance(append, bool):
                return _fail("bad_request")
            current = len(existing.embeddings) if (existing is not None and append) else 0
            if current + len(embeddings) > MAX_EMBEDDINGS_PER_USER:
                return _fail("too_many_samples")
            if append and existing is not None and existing.embeddings:
                if len(existing.embeddings[0]) != len(embeddings[0]):
                    return _fail("bad_request")
            profile = store.upsert_embeddings(username, embeddings, append=append, sid=client.sid)
            return {"status": "ok", "username": username, "profile": _public_profile(profile)}

        if cmd == "profile_set_password":
            password = msg.get("password")
            domain = msg.get("domain", ".")
            if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_CHARS:
                return _fail("bad_request")
            if not isinstance(domain, str) or len(domain) > 256:
                return _fail("bad_request")
            rejected = _validate_logon_password(username, domain or ".", password)
            if rejected:
                return _fail(rejected)
            profile = store.set_credentials(username, password, domain=domain or ".", sid=client.sid)
            return {"status": "ok", "username": username, "profile": _public_profile(profile)}

        if cmd == "profile_delete_look":
            look_id = msg.get("look_id")
            if not isinstance(look_id, str) or not look_id or len(look_id) > 64:
                return _fail("bad_request")
            if existing is None:
                return _fail("not_found")
            try:
                profile = store.delete_look(username, look_id)
            except KeyError:
                return _fail("not_found")
            except ValueError:
                return _fail("last_look")
            return {"status": "ok", "username": username, "profile": _public_profile(profile)}

        if cmd == "profile_rename_look":
            look_id = msg.get("look_id")
            label = msg.get("label")
            if (
                not isinstance(look_id, str)
                or not look_id
                or len(look_id) > 64
                or not isinstance(label, str)
            ):
                return _fail("bad_request")
            clean_label = label.strip()
            if not clean_label or len(clean_label) > 32:
                return _fail("bad_request")
            if existing is None:
                return _fail("not_found")
            try:
                profile = store.rename_look(username, look_id, clean_label)
            except KeyError:
                return _fail("not_found")
            except ValueError:
                return _fail("bad_request")
            return {"status": "ok", "username": username, "profile": _public_profile(profile)}

        if cmd == "profile_delete":
            removed = store.delete_user(username)
            return {"status": "ok", "username": username, "deleted": bool(removed)}

    return _fail("bad_request")


# LogonUser results that prove the password is wrong / unusable. Any other failure (for
# example a policy that denies network logons) means "could not verify" and is not
# treated as a bad password, so such machines are not locked out of the feature.
_LOGON_REJECT_REASONS = {
    winerror.ERROR_LOGON_FAILURE: "invalid_password",
    winerror.ERROR_ACCOUNT_LOCKED_OUT: "account_locked",
    winerror.ERROR_PASSWORD_EXPIRED: "password_expired",
    winerror.ERROR_PASSWORD_MUST_CHANGE: "password_expired",
    winerror.ERROR_ACCOUNT_DISABLED: "account_disabled",
}


def _validate_logon_password(username: str, domain: str, password: str) -> str | None:
    """Check *password* with LogonUserW before storing it.

    Returns ``None`` if the credentials are valid (or cannot be checked), otherwise a
    failure reason. A wrong password stored here would only surface at the lock screen.
    """
    try:
        token = win32security.LogonUser(
            username,
            domain or ".",
            password,
            win32con.LOGON32_LOGON_NETWORK,
            win32con.LOGON32_PROVIDER_DEFAULT,
        )
    except pywintypes.error as exc:
        reason = _LOGON_REJECT_REASONS.get(exc.winerror)
        if reason is None:
            logger.warning("Could not verify password for %s (error %s); storing anyway", username, exc.winerror)
        return reason
    try:
        token.Close()
    except Exception:
        pass
    return None


_DEFAULT_STORE_LOCK = threading.Lock()


def _apply_lockout(
    result: Any,
    lockout: BiometricLockout | None,
    max_attempts: int,
) -> Any:
    """Update the failed-scan counter from a verify result and strip internal fields."""
    if not isinstance(result, dict):
        return result
    face_detected = bool(result.pop("face_detected", False))
    if lockout is None or max_attempts <= 0:
        return result
    status = result.get("status")
    reason = result.get("reason")
    if status == "matched":
        lockout.record_success()
        return result
    if status == "failed" and face_detected and reason in ("timeout", "cancelled"):
        locked = lockout.record_failure(max_attempts)
        logger.info(
            "Face scan failed (%s, face seen); failed scans %d/%d.",
            reason,
            lockout.status()["failed_attempts"],
            max_attempts,
        )
        if locked:
            # The scan that hits the limit already reports the lockout to the tile.
            return _fail(LOCKED_OUT_REASON)
    elif status == "failed" and reason in ("timeout", "cancelled"):
        logger.info("Face scan failed (%s, no face seen); not counted toward lockout.", reason)
    elif status == "failed":
        logger.info("Face scan failed (%s); not counted toward lockout.", reason)
    return result


def handle_request(
    msg: dict[str, Any],
    store: ProfileStore,
    verifier: Any | None = None,
    cancel_event: threading.Event | None = None,
    *,
    client: ClientIdentity | None = None,
    config_path: Path | str | None = None,
    store_lock: threading.Lock | None = None,
    lockout: BiometricLockout | None = None,
) -> dict[str, Any]:
    """Handle one parsed IPC request dictionary and return a response dictionary.

    ``client`` is the authenticated pipe caller. ``None`` means a trusted in-process
    caller (unit tests, CLI); such callers are treated as privileged but cannot use the
    per-user management commands, which need a Windows identity to bind to.
    """
    if not isinstance(msg, dict):
        return _fail("bad_request")

    cmd = msg.get("cmd")
    if not isinstance(cmd, str):
        return _fail("bad_request")

    privileged = client is None or client.privileged

    if cmd == "ping":
        response: dict[str, Any] = {"status": "ok", "version": __version__}
        if privileged:
            response["enrolled_users"] = [u.username for u in store.list_users() if u.embeddings]
        return response

    if cmd == "cancel":
        if not privileged:
            return _fail("forbidden")
        if cancel_event is not None:
            cancel_event.set()
        return {"status": "ok"}

    if cmd == "verify":
        # Only LocalSystem (the credential provider inside LogonUI) may receive passwords.
        if not privileged:
            return _fail("forbidden")
        username = msg.get("username", "")
        if username is None:
            username = ""
        if not isinstance(username, str):
            return _fail("bad_request")

        raw_timeout = msg.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            return _fail("bad_request")
        timeout_ms = int(raw_timeout)
        if timeout_ms <= 0:
            return _fail("bad_request")
        timeout_ms = min(timeout_ms, 60000)

        max_attempts = 0
        if lockout is not None:
            max_attempts = load_service_config(config_path).max_failed_attempts
            if max_attempts > 0 and lockout.is_locked():
                return _fail(LOCKED_OUT_REASON)

        active_verifier = verifier if verifier is not None else IRFaceVerifier(store)
        if hasattr(active_verifier, "verify"):
            result = active_verifier.verify(
                username=username,
                timeout_ms=timeout_ms,
                cancel_event=cancel_event,
            )
        elif callable(active_verifier):
            result = active_verifier(
                username=username,
                timeout_ms=timeout_ms,
                cancel_event=cancel_event,
            )
        else:
            return _fail("camera_error")
        return _apply_lockout(result, lockout, max_attempts)

    if cmd in _MANAGEMENT_COMMANDS:
        if client is None:
            return _fail("forbidden")
        try:
            return _handle_management(
                cmd, msg, store, verifier, client, config_path, store_lock or _DEFAULT_STORE_LOCK
            )
        except Exception as exc:
            logger.exception("Management command '%s' failed: %s", cmd, exc)
            return _fail("internal_error")

    return _fail("bad_request")


def handle_raw_message(
    raw: str | bytes,
    store: ProfileStore,
    verifier: Any | None = None,
    cancel_event: threading.Event | None = None,
    *,
    client: ClientIdentity | None = None,
    config_path: Path | str | None = None,
) -> dict[str, Any]:
    """Parse a raw UTF-8 JSON line and dispatch it through ``handle_request``."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _fail("bad_request")
    if not isinstance(parsed, dict):
        return _fail("bad_request")
    return handle_request(
        parsed, store=store, verifier=verifier, cancel_event=cancel_event,
        client=client, config_path=config_path,
    )


class NamedPipeServer:
    """Multi-client Windows Named Pipe IPC server on ``\\\\.\\pipe\\WinIRFaceID``."""

    def __init__(
        self,
        pipe_name: str = PIPE_NAME,
        *,
        store: ProfileStore | None = None,
        verifier: Any | None = None,
        service_mode: bool = False,
        config_path: Path | str | None = None,
        lockout: BiometricLockout | None = None,
    ) -> None:
        self.pipe_name = pipe_name
        self.lockout = lockout
        self.store = store if store is not None else ProfileStore(local_machine=service_mode)
        self.verifier = verifier if verifier is not None else IRFaceVerifier(self.store)
        self.service_mode = service_mode
        self.config_path = config_path

        self._stop_event = threading.Event()
        self._win_stop_event = win32event.CreateEvent(None, True, False, None)
        self._verify_lock = threading.Lock()
        self._store_lock = threading.Lock()
        self._active_cancel_event = threading.Event()
        self._sa = build_pipe_security_attributes(service_mode=service_mode)
        self._first_instance = True

    def stop(self) -> None:
        """Signal the server accept loop and any active verification to stop."""
        self._stop_event.set()
        self._active_cancel_event.set()
        win32event.SetEvent(self._win_stop_event)

    def close(self) -> None:
        """Release Win32 event handles."""
        self.stop()
        if self._win_stop_event:
            win32api.CloseHandle(self._win_stop_event)
            self._win_stop_event = None

    def _create_pipe_instance(self) -> Any:
        open_mode = win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED
        if self._first_instance:
            # Fail loudly if another process already owns this pipe name (squatting).
            open_mode |= FILE_FLAG_FIRST_PIPE_INSTANCE
        handle = win32pipe.CreateNamedPipe(
            self.pipe_name,
            open_mode,
            win32pipe.PIPE_TYPE_BYTE
            | win32pipe.PIPE_READMODE_BYTE
            | win32pipe.PIPE_WAIT
            | PIPE_REJECT_REMOTE_CLIENTS,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            MAX_MESSAGE_BYTES,
            MAX_MESSAGE_BYTES,
            0,
            self._sa,
        )
        self._first_instance = False
        return handle

    def _overlapped_read_line(self, pipe_handle: Any) -> bytes | None:
        io_event = win32event.CreateEvent(None, True, False, None)
        read_buf = win32file.AllocateReadBuffer(4096)
        buf = bytearray()
        try:
            while len(buf) < MAX_REQUEST_BYTES:
                win32event.ResetEvent(io_event)
                ov = pywintypes.OVERLAPPED()
                ov.hEvent = io_event
                try:
                    hr, _ = win32file.ReadFile(pipe_handle, read_buf, ov)
                except pywintypes.error as exc:
                    if exc.winerror in (
                        winerror.ERROR_BROKEN_PIPE,
                        winerror.ERROR_PIPE_NOT_CONNECTED,
                    ):
                        break
                    raise

                if hr == winerror.ERROR_IO_PENDING:
                    wait_rc = win32event.WaitForMultipleObjects(
                        [io_event, self._win_stop_event],
                        False,
                        win32event.INFINITE,
                    )
                    if wait_rc != win32event.WAIT_OBJECT_0:
                        win32file.CancelIo(pipe_handle)
                        return None
                try:
                    n_read = win32file.GetOverlappedResult(pipe_handle, ov, True)
                except pywintypes.error as exc:
                    if exc.winerror in (
                        winerror.ERROR_BROKEN_PIPE,
                        winerror.ERROR_PIPE_NOT_CONNECTED,
                    ):
                        break
                    raise

                if n_read <= 0:
                    break
                chunk = bytes(read_buf[:n_read])
                buf.extend(chunk)
                if b"\n" in chunk:
                    break
            return bytes(buf)
        finally:
            win32api.CloseHandle(io_event)

    def _overlapped_write_all(self, pipe_handle: Any, data: bytes) -> None:
        io_event = win32event.CreateEvent(None, True, False, None)
        try:
            ov = pywintypes.OVERLAPPED()
            ov.hEvent = io_event
            hr, _ = win32file.WriteFile(pipe_handle, data, ov)
            if hr == winerror.ERROR_IO_PENDING:
                wait_rc = win32event.WaitForMultipleObjects(
                    [io_event, self._win_stop_event],
                    False,
                    win32event.INFINITE,
                )
                if wait_rc != win32event.WAIT_OBJECT_0:
                    win32file.CancelIo(pipe_handle)
                    return
            win32file.GetOverlappedResult(pipe_handle, ov, True)
            win32file.FlushFileBuffers(pipe_handle)
        finally:
            win32api.CloseHandle(io_event)

    def _identify(self, pipe_handle: Any) -> ClientIdentity:
        try:
            ident = identify_pipe_client(pipe_handle, service_mode=self.service_mode)
        except SystemExit:
            raise
        except Exception:
            ident = None
        if ident is None:
            # Unknown caller: least privilege, no identity to bind management commands to.
            return ClientIdentity(sid="", username="", domain="")
        return ident

    def _handle_client(self, pipe_handle: Any) -> None:
        try:
            raw_data = self._overlapped_read_line(pipe_handle)
            if raw_data is None:
                return

            line = raw_data.split(b"\n", 1)[0].strip()
            if not line:
                response = _fail("bad_request")
            else:
                try:
                    parsed = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = None

                if not isinstance(parsed, dict):
                    response = _fail("bad_request")
                else:
                    client = self._identify(pipe_handle)
                    kwargs: dict[str, Any] = dict(
                        store=self.store,
                        verifier=self.verifier,
                        cancel_event=self._active_cancel_event,
                        client=client,
                        config_path=self.config_path,
                        store_lock=self._store_lock,
                        lockout=self.lockout,
                    )
                    if parsed.get("cmd") == "verify" and client.privileged:
                        with self._verify_lock:
                            self._active_cancel_event.clear()
                            response = handle_request(parsed, **kwargs)
                    else:
                        response = handle_request(parsed, **kwargs)

            payload = (json.dumps(response, ensure_ascii=True) + "\n").encode("utf-8")
            self._overlapped_write_all(pipe_handle, payload)
        except pywintypes.error as exc:
            if getattr(exc, "winerror", None) in (109, 232):
                logger.debug("Pipe client disconnected cleanly (winerror=%s)", exc.winerror)
            else:
                logger.exception("Pipe client error: %s", exc)
        except Exception as exc:
            logger.exception("Pipe client error: %s", exc)
        finally:
            try:
                win32pipe.DisconnectNamedPipe(pipe_handle)
            except Exception:
                pass
            try:
                win32file.CloseHandle(pipe_handle)
            except Exception:
                pass

    def _wait_for_connection(self, pipe_handle: Any) -> bool | None:
        """Wait for a client on ``pipe_handle``. True=connected, False=retry, None=stopping."""
        connect_event = win32event.CreateEvent(None, True, False, None)
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = connect_event
        try:
            try:
                rc = win32pipe.ConnectNamedPipe(pipe_handle, overlapped)
            except pywintypes.error as exc:
                rc = exc.winerror

            if rc == 0 or rc == winerror.ERROR_PIPE_CONNECTED:
                return True
            if rc == winerror.ERROR_IO_PENDING:
                wait_rc = win32event.WaitForMultipleObjects(
                    [connect_event, self._win_stop_event],
                    False,
                    win32event.INFINITE,
                )
                if wait_rc == win32event.WAIT_OBJECT_0:
                    try:
                        win32pipe.GetOverlappedResult(pipe_handle, overlapped, True)
                    except pywintypes.error:
                        return False
                    return True
                win32file.CancelIo(pipe_handle)
                return None
            if rc == winerror.ERROR_NO_DATA:
                return False
            raise pywintypes.error(rc, "ConnectNamedPipe", "Failed to accept pipe client")
        finally:
            win32api.CloseHandle(connect_event)

    def serve_forever(self) -> None:
        """Accept Named Pipe connections until ``stop()`` is called.

        A listening instance always exists: the next instance is created *before* the
        connected one is handed to its worker thread, so there is no window in which
        another process could create a rogue instance under our pipe name.
        """
        logger.info("WinIRFaceID IPC server listening on %s", self.pipe_name)
        pipe_handle = self._create_pipe_instance()
        try:
            while not self._stop_event.is_set():
                state = self._wait_for_connection(pipe_handle)
                if state is None:
                    break
                if state is False:
                    try:
                        win32pipe.DisconnectNamedPipe(pipe_handle)
                    except pywintypes.error:
                        pass
                    continue

                connected_handle = pipe_handle
                try:
                    pipe_handle = self._create_pipe_instance()
                except Exception:
                    pipe_handle = None
                    self._handle_client(connected_handle)
                    raise
                threading.Thread(
                    target=self._handle_client,
                    args=(connected_handle,),
                    daemon=True,
                ).start()
        finally:
            if pipe_handle is not None:
                try:
                    win32file.CloseHandle(pipe_handle)
                except Exception:
                    pass
