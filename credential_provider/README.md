# WinIRFaceID Windows Credential Provider V2

Thin C++17 Windows Credential Provider V2 COM DLL (`WinIRFaceIDProvider.dll`) and standalone Named Pipe CLI tester (`pipe_client_test.exe`).

## Architecture & Safety Design

- **Zero AI / camera code in `LogonUI.exe`:** All IR camera control (`IOCTL_KS_PROPERTY`), liveness checks, and SFace recognition run outside `LogonUI.exe` in the Python/service engine. The DLL only exchanges newline-delimited UTF-8 JSON over Named Pipe `\\.\pipe\WinIRFaceID`.
- **Strictly additive:** Does not implement `ICredentialProviderFilter` and never disables Windows Hello Fingerprint, PIN, or Password.
- **Non-blocking cancellation:** Overlapped Win32 Named Pipe I/O paired with `CancelIoEx` ensures `SetDeselected` and `UnAdvise` return immediately without hanging `LogonUI.exe`.
- **Memory hygiene:** Plaintext passwords are wiped via `SecureZeroMemory` immediately after `KERB_INTERACTIVE_UNLOCK_LOGON` packing and in destructors.
- **Supported scenarios:** `CPUS_LOGON` and `CPUS_UNLOCK_WORKSTATION` (`CPUS_CHANGE_PASSWORD` and `CPUS_CREDUI` return `E_NOTIMPL`).

## Build (x64, CMake + Visual Studio)

```powershell
cd C:\Users\DELL\.gemini\antigravity\scratch\win-ir-faceid\credential_provider
# Using Visual Studio 18's bundled CMake (recommended when VS 18 / 2026 is installed):
& "C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" -B build -A x64
& "C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" --build build --config Release
```
*(Note: If a standalone `cmake` on `PATH` defaults to Ninja instead of Visual Studio, pass `-G "Visual Studio 17 2022" -A x64` or use VS 18's bundled `cmake.exe` above.)*

Build outputs:
- `build\Release\WinIRFaceIDProvider.dll`
- `build\Release\pipe_client_test.exe`

## Testing IPC Before Registering the DLL

Never register a Credential Provider before verifying IPC end-to-end with `pipe_client_test.exe`:

1. Start the Python Named Pipe server in one terminal.
2. In another terminal:
   ```powershell
   .\build\Release\pipe_client_test.exe ping
   .\build\Release\pipe_client_test.exe verify
   ```

## Installation & Registration (Manual, Elevated)

1. Run an **Administrator** PowerShell prompt:
   ```powershell
   cd C:\Users\DELL\.gemini\antigravity\scratch\win-ir-faceid\credential_provider
   .\install.ps1
   ```
   This copies `WinIRFaceIDProvider.dll` to `C:\Windows\System32\WinIRFaceIDProvider.dll` and imports `register.reg` (`CLSID {49C8AEC3-2A43-4BED-987D-DE31E44DF41D}`).

2. Verify before logging out:
   - Lock the workstation (`Win + L`) while keeping another sign-in method (PIN, Fingerprint, or Password) available.
   - Select **IR Face ID** under **Sign-in options** on your user tile.

## Uninstallation

Run in an **Administrator** PowerShell prompt:
```powershell
cd C:\Users\DELL\.gemini\antigravity\scratch\win-ir-faceid\credential_provider
.\uninstall.ps1
```

## Emergency Safe Mode Recovery

If `LogonUI.exe` ever fails to load or sign in after registration:
1. Boot into **Safe Mode with Command Prompt** (hold `Shift` while clicking **Restart** -> **Troubleshoot** -> **Advanced options** -> **Startup Settings** -> **F6 / 6**).
2. Remove the Credential Provider registry entries:
   ```cmd
   reg delete "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\{49C8AEC3-2A43-4BED-987D-DE31E44DF41D}" /f
   reg delete "HKLM\SOFTWARE\Classes\CLSID\{49C8AEC3-2A43-4BED-987D-DE31E44DF41D}" /f
   del C:\Windows\System32\WinIRFaceIDProvider.dll
   ```
3. Reboot normally.
