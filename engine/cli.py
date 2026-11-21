"""Command-line interface for WinIRFaceID (python -m engine.cli)."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import time
from typing import Sequence

from engine import __version__
from engine.liveness import check_ir_liveness
from engine.models import ensure_models
from engine.recognizer import DEFAULT_MATCH_THRESHOLD, FaceRecognizer, enhance_ir_bgr
from engine.store import ProfileStore


_POSE_HINTS = (
    "Look straight at the camera",
    "Look straight at the camera",
    "Turn your head slightly left",
    "Turn your head slightly right",
    "Tilt your head slightly up",
    "Tilt your head slightly down",
    "Look slightly left-up",
    "Look straight and hold still",
)


def _select_enrollment_camera(cameras: list) -> object | None:
    """Select an active IR camera only (real IR first, then any IR; never RGB)."""
    for cam in cameras:
        if getattr(cam, "is_ir", False) and not getattr(cam, "is_virtual", False):
            return cam
    for cam in cameras:
        if getattr(cam, "is_ir", False):
            return cam
    return None


def _cmd_download_models(args: argparse.Namespace) -> int:
    def _progress(name: str, downloaded: int, total: int | None) -> None:
        if total and total > 0:
            pct = min(100.0, (downloaded / total) * 100.0)
            print(f"\rDownloading {name}: {pct:5.1f}% ({downloaded}/{total} bytes)", end="", flush=True)
        else:
            print(f"\rDownloading {name}: {downloaded} bytes", end="", flush=True)

    paths = ensure_models(force=args.force, progress_callback=_progress)
    print()
    print(f"YuNet model ready: {paths.yunet}")
    print(f"SFace model ready: {paths.sface}")
    return 0


def _cmd_enroll(args: argparse.Namespace) -> int:
    from engine.ir_camera import WinRTCameraWorker, discover_cameras

    ensure_models()
    recognizer = FaceRecognizer()
    store = ProfileStore()

    cameras = discover_cameras()
    cam = _select_enrollment_camera(cameras)
    if cam is None:
        print("Error: No camera detected on this system.", file=sys.stderr)
        return 1

    if not cam.is_ir:
        print(
            f"Error: Active IR camera is required for enrollment (found only '{cam.label}').",
            file=sys.stderr,
        )
        return 1

    samples_target = max(1, int(args.samples))
    print(f"Enrolling user '{args.user}' using IR camera: {cam.label}")
    print(f"Capturing {samples_target} live IR samples. Follow the pose prompts below.")

    worker = WinRTCameraWorker(
        group=cam.group,
        source_id=cam.source_id,
        is_ir=True,
        ir_led_enabled=True,
    )

    embeddings = []
    last_capture_time = 0.0
    last_pair_ts = None
    deadline = time.monotonic() + float(args.timeout)

    try:
        worker.start()
        while len(embeddings) < samples_target and time.monotonic() < deadline:
            if worker.error:
                print(f"Camera error: {worker.error}", file=sys.stderr)
                return 1

            pair = worker.get_ir_pair()
            if pair is None or pair.timestamp == last_pair_ts:
                time.sleep(0.03)
                continue
            last_pair_ts = pair.timestamp

            now = time.monotonic()
            if now - last_capture_time < 0.45:
                continue

            eval_frame = pair.diff_bgr if pair.dark_bgr is not None else pair.bright_bgr
            det = recognizer.detect_largest(eval_frame)
            if det is None:
                enhanced = enhance_ir_bgr(eval_frame)
                det = recognizer.detect_largest(enhanced)
                if det is not None:
                    eval_frame = enhanced

            if det is None:
                continue

            liveness = check_ir_liveness(pair, det.bbox)
            if not liveness.passed:
                if liveness.reason == "overexposed_ae_settling":
                    print("  [AE] Settling auto-exposure flash...", flush=True)
                continue

            emb = recognizer.extract_embedding(eval_frame, det)
            embeddings.append(emb)
            last_capture_time = now

            hint = _POSE_HINTS[min(len(embeddings), len(_POSE_HINTS) - 1)]
            print(
                f"  Sample {len(embeddings)}/{samples_target} captured "
                f"(IR delta={liveness.delta:.1f}, ratio={liveness.ratio:.2f}) -> {hint}"
            )
    finally:
        worker.stop()

    if len(embeddings) < samples_target:
        print(
            f"Enrollment timed out after capturing {len(embeddings)}/{samples_target} samples.",
            file=sys.stderr,
        )
        return 1

    profile = store.upsert_embeddings(
        args.user,
        embeddings,
        append=not args.replace,
        domain=args.domain,
    )
    print(
        f"Enrollment complete for '{profile.username}' "
        f"(total stored embeddings: {len(profile.embeddings)})."
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from engine.ipc_server import IRFaceVerifier

    ensure_models()
    store = ProfileStore()
    verifier = IRFaceVerifier(
        store,
        threshold=args.threshold,
        require_motion=args.require_motion,
        require_open_eyes=args.require_open_eyes,
    )
    result = verifier.verify(username=args.user or "", timeout_ms=args.timeout)

    if result.get("status") == "matched":
        print(
            f"MATCHED user='{result['username']}' domain='{result['domain']}' "
            f"score={result['score']:.4f} liveness_delta={result['liveness_delta']:.2f}"
        )
        return 0

    print(f"VERIFICATION FAILED: reason={result.get('reason', 'unknown')}", file=sys.stderr)
    return 1


def _cmd_set_password(args: argparse.Namespace) -> int:
    store = ProfileStore()
    password = getpass.getpass(f"Enter Windows logon password for '{args.user}': ")
    confirm = getpass.getpass("Confirm Windows logon password: ")
    if password != confirm:
        print("Error: Passwords do not match.", file=sys.stderr)
        return 1

    profile = store.set_credentials(args.user, password=password, domain=args.domain)
    if not profile.embeddings:
        print(
            f"Saved DPAPI credentials for '{profile.username}' (note: no face embeddings enrolled yet)."
        )
    else:
        print(f"Saved DPAPI credentials for '{profile.username}'.")
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    store = ProfileStore()
    users = store.list_users()
    if not users:
        print("No enrolled profiles found.")
        return 0

    print(f"{'Username':<20} {'Domain':<12} {'Samples':>7} {'Password':>10} {'Updated At'}")
    print("-" * 74)
    for user in users:
        has_pw = "Yes" if user.password else "No"
        print(
            f"{user.username:<20} {user.domain:<12} {len(user.embeddings):>7} "
            f"{has_pw:>10} {user.updated_at}"
        )
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    store = ProfileStore()
    if store.delete_user(args.user):
        print(f"Deleted profile '{args.user}'.")
        return 0
    print(f"Profile '{args.user}' not found.", file=sys.stderr)
    return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    from engine.ipc_server import IRFaceVerifier, NamedPipeServer

    ensure_models()
    store = ProfileStore(local_machine=args.service)
    verifier = IRFaceVerifier(
        store,
        threshold=args.threshold,
        require_motion=args.require_motion,
        require_open_eyes=args.require_open_eyes,
    )
    server = NamedPipeServer(
        pipe_name=args.pipe,
        store=store,
        verifier=verifier,
        service_mode=args.service,
    )
    print(f"WinIRFaceID IPC server v{__version__} listening on {args.pipe}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down IPC server...")
    finally:
        server.close()
    return 0


def _cmd_gui(_args: argparse.Namespace) -> int:
    from engine.gui_enroll import main as gui_main

    gui_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m engine.cli",
        description="WinIRFaceID - Active Infrared Face Recognition Engine for Windows",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging output",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dl = sub.add_parser("download-models", help="Download YuNet and SFace ONNX models")
    p_dl.add_argument("--force", action="store_true", help="Re-download existing models")
    p_dl.set_defaults(func=_cmd_download_models)

    p_enroll = sub.add_parser("enroll", help="Enroll a user's IR face profile")
    p_enroll.add_argument(
        "--user",
        default=os.environ.get("USERNAME", ""),
        required=not bool(os.environ.get("USERNAME")),
        help="Windows username to enroll",
    )
    p_enroll.add_argument("--samples", type=int, default=8, help="Number of face samples (default: 8)")
    p_enroll.add_argument("--domain", default=".", help="Windows logon domain (default: '.')")
    p_enroll.add_argument("--replace", action="store_true", help="Replace existing embeddings instead of appending")
    p_enroll.add_argument("--timeout", type=float, default=30.0, help="Enrollment timeout in seconds (default: 30)")
    p_enroll.set_defaults(func=_cmd_enroll)

    p_verify = sub.add_parser("verify", help="Verify a live IR face against enrolled profiles")
    p_verify.add_argument("--user", default="", help="Optional username filter")
    p_verify.add_argument("--timeout", type=int, default=8000, help="Verification timeout in ms (default: 8000)")
    p_verify.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_MATCH_THRESHOLD,
        help=f"Cosine similarity threshold (default: {DEFAULT_MATCH_THRESHOLD})",
    )
    p_verify.add_argument(
        "--require-motion",
        action="store_true",
        help="Require live blink or 3D head micro-motion during verification",
    )
    p_verify.add_argument(
        "--require-open-eyes",
        action="store_true",
        help="Require both eyes to be open in active IR during verification",
    )
    p_verify.set_defaults(func=_cmd_verify)

    p_pw = sub.add_parser("set-password", help="Store DPAPI-encrypted Windows logon password for a user")
    p_pw.add_argument("--user", required=True, help="Windows username")
    p_pw.add_argument("--domain", default=".", help="Windows logon domain (default: '.')")
    p_pw.set_defaults(func=_cmd_set_password)

    p_list = sub.add_parser("list", help="List enrolled user profiles")
    p_list.set_defaults(func=_cmd_list)

    p_del = sub.add_parser("delete", help="Delete an enrolled user profile")
    p_del.add_argument("--user", required=True, help="Windows username to delete")
    p_del.set_defaults(func=_cmd_delete)

    p_serve = sub.add_parser("serve", help="Run the Named Pipe IPC server")
    p_serve.add_argument("--pipe", default=r"\\.\pipe\WinIRFaceID", help="Named pipe path")
    p_serve.add_argument("--service", action="store_true", help="Use LocalSystem pipe ACL and machine DPAPI store")
    p_serve.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_MATCH_THRESHOLD,
        help=f"Cosine similarity threshold (default: {DEFAULT_MATCH_THRESHOLD})",
    )
    p_serve.add_argument(
        "--require-motion",
        action="store_true",
        help="Require live blink or 3D head micro-motion during verification",
    )
    p_serve.add_argument(
        "--require-open-eyes",
        action="store_true",
        help="Require both eyes to be open in active IR during verification",
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_gui = sub.add_parser("gui", help="Launch the dark-mode IR Face ID Enrollment GUI")
    p_gui.set_defaults(func=_cmd_gui)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m engine.cli``."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
