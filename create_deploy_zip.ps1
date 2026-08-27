# Creates a CAP-ready zip with only the files needed to deploy.
# Uses forward-slash entry names so it extracts correctly on Linux (CAP builder).
# Usage:  .\create_deploy_zip.ps1
# Output: LogoFlux-deploy.zip  (in this folder)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$out  = Join-Path $root "LogoFlux-deploy.zip"

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

if (Test-Path $out) { Remove-Item $out -Force }

# Build the list of files to include as (absolutePath, zipEntryName) pairs.
# zipEntryName ALWAYS uses forward slashes.
$files = @()

# logoswap package
Get-ChildItem (Join-Path $root "logoswap") -File | ForEach-Object {
    $files += [pscustomobject]@{ Path = $_.FullName; Entry = "logoswap/$($_.Name)" }
}

# root-level files
@("logoswap_app.py", "Dockerfile", ".dockerignore", "requirements.txt", "README.md") | ForEach-Object {
    $f = Join-Path $root $_
    if (Test-Path $f) {
        $files += [pscustomobject]@{ Path = $f; Entry = $_ }
    } else {
        Write-Warning "Missing: $_ (skipped)"
    }
}

# Create the archive with explicit forward-slash entry names.
$fs  = [System.IO.File]::Open($out, [System.IO.FileMode]::Create)
$zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($item in $files) {
        $entry  = $zip.CreateEntry($item.Entry, [System.IO.Compression.CompressionLevel]::Optimal)
        $dest   = $entry.Open()
        $bytes  = [System.IO.File]::ReadAllBytes($item.Path)
        $dest.Write($bytes, 0, $bytes.Length)
        $dest.Close()
    }
}
finally {
    $zip.Dispose()
    $fs.Dispose()
}

# Verify
$verify = [System.IO.Compression.ZipFile]::OpenRead($out)
Write-Host "Zip entries (forward-slash paths):"
$verify.Entries | ForEach-Object { Write-Host "  $($_.FullName)" }
$verify.Dispose()

$mb = [math]::Round((Get-Item $out).Length / 1MB, 3)
Write-Host ""
Write-Host "Created: $out"
Write-Host "Size:    $mb MB"
Write-Host ""
Write-Host "Upload this zip to CAP (Create App -> Upload folder)."
