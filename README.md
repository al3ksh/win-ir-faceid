# WinIRFaceID

Custom Windows infrared (IR) facial recognition suite built for laptops with Windows Hello IR cameras. Designed to work in complete darkness using active IR illumination (`IR - Ambient` frame subtraction) alongside native Windows Hello Fingerprint, PIN, and Password sign-in.

## Architecture

The project intentionally separates hardware/AI processing from `LogonUI.exe` for stability and safety:

1. **`tools/ir_camera_tester/`** — Standalone English IR/RGB camera tester with hardware IR illuminator toggle (`KSPROPERTY_CAMERACONTROL_EXTENDED_FACEAUTH_MODE`), anti-flicker, `IR - Ambient` mode, Dual view, snapshots, and MP4 recording.
2. **`engine/`** — Python IR Face ID Engine:
   - `ir_camera.py`: WinRT `MediaCapture` + direct `IOCTL_KS_PROPERTY` IR LED illuminator control and synchronized bright/dark frame pairing.
   - `models.py` & `recognizer.py`: OpenCV Zoo YuNet face detection + SFace 128-D embeddings with cosine similarity matching.
   - `liveness.py`: Active IR illumination delta liveness check (rejects LCD/OLED phone screens and startup AE overexposure frames).
   - `store.py`: Windows DPAPI-encrypted face embeddings and optional logon credentials.
   - `ipc_server.py`: Windows Named Pipe server (`\\.\pipe\WinIRFaceID`) implementing [docs/IPC_PROTOCOL.md](docs/IPC_PROTOCOL.md).
   - `gui_enroll.py` & `cli.py`: Enrollment & live verification GUI and CLI.
3. **`credential_provider/`** — Thin C++17 Windows Credential Provider V2 DLL (`WinIRFaceIDProvider.dll`) + standalone `pipe_client_test.exe`:
   - Strictly additive alongside Windows Hello Fingerprint, PIN, and Password.
   - Zero camera or AI code inside `LogonUI.exe`; delegates verification to the Named Pipe daemon and serializes `KERB_INTERACTIVE_UNLOCK_LOGON`.

## Quick Start

```powershell
# 1. Activate environment
.\.venv\Scripts\Activate.ps1

# 2. Launch the IR/RGB Camera Tester
python tools\ir_camera_tester\app.py

# 3. Download ONNX face models (YuNet + SFace)
python -m engine.cli download-models

# 4. Launch Enrollment & Live IR Verification GUI
python -m engine.cli gui

# 5. Run unit tests
pytest
```

## Safety Note

The C++ Credential Provider DLL is **not** automatically registered in Windows. Always test enrollment, dark-room verification, and C++ ↔ Python IPC (`pipe_client_test.exe`) thoroughly first, and keep Windows PIN/Fingerprint/Password enabled as fallback. See [credential_provider/README.md](credential_provider/README.md) before installing to `LogonUI`.
