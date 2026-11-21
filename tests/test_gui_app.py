"""Unit tests for the WinIRFaceID WebView2 desktop icon, HTTP assets, and Win32 icon helpers."""

from __future__ import annotations

import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from engine.gui_app import (  # noqa: E402
    _MAX_EMBEDDINGS_PER_USER,
    WinIRFaceIDController,
    _JsApiBridge,
    _destroy_hicon,
    _get_window_hwnd,
    _load_hicon,
    _resolve_window_icon_path,
)
from engine.recognizer import FaceDetection  # noqa: E402
from engine.store import ProfileStore, ServiceConfig, save_service_config  # noqa: E402
from engine.ui.generate_app_icon import ICON_SIZES, generate_app_icon  # noqa: E402


def test_app_ico_exists_and_contains_multiresolution_sizes(tmp_path: Path) -> None:
    ico_path = _REPO_ROOT / "engine" / "ui" / "app.ico"
    assert ico_path.is_file()

    with Image.open(ico_path) as img:
        assert img.format == "ICO"
        sizes = set(img.info.get("sizes", set()))
        for required in (16, 24, 32, 48, 64, 128, 256):
            assert (required, required) in sizes

    # Verify generator produces a valid multi-size ICO into a fresh path as well
    out_ico = generate_app_icon(tmp_path / "generated.ico")
    assert out_ico.is_file()
    with Image.open(out_ico) as gen_img:
        assert gen_img.format == "ICO"
        assert set(gen_img.info.get("sizes", set())) == {(s, s) for s in ICON_SIZES}


def test_local_http_server_serves_app_ico_and_favicon(tmp_path: Path) -> None:
    cfg_path = tmp_path / "service_config.json"
    save_service_config(ServiceConfig(), cfg_path)
    store = ProfileStore(tmp_path / "profiles.enc")

    ctrl = WinIRFaceIDController(
        store=store,
        config_path=cfg_path,
        models_dir=tmp_path / "models",
        camera_discoverer=lambda: [],
        service_status_provider=lambda: "not_installed",
    )
    try:
        base = ctrl.ui_url.rstrip("/")
        expected_bytes = (_REPO_ROOT / "engine" / "ui" / "app.ico").read_bytes()

        for route in ("/app.ico", "/favicon.ico"):
            with urllib.request.urlopen(f"{base}{route}", timeout=3.0) as resp:
                assert resp.status == 200
                ctype = resp.headers.get("Content-Type", "")
                assert ctype.split(";")[0].strip().lower() == "image/x-icon"
                assert resp.read() == expected_bytes

        with urllib.request.urlopen(f"{base}/index.html", timeout=3.0) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
            assert 'href="/app.ico"' in html
    finally:
        ctrl.shutdown()


def test_resolve_and_load_hicon_on_windows() -> None:
    resolved = _resolve_window_icon_path()
    assert resolved is not None
    assert Path(resolved).resolve() == (_REPO_ROOT / "engine" / "ui" / "app.ico").resolve()

    if sys.platform != "win32":
        pytest.skip("Win32 LoadImageW / DestroyIcon only run on Windows")

    for size in (16, 32):
        hicon = _load_hicon(resolved, size, size)
        assert hicon is not None and hicon > 0
        assert _destroy_hicon(hicon) is True


def test_get_window_hwnd_extracts_int64_and_handles_missing() -> None:
    class _HandleObj:
        def ToInt64(self) -> int:  # noqa: N802
            return 0x123456789ABC

    win = SimpleNamespace(native=SimpleNamespace(Handle=_HandleObj()))
    assert _get_window_hwnd(win) == 0x123456789ABC
    assert _get_window_hwnd(SimpleNamespace(native=None)) == 0
    assert _get_window_hwnd(None) == 0


@dataclass
class _DummyPair:
    bright_bgr: np.ndarray
    dark_bgr: np.ndarray
    diff_bgr: np.ndarray
    bright_mean: float
    dark_mean: float
    illuminator_active: bool
    timestamp: float


def _make_active_ir_pair(timestamp: float) -> _DummyPair:
    h_img, w_img = 360, 640
    x, y, w, h = 220, 90, 160, 180
    bright = np.full((h_img, w_img, 3), 5, dtype=np.uint8)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = (xx - (w - 1) / 2.0) / max(w / 2.0, 1.0)
    ny = (yy - (h - 1) / 2.0) / max(h / 2.0, 1.0)
    r2 = np.clip(0.5 * (nx * nx + ny * ny), 0.0, 1.0)
    shading = 1.0 - 0.70 * r2
    face_b = np.clip(25.0 + (200.0 - 25.0) * shading, 0, 255).astype(np.uint8)
    bright[y : y + h, x : x + w] = face_b[:, :, None]

    dark = np.full((h_img, w_img, 3), 5, dtype=np.uint8)
    dark[y : y + h, x : x + w] = 25
    diff = np.clip(bright.astype(np.int16) - dark.astype(np.int16), 0, 255).astype(np.uint8)
    return _DummyPair(
        bright_bgr=bright,
        dark_bgr=dark,
        diff_bgr=diff,
        bright_mean=float(np.mean(bright)),
        dark_mean=float(np.mean(dark)),
        illuminator_active=True,
        timestamp=timestamp,
    )


def _make_det() -> FaceDetection:
    bbox = (220, 90, 160, 180)
    landmarks = np.array(
        [
            (270.0, 150.0),
            (330.0, 150.0),
            (300.0, 185.0),
            (275.0, 225.0),
            (325.0, 225.0),
        ],
        dtype=np.float32,
    )
    raw = np.zeros(15, dtype=np.float32)
    raw[0:4] = bbox
    raw[4:14] = landmarks.reshape(-1)
    raw[14] = 0.95
    return FaceDetection(bbox=bbox, score=0.95, landmarks=landmarks, raw_row=raw)


def test_start_enroll_append_adds_appearance_and_enforces_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("engine.gui_app._ENROLL_MIN_INTERVAL", 0.0)

    store = ProfileStore(tmp_path / "profiles.enc")
    initial_vecs = []
    for i in range(8):
        vec = np.zeros(128, dtype=np.float32)
        vec[i] = 1.0
        initial_vecs.append(vec)
    store.upsert_embeddings("Alice", initial_vecs, domain=".")

    cfg_path = tmp_path / "service_config.json"
    save_service_config(ServiceConfig(), cfg_path)

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "face_detection_yunet_2023mar.onnx").write_bytes(b"y" * 2048)
    (models_dir / "face_recognition_sface_2021dec.onnx").write_bytes(b"s" * 2048)

    cam_ir = MagicMock(group="g1", source_id="s1", label="IR Cam", is_ir=True, is_virtual=False)
    created_workers: list[MagicMock] = []
    ts_counter = [10.0]

    def _make_worker(**_kw: object) -> MagicMock:
        w = MagicMock()
        w.error = None

        def _next_pair() -> _DummyPair:
            ts_counter[0] += 1.0
            return _make_active_ir_pair(ts_counter[0])

        w.get_ir_pair.side_effect = _next_pair
        w.get_frame.return_value = _make_active_ir_pair(0.0).diff_bgr
        created_workers.append(w)
        return w

    new_vec = np.zeros(128, dtype=np.float32)
    new_vec[64] = 1.0
    mock_rec = MagicMock()
    mock_rec.detect_largest.return_value = _make_det()
    mock_rec.extract_embedding.return_value = new_vec

    ctrl = WinIRFaceIDController(
        store=store,
        config_path=cfg_path,
        models_dir=models_dir,
        camera_discoverer=lambda: [cam_ir],
        worker_factory=_make_worker,
        recognizer_factory=lambda: mock_rec,
        service_status_provider=lambda: "not_installed",
        auto_start_server=False,
    )
    bridge = _JsApiBridge(ctrl)

    try:
        res = bridge.start_enroll("Alice", True)
        assert res["ok"] is True
        assert res["state"]["enroll"]["append"] is True
        assert len(created_workers) == 1

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            st = ctrl.get_state()
            if st["mode"] == "standby" and st["enroll"]["completed_user"] == "Alice":
                break
            time.sleep(0.02)

        final_st = ctrl.get_state()
        assert final_st["mode"] == "standby"
        assert final_st["camera_active"] is False
        assert final_st["enroll"]["completed_user"] == "Alice"
        assert final_st["enroll"]["completed_append"] is True

        prof = store.get_user("Alice")
        assert prof is not None
        assert len(prof.embeddings) == 16
        assert prof.embeddings[0][0] == pytest.approx(1.0)
        assert prof.embeddings[8][64] == pytest.approx(1.0)

        # Fill up to _MAX_EMBEDDINGS_PER_USER and verify append=True blocks without starting camera
        extra_needed = _MAX_EMBEDDINGS_PER_USER - len(prof.embeddings)
        store.upsert_embeddings("Alice", [new_vec] * extra_needed, append=True)
        prof_full = store.get_user("Alice")
        assert prof_full is not None
        assert len(prof_full.embeddings) == _MAX_EMBEDDINGS_PER_USER

        worker_count_before = len(created_workers)
        cap_res = bridge.start_enroll("Alice", True)
        assert cap_res["ok"] is False
        assert f"Maximum appearances reached ({_MAX_EMBEDDINGS_PER_USER} samples)" in cap_res["error"]
        assert len(created_workers) == worker_count_before
        assert ctrl.get_state()["mode"] == "standby"
    finally:
        ctrl.shutdown()
