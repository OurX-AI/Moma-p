$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Resolve-Path (Join-Path $scriptDir "..")
$packRootDir = Join-Path $rootDir "dist"
$distDir = Join-Path $packRootDir "backend"
$buildDir = Join-Path $packRootDir "backend_build"
$specFile = Join-Path $packRootDir "momacoder.spec"
$cleanBuild = ($env:CLEAN_BUILD -eq "1")

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

    Write-Host "[backend] ensure dependencies..."
    try {
        poetry run pip install pywebview aiosqlite
    }
    catch {
        Write-Host "[backend] default index failed, retry with aliyun mirror..."
        poetry run pip install -i https://mirrors.aliyun.com/pypi/simple/ pywebview aiosqlite
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
}
finally {
    Pop-Location
}

Write-Host "[backend] done."
