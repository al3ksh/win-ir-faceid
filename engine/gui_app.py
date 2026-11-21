"""Minimalist Edge WebView2 desktop GUI controller and loopback MJPEG server."""

from __future__ import annotations

import ctypes
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from engine import REPO_URL, RELEASES_API_URL, __version__

from engine.liveness import (
    EyeOpenResult,
    LivenessResult,
    TemporalLivenessTracker,
    check_eyes_open_ir,
    check_ir_liveness,
)
from engine.models import ensure_models, models_exist
from engine.recognizer import (
    DEFAULT_MATCH_THRESHOLD,
    FaceDetection,
    FaceRecognizer,
    enhance_ir_bgr,
)
from engine.store import (
    ProfileStore,
    ServiceConfig,
    UserProfile,
    load_service_config,
    save_service_config,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "WinIRFaceIDService"

_POSE_PROMPTS = (
    "Look straight at the camera",
    "Hold steady looking straight",
    "Turn your head slightly left",
    "Turn your head slightly right",
    "Tilt your head slightly up",
    "Tilt your head slightly down",
    "Hold a natural expression",
    "Look straight for final capture",
)

_ENROLL_TARGET = 8
_MAX_EMBEDDINGS_PER_USER = 32
_ENROLL_MIN_INTERVAL = 0.45
_JPEG_QUALITY = 88

_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_CAPTION_COLOR = 35
_COLORREF_CANVAS = 0x000E0C0B  # #0b0c0e in 0x00BBGGRR

_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_WM_SETICON = 0x0080
_ICON_SMALL = 0
_ICON_BIG = 1
_SM_CXICON = 11
_SM_CYICON = 12
_SM_CXSMICON = 49
_SM_CYSMICON = 50
_APP_USER_MODEL_ID = "WinIRFaceID.Desktop.App"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


_SEMVER_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def parse_semver(version_str: str) -> tuple[int, int, int] | None:
    """Parse a semantic version string (e.g. 'v0.1.0', '1.0.0-rc1') into (major, minor, patch)."""
    if not isinstance(version_str, str):
        return None
    cleaned = version_str.strip()
    if not cleaned:
        return None
    match = _SEMVER_RE.fullmatch(cleaned)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer_version(latest: str, current: str) -> bool:
    """Return True if *latest* represents a strictly newer semantic version than *current*."""
    latest_tuple = parse_semver(latest)
    current_tuple = parse_semver(current)
    if latest_tuple is None or current_tuple is None:
        return False
    return latest_tuple > current_tuple


def check_github_latest_release(
    current_version: str = __version__,
    api_url: str = RELEASES_API_URL,
    timeout: float = 4.0,
) -> dict[str, Any]:
    """Query GitHub Releases API once on explicit user action for a newer release."""
    fallback_url = f"{REPO_URL}/releases"
    req = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"WinIRFaceID/{current_version}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status == 404:
                return {
                    "ok": True,
                    "current_version": current_version,
                    "latest_version": current_version,
                    "update_available": False,
                    "release_url": fallback_url,
                    "no_releases": True,
                    "error": None,
                }
            if status is not None and status >= 400:
                return {
                    "ok": False,
                    "current_version": current_version,
                    "latest_version": "",
                    "update_available": False,
                    "release_url": fallback_url,
                    "error": "Unable to reach GitHub.",
                }
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "ok": True,
                "current_version": current_version,
                "latest_version": current_version,
                "update_available": False,
                "release_url": fallback_url,
                "no_releases": True,
                "error": None,
            }
        return {
            "ok": False,
            "current_version": current_version,
            "latest_version": "",
            "update_available": False,
            "release_url": fallback_url,
            "error": "Unable to reach GitHub.",
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError):
        return {
            "ok": False,
            "current_version": current_version,
            "latest_version": "",
            "update_available": False,
            "release_url": fallback_url,
            "error": "Unable to reach GitHub.",
        }

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "current_version": current_version,
            "latest_version": "",
            "update_available": False,
            "release_url": fallback_url,
            "error": "Unable to reach GitHub.",
        }

    raw_tag = str(payload.get("tag_name") or "").strip()
    latest_clean = raw_tag[1:] if raw_tag.startswith(("v", "V")) else raw_tag
    raw_html_url = str(payload.get("html_url") or "").strip()
    safe_url = (
        raw_html_url
        if (raw_html_url == REPO_URL or raw_html_url.startswith(f"{REPO_URL}/"))
        else fallback_url
    )
    update_available = is_newer_version(latest_clean, current_version)
    return {
        "ok": True,
        "current_version": current_version,
        "latest_version": latest_clean,
        "update_available": update_available,
        "release_url": safe_url,
        "error": None,
    }


_ERROR_CLASSIFIERS: tuple[tuple[str, tuple[str, ...], str, list[str]], ...] = (
    (
        "no_ir_camera",
        ("no ir camera", "no camera detected", "active ir camera is required"),
        "No infrared camera detected",
        [
            "This device may not have a Windows Hello IR camera.",
            "Check Device Manager -> Cameras and ensure the IR sensor is enabled.",
            "Verify Windows Settings -> Privacy & security -> Camera allows desktop apps.",
        ],
    ),
    (
        "camera_privacy_denied",
        ("0x80070005", "access is denied", "privacy", "unauthorized"),
        "Camera access blocked by Windows",
        [
            "Open Windows Settings -> Privacy & security -> Camera.",
            "Enable 'Camera access' and 'Let desktop apps access your camera'.",
        ],
    ),
    (
        "camera_in_use",
        (
            "0xc00d3704",
            "0xc00dabe0",
            "0x80070020",
            "in use",
            "exclusive",
            "already open",
            "sharing violation",
        ),
        "IR camera is in use",
        [
            "Another app (such as Camera, Zoom, or Windows Hello settings) is holding the sensor.",
            "Close other camera apps and click Retry.",
        ],
    ),
    (
        "illuminator_error",
        ("ioctl", "ksproperty", "illuminator", "faceauth"),
        "IR illuminator did not respond",
        [
            "Check that a physical camera privacy shutter is not closed.",
            "Update your laptop's OEM camera / IR driver if the illuminator LED never pulses.",
        ],
    ),
    (
        "models_error",
        ("model", "onnx", "download"),
        "Face recognition models unavailable",
        [
            "Click Download Models in the Account section to fetch YuNet & SFace (~37 MB).",
            "Verify internet access to GitHub, or place the ONNX files manually in models/.",
        ],
    ),
)


def classify_error(raw_message: str | None) -> dict[str, Any] | None:
    """Map raw hardware, WinRT, or model error text into a structured UI error dict."""
    if raw_message is None:
        return None
    detail = str(raw_message).strip()
    if not detail:
        return None

    low = detail.lower()
    for code, keywords, title, causes in _ERROR_CLASSIFIERS:
        if any(kw in low for kw in keywords):
            return {
                "code": code,
                "title": title,
                "causes": list(causes),
                "detail": detail,
            }

    return {
        "code": "sensor_error",
        "title": "IR sensor error",
        "causes": [
            "Check that the IR camera is connected and not in use by another app.",
            "Try Rescan Cameras or restart the app.",
        ],
        "detail": detail,
    }


def _select_preferred_camera_index(cameras: list[Any]) -> int:
    """Return index of real IR camera first, any IR second, else 0."""
    for idx, cam in enumerate(cameras):
        if getattr(cam, "is_ir", False) and not getattr(cam, "is_virtual", False):
            return idx
    for idx, cam in enumerate(cameras):
        if getattr(cam, "is_ir", False):
            return idx
    return 0


def get_service_status(service_name: str = SERVICE_NAME) -> str:
    """Query Windows SCM read-only for the background service status."""
    try:
        import pywintypes
        import win32service
    except ImportError:
        return "unknown"

    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    except pywintypes.error:
        return "unknown"

    try:
        try:
            svc = win32service.OpenService(scm, service_name, win32service.SERVICE_QUERY_STATUS)
        except pywintypes.error as exc:
            if getattr(exc, "winerror", None) == 1060:
                return "not_installed"
            return "unknown"
        try:
            status = win32service.QueryServiceStatus(svc)
            state = status[1]
            if state in (
                win32service.SERVICE_RUNNING,
                win32service.SERVICE_START_PENDING,
                win32service.SERVICE_CONTINUE_PENDING,
            ):
                return "running"
            return "stopped"
        finally:
            win32service.CloseServiceHandle(svc)
    except pywintypes.error:
        return "unknown"
    finally:
        win32service.CloseServiceHandle(scm)


def restart_service_uac(service_name: str = SERVICE_NAME) -> dict[str, Any]:
    """Trigger an elevated Restart-Service via ShellExecuteW with a single UAC prompt."""
    try:
        cmd = f"-NoProfile -WindowStyle Hidden -Command Restart-Service {service_name}"
        rc = int(
            ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                "powershell.exe",
                cmd,
                None,
                0,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": 0, "error": str(exc)}

    if rc > 32:
        return {"ok": True, "code": rc, "error": None}
    return {
        "ok": False,
        "code": rc,
        "error": "Elevation was cancelled or failed." if rc == 5 else f"ShellExecuteW failed ({rc}).",
    }


def _get_window_hwnd(window: Any) -> int:
    """Extract the native Win32 HWND integer from a pywebview window, or 0 if unavailable."""
    try:
        native = getattr(window, "native", None)
        handle = getattr(native, "Handle", None)
        if handle is None:
            return 0
        if hasattr(handle, "ToInt64"):
            return int(handle.ToInt64())
        if hasattr(handle, "ToInt32"):
            return int(handle.ToInt32())
        return int(handle)
    except Exception:  # noqa: BLE001
        return 0


def _configure_user32_icon_api(user32: Any) -> None:
    """Configure 64-bit-safe ctypes signatures for Win32 icon APIs."""
    user32.LoadImageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_void_p,
    ]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.DestroyIcon.argtypes = [ctypes.c_void_p]
    user32.DestroyIcon.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int


def _load_hicon(icon_path: str | Path, width: int, height: int) -> int | None:
    """Load an .ico file at the requested pixel dimensions via Win32 LoadImageW."""
    path_str = str(icon_path)
    if not path_str:
        return None
    try:
        user32 = ctypes.windll.user32
        _configure_user32_icon_api(user32)
        handle = user32.LoadImageW(
            None,
            path_str,
            _IMAGE_ICON,
            int(width),
            int(height),
            _LR_LOADFROMFILE,
        )
        return int(handle) if handle else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("LoadImageW failed for %s (%dx%d): %s", path_str, width, height, exc)
        return None


def _destroy_hicon(hicon: int | None) -> bool:
    """Destroy a Win32 HICON handle previously loaded via _load_hicon."""
    if not hicon:
        return False
    try:
        user32 = ctypes.windll.user32
        _configure_user32_icon_api(user32)
        return bool(user32.DestroyIcon(ctypes.c_void_p(int(hicon))))
    except Exception as exc:  # noqa: BLE001
        logger.debug("DestroyIcon failed for %s: %s", hicon, exc)
        return False


def _apply_dark_title_bar(window: Any) -> None:
    """Apply native Windows 11 dark title bar and caption color via DwmSetWindowAttribute."""
    try:
        hwnd = _get_window_hwnd(window)
        if not hwnd:
            return

        dwmapi = ctypes.windll.dwmapi
        dark_val = ctypes.c_int(1)
        for attr in (_DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(attr),
                ctypes.byref(dark_val),
                ctypes.sizeof(dark_val),
            )

        caption_color = ctypes.c_uint(_COLORREF_CANVAS)
        dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(_DWMWA_CAPTION_COLOR),
            ctypes.byref(caption_color),
            ctypes.sizeof(caption_color),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Dark title bar setup skipped: %s", exc)


def _apply_window_icon(window: Any, icon_path: str | None) -> list[int]:
    """Apply the app .ico to the Win32 titlebar, Alt+Tab switcher, and taskbar.

    Returns the list of loaded HICON handles so the caller keeps them alive
    until the window closes.
    """
    loaded_handles: list[int] = []
    if not icon_path:
        return loaded_handles

    try:
        hwnd = _get_window_hwnd(window)
        if hwnd:
            user32 = ctypes.windll.user32
            _configure_user32_icon_api(user32)

            sm_cx = user32.GetSystemMetrics(_SM_CXSMICON) or 16
            sm_cy = user32.GetSystemMetrics(_SM_CYSMICON) or 16
            big_cx = user32.GetSystemMetrics(_SM_CXICON) or 32
            big_cy = user32.GetSystemMetrics(_SM_CYICON) or 32

            small_hicon = _load_hicon(icon_path, sm_cx, sm_cy)
            if small_hicon:
                user32.SendMessageW(
                    ctypes.c_void_p(hwnd),
                    _WM_SETICON,
                    _ICON_SMALL,
                    ctypes.c_void_p(small_hicon),
                )
                loaded_handles.append(small_hicon)

            big_hicon = _load_hicon(icon_path, big_cx, big_cy)
            if big_hicon:
                user32.SendMessageW(
                    ctypes.c_void_p(hwnd),
                    _WM_SETICON,
                    _ICON_BIG,
                    ctypes.c_void_p(big_hicon),
                )
                loaded_handles.append(big_hicon)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Win32 WM_SETICON setup skipped: %s", exc)

    # Extra WinForms safeguard when window.native is a .NET Form
    try:
        native = getattr(window, "native", None)
        if native is not None and hasattr(native, "Icon"):
            import System  # type: ignore[import-not-found]
            import System.Drawing  # type: ignore[import-not-found]

            dotnet_icon = System.Drawing.Icon(str(icon_path))

            def _set_form_icon() -> None:
                native.Icon = dotnet_icon

            if getattr(native, "InvokeRequired", False) and hasattr(native, "Invoke"):
                native.Invoke(System.Action(_set_form_icon))
            else:
                _set_form_icon()
    except Exception as exc:  # noqa: BLE001
        logger.debug("WinForms Icon setup skipped: %s", exc)

    return loaded_handles


def _resolve_window_icon_path() -> str | None:
    """Return engine/ui/app.ico, generating it if it does not exist yet."""
    ico_path = Path(__file__).resolve().parent / "ui" / "app.ico"
    if ico_path.is_file():
        return str(ico_path)
    try:
        from engine.ui.generate_app_icon import generate_app_icon

        generated = generate_app_icon(ico_path)
        if generated.is_file():
            return str(generated)
    except Exception as exc:  # noqa: BLE001
        logger.debug("App icon generation skipped: %s", exc)
    return None


class _LocalUIServer(ThreadingHTTPServer):
    """Loopback HTTP server hosting static UI assets and the token-gated MJPEG stream."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, controller: WinIRFaceIDController) -> None:
        self.controller = controller
        super().__init__(("127.0.0.1", 0), _LocalUIHandler)


class _LocalUIHandler(BaseHTTPRequestHandler):
    """HTTP handler for UI assets, /health, and /stream?token=..."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("LocalUIHTTP: " + format, *args)

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", raw)

    def do_GET(self) -> None:  # noqa: N802
        ctrl: WinIRFaceIDController = self.server.controller  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            state = ctrl.get_state()
            self._send_json(
                200,
                {
                    "status": "ok",
                    "mode": state["mode"],
                    "camera_active": state["camera_active"],
                },
            )
            return

        if path == "/stream":
            query = parse_qs(parsed.query)
            token_vals = query.get("token", [])
            token = token_vals[0] if token_vals else ""
            if not ctrl._verify_stream_token(token):
                self._send_json(403, {"error": "forbidden"})
                return

            if not ctrl.is_camera_active():
                self._send_json(503, {"error": "camera_standby"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Connection", "close")
            self.end_headers()

            last_seq = -1
            try:
                while ctrl.is_camera_active() and not ctrl._is_shutting_down():
                    seq, jpeg = ctrl._wait_for_jpeg(last_seq, timeout=0.5)
                    if jpeg is None or seq == last_seq:
                        continue
                    last_seq = seq
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                    )
                    self.wfile.write(header)
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            return

        # Serve static UI files from engine/ui/
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if rel == "favicon.ico":
            rel = "app.ico"
        if "/" in rel or "\\" in rel or ".." in rel:
            self._send_json(404, {"error": "not_found"})
            return

        allowed = {"index.html", "style.css", "app.js", "app.ico"}
        if rel not in allowed:
            self._send_json(404, {"error": "not_found"})
            return

        file_path = ctrl._ui_dir / rel
        if not file_path.is_file():
            self._send_json(404, {"error": "missing_asset"})
            return

        ctype, _ = mimetypes.guess_type(str(file_path))
        if rel.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif rel.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif rel.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        elif rel.endswith(".ico"):
            ctype = "image/x-icon"
        else:
            ctype = ctype or "application/octet-stream"

        self._send_bytes(200, ctype, file_path.read_bytes())


class WinIRFaceIDController:
    """Headless-testable backend controller and JS API for the WinIRFaceID desktop app."""

    def __init__(
        self,
        *,
        store: ProfileStore | None = None,
        config_path: Path | str | None = None,
        models_dir: Path | str | None = None,
        ui_dir: Path | str | None = None,
        camera_discoverer: Callable[[], list[Any]] | None = None,
        worker_factory: Callable[..., Any] | None = None,
        recognizer_factory: Callable[[], FaceRecognizer] | None = None,
        service_status_provider: Callable[[], str] | None = None,
        service_restarter: Callable[[], dict[str, Any]] | None = None,
        url_opener: Callable[[str], Any] | None = None,
        update_checker: Callable[[], dict[str, Any]] | None = None,
        auto_start_server: bool = True,
    ) -> None:
        self._store = store if store is not None else ProfileStore()
        self._config_path = Path(config_path) if config_path is not None else None
        self._models_dir = Path(models_dir) if models_dir is not None else None
        self._ui_dir = Path(ui_dir) if ui_dir is not None else (Path(__file__).resolve().parent / "ui")
        self._camera_discoverer = camera_discoverer
        self._worker_factory = worker_factory
        self._recognizer_factory = recognizer_factory
        self._service_status_provider = service_status_provider or get_service_status
        self._service_restarter = service_restarter or restart_service_uac
        self._url_opener = url_opener or webbrowser.open
        self._update_checker = update_checker or check_github_latest_release

        self._lock = threading.RLock()
        self._frame_cond = threading.Condition()
        self._shutting_down = False

        self._config: ServiceConfig = load_service_config(self._config_path)
        self._config.threshold = round(_clamp(self._config.threshold, 0.50, 0.80), 2)
        self._config_dirty = False

        self._mode: str = "standby"
        self._feed_mode: str = "subtract"
        self._camera_active: bool = False
        self._worker: Any | None = None
        self._cameras: list[Any] = []
        self._selected_camera_idx: int = 0
        self._current_cam_is_ir: bool = False

        self._recognizer: FaceRecognizer | None = None
        self._motion_tracker = TemporalLivenessTracker()

        self._enroll_username: str = ""
        self._enroll_append: bool = False
        self._enroll_embeddings: list[np.ndarray] = []
        self._last_enroll_capture_ts: float = 0.0
        self._last_processed_pair_ts: float | None = None

        self._proc_stop = threading.Event()
        self._proc_thread: threading.Thread | None = None

        self._latest_jpeg: bytes | None = None
        self._jpeg_seq: int = 0

        self._download_state: dict[str, Any] = {
            "active": False,
            "file": "",
            "downloaded": 0,
            "total": None,
            "error": None,
        }

        self._state: dict[str, Any] = self._build_standby_state()
        self._stream_token = secrets.token_urlsafe(16)
        self._server: _LocalUIServer | None = None
        self._server_thread: threading.Thread | None = None

        self._discover_cameras_safe()
        if auto_start_server:
            self._start_http_server()

    def _build_standby_state(
        self,
        *,
        completed_user: str = "",
        completed_append: bool = False,
        error_msg: str | None = None,
    ) -> dict[str, Any]:
        clean_err = str(error_msg).strip() if error_msg else None
        if not clean_err:
            clean_err = None
        return {
            "mode": "standby",
            "camera_active": False,
            "ae_settling": False,
            "bbox_norm": None,
            "liveness": {
                "passed": False,
                "reason": "standby",
                "delta": 0.0,
                "bg_ratio": 0.0,
                "relief_3d": 0.0,
                "eyes_left": None,
                "eyes_right": None,
                "eyes_passed": None,
            },
            "match": {
                "state": "idle",
                "username": "",
                "score": 0.0,
                "detail": "",
            },
            "enroll": {
                "count": 0,
                "target": _ENROLL_TARGET,
                "prompt": "",
                "completed_user": completed_user,
                "completed_append": bool(completed_append),
            },
            "error": clean_err,
            "error_info": classify_error(clean_err),
        }

    def _start_http_server(self) -> None:
        if self._server is not None:
            return
        self._server = _LocalUIServer(self)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="WinIRFaceID-LocalHTTP",
            daemon=True,
        )
        self._server_thread.start()

    @property
    def server_port(self) -> int:
        if self._server is None:
            return 0
        return int(self._server.server_address[1])

    @property
    def ui_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"

    @property
    def stream_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/stream?token={self._stream_token}"

    def _verify_stream_token(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(token, self._stream_token)

    def _is_shutting_down(self) -> bool:
        return self._shutting_down

    def is_camera_active(self) -> bool:
        with self._lock:
            return self._camera_active and self._mode != "standby"

    def _wait_for_jpeg(self, last_seq: int, timeout: float = 0.5) -> tuple[int, bytes | None]:
        with self._frame_cond:
            if self._jpeg_seq == last_seq and self.is_camera_active() and not self._shutting_down:
                self._frame_cond.wait(timeout=timeout)
            return self._jpeg_seq, self._latest_jpeg

    def _publish_jpeg(self, frame: np.ndarray) -> None:
        ok, buf = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), _JPEG_QUALITY],
        )
        if not ok:
            return
        data = buf.tobytes()
        with self._frame_cond:
            self._latest_jpeg = data
            self._jpeg_seq += 1
            self._frame_cond.notify_all()

    def _discover_cameras_safe(self) -> None:
        try:
            if self._camera_discoverer is not None:
                cams = list(self._camera_discoverer())
            else:
                from engine.ir_camera import discover_cameras

                cams = list(discover_cameras())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Camera discovery failed: %s", exc)
            cams = []

        with self._lock:
            self._cameras = cams
            if cams:
                self._selected_camera_idx = _select_preferred_camera_index(cams)
                self._current_cam_is_ir = bool(getattr(cams[self._selected_camera_idx], "is_ir", False))
            else:
                self._selected_camera_idx = 0
                self._current_cam_is_ir = False

    def _serialize_profiles(self) -> list[dict[str, Any]]:
        """Serialize profiles WITHOUT ever exposing plaintext passwords."""
        profiles: list[dict[str, Any]] = []
        for p in self._store.list_users():
            profiles.append(
                {
                    "username": p.username,
                    "domain": p.domain,
                    "sample_count": len(p.embeddings),
                    "has_password": bool(p.password),
                    "created_at": p.created_at,
                    "updated_at": p.updated_at,
                }
            )
        return profiles

    def _serialize_cameras(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "index": idx,
                    "label": str(getattr(cam, "label", f"Camera {idx}")),
                    "is_ir": bool(getattr(cam, "is_ir", False)),
                    "is_virtual": bool(getattr(cam, "is_virtual", False)),
                }
                for idx, cam in enumerate(self._cameras)
            ]

    def _models_ready(self) -> bool:
        return models_exist(self._models_dir)

    def _ensure_recognizer_loaded(self) -> bool:
        with self._lock:
            if self._recognizer is not None:
                return True
        if not self._models_ready():
            return False
        try:
            if self._recognizer_factory is not None:
                rec = self._recognizer_factory()
            else:
                from engine.models import get_model_paths

                paths = get_model_paths(self._models_dir)
                rec = FaceRecognizer(yunet_path=paths.yunet, sface_path=paths.sface)
            with self._lock:
                self._recognizer = rec
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load face recognizer: %s", exc)
            err_msg = f"Model load error: {exc}"
            with self._lock:
                self._state["error"] = err_msg
                self._state["error_info"] = classify_error(err_msg)
            return False

    def _set_standby_error(self, error_msg: str) -> dict[str, Any]:
        """Record a structured hardware or model error on standby state and return failure payload."""
        clean = str(error_msg).strip()
        with self._lock:
            self._state = self._build_standby_state(error_msg=clean)
        return {
            "ok": False,
            "error": clean,
            "error_info": classify_error(clean),
            "state": self.get_state(),
        }

    def _stop_camera_and_loop(
        self,
        *,
        completed_user: str = "",
        completed_append: bool = False,
        error_msg: str | None = None,
    ) -> None:
        self._proc_stop.set()
        proc_thread: threading.Thread | None
        worker: Any | None
        with self._lock:
            proc_thread = self._proc_thread
            self._proc_thread = None

        if proc_thread is not None and proc_thread is not threading.current_thread():
            proc_thread.join(timeout=2.5)

        with self._lock:
            worker = self._worker
            self._worker = None
            self._mode = "standby"
            self._camera_active = False
            self._enroll_embeddings = []
            self._enroll_username = ""
            self._enroll_append = False
            self._last_processed_pair_ts = None
            self._motion_tracker.reset()
            self._state = self._build_standby_state(
                completed_user=completed_user,
                completed_append=completed_append,
                error_msg=error_msg,
            )

        with self._frame_cond:
            self._latest_jpeg = None
            self._frame_cond.notify_all()

        if worker is not None:
            try:
                worker.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error stopping camera worker: %s", exc)

    def _start_camera_worker(self) -> tuple[bool, str | None]:
        with self._lock:
            if not self._cameras:
                self._discover_cameras_safe()
            if not self._cameras:
                return False, "No camera detected on this system."
            idx = max(0, min(self._selected_camera_idx, len(self._cameras) - 1))
            cam = self._cameras[idx]
            is_ir = bool(getattr(cam, "is_ir", False))
            self._current_cam_is_ir = is_ir
            feed_mode = self._feed_mode
            old_worker = self._worker
            self._worker = None

        if old_worker is not None:
            try:
                old_worker.stop()
            except Exception:  # noqa: BLE001
                pass

        try:
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
            worker.anti_flicker_mode = feed_mode
            worker.start()
        except Exception as exc:  # noqa: BLE001
            return False, f"Failed to start camera: {exc}"

        with self._lock:
            self._worker = worker
            self._camera_active = True
            self._last_processed_pair_ts = None
        return True, None

    def _ensure_processing_thread(self) -> None:
        with self._lock:
            if self._proc_thread is not None and self._proc_thread.is_alive():
                return
            self._proc_stop.clear()
            self._proc_thread = threading.Thread(
                target=self._processing_loop,
                name="WinIRFaceID-Loop",
                daemon=True,
            )
            self._proc_thread.start()

    def _processing_loop(self) -> None:
        while not self._proc_stop.is_set() and not self._shutting_down:
            try:
                with self._lock:
                    mode = self._mode
                    worker = self._worker
                    is_ir = self._current_cam_is_ir
                    feed_mode = self._feed_mode
                    rec = self._recognizer
                    cfg = ServiceConfig(
                        threshold=self._config.threshold,
                        require_motion=self._config.require_motion,
                        require_open_eyes=self._config.require_open_eyes,
                    )

                if mode == "standby" or worker is None:
                    break

                worker_err = getattr(worker, "error", None)
                if worker_err:
                    self._stop_camera_and_loop(error_msg=str(worker_err))
                    break

                pair = worker.get_ir_pair() if is_ir else None
                display_frame = worker.get_frame()
                if pair is not None:
                    if feed_mode == "subtract" and pair.dark_bgr is not None:
                        display_frame = pair.diff_bgr
                    else:
                        display_frame = pair.bright_bgr

                if display_frame is None:
                    time.sleep(0.015)
                    continue

                # Publish clean raw video frame without OpenCV drawings
                self._publish_jpeg(display_frame)

                h_img, w_img = display_frame.shape[:2]
                ae_settling = bool(pair is not None and pair.bright_mean > 225.0)

                det: FaceDetection | None = None
                eval_src = display_frame
                if rec is not None:
                    det = rec.detect_largest(eval_src)
                    if det is None:
                        enhanced = enhance_ir_bgr(eval_src)
                        det = rec.detect_largest(enhanced)
                        if det is not None:
                            eval_src = enhanced

                bbox_norm: list[float] | None = None
                if det is not None and w_img > 0 and h_img > 0:
                    bx, by, bw, bh = det.bbox
                    nx = _clamp(bx / float(w_img), 0.0, 1.0)
                    ny = _clamp(by / float(h_img), 0.0, 1.0)
                    nw = _clamp(bw / float(w_img), 0.0, 1.0 - nx)
                    nh = _clamp(bh / float(h_img), 0.0, 1.0 - ny)
                    bbox_norm = [round(nx, 4), round(ny, 4), round(nw, 4), round(nh, 4)]

                liveness: LivenessResult | None = None
                if pair is not None and det is not None:
                    liveness = check_ir_liveness(pair, det.bbox)
                    if liveness.reason == "overexposed_ae_settling":
                        ae_settling = True

                liveness_dict = {
                    "passed": bool(liveness and liveness.passed),
                    "reason": (
                        liveness.reason
                        if liveness is not None
                        else ("no_face" if is_ir else "non_ir_camera")
                    ),
                    "delta": round(float(liveness.delta), 2) if liveness is not None else 0.0,
                    "bg_ratio": round(float(liveness.bg_ratio), 3) if liveness is not None else 0.0,
                    "relief_3d": round(float(liveness.relief_3d), 3) if liveness is not None else 0.0,
                    "eyes_left": None,
                    "eyes_right": None,
                    "eyes_passed": None,
                }

                match_dict = {
                    "state": "idle",
                    "username": "",
                    "score": 0.0,
                    "detail": "",
                }

                is_new_pair = pair is not None and pair.timestamp != self._last_processed_pair_ts

                # Enrollment capture flow
                if mode == "enroll":
                    completed_user = ""
                    completed_append = False
                    enroll_done = False
                    if (
                        is_new_pair
                        and rec is not None
                        and det is not None
                        and liveness is not None
                        and liveness.passed
                    ):
                        now = time.monotonic()
                        if now - self._last_enroll_capture_ts >= _ENROLL_MIN_INTERVAL:
                            emb = rec.extract_embedding(eval_src, det)
                            with self._lock:
                                self._enroll_embeddings.append(emb)
                                self._last_enroll_capture_ts = now
                                captured = len(self._enroll_embeddings)
                                if captured >= _ENROLL_TARGET:
                                    target_user = self._enroll_username
                                    append_mode = self._enroll_append
                                    self._store.upsert_embeddings(
                                        target_user,
                                        self._enroll_embeddings,
                                        append=append_mode,
                                    )
                                    completed_user = target_user
                                    completed_append = append_mode
                                    enroll_done = True
                    if enroll_done:
                        if pair is not None:
                            self._last_processed_pair_ts = pair.timestamp
                        self._stop_camera_and_loop(
                            completed_user=completed_user,
                            completed_append=completed_append,
                        )
                        break

                # Live test flow
                elif mode == "test":
                    if det is None:
                        match_dict = {
                            "state": "no_face",
                            "username": "",
                            "score": 0.0,
                            "detail": "Look at the IR camera",
                        }
                    elif rec is not None:
                        emb = rec.extract_embedding(eval_src, det)
                        best_user = ""
                        best_score = 0.0
                        for profile in self._store.list_users():
                            if not profile.embeddings:
                                continue
                            _, score = rec.match_against(
                                emb,
                                profile.embedding_arrays(),
                                threshold=cfg.threshold,
                            )
                            if score > best_score:
                                best_score = score
                                best_user = profile.username

                        live_ok = is_ir and bool(liveness and liveness.passed)
                        motion_ok = True
                        motion_reason = ""
                        motion_src = (
                            pair.diff_bgr if (pair is not None and pair.dark_bgr is not None) else eval_src
                        )

                        # Always update TemporalLivenessTracker BEFORE open-eyes gating
                        if live_ok and cfg.require_motion:
                            motion_ts = pair.timestamp if pair is not None else None
                            motion_ok, motion_reason = self._motion_tracker.update(
                                motion_src,
                                det,
                                timestamp=motion_ts,
                            )

                        eye_res: EyeOpenResult | None = None
                        if live_ok and cfg.require_open_eyes:
                            eye_res = check_eyes_open_ir(motion_src, det)
                            liveness_dict["eyes_left"] = round(float(eye_res.left_score), 3)
                            liveness_dict["eyes_right"] = round(float(eye_res.right_score), 3)
                            liveness_dict["eyes_passed"] = bool(eye_res.passed)

                        if not live_ok:
                            reason_tag = liveness.reason if liveness is not None else "no_ir"
                            match_dict = {
                                "state": "blocked",
                                "username": best_user,
                                "score": round(float(best_score), 4),
                                "detail": reason_tag,
                            }
                        elif not motion_ok:
                            match_dict = {
                                "state": "waiting",
                                "username": best_user,
                                "score": round(float(best_score), 4),
                                "detail": "waiting_blink_or_motion",
                            }
                        elif eye_res is not None and not eye_res.passed:
                            match_dict = {
                                "state": "waiting",
                                "username": best_user,
                                "score": round(float(best_score), 4),
                                "detail": "eyes_closed",
                            }
                        elif best_user and best_score >= cfg.threshold:
                            match_dict = {
                                "state": "verified",
                                "username": best_user,
                                "score": round(float(best_score), 4),
                                "detail": motion_reason or "ok",
                            }
                        elif best_user:
                            match_dict = {
                                "state": "no_match",
                                "username": best_user,
                                "score": round(float(best_score), 4),
                                "detail": "below_threshold",
                            }
                        else:
                            match_dict = {
                                "state": "no_profiles",
                                "username": "",
                                "score": 0.0,
                                "detail": "no_enrolled_profiles",
                            }

                if pair is not None:
                    self._last_processed_pair_ts = pair.timestamp

                with self._lock:
                    if self._mode == "standby":
                        break
                    count = len(self._enroll_embeddings)
                    prompt = (
                        _POSE_PROMPTS[min(count, len(_POSE_PROMPTS) - 1)]
                        if self._mode == "enroll"
                        else ""
                    )
                    self._state = {
                        "mode": self._mode,
                        "camera_active": True,
                        "ae_settling": ae_settling,
                        "bbox_norm": bbox_norm,
                        "liveness": liveness_dict,
                        "match": match_dict,
                        "enroll": {
                            "count": count,
                            "target": _ENROLL_TARGET,
                            "prompt": prompt,
                            "completed_user": "",
                            "append": self._enroll_append,
                        },
                        "error": None,
                        "error_info": None,
                    }

                time.sleep(0.015)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Processing loop error: %s", exc)
                self._stop_camera_and_loop(error_msg=str(exc))
                break

    # ------------------------------------------------------------------
    # Public JS API methods
    # ------------------------------------------------------------------

    def get_bootstrap(self) -> dict[str, Any]:
        """Return initial UI bootstrap state without ever exposing plaintext passwords."""
        profiles = self._serialize_profiles()
        os_user = os.environ.get("USERNAME", "").strip()
        if os_user:
            active_user = os_user
        elif profiles:
            active_user = profiles[0]["username"]
        else:
            active_user = "User"

        with self._lock:
            config = {
                "threshold": float(self._config.threshold),
                "require_motion": bool(self._config.require_motion),
                "require_open_eyes": bool(self._config.require_open_eyes),
            }
            config_dirty = self._config_dirty
            feed_mode = self._feed_mode
            selected_camera_idx = self._selected_camera_idx
            has_ir_camera = bool(any(getattr(c, "is_ir", False) for c in self._cameras))

        return {
            "version": __version__,
            "repo_url": REPO_URL,
            "stream_url": self.stream_url,
            "profiles": profiles,
            "active_user": active_user,
            "config": config,
            "models_ready": self._models_ready(),
            "service_status": self._service_status_provider(),
            "config_dirty": config_dirty,
            "cameras": self._serialize_cameras(),
            "selected_camera": selected_camera_idx,
            "has_ir_camera": has_ir_camera,
            "feed_mode": feed_mode,
        }

    def get_state(self) -> dict[str, Any]:
        """Return a thread-safe snapshot of the live controller state."""
        with self._lock:
            snap = json.loads(json.dumps(self._state))
            snap["models_ready"] = self._models_ready()
            snap["download"] = dict(self._download_state)
            snap["config_dirty"] = self._config_dirty
            return snap

    def _require_ir_camera(self, action_label: str) -> str | None:
        """Ensure an IR camera is selected; return an error string if unavailable."""
        with self._lock:
            if not self._cameras:
                self._discover_cameras_safe()
            if not self._cameras:
                return "No IR camera detected."
            idx = max(0, min(self._selected_camera_idx, len(self._cameras) - 1))
            if not getattr(self._cameras[idx], "is_ir", False):
                ir_idx = _select_preferred_camera_index(self._cameras)
                if not getattr(self._cameras[ir_idx], "is_ir", False):
                    return f"Active IR camera is required for {action_label}."
                self._selected_camera_idx = ir_idx
        return None

    def start_enroll(self, username: str, append: bool = False) -> dict[str, Any]:
        """Start 8-sample active-IR face enrollment for *username*."""
        clean = str(username or "").strip()
        if not clean:
            return {"ok": False, "error": "Enter a Windows username first.", "state": self.get_state()}

        append_mode = bool(append)
        if append_mode:
            existing = self._store.get_user(clean)
            if existing is not None and len(existing.embeddings) >= _MAX_EMBEDDINGS_PER_USER:
                return {
                    "ok": False,
                    "error": (
                        f"Maximum appearances reached ({_MAX_EMBEDDINGS_PER_USER} samples). "
                        "Remove and re-enroll to start fresh."
                    ),
                    "state": self.get_state(),
                }

        if not self._ensure_recognizer_loaded():
            with self._lock:
                err_msg = self._state.get("error") or "Face models are not downloaded yet."
            return self._set_standby_error(str(err_msg))

        cam_err = self._require_ir_camera("enrollment")
        if cam_err:
            return self._set_standby_error(cam_err)

        self._stop_camera_and_loop()
        ok, err = self._start_camera_worker()
        if not ok:
            return self._set_standby_error(err or "Camera failed to start.")

        with self._lock:
            self._mode = "enroll"
            self._enroll_username = clean
            self._enroll_append = append_mode
            self._enroll_embeddings = []
            self._last_enroll_capture_ts = 0.0
            self._state = {
                **self._build_standby_state(),
                "mode": "enroll",
                "camera_active": True,
                "enroll": {
                    "count": 0,
                    "target": _ENROLL_TARGET,
                    "prompt": _POSE_PROMPTS[0],
                    "completed_user": "",
                    "append": append_mode,
                },
            }

        self._ensure_processing_thread()
        return {"ok": True, "error": None, "state": self.get_state()}

    def cancel_action(self) -> dict[str, Any]:
        """Cancel active enrollment, live test, or preview and return to standby."""
        self._stop_camera_and_loop()
        return {"ok": True, "state": self.get_state()}

    def toggle_live_test(self) -> dict[str, Any]:
        """Toggle live IR verification testing on demand."""
        with self._lock:
            currently_testing = self._mode == "test"

        if currently_testing:
            self._stop_camera_and_loop()
            return {"ok": True, "error": None, "state": self.get_state()}

        if not self._ensure_recognizer_loaded():
            with self._lock:
                err_msg = self._state.get("error") or "Face models are not downloaded yet."
            return self._set_standby_error(str(err_msg))

        cam_err = self._require_ir_camera("live testing")
        if cam_err:
            return self._set_standby_error(cam_err)

        self._stop_camera_and_loop()
        ok, err = self._start_camera_worker()
        if not ok:
            return self._set_standby_error(err or "Camera failed to start.")

        with self._lock:
            self._mode = "test"
            self._motion_tracker.reset()
            self._state = {
                **self._build_standby_state(),
                "mode": "test",
                "camera_active": True,
            }

        self._ensure_processing_thread()
        return {"ok": True, "error": None, "state": self.get_state()}

    def toggle_preview(self) -> dict[str, Any]:
        """Toggle passive IR camera preview on demand."""
        with self._lock:
            currently_preview = self._mode == "preview"

        if currently_preview:
            self._stop_camera_and_loop()
            return {"ok": True, "error": None, "state": self.get_state()}

        # Load recognizer opportunistically if models exist so vector reticle works in preview
        self._ensure_recognizer_loaded()
        self._stop_camera_and_loop()
        ok, err = self._start_camera_worker()
        if not ok:
            return self._set_standby_error(err or "Camera failed to start.")

        with self._lock:
            self._mode = "preview"
            self._state = {
                **self._build_standby_state(),
                "mode": "preview",
                "camera_active": True,
            }

        self._ensure_processing_thread()
        return {"ok": True, "error": None, "state": self.get_state()}

    def set_feed_mode(self, mode: str) -> dict[str, Any]:
        """Switch IR display mode between 'subtract' (IR - Ambient) and 'bright'."""
        normalized = "bright" if str(mode).strip().lower() == "bright" else "subtract"
        with self._lock:
            self._feed_mode = normalized
            if self._worker is not None:
                self._worker.anti_flicker_mode = normalized
        return {"ok": True, "feed_mode": normalized}

    def select_camera(self, index: int) -> dict[str, Any]:
        """Select camera by index; starts it only if camera is currently active."""
        with self._lock:
            if not (0 <= int(index) < len(self._cameras)):
                return {"ok": False, "error": "Invalid camera index."}
            self._selected_camera_idx = int(index)
            self._current_cam_is_ir = bool(getattr(self._cameras[int(index)], "is_ir", False))
            active = self._camera_active and self._mode != "standby"

        if active:
            ok, err = self._start_camera_worker()
            if not ok:
                err_msg = err or "Camera failed to start."
                self._stop_camera_and_loop(error_msg=err_msg)
                return {
                    "ok": False,
                    "error": err_msg,
                    "error_info": classify_error(err_msg),
                    "selected_camera": int(index),
                    "state": self.get_state(),
                }
            return {"ok": True, "error": None, "selected_camera": int(index)}
        return {"ok": True, "error": None, "selected_camera": int(index)}

    def rescan_cameras(self) -> dict[str, Any]:
        """Stop any active session, re-run camera discovery, and clear stale errors."""
        self._stop_camera_and_loop()
        self._discover_cameras_safe()
        with self._lock:
            selected_camera = self._selected_camera_idx
            has_ir_camera = bool(any(getattr(c, "is_ir", False) for c in self._cameras))
            self._state = self._build_standby_state()
        return {
            "ok": True,
            "cameras": self._serialize_cameras(),
            "selected_camera": selected_camera,
            "has_ir_camera": has_ir_camera,
            "state": self.get_state(),
        }

    def clear_error(self) -> dict[str, Any]:
        """Dismiss any active error state cleanly back to standby."""
        with self._lock:
            if self._mode == "standby" and not self._camera_active:
                self._state = self._build_standby_state()
            else:
                self._state["error"] = None
                self._state["error_info"] = None
        return {"ok": True, "state": self.get_state()}

    def update_config(
        self,
        threshold: float,
        require_motion: bool,
        require_open_eyes: bool,
    ) -> dict[str, Any]:
        """Persist updated ServiceConfig to data/service_config.json."""
        clean_thresh = round(_clamp(float(threshold), 0.50, 0.80), 2)
        cfg = ServiceConfig(
            threshold=clean_thresh,
            require_motion=bool(require_motion),
            require_open_eyes=bool(require_open_eyes),
        )
        save_service_config(cfg, self._config_path)
        svc_status = self._service_status_provider()

        with self._lock:
            self._config = cfg
            self._motion_tracker.reset()
            if svc_status == "running":
                self._config_dirty = True
            dirty = self._config_dirty

        return {
            "ok": True,
            "config": {
                "threshold": cfg.threshold,
                "require_motion": cfg.require_motion,
                "require_open_eyes": cfg.require_open_eyes,
            },
            "config_dirty": dirty,
            "service_status": svc_status,
        }

    def save_password(self, username: str, domain: str, password: str) -> dict[str, Any]:
        """Store DPAPI-encrypted logon credentials and return sanitized profile summaries."""
        clean_user = str(username or "").strip()
        if not clean_user:
            return {"ok": False, "error": "Username cannot be empty."}
        if not password:
            return {"ok": False, "error": "Password cannot be empty."}
        clean_domain = str(domain or ".").strip() or "."
        self._store.set_credentials(clean_user, password=password, domain=clean_domain)
        return {
            "ok": True,
            "error": None,
            "profiles": self._serialize_profiles(),
        }

    def delete_profile(self, username: str) -> dict[str, Any]:
        """Remove an enrolled user profile and return updated profile summaries."""
        clean_user = str(username or "").strip()
        deleted = self._store.delete_user(clean_user)
        return {
            "ok": True,
            "deleted": deleted,
            "profiles": self._serialize_profiles(),
        }

    def download_models(self) -> dict[str, Any]:
        """Download YuNet and SFace ONNX models in a background thread."""
        if self._models_ready():
            self._ensure_recognizer_loaded()
            return {"ok": True, "started": False, "models_ready": True}

        with self._lock:
            if self._download_state["active"]:
                return {"ok": True, "started": False, "models_ready": False}
            self._download_state = {
                "active": True,
                "file": "",
                "downloaded": 0,
                "total": None,
                "error": None,
            }

        def _progress(name: str, downloaded: int, total: int | None) -> None:
            with self._lock:
                self._download_state["file"] = name
                self._download_state["downloaded"] = downloaded
                self._download_state["total"] = total

        def _worker() -> None:
            try:
                ensure_models(models_dir=self._models_dir, progress_callback=_progress)
                self._ensure_recognizer_loaded()
                with self._lock:
                    self._download_state["active"] = False
                    self._download_state["error"] = None
            except Exception as exc:  # noqa: BLE001
                logger.error("Model download failed: %s", exc)
                with self._lock:
                    self._download_state["active"] = False
                    self._download_state["error"] = str(exc)

        threading.Thread(target=_worker, name="WinIRFaceID-ModelDL", daemon=True).start()
        return {"ok": True, "started": True, "models_ready": False}

    def refresh_service_status(self) -> dict[str, Any]:
        """Refresh the Windows Service status."""
        status = self._service_status_provider()
        with self._lock:
            if status != "running":
                self._config_dirty = False
            dirty = self._config_dirty
        return {"ok": True, "service_status": status, "config_dirty": dirty}

    def restart_service(self) -> dict[str, Any]:
        """Request an elevated restart of WinIRFaceIDService to apply saved settings."""
        res = self._service_restarter()
        if res.get("ok"):
            with self._lock:
                self._config_dirty = False
        status = self._service_status_provider()
        with self._lock:
            dirty = self._config_dirty
        return {
            "ok": bool(res.get("ok")),
            "error": res.get("error"),
            "service_status": status,
            "config_dirty": dirty,
        }

    def open_external_url(self, url: str | None = None) -> dict[str, Any]:
        """Open a strictly validated GitHub repository or release URL in the default browser."""
        target = str(url).strip() if url else REPO_URL
        if not target:
            target = REPO_URL
        if not (target == REPO_URL or target.startswith(f"{REPO_URL}/")):
            return {"ok": False, "error": "URL not allowed."}
        try:
            self._url_opener(target)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "url": target, "error": None}

    def check_for_updates(self) -> dict[str, Any]:
        """Manually check GitHub Releases for a newer version on user demand."""
        return dict(self._update_checker())

    def shutdown(self) -> None:
        """Stop camera worker, processing loop, and loopback HTTP server cleanly."""
        self._shutting_down = True
        self._stop_camera_and_loop()

        srv: _LocalUIServer | None
        srv_thread: threading.Thread | None
        with self._lock:
            srv = self._server
            srv_thread = self._server_thread
            self._server = None
            self._server_thread = None

        if srv is not None:
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                srv.server_close()
            except Exception:  # noqa: BLE001
                pass
        if srv_thread is not None and srv_thread is not threading.current_thread():
            srv_thread.join(timeout=2.0)


class _JsApiBridge:
    """Thin public method proxy exposed to pywebview so internal controller state stays private."""

    def __init__(self, controller: WinIRFaceIDController) -> None:
        self._ctrl = controller

    def get_bootstrap(self) -> dict[str, Any]:
        return self._ctrl.get_bootstrap()

    def get_state(self) -> dict[str, Any]:
        return self._ctrl.get_state()

    def start_enroll(self, username: str, append: bool = False) -> dict[str, Any]:
        return self._ctrl.start_enroll(username, append=append)

    def cancel_action(self) -> dict[str, Any]:
        return self._ctrl.cancel_action()

    def toggle_live_test(self) -> dict[str, Any]:
        return self._ctrl.toggle_live_test()

    def toggle_preview(self) -> dict[str, Any]:
        return self._ctrl.toggle_preview()

    def set_feed_mode(self, mode: str) -> dict[str, Any]:
        return self._ctrl.set_feed_mode(mode)

    def select_camera(self, index: int) -> dict[str, Any]:
        return self._ctrl.select_camera(index)

    def rescan_cameras(self) -> dict[str, Any]:
        return self._ctrl.rescan_cameras()

    def clear_error(self) -> dict[str, Any]:
        return self._ctrl.clear_error()

    def update_config(
        self,
        threshold: float,
        require_motion: bool,
        require_open_eyes: bool,
    ) -> dict[str, Any]:
        return self._ctrl.update_config(threshold, require_motion, require_open_eyes)

    def save_password(self, username: str, domain: str, password: str) -> dict[str, Any]:
        return self._ctrl.save_password(username, domain, password)

    def delete_profile(self, username: str) -> dict[str, Any]:
        return self._ctrl.delete_profile(username)

    def download_models(self) -> dict[str, Any]:
        return self._ctrl.download_models()

    def refresh_service_status(self) -> dict[str, Any]:
        return self._ctrl.refresh_service_status()

    def restart_service(self) -> dict[str, Any]:
        return self._ctrl.restart_service()

    def open_external_url(self, url: str | None = None) -> dict[str, Any]:
        return self._ctrl.open_external_url(url)

    def check_for_updates(self) -> dict[str, Any]:
        return self._ctrl.check_for_updates()

    def shutdown(self) -> None:
        self._ctrl.shutdown()


def main() -> None:
    """Launch the minimalist Edge WebView2 desktop window for WinIRFaceID."""
    import webview

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)
    except Exception as exc:  # noqa: BLE001
        logger.debug("SetCurrentProcessExplicitAppUserModelID skipped: %s", exc)

    controller = WinIRFaceIDController()
    bridge = _JsApiBridge(controller)
    icon_path = _resolve_window_icon_path()
    icon_handles: list[int] = []

    window = webview.create_window(
        "WinIRFaceID",
        url=controller.ui_url,
        js_api=bridge,
        width=1060,
        height=740,
        min_size=(920, 680),
        background_color="#0b0c0e",
    )

    def _cleanup_icons() -> None:
        while icon_handles:
            _destroy_hicon(icon_handles.pop())

    def _on_shown() -> None:
        _apply_dark_title_bar(window)
        if icon_path:
            icon_handles.extend(_apply_window_icon(window, icon_path))

    def _on_closed() -> None:
        _cleanup_icons()
        controller.shutdown()

    if window is not None:
        window.events.shown += _on_shown
        window.events.closed += _on_closed

    try:
        try:
            webview.start(gui="edgechromium", icon=icon_path)
        except Exception:
            webview.start(icon=icon_path)
    finally:
        _cleanup_icons()
        controller.shutdown()


if __name__ == "__main__":
    main()
