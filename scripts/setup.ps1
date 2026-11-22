<#
.SYNOPSIS
    One-click plug-and-play setup for WinIRFaceID (run as Administrator).
.DESCRIPTION
    1. Verifies Administrator privileges.
    2. Installs a private CPython 3.11 x64 into runtime\python (bundled with the release, otherwise the
       PSF-signed python.org NuGet package). A per-user Python is never used: the SYSTEM service runs the
       stdlib of the venv's base interpreter, so it must not be writable by non-administrators.
    3. (Re)creates .venv on that runtime and installs requirements.txt (offline from wheels\ when bundled).
    0. Unless -InPlace is set, copies the application into %ProgramFiles%\WinIRFaceID and re-runs from there,
       because the LocalSystem service executes code from the install directory (a user-writable install
       directory would let any local user run code as SYSTEM).
    4. Ensures data\ (SYSTEM + Administrators only) and models\ (read-only for Users) exist with locked-down ACLs.
    5. Downloads YuNet and SFace ONNX models if missing.
    6. Generates engine\ui\app.ico if missing.
    7. Locates prebuilt C++ binaries in bin\ or credential_provider\build\Release\ (or builds with cmake if on PATH).
    8. Unless -SkipLockScreen is set, installs WinIRFaceIDService and registers WinIRFaceIDProvider.dll.
    9. Creates the machine-wide Start Menu shortcut via scripts\create_shortcut.ps1 -AllUsers.
   10. Unless -NoLaunch is set, launches the WinIRFaceID desktop app.
.PARAMETER NoLaunch
    Skip launching the desktop GUI after setup finishes (used by Inno Setup).
.PARAMETER SkipLockScreen
    Install Python runtime, models, and desktop app only without installing the Windows service or Credential Provider.
.PARAMETER UserAccount
    Deprecated and ignored: profiles are managed through the service, so no user gets write access to data\.
.PARAMETER InPlace
    Install from the current directory instead of %ProgramFiles%\WinIRFaceID (developers only; insecure if the
    directory is writable by non-administrators).
.PARAMETER LogPath
    Also write a full transcript of the setup run to this file (used by the installer).
#>
[CmdletBinding()]
param(
    [switch]$NoLaunch,
    [switch]$SkipLockScreen,
    [string]$UserAccount = "",
    [switch]$InPlace,
    [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"

$script:Transcribing = $false
if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
    try {
        Start-Transcript -LiteralPath $LogPath -Force | Out-Null
        $script:Transcribing = $true
    }
    catch {
        Write-Warning "Could not start setup log at '$LogPath': $_"
    }
}

trap {
    Write-Host ""
    Write-Host "WinIRFaceID setup FAILED: $_"
    if ($_.InvocationInfo) { Write-Host $_.InvocationInfo.PositionMessage }
    if ($script:Transcribing) { Stop-Transcript | Out-Null }
    exit 1
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Administrator privileges are required. Right-click Install.bat or run from an elevated PowerShell prompt."
    exit 1
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

# 0. Relocate into Program Files so the SYSTEM service never runs user-writable code.
$SecureRoot = Join-Path $env:ProgramFiles "WinIRFaceID"
if (-not $InPlace -and ($RepoRoot.TrimEnd("\") -ne $SecureRoot.TrimEnd("\"))) {
    Write-Host "Copying WinIRFaceID into '$SecureRoot'..."
    $svc = Get-Service -Name "WinIRFaceIDService" -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne "Stopped") {
        Stop-Service -Name "WinIRFaceIDService" -Force -ErrorAction SilentlyContinue
        $svc.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(15))
    }
    New-Item -ItemType Directory -Path $SecureRoot -Force | Out-Null
    & robocopy.exe $RepoRoot $SecureRoot /E /NFL /NDL /NJH /NJS /NP `
        /XD .git .venv data build dist __pycache__ .pytest_cache `
        /XF *.pyc | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy into '$SecureRoot' failed with exit code $LASTEXITCODE."
    }
    # build\ is excluded above, so carry locally built C++ binaries over into bin\.
    $builtRelease = Join-Path $RepoRoot "credential_provider\build\Release"
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "bin")) -and (Test-Path -LiteralPath $builtRelease)) {
        $secureBin = Join-Path $SecureRoot "bin"
        New-Item -ItemType Directory -Path $secureBin -Force | Out-Null
        Copy-Item -Path (Join-Path $builtRelease "*") -Include *.exe, *.dll -Destination $secureBin -Force
    }
    # Program Files inheritance gives Users read/execute only; drop any explicit ACEs copied along.
    & icacls.exe $SecureRoot /reset /T /C /Q | Out-Null
    $forward = @{ InPlace = $true }
    if ($NoLaunch) { $forward["NoLaunch"] = $true }
    if ($SkipLockScreen) { $forward["SkipLockScreen"] = $true }
    if ($script:Transcribing) { Stop-Transcript | Out-Null; $script:Transcribing = $false; $forward["LogPath"] = $LogPath }
    & (Join-Path $SecureRoot "scripts\setup.ps1") @forward
    exit $LASTEXITCODE
}
if (-not $RepoRoot.StartsWith($env:ProgramFiles, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Warning "Installing in place from '$RepoRoot'. If non-administrators can write here, they can run code as SYSTEM via the service."
}
$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
$RequirementsPath = Join-Path $RepoRoot "requirements.txt"

$RuntimeDir = Join-Path $RepoRoot "runtime\python"
$RuntimePython = Join-Path $RuntimeDir "python.exe"
$WheelsDir = Join-Path $RepoRoot "wheels"
. (Join-Path $PSScriptRoot "python_runtime.ps1")

function Invoke-Native {
    # Run a native command, streaming stdout+stderr through Write-Host (captured by the transcript)
    # without PowerShell turning stderr lines into terminating errors.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $FilePath @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" }
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

function Stop-WinIRFaceIDProcesses {
    param([string[]]$Roots)
    $svc = Get-Service -Name "WinIRFaceIDService" -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne "Stopped") {
        Write-Host "Stopping WinIRFaceIDService..."
        Stop-Service -Name "WinIRFaceIDService" -Force -ErrorAction SilentlyContinue
        try { $svc.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(15)) } catch { }
    }
    $roots = $Roots | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { $_.TrimEnd("\") + "\" }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $exe = $_.ExecutablePath
        $exe -and ($roots | Where-Object { $exe.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }) -and
            ($_.CommandLine -match "engine[\.]cli")
    } | ForEach-Object {
        Write-Host "Closing running WinIRFaceID process $($_.ProcessId) ($($_.ExecutablePath))..."
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "=== WinIRFaceID Setup ==="
Write-Host "Install directory: $RepoRoot"

# 1. Private Python runtime (never a per-user Python: the SYSTEM service would run its stdlib)
if (Test-PrivatePython -RuntimeDir $RuntimeDir) {
    Write-Host "Using bundled Python runtime: $RuntimePython"
}
else {
    Write-Host "[1/6] Installing private Python runtime into '$RuntimeDir'..."
    $bundledPkg = Get-ChildItem -LiteralPath (Join-Path $RepoRoot "runtime") -Filter "python.*.nupkg" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($bundledPkg) {
        Install-PrivatePython -RuntimeDir $RuntimeDir -PackagePath $bundledPkg.FullName
    }
    else {
        Install-PrivatePython -RuntimeDir $RuntimeDir
    }
}

# 2. (Re)create .venv on top of the private runtime and install dependencies
$venvHome = Get-VenvHome -VenvDir $VenvDir
$venvOk = (Test-Path -LiteralPath $VenvPython) -and $venvHome -and
          ($venvHome.TrimEnd("\") -ieq $RuntimeDir.TrimEnd("\"))
if (-not $venvOk) {
    if (Test-Path -LiteralPath $VenvDir) {
        Write-Host "Existing .venv is based on '$venvHome', not the private runtime; recreating it..."
        Stop-WinIRFaceIDProcesses -Roots @($VenvDir, $venvHome)
        Remove-Item -LiteralPath $VenvDir -Recurse -Force
    }
    Write-Host "[2/6] Creating virtual environment at '$VenvDir'..."
    $rc = Invoke-Native -FilePath $RuntimePython -Arguments @("-I", "-m", "venv", $VenvDir)
    if ($rc -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        throw "Failed to create Python virtual environment at '$VenvDir' (exit code $rc)."
    }
}

$pipCommon = @("-m", "pip", "install", "--disable-pip-version-check", "--no-input")
if (Test-Path -LiteralPath (Join-Path $WheelsDir "*.whl")) {
    Write-Host "[3/6] Installing Python dependencies from bundled wheels (offline)..."
    $rc = Invoke-Native -FilePath $VenvPython -Arguments ($pipCommon + @("--no-index", "--find-links", $WheelsDir, "-r", $RequirementsPath))
}
else {
    Write-Host "[3/6] Downloading and installing Python dependencies..."
    $rc = Invoke-Native -FilePath $VenvPython -Arguments ($pipCommon + @("--upgrade", "pip"))
    if ($rc -ne 0) {
        throw "Failed to upgrade pip in '$VenvDir' (exit code $rc)."
    }
    $rc = Invoke-Native -FilePath $VenvPython -Arguments ($pipCommon + @("-r", $RequirementsPath))
}
if ($rc -ne 0) {
    throw "Failed to install dependencies from '$RequirementsPath' (exit code $rc)."
}

# 3. Ensure data\ and models\ exist with locked-down NTFS ACLs
$DataDir = Join-Path $RepoRoot "data"
$ModelsDir = Join-Path $RepoRoot "models"
foreach ($dir in @($DataDir, $ModelsDir)) {
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

if (-not [string]::IsNullOrWhiteSpace($UserAccount)) {
    Write-Warning "-UserAccount is ignored: profiles are managed through the WinIRFaceID service."
}

function Set-LockedDownAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string[]]$ExtraGrants = @()
    )
    # Drop inherited ACEs and replace explicit ones: SYSTEM + Administrators full control.
    $grantArgs = @($Path, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")
    foreach ($g in $ExtraGrants) { $grantArgs += $g }
    & icacls.exe @grantArgs | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed ($LASTEXITCODE) while securing '$Path'."
    }
    # Existing files (older versions granted the user Modify) re-inherit the new ACL.
    if (Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue) {
        & icacls.exe (Join-Path $Path "*") /reset /T /C /Q | Out-Null
    }
}

Write-Host "Securing data\ (SYSTEM/Administrators only) and models\ (read-only for Users)..."
Set-LockedDownAcl -Path $DataDir
Set-LockedDownAcl -Path $ModelsDir -ExtraGrants @("*S-1-5-32-545:(OI)(CI)RX")

# 4. Download ONNX models if missing
$YunetPath = Join-Path $ModelsDir "face_detection_yunet_2023mar.onnx"
$SfacePath = Join-Path $ModelsDir "face_recognition_sface_2021dec.onnx"
$modelsReady = (Test-Path -LiteralPath $YunetPath) -and ((Get-Item -LiteralPath $YunetPath).Length -gt 1024) -and
               (Test-Path -LiteralPath $SfacePath) -and ((Get-Item -LiteralPath $SfacePath).Length -gt 1024)

if (-not $modelsReady) {
    Write-Host "[4/6] Downloading YuNet and SFace ONNX models..."
    Push-Location $RepoRoot
    try {
        $rc = Invoke-Native -FilePath $VenvPython -Arguments @("-m", "engine.cli", "download-models")
        if ($rc -ne 0) {
            throw "Model download failed with exit code $rc."
        }
    }
    finally {
        Pop-Location
    }
}
else {
    Write-Host "ONNX models already present in '$ModelsDir'."
}

# 5. Generate application icon if missing
$IconPath = Join-Path $RepoRoot "engine\ui\app.ico"
if (-not (Test-Path -LiteralPath $IconPath)) {
    Write-Host "Generating application icon at '$IconPath'..."
    Push-Location $RepoRoot
    try {
        $rc = Invoke-Native -FilePath $VenvPython -Arguments @((Join-Path $RepoRoot "engine\ui\generate_app_icon.py"))
        if ($rc -ne 0) {
            throw "generate_app_icon.py failed with exit code $rc."
        }
    }
    finally {
        Pop-Location
    }
}

# 6. Locate or build C++ binaries, then install service & Credential Provider
function Find-CppBinDir {
    $required = @("WinIRFaceIDProvider.dll", "WinIRFaceIDService.exe", "pipe_client_test.exe")
    $candidates = @(
        (Join-Path $RepoRoot "bin"),
        (Join-Path $RepoRoot "credential_provider\build\Release")
    )
    foreach ($dir in $candidates) {
        $allPresent = $true
        foreach ($file in $required) {
            if (-not (Test-Path -LiteralPath (Join-Path $dir $file))) {
                $allPresent = $false
                break
            }
        }
        if ($allPresent) {
            return (Resolve-Path -LiteralPath $dir).Path
        }
    }
    return $null
}

if (-not $SkipLockScreen) {
    $BinDir = Find-CppBinDir
    if (-not $BinDir) {
        $cmakeCmd = Get-Command "cmake" -ErrorAction SilentlyContinue
        $cpSourceDir = Join-Path $RepoRoot "credential_provider"
        if ($cmakeCmd -and (Test-Path -LiteralPath (Join-Path $cpSourceDir "CMakeLists.txt"))) {
            Write-Host "Prebuilt C++ binaries not found; building credential_provider (x64 Release) with CMake..."
            $cpBuildDir = Join-Path $cpSourceDir "build"
            & $cmakeCmd.Source -S $cpSourceDir -B $cpBuildDir -A x64
            if ($LASTEXITCODE -eq 0) {
                & $cmakeCmd.Source --build $cpBuildDir --config Release
            }
            $BinDir = Find-CppBinDir
        }
    }

    if (-not $BinDir) {
        throw "Required C++ binaries (WinIRFaceIDProvider.dll, WinIRFaceIDService.exe, pipe_client_test.exe) were not found in 'bin\' or 'credential_provider\build\Release\'. Download the prebuilt release from GitHub Releases or install CMake + Visual Studio Build Tools (Desktop development with C++) and re-run setup."
    }

    Write-Host "Using C++ binaries from: $BinDir"

    Write-Host "[5/6] Installing and verifying WinIRFaceIDService..."
    & (Join-Path $RepoRoot "scripts\install_service.ps1") -BinDir $BinDir
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "install_service.ps1 failed with exit code $LASTEXITCODE."
    }

    Write-Host "Registering WinIRFaceIDProvider Credential Provider..."
    & (Join-Path $RepoRoot "credential_provider\install.ps1") -DllPath (Join-Path $BinDir "WinIRFaceIDProvider.dll")
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "credential_provider\install.ps1 failed with exit code $LASTEXITCODE."
    }
}
else {
    Write-Host "Skipping Windows service and Credential Provider registration (-SkipLockScreen)."
}

# 7. Create Start Menu shortcut (AllUsers when elevated)
Write-Host "[6/6] Creating Start Menu shortcut..."
& (Join-Path $RepoRoot "scripts\create_shortcut.ps1") -AllUsers
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    Write-Warning "create_shortcut.ps1 returned exit code $LASTEXITCODE."
}

# 8. Launch desktop GUI unless -NoLaunch
if (-not $NoLaunch) {
    if (Test-Path -LiteralPath $VenvPythonw) {
        Write-Host "Launching WinIRFaceID..."
        Start-Process -FilePath $VenvPythonw -ArgumentList "-m", "engine.cli", "gui" -WorkingDirectory $RepoRoot
    }
    else {
        Write-Warning "pythonw.exe not found at '$VenvPythonw'; skipping GUI launch."
    }
}

Write-Host ""
Write-Host "WinIRFaceID setup completed successfully."
if ($script:Transcribing) { Stop-Transcript | Out-Null }
exit 0
