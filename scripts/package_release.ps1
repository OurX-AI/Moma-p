$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Resolve-Path (Join-Path $scriptDir "..")

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

if (-not (Test-Path $releaseDir)) {
    New-Item -ItemType Directory -Path $releaseDir | Out-Null
}
New-Item -ItemType Directory -Path $releaseConfigDir -Force | Out-Null

# Copy env config
$envProd = Join-Path $rootDir "env.production"
$envFile = if (Test-Path $envProd) { $envProd } else { Join-Path $rootDir "env" }
$releaseEnv = Join-Path $releaseConfigDir "env"
if ((Test-Path $envFile) -and (-not (Test-Path $releaseEnv))) {
    Copy-Item $envFile $releaseEnv
}

Write-Host "[release] packaged at: $releaseDir"
