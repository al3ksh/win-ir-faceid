"""WinRT IR and RGB camera discovery, capture worker, and Windows Hello IR LED control."""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import struct
import threading
import time
from typing import Any, Literal
import uuid

import cv2
import numpy as np
from winrt.windows.graphics.imaging import (
    BitmapAlphaMode,
    BitmapPixelFormat,
    SoftwareBitmap,
)
from winrt.windows.media.capture import (
    MediaCapture,
    MediaCaptureInitializationSettings,
    MediaCaptureMemoryPreference,
    MediaCaptureSharingMode,
    StreamingCaptureMode,
)
from winrt.windows.media.capture.frames import (
    MediaFrameSourceGroup,
    MediaFrameSourceKind,
)
from winrt.windows.storage.streams import Buffer

# Windows Kernel Streaming constants for KSPROPERTY_CAMERACONTROL_EXTENDED_FACEAUTH_MODE
KSPROPERTYSETID_EXTENDED_CAMERA_CONTROL = uuid.UUID(
    "1CB79112-C0D2-4213-9CA6-CD4FDB927972"
).bytes_le
KSPROPERTY_CAMERACONTROL_EXTENDED_FACEAUTH_MODE = 35
KSPROPERTY_TYPE_SET = 2
FACEAUTH_MODE_DISABLED = 1
FACEAUTH_MODE_ALTERNATIVE_FRAME_ILLUMINATION = 2
IOCTL_KS_PROPERTY = 0x002F0003
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
GENERIC_READ_WRITE = 0xC0000000
FILE_SHARE_READ_WRITE = 3
OPEN_EXISTING = 3

AntiFlickerMode = Literal["bright", "subtract", "raw"]

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_kernel32.DeviceIoControl.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


@dataclass(slots=True)
class CameraDescriptor:
    """Metadata describing a discovered WinRT camera source."""

    label: str
    group: Any
    device_path: str
    source_id: str
    is_ir: bool
    is_virtual: bool


@dataclass(slots=True)
class IRFramePair:
    """Latest synchronized bright (illuminated) and dark (ambient) IR frame pair."""

    bright_bgr: np.ndarray
    dark_bgr: np.ndarray | None
    diff_bgr: np.ndarray
    bright_mean: float
    dark_mean: float
    illuminator_active: bool
    timestamp: float


def set_faceauth_illuminator(device_path: str, enabled: bool) -> bool:
    """Turn the Windows Hello IR illuminator LED ON or OFF via KSPROPERTY_CAMERACONTROL_EXTENDED_FACEAUTH_MODE."""
    if not device_path or not device_path.startswith("\\\\?\\"):
        return False

    handle = _kernel32.CreateFileW(
        device_path,
        GENERIC_READ_WRITE,
        FILE_SHARE_READ_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        return False

    try:
        mode = FACEAUTH_MODE_ALTERNATIVE_FRAME_ILLUMINATION if enabled else FACEAUTH_MODE_DISABLED
        # KSPROPERTY (24 bytes): Set GUID (16B) + PropertyId (4B) + Flags (4B), pin 0 is implicit in header
        prop_in = KSPROPERTYSETID_EXTENDED_CAMERA_CONTROL + struct.pack(
            "<II",
            KSPROPERTY_CAMERACONTROL_EXTENDED_FACEAUTH_MODE,
            KSPROPERTY_TYPE_SET,
        )
        # KSCAMERA_EXTENDEDPROP_HEADER (32B) + KSCAMERA_EXTENDEDPROP_VALUE (8B) = 40B
        payload = struct.pack("<IIIIQQQ", 1, 0, 40, 0, mode, 0, 0)
        out_buf = ctypes.create_string_buffer(payload, 40)
        bytes_returned = wintypes.DWORD(0)
        ok = _kernel32.DeviceIoControl(
            handle,
            IOCTL_KS_PROPERTY,
            prop_in,
            len(prop_in),
            out_buf,
            40,
            ctypes.byref(bytes_returned),
            None,
        )
        return bool(ok)
    finally:
        _kernel32.CloseHandle(handle)


def software_bitmap_to_bgr(sb: SoftwareBitmap) -> np.ndarray:
    """Convert a WinRT SoftwareBitmap (GRAY8, GRAY16, BGRA8, NV12, etc.) into a BGR uint8 NumPy array."""
    w, h = sb.pixel_width, sb.pixel_height
    fmt = sb.bitmap_pixel_format

    if fmt == BitmapPixelFormat.GRAY8:
        buf = Buffer(w * h)
        sb.copy_to_buffer(buf)
        gray = np.frombuffer(memoryview(buf), dtype=np.uint8).reshape((h, w))
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    if fmt == BitmapPixelFormat.GRAY16:
        buf = Buffer(w * h * 2)
        sb.copy_to_buffer(buf)
        gray16 = np.frombuffer(memoryview(buf), dtype=np.uint16).reshape((h, w))
        gray8 = (gray16 >> 8).astype(np.uint8)
        return cv2.cvtColor(gray8, cv2.COLOR_GRAY2BGR)

    converted: SoftwareBitmap | None = None
    src = sb
    try:
        if fmt != BitmapPixelFormat.BGRA8:
            converted = SoftwareBitmap.convert(sb, BitmapPixelFormat.BGRA8, BitmapAlphaMode.IGNORE)
            src = converted
        buf = Buffer(w * h * 4)
        src.copy_to_buffer(buf)
        bgra = np.frombuffer(memoryview(buf), dtype=np.uint8).reshape((h, w, 4))
        return bgra[:, :, :3].copy()
    finally:
        if converted is not None:
            converted.close()


_winrt_loop_lock = threading.Lock()
_winrt_loop: asyncio.AbstractEventLoop | None = None
_winrt_loop_thread: threading.Thread | None = None


def _get_winrt_loop() -> asyncio.AbstractEventLoop:
    """Return the single process-wide persistent WinRT MTA asyncio event loop."""
    global _winrt_loop, _winrt_loop_thread

    with _winrt_loop_lock:
        if (
            _winrt_loop is not None
            and _winrt_loop_thread is not None
            and _winrt_loop_thread.is_alive()
            and not _winrt_loop.is_closed()
        ):
            return _winrt_loop

        ready = threading.Event()
        loop_box: list[asyncio.AbstractEventLoop] = []
        exc_box: list[BaseException] = []

        def _loop_runner() -> None:
            loop: asyncio.AbstractEventLoop | None = None
            try:
                try:
                    from winrt.runtime import ApartmentType, init_apartment

                    init_apartment(ApartmentType.MULTI_THREADED)
                except Exception:  # noqa: BLE001
                    pass

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop_box.append(loop)
            except BaseException as exc:  # noqa: BLE001
                exc_box.append(exc)
                ready.set()
                return

            ready.set()
            try:
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:  # noqa: BLE001
                    pass

        thread = threading.Thread(
            target=_loop_runner,
            name="WinRT-AsyncLoop",
            daemon=True,
        )
        thread.start()
        ready.wait()
        if exc_box:
            raise exc_box[0]

        _winrt_loop = loop_box[0]
        _winrt_loop_thread = thread
        return _winrt_loop


def _run_coro_sync(coro: Any) -> Any:
    """Run an async coroutine on the persistent WinRT event loop and return its result."""
    loop = _get_winrt_loop()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        coro.close()
        raise RuntimeError("_run_coro_sync() cannot be called from the WinRT event loop thread")
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def discover_cameras() -> list[CameraDescriptor]:
    """Discover WinRT cameras, sorted with real IR first, real RGB second, and virtual cameras last."""

    async def _find() -> list[CameraDescriptor]:
        groups = await MediaFrameSourceGroup.find_all_async()
        cams: list[CameraDescriptor] = []
        for group in groups:
            if len(group.source_infos) != 1:
                continue
            info = group.source_infos[0]
            kind = info.source_kind
            if kind not in (MediaFrameSourceKind.INFRARED, MediaFrameSourceKind.COLOR):
                continue

            is_ir = kind == MediaFrameSourceKind.INFRARED
            tag = "Infrared / IR" if is_ir else "RGB"
            label = f"{group.display_name} [{tag}]"
            device_path = str(group.id)
            is_virtual = ("VCAM" in device_path.upper()) or ("virtual" in group.display_name.lower())
            cams.append(
                CameraDescriptor(
                    label=label,
                    group=group,
                    device_path=device_path,
                    source_id=str(info.id),
                    is_ir=is_ir,
                    is_virtual=is_virtual,
                )
            )
        cams.sort(key=lambda c: (c.is_virtual, not c.is_ir, c.label))
        return cams

    return _run_coro_sync(_find())


class WinRTCameraWorker:
    """Runs a WinRT MediaFrameReader in a background asyncio thread with optional IR LED control."""

    def __init__(
        self,
        group: Any,
        source_id: str,
        is_ir: bool = False,
        ir_led_enabled: bool = True,
    ) -> None:
        self.group = group
        self.device_path: str = str(getattr(group, "id", ""))
        self.source_id = source_id
        self.is_ir = is_ir
        self._ir_led_enabled = ir_led_enabled
        self._ir_led_dirty = False
        self.ir_led_active = False
        self.anti_flicker_mode: AntiFlickerMode = "bright"

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest_bgr: np.ndarray | None = None
        self._latest_ir_pair: IRFramePair | None = None
        self._last_bright: np.ndarray | None = None
        self._last_dark: np.ndarray | None = None
        self._last_bright_mean: float = 0.0
        self._last_dark_mean: float = 0.0
        self._recent_means: collections.deque[float] = collections.deque(maxlen=10)

        self.error: str | None = None
        self.resolution: tuple[int, int] | None = None
        self._future: concurrent.futures.Future[None] | None = None

    def start(self) -> None:
        """Start background capture on the persistent WinRT event loop."""
        if self._future is not None and not self._future.done():
            return
        loop = _get_winrt_loop()
        self._future = asyncio.run_coroutine_threadsafe(self._async_loop(), loop)

    def stop(self) -> None:
        """Stop capture and wait for the worker to turn off the IR LED and close WinRT objects."""
        self._stop_event.set()
        if self._future is not None:
            try:
                self._future.result(timeout=4.0)
            except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
                pass
            except Exception:  # noqa: BLE001
                pass

    def set_ir_led(self, enabled: bool) -> None:
        """Request turning the Windows Hello IR illuminator LED ON or OFF."""
        self._ir_led_enabled = enabled
        self._ir_led_dirty = True

    def get_frame(self) -> np.ndarray | None:
        """Return a copy of the latest processed BGR frame, or None if no frame is available yet."""
        with self._lock:
            return None if self._latest_bgr is None else self._latest_bgr.copy()

    def get_ir_pair(self) -> IRFramePair | None:
        """Return a copy of the latest synchronized IR bright/dark/diff frame pair, or None."""
        with self._lock:
            if self._latest_ir_pair is None:
                return None
            p = self._latest_ir_pair
            return IRFramePair(
                bright_bgr=p.bright_bgr.copy(),
                dark_bgr=None if p.dark_bgr is None else p.dark_bgr.copy(),
                diff_bgr=p.diff_bgr.copy(),
                bright_mean=p.bright_mean,
                dark_mean=p.dark_mean,
                illuminator_active=p.illuminator_active,
                timestamp=p.timestamp,
            )

    def _reset_ir_tracking(self) -> None:
        self._recent_means.clear()
        self._last_bright = None
        self._last_dark = None
        self._last_bright_mean = 0.0
        self._last_dark_mean = 0.0

    def _process_ir_frame(self, bgr: np.ndarray) -> tuple[np.ndarray, IRFramePair]:
        """Track alternating bright/dark IR frames and apply the selected anti-flicker mode."""
        now = time.time()
        mean_val = float(bgr[:, :, 0].mean())

        if not self.ir_led_active:
            pair = IRFramePair(
                bright_bgr=bgr,
                dark_bgr=None,
                diff_bgr=bgr,
                bright_mean=mean_val,
                dark_mean=0.0,
                illuminator_active=False,
                timestamp=now,
            )
            return bgr, pair

        self._recent_means.append(mean_val)

        if len(self._recent_means) < 4:
            self._last_bright = bgr
            self._last_bright_mean = mean_val
            pair = IRFramePair(
                bright_bgr=bgr,
                dark_bgr=None,
                diff_bgr=bgr,
                bright_mean=mean_val,
                dark_mean=0.0,
                illuminator_active=True,
                timestamp=now,
            )
            return bgr, pair

        lo = min(self._recent_means)
        hi = max(self._recent_means)
        is_alternating = (hi - lo > 8.0) and (hi > lo * 1.35)

        if is_alternating:
            threshold = (lo + hi) * 0.5
            if mean_val >= threshold:
                self._last_bright = bgr
                self._last_bright_mean = mean_val
            else:
                self._last_dark = bgr
                self._last_dark_mean = mean_val

            bright = self._last_bright if self._last_bright is not None else bgr
            bright_mean = self._last_bright_mean if self._last_bright is not None else mean_val
            dark = self._last_dark

            if dark is not None:
                diff = cv2.subtract(bright, dark)
                pair = IRFramePair(
                    bright_bgr=bright,
                    dark_bgr=dark,
                    diff_bgr=diff,
                    bright_mean=bright_mean,
                    dark_mean=self._last_dark_mean,
                    illuminator_active=True,
                    timestamp=now,
                )
            else:
                diff = bright
                pair = IRFramePair(
                    bright_bgr=bright,
                    dark_bgr=None,
                    diff_bgr=diff,
                    bright_mean=bright_mean,
                    dark_mean=0.0,
                    illuminator_active=True,
                    timestamp=now,
                )

            if self.anti_flicker_mode == "raw":
                return bgr, pair
            if self.anti_flicker_mode == "subtract" and dark is not None:
                return diff, pair
            return bright, pair

        # Non-alternating window (e.g., overexposed startup AE or uniform/occluded scene):
        # avoid pairing a stale dark frame with the current frame.
        self._last_dark = None
        self._last_dark_mean = 0.0
        pair = IRFramePair(
            bright_bgr=bgr,
            dark_bgr=None,
            diff_bgr=bgr,
            bright_mean=mean_val,
            dark_mean=0.0,
            illuminator_active=True,
            timestamp=now,
        )
        return bgr, pair

    async def _async_loop(self) -> None:
        mc: MediaCapture | None = None
        reader = None
        try:
            mc = MediaCapture()
            settings = MediaCaptureInitializationSettings()
            settings.source_group = self.group
            settings.sharing_mode = MediaCaptureSharingMode.EXCLUSIVE_CONTROL
            settings.memory_preference = MediaCaptureMemoryPreference.CPU
            settings.streaming_capture_mode = StreamingCaptureMode.VIDEO
            try:
                await mc.initialize_async(settings)
            except Exception:
                settings.sharing_mode = MediaCaptureSharingMode.SHARED_READ_ONLY
                await mc.initialize_async(settings)

            src = mc.frame_sources[self.source_id]
            vf = src.current_format.video_format
            self.resolution = (int(vf.width), int(vf.height))

            reader = await mc.create_frame_reader_async(src)
            await reader.start_async()

            if self.is_ir and self._ir_led_enabled:
                self.ir_led_active = set_faceauth_illuminator(self.device_path, True)

            while not self._stop_event.is_set():
                if self.is_ir and self._ir_led_dirty:
                    self._ir_led_dirty = False
                    self._reset_ir_tracking()
                    ok = set_faceauth_illuminator(self.device_path, self._ir_led_enabled)
                    self.ir_led_active = self._ir_led_enabled and ok

                frame = reader.try_acquire_latest_frame()
                if frame is not None:
                    try:
                        vmf = frame.video_media_frame
                        if vmf is not None and vmf.software_bitmap is not None:
                            sb = vmf.software_bitmap
                            try:
                                bgr = software_bitmap_to_bgr(sb)
                                if self.is_ir:
                                    display_bgr, pair = self._process_ir_frame(bgr)
                                    with self._lock:
                                        self._latest_bgr = display_bgr
                                        self._latest_ir_pair = pair
                                else:
                                    with self._lock:
                                        self._latest_bgr = bgr
                            finally:
                                sb.close()
                    finally:
                        frame.close()
                await asyncio.sleep(0.012)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        finally:
            if self.is_ir:
                try:
                    set_faceauth_illuminator(self.device_path, False)
                except Exception:  # noqa: BLE001
                    pass
                self.ir_led_active = False
            if reader is not None:
                try:
                    await reader.stop_async()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    reader.close()
                except Exception:  # noqa: BLE001
                    pass
            if mc is not None:
                try:
                    mc.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await asyncio.sleep(0.05)
            except Exception:  # noqa: BLE001
                pass
