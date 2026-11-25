#Requires -Version 5.1
# Cili Agent startup script
# Auto-detect and download Git Bash, Python, PowerShell 7, and LaTeX compiler

param(
    [int]$Port = 1618,
    [string]$HostName = "127.0.0.1"
)

$ErrorActionPreference = "Stop"

# Project root directory
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataDir = Join-Path $ProjectRoot "data"
$DepsDir = Join-Path $DataDir "deps"
$GitDir = Join-Path $DepsDir "git"
$PythonDir = Join-Path $DepsDir "python"
$PwshDir = Join-Path $DepsDir "pwsh"
$TectonicDir = Join-Path $DepsDir "tectonic"
$FontsDir = Join-Path $DepsDir "fonts"

# Download URLs
$PythonVersion = "3.11.9"
$GitVersion = "2.55.0"
$GitBuild = "5"
$PwshVersion = "7.7.0"
$TectonicVersion = "0.17.0"
$HarmonyFontZip = "HarmonyOS-Sans.zip"
$HarmonyFontDir = "HarmonyOS Sans"
$HarmonyFontSubDir = "HarmonyOS_Sans_SC"
$PythonUrl = "https://mirrors.huaweicloud.com/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GitUrl = @(
    "https://mirrors.huaweicloud.com/git-for-windows/v$GitVersion.windows.$GitBuild/PortableGit-$GitVersion.$GitBuild-64-bit.7z.exe",
    "https://mirrors.tuna.tsinghua.edu.cn/github-release/git-for-windows/git/LatestRelease/PortableGit-$GitVersion.$GitBuild-64-bit.7z.exe"
)
$PwshUrl = @(
    "https://github.com/PowerShell/PowerShell/releases/download/v$PwshVersion/PowerShell-$PwshVersion-win-x64.zip",
    "https://gh-proxy.com/https://github.com/PowerShell/PowerShell/releases/download/v$PwshVersion/PowerShell-$PwshVersion-win-x64.zip",
    "https://ghfast.top/https://github.com/PowerShell/PowerShell/releases/download/v$PwshVersion/PowerShell-$PwshVersion-win-x64.zip",
    "https://ghproxy.net/https://github.com/PowerShell/PowerShell/releases/download/v$PwshVersion/PowerShell-$PwshVersion-win-x64.zip"
)
$TectonicUrl = @(
    "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic@$TectonicVersion/tectonic-$TectonicVersion-x86_64-pc-windows-msvc.zip",
    "https://ghproxy.net/https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic@$TectonicVersion/tectonic-$TectonicVersion-x86_64-pc-windows-msvc.zip",
    "https://ghfast.top/https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic@$TectonicVersion/tectonic-$TectonicVersion-x86_64-pc-windows-msvc.zip",
    "https://gh-proxy.com/https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic@$TectonicVersion/tectonic-$TectonicVersion-x86_64-pc-windows-msvc.zip"
)
$HarmonyFontUrl = "https://developer.huawei.com/images/download/general/HarmonyOS-Sans.zip"

function Write-Status {
    param([string]$Message, [string]$Color = "White")
    Write-Host "[cili] " -NoNewline -ForegroundColor Cyan
    Write-Host $Message -ForegroundColor $Color
}

function Test-GitBash {
    # Only check deps directory
    $depsBash = Join-Path $GitDir "bin\bash.exe"
    if (Test-Path $depsBash) {
        Write-Status "Git Bash found in deps: $depsBash"
        return $depsBash
    }
    return $null
}

function Test-Python {
    # Only check deps directory
    $depsPython = Join-Path $PythonDir "python.exe"
    if (Test-Path $depsPython) {
        Write-Status "Python found in deps: $depsPython"
        return $depsPython
    }
    return $null
}

function Configure-PythonPth {
    # Rewrite python*._pth with canonical content. The project root is stored
    # relative to the exe directory, so moving the project does not break on stale absolute paths.
    $pthFile = Get-ChildItem -Path $PythonDir -Filter "python*._pth" | Select-Object -First 1
    if (-not $pthFile) {
        Write-Status "Warning: no python*._pth found in $PythonDir" "Yellow"
        return
    }
    # Derive the stdlib archive name (python311.zip, etc.) from the actual file, so Python upgrades work.
    $zipFile = Get-ChildItem -Path $PythonDir -Filter "python*.zip" | Select-Object -First 1
    $zipName = if ($zipFile) { $zipFile.Name } else { "python311.zip" }
    # Project root relative to data/deps/python (usually ..\..\..); PS 5.1 lacks GetRelativePath, so compute manually.
    $relProjectRoot = ""
    if ($PythonDir.StartsWith($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        $pyRelative = $PythonDir.Substring($ProjectRoot.Length).TrimStart('\', '/')
        $depth = ($pyRelative -split '[\\/]' | Where-Object { $_ -ne '' }).Count
        $relProjectRoot = ('..\' * $depth).TrimEnd('\')
    }
    # Write without BOM (critical for Python to parse paths correctly)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $lines = [string[]]@(
        $zipName,
        ".",
        "import site",
        $relProjectRoot,
        "Lib/site-packages"
    )
    [System.IO.File]::WriteAllLines($pthFile.FullName, $lines, $utf8NoBom)
    Write-Status "Python _pth configured."
}

function Test-PythonOk {
    # Integrity check: the embeddable stdlib lives in pythonXXX.zip;
    # a missing or corrupt zip makes "import encodings" fail fatally at startup.
    param([string]$PythonExe)
    if (-not (Test-Path $PythonExe)) { return $false }
    # Native stderr is a terminating error under EAP=Stop; switch to Continue temporarily.
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $PythonExe -c "import encodings" 2>$null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $oldEAP
    return $ok
}

function Test-Pwsh {
    # Only check deps directory; the VERSION marker lets a pinned upgrade reinstall.
    $exe = Join-Path $PwshDir "pwsh.exe"
    $versionFile = Join-Path $PwshDir "VERSION"
    if ((Test-Path $exe) -and (Test-Path $versionFile)) {
        if (((Get-Content -Path $versionFile -Raw).Trim()) -eq $PwshVersion) {
            Write-Status "PowerShell $PwshVersion found in deps: $exe"
            return $exe
        }
    }
    return $null
}

function Test-PwshOk {
    # Integrity check: a broken or partial extraction fails to start pwsh at all.
    param([string]$PwshExe)
    if (-not (Test-Path $PwshExe)) { return $false }
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $PwshExe -NoLogo -NoProfile -NonInteractive -Command "exit 0" 2>$null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $oldEAP
    return $ok
}

function Install-Pwsh {
    Write-Status "Installing PowerShell $PwshVersion to deps..." "Yellow"

    # Remove a previous partial/old extraction, then recreate the directory.
    if (Test-Path $PwshDir) {
        Write-Status "Removing old PowerShell directory..."
        Remove-Item -Path $PwshDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Path $PwshDir -Force | Out-Null

    $archivePath = Join-Path $DepsDir "pwsh.zip"
    Download-File -Url $PwshUrl -OutputPath $archivePath

    Write-Status "Extracting PowerShell..."
    Expand-Archive -Path $archivePath -DestinationPath $PwshDir -Force
    Remove-Item -Path $archivePath -Force -ErrorAction SilentlyContinue

    $exe = Join-Path $PwshDir "pwsh.exe"
    if (-not (Test-Path $exe)) {
        throw "PowerShell installation failed: $exe not found"
    }

    # Pin the version so bumping $PwshVersion reinstalls on the next start.
    Set-Content -Path (Join-Path $PwshDir "VERSION") -Value $PwshVersion -Encoding ASCII

    if (-not (Test-PwshOk $exe)) {
        throw "PowerShell installation failed: pwsh.exe cannot start"
    }

    Write-Status "PowerShell $PwshVersion installed successfully." "Green"
    return $exe
}

function Test-Tectonic {
    # Check if any LaTeX compiler is available (tectonic, pdflatex, xelatex, lualatex)
    $oldErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"

    # 1. Check PATH for any LaTeX compiler
    $latexTools = @("tectonic", "pdflatex", "xelatex", "lualatex")
    foreach ($tool in $latexTools) {
        $whereResult = where.exe $tool 2>$null
        if ($whereResult) {
            $toolPath = ($whereResult -split "`n")[0].Trim()
            Write-Status "LaTeX compiler found in PATH: $tool ($toolPath)"
            $ErrorActionPreference = $oldErrorAction
            return $toolPath
        }
    }

    $ErrorActionPreference = $oldErrorAction

    # 2. Check deps directory
    $depsTectonic = Join-Path $TectonicDir "tectonic.exe"
    if (Test-Path $depsTectonic) {
        Write-Status "Tectonic found in deps: $depsTectonic"
        return $depsTectonic
    }

    # 3. Check environment variables
    $envTectonic = $env:TECTONIC_PATH
    if ($envTectonic -and (Test-Path $envTectonic)) {
        Write-Status "Tectonic from env: $envTectonic"
        return $envTectonic
    }

    # 4. Check common installation paths (MiKTeX, TeX Live)
    $candidates = @(
        "$env:ProgramFiles\MiKTeX\miktex\bin\x64\pdflatex.exe",
        "$env:ProgramFiles\MiKTeX 2.9\miktex\bin\pdflatex.exe",
        "${env:ProgramFiles(x86)}\MiKTeX\miktex\bin\pdflatex.exe",
        "C:\texlive\2023\bin\windows\pdflatex.exe",
        "C:\texlive\2022\bin\windows\pdflatex.exe",
        "$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) {
            Write-Status "LaTeX compiler found: $path"
            return $path
        }
    }

    # No LaTeX compiler found
    return $null
}

function Download-File {
    param(
        [string[]]$Url,
        [string]$OutputPath
    )

    # Ensure TLS 1.2+ is enabled
    try {
        $secProtocol = [System.Net.ServicePointManager]::SecurityProtocol
        [System.Net.ServicePointManager]::SecurityProtocol = $secProtocol -bor [System.Net.SecurityProtocolType]::Tls12
    } catch { }

    Import-Module BitsTransfer -ErrorAction SilentlyContinue

    foreach ($currentUrl in $Url) {
        try {
            Write-Status "Downloading: $currentUrl" "Yellow"
            Write-Status "To: $OutputPath" "Yellow"

            Start-BitsTransfer -Source $currentUrl -Destination $OutputPath -DisplayName "Downloading" -ErrorAction Stop
            if ((Test-Path $OutputPath) -and ((Get-Item $OutputPath).Length -gt 0)) {
                $fileSize = (Get-Item $OutputPath).Length
                Write-Status "Download complete ($([math]::Round($fileSize / 1MB, 2)) MB)" "Green"
                return
            }
            Write-Status "Download failed (empty file), trying next mirror..." "Yellow"
            Remove-Item $OutputPath -Force -ErrorAction SilentlyContinue
        } catch {
            Write-Status "Download error: $($_.Exception.Message)" "Yellow"
            Remove-Item $OutputPath -Force -ErrorAction SilentlyContinue
        }
    }

    throw "All download mirrors failed"
}

function Install-GitBash {
    Write-Status "Installing Git Bash to deps..." "Yellow"

    New-Item -ItemType Directory -Path $GitDir -Force | Out-Null

    # Download 7z.exe self-extracting package
    $archivePath = Join-Path $DepsDir "PortableGit.7z.exe"
    Download-File -Url $GitUrl -OutputPath $archivePath

    # Extract (7z.exe self-extractor supports -o parameter)
    Write-Status "Extracting Git Bash..."
    $process = Start-Process -FilePath $archivePath -ArgumentList "-o`"$GitDir`"", "-y" -Wait -PassThru -NoNewWindow
    if ($process.ExitCode -ne 0) {
        throw "Git Bash extraction failed with exit code: $($process.ExitCode)"
    }

    # Clean up archive
    Remove-Item -Path $archivePath -Force -ErrorAction SilentlyContinue

    # Verify
    $bashPath = Join-Path $GitDir "bin\bash.exe"
    if (-not (Test-Path $bashPath)) {
        throw "Git Bash installation failed: $bashPath not found"
    }

    Write-Status "Git Bash installed successfully." "Green"
    return $bashPath
}

function Install-Python {
    Write-Status "Installing Python $PythonVersion to deps..." "Yellow"

    # Clean up existing directory (may have corrupted files from previous attempts)
    if (Test-Path $PythonDir) {
        Write-Status "Removing old Python directory..."
        Remove-Item -Path $PythonDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    New-Item -ItemType Directory -Path $PythonDir -Force | Out-Null

    # Download embeddable zip
    $archivePath = Join-Path $DepsDir "python-embed.zip"
    Download-File -Url $PythonUrl -OutputPath $archivePath

    # Extract
    Write-Status "Extracting Python..."
    Expand-Archive -Path $archivePath -DestinationPath $PythonDir -Force

    # Clean up archive
    Remove-Item -Path $archivePath -Force -ErrorAction SilentlyContinue

    # Configure _pth file (stdlib zip + site-packages + project root)
    Configure-PythonPth

    $pythonExe = Join-Path $PythonDir "python.exe"
    if (-not (Test-Path $pythonExe)) {
        throw "Python installation failed: $pythonExe not found"
    }

    # Install pip (embeddable Python doesn't include pip)
    Write-Status "Installing pip..."
    $getPipPath = Join-Path $PythonDir "get-pip.py"
    try {
        # Download get-pip.py using BITS
        Import-Module BitsTransfer -ErrorAction SilentlyContinue
        $getPipUrl = "https://mirrors.aliyun.com/pypi/get-pip.py"
        Start-BitsTransfer -Source $getPipUrl -Destination $getPipPath -DisplayName "Downloading get-pip.py" -ErrorAction Stop

        if (Test-Path $getPipPath) {
            # Install pip from the Huawei Cloud mirror
            $pipResult = & $pythonExe $getPipPath -i "https://repo.huaweicloud.com/repository/pypi/simple/" --no-warn-script-location 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Status "pip installed successfully." "Green"
            } else {
                Write-Status "Warning: pip installation failed: $pipResult" "Yellow"
            }
            Remove-Item -Path $getPipPath -Force -ErrorAction SilentlyContinue
        } else {
            Write-Status "Warning: could not download get-pip.py" "Yellow"
        }
    } catch {
        Write-Status "Warning: pip installation error: $_" "Yellow"
    }

    Write-Status "Python extracted successfully." "Green"
    return $pythonExe
}

function Install-Tectonic {
    Write-Status "Installing Tectonic $TectonicVersion to deps..." "Yellow"

    New-Item -ItemType Directory -Path $TectonicDir -Force | Out-Null

    # Download zip
    $archivePath = Join-Path $TectonicDir "tectonic.zip"
    Download-File -Url $TectonicUrl -OutputPath $archivePath

    # Extract
    Write-Status "Extracting Tectonic..."
    Expand-Archive -Path $archivePath -DestinationPath $TectonicDir -Force

    # Clean up archive
    Remove-Item -Path $archivePath -Force -ErrorAction SilentlyContinue

    # Verify
    $tectonicExe = Join-Path $TectonicDir "tectonic.exe"
    if (-not (Test-Path $tectonicExe)) {
        throw "Tectonic installation failed: $tectonicExe not found"
    }

    Write-Status "Tectonic installed successfully." "Green"
    return $tectonicExe
}

function Test-HarmonyFont {
    # Check whether the Simplified Chinese (SC) font is already extracted
    $scDir = Join-Path $FontsDir "$HarmonyFontDir\$HarmonyFontSubDir"
    $fontFile = Join-Path $scDir "HarmonyOS_Sans_SC_Regular.ttf"
    if (Test-Path $fontFile) {
        Write-Status "HarmonyOS Sans SC found in deps: $scDir"
        return $scDir
    }
    return $null
}

function Install-HarmonyFont {
    Write-Status "Installing HarmonyOS Sans SC to deps..." "Yellow"

    New-Item -ItemType Directory -Path $FontsDir -Force | Out-Null

    $zipPath = Join-Path $FontsDir $HarmonyFontZip
    Download-File -Url $HarmonyFontUrl -OutputPath $zipPath

    if (-not (Test-Path $zipPath)) {
        throw "HarmonyOS Sans font download failed: $zipPath not found"
    }

    # Extract into the fonts directory
    Write-Status "Extracting HarmonyOS Sans..."
    Expand-Archive -Path $zipPath -DestinationPath $FontsDir -Force
    Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue

    $scDir = Join-Path $FontsDir "$HarmonyFontDir\$HarmonyFontSubDir"
    if (-not (Test-Path $scDir)) {
        throw "HarmonyOS Sans SC extraction failed: $scDir not found"
    }

    Write-Status "HarmonyOS Sans SC installed successfully." "Green"
    return $scDir
}

# ==============================================================
# Main flow
# ==============================================================

Write-Host ""
Write-Host "  =====================================" -ForegroundColor Cyan
Write-Host "     Cili Agent - Starting...         " -ForegroundColor Cyan
Write-Host "  =====================================" -ForegroundColor Cyan
Write-Host ""

# Ensure the data directory exists
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null

# 1. Check/install Git Bash
$gitBashPath = Test-GitBash
if (-not $gitBashPath) {
    Write-Status "Git Bash not found, downloading..." "Yellow"
    $gitBashPath = Install-GitBash
}

# 2. Check/install Python
$pythonPath = Test-Python
if ($pythonPath) {
    # Refresh _pth on every start: fixes stale absolute paths left by moving the project
    Configure-PythonPth
    # Integrity check: an unimportable stdlib means pythonXXX.zip is missing/corrupt; reinstall
    if (-not (Test-PythonOk $pythonPath)) {
        Write-Status "Python runtime is broken (stdlib unavailable), reinstalling..." "Yellow"
        $pythonPath = $null
    }
}
if (-not $pythonPath) {
    Write-Status "Python not found or runtime broken, downloading..." "Yellow"
    $pythonPath = Install-Python
}

# 3. Check/install portable PowerShell 7
$pwshPath = Test-Pwsh
if ($pwshPath -and -not (Test-PwshOk $pwshPath)) {
    Write-Status "Portable PowerShell is broken, reinstalling..." "Yellow"
    $pwshPath = $null
}
if (-not $pwshPath) {
    Write-Status "PowerShell 7 not found, downloading..." "Yellow"
    try {
        $pwshPath = Install-Pwsh
    } catch {
        Write-Status "Warning: PowerShell 7 installation failed: $_" "Yellow"
        Write-Status "Continuing with system PowerShell..." "Yellow"
        $pwshPath = $null
    }
}

# 4. Check/install Tectonic (or another LaTeX compiler)
$tectonicPath = Test-Tectonic
if (-not $tectonicPath) {
    Write-Status "No LaTeX compiler found, installing Tectonic..." "Yellow"
    try {
        $tectonicPath = Install-Tectonic
    } catch {
        Write-Status "Warning: Tectonic installation failed: $_" "Yellow"
        Write-Status "Continuing without LaTeX support..." "Yellow"
        $tectonicPath = $null
    }
} else {
    # Check whether the compiler is Tectonic itself
    $toolName = [System.IO.Path]::GetFileNameWithoutExtension($tectonicPath)
    if ($toolName -ne "tectonic") {
        Write-Status "Using existing LaTeX compiler: $toolName" "Gray"
    }
}

# 5. Check/install HarmonyOS Sans fonts
$harmonyFontDir = Test-HarmonyFont
if (-not $harmonyFontDir) {
    Write-Status "HarmonyOS Sans SC not found, downloading..." "Yellow"
    try {
        $harmonyFontDir = Install-HarmonyFont
    } catch {
        Write-Status "Warning: HarmonyOS Sans font download failed: $_" "Yellow"
        Write-Status "Continuing without HarmonyOS Sans font..." "Yellow"
        $harmonyFontDir = $null
    }
}

# 6. Set environment variables
$env:GIT_BASH_PATH = $gitBashPath

# Prefer the portable PowerShell 7 for the pwsh tool and child processes.
if ($pwshPath) {
    $env:PWSH_PATH = $pwshPath
    $env:PATH = "$PwshDir;$env:PATH"
}

# Add Tectonic to PATH (when installed under deps)
if ($tectonicPath -and $tectonicPath.StartsWith($DepsDir)) {
    $tectonicBin = Split-Path $tectonicPath -Parent
    $env:PATH = "$tectonicBin;$env:PATH"
    Write-Status "Added LaTeX to PATH: $tectonicBin"
}

# Expose the font directory to matplotlib
if ($harmonyFontDir) {
    $env:HARMONY_FONT_DIR = $harmonyFontDir
}

# 7. Start Cili
Write-Status "Starting Cili Agent..."
Write-Status "  Python: $pythonPath"
Write-Status "  Git Bash: $gitBashPath"
if ($pwshPath) {
    Write-Status "  PowerShell: $pwshPath"
} else {
    Write-Status "  PowerShell: system default (portable 7 unavailable)"
}
if ($tectonicPath) {
    $toolName = [System.IO.Path]::GetFileNameWithoutExtension($tectonicPath)
    Write-Status "  LaTeX ($toolName): $tectonicPath"
}
if ($harmonyFontDir) {
    Write-Status "  Font (HarmonyOS Sans SC): $harmonyFontDir"
}
Write-Status "  Port: $Port"
Write-Host ""

# Build arguments
$scriptArgs = @()
if ($Port -ne 1618) {
    $scriptArgs += "--port"
    $scriptArgs += $Port
}
if ($HostName -ne "127.0.0.1") {
    $scriptArgs += "--host"
    $scriptArgs += $HostName
}

# Start main.py (upgrade/restart loop)
# Protocol: a service restart request (upgrade package downloaded) exits
# with code 42; the loop applies the package in a separate process
# (main.py --apply-upgrade-only) and restarts; direct "python main.py" has no such loop.
# Exit code 43 means the managed Python environment is broken beyond auto-repair:
# delete data/deps/python and reinstall it once, then restart.
$mainScript = Join-Path $ProjectRoot "main.py"
$upgradeZip = Join-Path $ProjectRoot "cili-update.zip"  # Must match UPGRADE_ZIP_NAME in core/updater.py
$env:CILI_LAUNCHER = "ps1"
$restartCount = 0
$maxRestarts = 5
$envRebuildCount = 0
$maxEnvRebuilds = 1
for ($i = 0; ; $i++) {
    # Apply a pending upgrade package in a separate short-lived process with no business imports,
    # so the service never runs mixed old/new code. Run only when the package exists; on first
    # start dependencies may not be installed yet (main.py installs them), so tolerate failures.
    # Under EAP=Stop, native stderr raises a terminating error (even with 2>$null); use Continue.
    if (Test-Path $upgradeZip) {
        $oldEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        # Chinese log lines were garbled: the apply subprocess emits UTF-8, but the
        # console default may be GBK, so PowerShell would decode it as GBK. Force both
        # sides to UTF-8 for the duration of the captured call, then restore.
        $oldOutEnc = [Console]::OutputEncoding
        $oldPythonIO = $env:PYTHONIOENCODING
        try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
        $env:PYTHONIOENCODING = "utf-8"
        $applyOut = & $pythonPath $mainScript --apply-upgrade-only 2>&1
        $applyCode = $LASTEXITCODE
        if ($null -eq $oldPythonIO) {
            Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONIOENCODING = $oldPythonIO
        }
        if ($oldOutEnc) {
            try { [Console]::OutputEncoding = $oldOutEnc } catch { }
        }
        $ErrorActionPreference = $oldEAP
        if ($applyCode -ne 0) {
            Write-Status "Upgrade apply skipped (exit $applyCode), continuing startup" "DarkGray"
        } elseif ($applyOut) {
            $applyOut | ForEach-Object { Write-Host $_ }
        }
    }
    # Restart iterations do not open a new browser (--no-browser, with CILI_LAUNCH_RESTART as fallback)
    $iterArgs = $scriptArgs
    if ($i -gt 0) {
        $env:CILI_LAUNCH_RESTART = "1"
        $iterArgs = @($scriptArgs) + "--no-browser"
    }
    $startedAt = Get-Date
    try {
        & $pythonPath $mainScript @iterArgs
    } catch {
        Write-Status "Error: Failed to start Cili Agent" "Red"
        exit 1
    }
    # Broken managed Python environment (main.py could not repair it): the process has
    # exited, so delete data/deps/python without file-lock issues and reinstall it.
    if ($LASTEXITCODE -eq 43) {
        $envRebuildCount++
        if ($envRebuildCount -gt $maxEnvRebuilds) {
            Write-Status "Python environment is still broken after rebuild, stopping" "Red"
            exit 1
        }
        Write-Status "Python environment is broken; reinstalling $PythonDir ..." "Yellow"
        try {
            Remove-Item -Path $PythonDir -Recurse -Force -ErrorAction Stop
            $pythonPath = Install-Python
        } catch {
            Write-Status "Error: Python environment rebuild failed: $_" "Red"
            exit 1
        }
        # The rebuilt environment is a fresh launch: let the next iteration open the browser
        $i = -1
        Remove-Item Env:CILI_LAUNCH_RESTART -ErrorAction SilentlyContinue
        continue
    }
    if ($LASTEXITCODE -ne 42) { break }
    # Only count closely spaced restarts toward the limit; an occasional upgrade of a long-running service does not accumulate
    if (((Get-Date) - $startedAt).TotalSeconds -gt 300) { $restartCount = 0 }
    $restartCount++
    if ($restartCount -ge $maxRestarts) {
        Write-Status "Upgrade restart limit reached ($maxRestarts consecutive), stopping" "Red"
        exit 1
    }
    Write-Status "Service requested restart (exit 42), applying upgrade and restarting..." "Cyan"
}
