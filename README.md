# WinIRFaceID

Custom Windows infrared (IR) facial recognition suite built for laptops with Windows Hello IR cameras. Designed to work in complete darkness using active IR illumination (`IR - Ambient` frame subtraction) alongside native Windows Hello Fingerprint, PIN, and Password sign-in.

## Key Features

- **Strict IR-Only Recognition:** Verification and enrollment use only hardware IR cameras (no RGB fallback), powered by OpenCV Zoo YuNet + SFace 128-D embeddings with a calibrated `0.65` cosine similarity threshold.
- **3D IR Anti-Spoofing:** Rejects LCD/OLED phone screens, startup auto-exposure overexposure frames, flat polycarbonate ID cards, and paper/cutout prints via active IR delta, background depth falloff, and 3D center-vs-corner relief checks, plus optional open-eyes and blink / 3D head-motion verification.
- **Native Windows 11 Lock Screen UX:** `WinIRFaceIDProvider.dll` uses `ICredentialProviderCredential3::GetBitmapBufferValue` for true alpha-transparent Fluent icons (sign-in option tile + selected-tile hero icon) and starts scanning automatically in the background on `Win + L` even while PIN is selected—cancelling immediately when you sign in with another method.
- **LocalSystem Service & Microsoft Account Support:** `WinIRFaceIDService.exe` supervises the Python Named Pipe engine under `LocalSystem` using machine-scoped DPAPI (`CRYPTPROTECT_LOCAL_MACHINE`) with hardened NTFS ACLs, and supports both local accounts and Microsoft Accounts (`MicrosoftAccount\...`).
- **On-Demand Standby GUI:** Enrollment and live testing GUI keeps the IR camera and LED idle on standby until you actively start preview, enrollment, or verification.

## Architecture

1. **`tools/ir_camera_tester/`** — Standalone IR/RGB camera tester with hardware IR illuminator toggle (`KSPROPERTY_CAMERACONTROL_EXTENDED_FACEAUTH_MODE`), anti-flicker, `IR - Ambient` mode, Dual view, snapshots, and MP4 recording.
2. **`engine/`** — Python IR Face ID Engine:
   - `ir_camera.py`: WinRT `MediaCapture` + direct `IOCTL_KS_PROPERTY` IR LED illuminator control and synchronized bright/dark frame pairing.
   - `models.py` & `recognizer.py`: YuNet face detection (`0.78` score threshold) + SFace 128-D cosine similarity matching (`0.65` default threshold).
   - `liveness.py`: Active IR illumination delta, background depth falloff, 3D facial relief, open-eyes, and `TemporalLivenessTracker` (blink / 3D parallax motion) checks.
   - `store.py`: Windows DPAPI-encrypted face embeddings, service settings, and logon credentials.
   - `ipc_server.py`: Windows Named Pipe server (`\\.\pipe\WinIRFaceID`) implementing [docs/IPC_PROTOCOL.md](docs/IPC_PROTOCOL.md).
   - `gui_enroll.py` & `cli.py`: On-demand dark-mode Enrollment & Live Verification GUI (`launch_gui.bat`) and CLI.
3. **`credential_provider/`** — Thin C++17 Windows Credential Provider V2/V3 COM DLL (`WinIRFaceIDProvider.dll`), Named Pipe test CLI (`pipe_client_test.exe`), and `LocalSystem` service host (`WinIRFaceIDService.exe`). Zero camera or AI code runs inside `LogonUI.exe`.

## Quick Start

```powershell
# 1. Activate environment
.\.venv\Scripts\Activate.ps1

# 2. Download ONNX face models (YuNet + SFace)
python -m engine.cli download-models

# 3. Launch Enrollment & Live IR Verification GUI (or run launch_gui.bat)
python -m engine.cli gui

# 4. Run unit tests
pytest
```

## Service & Lock Screen Setup (Elevated PowerShell)

1. Build the C++ targets (`WinIRFaceIDProvider.dll`, `pipe_client_test.exe`, `WinIRFaceIDService.exe`) — see [credential_provider/README.md](credential_provider/README.md).
2. Migrate the encrypted profile store to machine-scoped DPAPI and install `WinIRFaceIDService`:
   ```powershell
   .\scripts\install_service.ps1
   ```
3. Verify the Named Pipe responds (`credential_provider\build\Release\pipe_client_test.exe ping` and `verify`), then register the Credential Provider:
   ```powershell
   cd credential_provider
   .\install.ps1
   ```

## Safety & Uninstall

- Keep Windows Hello PIN, Fingerprint, or Password enabled as fallback—the Credential Provider is strictly additive and never disables built-in sign-in methods.
- To uninstall completely (run in an **Administrator** PowerShell prompt):
  ```powershell
  .\credential_provider\uninstall.ps1
  .\scripts\uninstall_service.ps1
  ```
- See [credential_provider/README.md](credential_provider/README.md) for Safe Mode emergency recovery commands.
