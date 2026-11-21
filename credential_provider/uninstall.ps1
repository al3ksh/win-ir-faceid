<#
.SYNOPSIS
    Unregisters and removes WinIRFaceIDProvider.dll from System32 (run as Administrator).
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Administrator privileges are required. Re-run this script from an elevated PowerShell prompt."
}

$regFile = Join-Path $PSScriptRoot "unregister.reg"
$destPath = Join-Path $env:WINDIR "System32\WinIRFaceIDProvider.dll"

if (Test-Path -LiteralPath $regFile) {
    Write-Host "Importing '$regFile'..."
    & reg.exe import $regFile
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "reg.exe import returned exit code $LASTEXITCODE"
    }
}

if (Test-Path -LiteralPath $destPath) {
    try {
        Remove-Item -LiteralPath $destPath -Force
        Write-Host "Removed '$destPath'."
    }
    catch {
        Write-Warning "Could not remove '$destPath' immediately (it may still be loaded by LogonUI.exe). Reboot and delete it manually, or re-run uninstall.ps1 after reboot."
    }
}
else {
    Write-Host "'$destPath' is already absent."
}

Write-Host "WinIRFaceIDProvider unregistered."
