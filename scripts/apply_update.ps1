<#
.SYNOPSIS
    Applies a verified WinIRFaceID delta update package in-place with Administrator privileges.
.DESCRIPTION
    Stages the verified update ZIP into an Administrator-only protected directory inside TargetDir,
    re-verifies its SHA-256 checksum, safely extracts the archive (blocking path traversal and
    symlinks), stops WinIRFaceIDService, backs up existing payload files, swaps in the updated
    code and native binaries (without touching data\, models\, runtime\, .venv\, or wheels\),
    refuses updates that change Python dependencies (those need the full installer), registers the updated Credential
    Provider DLL, restarts and verifies the service, and rolls back automatically on failure.
#>
# Permanent backward-compatible CLI contract (v0.3.0+): -ZipPath, -ExpectedSha256,
# -TargetDir, -WaitPid, and -CleanupDir must remain supported because the installed
# script executes future updates. Any new parameters added later must be optional.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ZipPath,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedSha256,

    [Parameter(Mandatory = $true)]
    [string]$TargetDir,

    [int]$WaitPid = 0,

    [string]$CleanupDir = ""
)

$ErrorActionPreference = "Stop"
$ServiceName = "WinIRFaceIDService"
$PayloadDirs = @("bin", "engine", "credential_provider", "scripts", "tools", "docs")
$RootFiles = @("Install.bat", "Uninstall.bat", "launch_gui.bat", "requirements.txt", "LICENSE", "README.md")

$ResolvedTarget = $TargetDir.TrimEnd("\", "/")
$LogDir = Join-Path $ResolvedTarget "data"
$LogPath = Join-Path $LogDir "update.log"
$StagingRoot = $null
$BackupDir = $null
$ServiceWasRunning = $false
$PayloadTouched = $false
$ExitCode = 0

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

function Set-AdminOnlyDirectoryAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inheritFlags = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    $propFlags = [System.Security.AccessControl.PropagationFlags]::None
    $accessType = [System.Security.AccessControl.AccessControlType]::Allow

    $systemSid = New-Object System.Security.Principal.SecurityIdentifier([System.Security.Principal.WellKnownSidType]::LocalSystemSid, $null)
    $adminsSid = New-Object System.Security.Principal.SecurityIdentifier([System.Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid, $null)

    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $systemSid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inheritFlags,
        $propFlags,
        $accessType
    )))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $adminsSid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inheritFlags,
        $propFlags,
        $accessType
    )))

    [System.IO.Directory]::SetAccessControl($Path, $acl)
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

function Remove-TreeWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Attempts = 5
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force
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

function Replace-DirectoryClean {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    Remove-TreeWithRetry -Path $Destination
    Copy-TreeClean -Source $Source -Destination $Destination
}

function Get-RequirementsFingerprint {
    # Content-only view of requirements.txt: ignores line endings, blank lines,
    # comments, case and order, so a CRLF/LF-only difference never counts as a change.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    $lines = Get-Content -LiteralPath $Path |
        ForEach-Object { ($_ -replace '#.*$', '').Trim().ToLowerInvariant() } |
        Where-Object { $_ -ne "" } |
        Sort-Object -Unique
    return ($lines -join "`n")
}

function Expand-VerifiedZipSafe {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$DestinationDir
    )
    New-Item -ItemType Directory -Path $DestinationDir -Force | Out-Null
    $extractFull = [System.IO.Path]::GetFullPath($DestinationDir).TrimEnd("\", "/")
    $extractRootPrefix = $extractFull + [System.IO.Path]::DirectorySeparatorChar

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $planned = [System.Collections.Generic.List[object]]::new()
        foreach ($entry in $zip.Entries) {
            $rawName = $entry.FullName
            if ([string]::IsNullOrEmpty($rawName) -or ($rawName.IndexOf([char]0) -ge 0)) {
                throw "Unsafe zip entry name: '$rawName'."
            }

            if ((($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) {
                throw "Refusing symlink inside zip archive: '$rawName'."
            }

            $normalized = $rawName.Replace("\", "/")
            if ($normalized.StartsWith("/") -or ($normalized -match '^[A-Za-z]:')) {
                throw "Refusing absolute path inside zip archive: '$rawName'."
            }

            $segments = $normalized.Split("/")
            foreach ($seg in $segments) {
                if ($seg -eq ".." -or $seg.Contains(":")) {
                    throw "Refusing path traversal inside zip archive: '$rawName'."
                }
            }

            $relParts = @($segments | Where-Object { $_ -ne "" -and $_ -ne "." })
            $isDir = $rawName.EndsWith("/") -or $rawName.EndsWith("\")
            if ($isDir -and $entry.Length -gt 0) {
                throw "Invalid directory entry with data inside zip archive: '$rawName'."
            }

            if ($relParts.Count -eq 0) {
                if (-not $isDir) {
                    throw "Invalid zip file entry path: '$rawName'."
                }
                continue
            }

            $destPath = $extractFull
            foreach ($part in $relParts) {
                $destPath = Join-Path $destPath $part
            }
            $fullDest = [System.IO.Path]::GetFullPath($destPath)

            if ($isDir) {
                $dirWithSep = $fullDest.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
                if (-not $dirWithSep.StartsWith($extractRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "Zip directory entry escapes staging directory: '$rawName'."
                }
            }
            else {
                if (-not $fullDest.StartsWith($extractRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "Zip entry escapes staging directory: '$rawName'."
                }
            }

            $planned.Add([PSCustomObject]@{
                Entry    = $entry
                IsDir    = $isDir
                DestPath = $fullDest
            })
        }

        foreach ($item in $planned) {
            if ($item.IsDir) {
                if (-not (Test-Path -LiteralPath $item.DestPath)) {
                    New-Item -ItemType Directory -Path $item.DestPath -Force | Out-Null
                }
                continue
            }

            $parentDir = Split-Path -Path $item.DestPath -Parent
            if (-not (Test-Path -LiteralPath $parentDir)) {
                New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
            }

            $srcStream = $item.Entry.Open()
            try {
                $dstStream = [System.IO.File]::Open($item.DestPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                try {
                    $srcStream.CopyTo($dstStream)
                }
                finally {
                    $dstStream.Dispose()
                }
            }
            finally {
                $srcStream.Dispose()
            }
        }
    }
    finally {
        $zip.Dispose()
    }
}

try {
    # 1. Require Administrator privileges.
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Administrator privileges are required to apply updates."
    }

    # 2. Validate inputs before staging.
    if ([string]::IsNullOrWhiteSpace($ResolvedTarget) -or -not (Test-Path -LiteralPath $ResolvedTarget -PathType Container)) {
        throw "Target directory not found: '$TargetDir'."
    }
    $ResolvedTarget = (Resolve-Path -LiteralPath $ResolvedTarget).Path
    $LogDir = Join-Path $ResolvedTarget "data"
    $LogPath = Join-Path $LogDir "update.log"

    if (Test-Path -LiteralPath (Join-Path $ResolvedTarget ".git")) {
        throw "Refusing in-place update inside a git checkout ('$ResolvedTarget')."
    }
    if ([string]::IsNullOrWhiteSpace($ZipPath) -or -not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
        throw "Verified update archive not found: '$ZipPath'."
    }
    if ($ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw "ExpectedSha256 must be a valid 64-character hexadecimal SHA-256 digest."
    }

    $ResolvedZip = (Resolve-Path -LiteralPath $ZipPath).Path
    $ExpectedLower = $ExpectedSha256.ToLowerInvariant()

    Write-UpdateLog "=== Starting WinIRFaceID delta update ==="
    Write-UpdateLog "ZipPath: $ResolvedZip"
    Write-UpdateLog "TargetDir: $ResolvedTarget"

    # 3. Create admin-only protected staging root inside TargetDir.
    $StagingRoot = Join-Path $ResolvedTarget (".update-staging-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $StagingRoot -Force | Out-Null
    Set-AdminOnlyDirectoryAcl -Path $StagingRoot

    # 4. Copy ZIP into protected staging and re-verify SHA-256.
    $StagedZip = Join-Path $StagingRoot "update.zip"
    Copy-Item -LiteralPath $ResolvedZip -Destination $StagedZip -Force
    $actualSha256 = (Get-FileHash -LiteralPath $StagedZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -ne $ExpectedLower) {
        throw "SHA-256 mismatch on staged update archive (expected $ExpectedLower, got $actualSha256)."
    }
    Write-UpdateLog "Staged archive SHA-256 verified ($actualSha256)."

    # 5. Safely extract staged ZIP inside protected staging directory.
    $ExtractDir = Join-Path $StagingRoot "extracted"
    Expand-VerifiedZipSafe -ArchivePath $StagedZip -DestinationDir $ExtractDir

    $reqEngine = Join-Path $ExtractDir "engine\__init__.py"
    $reqProvider = Join-Path $ExtractDir "bin\WinIRFaceIDProvider.dll"
    if (-not (Test-Path -LiteralPath $reqEngine -PathType Leaf)) {
        throw "Invalid update payload: missing 'engine\__init__.py'."
    }
    if (-not (Test-Path -LiteralPath $reqProvider -PathType Leaf)) {
        throw "Invalid update payload: missing 'bin\WinIRFaceIDProvider.dll'."
    }

    # Delta updates never install Python packages: pip would pull unpinned code from
    # PyPI into the environment the SYSTEM service runs from. A release that changes
    # dependencies must be installed with the full offline installer instead.
    $oldReqs = Get-RequirementsFingerprint -Path (Join-Path $ResolvedTarget "requirements.txt")
    $newReqs = Get-RequirementsFingerprint -Path (Join-Path $ExtractDir "requirements.txt")
    if ($oldReqs -ne "" -and $newReqs -ne "" -and $oldReqs -ne $newReqs) {
        throw "This update changes Python dependencies and cannot be applied as a delta update. Install it with the full WinIRFaceID-Setup installer from GitHub Releases."
    }

    # 6. Wait for GUI process to exit if requested.
    if ($WaitPid -gt 0) {
        Write-UpdateLog "Waiting up to 8s for PID $WaitPid to exit..."
        Wait-Process -Id $WaitPid -Timeout 8 -ErrorAction SilentlyContinue
    }

    # 7. Capture service state BEFORE stopping, then stop WinIRFaceIDService cleanly.
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $svc) {
        $ServiceWasRunning = (
            $svc.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Running -or
            $svc.Status -eq [System.ServiceProcess.ServiceControllerStatus]::StartPending
        )
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

    # 8. Back up existing payload directories and root files before modifying TargetDir.
    $BackupDir = Join-Path $StagingRoot "backup"
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    foreach ($dirName in $PayloadDirs) {
        $existingSub = Join-Path $ResolvedTarget $dirName
        if (Test-Path -LiteralPath $existingSub) {
            Copy-TreeClean -Source $existingSub -Destination (Join-Path $BackupDir $dirName)
        }
    }
    foreach ($fileName in $RootFiles) {
        $existingFile = Join-Path $ResolvedTarget $fileName
        if (Test-Path -LiteralPath $existingFile -PathType Leaf) {
            Copy-FileWithRetry -SourceFile $existingFile -DestFile (Join-Path $BackupDir $fileName)
        }
    }
    Write-UpdateLog "Backed up existing payload to protected staging."

    # 9. Cleanly replace payload directories (removing stale files; never touch data\, models\, runtime\, .venv\, or wheels\).
    $PayloadTouched = $true
    foreach ($dirName in $PayloadDirs) {
        $srcSub = Join-Path $ExtractDir $dirName
        if (Test-Path -LiteralPath $srcSub) {
            $dstSub = Join-Path $ResolvedTarget $dirName
            Write-UpdateLog "Replacing directory '$dirName'..."
            Replace-DirectoryClean -Source $srcSub -Destination $dstSub
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

    foreach ($fileName in $RootFiles) {
        $srcFile = Join-Path $ExtractDir $fileName
        if (Test-Path -LiteralPath $srcFile -PathType Leaf) {
            Copy-FileWithRetry -SourceFile $srcFile -DestFile (Join-Path $ResolvedTarget $fileName)
        }
    }
    Write-UpdateLog "Payload swap completed."

    # 10. Install updated Credential Provider DLL.
    $cpInstallScript = Join-Path $ResolvedTarget "credential_provider\install.ps1"
    $targetDllPath = Join-Path $ResolvedTarget "bin\WinIRFaceIDProvider.dll"
    if ((Test-Path -LiteralPath $cpInstallScript) -and (Test-Path -LiteralPath $targetDllPath)) {
        Write-UpdateLog "Installing updated Credential Provider DLL..."
        & $cpInstallScript -DllPath $targetDllPath
        if ($LASTEXITCODE -ne 0) {
            throw "credential_provider\install.ps1 failed with exit code $LASTEXITCODE."
        }
    }

    # 11. Start WinIRFaceIDService and verify Named Pipe up to 15 seconds.
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

    Write-UpdateLog "=== WinIRFaceID update completed successfully ==="
    $ExitCode = 0
}
catch {
    Write-UpdateLog "Update failed: $_"
    $ExitCode = 1

    if ($PayloadTouched -and -not [string]::IsNullOrWhiteSpace($BackupDir) -and (Test-Path -LiteralPath $BackupDir)) {
        Write-UpdateLog "Rolling back payload from backup..."
        try {
            $svcRollback = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if ($null -ne $svcRollback -and $svcRollback.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
                Write-UpdateLog "Stopping $ServiceName before rollback..."
                & sc.exe stop $ServiceName | Out-Null
                $rbStopDeadline = (Get-Date).AddSeconds(8)
                while ((Get-Date) -lt $rbStopDeadline) {
                    Start-Sleep -Milliseconds 300
                    $svcRollback = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
                    if ($null -eq $svcRollback -or $svcRollback.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
                        break
                    }
                }
                Start-Sleep -Milliseconds 300
            }
        }
        catch {
            Write-UpdateLog "Rollback warning stopping ${ServiceName}: $_"
        }

        foreach ($dirName in $PayloadDirs) {
            $bakSub = Join-Path $BackupDir $dirName
            $dstSub = Join-Path $ResolvedTarget $dirName
            try {
                if (Test-Path -LiteralPath $bakSub) {
                    Replace-DirectoryClean -Source $bakSub -Destination $dstSub
                }
            }
            catch {
                Write-UpdateLog "Rollback warning for directory '$dirName': $_"
            }
        }
        foreach ($fileName in $RootFiles) {
            $bakFile = Join-Path $BackupDir $fileName
            if (Test-Path -LiteralPath $bakFile -PathType Leaf) {
                try {
                    Copy-FileWithRetry -SourceFile $bakFile -DestFile (Join-Path $ResolvedTarget $fileName)
                }
                catch {
                    Write-UpdateLog "Rollback warning for file '$fileName': $_"
                }
            }
        }

        $restoredCpScript = Join-Path $ResolvedTarget "credential_provider\install.ps1"
        $restoredDllPath = Join-Path $ResolvedTarget "bin\WinIRFaceIDProvider.dll"
        if ((Test-Path -LiteralPath $restoredCpScript) -and (Test-Path -LiteralPath $restoredDllPath)) {
            try {
                Write-UpdateLog "Re-registering restored Credential Provider DLL..."
                & $restoredCpScript -DllPath $restoredDllPath
                if ($LASTEXITCODE -ne 0) {
                    Write-UpdateLog "Rollback warning: credential_provider\install.ps1 exited with $LASTEXITCODE."
                }
            }
            catch {
                Write-UpdateLog "Rollback warning re-registering Credential Provider DLL: $_"
            }
        }
        Write-UpdateLog "Payload rollback completed."
    }

    if ($ServiceWasRunning) {
        try {
            Write-UpdateLog "Attempting to recover $ServiceName..."
            & sc.exe start $ServiceName | Out-Null
            $recoverDeadline = (Get-Date).AddSeconds(8)
            $recovered = $false
            while ((Get-Date) -lt $recoverDeadline) {
                Start-Sleep -Milliseconds 300
                $svcAfter = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
                if ($null -ne $svcAfter -and $svcAfter.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Running) {
                    $recovered = $true
                    break
                }
            }
            if ($recovered) {
                Write-UpdateLog "$ServiceName recovered and running."
            }
            else {
                $statusNow = if ($null -ne $svcAfter) { $svcAfter.Status } else { "missing" }
                Write-UpdateLog "Failed to recover $ServiceName (current status: $statusNow)."
            }
        }
        catch {
            Write-UpdateLog "Failed to restart $ServiceName during recovery: $_"
        }
    }
}
finally {
    if (-not [string]::IsNullOrWhiteSpace($StagingRoot) -and (Test-Path -LiteralPath $StagingRoot)) {
        try {
            Remove-Item -LiteralPath $StagingRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
        catch {
            Write-UpdateLog "Staging cleanup skipped: $_"
        }
    }

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
}

exit $ExitCode
