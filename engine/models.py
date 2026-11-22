"""Download and manage official OpenCV Zoo ONNX face models."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple

from engine import __version__

logger = logging.getLogger(__name__)

YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"
SFACE_FILENAME = "face_recognition_sface_2021dec.onnx"

YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
SFACE_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx"
)

# Pinned SHA-256 digests: a tampered or swapped model (e.g. a compromised mirror) must
# never be loaded by the LocalSystem service that unlocks the machine.
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"

_MIN_VALID_BYTES = 1024
_CHUNK_SIZE = 64 * 1024


class ModelPaths(NamedTuple):
    """Resolved filesystem paths for the YuNet and SFace ONNX models."""

    yunet: Path
    sface: Path


def get_default_models_dir() -> Path:
    """Return the default ``<repo>/models`` directory path."""
    return Path(__file__).resolve().parent.parent / "models"


def get_model_paths(models_dir: Path | str | None = None) -> ModelPaths:
    """Return expected YuNet and SFace file paths without downloading."""
    base = Path(models_dir) if models_dir is not None else get_default_models_dir()
    return ModelPaths(yunet=base / YUNET_FILENAME, sface=base / SFACE_FILENAME)


def models_exist(models_dir: Path | str | None = None) -> bool:
    """Return True if both ONNX model files exist and are non-empty."""
    paths = get_model_paths(models_dir)
    return (
        paths.yunet.is_file()
        and paths.yunet.stat().st_size > _MIN_VALID_BYTES
        and paths.sface.is_file()
        and paths.sface.stat().st_size > _MIN_VALID_BYTES
    )


def _download_file(
    url: str,
    dest: Path,
    progress_callback: Callable[[str, int, int | None], None] | None = None,
    timeout: float = 60.0,
    expected_sha256: str | None = None,
) -> Path:
    """Download *url* atomically to *dest*, verifying size and (optionally) SHA-256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"WinIRFaceID/{__version__}"},
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.",
        suffix=".tmp",
        dir=str(dest.parent),
    )
    tmp_path = Path(tmp_name)
    downloaded = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as out_file:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                if status is not None and status >= 400:
                    raise RuntimeError(f"HTTP {status} downloading {url}")
                content_len_hdr = resp.headers.get("Content-Length")
                total_bytes = int(content_len_hdr) if content_len_hdr and content_len_hdr.isdigit() else None

                while True:
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if progress_callback is not None:
                        progress_callback(dest.name, downloaded, total_bytes)

        if downloaded <= _MIN_VALID_BYTES:
            raise ValueError(
                f"Downloaded file {dest.name} is unexpectedly small ({downloaded} bytes)"
            )

        if expected_sha256 is not None and digest.hexdigest().lower() != expected_sha256.lower():
            raise ValueError(
                f"SHA-256 mismatch for {dest.name}: got {digest.hexdigest()}, expected {expected_sha256}"
            )

        os.replace(tmp_path, dest)
        logger.info("Downloaded %s (%d bytes)", dest.name, downloaded)
        return dest
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def ensure_models(
    models_dir: Path | str | None = None,
    force: bool = False,
    progress_callback: Callable[[str, int, int | None], None] | None = None,
) -> ModelPaths:
    """Ensure YuNet and SFace ONNX models are present locally, downloading if needed."""
    paths = get_model_paths(models_dir)
    specs = (
        (YUNET_URL, paths.yunet, YUNET_SHA256),
        (SFACE_URL, paths.sface, SFACE_SHA256),
    )
    for url, dest, sha in specs:
        if not force and dest.is_file() and dest.stat().st_size > _MIN_VALID_BYTES:
            continue
        logger.info("Downloading model %s from %s", dest.name, url)
        _download_file(url, dest, progress_callback=progress_callback, expected_sha256=sha)
    return paths
