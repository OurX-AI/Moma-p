$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Step 1/3: build frontend"
& (Join-Path $scriptDir "build_frontend.ps1")

Write-Host "Step 2/3: build backend"
& (Join-Path $scriptDir "build_backend.ps1")

Write-Host "Step 3/3: package release"
& (Join-Path $scriptDir "package_release.ps1")

Write-Host "All done."
