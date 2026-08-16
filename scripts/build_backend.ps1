$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Resolve-Path (Join-Path $scriptDir "..")
$backendDir = Join-Path $rootDir "backend"
$packRootDir = Join-Path $rootDir "dist"
$distDir = Join-Path $packRootDir "backend"
$buildDir = Join-Path $packRootDir "backend_build"
$specFile = Join-Path $packRootDir "momacoder.spec"
$frontendDistDir = Join-Path $rootDir "frontend/dist"
$embeddedFrontendDir = Join-Path $packRootDir "frontend_dist"
$cleanBuild = ($env:CLEAN_BUILD -eq "1")
$guiBuild = ($env:DEBUG_CONSOLE -ne "1")

function Remove-WithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Recurse
    )
    if (-not (Test-Path $Path)) { return $true }
    $maxTries = 8
    for ($i = 1; $i -le $maxTries; $i++) {
        try {
            if ($Recurse) {
                Remove-Item $Path -Recurse -Force -ErrorAction Stop
            }
            else {
                Remove-Item $Path -Force -ErrorAction Stop
            }
            return $true
        }
        catch {
            if ($i -eq $maxTries) {
                Write-Warning "[backend] cleanup skipped after retries: $Path"
                Write-Warning "[backend] last cleanup error: $($_.Exception.Message)"
                return $false
            }
            Start-Sleep -Milliseconds (150 * $i)
        }
    }
    return $false
}

if (-not (Test-Path $backendDir)) {
    throw "backend directory not found: $backendDir"
}

if (-not (Get-Command poetry -ErrorAction SilentlyContinue)) {
    throw "poetry not found in PATH."
}

Write-Host "[backend] install dependencies..."
Push-Location $backendDir
try {
    poetry install

    Write-Host "[backend] ensure packager dependencies..."
    try {
        poetry run pip install pyinstaller pywebview aiosqlite
    }
    catch {
        Write-Host "[backend] default index failed, retry with aliyun mirror..."
        poetry run pip install -i https://mirrors.aliyun.com/pypi/simple/ pyinstaller pywebview aiosqlite
    }

    if (-not (Test-Path $frontendDistDir)) {
        throw "frontend dist not found: $frontendDistDir. Please run scripts/build_frontend.ps1 first."
    }

    if ($cleanBuild) {
        Write-Host "[backend] CLEAN_BUILD=1, cleanup old build cache..."
        [void](Remove-WithRetry -Path $distDir -Recurse)
        [void](Remove-WithRetry -Path $buildDir -Recurse)
        [void](Remove-WithRetry -Path $specFile)
    }
    else {
        Write-Host "[backend] incremental build mode (set CLEAN_BUILD=1 for full clean rebuild)."
    }
    if (-not (Test-Path $packRootDir)) {
        New-Item -ItemType Directory -Path $packRootDir | Out-Null
    }
    [void](Remove-WithRetry -Path $embeddedFrontendDir -Recurse)

    Write-Host "[backend] embed frontend dist..."
    New-Item -ItemType Directory -Path $embeddedFrontendDir -Force | Out-Null
    Copy-Item (Join-Path $frontendDistDir "*") $embeddedFrontendDir -Recurse

    Write-Host "[backend] build exe..."
    $backendPyproject = Join-Path $backendDir "pyproject.toml"
    $agentPresetDir = Join-Path $backendDir "app/agents/.agent"
    $llmPromptsDir = Join-Path $backendDir "app/infrastructure/llms/prompts"
    $pyinstallerArgs = @(
        "--name", "momacoder",
        "--onefile",
        "--paths", ".",
        "--distpath", "$distDir",
        "--workpath", "$buildDir",
        "--specpath", "$packRootDir",
        "--add-data", "$embeddedFrontendDir;frontend_dist",
        "--add-data", "$backendPyproject;app",
        "--add-data", "$agentPresetDir;app/agents/.agent",
        "--add-data", "$llmPromptsDir;app/infrastructure/llms/prompts",
        "--hidden-import", "webview",
        "--hidden-import", "aiosqlite",
        "--hidden-import", "tiktoken_ext.openai_public",
        "--exclude-module", "torch",
        "--exclude-module", "torchaudio",
        "--exclude-module", "torchvision",
        "--exclude-module", "tensorflow"
    )
    if ($guiBuild) {
        Write-Host "[backend] GUI build mode (no console window). Set DEBUG_CONSOLE=1 to keep console."
        $pyinstallerArgs += "--noconsole"
    }
    else {
        Write-Host "[backend] debug console mode enabled."
    }
    $pyinstallerArgs += "app/main_packaged.py"
    poetry run pyinstaller @pyinstallerArgs

    # 清理打包过程中的临时目录（保留 backend_build/spec 以支持增量构建）
    [void](Remove-WithRetry -Path $embeddedFrontendDir -Recurse)
}
finally {
    Pop-Location
}

Write-Host "[backend] done."
