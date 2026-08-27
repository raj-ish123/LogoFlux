# Creates a CAP-ready zip with only the files needed to deploy.
# Usage:  .\create_deploy_zip.ps1
# Output: LogoFlux-deploy.zip  (in this folder)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$out  = Join-Path $root "LogoFlux-deploy.zip"

if (Test-Path $out) { Remove-Item $out -Force }

$staging = Join-Path $env:TEMP ("logoflux_deploy_" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Path $staging | Out-Null

try {
    # Copy the logoswap package folder
    $src = Join-Path $root "logoswap"
    $dst = Join-Path $staging "logoswap"
    New-Item -ItemType Directory -Path $dst | Out-Null
    Get-ChildItem $src -File | Copy-Item -Destination $dst

    # Copy individual files
    @("logoswap_app.py", "Dockerfile", ".dockerignore", "requirements.txt", "README.md") | ForEach-Object {
        $f = Join-Path $root $_
        if (Test-Path $f) {
            Copy-Item $f (Join-Path $staging $_) -Force
        } else {
            Write-Warning "Missing: $_ (skipped)"
        }
    }

    # Verify logoswap folder made it in
    $pyFiles = Get-ChildItem (Join-Path $staging "logoswap") -File
    Write-Host "logoswap/ contains $($pyFiles.Count) file(s): $($pyFiles.Name -join ', ')"

    # Build zip from staging contents
    Compress-Archive -Path "$staging\*" -DestinationPath $out -Force

    $mb = [math]::Round((Get-Item $out).Length / 1MB, 3)
    Write-Host ""
    Write-Host "Created: $out"
    Write-Host "Size:    $mb MB"
    Write-Host ""
    Write-Host "Upload this zip to CAP (Create App -> Upload folder)."
}
finally {
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}
