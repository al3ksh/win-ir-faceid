<#
.SYNOPSIS
    Installs and registers WinIRFaceIDProvider.dll into System32 (run as Administrator).
.DESCRIPTION
    Copies the built x64 Release DLL to C:\Windows\System32 and imports register.reg.
    This provider is strictly additive alongside Windows Hello Fingerprint, PIN, and Password.
#>
[CmdletBinding()]
param(
    [string]$DllPath = (Join-Path $PSScriptRoot "build\Release\WinIRFaceIDProvider.dll")
)

$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Administrator privileges are required. Re-run this script from an elevated PowerShell prompt."
}

if (-not (Test-Path -LiteralPath $DllPath)) {
    throw "DLL not found at '$DllPath'. Build the project first: cmake -B build -A x64 && cmake --build build --config Release"
}

$destPath = Join-Path $env:WINDIR "System32\WinIRFaceIDProvider.dll"
$regFile = Join-Path $PSScriptRoot "register.reg"

if (-not (Test-Path -LiteralPath $regFile)) {
    throw "Registration file not found at '$regFile'."
}

Write-Host "Copying '$DllPath' -> '$destPath'..."
Copy-Item -LiteralPath $DllPath -Destination $destPath -Force

Write-Host "Importing '$regFile'..."
& reg.exe import $regFile
if ($LASTEXITCODE -ne 0) {
    throw "reg.exe import failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "WinIRFaceIDProvider installed and registered successfully."
Write-Host "Reminder:"
Write-Host "  1. Ensure the WinIRFaceID engine / service is running before locking the PC."
Write-Host "  2. Windows Hello Fingerprint, PIN, and Password remain enabled."
Write-Host "  3. To uninstall cleanly, run .\uninstall.ps1 as Administrator."
