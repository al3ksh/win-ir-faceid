<#
.SYNOPSIS
    Helpers for the private, admin-only Python runtime used by WinIRFaceID (dot-source this file).
.DESCRIPTION
    The LocalSystem service runs .venv\Scripts\python.exe, and a venv executes the stdlib of its base
    interpreter ("home" in pyvenv.cfg). A per-user Python (e.g. %LOCALAPPDATA%\Programs\Python) is writable
    by that user, which would let them run code as SYSTEM. So WinIRFaceID ships its own CPython in
    <install dir>\runtime\python, which inherits the Program Files ACL (Users: read/execute only).

    The runtime comes from the official python.org NuGet package ("python", x64), whose binaries are
    Authenticode-signed by the Python Software Foundation; the signature is verified after extraction.
#>

$script:PrivatePythonVersion = "3.11.9"

function Test-PsfSignature {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sig = Get-AuthenticodeSignature -LiteralPath $Path
    return ($sig.Status -eq "Valid") -and ($sig.SignerCertificate.Subject -match "O=Python Software Foundation")
}

function Test-PrivatePython {
    param([Parameter(Mandatory = $true)][string]$RuntimeDir)
    $exe = Join-Path $RuntimeDir "python.exe"
    if (-not (Test-Path -LiteralPath $exe)) { return $false }
    $probe = "import sys, struct, venv, ensurepip; sys.exit(0 if sys.version_info[:2] == (3, 11) and struct.calcsize('P') == 8 else 1)"
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $exe -I -c $probe 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

function Install-PrivatePython {
    <#
    Downloads python.<ver>.nupkg from nuget.org (or uses -PackagePath), verifies the PSF signature of the
    extracted python.exe / python311.dll, and installs the package's tools\ tree into -RuntimeDir.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [string]$PackagePath = "",
        [string]$Version = $script:PrivatePythonVersion,
        [string]$ExpectedSha256 = ""
    )

    # Work next to the destination (inside the admin-only install dir), never in the user's %TEMP%:
    # the stdlib .py files are not signed, so the package must not be swappable mid-install.
    $parent = Split-Path -Parent $RuntimeDir
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $work = Join-Path $parent (".python-staging-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    try {
        if ([string]::IsNullOrWhiteSpace($PackagePath)) {
            $url = "https://api.nuget.org/v3-flatcontainer/python/$Version/python.$Version.nupkg"
            $PackagePath = Join-Path $work "python.$Version.nupkg"
            Write-Host "Downloading Python $Version runtime from $url ..."
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $oldProgress = $ProgressPreference
            $ProgressPreference = "SilentlyContinue"
            try {
                Invoke-WebRequest -Uri $url -OutFile $PackagePath -UseBasicParsing
            }
            finally {
                $ProgressPreference = $oldProgress
            }
        }

        if (-not [string]::IsNullOrWhiteSpace($ExpectedSha256)) {
            $actual = (Get-FileHash -LiteralPath $PackagePath -Algorithm SHA256).Hash
            if ($actual -ne $ExpectedSha256.Trim()) {
                throw "SHA-256 mismatch for Python package: got $actual, expected $ExpectedSha256."
            }
        }

        $extract = Join-Path $work "pkg"
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($PackagePath, $extract)
        $tools = Join-Path $extract "tools"
        foreach ($bin in @("python.exe", "pythonw.exe", "python311.dll")) {
            $binPath = Join-Path $tools $bin
            if (-not (Test-Path -LiteralPath $binPath)) {
                throw "Python package is missing tools\$bin."
            }
            if (-not (Test-PsfSignature -Path $binPath)) {
                throw "tools\$bin in the Python package is not validly signed by the Python Software Foundation."
            }
        }

        if (Test-Path -LiteralPath $RuntimeDir) {
            Remove-Item -LiteralPath $RuntimeDir -Recurse -Force
        }
        Move-Item -LiteralPath $tools -Destination $RuntimeDir
    }
    finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (-not (Test-PrivatePython -RuntimeDir $RuntimeDir)) {
        throw "The private Python runtime in '$RuntimeDir' does not work (expected CPython 3.11 x64 with venv)."
    }
}

function Get-VenvHome {
    param([Parameter(Mandatory = $true)][string]$VenvDir)
    $cfg = Join-Path $VenvDir "pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $cfg)) { return $null }
    foreach ($line in Get-Content -LiteralPath $cfg) {
        if ($line -match '^\s*home\s*=\s*(.+?)\s*$') {
            return $Matches[1].TrimEnd("\")
        }
    }
    return $null
}
