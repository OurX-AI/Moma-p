<#
.SYNOPSIS
    MomaCoder 一键安装脚本（Windows PowerShell）

.DESCRIPTION
    自动检测 Python → 创建虚拟环境 → 从 GitHub Releases 下载 wheel → pip install → moma-setup → 配置 PATH

.EXAMPLE
    # 默认安装最新版本
    iwr -useb https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.ps1 | iex

    # 指定版本
    $env:MOMA_VERSION="v0.1.0"; iwr -useb https://raw.githubusercontent.com/OurX-AI/Moma-p/main/scripts/install.ps1 | iex
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ============ 配置 ============
$GitHubUser = "OurX-AI"
$GitHubRepo = "Moma-p"
$InstallDir = if ($env:MOMA_HOME) { $env:MOMA_HOME } else { Join-Path $env:USERPROFILE ".moma" }
$VenvDir    = Join-Path $InstallDir "venv"
$BinDir     = Join-Path $VenvDir "Scripts"
$MomaVersion = if ($env:MOMA_VERSION) { $env:MOMA_VERSION } else { "latest" }

# ============ 工具函数 ============
function Write-Info  { param([string]$Msg) Write-Host "[INFO] $Msg" -ForegroundColor Green }
function Write-Warn  { param([string]$Msg) Write-Host "[WARN] $Msg" -ForegroundColor Yellow }
function Write-Error { param([string]$Msg) Write-Host "[ERROR] $Msg" -ForegroundColor Red }

# ============ 检测 Python ============
function Find-Python {
    $candidates = @("python", "python3", "py -3")
    foreach ($cmd in $candidates) {
        try {
            $verStr = & cmd /c "$cmd -c `"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')`" 2>$null"
            if ($verStr -match "^(\d+)\.(\d+)$") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -eq 3 -and $minor -ge 10 -and $minor -lt 13) {
                    Write-Info "检测到 Python $verStr ($cmd)"
                    return $cmd
                }
            }
        } catch { }
    }
    Write-Error "需要 Python 3.10 ~ 3.12，但未找到。"
    Write-Error "请从 https://www.python.org/downloads/ 安装后重试。"
    exit 1
}

# ============ 获取 wheel 下载地址 ============
function Get-WheelUrl {
    param([string]$Version)
    if ($Version -eq "latest") {
        $apiUrl = "https://api.github.com/repos/$GitHubUser/$GitHubRepo/releases/latest"
    } else {
        $apiUrl = "https://api.github.com/repos/$GitHubUser/$GitHubRepo/releases/tags/$Version"
    }

    Write-Info "查询 GitHub Release: $Version"

    try {
        $headers = @{ "User-Agent" = "moma-installer" }
        $resp = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 30
        $asset = $resp.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1
        if (-not $asset) {
            Write-Error "Release 中未找到 .whl 文件"
            exit 1
        }
        return $asset.browser_download_url
    } catch {
        Write-Error "GitHub API 请求失败: $_"
        Write-Error "也可手动下载: https://github.com/$GitHubUser/$GitHubRepo/releases"
        exit 1
    }
}

# ============ 配置 PATH ============
function Add-ToUserPath {
    param([string]$Dir)
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -split ";" | Where-Object { $_ -eq $Dir }) {
        Write-Info "$Dir 已在 PATH 中"
        return
    }
    [Environment]::SetEnvironmentVariable("Path", "$Dir;$userPath", "User")
    $env:Path = "$Dir;$env:Path"
    Write-Info "已将 $Dir 添加到用户 PATH（重新打开终端后生效）"
}

# ============ 主流程 ============
function Install-MomaCoder {
    Write-Host ""
    Write-Host "  ============================================" -ForegroundColor Cyan
    Write-Host "           MomaCoder 安装程序 (Windows)         " -ForegroundColor Cyan
    Write-Host "  ============================================" -ForegroundColor Cyan
    Write-Host ""

    $pythonCmd = Find-Python

    # 创建安装目录
    if (-not (Test-Path $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }

    # 创建虚拟环境
    if (-not (Test-Path $VenvDir)) {
        Write-Info "创建虚拟环境: $VenvDir"
        & $pythonCmd -m venv $VenvDir
    } else {
        Write-Info "虚拟环境已存在: $VenvDir"
    }

    $venvPip    = Join-Path $VenvDir "Scripts\pip.exe"
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"

    # 升级 pip
    Write-Info "升级 pip..."
    & $venvPython -m pip install --upgrade pip --quiet

    # 获取 wheel 地址并安装
    $wheelUrl = Get-WheelUrl -Version $MomaVersion
    Write-Info "下载并安装: $wheelUrl"
    & $venvPip install $wheelUrl

    # 运行 moma-setup 初始化运行时数据
    $momaSetup = Join-Path $VenvDir "Scripts\moma-setup.exe"
    Write-Info "初始化运行时数据..."
    if (Test-Path $momaSetup) {
        & $momaSetup
    } else {
        Write-Warn "未找到 moma-setup，跳过初始化"
    }

    # 添加到 PATH
    Add-ToUserPath -Dir $BinDir

    # 完成
    Write-Host ""
    Write-Host "  [OK] 安装完成！" -ForegroundColor Green
    Write-Host ""
    Write-Host "  使用方法（重新打开终端后）："
    Write-Host "    moma              # 启动 MomaCoder"
    Write-Host "    moma-setup        # 重新初始化运行时数据"
    Write-Host ""
    Write-Host "  配置文件: $InstallDir\env"
    Write-Host "  模型配置: $InstallDir\models\chat_models.json"
    Write-Host "  虚拟环境: $VenvDir"
    Write-Host ""
    Write-Host "  指定版本安装："
    Write-Host "    `$env:MOMA_VERSION=`"v0.1.0`"; iwr -useb https://raw.githubusercontent.com/$GitHubUser/$GitHubRepo/main/scripts/install.ps1 | iex"
    Write-Host ""
}

Install-MomaCoder
