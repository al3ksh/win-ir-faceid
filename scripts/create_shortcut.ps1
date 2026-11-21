<#
.SYNOPSIS
    Creates a Windows Start Menu (and optionally Desktop) shortcut for WinIRFaceID.
.DESCRIPTION
    Creates %APPDATA%\Microsoft\Windows\Start Menu\Programs\WinIRFaceID.lnk
    (or %ProgramData%\Microsoft\Windows\Start Menu\Programs\WinIRFaceID.lnk when -AllUsers is set)
    pointing to .venv\Scripts\pythonw.exe -m engine.cli gui with engine\ui\app.ico.
    If engine\ui\app.ico is missing, runs engine\ui\generate_app_icon.py first.
.PARAMETER Desktop
    Also create a WinIRFaceID.lnk shortcut on the Desktop.
.PARAMETER AllUsers
    Create shortcuts in the machine-wide ProgramData Start Menu (and Public Desktop if -Desktop is set).
#>
[CmdletBinding()]
param(
    [switch]$Desktop,
    [switch]$AllUsers
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonwExe = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$IconPath = Join-Path $RepoRoot "engine\ui\app.ico"
$IconGenerator = Join-Path $RepoRoot "engine\ui\generate_app_icon.py"

if (-not (Test-Path -LiteralPath $PythonwExe)) {
    throw "Python windowless executable not found at '$PythonwExe'."
}

if (-not (Test-Path -LiteralPath $IconPath)) {
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        throw "Python executable not found at '$PythonExe' to generate '$IconPath'."
    }
    if (-not (Test-Path -LiteralPath $IconGenerator)) {
        throw "Icon generator script not found at '$IconGenerator'."
    }
    Write-Host "Generating application icon at '$IconPath'..."
    Push-Location $RepoRoot
    try {
        & $PythonExe $IconGenerator
        if ($LASTEXITCODE -ne 0) {
            throw "generate_app_icon.py failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function New-WinIRFaceIDShortcut {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ShortcutPath
    )

    $parentDir = Split-Path -Parent $ShortcutPath
    if (-not (Test-Path -LiteralPath $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $PythonwExe
    $shortcut.Arguments = "-m engine.cli gui"
    $shortcut.WorkingDirectory = $RepoRoot
    $shortcut.IconLocation = "$IconPath,0"
    $shortcut.Description = "WinIRFaceID - Active IR Face Unlock"
    $shortcut.Save()
    Write-Host "Created shortcut: $ShortcutPath"
}

if ($AllUsers) {
    $StartMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"
}
else {
    $StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
}
$StartMenuShortcut = Join-Path $StartMenuDir "WinIRFaceID.lnk"
New-WinIRFaceIDShortcut -ShortcutPath $StartMenuShortcut

if ($Desktop) {
    if ($AllUsers) {
        $DesktopDir = [Environment]::GetFolderPath("CommonDesktopDirectory")
    }
    else {
        $DesktopDir = [Environment]::GetFolderPath("Desktop")
    }
    $DesktopShortcut = Join-Path $DesktopDir "WinIRFaceID.lnk"
    New-WinIRFaceIDShortcut -ShortcutPath $DesktopShortcut
}
