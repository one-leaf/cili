#Requires -Version 5.1
# Cili Agent startup script
# Auto-detect and download Git Bash, Python, and LaTeX compiler

param(
    [int]$Port = 8000,
    [string]$HostName = "0.0.0.0"
)

$ErrorActionPreference = "Stop"

# Project root directory
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataDir = Join-Path $ProjectRoot "data"
$DepsDir = Join-Path $DataDir "deps"
$GitDir = Join-Path $DepsDir "git"
$PythonDir = Join-Path $DepsDir "python"
$TectonicDir = Join-Path $DepsDir "tectonic"
$SettingFile = Join-Path $DataDir "setting.json"

# Download URLs
$PythonVersion = "3.11.9"
$GitVersion = "2.55.0"
$GitBuild = "5"
$TectonicVersion = "0.17.0"
$PythonUrl = "https://mirrors.huaweicloud.com/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GitUrl = @(
    "https://mirrors.huaweicloud.com/git-for-windows/v$GitVersion.windows.$GitBuild/PortableGit-$GitVersion.$GitBuild-64-bit.7z.exe",
    "https://mirrors.tuna.tsinghua.edu.cn/github-release/git-for-windows/git/LatestRelease/PortableGit-$GitVersion.$GitBuild-64-bit.7z.exe"
)
$TectonicUrl = @(
    "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic@$TectonicVersion/tectonic-$TectonicVersion-x86_64-pc-windows-msvc.zip",
    "https://gh-proxy.com/https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic@$TectonicVersion/tectonic-$TectonicVersion-x86_64-pc-windows-msvc.zip"
)

function Write-Status {
    param([string]$Message, [string]$Color = "White")
    Write-Host "[cili] " -NoNewline -ForegroundColor Cyan
    Write-Host $Message -ForegroundColor $Color
}

function Test-GitBash {
    # 1. Check deps directory
    $depsBash = Join-Path $GitDir "bin\bash.exe"
    if (Test-Path $depsBash) {
        Write-Status "Git Bash found in deps: $depsBash"
        return $depsBash
    }

    # 2. Check environment variables
    $envBash = $env:GIT_BASH_PATH
    if ($envBash -and (Test-Path $envBash)) {
        Write-Status "Git Bash from env: $envBash"
        return $envBash
    }

    # 3. Check PATH
    $oldErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $whereResult = where.exe bash 2>$null
    $ErrorActionPreference = $oldErrorAction
    if ($whereResult) {
        foreach ($line in $whereResult -split "`n") {
            $line = $line.Trim()
            if ($line -like "*Git*" -and $line -like "*bash.exe") {
                Write-Status "Git Bash from PATH: $line"
                return $line
            }
        }
    }

    # 4. Check common installation paths
    $candidates = @(
        "$env:ProgramFiles\Git\bin\bash.exe",
        "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
        "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) {
            Write-Status "Git Bash found: $path"
            return $path
        }
    }

    return $null
}

function Test-Python {
    # 1. Check deps directory
    $depsPython = Join-Path $PythonDir "python.exe"
    if (Test-Path $depsPython) {
        Write-Status "Python found in deps: $depsPython"
        return $depsPython
    }

    # 2. Check system Python
    $systemPython = $null
    $oldErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $whereResult = where.exe python 2>$null
    $ErrorActionPreference = $oldErrorAction
    if ($whereResult) {
        $systemPython = ($whereResult -split "`n")[0].Trim()
    }

    if ($systemPython -and (Test-Path $systemPython)) {
        # Check Python version (require 3.10+)
        # Temporarily allow stderr (some Python versions output version info to stderr)
        $oldErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $versionOutput = & $systemPython --version 2>&1
        $ErrorActionPreference = $oldErrorAction
        if ($versionOutput -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 10) {
                Write-Status "System Python: $systemPython (version $major.$minor)"
                return $systemPython
            } else {
                Write-Status "System Python version too low: $major.$minor (need 3.10+)" "Yellow"
                return $null
            }
        }
    }

    return $null
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

    # Ensure TLS 1.2+ is enabled (required for HTTPS mirrors on Win10)
    try {
        $secProtocol = [System.Net.ServicePointManager]::SecurityProtocol
        [System.Net.ServicePointManager]::SecurityProtocol = $secProtocol -bor [System.Net.SecurityProtocolType]::Tls12
    } catch { }

    foreach ($currentUrl in $Url) {
        try {
            Write-Status "Downloading: $currentUrl" "Yellow"
            Write-Status "To: $OutputPath" "Yellow"

            Write-Status "Using certutil..."
            & certutil -urlcache -split -f $currentUrl $OutputPath 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0 -and (Test-Path $OutputPath)) {
                $fileSize = (Get-Item $OutputPath).Length
                if ($fileSize -gt 0) {
                    Write-Status "Download complete ($([math]::Round($fileSize / 1MB, 2)) MB)" "Green"
                    return
                }
            }
            Write-Status "certutil failed (exit $LASTEXITCODE), trying next mirror..." "Yellow"
            Remove-Item $OutputPath -Force -ErrorAction SilentlyContinue
        } catch {
            Write-Status "Download error: $($_.Exception.Message)" "Yellow"
        }
        Remove-Item $OutputPath -Force -ErrorAction SilentlyContinue
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

    # Configure _pth file (enable site-packages + project root)
    $pthFile = Get-ChildItem -Path $PythonDir -Filter "python*._pth" | Select-Object -First 1
    if ($pthFile) {
        Write-Status "Configuring Python _pth file..."
        # Read with ASCII to avoid BOM issues
        $pthContent = [System.IO.File]::ReadAllLines($pthFile.FullName)
        $newContent = New-Object System.Collections.Generic.List[string]
        $hasLibSitePackages = $false
        foreach ($line in $pthContent) {
            if ($line -match "^#import site") {
                $newContent.Add("import site")
            } elseif ($line -match "^Lib\\site-packages") {
                $newContent.Add($line)
                $hasLibSitePackages = $true
            } else {
                $newContent.Add($line)
            }
        }
        # Add project root (so `from core.xxx import` works)
        $newContent.Add($ProjectRoot)
        # Add Lib/site-packages path if not present
        if (-not $hasLibSitePackages) {
            $newContent.Add("Lib/site-packages")
        }
        # Write without BOM (critical for Python to parse paths correctly)
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllLines($pthFile.FullName, $newContent.ToArray(), $utf8NoBom)
        Write-Status "Python _pth configured."
    }

    $pythonExe = Join-Path $PythonDir "python.exe"
    if (-not (Test-Path $pythonExe)) {
        throw "Python installation failed: $pythonExe not found"
    }

    # Install pip (embeddable Python doesn't include pip)
    Write-Status "Installing pip..."
    $getPipPath = Join-Path $PythonDir "get-pip.py"
    try {
        # 使用阿里云镜像下载 get-pip.py
        $getPipUrl = "https://mirrors.aliyun.com/pypi/get-pip.py"
        & certutil -urlcache -split -f $getPipUrl $getPipPath 2>&1 | Out-Null

        if (Test-Path $getPipPath) {
            # 使用阿里云源安装 pip
            $pipResult = & $pythonExe $getPipPath -i "https://mirrors.aliyun.com/pypi/simple/" --no-warn-script-location 2>&1
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

function Update-SettingJson {
    param([string]$BashPath)

    if (-not (Test-Path $SettingFile)) {
        return
    }

    try {
        $settings = Get-Content $SettingFile -Raw | ConvertFrom-Json
        if (-not $settings.system) {
            $settings | Add-Member -NotePropertyName "system" -NotePropertyValue @{}
        }
        $settings.system.bash_path = $BashPath
        $settings | ConvertTo-Json -Depth 10 | Set-Content $SettingFile -Encoding UTF8
        Write-Status "Updated setting.json with bash_path"
    } catch {
        Write-Status "Warning: failed to update setting.json" "Yellow"
    }
}

# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

Write-Host ""
Write-Host "  =====================================" -ForegroundColor Cyan
Write-Host "     Cili Agent - Starting...         " -ForegroundColor Cyan
Write-Host "  =====================================" -ForegroundColor Cyan
Write-Host ""

# 确保 data 目录存在
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null

# 1. 检查/安装 Git Bash
$gitBashPath = Test-GitBash
$usingDepsGit = $false
if (-not $gitBashPath) {
    Write-Status "Git Bash not found, downloading..." "Yellow"
    $gitBashPath = Install-GitBash
    $usingDepsGit = $true
}

# 2. 检查/安装 Python
$pythonPath = Test-Python
if (-not $pythonPath) {
    Write-Status "Python not found or version too low, downloading..." "Yellow"
    $pythonPath = Install-Python
}

# 3. 检查/安装 Tectonic (或其他 LaTeX 编译器)
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
    # 检查是否是 tectonic 本身
    $toolName = [System.IO.Path]::GetFileNameWithoutExtension($tectonicPath)
    if ($toolName -ne "tectonic") {
        Write-Status "Using existing LaTeX compiler: $toolName" "Gray"
    }
}

# 4. 设置环境变量
if ($usingDepsGit) {
    $env:GIT_BASH_PATH = $gitBashPath
    Update-SettingJson -BashPath $gitBashPath
}

# 将 Tectonic 添加到 PATH（如果安装在 deps 目录）
if ($tectonicPath -and $tectonicPath.StartsWith($DepsDir)) {
    $tectonicBin = Split-Path $tectonicPath -Parent
    $env:PATH = "$tectonicBin;$env:PATH"
    Write-Status "Added LaTeX to PATH: $tectonicBin"
}

# 5. 启动 Cili
Write-Status "Starting Cili Agent..."
Write-Status "  Python: $pythonPath"
Write-Status "  Git Bash: $gitBashPath"
if ($tectonicPath) {
    $toolName = [System.IO.Path]::GetFileNameWithoutExtension($tectonicPath)
    Write-Status "  LaTeX ($toolName): $tectonicPath"
}
Write-Status "  Port: $Port"
Write-Host ""

# Build arguments
$scriptArgs = @()
if ($Port -ne 8000) {
    $scriptArgs += "--port"
    $scriptArgs += $Port
}
if ($HostName -ne "0.0.0.0") {
    $scriptArgs += "--host"
    $scriptArgs += $HostName
}

# 启动 main.py
try {
    $mainScript = Join-Path $ProjectRoot "main.py"
    & $pythonPath $mainScript @scriptArgs
} catch {
    Write-Status "Error: Failed to start Cili Agent" "Red"
    exit 1
}
