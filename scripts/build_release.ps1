<#
.SYNOPSIS
    Stages and packages a WinIRFaceID Windows x64 release (ZIP, optional Inno Setup EXE, and SHA256SUMS.txt).
.PARAMETER Version
    Semantic version string without leading 'v' (defaults to __version__ in engine\__init__.py).
.PARAMETER SkipBuild
    Skip running CMake to build credential_provider and use existing Release binaries.
.PARAMETER SkipInstaller
    Skip compiling the Inno Setup .exe installer even if ISCC.exe is installed.
.PARAMETER SkipBundle
    Do not bundle the private Python runtime, dependency wheels, and ONNX models (the installer then
    downloads them at install time). Bundling needs network access at build time.
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$SkipBuild,
    [switch]$SkipInstaller,
    [switch]$SkipBundle
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ([string]::IsNullOrWhiteSpace($Version)) {
    $InitPy = Join-Path $RepoRoot "engine\__init__.py"
    if (Test-Path -LiteralPath $InitPy) {
        $initText = Get-Content -LiteralPath $InitPy -Raw -ErrorAction SilentlyContinue
        if ($initText -match '__version__\s*=\s*["'']([^"'']+)["'']') {
            $Version = $Matches[1].Trim()
        }
    }
    if ([string]::IsNullOrWhiteSpace($Version)) {
        $Version = "0.1.0"
    }
}
$DistDir = Join-Path $RepoRoot "dist"
$StageDir = Join-Path $DistDir "stage"
$CpDir = Join-Path $RepoRoot "credential_provider"
$CpBuildDir = Join-Path $CpDir "build"
$CpReleaseDir = Join-Path $CpBuildDir "Release"

function Find-CMake {
    $cmd = Get-Command "cmake" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        "C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
        "C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
        "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
        "C:\Program Files\CMake\bin\cmake.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return $null
}

function Find-ISCC {
    $cmd = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    foreach ($candidate in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    return $null
}

function Copy-TreeClean {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        if ($_.PSIsContainer) {
            if ($_.Name -eq "__pycache__" -or $_.Name -eq ".pytest_cache") {
                return
            }
            Copy-TreeClean -Source $_.FullName -Destination (Join-Path $Destination $_.Name)
        }
        else {
            if ($_.Extension -eq ".pyc" -or $_.Extension -eq ".pyo") {
                return
            }
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Destination $_.Name) -Force
        }
    }
}

Write-Host "=== WinIRFaceID Release Packager (v$Version) ==="

# 1. Build C++ targets unless -SkipBuild
if (-not $SkipBuild) {
    $cmakeExe = Find-CMake
    if (-not $cmakeExe) {
        throw "CMake not found on PATH or standard Visual Studio paths. Install CMake + Visual Studio C++ Build Tools or pass -SkipBuild."
    }
    Write-Host "Building credential_provider (x64 Release) using $cmakeExe..."
    & $cmakeExe -S $CpDir -B $CpBuildDir -A x64
    if ($LASTEXITCODE -ne 0) {
        throw "CMake configure failed with exit code $LASTEXITCODE."
    }
    & $cmakeExe --build $CpBuildDir --config Release
    if ($LASTEXITCODE -ne 0) {
        throw "CMake build failed with exit code $LASTEXITCODE."
    }
}

$RequiredBinaries = @(
    "WinIRFaceIDProvider.dll",
    "WinIRFaceIDService.exe",
    "pipe_client_test.exe"
)
foreach ($binName in $RequiredBinaries) {
    $binPath = Join-Path $CpReleaseDir $binName
    if (-not (Test-Path -LiteralPath $binPath)) {
        throw "Expected Release binary not found at '$binPath'."
    }
}

# Ensure app.ico exists before staging / Inno Setup compilation
$AppIconPath = Join-Path $RepoRoot "engine\ui\app.ico"
if (-not (Test-Path -LiteralPath $AppIconPath)) {
    $VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $PyExe = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }
    Write-Host "Generating missing engine\ui\app.ico..."
    & $PyExe (Join-Path $RepoRoot "engine\ui\generate_app_icon.py")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $AppIconPath)) {
        throw "Failed to generate '$AppIconPath'."
    }
}

# 2. Stage clean release directory
if (Test-Path -LiteralPath $StageDir) {
    Remove-Item -LiteralPath $StageDir -Recurse -Force
}
New-Item -ItemType Directory -Path $StageDir -Force | Out-Null

Write-Host "Staging files into '$StageDir'..."

$StageBinDir = Join-Path $StageDir "bin"
New-Item -ItemType Directory -Path $StageBinDir -Force | Out-Null
foreach ($binName in $RequiredBinaries) {
    Copy-Item -LiteralPath (Join-Path $CpReleaseDir $binName) -Destination (Join-Path $StageBinDir $binName) -Force
}

Copy-TreeClean -Source (Join-Path $RepoRoot "engine") -Destination (Join-Path $StageDir "engine")

$StageCpDir = Join-Path $StageDir "credential_provider"
New-Item -ItemType Directory -Path $StageCpDir -Force | Out-Null
foreach ($cpFile in @("install.ps1", "uninstall.ps1", "register.reg", "unregister.reg", "README.md")) {
    Copy-Item -LiteralPath (Join-Path $CpDir $cpFile) -Destination (Join-Path $StageCpDir $cpFile) -Force
}

$StageScriptsDir = Join-Path $StageDir "scripts"
New-Item -ItemType Directory -Path $StageScriptsDir -Force | Out-Null
foreach ($scriptFile in @("setup.ps1", "python_runtime.ps1", "install_service.ps1", "uninstall_service.ps1", "create_shortcut.ps1", "apply_update.ps1")) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot "scripts\$scriptFile") -Destination (Join-Path $StageScriptsDir $scriptFile) -Force
}

Copy-TreeClean -Source (Join-Path $RepoRoot "tools\ir_camera_tester") -Destination (Join-Path $StageDir "tools\ir_camera_tester")

$StageDocsDir = Join-Path $StageDir "docs"
New-Item -ItemType Directory -Path $StageDocsDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $RepoRoot "docs\IPC_PROTOCOL.md") -Destination (Join-Path $StageDocsDir "IPC_PROTOCOL.md") -Force

foreach ($rootFile in @("Install.bat", "Uninstall.bat", "launch_gui.bat", "requirements.txt", "LICENSE", "README.md")) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot $rootFile) -Destination (Join-Path $StageDir $rootFile) -Force
}

# 2a. Create lightweight delta update archive (code + binaries only, before bundling runtime/wheels/models)
$UpdateZipName = "WinIRFaceID-Update-v$Version-win64.zip"
$UpdateZipPath = Join-Path $DistDir $UpdateZipName
if (Test-Path -LiteralPath $UpdateZipPath) {
    Remove-Item -LiteralPath $UpdateZipPath -Force
}
Write-Host "Creating lightweight delta update archive '$UpdateZipPath'..."
Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $UpdateZipPath -CompressionLevel Optimal

# 2b. Bundle the private Python runtime, dependency wheels, and ONNX models (offline install)
if (-not $SkipBundle) {
    . (Join-Path $RepoRoot "scripts\python_runtime.ps1")
    $StageRuntime = Join-Path $StageDir "runtime\python"
    Write-Host "Bundling private Python runtime into '$StageRuntime'..."
    Install-PrivatePython -RuntimeDir $StageRuntime
    $StagePython = Join-Path $StageRuntime "python.exe"

    # Smoke test: a venv built on the bundled runtime (as setup.ps1 does) must work end to end.
    $SmokeVenv = Join-Path $DistDir "smoke-venv"
    if (Test-Path -LiteralPath $SmokeVenv) { Remove-Item -LiteralPath $SmokeVenv -Recurse -Force }
    & $StagePython -I -m venv $SmokeVenv
    if ($LASTEXITCODE -ne 0) { throw "Creating a venv from the bundled runtime failed ($LASTEXITCODE)." }
    $SmokePython = Join-Path $SmokeVenv "Scripts\python.exe"
    $Requirements = Join-Path $RepoRoot "requirements.txt"

    $StageWheels = Join-Path $StageDir "wheels"
    New-Item -ItemType Directory -Path $StageWheels -Force | Out-Null
    Write-Host "Building dependency wheelhouse in '$StageWheels'..."
    # pip wheel (not download) so sdist-only dependencies such as proxy_tools become wheels too.
    & $SmokePython -m pip wheel --disable-pip-version-check --no-input -w $StageWheels -r $Requirements
    if ($LASTEXITCODE -ne 0) { throw "pip wheel failed with exit code $LASTEXITCODE." }

    & $SmokePython -m pip install --disable-pip-version-check --no-input --no-index --find-links $StageWheels -r $Requirements
    if ($LASTEXITCODE -ne 0) { throw "Offline install from bundled wheels failed ($LASTEXITCODE)." }
    $StageModels = Join-Path $StageDir "models"
    Push-Location $StageDir
    try {
        & $SmokePython -c "import cv2, numpy, webview, win32api, winrt.windows.media.capture, engine.cli; print('smoke ok', cv2.__version__)"
        if ($LASTEXITCODE -ne 0) { throw "Import smoke test in the bundled runtime failed ($LASTEXITCODE)." }
        Write-Host "Downloading ONNX models into '$StageModels' (SHA-256 pinned)..."
        & $SmokePython -m engine.cli download-models
        if ($LASTEXITCODE -ne 0) { throw "Model download failed ($LASTEXITCODE)." }
    }
    finally {
        Pop-Location
    }
    foreach ($model in @("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx")) {
        if (-not (Test-Path -LiteralPath (Join-Path $StageModels $model))) {
            throw "Expected bundled model '$model' in '$StageModels'."
        }
    }
    Remove-Item -LiteralPath $SmokeVenv -Recurse -Force
    Get-ChildItem -LiteralPath $StageDir -Recurse -Directory -Filter "__pycache__" | Where-Object {
        $_.FullName -notlike "$StageRuntime*"
    } | Remove-Item -Recurse -Force
}
else {
    Write-Host "Skipping runtime/wheels/models bundling (-SkipBundle); setup will download them at install time."
}

# 3. Create portable ZIP archive
$ZipName = "WinIRFaceID-v$Version-win64.zip"
$ZipPath = Join-Path $DistDir $ZipName
if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}

Write-Host "Creating ZIP archive '$ZipPath'..."
Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $ZipPath -CompressionLevel Optimal

$Artifacts = [System.Collections.Generic.List[string]]::new()
$Artifacts.Add($UpdateZipPath)
$Artifacts.Add($ZipPath)

# 4. Compile Inno Setup installer if ISCC.exe is available and not -SkipInstaller
$InstallerName = "WinIRFaceID-Setup-v$Version-x64.exe"
$InstallerPath = Join-Path $DistDir $InstallerName
if (-not $SkipInstaller) {
    $isccExe = Find-ISCC
    if ($isccExe) {
        if (Test-Path -LiteralPath $InstallerPath) {
            Remove-Item -LiteralPath $InstallerPath -Force
        }
        Write-Host "Compiling Inno Setup installer using $isccExe..."
        & $isccExe "/DAppVersion=$Version" (Join-Path $RepoRoot "installer\WinIRFaceID.iss")
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $InstallerPath)) {
            throw "ISCC.exe failed with exit code $LASTEXITCODE."
        }
        $Artifacts.Add($InstallerPath)
    }
    else {
        Write-Host "Inno Setup 6 (ISCC.exe) not found; skipping .exe installer build."
    }
}
else {
    Write-Host "Skipping Inno Setup installer compilation (-SkipInstaller)."
}

# 5. Generate SHA256SUMS.txt
$SumsPath = Join-Path $DistDir "SHA256SUMS.txt"
$sumLines = foreach ($artifact in $Artifacts) {
    $hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    $name = [System.IO.Path]::GetFileName($artifact)
    "$hash  $name"
}
[System.IO.File]::WriteAllLines($SumsPath, $sumLines, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ""
Write-Host "Release artifacts ready in '$DistDir':"
foreach ($artifact in $Artifacts) {
    $item = Get-Item -LiteralPath $artifact
    $sizeMb = [Math]::Round($item.Length / 1MB, 2)
    Write-Host ("  - {0} ({1} MB)" -f $item.Name, $sizeMb)
}
Write-Host "  - SHA256SUMS.txt"
Write-Host ""
Get-Content -LiteralPath $SumsPath | ForEach-Object { Write-Host "  $_" }
