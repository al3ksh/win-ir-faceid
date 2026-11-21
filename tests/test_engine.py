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
from engine.ipc_server import (
    IRFaceVerifier,
    NamedPipeServer,
    handle_raw_message,
    handle_request,
)
from engine.liveness import check_ir_liveness
from engine.models import ModelPaths, get_model_paths, models_exist
from engine.recognizer import FaceDetection, FaceRecognizer, enhance_ir_bgr
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
    face_bright: int = 130,
    face_dark: int = 50,
    bg_bright: int = 40,
    bg_dark: int = 35,
    bright_mean_override: float | None = None,
    illuminator_active: bool = True,
    with_dark: bool = True,
    timestamp: float = 1.0,
) -> DummyIRFramePair:
    bright = np.full((120, 120, 3), bg_bright, dtype=np.uint8)
    bright[20:80, 20:80] = face_bright

    if with_dark:
        dark = np.full((120, 120, 3), bg_dark, dtype=np.uint8)
        dark[20:80, 20:80] = face_dark
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
    pair = _make_pair(face_bright=140, face_dark=45)
    res = check_ir_liveness(pair, (20, 20, 60, 60))
    assert res.passed is True
    assert res.reason == "ok"
    assert res.delta == pytest.approx(95.0)
    assert res.ratio == pytest.approx(140.0 / 45.0)


def test_liveness_rejects_phone_screen_low_delta() -> None:
    # Phone OLED/LCD screen emits constant light with virtually no 850/940nm IR reflection delta
    pair = _make_pair(face_bright=92, face_dark=88)
    res = check_ir_liveness(pair, (20, 20, 60, 60))
    assert res.passed is False
    assert res.reason == "low_ir_delta"
    assert res.delta == pytest.approx(4.0)


def test_liveness_rejects_low_ratio_under_high_ambient() -> None:
    pair = _make_pair(face_bright=135, face_dark=115)
    res = check_ir_liveness(pair, (20, 20, 60, 60))
    assert res.passed is False
    assert res.reason == "low_ir_ratio"
    assert res.delta == pytest.approx(20.0)
    assert res.ratio < 1.30


def test_liveness_rejects_flashbang_overexposure() -> None:
    pair_face_hot = _make_pair(face_bright=248, face_dark=60, bright_mean_override=120.0)
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
    matched_empty, score_empty = FaceRecognizer.match_against(v1, [], threshold=0.42)
    assert matched_empty is False
    assert score_empty == 0.0

    # Above threshold
    close = v1.copy()
    close[1] = 0.5
    close /= np.linalg.norm(close)
    matched, score = FaceRecognizer.match_against(close, [v2, v1], threshold=0.42)
    assert matched is True
    assert score > 0.85

    # Below threshold
    far = np.zeros(128, dtype=np.float32)
    far[1] = 0.99
    far[0] = 0.1
    far /= np.linalg.norm(far)
    matched_low, score_low = FaceRecognizer.match_against(far, [v1], threshold=0.42)
    assert matched_low is False
    assert score_low < 0.42


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
        _make_pair(face_bright=150, face_dark=40, timestamp=1.0),
        _make_pair(face_bright=150, face_dark=40, timestamp=2.0),
        _make_pair(face_bright=150, face_dark=40, timestamp=3.0),
        _make_pair(face_bright=150, face_dark=40, timestamp=4.0),
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
    assert res["liveness_delta"] == pytest.approx(110.0)
    worker.start.assert_called_once()
    worker.stop.assert_called_once()


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
