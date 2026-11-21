"""Unit tests for the WinIRFaceID WebView2 desktop icon, HTTP assets, and Win32 icon helpers."""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from engine.gui_app import (  # noqa: E402
    WinIRFaceIDController,
    _destroy_hicon,
    _get_window_hwnd,
    _load_hicon,
    _resolve_window_icon_path,
)
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
