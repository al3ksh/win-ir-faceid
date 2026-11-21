<#
.SYNOPSIS
    Stops and removes the WinIRFaceIDService Windows Service.
.DESCRIPTION
    Must be run from an elevated (Administrator) PowerShell prompt.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ServiceName = "WinIRFaceIDService"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Administrator privileges are required. Re-run this script from an elevated PowerShell prompt."
    exit 1
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($null -eq $existing) {
    Write-Host "Service '$ServiceName' is not installed."
    exit 0
}

if ($existing.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
    Write-Host "Stopping '$ServiceName'..."
    & sc.exe stop $ServiceName
    $stopDeadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $stopDeadline) {
        Start-Sleep -Milliseconds 300
        $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($null -eq $svc -or $svc.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
            break
        }
    }
}

Write-Host "Deleting '$ServiceName'..."
& sc.exe delete $ServiceName
if ($LASTEXITCODE -ne 0) {
    throw "sc.exe delete failed with exit code $LASTEXITCODE"
}

$deleteDeadline = (Get-Date).AddSeconds(5)
while ((Get-Date) -lt $deleteDeadline) {
    Start-Sleep -Milliseconds 250
    if ($null -eq (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        break
    }
}

Write-Host "Service '$ServiceName' stopped and deleted."
