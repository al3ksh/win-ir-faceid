"""Unit tests for the WinIRFaceID WebView2 desktop icon, HTTP assets, and Win32 icon helpers."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
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

from engine import REPO_URL, __version__  # noqa: E402
from engine.gui_app import (  # noqa: E402
    _ENROLL_TARGET,
    _MAX_EMBEDDINGS_PER_USER,
    WinIRFaceIDController,
    _JsApiBridge,
    _destroy_hicon,
    _get_window_hwnd,
    _load_hicon,
    _resolve_window_icon_path,
    check_github_latest_release,
    classify_error,
    get_disk_encryption_status,
    is_newer_version,
    parse_semver,
)
from engine.recognizer import FaceDetection  # noqa: E402
from engine.store import ProfileStore, ServiceConfig, save_service_config  # noqa: E402
from engine.ui.generate_app_icon import ICON_SIZES, generate_app_icon  # noqa: E402
from engine.updater import (  # noqa: E402
    download_and_verify_update,
    fetch_latest_release,
    is_allowed_release_url,
    launch_elevated_update,
    parse_sha256sums,
    safe_extract_zip,
)


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
        assert len(prof.embeddings) == 8 + _ENROLL_TARGET
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


def test_classify_error_mappings() -> None:
    assert classify_error(None) is None
    assert classify_error("") is None
    assert classify_error("   ") is None

    samples = [
        ("No IR camera detected.", "no_ir_camera", "No infrared camera detected"),
        ("No camera detected on this system.", "no_ir_camera", "No infrared camera detected"),
        ("Active IR camera is required for enrollment.", "no_ir_camera", "No infrared camera detected"),
        ("WinRT HRESULT 0x80070005: Access is denied.", "camera_privacy_denied", "Camera access blocked by Windows"),
        ("Camera privacy setting disabled", "camera_privacy_denied", "Camera access blocked by Windows"),
        ("Failed to open stream (0xC00D3704)", "camera_in_use", "IR camera is in use"),
        ("Sensor is already open in exclusive mode", "camera_in_use", "IR camera is in use"),
        ("IOCTL_KS_PROPERTY failed for FACEAUTH illuminator", "illuminator_error", "IR illuminator did not respond"),
        ("Face models are not downloaded yet.", "models_error", "Face recognition models unavailable"),
        ("Failed loading ONNX weights", "models_error", "Face recognition models unavailable"),
        ("Unexpected USB disconnect timeout", "sensor_error", "IR sensor error"),
    ]

    for raw, expected_code, expected_title in samples:
        info = classify_error(raw)
        assert info is not None
        assert info["code"] == expected_code
        assert info["title"] == expected_title
        assert isinstance(info["causes"], list) and len(info["causes"]) >= 2
        assert info["detail"] == raw


def test_rescan_cameras_updates_has_ir_and_clears_error(tmp_path: Path) -> None:
    cfg_path = tmp_path / "service_config.json"
    save_service_config(ServiceConfig(), cfg_path)
    store = ProfileStore(tmp_path / "profiles.enc")

    cam_rgb = MagicMock(group="g0", source_id="s0", label="RGB Cam", is_ir=False, is_virtual=False)
    cam_ir = MagicMock(group="g1", source_id="s1", label="IR Cam", is_ir=True, is_virtual=False)
    discovered = [[cam_rgb]]

    ctrl = WinIRFaceIDController(
        store=store,
        config_path=cfg_path,
        models_dir=tmp_path / "models",
        camera_discoverer=lambda: list(discovered[0]),
        service_status_provider=lambda: "not_installed",
        auto_start_server=False,
    )
    bridge = _JsApiBridge(ctrl)

    try:
        boot0 = bridge.get_bootstrap()
        assert boot0["has_ir_camera"] is False
        assert len(boot0["cameras"]) == 1

        # Trigger an IR-required failure via preview/enroll without models/IR
        fail_res = bridge.toggle_live_test()
        assert fail_res["ok"] is False
        assert fail_res["error_info"]["code"] == "models_error"
        assert ctrl.get_state()["error_info"]["code"] == "models_error"

        # Clear error returns cleanly to standby
        cleared = bridge.clear_error()
        assert cleared["ok"] is True
        assert cleared["state"]["error"] is None
        assert cleared["state"]["error_info"] is None

        # Simulate user enabling / plugging in IR sensor and rescanning
        fail_again = bridge.toggle_live_test()
        assert fail_again["ok"] is False
        assert ctrl.get_state()["error_info"] is not None

        discovered[0] = [cam_rgb, cam_ir]
        rescan = bridge.rescan_cameras()
        assert rescan["ok"] is True
        assert rescan["has_ir_camera"] is True
        assert rescan["selected_camera"] == 1
        assert [c["label"] for c in rescan["cameras"]] == ["RGB Cam", "IR Cam"]
        assert rescan["state"]["error"] is None
        assert rescan["state"]["error_info"] is None
        assert bridge.get_bootstrap()["has_ir_camera"] is True
    finally:
        ctrl.shutdown()


def test_worker_error_in_processing_loop_stops_camera_with_error_info(tmp_path: Path) -> None:
    cfg_path = tmp_path / "service_config.json"
    save_service_config(ServiceConfig(), cfg_path)
    store = ProfileStore(tmp_path / "profiles.enc")
    cam_ir = MagicMock(group="g1", source_id="s1", label="IR Cam", is_ir=True, is_virtual=False)

    created_workers: list[MagicMock] = []

    def _make_worker(**_kw: object) -> MagicMock:
        w = MagicMock()
        w.error = "WinRT error 0xC00D3704: camera in use by another app"
        w.get_ir_pair.return_value = None
        w.get_frame.return_value = None
        created_workers.append(w)
        return w

    ctrl = WinIRFaceIDController(
        store=store,
        config_path=cfg_path,
        models_dir=tmp_path / "models",
        camera_discoverer=lambda: [cam_ir],
        worker_factory=_make_worker,
        service_status_provider=lambda: "not_installed",
        auto_start_server=False,
    )
    bridge = _JsApiBridge(ctrl)

    try:
        res = bridge.toggle_preview()
        assert res["ok"] is True
        assert len(created_workers) == 1

        deadline = time.monotonic() + 3.0
        st = ctrl.get_state()
        while time.monotonic() < deadline:
            st = ctrl.get_state()
            if st["mode"] == "standby" and st["error_info"] is not None:
                break
            time.sleep(0.02)

        assert st["mode"] == "standby"
        assert st["camera_active"] is False
        assert st["error"] == "WinRT error 0xC00D3704: camera in use by another app"
        assert st["error_info"] is not None
        assert st["error_info"]["code"] == "camera_in_use"
        assert st["error_info"]["title"] == "IR camera is in use"
        created_workers[0].stop.assert_called_once()
    finally:
        ctrl.shutdown()


def test_parse_semver_and_is_newer_version() -> None:
    assert parse_semver("0.1.0") == (0, 1, 0)
    assert parse_semver("v0.2.0") == (0, 2, 0)
    assert parse_semver("V1.2.3+build42") == (1, 2, 3)
    assert parse_semver("v1.0.0-rc1") == (1, 0, 0)
    assert parse_semver("  0.9.10-beta.2+sha  ") == (0, 9, 10)

    for invalid in ("", "   ", "abc", "1.2", "1.2.3.4", "v1.2.x", "vv0.1.0", None):
        assert parse_semver(invalid) is None  # type: ignore[arg-type]

    assert is_newer_version("v0.2.0", "0.1.0") is True
    assert is_newer_version("0.1.1", "0.1.0") is True
    assert is_newer_version("v1.0.0-rc1", "0.9.0") is True
    assert is_newer_version("0.1.0", "0.1.0") is False
    assert is_newer_version("v0.1.0", "0.2.0") is False
    assert is_newer_version("invalid", "0.1.0") is False
    assert is_newer_version("0.2.0", "invalid") is False


def test_open_external_url_allows_repo_and_rejects_arbitrary_urls(tmp_path: Path) -> None:
    cfg_path = tmp_path / "service_config.json"
    save_service_config(ServiceConfig(), cfg_path)
    store = ProfileStore(tmp_path / "profiles.enc")
    opened_urls: list[str] = []

    ctrl = WinIRFaceIDController(
        store=store,
        config_path=cfg_path,
        models_dir=tmp_path / "models",
        camera_discoverer=lambda: [],
        service_status_provider=lambda: "not_installed",
        url_opener=opened_urls.append,
        auto_start_server=False,
    )
    bridge = _JsApiBridge(ctrl)
    try:
        res_default = bridge.open_external_url()
        assert res_default["ok"] is True
        assert res_default["url"] == REPO_URL

        res_repo = bridge.open_external_url(REPO_URL)
        assert res_repo["ok"] is True
        assert res_repo["url"] == REPO_URL

        release_url = f"{REPO_URL}/releases/tag/v0.2.0"
        res_rel = bridge.open_external_url(release_url)
        assert res_rel["ok"] is True
        assert res_rel["url"] == release_url

        assert opened_urls == [REPO_URL, REPO_URL, release_url]

        for bad_url in (
            "https://evil.example",
            "https://github.com/al3ksh/win-ir-faceid-evil",
            "http://github.com/al3ksh/win-ir-faceid",
            "javascript:alert(1)",
        ):
            bad_res = bridge.open_external_url(bad_url)
            assert bad_res == {"ok": False, "error": "URL not allowed."}

        assert len(opened_urls) == 3
    finally:
        ctrl.shutdown()


def test_bootstrap_version_and_injected_check_for_updates(tmp_path: Path) -> None:
    cfg_path = tmp_path / "service_config.json"
    save_service_config(ServiceConfig(), cfg_path)
    store = ProfileStore(tmp_path / "profiles.enc")

    mock_payload = {
        "ok": True,
        "current_version": __version__,
        "latest_version": "0.2.0",
        "update_available": True,
        "release_url": f"{REPO_URL}/releases/tag/v0.2.0",
        "error": None,
    }
    checker = MagicMock(return_value=mock_payload)

    ctrl = WinIRFaceIDController(
        store=store,
        config_path=cfg_path,
        models_dir=tmp_path / "models",
        camera_discoverer=lambda: [],
        service_status_provider=lambda: "not_installed",
        update_checker=checker,
        auto_start_server=False,
    )
    bridge = _JsApiBridge(ctrl)
    try:
        boot = bridge.get_bootstrap()
        assert boot["version"] == __version__
        assert boot["repo_url"] == REPO_URL
        checker.assert_not_called()

        res = bridge.check_for_updates()
        assert res == mock_payload
        checker.assert_called_once()
    finally:
        ctrl.shutdown()


def test_check_github_latest_release_handles_200_404_and_network_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResp:
        def __init__(self, status: int, body: dict[str, object]) -> None:
            self.status = status
            self._raw = json.dumps(body).encode("utf-8")

        def read(self) -> bytes:
            return self._raw

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    # HTTP 200 with newer release and valid html_url
    monkeypatch.setattr(
        "engine.gui_app.urllib.request.urlopen",
        lambda req, timeout=4.0: _FakeResp(
            200,
            {
                "tag_name": "v0.2.0",
                "html_url": f"{REPO_URL}/releases/tag/v0.2.0",
            },
        ),
    )
    ok_res = check_github_latest_release(current_version="0.1.0")
    assert ok_res["ok"] is True
    assert ok_res["current_version"] == "0.1.0"
    assert ok_res["latest_version"] == "0.2.0"
    assert ok_res["update_available"] is True
    assert ok_res["release_url"] == f"{REPO_URL}/releases/tag/v0.2.0"
    assert ok_res["no_releases"] is False
    assert ok_res["error"] is None

    # HTTP 200 with untrusted html_url falls back to REPO_URL/releases
    monkeypatch.setattr(
        "engine.gui_app.urllib.request.urlopen",
        lambda req, timeout=4.0: _FakeResp(
            200,
            {
                "tag_name": "v0.1.0",
                "html_url": "https://evil.example/phish",
            },
        ),
    )
    same_res = check_github_latest_release(current_version="0.1.0")
    assert same_res["ok"] is True
    assert same_res["update_available"] is False
    assert same_res["release_url"] == f"{REPO_URL}/releases"

    # HTTP 404 returns no_releases
    def _raise_404(req: object, timeout: float = 4.0) -> object:
        raise urllib.error.HTTPError(str(req), 404, "Not Found", {}, io.BytesIO(b""))

    monkeypatch.setattr("engine.gui_app.urllib.request.urlopen", _raise_404)
    not_found = check_github_latest_release(current_version="0.1.0")
    assert not_found["ok"] is True
    assert not_found["current_version"] == "0.1.0"
    assert not_found["latest_version"] == "0.1.0"
    assert not_found["update_available"] is False
    assert not_found["release_url"] == f"{REPO_URL}/releases"
    assert not_found["no_releases"] is True
    assert not_found["error"] is None

    # Network error returns clean failure payload
    def _raise_net(req: object, timeout: float = 4.0) -> object:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("engine.gui_app.urllib.request.urlopen", _raise_net)
    err_res = check_github_latest_release(current_version="0.1.0")
    assert err_res["ok"] is False
    assert err_res["current_version"] == "0.1.0"
    assert err_res["latest_version"] == ""
    assert err_res["update_available"] is False
    assert err_res["release_url"] == f"{REPO_URL}/releases"
    assert err_res["error"] == "Unable to reach GitHub."


def test_get_disk_encryption_status_mappings_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"init": 0, "uninit": 0}
    prop_value: list[object] = [None]
    raise_on_dispatch = [False]
    namespace_none = [False]

    class _FakeSelf:
        def ExtendedProperty(self, name: str) -> object:  # noqa: N802
            assert name == "System.Volume.BitLockerProtection"
            return prop_value[0]

    class _FakeNamespace:
        Self = _FakeSelf()

    class _FakeShell:
        def NameSpace(self, drive: str) -> object:  # noqa: N802
            assert drive == "C:\\"
            if namespace_none[0]:
                return None
            return _FakeNamespace()

    fake_pythoncom = SimpleNamespace(
        CoInitialize=lambda: calls.__setitem__("init", calls["init"] + 1),
        CoUninitialize=lambda: calls.__setitem__("uninit", calls["uninit"] + 1),
    )

    def _fake_dispatch(prog_id: str) -> object:
        assert prog_id == "Shell.Application"
        if raise_on_dispatch[0]:
            raise RuntimeError("COM failure")
        return _FakeShell()

    fake_win32com_client = SimpleNamespace(Dispatch=_fake_dispatch)
    fake_win32com = SimpleNamespace(client=fake_win32com_client)
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_win32com_client)

    for code in (1, 3, 6):
        prop_value[0] = code
        assert get_disk_encryption_status() == {
            "status": "encrypted",
            "code": code,
            "detail": "protected",
        }

    for code in (2, 4):
        prop_value[0] = code
        assert get_disk_encryption_status() == {
            "status": "unencrypted",
            "code": code,
            "detail": "off",
        }

    prop_value[0] = 5
    assert get_disk_encryption_status() == {
        "status": "unencrypted",
        "code": 5,
        "detail": "suspended",
    }

    for code in (7, 8):
        prop_value[0] = code
        assert get_disk_encryption_status() == {
            "status": "unencrypted",
            "code": code,
            "detail": "waiting_activation",
        }

    for bad_val in (None, 0, 99, True, "bad", 1.5):
        prop_value[0] = bad_val
        assert get_disk_encryption_status() == {
            "status": "unknown",
            "code": None,
            "detail": "unknown",
        }

    namespace_none[0] = True
    assert get_disk_encryption_status() == {
        "status": "unknown",
        "code": None,
        "detail": "unknown",
    }
    namespace_none[0] = False

    raise_on_dispatch[0] = True
    assert get_disk_encryption_status() == {
        "status": "unknown",
        "code": None,
        "detail": "unknown",
    }
    assert calls["init"] == calls["uninit"] and calls["init"] > 0


def test_bootstrap_disk_encryption_and_open_settings(tmp_path: Path) -> None:
    cfg_path = tmp_path / "service_config.json"
    save_service_config(ServiceConfig(), cfg_path)
    store = ProfileStore(tmp_path / "profiles.enc")
    opened_uris: list[str] = []

    fake_status = {"status": "unencrypted", "code": 2, "detail": "off"}
    ctrl = WinIRFaceIDController(
        store=store,
        config_path=cfg_path,
        models_dir=tmp_path / "models",
        camera_discoverer=lambda: [],
        service_status_provider=lambda: "not_installed",
        disk_encryption_provider=lambda: fake_status,
        settings_opener=opened_uris.append,
        auto_start_server=False,
    )
    bridge = _JsApiBridge(ctrl)
    try:
        boot = bridge.get_bootstrap()
        assert boot["disk_encryption"] == fake_status

        res = bridge.open_disk_encryption_settings()
        assert res == {"ok": True, "uri": "ms-settings:deviceencryption", "error": None}
        assert opened_uris == ["ms-settings:deviceencryption"]
    finally:
        ctrl.shutdown()


def test_enroll_state_exposes_just_captured_pause_beat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("engine.gui_app._ENROLL_MIN_INTERVAL", 0.06)
    monkeypatch.setattr("engine.gui_app._ENROLL_CAPTURE_PAUSE", 0.18)

    store = ProfileStore(tmp_path / "profiles.enc")
    cfg_path = tmp_path / "service_config.json"
    save_service_config(ServiceConfig(), cfg_path)

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "face_detection_yunet_2023mar.onnx").write_bytes(b"y" * 2048)
    (models_dir / "face_recognition_sface_2021dec.onnx").write_bytes(b"s" * 2048)

    cam_ir = MagicMock(group="g1", source_id="s1", label="IR Cam", is_ir=True, is_virtual=False)
    ts_counter = [1.0]

    def _make_worker(**_kw: object) -> MagicMock:
        w = MagicMock()
        w.error = None

        def _next_pair() -> _DummyPair:
            ts_counter[0] += 1.0
            return _make_active_ir_pair(ts_counter[0])

        w.get_ir_pair.side_effect = _next_pair
        w.get_frame.return_value = _make_active_ir_pair(0.0).diff_bgr
        return w

    vec = np.zeros(128, dtype=np.float32)
    vec[0] = 1.0
    mock_rec = MagicMock()
    mock_rec.detect_largest.return_value = _make_det()
    mock_rec.extract_embedding.return_value = vec

    ctrl = WinIRFaceIDController(
        store=store,
        config_path=cfg_path,
        models_dir=models_dir,
        camera_discoverer=lambda: [cam_ir],
        worker_factory=_make_worker,
        recognizer_factory=lambda: mock_rec,
        service_status_provider=lambda: "not_installed",
        disk_encryption_provider=lambda: {"status": "encrypted", "code": 1, "detail": "protected"},
        auto_start_server=False,
    )
    bridge = _JsApiBridge(ctrl)
    try:
        assert ctrl.get_state()["enroll"]["just_captured"] is False
        res = bridge.start_enroll("Bob", False)
        assert res["ok"] is True
        assert res["state"]["enroll"]["just_captured"] is False

        saw_just_captured = False
        saw_post_pause_resume = False
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            st = ctrl.get_state()
            enr = st["enroll"]
            if st["mode"] == "enroll":
                if enr.get("just_captured") is True and enr["count"] >= 1:
                    saw_just_captured = True
                elif saw_just_captured and enr.get("just_captured") is False:
                    saw_post_pause_resume = True
            if st["mode"] == "standby" and enr["completed_user"] == "Bob":
                break
            time.sleep(0.015)

        final_st = ctrl.get_state()
        assert final_st["mode"] == "standby"
        assert final_st["enroll"]["completed_user"] == "Bob"
        assert final_st["enroll"]["just_captured"] is False
        assert saw_just_captured is True
        assert saw_post_pause_resume is True
    finally:
        ctrl.shutdown()


def test_parse_sha256sums_and_is_allowed_release_url() -> None:
    h1 = "A" * 64
    h2 = "0123456789abcdef" * 4
    manifest = (
        f"# comment\n"
        f"{h1}  WinIRFaceID-Update-v0.3.0-win64.zip\n"
        f"{h2} *./SHA256SUMS.txt\n"
        f"not-a-hash  broken.zip\n\n"
    )
    parsed = parse_sha256sums(manifest)
    assert parsed == {
        "WinIRFaceID-Update-v0.3.0-win64.zip": "a" * 64,
        "SHA256SUMS.txt": h2,
    }

    assert is_allowed_release_url(f"{REPO_URL}/releases/download/v0.3.0/WinIRFaceID-Update-v0.3.0-win64.zip") is True
    assert is_allowed_release_url("https://objects.githubusercontent.com/github-production-release-asset/foo") is True
    assert is_allowed_release_url("https://release-assets.githubusercontent.com/github-production-release-asset/bar") is True

    for bad in (
        "",
        "http://github.com/al3ksh/win-ir-faceid/releases",
        "https://github.com/al3ksh/win-ir-faceid-evil/releases",
        "https://github.com/other/repo/releases",
        "https://user@github.com/al3ksh/win-ir-faceid/releases",
        "https://github.com:8443/al3ksh/win-ir-faceid/releases",
        "https://evil.example/objects.githubusercontent.com/a.zip",
        "javascript:alert(1)",
    ):
        assert is_allowed_release_url(bad) is False


def test_safe_extract_zip_valid_and_rejects_traversal_and_symlinks(tmp_path: Path) -> None:
    good_zip = tmp_path / "good.zip"
    with zipfile.ZipFile(good_zip, "w") as zf:
        zf.writestr("engine/__init__.py", "__version__ = '0.3.0'\n")
        zf.writestr("scripts/apply_update.ps1", "Write-Host 'ok'\n")

    dest_good = tmp_path / "out_good"
    safe_extract_zip(good_zip, dest_good)
    assert (dest_good / "engine" / "__init__.py").is_file()
    assert (dest_good / "scripts" / "apply_update.ps1").is_file()

    for bad_name in ("../evil.txt", "..\\evil.txt", "/abs/evil.txt", "C:\\evil.txt", "sub/../../evil.txt"):
        bad_zip = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr(bad_name, "pwned")
        with pytest.raises(ValueError):
            safe_extract_zip(bad_zip, tmp_path / "out_bad")
        assert not (tmp_path / "evil.txt").exists()

    symlink_zip = tmp_path / "symlink.zip"
    with zipfile.ZipFile(symlink_zip, "w") as zf:
        info = zipfile.ZipInfo("link.txt")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        safe_extract_zip(symlink_zip, tmp_path / "out_sym")


def test_download_and_verify_update_passes_and_deletes_on_tampered_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_zip = tmp_path / "source.zip"
    with zipfile.ZipFile(payload_zip, "w") as zf:
        zf.writestr("engine/__init__.py", '__version__ = "0.3.0"\n')
        zf.writestr("scripts/apply_update.ps1", "exit 0\n")
    zip_bytes = payload_zip.read_bytes()
    valid_hash = hashlib.sha256(zip_bytes).hexdigest()

    zip_name = "WinIRFaceID-Update-v0.3.0-win64.zip"
    zip_url = f"{REPO_URL}/releases/download/v0.3.0/{zip_name}"
    sums_url = f"{REPO_URL}/releases/download/v0.3.0/SHA256SUMS.txt"

    class _FakeStreamResp:
        def __init__(self, data: bytes) -> None:
            self._bio = io.BytesIO(data)
            self.headers = {"Content-Length": str(len(data))}

        def read(self, n: int = -1) -> bytes:
            return self._bio.read(n)

        def __enter__(self) -> _FakeStreamResp:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    sums_body = [f"{valid_hash}  {zip_name}\n".encode("utf-8")]

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 20.0) -> _FakeStreamResp:
        url = req.full_url
        if url == sums_url:
            return _FakeStreamResp(sums_body[0])
        if url == zip_url:
            return _FakeStreamResp(zip_bytes)
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("engine.updater.urllib.request.urlopen", _fake_urlopen)

    progress_events: list[tuple[int, int | None]] = []
    work_ok = tmp_path / "work_ok"
    extracted = download_and_verify_update(
        zip_url,
        sums_url,
        zip_name,
        work_ok,
        progress_cb=lambda d, t: progress_events.append((d, t)),
    )
    assert extracted == work_ok / "extracted"
    assert (extracted / "scripts" / "apply_update.ps1").is_file()
    assert progress_events and progress_events[-1] == (len(zip_bytes), len(zip_bytes))

    # Tampered SHA-256 must fail AND delete the downloaded zip
    sums_body[0] = f"{'0' * 64}  {zip_name}\n".encode("utf-8")
    work_bad = tmp_path / "work_bad"
    with pytest.raises(ValueError, match="SHA-256 verification failed"):
        download_and_verify_update(zip_url, sums_url, zip_name, work_bad)
    assert not (work_bad / zip_name).exists()
    assert not (work_bad / "extracted").exists()


def test_fetch_latest_release_parses_delta_assets_and_git_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zip_name = "WinIRFaceID-Update-v0.3.0-win64.zip"
    zip_url = f"{REPO_URL}/releases/download/v0.3.0/{zip_name}"
    sums_url = f"{REPO_URL}/releases/download/v0.3.0/SHA256SUMS.txt"
    long_notes = "  Release highlights\r\n" + ("x" * 2500)

    class _FakeResp:
        status = 200

        def read(self) -> bytes:
            return json.dumps(
                {
                    "tag_name": "v0.3.0",
                    "html_url": f"{REPO_URL}/releases/tag/v0.3.0",
                    "body": long_notes,
                    "assets": [
                        {"name": "WinIRFaceID-Setup-v0.3.0-x64.exe", "browser_download_url": f"{REPO_URL}/releases/download/v0.3.0/setup.exe", "size": 120_000_000},
                        {"name": zip_name, "browser_download_url": zip_url, "size": 348_160},
                        {"name": "SHA256SUMS.txt", "browser_download_url": sums_url, "size": 256},
                    ],
                }
            ).encode("utf-8")

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr("engine.updater.urllib.request.urlopen", lambda req, timeout=4.0: _FakeResp())

    install_dir = tmp_path / "installed_app"
    install_dir.mkdir()
    res_install = fetch_latest_release(current_version="0.2.3", app_root=install_dir)
    assert res_install["ok"] is True
    assert res_install["update_available"] is True
    assert res_install["update_asset"] == {"name": zip_name, "url": zip_url, "size": 348_160}
    assert res_install["checksums_url"] == sums_url
    assert res_install["is_git_checkout"] is False
    assert res_install["can_auto_update"] is True
    assert len(res_install["release_notes"]) <= 2000
    assert res_install["release_notes"].startswith("Release highlights")

    git_dir = tmp_path / "git_repo"
    (git_dir / ".git").mkdir(parents=True)
    res_git = fetch_latest_release(current_version="0.2.3", app_root=git_dir)
    assert res_git["is_git_checkout"] is True
    assert res_git["can_auto_update"] is False


def test_git_checkout_guard_and_controller_install_update(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    (extracted / "scripts").mkdir(parents=True)
    (extracted / "scripts" / "apply_update.ps1").write_text("exit 0\n", encoding="utf-8")

    git_target = tmp_path / "dev_repo"
    (git_target / ".git").mkdir(parents=True)
    called_shell: list[object] = []
    guard_res = launch_elevated_update(
        extracted,
        git_target,
        tmp_path / "cleanup",
        1234,
        shell_execute=lambda *args: called_shell.append(args) or 42,
    )
    assert guard_res["ok"] is False
    assert "git checkout" in str(guard_res["error"]).lower()
    assert called_shell == []

    # Controller refuses install_update inside a git checkout
    cfg_path = tmp_path / "service_config.json"
    save_service_config(ServiceConfig(), cfg_path)
    store = ProfileStore(tmp_path / "profiles.enc")
    dl_mock = MagicMock()
    launch_mock = MagicMock()
    ctrl_git = WinIRFaceIDController(
        store=store,
        config_path=cfg_path,
        models_dir=tmp_path / "models",
        app_root=git_target,
        camera_discoverer=lambda: [],
        service_status_provider=lambda: "not_installed",
        update_checker=lambda: {
            "ok": True,
            "current_version": "0.2.3",
            "latest_version": "0.3.0",
            "update_available": True,
            "release_url": f"{REPO_URL}/releases/tag/v0.3.0",
            "release_notes": "Notes",
            "update_asset": {"name": "WinIRFaceID-Update-v0.3.0-win64.zip", "url": f"{REPO_URL}/releases/download/v0.3.0/WinIRFaceID-Update-v0.3.0-win64.zip", "size": 340000},
            "checksums_url": f"{REPO_URL}/releases/download/v0.3.0/SHA256SUMS.txt",
            "can_auto_update": True,
            "is_git_checkout": False,
            "no_releases": False,
            "error": None,
        },
        update_downloader=dl_mock,
        update_launcher=launch_mock,
        auto_start_server=False,
    )
    try:
        refused = ctrl_git.install_update()
        assert refused["ok"] is False
        assert "git checkout" in refused["error"].lower()
        dl_mock.assert_not_called()
        launch_mock.assert_not_called()
    finally:
        ctrl_git.shutdown()

    # Non-git install dir succeeds and schedules close_callback
    prod_target = tmp_path / "prod_install"
    prod_target.mkdir()
    closed_event = []
    ctrl_prod = WinIRFaceIDController(
        store=store,
        config_path=cfg_path,
        models_dir=tmp_path / "models",
        app_root=prod_target,
        camera_discoverer=lambda: [],
        service_status_provider=lambda: "not_installed",
        update_checker=lambda: {
            "ok": True,
            "current_version": "0.2.3",
            "latest_version": "0.3.0",
            "update_available": True,
            "release_url": f"{REPO_URL}/releases/tag/v0.3.0",
            "release_notes": "Notes",
            "update_asset": {"name": "WinIRFaceID-Update-v0.3.0-win64.zip", "url": f"{REPO_URL}/releases/download/v0.3.0/WinIRFaceID-Update-v0.3.0-win64.zip", "size": 340000},
            "checksums_url": f"{REPO_URL}/releases/download/v0.3.0/SHA256SUMS.txt",
            "can_auto_update": True,
            "is_git_checkout": False,
            "no_releases": False,
            "error": None,
        },
        update_downloader=lambda *_a, **_kw: extracted,
        update_launcher=lambda *_a, **_kw: {"ok": True, "code": 42, "error": None},
        auto_start_server=False,
    )
    bridge = _JsApiBridge(ctrl_prod)
    try:
        ctrl_prod.set_close_callback(lambda: closed_event.append(True))
        start_res = bridge.install_update()
        assert start_res["ok"] is True
        assert start_res["started"] is True

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if closed_event and ctrl_prod.get_state()["update"]["ready_to_close"] is True:
                break
            time.sleep(0.02)

        upd_st = ctrl_prod.get_state()["update"]
        assert upd_st["active"] is False
        assert upd_st["ready_to_close"] is True
        assert upd_st["error"] is None
        assert closed_event == [True]
    finally:
        ctrl_prod.shutdown()
