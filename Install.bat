@echo off
setlocal
set "REPO_DIR=%~dp0"
if "%REPO_DIR:~-1%"=="\" set "REPO_DIR=%REPO_DIR:~0,-1%"
set "CALLER_USER=%USERDOMAIN%\%USERNAME%"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator privileges to install WinIRFaceID...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$scriptPath = Join-Path $env:REPO_DIR 'scripts\setup.ps1'; $userAcct = $env:CALLER_USER; $cmd = '& { try { & ''' + ($scriptPath -replace '''','''''') + ''' -UserAccount ''' + ($userAcct -replace '''','''''') + '''; if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw \"Setup exited with code $LASTEXITCODE\" } } catch { Write-Host $_ -ForegroundColor Red; Read-Host ''Press Enter to close''; exit 1 } }'; $bytes = [System.Text.Encoding]::Unicode.GetBytes($cmd); $b64 = [Convert]::ToBase64String($bytes); $p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $b64); exit $p.ExitCode"
    exit /b %errorlevel%
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO_DIR%\scripts\setup.ps1" -UserAccount "%CALLER_USER%" %*
if %errorlevel% neq 0 (
    echo.
    echo WinIRFaceID setup failed with exit code %errorlevel%.
    pause
    exit /b %errorlevel%
)

exit /b 0
