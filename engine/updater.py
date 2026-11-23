"""Lightweight, SHA-256 verified GitHub Releases delta updater for WinIRFaceID."""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import re
import stat
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlparse

from engine import REPO_URL, RELEASES_API_URL, __version__

_SEMVER_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_SHA256_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+[*]?(.+?)\s*$")
_ALLOWED_ASSET_HOSTS = frozenset(
    {
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
_MAX_NOTES_CHARS = 2000
_MAX_CHECKSUMS_BYTES = 512 * 1024
_DEFAULT_MAX_UPDATE_BYTES = 25 * 1024 * 1024
_CHUNK_SIZE = 65536
_ORIG_URLOPEN = urllib.request.urlopen


def _default_app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def parse_semver(version_str: str) -> tuple[int, int, int] | None:
    """Parse a semantic version string (e.g. 'v0.1.0', '1.0.0-rc1') into (major, minor, patch)."""
    if not isinstance(version_str, str):
        return None
    cleaned = version_str.strip()
    if not cleaned:
        return None
    match = _SEMVER_RE.fullmatch(cleaned)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer_version(latest: str, current: str) -> bool:
    """Return True if *latest* represents a strictly newer semantic version than *current*."""
    latest_tuple = parse_semver(latest)
    current_tuple = parse_semver(current)
    if latest_tuple is None or current_tuple is None:
        return False
    return latest_tuple > current_tuple


def is_allowed_release_url(url: str) -> bool:
    """Enforce HTTPS URLs under al3ksh/win-ir-faceid or official GitHub release asset hosts."""
    if not isinstance(url, str):
        return False
    cleaned = url.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        return False

    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return False

    if parsed.scheme.lower() != "https":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.port is not None:
        return False

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host == "github.com":
        return path == "/al3ksh/win-ir-faceid" or path.startswith("/al3ksh/win-ir-faceid/")
    if host in _ALLOWED_ASSET_HOSTS:
        return len(path) > 1 and path.startswith("/")
    return False


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse standard '<64-hex>  <filename>' checksum manifests into {filename: sha256_lower}."""
    if not isinstance(text, str):
        return {}
    sums: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _SHA256_LINE_RE.match(line)
        if match is None:
            continue
        digest = match.group(1).lower()
        filename = match.group(2).strip()
        if filename.startswith("./") or filename.startswith(".\\"):
            filename = filename[2:]
        if filename:
            sums[filename] = digest
    return sums


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Ensure HTTP redirects during update downloads never leave trusted GitHub asset hosts."""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not is_allowed_release_url(newurl):
            raise ValueError(f"Blocked redirect to disallowed host: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(
    req: urllib.request.Request,
    *,
    timeout: float,
    urlopen_fn: Callable[..., Any] | None = None,
    enforce_redirect_allowlist: bool = False,
) -> Any:
    if urlopen_fn is not None:
        return urlopen_fn(req, timeout=timeout)
    if enforce_redirect_allowlist and urllib.request.urlopen is _ORIG_URLOPEN:
        opener = urllib.request.build_opener(_ValidatedRedirectHandler())
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _trim_release_notes(body: Any) -> str:
    if not isinstance(body, str):
        return ""
    cleaned = body.replace("\r\n", "\n").strip()
    if len(cleaned) <= _MAX_NOTES_CHARS:
        return cleaned
    return cleaned[: _MAX_NOTES_CHARS - 3].rstrip() + "..."


def _is_update_zip_name(name: str) -> bool:
    return (
        isinstance(name, str)
        and name.startswith("WinIRFaceID-Update-v")
        and name.endswith(".zip")
        and "/" not in name
        and "\\" not in name
    )


def fetch_latest_release(
    current_version: str = __version__,
    api_url: str = RELEASES_API_URL,
    timeout: float = 4.0,
    *,
    app_root: Path | None = None,
    urlopen_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Query GitHub Releases API for latest version metadata and delta update assets."""
    fallback_url = f"{REPO_URL}/releases"
    root = Path(app_root) if app_root is not None else _default_app_root()
    is_git_checkout = (root / ".git").exists()

    def _error_payload() -> dict[str, Any]:
        return {
            "ok": False,
            "current_version": current_version,
            "latest_version": "",
            "update_available": False,
            "release_url": fallback_url,
            "release_notes": "",
            "update_asset": None,
            "checksums_url": None,
            "can_auto_update": False,
            "is_git_checkout": is_git_checkout,
            "no_releases": False,
            "error": "Unable to reach GitHub.",
        }

    def _no_releases_payload() -> dict[str, Any]:
        return {
            "ok": True,
            "current_version": current_version,
            "latest_version": current_version,
            "update_available": False,
            "release_url": fallback_url,
            "release_notes": "",
            "update_asset": None,
            "checksums_url": None,
            "can_auto_update": False,
            "is_git_checkout": is_git_checkout,
            "no_releases": True,
            "error": None,
        }

    req = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"WinIRFaceID/{current_version}",
        },
    )
    try:
        with _open_url(req, timeout=timeout, urlopen_fn=urlopen_fn) as resp:
            status = getattr(resp, "status", 200)
            if status == 404:
                return _no_releases_payload()
            if status is not None and status >= 400:
                return _error_payload()
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _no_releases_payload()
        return _error_payload()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError):
        return _error_payload()

    if not isinstance(payload, dict):
        return _error_payload()

    raw_tag = str(payload.get("tag_name") or "").strip()
    latest_clean = raw_tag[1:] if raw_tag.startswith(("v", "V")) else raw_tag
    raw_html_url = str(payload.get("html_url") or "").strip()
    safe_url = (
        raw_html_url
        if (raw_html_url == REPO_URL or raw_html_url.startswith(f"{REPO_URL}/"))
        else fallback_url
    )
    update_available = is_newer_version(latest_clean, current_version)
    release_notes = _trim_release_notes(payload.get("body"))

    update_asset: dict[str, Any] | None = None
    checksums_url: str | None = None
    assets = payload.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            dl_url = str(asset.get("browser_download_url") or "").strip()
            if not is_allowed_release_url(dl_url):
                continue
            if _is_update_zip_name(name) and update_asset is None:
                try:
                    size = max(0, int(asset.get("size") or 0))
                except (TypeError, ValueError):
                    size = 0
                update_asset = {
                    "name": name,
                    "url": dl_url,
                    "size": size,
                }
            elif name == "SHA256SUMS.txt" and checksums_url is None:
                checksums_url = dl_url

    can_auto_update = bool(
        update_asset is not None and checksums_url is not None and not is_git_checkout
    )

    return {
        "ok": True,
        "current_version": current_version,
        "latest_version": latest_clean,
        "update_available": update_available,
        "release_url": safe_url,
        "release_notes": release_notes,
        "update_asset": update_asset,
        "checksums_url": checksums_url,
        "can_auto_update": can_auto_update,
        "is_git_checkout": is_git_checkout,
        "no_releases": False,
        "error": None,
    }


def _validate_zip_member(info: zipfile.ZipInfo, dest_dir: Path, resolved_dest: Path) -> Path:
    raw_name = info.filename
    if not raw_name or "\x00" in raw_name:
        raise ValueError(f"Unsafe zip entry name: {raw_name!r}")

    # Reject POSIX symlinks or special file modes stored in upper 16 bits of external_attr.
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        raise ValueError(f"Refusing symlink inside zip archive: {raw_name}")

    normalized = raw_name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"Refusing absolute path inside zip archive: {raw_name}")

    posix = PurePosixPath(normalized)
    if ".." in posix.parts or any(":" in part for part in posix.parts):
        raise ValueError(f"Refusing path traversal inside zip archive: {raw_name}")

    rel_parts = [part for part in posix.parts if part not in ("", ".")]
    if not rel_parts:
        return resolved_dest

    target = dest_dir.joinpath(*rel_parts).resolve()
    if not target.is_relative_to(resolved_dest):
        raise ValueError(f"Zip entry escapes destination directory: {raw_name}")
    if not info.is_dir() and target == resolved_dest:
        raise ValueError(f"Invalid zip file entry path: {raw_name}")
    return target


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> Path:
    """Safely extract *zip_path* into *dest_dir*, rejecting traversal, symlinks, and absolute paths."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as zf:
        entries: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in zf.infolist():
            target = _validate_zip_member(info, dest_dir, resolved_dest)
            entries.append((info, target))

        for info, target in entries:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)

    return dest_dir


def download_and_verify_update(
    zip_url: str,
    checksums_url: str,
    zip_name: str,
    work_dir: Path,
    progress_cb: Callable[[int, int | None], None] | None = None,
    max_bytes: int = _DEFAULT_MAX_UPDATE_BYTES,
    *,
    stage_cb: Callable[[str], None] | None = None,
    urlopen_fn: Callable[..., Any] | None = None,
    timeout: float = 20.0,
) -> Path:
    """Download SHA256SUMS.txt + delta zip, verify SHA-256 strictly, and safely extract."""
    if not is_allowed_release_url(zip_url):
        raise ValueError("Update download URL is not allowed.")
    if not is_allowed_release_url(checksums_url):
        raise ValueError("Checksums URL is not allowed.")
    if not zip_name or "/" in zip_name or "\\" in zip_name or ".." in zip_name:
        raise ValueError(f"Invalid update archive name: {zip_name!r}")

    work_dir.mkdir(parents=True, exist_ok=True)

    if stage_cb is not None:
        stage_cb("downloading")

    sums_req = urllib.request.Request(
        checksums_url,
        headers={"User-Agent": f"WinIRFaceID/{__version__}"},
    )
    with _open_url(
        sums_req,
        timeout=timeout,
        urlopen_fn=urlopen_fn,
        enforce_redirect_allowlist=True,
    ) as sums_resp:
        raw_sums = sums_resp.read(_MAX_CHECKSUMS_BYTES + 1)
    if len(raw_sums) > _MAX_CHECKSUMS_BYTES:
        raise ValueError("SHA256SUMS.txt exceeds maximum allowed size.")

    checksums = parse_sha256sums(raw_sums.decode("utf-8"))
    expected_sha256 = checksums.get(zip_name)
    if not expected_sha256:
        raise ValueError(f"SHA-256 entry missing for {zip_name}")

    zip_path = work_dir / zip_name
    zip_req = urllib.request.Request(
        zip_url,
        headers={"User-Agent": f"WinIRFaceID/{__version__}"},
    )
    hasher = hashlib.sha256()
    downloaded = 0

    try:
        with _open_url(
            zip_req,
            timeout=timeout,
            urlopen_fn=urlopen_fn,
            enforce_redirect_allowlist=True,
        ) as resp:
            headers = getattr(resp, "headers", None)
            total: int | None = None
            if headers is not None:
                raw_len = headers.get("Content-Length")
                if raw_len is not None:
                    try:
                        parsed_len = int(raw_len)
                        if parsed_len > max_bytes:
                            raise ValueError("Update archive exceeds maximum allowed size.")
                        if parsed_len > 0:
                            total = parsed_len
                    except ValueError as exc:
                        if "maximum allowed size" in str(exc):
                            raise

            with open(zip_path, "wb") as out_file:
                while True:
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise ValueError("Update archive exceeds maximum allowed size.")
                    hasher.update(chunk)
                    out_file.write(chunk)
                    if progress_cb is not None:
                        progress_cb(downloaded, total)
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise

    if stage_cb is not None:
        stage_cb("verifying")

    actual_sha256 = hasher.hexdigest().lower()
    if not hmac.compare_digest(actual_sha256, expected_sha256.lower()):
        zip_path.unlink(missing_ok=True)
        raise ValueError("SHA-256 verification failed")

    if stage_cb is not None:
        stage_cb("extracting")

    extracted_dir = work_dir / "extracted"
    return safe_extract_zip(zip_path, extracted_dir)


def _ps_quote_arg(value: str) -> str:
    cleaned = str(value).rstrip("\\")
    return '"' + cleaned.replace('"', "") + '"'


def launch_elevated_update(
    extracted_dir: Path,
    target_dir: Path,
    cleanup_dir: Path,
    wait_pid: int,
    *,
    shell_execute: Callable[..., int] | None = None,
) -> dict[str, Any]:
    """Launch elevated PowerShell to run apply_update.ps1 in-place (refuses .git checkouts)."""
    target_path = Path(target_dir)
    if (target_path / ".git").exists():
        return {
            "ok": False,
            "code": 0,
            "error": "Refusing in-place update inside a git checkout.",
        }

    apply_script = Path(extracted_dir) / "scripts" / "apply_update.ps1"
    if not apply_script.is_file():
        return {
            "ok": False,
            "code": 0,
            "error": "Update package is missing scripts/apply_update.ps1.",
        }

    params = " ".join(
        [
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            _ps_quote_arg(str(apply_script)),
            "-SourceDir",
            _ps_quote_arg(str(Path(extracted_dir))),
            "-TargetDir",
            _ps_quote_arg(str(target_path)),
            "-WaitPid",
            str(int(wait_pid)),
            "-RelaunchGui",
            "-CleanupDir",
            _ps_quote_arg(str(Path(cleanup_dir))),
        ]
    )

    try:
        exec_fn = shell_execute if shell_execute is not None else ctypes.windll.shell32.ShellExecuteW
        rc = int(exec_fn(None, "runas", "powershell.exe", params, None, 0))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": 0, "error": str(exc)}

    if rc > 32:
        return {"ok": True, "code": rc, "error": None}
    return {
        "ok": False,
        "code": rc,
        "error": "Elevation was cancelled or failed." if rc == 5 else f"ShellExecuteW failed ({rc}).",
    }
