"""Active IR liveness detection against spoofing and startup AE overexposure."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import time
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


@runtime_checkable
class FaceDetectionLike(Protocol):
    """Structural contract matching ``engine.recognizer.FaceDetection``."""

    bbox: tuple[int, int, int, int]
    score: float
    landmarks: np.ndarray


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
    bg_ratio: float = 0.0
    relief_3d: float = 0.0
    face_height_ratio: float = 0.0


@dataclass(frozen=True)
class EyeOpenResult:
    """Result of evaluating active IR open-eye pupil absorption vs eyelid reflection."""

    passed: bool
    reason: str
    left_score: float
    right_score: float
    min_score: float


def _extract_clamped_roi(
    frame: np.ndarray | None,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> tuple[float, int] | None:
    """Compute ``(mean_intensity, spatial_pixel_count)`` for a clamped rectangle."""
    if frame is None or frame.size == 0 or frame.ndim < 2:
        return None
    h_img, w_img = frame.shape[:2]
    cx0 = max(0, int(x0))
    cy0 = max(0, int(y0))
    cx1 = min(w_img, int(x1))
    cy1 = min(h_img, int(y1))
    if cx1 <= cx0 or cy1 <= cy0:
        return None
    pixel_count = (cx1 - cx0) * (cy1 - cy0)
    roi = frame[cy0:cy1, cx0:cx1]
    return float(np.mean(roi)), pixel_count


def _extract_roi_mean(frame: np.ndarray | None, bbox: tuple[int, int, int, int]) -> float | None:
    """Compute mean intensity inside *bbox* ``(x, y, w, h)`` clamped to *frame*."""
    x, y, w, h = bbox
    res = _extract_clamped_roi(frame, int(x), int(y), int(x + w), int(y + h))
    return res[0] if res is not None else None


def check_ir_liveness(
    pair: IRFramePairLike,
    bbox: tuple[int, int, int, int],
    *,
    min_delta: float = 12.0,
    min_ratio: float = 1.30,
    min_face_bright: float = 20.0,
    max_face_bright: float = 242.0,
    max_frame_bright: float = 225.0,
    min_face_height_ratio: float = 0.22,
    min_face_width_ratio: float = 0.11,
    max_bg_ratio: float = 0.48,
    min_3d_relief: float = 0.12,
) -> LivenessResult:
    """Validate active IR illuminator response and 3D physical cues on a face ROI.

    Real skin strongly reflects 850/940nm active IR light when the hardware
    illuminator pulses ON vs OFF, whereas OLED/LCD phone and tablet screens emit
    negligible near-IR light and yield near-zero delta. Physical polycarbonate ID
    cards and flat photos that reflect 850nm IR are rejected via minimum face
    size, active-IR background depth falloff, and 3D center-vs-corner relief.
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

    diff_frame = pair.diff_bgr if pair.diff_bgr is not None and pair.diff_bgr.size > 0 else pair.bright_bgr
    h_img, w_img = diff_frame.shape[:2]
    x, y, w, h = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
    face_height_ratio = float(h) / float(max(1, h_img))
    face_width_ratio = float(w) / float(max(1, w_img))

    if face_height_ratio < min_face_height_ratio or face_width_ratio < min_face_width_ratio:
        return LivenessResult(
            passed=False,
            reason="face_too_small",
            delta=delta,
            ratio=ratio,
            face_bright_mean=face_bright_mean,
            face_dark_mean=face_dark_mean,
            frame_bright_mean=frame_bright_mean,
            face_height_ratio=face_height_ratio,
        )

    # Check (b): Active IR Background Depth Falloff (surround outside head must be dim)
    core50 = _extract_clamped_roi(
        diff_frame,
        x + int(round(0.25 * w)),
        y + int(round(0.25 * h)),
        x + int(round(0.75 * w)),
        y + int(round(0.75 * h)),
    )
    if core50 is None:
        core50 = _extract_clamped_roi(diff_frame, x, y, x + w, y + h)
    face_core50_diff = core50[0] if core50 is not None else max(0.0, delta)

    surround_rects = (
        (x - int(0.55 * w), y, x - int(0.15 * w), y + int(0.55 * h)),
        (x + int(1.15 * w), y, x + int(1.55 * w), y + int(0.55 * h)),
        (x + int(0.10 * w), y - int(0.40 * h), x + int(0.90 * w), y - int(0.12 * h)),
    )
    band_means: list[float] = []
    for bx0, by0, bx1, by1 in surround_rects:
        band = _extract_clamped_roi(diff_frame, bx0, by0, bx1, by1)
        if band is not None and band[1] >= 64:
            band_means.append(band[0])

    bg_ratio = 0.0
    if band_means:
        bg_diff = float(sum(band_means) / len(band_means))
        bg_ratio = bg_diff / max(face_core50_diff, 1.0)
        if bg_ratio > max_bg_ratio:
            return LivenessResult(
                passed=False,
                reason="flat_background_spoof",
                delta=delta,
                ratio=ratio,
                face_bright_mean=face_bright_mean,
                face_dark_mean=face_dark_mean,
                frame_bright_mean=frame_bright_mean,
                bg_ratio=bg_ratio,
                face_height_ratio=face_height_ratio,
            )

    # Check (c): 3D Center-vs-Upper-Corner Relief inside the face bounding box
    core45 = _extract_clamped_roi(
        diff_frame,
        x + int(round(0.275 * w)),
        y + int(round(0.275 * h)),
        x + int(round(0.725 * w)),
        y + int(round(0.725 * h)),
    )
    face_core45_diff = core45[0] if core45 is not None else face_core50_diff

    cw = max(1, int(round(0.22 * w)))
    ch = max(1, int(round(0.22 * h)))
    ul_roi = _extract_clamped_roi(diff_frame, x, y, x + cw, y + ch)
    ur_roi = _extract_clamped_roi(diff_frame, x + w - cw, y, x + w, y + ch)
    corner_means = [r[0] for r in (ul_roi, ur_roi) if r is not None]

    relief_3d = 0.0
    if corner_means:
        upper_corners_diff = float(sum(corner_means) / len(corner_means))
        relief_3d = (face_core45_diff - upper_corners_diff) / max(face_core45_diff, 1.0)
        if relief_3d < min_3d_relief:
            return LivenessResult(
                passed=False,
                reason="flat_plane_spoof",
                delta=delta,
                ratio=ratio,
                face_bright_mean=face_bright_mean,
                face_dark_mean=face_dark_mean,
                frame_bright_mean=frame_bright_mean,
                bg_ratio=bg_ratio,
                relief_3d=relief_3d,
                face_height_ratio=face_height_ratio,
            )

    return LivenessResult(
        passed=True,
        reason="ok",
        delta=delta,
        ratio=ratio,
        face_bright_mean=face_bright_mean,
        face_dark_mean=face_dark_mean,
        frame_bright_mean=frame_bright_mean,
        bg_ratio=bg_ratio,
        relief_3d=relief_3d,
        face_height_ratio=face_height_ratio,
    )


class TemporalLivenessTracker:
    """Sliding-window active micro-motion and blink detector for 3D liveness."""

    def __init__(
        self,
        *,
        window_seconds: float = 3.0,
        min_parallax: float = 0.055,
        min_eye_dip: float = 0.12,
        min_samples: int = 2,
    ) -> None:
        self.window_seconds = max(0.2, float(window_seconds))
        self.min_parallax = float(min_parallax)
        self.min_eye_dip = float(min_eye_dip)
        self.min_samples = max(2, int(min_samples))
        self._samples: deque[tuple[float, float, float, float]] = deque()

    def reset(self) -> None:
        """Clear all buffered temporal samples."""
        self._samples.clear()

    def update(
        self,
        diff_bgr: np.ndarray,
        det: FaceDetectionLike,
        timestamp: float | None = None,
    ) -> tuple[bool, str]:
        """Record the latest frame + landmarks and evaluate blink or 3D parallax."""
        now = time.monotonic() if timestamp is None else float(timestamp)
        if self._samples and now < self._samples[-1][0]:
            self._samples.clear()

        cutoff = now - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        if diff_bgr is None or diff_bgr.size == 0 or diff_bgr.ndim < 2:
            return False, "waiting_blink_or_motion"

        landmarks = np.asarray(det.landmarks, dtype=np.float32)
        if landmarks.ndim != 2 or landmarks.shape[0] < 3 or landmarks.shape[1] < 2:
            return False, "waiting_blink_or_motion"

        eye_left = landmarks[0, :2]
        eye_right = landmarks[1, :2]
        nose = landmarks[2, :2]

        inter_eye_dist = float(np.linalg.norm(eye_right - eye_left))
        if inter_eye_dist < 4.0:
            return False, "waiting_blink_or_motion"

        eye_mid = 0.5 * (eye_left + eye_right)
        px = float((nose[0] - eye_mid[0]) / inter_eye_dist)
        py = float((nose[1] - eye_mid[1]) / inter_eye_dist)

        r_eye = max(2, int(round(0.16 * inter_eye_dist)))
        r_bridge = max(2, int(round(0.15 * inter_eye_dist)))
        bridge = 0.7 * eye_mid + 0.3 * nose

        eye_means: list[float] = []
        for pt in (eye_left, eye_right):
            cx, cy = int(round(float(pt[0]))), int(round(float(pt[1])))
            patch = _extract_clamped_roi(diff_bgr, cx - r_eye, cy - r_eye, cx + r_eye + 1, cy + r_eye + 1)
            if patch is not None:
                eye_means.append(patch[0])

        bx, by = int(round(float(bridge[0]))), int(round(float(bridge[1])))
        bridge_patch = _extract_clamped_roi(
            diff_bgr, bx - r_bridge, by - r_bridge, bx + r_bridge + 1, by + r_bridge + 1
        )
        if not eye_means or bridge_patch is None:
            return False, "waiting_blink_or_motion"

        eye_mean = float(sum(eye_means) / len(eye_means))
        rel_eye = eye_mean / max(bridge_patch[0], 1.0)

        self._samples.append((now, px, py, rel_eye))
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        if len(self._samples) < self.min_samples:
            return False, "waiting_blink_or_motion"

        px_vals = [s[1] for s in self._samples]
        py_vals = [s[2] for s in self._samples]
        rel_vals = [s[3] for s in self._samples]

        parallax_delta = math.hypot(max(px_vals) - min(px_vals), max(py_vals) - min(py_vals))
        max_rel = max(rel_vals)
        min_rel = min(rel_vals)
        eye_dip = (max_rel - min_rel) / max(max_rel, 1e-6)

        if eye_dip >= self.min_eye_dip:
            return True, "ok_blink"
        if parallax_delta >= self.min_parallax:
            return True, "ok_3d_motion"
        return False, "waiting_blink_or_motion"


def _clamped_patch(gray: np.ndarray, cx: float, cy: float, radius: int) -> np.ndarray | None:
    """Extract a 2D slice centered at ``(cx, cy)`` with half-width *radius*."""
    h_img, w_img = gray.shape[:2]
    ix = int(round(float(cx)))
    iy = int(round(float(cy)))
    x0 = max(0, ix - radius)
    y0 = max(0, iy - radius)
    x1 = min(w_img, ix + radius + 1)
    y1 = min(h_img, iy + radius + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    return gray[y0:y1, x0:x1]


def check_eyes_open_ir(
    diff_bgr: np.ndarray,
    det: FaceDetectionLike,
    *,
    min_open_score: float = 0.28,
) -> EyeOpenResult:
    """Check that both eyes appear open in an active-IR difference frame.

    In 850/940nm active IR, open pupils and irises absorb near-IR light and
    appear substantially darker than surrounding illuminated facial skin, whereas
    closed eyelids reflect IR almost as brightly as skin.
    """
    if diff_bgr is None or diff_bgr.size == 0 or diff_bgr.ndim < 2:
        return EyeOpenResult(
            passed=False,
            reason="invalid_landmarks",
            left_score=0.0,
            right_score=0.0,
            min_score=0.0,
        )

    if det is None or getattr(det, "landmarks", None) is None:
        return EyeOpenResult(
            passed=False,
            reason="invalid_landmarks",
            left_score=0.0,
            right_score=0.0,
            min_score=0.0,
        )

    landmarks = np.asarray(det.landmarks, dtype=np.float32)
    if (
        landmarks.ndim != 2
        or landmarks.shape[0] < 3
        or landmarks.shape[1] < 2
        or not np.all(np.isfinite(landmarks[:3, :2]))
    ):
        return EyeOpenResult(
            passed=False,
            reason="invalid_landmarks",
            left_score=0.0,
            right_score=0.0,
            min_score=0.0,
        )

    eye_left = landmarks[0, :2]
    eye_right = landmarks[1, :2]
    nose = landmarks[2, :2]
    inter_eye_dist = float(np.linalg.norm(eye_right - eye_left))
    if inter_eye_dist < 6.0:
        return EyeOpenResult(
            passed=False,
            reason="invalid_landmarks",
            left_score=0.0,
            right_score=0.0,
            min_score=0.0,
        )

    if diff_bgr.ndim == 2:
        gray = diff_bgr.astype(np.float32, copy=False)
    else:
        gray = np.mean(diff_bgr, axis=2, dtype=np.float32)

    eye_mid = 0.5 * (eye_left + eye_right)
    bridge = 0.65 * eye_mid + 0.35 * nose
    left_cheek = (float(eye_left[0]), float(eye_left[1]) + 0.38 * inter_eye_dist)
    right_cheek = (float(eye_right[0]), float(eye_right[1]) + 0.38 * inter_eye_dist)

    r_skin = max(2, int(round(0.12 * inter_eye_dist)))
    skin_means: list[float] = []
    for sx, sy in (
        (float(bridge[0]), float(bridge[1])),
        left_cheek,
        right_cheek,
    ):
        patch = _clamped_patch(gray, sx, sy, r_skin)
        if patch is not None and patch.size > 0:
            skin_means.append(float(np.mean(patch)))

    if not skin_means:
        return EyeOpenResult(
            passed=False,
            reason="invalid_landmarks",
            left_score=0.0,
            right_score=0.0,
            min_score=0.0,
        )

    skin_ref = float(np.median(skin_means))
    if skin_ref < 15.0:
        return EyeOpenResult(
            passed=False,
            reason="too_dark",
            left_score=0.0,
            right_score=0.0,
            min_score=0.0,
        )

    r_eye = max(3, int(round(0.13 * inter_eye_dist)))
    left_patch = _clamped_patch(gray, float(eye_left[0]), float(eye_left[1]), r_eye)
    right_patch = _clamped_patch(gray, float(eye_right[0]), float(eye_right[1]), r_eye)
    if left_patch is None or right_patch is None or left_patch.size == 0 or right_patch.size == 0:
        return EyeOpenResult(
            passed=False,
            reason="invalid_landmarks",
            left_score=0.0,
            right_score=0.0,
            min_score=0.0,
        )

    left_p15 = float(np.percentile(left_patch, 15))
    right_p15 = float(np.percentile(right_patch, 15))
    denom = max(skin_ref, 1.0)
    left_score = float((skin_ref - left_p15) / denom)
    right_score = float((skin_ref - right_p15) / denom)
    min_score = min(left_score, right_score)

    if min_score < min_open_score:
        return EyeOpenResult(
            passed=False,
            reason="eyes_closed",
            left_score=left_score,
            right_score=right_score,
            min_score=min_score,
        )

    return EyeOpenResult(
        passed=True,
        reason="ok",
        left_score=left_score,
        right_score=right_score,
        min_score=min_score,
    )
