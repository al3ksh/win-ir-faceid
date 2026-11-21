"""Face detection (YuNet) and 128-D embedding extraction/matching (SFace)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

from engine.models import get_model_paths


DEFAULT_MATCH_THRESHOLD = 0.65
DEFAULT_DET_SCORE_THRESHOLD = 0.78


@dataclass(frozen=True)
class FaceDetection:
    """Detected face bounding box, confidence score, 5-point landmarks, and raw YuNet row."""

    bbox: tuple[int, int, int, int]
    score: float
    landmarks: np.ndarray
    raw_row: np.ndarray


def _ensure_bgr_uint8(image: np.ndarray) -> np.ndarray:
    """Convert a 2D grayscale or BGR image to contiguous uint8 3-channel BGR."""
    if image is None or image.size == 0:
        raise ValueError("Input image is empty")
    arr = np.asarray(image)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    if arr.ndim == 3 and arr.shape[2] == 1:
        return cv2.cvtColor(arr[:, :, 0], cv2.COLOR_GRAY2BGR)
    if arr.ndim == 3 and arr.shape[2] == 4:
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    if arr.ndim == 3 and arr.shape[2] == 3:
        return np.ascontiguousarray(arr)
    raise ValueError(f"Unsupported image shape: {arr.shape}")


def enhance_ir_bgr(
    bgr: np.ndarray,
    *,
    clip_limit: float = 2.5,
    tile_grid_size: tuple[int, int] = (8, 8),
) -> np.ndarray:
    """Apply percentile stretch and CLAHE contrast enhancement for low-contrast IR frames."""
    img = _ensure_bgr_uint8(bgr)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    lo, hi = np.percentile(gray, (1.0, 99.5))
    if hi > lo + 1.0:
        scaled = np.clip((gray.astype(np.float32) - lo) * (255.0 / (hi - lo)), 0, 255).astype(
            np.uint8
        )
    else:
        scaled = gray

    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid_size)
    enhanced = clahe.apply(scaled)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)


class FaceRecognizer:
    """OpenCV YuNet + SFace wrapper for IR and RGB face detection and verification."""

    def __init__(
        self,
        yunet_path: str | Path | None = None,
        sface_path: str | Path | None = None,
        *,
        score_threshold: float = DEFAULT_DET_SCORE_THRESHOLD,
        nms_threshold: float = 0.3,
        top_k: int = 5000,
    ) -> None:
        default_paths = get_model_paths()
        self.yunet_path = Path(yunet_path) if yunet_path is not None else default_paths.yunet
        self.sface_path = Path(sface_path) if sface_path is not None else default_paths.sface

        if not self.yunet_path.is_file():
            raise FileNotFoundError(
                f"YuNet model not found at {self.yunet_path}. Run 'download-models' first."
            )
        if not self.sface_path.is_file():
            raise FileNotFoundError(
                f"SFace model not found at {self.sface_path}. Run 'download-models' first."
            )

        self._detector = cv2.FaceDetectorYN.create(
            str(self.yunet_path),
            "",
            (320, 320),
            score_threshold,
            nms_threshold,
            top_k,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(
            str(self.sface_path),
            "",
        )

    def detect_largest(self, bgr: np.ndarray) -> FaceDetection | None:
        """Detect faces in *bgr* and return the largest face by bounding box area."""
        img = _ensure_bgr_uint8(bgr)
        h, w = img.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(img)
        if faces is None or len(faces) == 0:
            return None

        best_row = max(faces, key=lambda r: max(0.0, float(r[2])) * max(0.0, float(r[3])))
        raw_row = np.asarray(best_row, dtype=np.float32).reshape(-1)
        x = int(round(float(raw_row[0])))
        y = int(round(float(raw_row[1])))
        bw = max(1, int(round(float(raw_row[2]))))
        bh = max(1, int(round(float(raw_row[3]))))
        score = float(raw_row[14]) if raw_row.size > 14 else 0.0
        landmarks = raw_row[4:14].reshape(5, 2).copy()

        return FaceDetection(
            bbox=(x, y, bw, bh),
            score=score,
            landmarks=landmarks,
            raw_row=raw_row,
        )

    def extract_embedding(self, bgr: np.ndarray, det: FaceDetection) -> np.ndarray:
        """Align, crop, and extract an L2-normalized 128-D float32 SFace embedding."""
        img = _ensure_bgr_uint8(bgr)
        aligned = self._recognizer.alignCrop(img, det.raw_row)
        feature = self._recognizer.feature(aligned)
        vec = np.asarray(feature, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec = vec / norm
        return vec.astype(np.float32, copy=False)

    @staticmethod
    def cosine_similarity(emb1: np.ndarray, emb2: np.ndarray) -> float:
        """Compute cosine similarity in ``[-1.0, 1.0]`` between two embeddings."""
        v1 = np.asarray(emb1, dtype=np.float32).reshape(-1)
        v2 = np.asarray(emb2, dtype=np.float32).reshape(-1)
        if v1.shape != v2.shape or v1.size == 0:
            raise ValueError(f"Embedding shape mismatch: {v1.shape} vs {v2.shape}")
        n1 = float(np.linalg.norm(v1))
        n2 = float(np.linalg.norm(v2))
        if n1 <= 0.0 or n2 <= 0.0:
            return 0.0
        sim = float(np.dot(v1, v2) / (n1 * n2))
        return max(-1.0, min(1.0, sim))

    @classmethod
    def match_against(
        cls,
        emb: np.ndarray,
        gallery: Sequence[np.ndarray | Sequence[float]],
        threshold: float = DEFAULT_MATCH_THRESHOLD,
    ) -> tuple[bool, float]:
        """Match *emb* against a gallery of embeddings, returning ``(matched, best_score)``."""
        if not gallery:
            return False, 0.0
        best_score = max(cls.cosine_similarity(emb, item) for item in gallery)
        return best_score >= threshold, float(best_score)
