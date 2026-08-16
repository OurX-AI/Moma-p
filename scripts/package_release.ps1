$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Resolve-Path (Join-Path $scriptDir "..")

$backendExe = Join-Path $rootDir "dist/backend/momacoder.exe"
$packRootDir = Join-Path $rootDir "dist"
$releaseDir = Join-Path $rootDir "release"
$releaseConfigDir = Join-Path $releaseDir "config"
$legacyBackendBuildDir = Join-Path $rootDir "backend/build"
$legacyBackendDistDir = Join-Path $rootDir "backend/dist"
$legacyBackendFrontendDistDir = Join-Path $rootDir "backend/frontend_dist"
$legacyBackendSpecFile = Join-Path $rootDir "backend/momacoder.spec"

function Remove-WithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Recurse,
        [int]$MaxTries = 8
    )
    if (-not (Test-Path $Path)) { return $true }
    for ($i = 1; $i -le $MaxTries; $i++) {
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
            if ($i -eq $MaxTries) {
                Write-Warning "[release] cleanup skipped after retries: $Path"
                Write-Warning "[release] last cleanup error: $($_.Exception.Message)"
                return $false
            }
            Start-Sleep -Milliseconds (200 * $i)
        }
    }
    return $false
}

if (-not (Test-Path $backendExe)) {
    throw "backend exe not found: $backendExe. Please run scripts/build_backend.ps1 first."
}

if (-not (Test-Path $releaseDir)) {
    New-Item -ItemType Directory -Path $releaseDir | Out-Null
}
New-Item -ItemType Directory -Path $releaseConfigDir -Force | Out-Null

Copy-Item $backendExe $releaseDir -Force

$backendEnvProd = Join-Path $rootDir "backend/env.production"
$backendEnv = if (Test-Path $backendEnvProd) { $backendEnvProd } else { Join-Path $rootDir "backend/env" }
$releaseEnv = Join-Path $releaseConfigDir "env"
if ((Test-Path $backendEnv) -and (-not (Test-Path $releaseEnv))) {
    Copy-Item $backendEnv $releaseEnv
}

$modelConfigDir = Join-Path $rootDir "backend/app/config"
if (Test-Path $modelConfigDir) {
    Get-ChildItem -Path $modelConfigDir -Filter "*.json" | ForEach-Object {
        $target = Join-Path $releaseConfigDir $_.Name
        if (-not (Test-Path $target)) {
            Copy-Item $_.FullName $target
        }
    }
}

# 清理 dist 中已复制到 release 的产物，保留 backend_build 与 momacoder.spec 以支持增量构建
if (Test-Path $packRootDir) {
    $distBackendDir = Join-Path $packRootDir "backend"
    $distFrontendEmbed = Join-Path $packRootDir "frontend_dist"
    if (Test-Path $distBackendDir) {
        [void](Remove-WithRetry -Path $distBackendDir -Recurse)
    }
    if (Test-Path $distFrontendEmbed) {
        [void](Remove-WithRetry -Path $distFrontendEmbed -Recurse)
    }
}
if (Test-Path $legacyBackendBuildDir) {
    [void](Remove-WithRetry -Path $legacyBackendBuildDir -Recurse)
}
if (Test-Path $legacyBackendDistDir) {
    [void](Remove-WithRetry -Path $legacyBackendDistDir -Recurse)
}
if (Test-Path $legacyBackendFrontendDistDir) {
    [void](Remove-WithRetry -Path $legacyBackendFrontendDistDir -Recurse)
}
if (Test-Path $legacyBackendSpecFile) {
    [void](Remove-WithRetry -Path $legacyBackendSpecFile)
}

Write-Host "[release] packaged at: $releaseDir"

