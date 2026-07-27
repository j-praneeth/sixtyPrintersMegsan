<#
    Fetch the OFFLINE dependency bundle for Windows 7 clients.

    Run this ONCE on any machine with internet, then copy the whole repo
    (including this vendor\ folder) to each Windows 7 client. setup.ps1
    prefers these local files over downloading, so the Win7 install runs
    fully offline. Windows 10/11 clients ignore the bundle and fetch the
    latest versions themselves.

    These versions are the ones pinned in setup.ps1 for legacy OS - do not
    "upgrade" them without testing on Windows 7, newer builds are not.

      powershell -ExecutionPolicy Bypass -File vendor\fetch-win7-bundle.ps1
#>
$ErrorActionPreference = 'Stop'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
$dir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }

# name (in vendor\)                    ->  download URL
$items = [ordered]@{
    'python-3.8.10-embed-amd64.zip'    = 'https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-amd64.zip'
    'gs9561w64.exe'                    = 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs9561/gs9561w64.exe'
    'qpdf-10.6.3-bin-mingw64.zip'      = 'https://github.com/qpdf/qpdf/releases/download/release-qpdf-10.6.3/qpdf-10.6.3-bin-mingw64.zip'
    'mfilemon-setup.exe'               = 'https://github.com/lomo74/mfilemon/releases/download/v1.6.1/mfilemon-setup.exe'
}

foreach ($name in $items.Keys) {
    $dest = Join-Path $dir $name
    if (Test-Path $dest) { Write-Host "[skip] $name already present" -ForegroundColor Gray; continue }
    Write-Host "[get ] $name" -ForegroundColor Cyan
    Invoke-WebRequest -UseBasicParsing -Uri $items[$name] -OutFile $dest `
        -Headers @{ 'User-Agent' = 'Mozilla/5.0 VirtualCloudPrinter' } -MaximumRedirection 10
    Write-Host ("       {0:N0} bytes" -f (Get-Item $dest).Length) -ForegroundColor Green
}
Write-Host "`nDone. Copy the whole repo (with this vendor\ folder) to each Windows 7 client." -ForegroundColor Green
Write-Host "Remember: on each Win7 box install .NET 4.8 + WMF 5.1 BEFORE running install.bat (see SETUP.md)." -ForegroundColor Gray
