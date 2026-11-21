# WinIRFaceID IPC Protocol Specification

The thin C++ Windows Credential Provider DLL communicates with the background IR Face ID Engine over a local Windows Named Pipe.

- **Pipe Name:** `\\.\pipe\WinIRFaceID`
- **Framing:** Newline-delimited UTF-8 JSON (`\n`-terminated, max 64 KB per message).
- **Security:** When deployed as a Windows Service, the pipe DACL restricts access to `LocalSystem` (which hosts `LogonUI.exe`) and `Administrators`. For local user-mode testing, the current user SID is permitted.

## Commands (Client → Server)

### 1. Ping
```json
{"cmd": "ping"}
```
Response:
```json
{"status": "ok", "version": "0.1.0", "enrolled_users": ["DELL"]}
```

### 2. Verify
Starts the IR camera and hardware IR LED illuminator (`KSPROPERTY_CAMERACONTROL_EXTENDED_FACEAUTH_MODE`), captures active-illumination frames (`IR - Ambient`), runs liveness and SFace cosine similarity matching, and immediately shuts off the IR LED upon match or timeout.
```json
{"cmd": "verify", "username": "", "timeout_ms": 8000}
```
- `username`: optional Windows username filter (empty string matches any enrolled user).
- `timeout_ms`: verification timeout in milliseconds (default `8000`).

Responses:
- Match succeeded:
```json
{"status": "matched", "username": "DELL", "domain": ".", "password": "<secret-or-empty>", "score": 0.68, "liveness_delta": 42.5}
```
- Match failed / timed out:
```json
{"status": "failed", "reason": "timeout"}
```
Possible `reason` values: `"timeout"`, `"no_enrolled_faces"`, `"camera_error"`, `"cancelled"`, `"bad_request"`.

### 3. Cancel
```json
{"cmd": "cancel"}
```
Response:
```json
{"status": "ok"}
```
