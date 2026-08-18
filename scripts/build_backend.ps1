$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Resolve-Path (Join-Path $scriptDir "..")
$packRootDir = Join-Path $rootDir "dist"
$distDir = Join-Path $packRootDir "backend"
$buildDir = Join-Path $packRootDir "backend_build"
$specFile = Join-Path $packRootDir "momacoder.spec"
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

if (-not (Get-Command poetry -ErrorAction SilentlyContinue)) {
    throw "poetry not found in PATH."
}

Write-Host "[backend] install dependencies..."
Push-Location $rootDir
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

    Write-Host "[backend] build exe..."
    $pyproject = Join-Path $rootDir "pyproject.toml"
    $agentsDir = Join-Path $rootDir "data/agents"
    $skillsDir = Join-Path $rootDir "data/skills"
    $modelsDir = Join-Path $rootDir "data/models"
    $promptsDir = Join-Path $rootDir "app/infrastructure/llms/prompts"
    $pyinstallerArgs = @(
        "--name", "momacoder",
        "--onefile",
        "--paths", ".",
        "--distpath", "$distDir",
        "--workpath", "$buildDir",
        "--specpath", "$packRootDir",
        "--add-data", "$pyproject;app",
        "--add-data", "$agentsDir;data/agents",
        "--add-data", "$skillsDir;data/skills",
        "--add-data", "$modelsDir;data/models",
        "--add-data", "$promptsDir;app/infrastructure/llms/prompts",
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
    $pyinstallerArgs += "app/main.py"
    poetry run pyinstaller @pyinstallerArgs
}
finally {
    Pop-Location
}

Write-Host "[backend] done."
