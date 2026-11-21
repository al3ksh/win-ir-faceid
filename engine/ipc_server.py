"""Windows Named Pipe IPC server and protocol handler for WinIRFaceID."""

from __future__ import annotations

import json
import logging
import threading
import time
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
from engine.liveness import TemporalLivenessTracker, check_eyes_open_ir, check_ir_liveness
from engine.recognizer import DEFAULT_MATCH_THRESHOLD, FaceRecognizer, enhance_ir_bgr
from engine.store import ProfileStore, UserProfile

logger = logging.getLogger(__name__)

PIPE_NAME = r"\\.\pipe\WinIRFaceID"
MAX_MESSAGE_BYTES = 65536
DEFAULT_TIMEOUT_MS = 8000
DEFAULT_THRESHOLD = DEFAULT_MATCH_THRESHOLD
DEFAULT_REQUIRED_CONSECUTIVE = 2


def build_pipe_security_attributes(service_mode: bool = False) -> pywintypes.SECURITY_ATTRIBUTES:
    """Build a Named Pipe DACL permitting LocalSystem, Administrators, and current user."""
    sd = win32security.SECURITY_DESCRIPTOR()
    acl = win32security.ACL()

    sid_system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    sid_admins = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)

    acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid_system)
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid_admins)

    if not service_mode:
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32con.TOKEN_QUERY,
        )
        try:
            sid_user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            win32api.CloseHandle(token)
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

        try:
            worker.start()

            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    return {"status": "failed", "reason": "cancelled"}

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
                        return {
                            "status": "matched",
                            "username": best_profile.username,
                            "domain": best_profile.domain,
                            "password": best_profile.password,
                            "score": round(float(best_score), 4),
                            "liveness_delta": round(float(liveness.delta), 2),
                        }
                else:
                    consecutive_user = None
                    consecutive_count = 0

            return {"status": "failed", "reason": "timeout"}
        except Exception as exc:
            logger.exception("Unexpected verification error: %s", exc)
            return {"status": "failed", "reason": "camera_error"}
        finally:
            try:
                worker.stop()
            except Exception as stop_exc:
                logger.warning("Error stopping camera worker: %s", stop_exc)


def handle_request(
    msg: dict[str, Any],
    store: ProfileStore,
    verifier: Any | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Handle one parsed IPC request dictionary and return a response dictionary."""
    if not isinstance(msg, dict):
        return {"status": "failed", "reason": "bad_request"}

    cmd = msg.get("cmd")
    if not isinstance(cmd, str):
        return {"status": "failed", "reason": "bad_request"}

    if cmd == "ping":
        enrolled = [u.username for u in store.list_users() if u.embeddings]
        return {
            "status": "ok",
            "version": __version__,
            "enrolled_users": enrolled,
        }

    if cmd == "cancel":
        if cancel_event is not None:
            cancel_event.set()
        return {"status": "ok"}

    if cmd == "verify":
        username = msg.get("username", "")
        if username is None:
            username = ""
        if not isinstance(username, str):
            return {"status": "failed", "reason": "bad_request"}

        raw_timeout = msg.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            return {"status": "failed", "reason": "bad_request"}
        timeout_ms = int(raw_timeout)
        if timeout_ms <= 0:
            return {"status": "failed", "reason": "bad_request"}

        active_verifier = verifier if verifier is not None else IRFaceVerifier(store)
        if hasattr(active_verifier, "verify"):
            return active_verifier.verify(
                username=username,
                timeout_ms=timeout_ms,
                cancel_event=cancel_event,
            )
        if callable(active_verifier):
            return active_verifier(
                username=username,
                timeout_ms=timeout_ms,
                cancel_event=cancel_event,
            )
        return {"status": "failed", "reason": "camera_error"}

    return {"status": "failed", "reason": "bad_request"}


def handle_raw_message(
    raw: str | bytes,
    store: ProfileStore,
    verifier: Any | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Parse a raw UTF-8 JSON line and dispatch it through ``handle_request``."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "failed", "reason": "bad_request"}
    if not isinstance(parsed, dict):
        return {"status": "failed", "reason": "bad_request"}
    return handle_request(parsed, store=store, verifier=verifier, cancel_event=cancel_event)


class NamedPipeServer:
    """Multi-client Windows Named Pipe IPC server on ``\\\\.\\pipe\\WinIRFaceID``."""

    def __init__(
        self,
        pipe_name: str = PIPE_NAME,
        *,
        store: ProfileStore | None = None,
        verifier: Any | None = None,
        service_mode: bool = False,
    ) -> None:
        self.pipe_name = pipe_name
        self.store = store if store is not None else ProfileStore(local_machine=service_mode)
        self.verifier = verifier if verifier is not None else IRFaceVerifier(self.store)
        self.service_mode = service_mode

        self._stop_event = threading.Event()
        self._win_stop_event = win32event.CreateEvent(None, True, False, None)
        self._verify_lock = threading.Lock()
        self._active_cancel_event = threading.Event()
        self._sa = build_pipe_security_attributes(service_mode=service_mode)

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
        return win32pipe.CreateNamedPipe(
            self.pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            MAX_MESSAGE_BYTES,
            MAX_MESSAGE_BYTES,
            0,
            self._sa,
        )

    def _overlapped_read_line(self, pipe_handle: Any) -> bytes | None:
        io_event = win32event.CreateEvent(None, True, False, None)
        read_buf = win32file.AllocateReadBuffer(4096)
        buf = bytearray()
        try:
            while len(buf) < MAX_MESSAGE_BYTES:
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

    def _handle_client(self, pipe_handle: Any) -> None:
        try:
            raw_data = self._overlapped_read_line(pipe_handle)
            if raw_data is None:
                return

            line = raw_data.split(b"\n", 1)[0].strip()
            if not line:
                response = {"status": "failed", "reason": "bad_request"}
            else:
                try:
                    parsed = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = None

                if not isinstance(parsed, dict):
                    response = {"status": "failed", "reason": "bad_request"}
                elif parsed.get("cmd") == "verify":
                    with self._verify_lock:
                        self._active_cancel_event.clear()
                        response = handle_request(
                            parsed,
                            store=self.store,
                            verifier=self.verifier,
                            cancel_event=self._active_cancel_event,
                        )
                else:
                    response = handle_request(
                        parsed,
                        store=self.store,
                        verifier=self.verifier,
                        cancel_event=self._active_cancel_event,
                    )

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
            except pywintypes.error:
                pass
            try:
                win32file.CloseHandle(pipe_handle)
            except pywintypes.error:
                pass

    def serve_forever(self) -> None:
        """Accept Named Pipe connections until ``stop()`` is called."""
        logger.info("WinIRFaceID IPC server listening on %s", self.pipe_name)
        while not self._stop_event.is_set():
            pipe_handle = self._create_pipe_instance()
            connect_event = win32event.CreateEvent(None, True, False, None)
            overlapped = pywintypes.OVERLAPPED()
            overlapped.hEvent = connect_event
            connected = False

            try:
                try:
                    rc = win32pipe.ConnectNamedPipe(pipe_handle, overlapped)
                except pywintypes.error as exc:
                    rc = exc.winerror

                if rc == 0 or rc == winerror.ERROR_PIPE_CONNECTED:
                    connected = True
                elif rc == winerror.ERROR_IO_PENDING:
                    wait_rc = win32event.WaitForMultipleObjects(
                        [connect_event, self._win_stop_event],
                        False,
                        win32event.INFINITE,
                    )
                    if wait_rc == win32event.WAIT_OBJECT_0:
                        win32pipe.GetOverlappedResult(pipe_handle, overlapped, True)
                        connected = True
                    else:
                        win32file.CancelIo(pipe_handle)
                        win32file.CloseHandle(pipe_handle)
                        break
                else:
                    raise pywintypes.error(rc, "ConnectNamedPipe", "Failed to accept pipe client")
            finally:
                win32api.CloseHandle(connect_event)

            if not connected:
                win32file.CloseHandle(pipe_handle)
                continue

            thread = threading.Thread(
                target=self._handle_client,
                args=(pipe_handle,),
                daemon=True,
            )
            thread.start()
