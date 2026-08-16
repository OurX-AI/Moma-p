$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Resolve-Path (Join-Path $scriptDir "..")
$frontendDir = Join-Path $rootDir "frontend"

if (-not (Test-Path $frontendDir)) {
    throw "frontend directory not found: $frontendDir"
}

Write-Host "[frontend] install dependencies..."
Push-Location $frontendDir
try {
    if (Get-Command pnpm -ErrorAction SilentlyContinue) {
        pnpm install --frozen-lockfile
        if ($LASTEXITCODE -ne 0) { throw "pnpm install failed with exit code $LASTEXITCODE" }
        Write-Host "[frontend] build with pnpm..."
        pnpm run build
        if ($LASTEXITCODE -ne 0) { throw "pnpm run build failed with exit code $LASTEXITCODE" }
    }
    elseif (Get-Command npm -ErrorAction SilentlyContinue) {
        function Exec([string]$cmd, [string[]]$argv) {
            & $cmd @argv
            if ($LASTEXITCODE -ne 0) {
                throw "$cmd failed with exit code $LASTEXITCODE"
            }
        }

        $maxTries = 3
        $installed = $false
        for ($i = 1; $i -le $maxTries; $i++) {
            try {
                Write-Host "[frontend] npm ci (try $i/$maxTries)..."
                Exec "npm" @("ci")
                $installed = $true
                break
            }
            catch {
                Write-Host "[frontend] npm ci failed: $($_.Exception.Message)"
                Write-Host "[frontend] cleanup node_modules and retry..."
                if (Test-Path (Join-Path $frontendDir "node_modules")) {
                    Remove-Item (Join-Path $frontendDir "node_modules") -Recurse -Force -ErrorAction SilentlyContinue
                }
                Start-Sleep -Seconds 2
                try {
                    Write-Host "[frontend] npm install (try $i/$maxTries)..."
                    Exec "npm" @("install")
                    $installed = $true
                    break
                }
                catch {
                    Write-Host "[frontend] npm install failed: $($_.Exception.Message)"
                    Start-Sleep -Seconds 3
                }
            }
        }

        if (-not $installed) { throw "npm dependencies install failed." }

        Write-Host "[frontend] build..."
        Exec "npm" @("run","build")
    }
    else {
        throw "Neither pnpm nor npm found in PATH."
    }
}
finally {
    Pop-Location
}

Write-Host "[frontend] done."
