<#
.SYNOPSIS
    Applies a verified WinIRFaceID delta update package in-place with Administrator privileges.
.DESCRIPTION
    Stops WinIRFaceIDService, updates code and native binaries without touching user data,
    ONNX models, the private Python runtime, or .venv, updates pip dependencies only if
    requirements.txt changed, registers the updated Credential Provider DLL, restarts and
    verifies the service, and optionally relaunches the desktop GUI.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,

    [Parameter(Mandatory = $true)]
    [string]$TargetDir,

    [int]$WaitPid = 0,

    [switch]$RelaunchGui,

    [string]$CleanupDir = ""
)

$ErrorActionPreference = "Stop"
$ServiceName = "WinIRFaceIDService"

$ResolvedTarget = $TargetDir.TrimEnd("\", "/")
$LogDir = Join-Path $ResolvedTarget "data"
$LogPath = Join-Path $LogDir "update.log"

function Write-UpdateLog {
    param([string]$Message)
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$stamp] $Message"
    Write-Host $line
    try {
        if (-not (Test-Path -LiteralPath $LogDir)) {
            New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        }
        [System.IO.File]::AppendAllText($LogPath, $line + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
    }
    catch {
        # Best effort logging if data\ is not yet accessible.
    }
}

function Copy-FileWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$SourceFile,
        [Parameter(Mandatory = $true)][string]$DestFile,
        [int]$Attempts = 4
    )
    $destParent = Split-Path -Path $DestFile -Parent
    if (-not (Test-Path -LiteralPath $destParent)) {
        New-Item -ItemType Directory -Path $destParent -Force | Out-Null
    }
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            Copy-Item -LiteralPath $SourceFile -Destination $DestFile -Force
            return
        }
        catch {
            if ($i -eq $Attempts) { throw }
            Start-Sleep -Milliseconds (300 * $i)
        }
    }
}

function Copy-TreeClean {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Destination)) {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        if ($_.PSIsContainer) {
            if ($_.Name -eq "__pycache__" -or $_.Name -eq ".pytest_cache" -or $_.Name -eq ".git") {
                return
            }
            Copy-TreeClean -Source $_.FullName -Destination (Join-Path $Destination $_.Name)
        }
        else {
            if ($_.Extension -eq ".pyc" -or $_.Extension -eq ".pyo") {
                return
            }
            Copy-FileWithRetry -SourceFile $_.FullName -DestFile (Join-Path $Destination $_.Name)
        }
    }
}

function Get-FileSha256OrEmpty {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

try {
    # 1. Require Administrator privileges.
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Administrator privileges are required to apply updates."
    }

    if (-not (Test-Path -LiteralPath $SourceDir)) {
        throw "Source directory not found: '$SourceDir'."
    }
    if (-not (Test-Path -LiteralPath $ResolvedTarget)) {
        throw "Target directory not found: '$ResolvedTarget'."
    }
    if (Test-Path -LiteralPath (Join-Path $ResolvedTarget ".git")) {
        throw "Refusing in-place update inside a git checkout ('$ResolvedTarget')."
    }

    $ResolvedSource = (Resolve-Path -LiteralPath $SourceDir).Path
    $ResolvedTarget = (Resolve-Path -LiteralPath $ResolvedTarget).Path
    $LogDir = Join-Path $ResolvedTarget "data"
    $LogPath = Join-Path $LogDir "update.log"

    Write-UpdateLog "=== Starting WinIRFaceID delta update ==="
    Write-UpdateLog "SourceDir: $ResolvedSource"
    Write-UpdateLog "TargetDir: $ResolvedTarget"

    # 2. Validate payload structure.
    $reqEngine = Join-Path $ResolvedSource "engine\__init__.py"
    $reqProvider = Join-Path $ResolvedSource "bin\WinIRFaceIDProvider.dll"
    if (-not (Test-Path -LiteralPath $reqEngine)) {
        throw "Invalid update payload: missing 'engine\__init__.py'."
    }
    if (-not (Test-Path -LiteralPath $reqProvider)) {
        throw "Invalid update payload: missing 'bin\WinIRFaceIDProvider.dll'."
    }

    # 3. Wait for GUI process to exit if requested.
    if ($WaitPid -gt 0) {
        Write-UpdateLog "Waiting up to 8s for PID $WaitPid to exit..."
        Wait-Process -Id $WaitPid -Timeout 8 -ErrorAction SilentlyContinue
    }

    # 4. Stop WinIRFaceIDService cleanly and wait up to 12 seconds.
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $svc) {
        if ($svc.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
            Write-UpdateLog "Stopping $ServiceName..."
            & sc.exe stop $ServiceName | Out-Null
            $stopDeadline = (Get-Date).AddSeconds(12)
            while ((Get-Date) -lt $stopDeadline) {
                Start-Sleep -Milliseconds 300
                $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
                if ($null -eq $svc -or $svc.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
                    break
                }
            }
            $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if ($null -ne $svc -and $svc.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
                throw "$ServiceName did not stop within 12 seconds."
            }
            Start-Sleep -Milliseconds 300
        }
        Write-UpdateLog "$ServiceName stopped."
    }
    else {
        Write-UpdateLog "$ServiceName is not installed; skipping stop."
    }

    # 5. Record SHA-256 of existing vs incoming requirements.txt.
    $targetReqPath = Join-Path $ResolvedTarget "requirements.txt"
    $sourceReqPath = Join-Path $ResolvedSource "requirements.txt"
    $oldReqHash = Get-FileSha256OrEmpty -Path $targetReqPath
    $newReqHash = Get-FileSha256OrEmpty -Path $sourceReqPath

    # 6. Copy ONLY code & binary payloads (never touch data\, models\, runtime\, .venv\, or wheels\).
    $payloadDirs = @("bin", "engine", "credential_provider", "scripts", "tools", "docs")
    foreach ($dirName in $payloadDirs) {
        $srcSub = Join-Path $ResolvedSource $dirName
        if (Test-Path -LiteralPath $srcSub) {
            $dstSub = Join-Path $ResolvedTarget $dirName
            Write-UpdateLog "Updating directory '$dirName'..."
            Copy-TreeClean -Source $srcSub -Destination $dstSub
        }
    }

    $targetEngineDir = Join-Path $ResolvedTarget "engine"
    if (Test-Path -LiteralPath $targetEngineDir) {
        Get-ChildItem -LiteralPath $targetEngineDir -Recurse -Force -ErrorAction SilentlyContinue |
            Where-Object {
                ($_.PSIsContainer -and $_.Name -eq "__pycache__") -or
                (-not $_.PSIsContainer -and ($_.Extension -eq ".pyc" -or $_.Extension -eq ".pyo"))
            } |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }

    $rootFiles = @("Install.bat", "Uninstall.bat", "launch_gui.bat", "requirements.txt", "LICENSE", "README.md")
    foreach ($fileName in $rootFiles) {
        $srcFile = Join-Path $ResolvedSource $fileName
        if (Test-Path -LiteralPath $srcFile) {
            Copy-FileWithRetry -SourceFile $srcFile -DestFile (Join-Path $ResolvedTarget $fileName)
        }
    }
    Write-UpdateLog "Payload copy completed."

    # 7. Run pip install only if requirements.txt changed and .venv exists.
    $venvPython = Join-Path $ResolvedTarget ".venv\Scripts\python.exe"
    if (($oldReqHash -ne $newReqHash) -and (Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $targetReqPath)) {
        Write-UpdateLog "requirements.txt changed ($oldReqHash -> $newReqHash); updating Python dependencies..."
        & $venvPython -m pip install --disable-pip-version-check --no-input -r $targetReqPath
        if ($LASTEXITCODE -ne 0) {
            throw "pip install failed with exit code $LASTEXITCODE."
        }
    }
    else {
        Write-UpdateLog "requirements.txt unchanged or .venv missing; skipping pip."
    }

    # 8. Install updated Credential Provider DLL.
    $cpInstallScript = Join-Path $ResolvedTarget "credential_provider\install.ps1"
    $targetDllPath = Join-Path $ResolvedTarget "bin\WinIRFaceIDProvider.dll"
    if ((Test-Path -LiteralPath $cpInstallScript) -and (Test-Path -LiteralPath $targetDllPath)) {
        Write-UpdateLog "Installing updated Credential Provider DLL..."
        & $cpInstallScript -DllPath $targetDllPath
        if ($LASTEXITCODE -ne 0) {
            throw "credential_provider\install.ps1 failed with exit code $LASTEXITCODE."
        }
    }

    # 9. Start WinIRFaceIDService and verify Named Pipe up to 15 seconds.
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $svc) {
        Write-UpdateLog "Starting $ServiceName..."
        & sc.exe start $ServiceName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "sc.exe start failed with exit code $LASTEXITCODE."
        }

        $pipeTestExe = Join-Path $ResolvedTarget "bin\pipe_client_test.exe"
        $pingOk = $false
        $waitDeadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $waitDeadline) {
            Start-Sleep -Milliseconds 400
            $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if ($null -ne $svc -and $svc.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Running) {
                if (Test-Path -LiteralPath $pipeTestExe) {
                    $prevEap = $ErrorActionPreference
                    $ErrorActionPreference = "Continue"
                    try {
                        & $pipeTestExe ping 2>$null | Out-Null
                        $pingRc = $LASTEXITCODE
                    }
                    finally {
                        $ErrorActionPreference = $prevEap
                    }
                    if ($pingRc -eq 0) {
                        $pingOk = $true
                        break
                    }
                }
                else {
                    $pingOk = $true
                    break
                }
            }
        }
        if (-not $pingOk) {
            throw "Named Pipe verification failed after starting '$ServiceName'."
        }
        Write-UpdateLog "$ServiceName started and verified."
    }

    # 10. Relaunch GUI if requested.
    $pythonwExe = Join-Path $ResolvedTarget ".venv\Scripts\pythonw.exe"
    if ($RelaunchGui -and (Test-Path -LiteralPath $pythonwExe)) {
        Write-UpdateLog "Relaunching WinIRFaceID GUI..."
        Start-Process -FilePath $pythonwExe -ArgumentList "-m engine.cli gui" -WorkingDirectory $ResolvedTarget
    }

    # 11. Remove temporary staging directory if inside $env:TEMP.
    if (-not [string]::IsNullOrWhiteSpace($CleanupDir) -and -not [string]::IsNullOrWhiteSpace($env:TEMP)) {
        try {
            $tempRoot = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
            $cleanFull = [System.IO.Path]::GetFullPath($CleanupDir).TrimEnd("\", "/")
            if ($cleanFull.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $cleanFull)) {
                Write-UpdateLog "Cleaning up temporary directory '$cleanFull'..."
                Remove-Item -LiteralPath $cleanFull -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        catch {
            Write-UpdateLog "Temporary directory cleanup skipped: $_"
        }
    }

    Write-UpdateLog "=== WinIRFaceID update completed successfully ==="
    exit 0
}
catch {
    Write-UpdateLog "Update failed: $_"
    exit 1
}
