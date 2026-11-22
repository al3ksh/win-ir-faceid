<#
.SYNOPSIS
    Migrates the WinIRFaceID profile store and installs WinIRFaceIDService as a LocalSystem Windows Service.
.DESCRIPTION
    Must be run from an elevated (Administrator) PowerShell prompt.
    1. Migrates data\profiles.enc to machine-scoped DPAPI with hardened NTFS ACL.
    2. Stops and deletes any existing WinIRFaceIDService registration.
    3. Registers WinIRFaceIDService (LocalSystem, auto-start) pointing to WinIRFaceIDService.exe.
    4. Starts the service and verifies the LocalSystem Named Pipe with pipe_client_test.exe ping.
#>
[CmdletBinding()]
param(
    [string]$BinDir = ""
)

$ErrorActionPreference = "Stop"
$ServiceName = "WinIRFaceIDService"
$ServiceDescription = "WinIRFaceID Active Infrared Face Recognition Service"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Administrator privileges are required. Re-run this script from an elevated PowerShell prompt."
    exit 1
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"

function Resolve-ServiceBinary {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FileName
    )

    if (-not [string]::IsNullOrWhiteSpace($BinDir)) {
        $candidate = Join-Path $BinDir $FileName
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
        throw "$FileName not found in specified BinDir '$BinDir'."
    }

    $candidates = @(
        (Join-Path $RepoRoot "bin\$FileName"),
        (Join-Path $RepoRoot "credential_provider\build\Release\$FileName")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "$FileName not found in '$RepoRoot\bin' or '$RepoRoot\credential_provider\build\Release'. Download a prebuilt release or build credential_provider first (x64 Release)."
}

$ServiceExe = Resolve-ServiceBinary -FileName "WinIRFaceIDService.exe"
$PipeTestExe = Resolve-ServiceBinary -FileName "pipe_client_test.exe"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python virtualenv not found at '$PythonExe'."
}

Write-Host "Migrating profile store to machine-scoped DPAPI..."
Push-Location $RepoRoot
try {
    & $PythonExe -m engine.cli migrate-store
    if ($LASTEXITCODE -ne 0) {
        throw "engine.cli migrate-store failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Write-Host "Removing existing '$ServiceName' registration..."
    if ($existing.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
        & sc.exe stop $ServiceName | Out-Null
        $stopDeadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $stopDeadline) {
            Start-Sleep -Milliseconds 300
            $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if ($null -eq $svc -or $svc.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
                break
            }
        }
    }

    & sc.exe delete $ServiceName | Out-Null
    $deleteDeadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deleteDeadline) {
        Start-Sleep -Milliseconds 250
        if ($null -eq (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
            break
        }
    }
}

Write-Host "Creating service '$ServiceName' (LocalSystem, auto-start)..."
$scCreateArgs = "create $ServiceName start= auto obj= LocalSystem DisplayName= `"$ServiceDescription`" binPath= `"\`"$ServiceExe\`" --repo-root \`"$RepoRoot\`"`""
$createProc = Start-Process -FilePath "sc.exe" -ArgumentList $scCreateArgs -Wait -NoNewWindow -PassThru
if ($createProc.ExitCode -ne 0) {
    throw "sc.exe create failed with exit code $($createProc.ExitCode)"
}

& sc.exe description $ServiceName $ServiceDescription
if ($LASTEXITCODE -ne 0) {
    Write-Warning "sc.exe description returned exit code $LASTEXITCODE"
}

Write-Host "Starting '$ServiceName'..."
& sc.exe start $ServiceName
if ($LASTEXITCODE -ne 0) {
    throw "sc.exe start failed with exit code $LASTEXITCODE"
}

# First start in a fresh venv compiles .pyc files and loads OpenCV; allow time for that.
$waitDeadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $waitDeadline) {
    Start-Sleep -Milliseconds 500
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $svc -and $svc.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Running) {
        & $PipeTestExe ping 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            break
        }
    }
}

Write-Host "Verifying LocalSystem Named Pipe via pipe_client_test.exe ping..."
& $PipeTestExe ping
if ($LASTEXITCODE -ne 0) {
    throw "Named Pipe verification failed after starting '$ServiceName'. Inspect '$RepoRoot\data\service.log'."
}

Write-Host ""
Write-Host "WinIRFaceIDService installed, started, and verified successfully."
