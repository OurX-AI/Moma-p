<#
.SYNOPSIS
    MomaCoder One-Click Install Script (Windows PowerShell)

.DESCRIPTION
    Auto-detect/Install Python -> Create venv -> Download wheel from GitHub Releases -> pip install -> moma-setup -> Configure PATH

.EXAMPLE
    # Install latest version
    $script = "$env:TEMP\moma_install.ps1"; iwr -useb https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.ps1 -OutFile $script; & $script; Remove-Item $script -Force

    # Install specific version
    $env:MOMA_VERSION="v0.1.0"; $script = "$env:TEMP\moma_install.ps1"; iwr -useb https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.ps1 -OutFile $script; & $script; Remove-Item $script -Force
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ============ Configuration ============
$GitHubUser = "OurX-AI"
$GitHubRepo = "Moma"
$InstallDir = if ($env:MOMA_HOME) { $env:MOMA_HOME } else { Join-Path $env:USERPROFILE ".moma" }
$VenvDir    = Join-Path $InstallDir "venv"
$BinDir     = Join-Path $VenvDir "Scripts"
$MomaVersion = if ($env:MOMA_VERSION) { $env:MOMA_VERSION } else { "latest" }
$PythonVersion = "3.11.9"
$PythonInstallerUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"

# ============ Utility Functions ============
function Write-Info  { param([string]$Msg) Write-Host "[INFO] $Msg" -ForegroundColor Green }
function Write-Warn  { param([string]$Msg) Write-Host "[WARN] $Msg" -ForegroundColor Yellow }
function Write-Error { param([string]$Msg) Write-Host "[ERROR] $Msg" -ForegroundColor Red }

# ============ Download File ============
function Download-File {
    param([string]$Url, [string]$OutputPath)
    Write-Info "Downloading: $Url"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $webClient = New-Object System.Net.WebClient
        $webClient.DownloadFile($Url, $OutputPath)
        return $true
    } catch {
        Write-Warn "Download failed: $_"
        return $false
    }
}

# ============ Detect Python ============
function Find-Python {
    $candidates = @("python", "python3", "py -3")
    foreach ($cmd in $candidates) {
        try {
            $verStr = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($verStr -match "^(\d+)\.(\d+)$") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -eq 3 -and $minor -ge 10 -and $minor -lt 13) {
                    Write-Info "Detected Python $verStr ($cmd)"
                    return $cmd
                }
            }
        } catch { }
    }
    return $null
}

# ============ Install Python ============
function Install-Python {
    Write-Warn "Python 3.10 ~ 3.12 not found. Installing Python $PythonVersion..."

    $tempDir = Join-Path $env:TEMP "moma_install"
    if (-not (Test-Path $tempDir)) {
        New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    }

    $installerPath = Join-Path $tempDir "python-installer.exe"

    # Download Python installer
    if (-not (Download-File -Url $PythonInstallerUrl -OutputPath $installerPath)) {
        Write-Error "Cannot download Python installer"
        Write-Error "Please install Python 3.10 ~ 3.12 manually: https://www.python.org/downloads/"
        exit 1
    }

    # Silent install Python
    Write-Info "Installing Python $PythonVersion (silent install, may take a few minutes)..."
    $installArgs = @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=1",
        "Include_test=0",
        "TargetDir=$env:LOCALAPPDATA\Programs\Python\Python$($PythonVersion.Replace('.', ''))"
    )

    $process = Start-Process -FilePath $installerPath -ArgumentList $installArgs -Wait -PassThru -NoNewWindow
    if ($process.ExitCode -ne 0) {
        Write-Error "Python installation failed, exit code: $($process.ExitCode)"
        Write-Error "Please install Python 3.10 ~ 3.12 manually: https://www.python.org/downloads/"
        exit 1
    }

    # Cleanup installer
    Remove-Item -Path $installerPath -Force -ErrorAction SilentlyContinue

    # Refresh PATH
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")

    # Verify installation
    $pythonCmd = Find-Python
    if ($pythonCmd) {
        Write-Info "Python installed successfully!"
        return $pythonCmd
    } else {
        Write-Error "Python still not found after installation. Please restart terminal and run install script again."
        exit 1
    }
}

# ============ Get Wheel URL ============
function Get-WheelUrl {
    param([string]$Version)
    if ($Version -eq "latest") {
        $apiUrl = "https://api.github.com/repos/$GitHubUser/$GitHubRepo/releases/latest"
    } else {
        $apiUrl = "https://api.github.com/repos/$GitHubUser/$GitHubRepo/releases/tags/$Version"
    }

    Write-Info "Querying GitHub Release: $Version"

    try {
        $headers = @{ "User-Agent" = "moma-installer" }
        $resp = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 30
        $assets = $resp.assets | Where-Object { $_.name -like "*.whl" }
        if (-not $assets) {
            Write-Error ".whl file not found in Release"
            exit 1
        }
        # Select the latest version (highest version number)
        $asset = $assets | Sort-Object { [version]($_.name -replace 'momacoder-(.+)-py3-none-any\.whl', '$1') } -Descending | Select-Object -First 1
        return $asset.browser_download_url
    } catch {
        Write-Error "GitHub API request failed: $_"
        Write-Error "Manual download: https://github.com/$GitHubUser/$GitHubRepo/releases"
        exit 1
    }
}

# ============ Configure PATH ============
function Add-ToUserPath {
    param([string]$Dir)
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -split ";" | Where-Object { $_ -eq $Dir }) {
        Write-Info "$Dir already in PATH"
        return
    }
    [Environment]::SetEnvironmentVariable("Path", "$Dir;$userPath", "User")
    $env:Path = "$Dir;$env:Path"
    Write-Info "Added $Dir to user PATH (restart terminal to take effect)"
}

# ============ Main Flow ============
function Install-MomaCoder {
    Write-Host ""
    Write-Host "  ============================================" -ForegroundColor Cyan
    Write-Host "           MomaCoder Installer (Windows)       " -ForegroundColor Cyan
    Write-Host "  ============================================" -ForegroundColor Cyan
    Write-Host ""

    # Detect or install Python
    $pythonCmd = Find-Python
    if (-not $pythonCmd) {
        $pythonCmd = Install-Python
    }

    # Create install directory
    if (-not (Test-Path $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }

    # Create virtual environment
    if (-not (Test-Path $VenvDir)) {
        Write-Info "Creating virtual environment: $VenvDir"
        & $pythonCmd -m venv $VenvDir
    } else {
        Write-Info "Virtual environment already exists: $VenvDir"
    }

    $venvPip    = Join-Path $VenvDir "Scripts\pip.exe"
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"

    # Upgrade pip
    Write-Info "Upgrading pip..."
    & $venvPython -m pip install --upgrade pip --quiet

    # Get wheel URL and install
    $wheelUrl = Get-WheelUrl -Version $MomaVersion
    Write-Info "Downloading and installing: $wheelUrl"
    & $venvPython -m pip install $wheelUrl

    # Run moma-setup to initialize runtime data
    $momaSetup = Join-Path $VenvDir "Scripts\moma-setup.exe"
    Write-Info "Initializing runtime data..."
    if (Test-Path $momaSetup) {
        & $momaSetup
    } else {
        Write-Warn "moma-setup not found, skipping initialization"
    }

    # Add to PATH
    Add-ToUserPath -Dir $BinDir

    # Done
    Write-Host ""
    Write-Host "  [OK] Installation complete!" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Usage (restart terminal first):"
    Write-Host "    moma              # Start MomaCoder"
    Write-Host "    moma-setup        # Re-initialize runtime data"
    Write-Host ""
    Write-Host "  Config: $InstallDir\env"
    Write-Host "  Models: $InstallDir\models\chat_models.json"
    Write-Host "  Venv: $VenvDir"
    Write-Host ""
    Write-Host "  Install specific version:"
    Write-Host "    `$env:MOMA_VERSION=`"v0.1.0`"; iwr -useb https://raw.githubusercontent.com/$GitHubUser/$GitHubRepo/main/scripts/install.ps1 | iex"
    Write-Host ""
    Read-Host "Press Enter to close"
}

Install-MomaCoder
