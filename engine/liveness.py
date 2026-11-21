"""Active IR liveness detection against spoofing and startup AE overexposure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class IRFramePairLike(Protocol):
    """Structural contract matching ``engine.ir_camera.IRFramePair``."""

    bright_bgr: np.ndarray
    dark_bgr: np.ndarray | None
    diff_bgr: np.ndarray
    bright_mean: float
    dark_mean: float
    illuminator_active: bool
    timestamp: float


@dataclass(frozen=True)
class LivenessResult:
    """Result of evaluating active IR skin reflection liveness on a face ROI."""

    passed: bool
    reason: str
    delta: float
    ratio: float
    face_bright_mean: float
    face_dark_mean: float
    frame_bright_mean: float


def _extract_roi_mean(frame: np.ndarray, bbox: tuple[int, int, int, int]) -> float | None:
    """Compute mean intensity inside *bbox* ``(x, y, w, h)`` clamped to *frame*."""
    if frame is None or frame.size == 0 or frame.ndim < 2:
        return None
    h_img, w_img = frame.shape[:2]
    x, y, w, h = bbox
    x0 = max(0, int(x))
    y0 = max(0, int(y))
    x1 = min(w_img, int(x + w))
    y1 = min(h_img, int(y + h))
    if x1 <= x0 or y1 <= y0:
        return None
    roi = frame[y0:y1, x0:x1]
    return float(np.mean(roi))


def check_ir_liveness(
    pair: IRFramePairLike,
    bbox: tuple[int, int, int, int],
    *,
    min_delta: float = 12.0,
    min_ratio: float = 1.30,
    min_face_bright: float = 20.0,
    max_face_bright: float = 242.0,
    max_frame_bright: float = 225.0,
) -> LivenessResult:
    """Validate active IR illuminator response inside the detected face bounding box.

    Real skin strongly reflects 850/940nm active IR light when the hardware
    illuminator pulses ON vs OFF, whereas OLED/LCD phone and tablet screens emit
    negligible near-IR light and yield near-zero delta. Startup auto-exposure
    flashbang frames are also rejected so AE can settle cleanly.
    """
    frame_bright_mean = float(pair.bright_mean)

    if not pair.illuminator_active:
        return LivenessResult(
            passed=False,
            reason="illuminator_inactive",
            delta=0.0,
            ratio=1.0,
            face_bright_mean=0.0,
            face_dark_mean=0.0,
            frame_bright_mean=frame_bright_mean,
        )

    if pair.dark_bgr is None:
        return LivenessResult(
            passed=False,
            reason="missing_dark_frame",
            delta=0.0,
            ratio=1.0,
            face_bright_mean=0.0,
            face_dark_mean=0.0,
            frame_bright_mean=frame_bright_mean,
        )

    face_bright_mean = _extract_roi_mean(pair.bright_bgr, bbox)
    face_dark_mean = _extract_roi_mean(pair.dark_bgr, bbox)
    if face_bright_mean is None or face_dark_mean is None:
        return LivenessResult(
            passed=False,
            reason="invalid_bbox",
            delta=0.0,
            ratio=1.0,
            face_bright_mean=0.0,
            face_dark_mean=0.0,
            frame_bright_mean=frame_bright_mean,
        )

    delta = face_bright_mean - face_dark_mean
    ratio = face_bright_mean / max(face_dark_mean, 1.0)

    if face_bright_mean > max_face_bright or frame_bright_mean > max_frame_bright:
        return LivenessResult(
            passed=False,
            reason="overexposed_ae_settling",
            delta=delta,
            ratio=ratio,
            face_bright_mean=face_bright_mean,
            face_dark_mean=face_dark_mean,
            frame_bright_mean=frame_bright_mean,
        )

    if face_bright_mean < min_face_bright:
        return LivenessResult(
            passed=False,
            reason="too_dark",
            delta=delta,
            ratio=ratio,
            face_bright_mean=face_bright_mean,
            face_dark_mean=face_dark_mean,
            frame_bright_mean=frame_bright_mean,
        )

    if delta < min_delta:
        return LivenessResult(
            passed=False,
            reason="low_ir_delta",
            delta=delta,
            ratio=ratio,
            face_bright_mean=face_bright_mean,
            face_dark_mean=face_dark_mean,
            frame_bright_mean=frame_bright_mean,
        )

    if ratio < min_ratio:
        return LivenessResult(
            passed=False,
            reason="low_ir_ratio",
            delta=delta,
            ratio=ratio,
            face_bright_mean=face_bright_mean,
            face_dark_mean=face_dark_mean,
            frame_bright_mean=frame_bright_mean,
        )

    return LivenessResult(
        passed=True,
        reason="ok",
        delta=delta,
        ratio=ratio,
        face_bright_mean=face_bright_mean,
        face_dark_mean=face_dark_mean,
        frame_bright_mean=frame_bright_mean,
    )
