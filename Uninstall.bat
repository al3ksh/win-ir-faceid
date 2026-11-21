@echo off
setlocal
set "REPO_DIR=%~dp0"
if "%REPO_DIR:~-1%"=="\" set "REPO_DIR=%REPO_DIR:~0,-1%"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator privileges to uninstall WinIRFaceID...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$repo = $env:REPO_DIR; $cpUninstall = Join-Path $repo 'credential_provider\uninstall.ps1'; $svcUninstall = Join-Path $repo 'scripts\uninstall_service.ps1'; $cmd = '& { $ErrorActionPreference = ''Stop''; try { if (Test-Path -LiteralPath ''' + ($cpUninstall -replace '''','''''') + ''') { & ''' + ($cpUninstall -replace '''','''''') + ''' }; if (Test-Path -LiteralPath ''' + ($svcUninstall -replace '''','''''') + ''') { & ''' + ($svcUninstall -replace '''','''''') + ''' }; $links = @((Join-Path $env:ProgramData ''Microsoft\Windows\Start Menu\Programs\WinIRFaceID.lnk''), (Join-Path $env:APPDATA ''Microsoft\Windows\Start Menu\Programs\WinIRFaceID.lnk''), (Join-Path ([Environment]::GetFolderPath(''Desktop'')) ''WinIRFaceID.lnk''), (Join-Path ([Environment]::GetFolderPath(''CommonDesktopDirectory'')) ''WinIRFaceID.lnk'')); foreach ($lnk in $links) { if ($lnk -and (Test-Path -LiteralPath $lnk)) { Remove-Item -LiteralPath $lnk -Force -ErrorAction SilentlyContinue; Write-Host \"Removed shortcut: $lnk\" } }; Write-Host ''''; Write-Host ''WinIRFaceID lock screen integration and service uninstalled.'' -ForegroundColor Green } catch { Write-Host $_ -ForegroundColor Red; Read-Host ''Press Enter to close''; exit 1 } }'; $bytes = [System.Text.Encoding]::Unicode.GetBytes($cmd); $b64 = [Convert]::ToBase64String($bytes); $p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $b64); exit $p.ExitCode"
    exit /b %errorlevel%
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $ErrorActionPreference = 'Stop'; $repo = $env:REPO_DIR; if (Test-Path -LiteralPath (Join-Path $repo 'credential_provider\uninstall.ps1')) { & (Join-Path $repo 'credential_provider\uninstall.ps1') }; if (Test-Path -LiteralPath (Join-Path $repo 'scripts\uninstall_service.ps1')) { & (Join-Path $repo 'scripts\uninstall_service.ps1') }; $links = @((Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\WinIRFaceID.lnk'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\WinIRFaceID.lnk'), (Join-Path ([Environment]::GetFolderPath('Desktop')) 'WinIRFaceID.lnk'), (Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) 'WinIRFaceID.lnk')); foreach ($lnk in $links) { if ($lnk -and (Test-Path -LiteralPath $lnk)) { Remove-Item -LiteralPath $lnk -Force -ErrorAction SilentlyContinue; Write-Host ('Removed shortcut: ' + $lnk) } } }"
if %errorlevel% neq 0 (
    echo.
    echo WinIRFaceID uninstall failed with exit code %errorlevel%.
    pause
    exit /b %errorlevel%
)

echo.
echo WinIRFaceID uninstalled cleanly.
exit /b 0
