<#
.SYNOPSIS
    One-click plug-and-play setup for WinIRFaceID (run as Administrator).
.DESCRIPTION
    1. Verifies Administrator privileges.
    2. Locates Python 3.11+ x64 (or installs Python 3.11 via winget if missing).
    3. Creates .venv, upgrades pip, and installs requirements.txt.
    4. Ensures data\ and models\ exist with Modify ACLs for SYSTEM, Administrators, and the interactive user.
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
    Optional DOMAIN\User (or local username) granted Modify access on data\ and models\.
#>
[CmdletBinding()]
param(
    [switch]$NoLaunch,
    [switch]$SkipLockScreen,
    [string]$UserAccount = ""
)

$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Administrator privileges are required. Right-click Install.bat or run from an elevated PowerShell prompt."
    exit 1
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
$RequirementsPath = Join-Path $RepoRoot "requirements.txt"

function Test-PythonX64 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [string[]]$PrefixArgs = @()
    )

    if (-not (Get-Command $Executable -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $Executable)) {
        return $null
    }

    try {
        $probe = "import sys, struct; ok = sys.version_info >= (3, 11) and struct.calcsize('P') * 8 == 64; print(sys.executable if ok else ''); sys.exit(0 if ok else 1)"
        $out = & $Executable @PrefixArgs -c $probe 2>$null
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($out)) {
            $resolved = ($out | Select-Object -First 1).Trim()
            if (Test-Path -LiteralPath $resolved) {
                return $resolved
            }
        }
    }
    catch {
        return $null
    }

    return $null
}

function Find-Python311X64 {
    param([switch]$IncludeVenv)

    if ($IncludeVenv -and (Test-Path -LiteralPath $VenvPython)) {
        $found = Test-PythonX64 -Executable $VenvPython
        if ($found) { return $found }
    }

    if (Get-Command "py" -ErrorAction SilentlyContinue) {
        foreach ($ver in @("-3.11", "-3.12", "-3.13", "-3")) {
            $found = Test-PythonX64 -Executable "py" -PrefixArgs @($ver)
            if ($found) { return $found }
        }
    }

    foreach ($cmdName in @("python.exe", "python3.exe")) {
        $cmds = Get-Command $cmdName -All -ErrorAction SilentlyContinue
        foreach ($cmd in $cmds) {
            if ($cmd.Source -and ($cmd.Source -notmatch "\\WindowsApps\\")) {
                $found = Test-PythonX64 -Executable $cmd.Source
                if ($found) { return $found }
            }
        }
    }

    $standardCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        "C:\Program Files\Python311\python.exe",
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python313\python.exe"
    )
    foreach ($candidate in $standardCandidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate)) {
            $found = Test-PythonX64 -Executable $candidate
            if ($found) { return $found }
        }
    }

    return $null
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $combined = @($machinePath, $userPath) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $env:Path = ($combined -join ";")
}

Write-Host "=== WinIRFaceID Setup ==="
Write-Host "Install directory: $RepoRoot"

# 1. Locate or install Python 3.11+ x64
$PythonExe = Find-Python311X64 -IncludeVenv
if (-not $PythonExe) {
    Write-Host "Python 3.11+ (x64) not found. Attempting automatic installation via winget..."
    $wingetCmd = Get-Command "winget" -ErrorAction SilentlyContinue
    if ($wingetCmd) {
        & $wingetCmd.Source install --id Python.Python.3.11 --exact --silent --accept-package-agreements --accept-source-agreements
        Refresh-ProcessPath
        $PythonExe = Find-Python311X64
    }
}

if (-not $PythonExe) {
    throw "Python 3.11 or newer (64-bit) is required and could not be installed automatically. Install Python 3.11 x64 from https://www.python.org/downloads/ (enable 'Add python.exe to PATH') and re-run setup."
}

Write-Host "Using Python: $PythonExe"

# 2. Create .venv if missing and install dependencies
if (-not (Test-PythonX64 -Executable $VenvPython)) {
    Write-Host "Creating virtual environment at '$VenvDir'..."
    & $PythonExe -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        throw "Failed to create Python virtual environment at '$VenvDir'."
    }
}

Write-Host "Upgrading pip and installing Python dependencies..."
& $VenvPython -m pip install --upgrade pip --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip in '$VenvDir'."
}

& $VenvPython -m pip install -r $RequirementsPath --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install dependencies from '$RequirementsPath'."
}

# 3. Ensure data\ and models\ exist and grant NTFS Modify ACLs for non-elevated desktop usage
$DataDir = Join-Path $RepoRoot "data"
$ModelsDir = Join-Path $RepoRoot "models"
foreach ($dir in @($DataDir, $ModelsDir)) {
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

$targetUser = $UserAccount.Trim()
if ([string]::IsNullOrWhiteSpace($targetUser)) {
    if (-not [string]::IsNullOrWhiteSpace($env:USERNAME) -and
        $env:USERNAME -ne "SYSTEM" -and
        -not $env:USERNAME.EndsWith("$")) {
        if (-not [string]::IsNullOrWhiteSpace($env:USERDOMAIN)) {
            $targetUser = "$($env:USERDOMAIN)\$($env:USERNAME)"
        }
        else {
            $targetUser = $env:USERNAME
        }
    }
}
elseif ($targetUser -eq "SYSTEM" -or $targetUser -eq "NT AUTHORITY\SYSTEM" -or $targetUser.EndsWith("$")) {
    $targetUser = ""
}

Write-Host "Configuring directory permissions for data\ and models\..."
foreach ($dir in @($DataDir, $ModelsDir)) {
    $icaclsArgs = @($dir, "/grant", "*S-1-5-18:(OI)(CI)M", "*S-1-5-32-544:(OI)(CI)M")
    if (-not [string]::IsNullOrWhiteSpace($targetUser)) {
        $icaclsArgs += "${targetUser}:(OI)(CI)M"
    }
    & icacls.exe @icaclsArgs | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "icacls returned exit code $LASTEXITCODE for '$dir' (continuing)."
    }
}

# 4. Download ONNX models if missing
$YunetPath = Join-Path $ModelsDir "face_detection_yunet_2023mar.onnx"
$SfacePath = Join-Path $ModelsDir "face_recognition_sface_2021dec.onnx"
$modelsReady = (Test-Path -LiteralPath $YunetPath) -and ((Get-Item -LiteralPath $YunetPath).Length -gt 1024) -and
               (Test-Path -LiteralPath $SfacePath) -and ((Get-Item -LiteralPath $SfacePath).Length -gt 1024)

if (-not $modelsReady) {
    Write-Host "Downloading YuNet and SFace ONNX models..."
    Push-Location $RepoRoot
    try {
        & $VenvPython -m engine.cli download-models
        if ($LASTEXITCODE -ne 0) {
            throw "Model download failed with exit code $LASTEXITCODE."
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
        & $VenvPython (Join-Path $RepoRoot "engine\ui\generate_app_icon.py")
        if ($LASTEXITCODE -ne 0) {
            throw "generate_app_icon.py failed with exit code $LASTEXITCODE."
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

    Write-Host "Installing and verifying WinIRFaceIDService..."
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
Write-Host "Creating Start Menu shortcut..."
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
exit 0
