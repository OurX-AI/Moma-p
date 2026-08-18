$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Resolve-Path (Join-Path $scriptDir "..")

$backendExe = Join-Path $rootDir "dist/backend/momacoder.exe"
$packRootDir = Join-Path $rootDir "dist"
$releaseDir = Join-Path $rootDir "release"
$releaseConfigDir = Join-Path $releaseDir "config"

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

# Copy env config
$envProd = Join-Path $rootDir "env.production"
$envFile = if (Test-Path $envProd) { $envProd } else { Join-Path $rootDir "env" }
$releaseEnv = Join-Path $releaseConfigDir "env"
if ((Test-Path $envFile) -and (-not (Test-Path $releaseEnv))) {
    Copy-Item $envFile $releaseEnv
}

# Clean up dist build artifacts (keep momacoder.spec for incremental builds)
$distBackendDir = Join-Path $packRootDir "backend"
$distBuildDir = Join-Path $packRootDir "backend_build"
if (Test-Path $distBackendDir) {
    [void](Remove-WithRetry -Path $distBackendDir -Recurse)
}
if (Test-Path $distBuildDir) {
    [void](Remove-WithRetry -Path $distBuildDir -Recurse)
}

Write-Host "[release] packaged at: $releaseDir"
