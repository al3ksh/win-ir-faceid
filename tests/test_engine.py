"""Unit tests for the WinIRFaceID Python engine."""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from engine import __version__
from engine.cli import _select_enrollment_camera
from engine.ipc_server import (
    DEFAULT_THRESHOLD,
    IRFaceVerifier,
    NamedPipeServer,
    _select_camera,
    handle_raw_message,
    handle_request,
)
from engine.liveness import TemporalLivenessTracker, check_ir_liveness
from engine.models import ModelPaths, get_model_paths, models_exist
from engine.recognizer import (
    DEFAULT_DET_SCORE_THRESHOLD,
    DEFAULT_MATCH_THRESHOLD,
    FaceDetection,
    FaceRecognizer,
    enhance_ir_bgr,
)
from engine.store import ProfileStore


@dataclass
class DummyIRFramePair:
    bright_bgr: np.ndarray
    dark_bgr: np.ndarray | None
    diff_bgr: np.ndarray
    bright_mean: float
    dark_mean: float
    illuminator_active: bool
    timestamp: float


def _make_pair(
    *,
    face_bright: int = 180,
    face_dark: int = 20,
    bg_bright: int = 5,
    bg_dark: int = 5,
    shape: tuple[int, int] = (120, 120),
    bbox: tuple[int, int, int, int] = (20, 20, 60, 60),
    radial_shading: bool = True,
    bright_mean_override: float | None = None,
    illuminator_active: bool = True,
    with_dark: bool = True,
    timestamp: float = 1.0,
) -> DummyIRFramePair:
    h_img, w_img = shape
    x, y, w, h = bbox
    bright = np.full((h_img, w_img, 3), bg_bright, dtype=np.uint8)

    if radial_shading:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        nx = (xx - (w - 1) / 2.0) / max(w / 2.0, 1.0)
        ny = (yy - (h - 1) / 2.0) / max(h / 2.0, 1.0)
        r2 = np.clip(0.5 * (nx * nx + ny * ny), 0.0, 1.0)
        shading = 1.0 - 0.70 * r2
        face_b = np.clip(face_dark + (face_bright - face_dark) * shading, 0, 255).astype(np.uint8)
        bright[y : y + h, x : x + w] = face_b[:, :, None]
    else:
        bright[y : y + h, x : x + w] = face_bright

    if with_dark:
        dark = np.full((h_img, w_img, 3), bg_dark, dtype=np.uint8)
        dark[y : y + h, x : x + w] = face_dark
        diff = np.clip(bright.astype(np.int16) - dark.astype(np.int16), 0, 255).astype(np.uint8)
        dark_mean = float(np.mean(dark))
    else:
        dark = None
        diff = bright.copy()
        dark_mean = 0.0

    bright_mean = float(np.mean(bright)) if bright_mean_override is None else bright_mean_override
    return DummyIRFramePair(
        bright_bgr=bright,
        dark_bgr=dark,
        diff_bgr=diff,
        bright_mean=bright_mean,
        dark_mean=dark_mean,
        illuminator_active=illuminator_active,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Liveness tests
# ---------------------------------------------------------------------------


def test_liveness_passes_active_ir_skin_delta() -> None:
    pair = _make_pair(face_bright=200, face_dark=30, bg_bright=5, bg_dark=5)
    res = check_ir_liveness(pair, (20, 20, 60, 60))
    assert res.passed is True
    assert res.reason == "ok"
    assert res.delta > 12.0
    assert res.ratio > 1.30
    assert res.bg_ratio < 0.10
    assert res.relief_3d > 0.30
    assert res.face_height_ratio == pytest.approx(0.50)


def test_liveness_rejects_phone_screen_low_delta() -> None:
    # Phone OLED/LCD screen emits constant light with virtually no 850/940nm IR reflection delta
    pair = _make_pair(face_bright=92, face_dark=88, radial_shading=False)
    res = check_ir_liveness(pair, (20, 20, 60, 60))
    assert res.passed is False
    assert res.reason == "low_ir_delta"
    assert res.delta == pytest.approx(4.0)


def test_liveness_rejects_low_ratio_under_high_ambient() -> None:
    pair = _make_pair(face_bright=135, face_dark=115, radial_shading=False)
    res = check_ir_liveness(pair, (20, 20, 60, 60))
    assert res.passed is False
    assert res.reason == "low_ir_ratio"
    assert res.delta == pytest.approx(20.0)
    assert res.ratio < 1.30


def test_liveness_rejects_flashbang_overexposure() -> None:
    pair_face_hot = _make_pair(face_bright=248, face_dark=60, radial_shading=False, bright_mean_override=120.0)
    res1 = check_ir_liveness(pair_face_hot, (20, 20, 60, 60))
    assert res1.passed is False
    assert res1.reason == "overexposed_ae_settling"

    pair_frame_hot = _make_pair(face_bright=180, face_dark=60, bright_mean_override=230.0)
    res2 = check_ir_liveness(pair_frame_hot, (20, 20, 60, 60))
    assert res2.passed is False
    assert res2.reason == "overexposed_ae_settling"


def test_liveness_rejects_inactive_illuminator_and_missing_dark() -> None:
    pair_off = _make_pair(illuminator_active=False)
    assert check_ir_liveness(pair_off, (20, 20, 60, 60)).reason == "illuminator_inactive"

    pair_no_dark = _make_pair(with_dark=False)
    assert check_ir_liveness(pair_no_dark, (20, 20, 60, 60)).reason == "missing_dark_frame"

    pair_valid = _make_pair()
    assert check_ir_liveness(pair_valid, (500, 500, 20, 20)).reason == "invalid_bbox"


def test_liveness_rejects_face_too_small() -> None:
    pair = _make_pair(
        face_bright=190,
        face_dark=25,
        bg_bright=5,
        bg_dark=5,
        shape=(360, 640),
        bbox=(280, 140, 60, 60),
    )
    res = check_ir_liveness(pair, (280, 140, 60, 60))
    assert res.passed is False
    assert res.reason == "face_too_small"
    assert res.face_height_ratio == pytest.approx(60.0 / 360.0)

    # Also reject overly narrow face bbox (< 11% image width)
    pair_narrow = _make_pair(
        face_bright=190,
        face_dark=25,
        bg_bright=5,
        bg_dark=5,
        shape=(360, 640),
        bbox=(280, 100, 60, 120),
    )
    res_narrow = check_ir_liveness(pair_narrow, (280, 100, 60, 120))
    assert res_narrow.passed is False
    assert res_narrow.reason == "face_too_small"


def test_liveness_rejects_flat_background_spoof() -> None:
    # Bright reflective card/wall surround around the head (bg_ratio > 0.48)
    pair = _make_pair(
        face_bright=180,
        face_dark=20,
        bg_bright=135,
        bg_dark=15,
        shape=(360, 640),
        bbox=(220, 80, 180, 200),
        radial_shading=True,
    )
    res = check_ir_liveness(pair, (220, 80, 180, 200))
    assert res.passed is False
    assert res.reason == "flat_background_spoof"
    assert res.bg_ratio > 0.48


def test_liveness_rejects_flat_plane_spoof() -> None:
    # Flat polycarbonate ID card photo: uniform high IR reflection inside bbox with dark surround
    pair = _make_pair(
        face_bright=170,
        face_dark=20,
        bg_bright=5,
        bg_dark=5,
        shape=(360, 640),
        bbox=(220, 80, 180, 200),
        radial_shading=False,
    )
    res = check_ir_liveness(pair, (220, 80, 180, 200))
    assert res.passed is False
    assert res.reason == "flat_plane_spoof"
    assert res.bg_ratio < 0.10
    assert res.relief_3d < 0.12


def _make_face_det(
    *,
    eye_left: tuple[float, float] = (270.0, 150.0),
    eye_right: tuple[float, float] = (330.0, 150.0),
    nose: tuple[float, float] = (300.0, 185.0),
    bbox: tuple[int, int, int, int] = (220, 90, 160, 180),
    score: float = 0.95,
) -> FaceDetection:
    landmarks = np.array(
        [
            eye_left,
            eye_right,
            nose,
            (275.0, 225.0),
            (325.0, 225.0),
        ],
        dtype=np.float32,
    )
    raw = np.zeros(15, dtype=np.float32)
    raw[0:4] = bbox
    raw[4:14] = landmarks.reshape(-1)
    raw[14] = score
    return FaceDetection(bbox=bbox, score=score, landmarks=landmarks, raw_row=raw)


def _make_eye_patch_frame(
    *,
    eye_intensity: int = 110,
    bridge_intensity: int = 180,
    shape: tuple[int, int] = (360, 640),
    eye_left: tuple[int, int] = (270, 150),
    eye_right: tuple[int, int] = (330, 150),
    nose: tuple[int, int] = (300, 185),
) -> np.ndarray:
    frame = np.full((shape[0], shape[1], 3), bridge_intensity, dtype=np.uint8)
    for ex, ey in (eye_left, eye_right):
        frame[ey - 12 : ey + 13, ex - 12 : ex + 13] = eye_intensity
    # Keep nose bridge at bridge_intensity explicitly
    bx = int(round(0.7 * 0.5 * (eye_left[0] + eye_right[0]) + 0.3 * nose[0]))
    by = int(round(0.7 * 0.5 * (eye_left[1] + eye_right[1]) + 0.3 * nose[1]))
    frame[by - 12 : by + 13, bx - 12 : bx + 13] = bridge_intensity
    return frame


def test_temporal_liveness_tracker_static_photo_stays_unverified() -> None:
    tracker = TemporalLivenessTracker(window_seconds=3.0)
    frame1 = _make_eye_patch_frame(eye_intensity=100, bridge_intensity=180)
    det1 = _make_face_det()

    # Frame 1..3: static photo
    for i in range(3):
        ok, reason = tracker.update(frame1, det1, timestamp=1.0 + i * 0.2)
        assert ok is False
        assert reason == "waiting_blink_or_motion"

    # In-plane translation + uniform brightness scaling of flat photo must stay unverified
    frame_shifted = _make_eye_patch_frame(
        eye_intensity=120,
        bridge_intensity=216,  # exact same 100/180 = 120/216 ratio
        eye_left=(290, 160),
        eye_right=(350, 160),
        nose=(320, 195),
    )
    det_shifted = _make_face_det(
        eye_left=(290.0, 160.0),
        eye_right=(350.0, 160.0),
        nose=(320.0, 195.0),
        bbox=(240, 100, 160, 180),
    )
    ok, reason = tracker.update(frame_shifted, det_shifted, timestamp=1.6)
    assert ok is False
    assert reason == "waiting_blink_or_motion"


def test_temporal_liveness_tracker_blink_verifies() -> None:
    tracker = TemporalLivenessTracker(window_seconds=3.0)
    det = _make_face_det()

    frame_open = _make_eye_patch_frame(eye_intensity=140, bridge_intensity=180)
    frame_closed = _make_eye_patch_frame(eye_intensity=85, bridge_intensity=180)

    assert tracker.update(frame_open, det, timestamp=1.0) == (False, "waiting_blink_or_motion")
    ok, reason = tracker.update(frame_closed, det, timestamp=1.2)
    assert ok is True
    assert reason == "ok_blink"
    # Recovery frame inside window remains verified
    ok_rec, reason_rec = tracker.update(frame_open, det, timestamp=1.4)
    assert ok_rec is True
    assert reason_rec == "ok_blink"

    tracker.reset()
    assert tracker.update(frame_open, det, timestamp=2.0) == (False, "waiting_blink_or_motion")


def test_temporal_liveness_tracker_3d_nose_parallax_verifies() -> None:
    tracker = TemporalLivenessTracker(window_seconds=3.0)
    frame = _make_eye_patch_frame(eye_intensity=120, bridge_intensity=180)

    det_center = _make_face_det(
        eye_left=(270.0, 150.0),
        eye_right=(330.0, 150.0),
        nose=(300.0, 185.0),
    )
    # 3D yaw turn: nose shifts relative to eye_mid by 6px / 60px inter-eye = 0.10 >= 0.055
    det_turned = _make_face_det(
        eye_left=(270.0, 150.0),
        eye_right=(330.0, 150.0),
        nose=(306.0, 185.0),
    )

    assert tracker.update(frame, det_center, timestamp=1.0) == (False, "waiting_blink_or_motion")
    ok, reason = tracker.update(frame, det_turned, timestamp=1.4)
    assert ok is True
    assert reason == "ok_3d_motion"

    # After the 3.0s window expires without further motion, returns to waiting
    ok_expired, reason_expired = tracker.update(frame, det_turned, timestamp=5.0)
    assert ok_expired is False
    assert reason_expired == "waiting_blink_or_motion"


# ---------------------------------------------------------------------------
# DPAPI Store tests
# ---------------------------------------------------------------------------


def test_store_dpapi_roundtrip_and_encryption(tmp_path: Path) -> None:
    store_path = tmp_path / "profiles.enc"
    store = ProfileStore(store_path)

    emb1 = np.linspace(0.1, 1.0, 128, dtype=np.float32)
    emb1 /= np.linalg.norm(emb1)
    emb2 = np.linspace(1.0, 0.1, 128, dtype=np.float32)
    emb2 /= np.linalg.norm(emb2)

    store.upsert_embeddings("Alice", [emb1], domain="CORP")
    store.set_credentials("Alice", password="SuperSecretPassword!123", domain="CORP")
    store.upsert_embeddings("Alice", [emb2], append=True)

    # Verify disk contents are DPAPI encrypted (no plaintext leaks)
    raw_bytes = store_path.read_bytes()
    assert b"SuperSecretPassword!123" not in raw_bytes
    assert b"Alice" not in raw_bytes

    # Reload via a fresh ProfileStore instance
    reloaded = ProfileStore(store_path)
    users = reloaded.list_users()
    assert [u.username for u in users] == ["Alice"]

    alice = reloaded.get_user("alice")  # case-insensitive lookup
    assert alice is not None
    assert alice.domain == "CORP"
    assert alice.password == "SuperSecretPassword!123"
    assert len(alice.embeddings) == 2
    np.testing.assert_allclose(alice.embedding_arrays()[0], emb1, atol=1e-6)

    # Replace embeddings
    reloaded.upsert_embeddings("Alice", [emb2], append=False)
    alice2 = reloaded.get_user("Alice")
    assert alice2 is not None
    assert len(alice2.embeddings) == 1
    assert alice2.password == "SuperSecretPassword!123"

    # Delete user
    assert reloaded.delete_user("Alice") is True
    assert reloaded.delete_user("Alice") is False
    assert reloaded.list_users() == []


# ---------------------------------------------------------------------------
# Recognizer tests
# ---------------------------------------------------------------------------


def test_recognizer_cosine_similarity_and_gallery_matching() -> None:
    v1 = np.zeros(128, dtype=np.float32)
    v1[0] = 1.0
    v2 = np.zeros(128, dtype=np.float32)
    v2[1] = 1.0
    v_neg = -v1

    assert FaceRecognizer.cosine_similarity(v1, v1) == pytest.approx(1.0)
    assert FaceRecognizer.cosine_similarity(v1, v2) == pytest.approx(0.0)
    assert FaceRecognizer.cosine_similarity(v1, v_neg) == pytest.approx(-1.0)
    assert FaceRecognizer.cosine_similarity(v1, np.zeros(128, dtype=np.float32)) == 0.0

    with pytest.raises(ValueError):
        FaceRecognizer.cosine_similarity(v1, np.ones(64, dtype=np.float32))

    # Empty gallery
    matched_empty, score_empty = FaceRecognizer.match_against(v1, [])
    assert matched_empty is False
    assert score_empty == 0.0

    # Default threshold constants
    assert DEFAULT_MATCH_THRESHOLD == pytest.approx(0.65)
    assert DEFAULT_DET_SCORE_THRESHOLD == pytest.approx(0.78)
    assert DEFAULT_THRESHOLD == pytest.approx(0.65)

    # Real full-face similarity (~0.897) passes default threshold (0.65)
    full_face = np.zeros(128, dtype=np.float32)
    full_face[0] = 0.897
    full_face[1] = float(np.sqrt(1.0 - 0.897**2))
    matched, score = FaceRecognizer.match_against(full_face, [v2, v1])
    assert matched is True
    assert score == pytest.approx(0.897, abs=1e-4)

    # Half-covered face similarity (~0.546) is rejected by default threshold (0.65)
    half_covered = np.zeros(128, dtype=np.float32)
    half_covered[0] = 0.546
    half_covered[1] = float(np.sqrt(1.0 - 0.546**2))
    matched_half, score_half = FaceRecognizer.match_against(half_covered, [v1])
    assert matched_half is False
    assert score_half == pytest.approx(0.546, abs=1e-4)


def test_recognizer_detect_and_extract_with_mocked_opencv(tmp_path: Path) -> None:
    yunet = tmp_path / "yunet.onnx"
    sface = tmp_path / "sface.onnx"
    yunet.write_bytes(b"fake-yunet")
    sface.write_bytes(b"fake-sface")

    mock_detector = MagicMock()
    small_face = np.array([5, 5, 20, 20, 1, 1, 2, 1, 1, 2, 1, 3, 2, 3, 0.80], dtype=np.float32)
    large_face = np.array([30, 40, 80, 90, 1, 1, 2, 1, 1, 2, 1, 3, 2, 3, 0.96], dtype=np.float32)
    mock_detector.detect.return_value = (1, np.stack([small_face, large_face]))

    mock_sface = MagicMock()
    mock_sface.alignCrop.return_value = np.zeros((112, 112, 3), dtype=np.uint8)
    raw_feat = np.full((1, 128), 3.0, dtype=np.float32)
    mock_sface.feature.return_value = raw_feat

    with (
        patch("engine.recognizer.cv2.FaceDetectorYN.create", return_value=mock_detector),
        patch("engine.recognizer.cv2.FaceRecognizerSF.create", return_value=mock_sface),
    ):
        rec = FaceRecognizer(yunet_path=yunet, sface_path=sface)
        frame = np.zeros((240, 320), dtype=np.uint8)  # 2D grayscale IR frame
        det = rec.detect_largest(frame)

        assert det is not None
        assert det.bbox == (30, 40, 80, 90)
        assert det.score == pytest.approx(0.96)
        assert det.landmarks.shape == (5, 2)
        mock_detector.setInputSize.assert_called_with((320, 240))

        emb = rec.extract_embedding(frame, det)
        assert emb.shape == (128,)
        assert emb.dtype == np.float32
        assert np.linalg.norm(emb) == pytest.approx(1.0, abs=1e-5)


def test_enhance_ir_bgr_preserves_shape() -> None:
    gray = np.linspace(20, 80, 100 * 80, dtype=np.uint8).reshape(100, 80)
    out = enhance_ir_bgr(gray)
    assert out.shape == (100, 80, 3)
    assert out.dtype == np.uint8


def test_models_exist_checks_non_empty(tmp_path: Path) -> None:
    paths = get_model_paths(tmp_path)
    assert isinstance(paths, ModelPaths)
    assert models_exist(tmp_path) is False

    paths.yunet.write_bytes(b"too-small")
    paths.sface.write_bytes(b"too-small")
    assert models_exist(tmp_path) is False

    paths.yunet.write_bytes(b"x" * 2048)
    paths.sface.write_bytes(b"y" * 2048)
    assert models_exist(tmp_path) is True


# ---------------------------------------------------------------------------
# IPC Server & Verifier tests
# ---------------------------------------------------------------------------


def test_ipc_handle_request_ping_cancel_and_validation(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles.enc")
    vec = np.ones(128, dtype=np.float32) / np.sqrt(128.0)
    store.upsert_embeddings("DELL", [vec])

    ping_resp = handle_request({"cmd": "ping"}, store=store)
    assert ping_resp == {
        "status": "ok",
        "version": __version__,
        "enrolled_users": ["DELL"],
    }

    cancel_evt = threading.Event()
    cancel_resp = handle_request({"cmd": "cancel"}, store=store, cancel_event=cancel_evt)
    assert cancel_resp == {"status": "ok"}
    assert cancel_evt.is_set() is True

    # Invalid commands / payloads
    assert handle_request({"cmd": "unknown"}, store=store) == {
        "status": "failed",
        "reason": "bad_request",
    }
    assert handle_request({"cmd": "verify", "timeout_ms": -5}, store=store) == {
        "status": "failed",
        "reason": "bad_request",
    }
    assert handle_raw_message(b"{not-json", store=store) == {
        "status": "failed",
        "reason": "bad_request",
    }


def test_ipc_handle_verify_with_mock_verifier(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles.enc")
    mock_verifier = MagicMock()
    mock_verifier.verify.return_value = {
        "status": "matched",
        "username": "DELL",
        "domain": ".",
        "password": "secret",
        "score": 0.72,
        "liveness_delta": 50.0,
    }

    resp = handle_request(
        {"cmd": "verify", "username": "DELL", "timeout_ms": 5000},
        store=store,
        verifier=mock_verifier,
    )
    assert resp["status"] == "matched"
    assert resp["username"] == "DELL"
    mock_verifier.verify.assert_called_once()


def test_ir_face_verifier_consecutive_match_and_cleanup(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles.enc")
    alice_vec = np.zeros(128, dtype=np.float32)
    alice_vec[0] = 1.0
    impostor_vec = np.zeros(128, dtype=np.float32)
    impostor_vec[1] = 1.0

    store.upsert_embeddings("Alice", [alice_vec], domain=".")
    store.set_credentials("Alice", password="WinPassword99", domain=".")

    dummy_cam = MagicMock(group="g1", source_id="s1", is_ir=True, is_virtual=False)
    worker = MagicMock()
    worker.error = None
    # Frame 1: match, Frame 2: non-match (resets streak), Frame 3 & 4: two consecutive matches
    worker.get_ir_pair.side_effect = [
        _make_pair(face_bright=190, face_dark=30, bg_bright=5, bg_dark=5, timestamp=1.0),
        _make_pair(face_bright=190, face_dark=30, bg_bright=5, bg_dark=5, timestamp=2.0),
        _make_pair(face_bright=190, face_dark=30, bg_bright=5, bg_dark=5, timestamp=3.0),
        _make_pair(face_bright=190, face_dark=30, bg_bright=5, bg_dark=5, timestamp=4.0),
    ]

    det = FaceDetection(
        bbox=(20, 20, 60, 60),
        score=0.95,
        landmarks=np.zeros((5, 2), dtype=np.float32),
        raw_row=np.zeros(15, dtype=np.float32),
    )
    recognizer = MagicMock()
    recognizer.detect_largest.return_value = det
    recognizer.extract_embedding.side_effect = [alice_vec, impostor_vec, alice_vec, alice_vec]
    recognizer.match_against.side_effect = FaceRecognizer.match_against

    verifier = IRFaceVerifier(
        store,
        recognizer=recognizer,
        required_consecutive=2,
        camera_discoverer=lambda: [dummy_cam],
        worker_factory=lambda **_kw: worker,
    )

    res = verifier.verify(timeout_ms=3000)
    assert res["status"] == "matched"
    assert res["username"] == "Alice"
    assert res["domain"] == "."
    assert res["password"] == "WinPassword99"
    assert res["score"] == pytest.approx(1.0)
    assert res["liveness_delta"] > 12.0
    worker.start.assert_called_once()
    worker.stop.assert_called_once()


def test_strict_ir_only_camera_selection(tmp_path: Path) -> None:
    rgb_cam = MagicMock(group="g_rgb", source_id="s_rgb", is_ir=False, is_virtual=False)
    virt_ir = MagicMock(group="g_vir", source_id="s_vir", is_ir=True, is_virtual=True)
    real_ir = MagicMock(group="g_rir", source_id="s_rir", is_ir=True, is_virtual=False)

    assert _select_camera([]) is None
    assert _select_camera([rgb_cam]) is None
    assert _select_camera([rgb_cam, virt_ir]) is virt_ir
    assert _select_camera([rgb_cam, virt_ir, real_ir]) is real_ir

    assert _select_enrollment_camera([]) is None
    assert _select_enrollment_camera([rgb_cam]) is None
    assert _select_enrollment_camera([rgb_cam, virt_ir]) is virt_ir
    assert _select_enrollment_camera([rgb_cam, virt_ir, real_ir]) is real_ir

    # IRFaceVerifier with RGB-only system fails with camera_error
    store = ProfileStore(tmp_path / "profiles.enc")
    vec = np.ones(128, dtype=np.float32) / np.sqrt(128.0)
    store.upsert_embeddings("Alice", [vec])
    verifier = IRFaceVerifier(
        store,
        recognizer=MagicMock(),
        camera_discoverer=lambda: [rgb_cam],
    )
    assert verifier.verify(timeout_ms=1000) == {"status": "failed", "reason": "camera_error"}


def test_ir_face_verifier_require_motion_blocks_static_and_passes_on_motion(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles.enc")
    alice_vec = np.zeros(128, dtype=np.float32)
    alice_vec[0] = 1.0
    store.upsert_embeddings("Alice", [alice_vec], domain=".")

    dummy_cam = MagicMock(group="g1", source_id="s1", is_ir=True, is_virtual=False)
    pair1 = _make_pair(
        face_bright=200,
        face_dark=20,
        bg_bright=5,
        bg_dark=5,
        shape=(360, 640),
        bbox=(220, 90, 160, 180),
        timestamp=1.0,
    )
    pair2 = _make_pair(
        face_bright=200,
        face_dark=20,
        bg_bright=5,
        bg_dark=5,
        shape=(360, 640),
        bbox=(220, 90, 160, 180),
        timestamp=1.2,
    )
    pair3 = _make_pair(
        face_bright=200,
        face_dark=20,
        bg_bright=5,
        bg_dark=5,
        shape=(360, 640),
        bbox=(220, 90, 160, 180),
        timestamp=1.4,
    )

    det_center = _make_face_det(nose=(300.0, 185.0))
    det_turned = _make_face_det(nose=(306.0, 185.0))

    # First verify static sequence fails (times out after exhausting frames)
    worker_static = MagicMock()
    worker_static.error = None
    worker_static.get_ir_pair.side_effect = [pair1, pair2, pair3] + [None] * 50

    rec_static = MagicMock()
    rec_static.detect_largest.return_value = det_center
    rec_static.extract_embedding.return_value = alice_vec
    rec_static.match_against.side_effect = FaceRecognizer.match_against

    verifier_static = IRFaceVerifier(
        store,
        recognizer=rec_static,
        required_consecutive=2,
        require_motion=True,
        camera_discoverer=lambda: [dummy_cam],
        worker_factory=lambda **_kw: worker_static,
    )
    assert verifier_static.verify(timeout_ms=150) == {"status": "failed", "reason": "timeout"}

    # Now verify 3D head motion triggers consecutive matches
    worker_motion = MagicMock()
    worker_motion.error = None
    worker_motion.get_ir_pair.side_effect = [pair1, pair2, pair3]

    rec_motion = MagicMock()
    rec_motion.detect_largest.side_effect = [det_center, det_turned, det_turned]
    rec_motion.extract_embedding.return_value = alice_vec
    rec_motion.match_against.side_effect = FaceRecognizer.match_against

    verifier_motion = IRFaceVerifier(
        store,
        recognizer=rec_motion,
        required_consecutive=2,
        require_motion=True,
        camera_discoverer=lambda: [dummy_cam],
        worker_factory=lambda **_kw: worker_motion,
    )
    res = verifier_motion.verify(timeout_ms=2000)
    assert res["status"] == "matched"
    assert res["username"] == "Alice"


def test_ir_face_verifier_no_enrolled_cancel_and_camera_error(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles.enc")
    verifier = IRFaceVerifier(store, recognizer=MagicMock())
    assert verifier.verify() == {"status": "failed", "reason": "no_enrolled_faces"}

    # Enroll user and test cancel + always calling worker.stop()
    vec = np.ones(128, dtype=np.float32) / np.sqrt(128.0)
    store.upsert_embeddings("Alice", [vec])

    dummy_cam = MagicMock(group="g1", source_id="s1", is_ir=True, is_virtual=False)
    worker = MagicMock()
    worker.error = None
    worker.get_ir_pair.return_value = None

    cancel_evt = threading.Event()
    cancel_evt.set()
    verifier2 = IRFaceVerifier(
        store,
        recognizer=MagicMock(),
        camera_discoverer=lambda: [dummy_cam],
        worker_factory=lambda **_kw: worker,
    )
    assert verifier2.verify(timeout_ms=2000, cancel_event=cancel_evt) == {
        "status": "failed",
        "reason": "cancelled",
    }
    worker.stop.assert_called_once()

    # Worker error path
    worker_err = MagicMock()
    worker_err.error = "Device disconnected"
    verifier3 = IRFaceVerifier(
        store,
        recognizer=MagicMock(),
        camera_discoverer=lambda: [dummy_cam],
        worker_factory=lambda **_kw: worker_err,
    )
    assert verifier3.verify(timeout_ms=2000) == {"status": "failed", "reason": "camera_error"}
    worker_err.stop.assert_called_once()


def test_named_pipe_server_live_roundtrip(tmp_path: Path) -> None:
    import os
    import win32file

    store = ProfileStore(tmp_path / "profiles.enc")
    vec = np.ones(128, dtype=np.float32) / np.sqrt(128.0)
    store.upsert_embeddings("PipeUser", [vec])

    pipe_name = rf"\\.\pipe\WinIRFaceID_UnitTest_{os.getpid()}_{time.time_ns()}"
    server = NamedPipeServer(pipe_name=pipe_name, store=store, service_mode=False)
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()

    try:
        # Wait briefly for pipe instance creation
        handle = None
        for _ in range(30):
            try:
                handle = win32file.CreateFile(
                    pipe_name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
                break
            except Exception:
                time.sleep(0.05)

        assert handle is not None, "Failed to connect to NamedPipeServer"
        try:
            win32file.WriteFile(handle, b'{"cmd":"ping"}\n')
            _, data = win32file.ReadFile(handle, 4096)
            resp = json.loads(data.decode("utf-8").strip())
            assert resp["status"] == "ok"
            assert resp["enrolled_users"] == ["PipeUser"]
        finally:
            win32file.CloseHandle(handle)
    finally:
        server.close()
        srv_thread.join(timeout=2.0)
