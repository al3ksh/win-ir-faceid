"""WinIRFaceID Camera Tester GUI for Windows Hello IR and RGB cameras."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.ir_camera import (  # noqa: E402
    AntiFlickerMode,
    CameraDescriptor,
    WinRTCameraWorker,
    discover_cameras,
)

SAVE_DIR = os.path.join(os.path.expanduser("~"), "Pictures", "WinIRFaceID_Captures")
os.makedirs(SAVE_DIR, exist_ok=True)


class IRCameraTesterApp:
    """Interactive Tkinter viewer for testing WinRT IR/RGB streams, IR LED pulsing, and capture."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("WinIRFaceID — IR & RGB Camera Tester")
        self.root.configure(bg="#121216")
        self.root.geometry("1160x720")
        self.root.minsize(900, 580)

        self.cameras: list[CameraDescriptor] = discover_cameras()
        self.worker_primary: WinRTCameraWorker | None = None
        self.worker_secondary: WinRTCameraWorker | None = None
        self.running = True
        self.dual_mode = False
        self.ir_led_on = True

        self.mirror = tk.BooleanVar(value=True)
        self.ir_filter = tk.StringVar(value="Original")
        self.ir_frame_mode = tk.StringVar(value="Anti-Flicker (IR)")
        self.selected_cam_var = tk.StringVar()

        self.recording = False
        self.video_writer: cv2.VideoWriter | None = None
        self.record_start_time = 0.0
        self.last_frame_bgr: np.ndarray | None = None

        self._build_ui()

        if self.cameras:
            self.selected_cam_var.set(self.cameras[0].label)
            self.open_single_camera(self.cameras[0])
        else:
            self.status_var.set("No WinRT camera detected.")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.update_loop()

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#121216")
        style.configure("Top.TFrame", background="#1b1b22")
        style.configure("TLabel", background="#1b1b22", foreground="#e6e6f0", font=("Segoe UI", 9))
        style.configure("TCheckbutton", background="#1b1b22", foreground="#e6e6f0", font=("Segoe UI", 9))

        top = ttk.Frame(self.root, style="Top.TFrame", padding=(10, 8))
        top.pack(fill=tk.X)

        ttk.Label(top, text="Camera:").pack(side=tk.LEFT, padx=(0, 5))
        cam_labels = [c.label for c in self.cameras] or ["No cameras found"]
        self.cam_combo = ttk.Combobox(
            top,
            textvariable=self.selected_cam_var,
            values=cam_labels,
            state="readonly",
            width=34,
        )
        self.cam_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.cam_combo.bind("<<ComboboxSelected>>", self.on_camera_select)

        has_both = any(c.is_ir and not c.is_virtual for c in self.cameras) and any(
            (not c.is_ir) and (not c.is_virtual) for c in self.cameras
        )
        self.btn_dual = tk.Button(
            top,
            text="Dual (IR + RGB)",
            command=self.toggle_dual_mode,
            bg="#2b2d3a",
            fg="#ffffff",
            activebackground="#3b3e52",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=10,
            pady=4,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self.btn_dual.pack(side=tk.LEFT, padx=(0, 8))
        if not has_both:
            self.btn_dual.config(state=tk.DISABLED)

        self.btn_ir_led = tk.Button(
            top,
            text="IR LED: ON",
            command=self.toggle_ir_led,
            bg="#b91c1c",
            fg="#ffffff",
            activebackground="#991b1b",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=10,
            pady=4,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self.btn_ir_led.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(top, text="IR Frame Mode:").pack(side=tk.LEFT, padx=(0, 4))
        frame_mode_combo = ttk.Combobox(
            top,
            textvariable=self.ir_frame_mode,
            values=["Anti-Flicker (IR)", "IR - Ambient (Hello)", "Raw (Pulsing)"],
            state="readonly",
            width=19,
        )
        frame_mode_combo.pack(side=tk.LEFT, padx=(0, 8))
        frame_mode_combo.bind("<<ComboboxSelected>>", self.on_frame_mode_change)

        ttk.Label(top, text="Filter:").pack(side=tk.LEFT, padx=(0, 4))
        filter_combo = ttk.Combobox(
            top,
            textvariable=self.ir_filter,
            values=[
                "Original",
                "Night Vision (Green)",
                "Thermal (Inferno)",
                "High Contrast (CLAHE)",
            ],
            state="readonly",
            width=21,
        )
        filter_combo.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Checkbutton(top, text="Mirror", variable=self.mirror).pack(side=tk.LEFT)

        self.video_label = tk.Label(
            self.root,
            bg="#0a0a0d",
            text="Starting camera...",
            fg="#666a7a",
            font=("Segoe UI", 13),
        )
        self.video_label.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

        bottom = ttk.Frame(self.root, style="Top.TFrame", padding=(12, 10))
        bottom.pack(fill=tk.X)

        self.btn_photo = tk.Button(
            bottom,
            text="Take Photo",
            command=self.take_photo,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=16,
            pady=7,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.btn_photo.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_record = tk.Button(
            bottom,
            text="Record Video",
            command=self.toggle_recording,
            bg="#dc2626",
            fg="#ffffff",
            activebackground="#b91c1c",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=16,
            pady=7,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.btn_record.pack(side=tk.LEFT, padx=(0, 10))

        btn_folder = tk.Button(
            bottom,
            text="Open Captures Folder",
            command=self.open_save_folder,
            bg="#2b2d3a",
            fg="#e6e6f0",
            activebackground="#3b3e52",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=12,
            pady=7,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        btn_folder.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value=f"Saving to: {SAVE_DIR}")
        ttk.Label(bottom, textvariable=self.status_var, style="TLabel").pack(side=tk.RIGHT)

    def _get_anti_flicker_key(self) -> AntiFlickerMode:
        val = self.ir_frame_mode.get()
        if val == "IR - Ambient (Hello)":
            return "subtract"
        if val == "Raw (Pulsing)":
            return "raw"
        return "bright"

    def on_frame_mode_change(self, _event: object = None) -> None:
        mode = self._get_anti_flicker_key()
        if self.worker_primary and self.worker_primary.is_ir:
            self.worker_primary.anti_flicker_mode = mode

    def toggle_ir_led(self) -> None:
        self.ir_led_on = not self.ir_led_on
        if self.ir_led_on:
            self.btn_ir_led.config(text="IR LED: ON", bg="#b91c1c")
        else:
            self.btn_ir_led.config(text="IR LED: OFF", bg="#2b2d3a")
        if self.worker_primary and self.worker_primary.is_ir:
            self.worker_primary.set_ir_led(self.ir_led_on)

    def _stop_workers(self) -> None:
        if self.recording:
            self.toggle_recording()
        if self.worker_primary is not None:
            self.worker_primary.stop()
            self.worker_primary = None
        if self.worker_secondary is not None:
            self.worker_secondary.stop()
            self.worker_secondary = None

    def open_single_camera(self, cam: CameraDescriptor) -> None:
        self._stop_workers()
        self.dual_mode = False
        self.btn_dual.config(bg="#2b2d3a", text="Dual (IR + RGB)")
        self.worker_primary = WinRTCameraWorker(
            cam.group,
            cam.source_id,
            is_ir=cam.is_ir,
            ir_led_enabled=self.ir_led_on,
        )
        self.worker_primary.anti_flicker_mode = self._get_anti_flicker_key()
        self.worker_primary.start()
        self.status_var.set(f"Active: {cam.label}")

    def toggle_dual_mode(self) -> None:
        if self.dual_mode:
            self.on_camera_select()
            return
        ir_cam = next((c for c in self.cameras if c.is_ir and not c.is_virtual), None)
        rgb_cam = next((c for c in self.cameras if (not c.is_ir) and (not c.is_virtual)), None)
        if not ir_cam or not rgb_cam:
            return
        self._stop_workers()
        self.dual_mode = True
        self.btn_dual.config(bg="#7c3aed", text="Dual: ON")
        self.worker_primary = WinRTCameraWorker(
            ir_cam.group,
            ir_cam.source_id,
            is_ir=True,
            ir_led_enabled=self.ir_led_on,
        )
        self.worker_primary.anti_flicker_mode = self._get_anti_flicker_key()
        self.worker_secondary = WinRTCameraWorker(
            rgb_cam.group,
            rgb_cam.source_id,
            is_ir=False,
        )
        self.worker_primary.start()
        self.worker_secondary.start()
        self.status_var.set("Dual Mode: Infrared (IR + LED) + RGB")

    def on_camera_select(self, _event: object = None) -> None:
        sel = self.selected_cam_var.get()
        for cam in self.cameras:
            if cam.label == sel:
                self.open_single_camera(cam)
                break

    def apply_filter(self, frame_bgr: np.ndarray) -> np.ndarray:
        mode = self.ir_filter.get()
        if mode == "Original":
            return frame_bgr
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        if mode == "High Contrast (CLAHE)":
            clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
            return cv2.cvtColor(clahe.apply(gray), cv2.COLOR_GRAY2BGR)
        if mode == "Thermal (Inferno)":
            return cv2.applyColorMap(gray, cv2.COLORMAP_INFERNO)
        if mode == "Night Vision (Green)":
            clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
            enhanced = clahe.apply(gray)
            out = np.zeros_like(frame_bgr)
            out[:, :, 1] = enhanced
            out[:, :, 0] = (enhanced * 0.15).astype(np.uint8)
            return out
        return frame_bgr

    def _read_composed_frame(self) -> np.ndarray | None:
        if self.dual_mode and self.worker_primary and self.worker_secondary:
            if self.worker_primary.error:
                self.status_var.set(f"IR camera error: {self.worker_primary.error}")
                return None
            if self.worker_secondary.error:
                self.status_var.set(f"RGB camera error: {self.worker_secondary.error}")
                return None
            f_ir = self.worker_primary.get_frame()
            f_rgb = self.worker_secondary.get_frame()
            if f_ir is None or f_rgb is None:
                return None
            if self.mirror.get():
                f_ir = cv2.flip(f_ir, 1)
                f_rgb = cv2.flip(f_rgb, 1)
            f_ir = self.apply_filter(f_ir)
            target_h = 480
            f1_r = cv2.resize(f_ir, (int(f_ir.shape[1] * target_h / f_ir.shape[0]), target_h))
            f2_r = cv2.resize(f_rgb, (int(f_rgb.shape[1] * target_h / f_rgb.shape[0]), target_h))
            return np.hstack([f1_r, f2_r])

        if self.worker_primary is not None:
            if self.worker_primary.error:
                self.status_var.set(f"Camera error: {self.worker_primary.error}")
                return None
            frame = self.worker_primary.get_frame()
            if frame is None:
                return None
            if self.mirror.get():
                frame = cv2.flip(frame, 1)
            return self.apply_filter(frame)
        return None

    def update_loop(self) -> None:
        if not self.running:
            return

        frame = self._read_composed_frame()
        if frame is not None:
            self.last_frame_bgr = frame

            if self.recording and self.video_writer is not None:
                self.video_writer.write(frame)
                elapsed = int(time.time() - self.record_start_time)
                self.status_var.set(f"REC ● {elapsed // 60:02d}:{elapsed % 60:02d}")

            lbl_w = max(self.video_label.winfo_width(), 320)
            lbl_h = max(self.video_label.winfo_height(), 240)
            fh, fw = frame.shape[:2]
            scale = min(lbl_w / fw, lbl_h / fh)
            new_w, new_h = max(1, int(fw * scale)), max(1, int(fh * scale))

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb).resize((new_w, new_h), Image.Resampling.BILINEAR)
            imgtk = ImageTk.PhotoImage(image=img)
            self.video_label.imgtk = imgtk
            self.video_label.configure(image=imgtk, text="")

        self.root.after(20, self.update_loop)

    def take_photo(self) -> None:
        if self.last_frame_bgr is None:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SAVE_DIR, f"cam_{ts}.png")
        cv2.imwrite(path, self.last_frame_bgr)
        self.status_var.set(f"Saved photo: {os.path.basename(path)}")

    def toggle_recording(self) -> None:
        if self.recording:
            self.recording = False
            if self.video_writer is not None:
                self.video_writer.release()
                self.video_writer = None
            self.btn_record.config(text="Record Video", bg="#dc2626")
            self.status_var.set("Recording stopped.")
            return

        if self.last_frame_bgr is None:
            return
        h, w = self.last_frame_bgr.shape[:2]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SAVE_DIR, f"vid_{ts}.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.video_writer = cv2.VideoWriter(path, fourcc, 25.0, (w, h))
        self.recording = True
        self.record_start_time = time.time()
        self.btn_record.config(text="Stop Recording", bg="#991b1b")
        self.status_var.set(f"Recording: {os.path.basename(path)}")

    def open_save_folder(self) -> None:
        subprocess.Popen(["explorer", SAVE_DIR])

    def on_close(self) -> None:
        self.running = False
        self._stop_workers()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = IRCameraTesterApp(root)
    root.mainloop()
