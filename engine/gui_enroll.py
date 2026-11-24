"""Dark-mode Tkinter GUI for enrolling and live-testing WinIRFaceID."""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageTk

from engine.liveness import (
    EyeOpenResult,
    LivenessResult,
    TemporalLivenessTracker,
    check_eyes_open_ir,
    check_ir_liveness,
)
from engine.models import ensure_models, models_exist
from engine.recognizer import (
    DEFAULT_MATCH_THRESHOLD,
    FaceDetection,
    FaceRecognizer,
    enhance_ir_bgr,
)
from engine.store import (
    ProfileStore,
    ServiceConfig,
    load_service_config,
    save_service_config,
)


_BG_MAIN = "#12151c"
_BG_PANEL = "#1b202b"
_BG_CARD = "#232a38"
_FG_PRIMARY = "#edf2f7"
_FG_MUTED = "#94a3b8"
_ACCENT_BLUE = "#3b82f6"
_ACCENT_GREEN = "#22c55e"
_ACCENT_RED = "#ef4444"
_ACCENT_AMBER = "#f59e0b"

_STANDBY_VIDEO_TEXT = "IR Camera Standby — click Enroll, Test Live Match, or Start Preview"
_STANDBY_LIVENESS_TEXT = "IR Δ: -- | BG: -- | 3D: -- | Standby"

_POSE_PROMPTS = [
    "1/8: Look straight at the IR camera",
    "2/8: Hold steady looking straight",
    "3/8: Turn head slightly LEFT",
    "4/8: Turn head slightly RIGHT",
    "5/8: Tilt head slightly UP",
    "6/8: Tilt head slightly DOWN",
    "7/8: Slight natural smile or neutral look",
    "8/8: Look straight for final capture",
]


class PasswordDialog(tk.Toplevel):
    """Modal dark-mode dialog to set DPAPI-encrypted logon credentials."""

    def __init__(self, parent: tk.Misc, username: str, default_domain: str = ".") -> None:
        super().__init__(parent)
        self.title("Set Logon Password (DPAPI)")
        self.configure(bg=_BG_PANEL)
        self.resizable(False, False)
        self.result: tuple[str, str] | None = None

        self.transient(parent)
        self.grab_set()

        frame = tk.Frame(self, bg=_BG_PANEL, padx=20, pady=16)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text=f"Windows Logon Credentials for '{username}'",
            bg=_BG_PANEL,
            fg=_FG_PRIMARY,
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        tk.Label(
            frame,
            text="Domain (use '.' for local account):",
            bg=_BG_PANEL,
            fg=_FG_MUTED,
            font=("Segoe UI", 9),
        ).grid(row=1, column=0, sticky="w", pady=4)
        self.domain_var = tk.StringVar(value=default_domain or ".")
        domain_entry = tk.Entry(
            frame,
            textvariable=self.domain_var,
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            insertbackground=_FG_PRIMARY,
            relief="flat",
            width=28,
        )
        domain_entry.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=4)

        tk.Label(
            frame,
            text="Windows Password:",
            bg=_BG_PANEL,
            fg=_FG_MUTED,
            font=("Segoe UI", 9),
        ).grid(row=2, column=0, sticky="w", pady=4)
        self.pw_var = tk.StringVar()
        pw_entry = tk.Entry(
            frame,
            textvariable=self.pw_var,
            show="*",
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            insertbackground=_FG_PRIMARY,
            relief="flat",
            width=28,
        )
        pw_entry.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=4)

        tk.Label(
            frame,
            text="Confirm Password:",
            bg=_BG_PANEL,
            fg=_FG_MUTED,
            font=("Segoe UI", 9),
        ).grid(row=3, column=0, sticky="w", pady=4)
        self.confirm_var = tk.StringVar()
        confirm_entry = tk.Entry(
            frame,
            textvariable=self.confirm_var,
            show="*",
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            insertbackground=_FG_PRIMARY,
            relief="flat",
            width=28,
        )
        confirm_entry.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=4)

        tk.Label(
            frame,
            text="For a Microsoft Account, enter your Microsoft Account password (keep Domain as '.').",
            bg=_BG_PANEL,
            fg=_FG_MUTED,
            font=("Segoe UI", 8),
            wraplength=340,
            justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        btn_row = tk.Frame(frame, bg=_BG_PANEL)
        btn_row.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))

        tk.Button(
            btn_row,
            text="Cancel",
            command=self.destroy,
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            activebackground="#2f3749",
            activeforeground=_FG_PRIMARY,
            relief="flat",
            padx=14,
            pady=6,
        ).pack(side="right", padx=(8, 0))

        tk.Button(
            btn_row,
            text="Save Encrypted",
            command=self._on_save,
            bg=_ACCENT_BLUE,
            fg="white",
            activebackground="#2563eb",
            activeforeground="white",
            relief="flat",
            padx=14,
            pady=6,
        ).pack(side="right")

        pw_entry.focus_set()
        self.bind("<Return>", lambda _e: self._on_save())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _on_save(self) -> None:
        pw = self.pw_var.get()
        confirm = self.confirm_var.get()
        if pw != confirm:
            messagebox.showerror("Password Mismatch", "Passwords do not match.", parent=self)
            return
        if not pw:
            messagebox.showerror("Empty Password", "Please enter a non-empty password.", parent=self)
            return
        self.result = (self.domain_var.get().strip() or ".", pw)
        self.destroy()


class IREnrollmentApp(tk.Tk):
    """Dark-mode Tkinter application for IR Face ID enrollment and live testing."""

    def __init__(self) -> None:
        super().__init__()
        self.title("WinIRFaceID - IR Face Enrollment & Live Tester")
        self.geometry("1140x720")
        self.minsize(960, 620)
        self.configure(bg=_BG_MAIN)

        self.store = ProfileStore()
        self.recognizer: FaceRecognizer | None = None
        self.cameras: list[Any] = []
        self.worker: Any | None = None
        self.current_cam_is_ir = False

        self._photo_ref: ImageTk.PhotoImage | None = None
        self._preview_enabled = False
        self._live_match_enabled = False
        self._enrolling = False
        self._enroll_embeddings: list[np.ndarray] = []
        self._enroll_target = 8
        self._last_enroll_capture_ts = 0.0
        self._last_processed_pair_ts: float | None = None
        self._downloading_models = False
        self._motion_tracker = TemporalLivenessTracker()

        svc_cfg = load_service_config()
        initial_thresh = max(0.45, min(0.85, float(svc_cfg.threshold)))

        self.username_var = tk.StringVar(value=os.environ.get("USERNAME", "User"))
        self.feed_mode_var = tk.StringVar(value="IR - Ambient")
        self.camera_var = tk.StringVar(value="")
        self.threshold_var = tk.DoubleVar(value=initial_thresh)
        self.require_motion_var = tk.BooleanVar(value=bool(svc_cfg.require_motion))
        self.require_open_eyes_var = tk.BooleanVar(value=bool(svc_cfg.require_open_eyes))
        self.status_var = tk.StringVar(value="Initializing...")
        self.liveness_var = tk.StringVar(value=_STANDBY_LIVENESS_TEXT)
        self.match_var = tk.StringVar(value="Match: Idle")
        self.ae_var = tk.StringVar(value="AE Status: Standby")
        self.prompt_var = tk.StringVar(value="Ready to enroll or test live IR matching.")

        self._build_ui()
        self._refresh_profiles_list()
        self._try_load_recognizer()
        self._discover_cameras()
        self._enter_standby()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.after(30, self._update_loop)

    def _build_ui(self) -> None:
        top_bar = tk.Frame(self, bg=_BG_PANEL, padx=16, pady=10)
        top_bar.pack(fill="x")

        tk.Label(
            top_bar,
            text="WinIRFaceID",
            bg=_BG_PANEL,
            fg=_FG_PRIMARY,
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")

        tk.Label(
            top_bar,
            text="Active Infrared Face Recognition",
            bg=_BG_PANEL,
            fg=_FG_MUTED,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(10, 0))

        self.ae_badge = tk.Label(
            top_bar,
            textvariable=self.ae_var,
            bg=_BG_CARD,
            fg=_FG_MUTED,
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=4,
        )
        self.ae_badge.pack(side="right")

        content = tk.Frame(self, bg=_BG_MAIN, padx=14, pady=12)
        content.pack(fill="both", expand=True)

        # Left pane: live IR feed + telemetry overlays
        left = tk.Frame(content, bg=_BG_PANEL, padx=12, pady=12)
        left.pack(side="left", fill="both", expand=True, padx=(0, 12))

        feed_ctrl = tk.Frame(left, bg=_BG_PANEL)
        feed_ctrl.pack(fill="x", pady=(0, 8))

        tk.Label(
            feed_ctrl,
            text="Camera:",
            bg=_BG_PANEL,
            fg=_FG_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")

        self.cam_combo = ttk.Combobox(
            feed_ctrl,
            textvariable=self.camera_var,
            state="readonly",
            width=36,
        )
        self.cam_combo.pack(side="left", padx=(6, 12))
        self.cam_combo.bind("<<ComboboxSelected>>", self._on_camera_selected)

        tk.Label(
            feed_ctrl,
            text="IR Mode:",
            bg=_BG_PANEL,
            fg=_FG_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")

        self.mode_combo = ttk.Combobox(
            feed_ctrl,
            textvariable=self.feed_mode_var,
            values=["IR - Ambient", "Anti-Flicker"],
            state="readonly",
            width=15,
        )
        self.mode_combo.pack(side="left", padx=(6, 0))
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_feed_mode_changed)

        self.video_label = tk.Label(
            left,
            text=_STANDBY_VIDEO_TEXT,
            bg="#0b0d12",
            fg=_FG_MUTED,
            font=("Segoe UI", 11),
        )
        self.video_label.pack(fill="both", expand=True)

        telemetry = tk.Frame(left, bg=_BG_CARD, padx=12, pady=8)
        telemetry.pack(fill="x", pady=(8, 0))

        tk.Label(
            telemetry,
            textvariable=self.liveness_var,
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            font=("Consolas", 9, "bold"),
        ).pack(side="left")

        self.match_label = tk.Label(
            telemetry,
            textvariable=self.match_var,
            bg=_BG_CARD,
            fg=_ACCENT_BLUE,
            font=("Consolas", 9, "bold"),
        )
        self.match_label.pack(side="right")

        self.prompt_banner = tk.Label(
            left,
            textvariable=self.prompt_var,
            bg=_BG_PANEL,
            fg=_ACCENT_AMBER,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            pady=6,
        )
        self.prompt_banner.pack(fill="x")

        # Right pane: user & actions
        right = tk.Frame(content, bg=_BG_PANEL, width=310, padx=14, pady=12)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        tk.Label(
            right,
            text="Target Username",
            bg=_BG_PANEL,
            fg=_FG_MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w")

        user_entry = tk.Entry(
            right,
            textvariable=self.username_var,
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            insertbackground=_FG_PRIMARY,
            relief="flat",
            font=("Segoe UI", 10),
        )
        user_entry.pack(fill="x", pady=(4, 10), ipady=5)

        thresh_row = tk.Frame(right, bg=_BG_PANEL)
        thresh_row.pack(fill="x", pady=(0, 6))

        tk.Label(
            thresh_row,
            text="Match Threshold (0.45-0.85):",
            bg=_BG_PANEL,
            fg=_FG_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")

        self.threshold_spin = tk.Spinbox(
            thresh_row,
            from_=0.45,
            to=0.85,
            increment=0.01,
            format="%.2f",
            textvariable=self.threshold_var,
            command=self._on_threshold_changed,
            width=6,
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            buttonbackground=_BG_CARD,
            insertbackground=_FG_PRIMARY,
            relief="flat",
            font=("Consolas", 9, "bold"),
        )
        self.threshold_spin.pack(side="right")
        self.threshold_spin.bind("<FocusOut>", self._on_threshold_changed)
        self.threshold_spin.bind("<Return>", self._on_threshold_changed)

        self.motion_check = tk.Checkbutton(
            right,
            text="Require Blink / 3D Motion",
            variable=self.require_motion_var,
            command=self._on_motion_toggle,
            bg=_BG_PANEL,
            fg=_FG_PRIMARY,
            selectcolor=_BG_CARD,
            activebackground=_BG_PANEL,
            activeforeground=_FG_PRIMARY,
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.motion_check.pack(fill="x", pady=(0, 2))

        self.open_eyes_check = tk.Checkbutton(
            right,
            text="Require Open Eyes",
            variable=self.require_open_eyes_var,
            command=self._on_open_eyes_toggle,
            bg=_BG_PANEL,
            fg=_FG_PRIMARY,
            selectcolor=_BG_CARD,
            activebackground=_BG_PANEL,
            activeforeground=_FG_PRIMARY,
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.open_eyes_check.pack(fill="x", pady=(0, 10))

        self.btn_enroll = tk.Button(
            right,
            text="Enroll User (8 Samples)",
            command=self._on_enroll_click,
            bg=_ACCENT_BLUE,
            fg="white",
            activebackground="#2563eb",
            activeforeground="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            pady=8,
        )
        self.btn_enroll.pack(fill="x", pady=4)

        self.btn_live_test = tk.Button(
            right,
            text="Test Live Match",
            command=self._on_toggle_live_match,
            bg=_ACCENT_GREEN,
            fg="#09150d",
            activebackground="#16a34a",
            activeforeground="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            pady=8,
        )
        self.btn_live_test.pack(fill="x", pady=4)

        self.btn_preview = tk.Button(
            right,
            text="Start Preview",
            command=self._on_toggle_preview,
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            activebackground="#2f3749",
            activeforeground=_FG_PRIMARY,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            pady=7,
        )
        self.btn_preview.pack(fill="x", pady=4)

        tk.Button(
            right,
            text="Set Logon Password (DPAPI)",
            command=self._on_set_password,
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            activebackground="#2f3749",
            activeforeground=_FG_PRIMARY,
            relief="flat",
            font=("Segoe UI", 9),
            pady=7,
        ).pack(fill="x", pady=4)

        tk.Button(
            right,
            text="Delete Profile",
            command=self._on_delete_profile,
            bg="#3b1d24",
            fg="#fecaca",
            activebackground=_ACCENT_RED,
            activeforeground="white",
            relief="flat",
            font=("Segoe UI", 9),
            pady=7,
        ).pack(fill="x", pady=4)

        self.btn_download = tk.Button(
            right,
            text="Download Models",
            command=self._on_download_models,
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            activebackground="#2f3749",
            activeforeground=_FG_PRIMARY,
            relief="flat",
            font=("Segoe UI", 9),
            pady=7,
        )
        self.btn_download.pack(fill="x", pady=(4, 12))

        tk.Label(
            right,
            text="Enrolled Profiles (DPAPI Encrypted)",
            bg=_BG_PANEL,
            fg=_FG_MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(4, 4))

        self.profile_list = tk.Listbox(
            right,
            bg=_BG_CARD,
            fg=_FG_PRIMARY,
            selectbackground=_ACCENT_BLUE,
            selectforeground="white",
            relief="flat",
            font=("Consolas", 9),
            height=6,
        )
        self.profile_list.pack(fill="both", expand=True)
        self.profile_list.bind("<<ListboxSelect>>", self._on_profile_selected)

        status_bar = tk.Label(
            self,
            textvariable=self.status_var,
            bg="#0d1016",
            fg=_FG_MUTED,
            anchor="w",
            padx=14,
            pady=6,
            font=("Segoe UI", 9),
        )
        status_bar.pack(fill="x", side="bottom")

    def _try_load_recognizer(self) -> bool:
        if self.recognizer is not None:
            return True
        if not models_exist():
            self.status_var.set("ONNX models missing. Click 'Download Models' to fetch YuNet + SFace.")
            return False
        try:
            self.recognizer = FaceRecognizer()
            self.status_var.set("YuNet + SFace models loaded.")
            return True
        except Exception as exc:
            self.status_var.set(f"Model load error: {exc}")
            return False

    def _discover_cameras(self) -> None:
        try:
            from engine.ir_camera import discover_cameras

            self.cameras = discover_cameras()
        except Exception as exc:
            self.status_var.set(f"Camera discovery failed: {exc}")
            return

        if not self.cameras:
            self.status_var.set("No cameras found.")
            return

        labels = [
            f"{'[IR] ' if c.is_ir else '[RGB] '}{c.label}"
            for c in self.cameras
        ]
        self.cam_combo["values"] = labels
        default_idx = 0
        for idx, c in enumerate(self.cameras):
            if getattr(c, "is_ir", False) and not getattr(c, "is_virtual", False):
                default_idx = idx
                break
        else:
            for idx, c in enumerate(self.cameras):
                if getattr(c, "is_ir", False):
                    default_idx = idx
                    break
        self.cam_combo.current(default_idx)
        self.current_cam_is_ir = bool(self.cameras[default_idx].is_ir)
        self.status_var.set(f"Camera ready (standby): {self.cameras[default_idx].label}")

    def _stop_worker(self) -> None:
        if self.worker is not None:
            try:
                self.worker.stop()
            except Exception:
                pass
            self.worker = None

    def _enter_standby(self) -> None:
        self._stop_worker()
        self._last_processed_pair_ts = None
        self._photo_ref = None
        self.video_label.configure(image="", text=_STANDBY_VIDEO_TEXT)
        self.ae_var.set("AE Status: Standby")
        self.ae_badge.configure(fg=_FG_MUTED)
        self.liveness_var.set(_STANDBY_LIVENESS_TEXT)
        self.match_var.set("Match: Idle")
        self.match_label.configure(fg=_ACCENT_BLUE)

    def _stop_camera_if_idle(self) -> None:
        if not self._preview_enabled and not self._enrolling and not self._live_match_enabled:
            self._enter_standby()

    def _ensure_selected_camera_started(self) -> bool:
        if self.worker is not None:
            return True
        idx = self.cam_combo.current()
        if idx < 0 or idx >= len(self.cameras):
            self.status_var.set("No camera available.")
            return False
        return self._start_camera_by_index(idx)

    def _start_camera_by_index(self, idx: int) -> bool:
        self._stop_worker()
        if idx < 0 or idx >= len(self.cameras):
            return False
        cam = self.cameras[idx]
        self.current_cam_is_ir = bool(cam.is_ir)
        try:
            from engine.ir_camera import WinRTCameraWorker

            worker = WinRTCameraWorker(
                group=cam.group,
                source_id=cam.source_id,
                is_ir=cam.is_ir,
                ir_led_enabled=cam.is_ir,
            )
            mode = "subtract" if self.feed_mode_var.get() == "IR - Ambient" else "bright"
            worker.anti_flicker_mode = mode
            worker.start()
            self.worker = worker
            if self._photo_ref is None:
                self.video_label.configure(text="Starting IR camera...")
            self.ae_var.set("AE Status: Normal")
            self.ae_badge.configure(fg=_ACCENT_GREEN)
            self.status_var.set(f"Camera active: {cam.label}")
            return True
        except Exception as exc:
            self.status_var.set(f"Failed to start camera: {exc}")
            return False

    def _on_camera_selected(self, _event: Any = None) -> None:
        idx = self.cam_combo.current()
        if 0 <= idx < len(self.cameras):
            self.current_cam_is_ir = bool(self.cameras[idx].is_ir)
            if self.worker is not None:
                self._start_camera_by_index(idx)
            else:
                self.status_var.set(f"Selected camera (standby): {self.cameras[idx].label}")

    def _on_feed_mode_changed(self, _event: Any = None) -> None:
        if self.worker is not None:
            mode = "subtract" if self.feed_mode_var.get() == "IR - Ambient" else "bright"
            self.worker.anti_flicker_mode = mode

    def _refresh_profiles_list(self) -> None:
        self.profile_list.delete(0, tk.END)
        for p in self.store.list_users():
            pw_flag = "PW:Yes" if p.password else "PW:No"
            self.profile_list.insert(
                tk.END,
                f"{p.username} ({len(p.embeddings)} samples, {pw_flag})",
            )

    def _on_profile_selected(self, _event: Any = None) -> None:
        sel = self.profile_list.curselection()
        if not sel:
            return
        text = self.profile_list.get(sel[0])
        username = text.split(" (", 1)[0].strip()
        if username:
            self.username_var.set(username)

    def _on_download_models(self) -> None:
        if self._downloading_models:
            return
        self._downloading_models = True
        self.btn_download.configure(state="disabled")
        self.status_var.set("Downloading YuNet and SFace ONNX models...")

        def _worker() -> None:
            try:
                ensure_models()
                self.after(0, self._on_models_downloaded, None)
            except Exception as exc:
                self.after(0, self._on_models_downloaded, str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_models_downloaded(self, err: str | None) -> None:
        self._downloading_models = False
        self.btn_download.configure(state="normal")
        if err:
            self.status_var.set(f"Model download failed: {err}")
            messagebox.showerror("Download Error", err, parent=self)
            return
        self._try_load_recognizer()
        messagebox.showinfo("Models Ready", "YuNet and SFace models downloaded successfully.", parent=self)

    def _on_enroll_click(self) -> None:
        if self._enrolling:
            self._enrolling = False
            self._enroll_embeddings.clear()
            self.btn_enroll.configure(text="Enroll User (8 Samples)")
            self.prompt_var.set("Enrollment cancelled.")
            self._stop_camera_if_idle()
            if self.worker is None:
                self.status_var.set("Enrollment cancelled. IR camera in standby.")
            return

        if not self._try_load_recognizer():
            messagebox.showwarning(
                "Models Required",
                "Please download the YuNet and SFace models first.",
                parent=self,
            )
            return

        username = self.username_var.get().strip()
        if not username:
            messagebox.showwarning("Username Required", "Enter a Windows username.", parent=self)
            return

        idx = self.cam_combo.current()
        if 0 <= idx < len(self.cameras):
            self.current_cam_is_ir = bool(self.cameras[idx].is_ir)
        else:
            self.current_cam_is_ir = False

        if not self.current_cam_is_ir:
            messagebox.showwarning(
                "IR Camera Required",
                "Select an active IR camera before enrolling.",
                parent=self,
            )
            return

        self._live_match_enabled = False
        self.btn_live_test.configure(text="Test Live Match")
        self.match_var.set("Match: Idle")
        self.match_label.configure(fg=_ACCENT_BLUE)
        if not self._ensure_selected_camera_started():
            messagebox.showerror(
                "Camera Error",
                "Failed to start the selected IR camera.",
                parent=self,
            )
            return
        self._enroll_embeddings = []
        self._last_enroll_capture_ts = 0.0
        self._enrolling = True
        self.btn_enroll.configure(text="Cancel Enrollment")
        self.prompt_var.set(_POSE_PROMPTS[0])
        self.status_var.set(f"Enrolling '{username}'...")

    def _get_match_threshold(self) -> float:
        try:
            val = float(self.threshold_var.get())
        except (tk.TclError, ValueError):
            val = DEFAULT_MATCH_THRESHOLD
        return max(0.45, min(0.85, val))

    def _save_service_settings(self) -> None:
        thresh = round(self._get_match_threshold(), 2)
        cfg = ServiceConfig(
            threshold=thresh,
            require_motion=bool(self.require_motion_var.get()),
            require_open_eyes=bool(self.require_open_eyes_var.get()),
            # This legacy window has no lockout control; keep the configured limit.
            max_failed_attempts=load_service_config().max_failed_attempts,
        )
        try:
            save_service_config(cfg)
            self.status_var.set(
                f"Saved service settings (threshold={cfg.threshold:.2f}, "
                f"motion={cfg.require_motion}, open_eyes={cfg.require_open_eyes})."
            )
        except Exception as exc:
            self.status_var.set(f"Failed to save service settings: {exc}")

    def _on_threshold_changed(self, _event: Any = None) -> None:
        thresh = round(self._get_match_threshold(), 2)
        self.threshold_var.set(thresh)
        self._save_service_settings()

    def _on_motion_toggle(self) -> None:
        self._motion_tracker.reset()
        self._save_service_settings()

    def _on_open_eyes_toggle(self) -> None:
        self._save_service_settings()

    def _on_toggle_preview(self) -> None:
        if not self._preview_enabled:
            idx = self.cam_combo.current()
            if idx < 0 or idx >= len(self.cameras):
                messagebox.showwarning(
                    "No Camera",
                    "No camera available to preview.",
                    parent=self,
                )
                return
            if not self._ensure_selected_camera_started():
                messagebox.showerror(
                    "Camera Error",
                    "Failed to start the selected camera.",
                    parent=self,
                )
                return
            self._preview_enabled = True
            self.btn_preview.configure(text="Stop Preview", bg=_ACCENT_AMBER, fg="#09150d")
            if not self._enrolling and not self._live_match_enabled:
                self.prompt_var.set("IR camera preview active.")
        else:
            self._preview_enabled = False
            self.btn_preview.configure(text="Start Preview", bg=_BG_CARD, fg=_FG_PRIMARY)
            self._stop_camera_if_idle()
            if self.worker is None:
                self.prompt_var.set("Ready to enroll or test live IR matching.")
                self.status_var.set("IR camera in standby.")

    def _on_toggle_live_match(self) -> None:
        if not self._live_match_enabled:
            if not self._try_load_recognizer():
                messagebox.showwarning(
                    "Models Required",
                    "Please download the YuNet and SFace models first.",
                    parent=self,
                )
                return
            idx = self.cam_combo.current()
            if 0 <= idx < len(self.cameras):
                self.current_cam_is_ir = bool(self.cameras[idx].is_ir)
            else:
                self.current_cam_is_ir = False
            if not self.current_cam_is_ir:
                messagebox.showwarning(
                    "IR Camera Required",
                    "Select an active IR camera before testing live match.",
                    parent=self,
                )
                return
            self._enrolling = False
            self._enroll_embeddings.clear()
            self.btn_enroll.configure(text="Enroll User (8 Samples)")
            if not self._ensure_selected_camera_started():
                messagebox.showerror(
                    "Camera Error",
                    "Failed to start the selected IR camera.",
                    parent=self,
                )
                return
            self._motion_tracker.reset()
            self._live_match_enabled = True
            self.btn_live_test.configure(text="Stop Live Match")
            if self.require_motion_var.get():
                self.prompt_var.set("Live IR match active — blink or slightly turn your head.")
            else:
                self.prompt_var.set("Live IR match active — look at the IR camera.")
        else:
            self._live_match_enabled = False
            self._motion_tracker.reset()
            self.btn_live_test.configure(text="Test Live Match")
            self.match_var.set("Match: Idle")
            self.match_label.configure(fg=_ACCENT_BLUE)
            self.prompt_var.set("Ready to enroll or test live IR matching.")
            self._stop_camera_if_idle()
            if self.worker is None:
                self.status_var.set("Live match stopped. IR camera in standby.")

    def _on_set_password(self) -> None:
        username = self.username_var.get().strip()
        if not username:
            messagebox.showwarning("Username Required", "Enter a Windows username.", parent=self)
            return
        existing = self.store.get_user(username)
        default_domain = existing.domain if existing else "."
        dlg = PasswordDialog(self, username=username, default_domain=default_domain)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        domain, password = dlg.result
        self.store.set_credentials(username, password=password, domain=domain)
        self._refresh_profiles_list()
        self.status_var.set(f"Saved DPAPI credentials for '{username}'.")

    def _on_delete_profile(self) -> None:
        username = self.username_var.get().strip()
        if not username:
            return
        if not messagebox.askyesno(
            "Delete Profile",
            f"Delete stored face embeddings and credentials for '{username}'?",
            parent=self,
        ):
            return
        if self.store.delete_user(username):
            self._refresh_profiles_list()
            self.status_var.set(f"Deleted profile '{username}'.")
        else:
            self.status_var.set(f"Profile '{username}' not found.")

    def _evaluate_frame(
        self,
    ) -> tuple[np.ndarray | None, FaceDetection | None, LivenessResult | None, np.ndarray | None]:
        if self.worker is None:
            return None, None, None, None

        if self.worker.error:
            self.status_var.set(f"Camera error: {self.worker.error}")
            return None, None, None, None

        pair = self.worker.get_ir_pair() if self.current_cam_is_ir else None
        display_frame = self.worker.get_frame()
        if pair is not None:
            if self.feed_mode_var.get() == "IR - Ambient" and pair.dark_bgr is not None:
                display_frame = pair.diff_bgr
            else:
                display_frame = pair.bright_bgr

        if display_frame is None:
            return None, None, None, None

        det: FaceDetection | None = None
        eval_src = display_frame
        if self.recognizer is not None:
            det = self.recognizer.detect_largest(eval_src)
            if det is None:
                enhanced = enhance_ir_bgr(eval_src)
                det = self.recognizer.detect_largest(enhanced)
                if det is not None:
                    eval_src = enhanced

        liveness: LivenessResult | None = None
        if pair is not None:
            if pair.bright_mean > 225.0:
                self.ae_var.set("AE Status: Settling Flash...")
                self.ae_badge.configure(fg=_ACCENT_AMBER)
            else:
                self.ae_var.set("AE Status: Normal")
                self.ae_badge.configure(fg=_ACCENT_GREEN)

            if det is not None:
                liveness = check_ir_liveness(pair, det.bbox)
                if liveness.reason == "overexposed_ae_settling":
                    self.ae_var.set("AE Status: Settling Flash...")
                    self.ae_badge.configure(fg=_ACCENT_AMBER)

                self.liveness_var.set(
                    f"IR Δ: {liveness.delta:5.1f}  |  BG: {liveness.bg_ratio:.2f}  |  "
                    f"3D: {liveness.relief_3d:+.2f}  |  {liveness.reason}"
                )
            else:
                self.liveness_var.set("IR Δ: --  |  BG: --  |  3D: --  |  No face")
        elif not self.current_cam_is_ir:
            self.liveness_var.set("IR Δ: --  |  BG: --  |  3D: --  |  Non-IR camera")

        # Handle enrollment sample capture on new IR pairs
        if (
            self._enrolling
            and pair is not None
            and pair.timestamp != self._last_processed_pair_ts
            and self.recognizer is not None
            and det is not None
            and liveness is not None
            and liveness.passed
        ):
            now = time.monotonic()
            if now - self._last_enroll_capture_ts >= 0.45:
                emb = self.recognizer.extract_embedding(eval_src, det)
                self._enroll_embeddings.append(emb)
                self._last_enroll_capture_ts = now
                captured = len(self._enroll_embeddings)
                if captured >= self._enroll_target:
                    username = self.username_var.get().strip()
                    self.store.upsert_embeddings(username, self._enroll_embeddings, append=True)
                    self._enrolling = False
                    self._enroll_embeddings = []
                    self.btn_enroll.configure(text="Enroll User (8 Samples)")
                    self.prompt_var.set(f"Enrollment complete for '{username}'!")
                    self.status_var.set(f"Saved {captured} IR samples for '{username}'.")
                    self._refresh_profiles_list()
                    self._stop_camera_if_idle()
                    if self.worker is None:
                        return None, None, None, None
                else:
                    self.prompt_var.set(_POSE_PROMPTS[captured])

        # Handle live verification overlay
        if (
            self._live_match_enabled
            and self.recognizer is not None
            and det is not None
        ):
            thresh = self._get_match_threshold()
            emb = self.recognizer.extract_embedding(eval_src, det)
            best_user = ""
            best_score = 0.0
            for profile in self.store.list_users():
                if not profile.embeddings:
                    continue
                _, score = self.recognizer.match_against(
                    emb,
                    profile.embedding_arrays(),
                    threshold=thresh,
                )
                if score > best_score:
                    best_score = score
                    best_user = profile.username

            live_ok = self.current_cam_is_ir and bool(liveness and liveness.passed)
            motion_ok = True
            motion_reason = ""
            motion_src = (
                pair.diff_bgr if (pair is not None and pair.dark_bgr is not None) else eval_src
            )
            if live_ok and self.require_motion_var.get():
                motion_ts = pair.timestamp if pair is not None else None
                motion_ok, motion_reason = self._motion_tracker.update(
                    motion_src,
                    det,
                    timestamp=motion_ts,
                )

            eye_res: EyeOpenResult | None = None
            if live_ok and self.require_open_eyes_var.get():
                eye_res = check_eyes_open_ir(motion_src, det)
                if liveness is not None:
                    self.liveness_var.set(
                        f"IR Δ: {liveness.delta:5.1f}  |  BG: {liveness.bg_ratio:.2f}  |  "
                        f"3D: {liveness.relief_3d:+.2f}  |  "
                        f"Eyes: {eye_res.left_score:.2f}/{eye_res.right_score:.2f}  |  "
                        f"{liveness.reason}"
                    )

            if not live_ok:
                reason_tag = liveness.reason if liveness is not None else "no_ir"
                if best_user:
                    self.match_var.set(f"Match: No ({best_user}? {best_score:.3f}, {reason_tag})")
                else:
                    self.match_var.set(f"Match: Blocked ({reason_tag})")
                self.match_label.configure(fg=_ACCENT_RED)
            elif not motion_ok:
                self.match_var.set("Match: Blink / move head (3D)")
                self.match_label.configure(fg=_ACCENT_AMBER)
            elif eye_res is not None and not eye_res.passed:
                self.match_var.set(f"Match: Eyes closed ({eye_res.min_score:.2f})")
                self.match_label.configure(fg=_ACCENT_AMBER)
            elif best_user and best_score >= thresh:
                suffix = f", {motion_reason}" if motion_reason else ""
                self.match_var.set(f"Match: {best_user} ({best_score:.3f}{suffix})")
                self.match_label.configure(fg=_ACCENT_GREEN)
            elif best_user:
                self.match_var.set(f"Match: No ({best_user}? {best_score:.3f})")
                self.match_label.configure(fg=_ACCENT_RED)
            else:
                self.match_var.set("Match: No enrolled profiles")
                self.match_label.configure(fg=_ACCENT_AMBER)

        if pair is not None:
            self._last_processed_pair_ts = pair.timestamp

        return display_frame, det, liveness, eval_src

    def _draw_overlay(
        self,
        frame: np.ndarray,
        det: FaceDetection | None,
        liveness: LivenessResult | None,
    ) -> np.ndarray:
        canvas = frame.copy()
        if det is not None:
            x, y, w, h = det.bbox
            passed = bool(liveness and liveness.passed) if self.current_cam_is_ir else True
            color = (34, 197, 94) if passed else (68, 68, 239)  # BGR: green or red
            cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 2)
            label = f"score={det.score:.2f}"
            if liveness is not None:
                label = f"IR d={liveness.delta:.1f} ({liveness.reason})"
            cv2.putText(
                canvas,
                label,
                (max(4, x), max(18, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                cv2.LINE_AA,
            )
        return canvas

    def _update_loop(self) -> None:
        try:
            frame, det, liveness, _ = self._evaluate_frame()
            if frame is not None:
                annotated = self._draw_overlay(frame, det, liveness)
                rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
                pil_img = Image.fromarray(rgb)

                lbl_w = max(320, self.video_label.winfo_width())
                lbl_h = max(240, self.video_label.winfo_height())
                pil_img.thumbnail((lbl_w, lbl_h), Image.Resampling.BILINEAR)

                photo = ImageTk.PhotoImage(image=pil_img)
                self._photo_ref = photo
                self.video_label.configure(image=photo, text="")
        except Exception as exc:
            self.status_var.set(f"UI update error: {exc}")

        self.after(30, self._update_loop)

    def _on_close(self) -> None:
        self._preview_enabled = False
        self._enrolling = False
        self._live_match_enabled = False
        self._stop_worker()
        self.destroy()


def main() -> None:
    """Launch the dark-mode IR Face ID Enrollment GUI."""
    app = IREnrollmentApp()
    app.mainloop()


if __name__ == "__main__":
    main()
