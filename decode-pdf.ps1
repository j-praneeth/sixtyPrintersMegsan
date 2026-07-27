<#
    Decode (decrypt) an AES-256 encrypted PDF produced by this toolkit.
    ====================================================================
    Writes a plaintext copy next to each input: <name>-decrypted.pdf.

    Simplest of all: just open the encrypted PDF in any PDF reader and type the
    password - that is lossless and needs no tool. This script is for producing a
    shareable plaintext file or batch-decrypting.

    It uses **qpdf** if available (lossless), otherwise falls back to Ghostscript
    (works, but re-emits the PDF). Install qpdf for lossless output:
        winget install qpdf.qpdf

    Usage:
      * Double-click decode-pdf.bat and pick file(s), or drag PDFs onto it.
      * powershell -File decode-pdf.ps1 -Files a.pdf,b.pdf [-Password xxx] [-OutDir C:\out]
    Password source (first found): -Password  ->  $env:VCP_PDF_PASSWORD  ->  prompt.
#>
param(
    [string[]]$Files,
    [string]$Password,
    [string]$OutDir
)

Add-Type -AssemblyName System.Windows.Forms | Out-Null

function Find-Exe {
    param([string]$Name, [string[]]$Roots)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($root in $Roots) {
        if ($root -and (Test-Path $root)) {
            $hit = Get-ChildItem $root -Recurse -Filter $Name -ErrorAction SilentlyContinue |
                   Sort-Object FullName -Descending | Select-Object -First 1
            if ($hit) { return $hit.FullName }
        }
    }
    return $null
}

$qpdf = Find-Exe 'qpdf.exe' @("$env:ProgramFiles\qpdf", "${env:ProgramFiles(x86)}\qpdf",
                              "$env:LOCALAPPDATA\Microsoft\WinGet\Packages")
$gs   = Find-Exe 'gswin64c.exe' @("$env:ProgramFiles\gs", "${env:ProgramFiles(x86)}\gs")
if (-not $qpdf -and -not $gs) {
    [System.Windows.Forms.MessageBox]::Show(
        "Neither qpdf nor Ghostscript was found. Install qpdf (winget install qpdf.qpdf), " +
        "or just open the PDF in a reader and enter the password.",
        'Decode PDF', 'OK', 'Error') | Out-Null
    return
}

if (-not $Files -or $Files.Count -eq 0) {
    $ofd = New-Object System.Windows.Forms.OpenFileDialog
    $ofd.Multiselect = $true
    $ofd.Filter = 'PDF files (*.pdf)|*.pdf|All files (*.*)|*.*'
    $ofd.Title = 'Select the encrypted PDF(s) to decode'
    if ($ofd.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return }
    $Files = $ofd.FileNames
}

if (-not $Password) { $Password = $env:VCP_PDF_PASSWORD }
if (-not $Password) {
    $sec = Read-Host -AsSecureString 'Enter the PDF password'
    $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
if (-not $Password) { Write-Host 'No password provided; aborting.'; return }

$engine = if ($qpdf) { "qpdf (lossless)" } else { "Ghostscript (re-emits)" }
Write-Host "Decoding with $engine ..."

foreach ($f in $Files) {
    if (-not (Test-Path $f)) { Write-Host "  skip (not found): $f"; continue }
    $dir = if ($OutDir) { $OutDir } else { Split-Path -Parent (Resolve-Path $f) }
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $out = Join-Path $dir ([IO.Path]::GetFileNameWithoutExtension($f) + '-decrypted.pdf')

    if ($qpdf) {
        & $qpdf --decrypt ("--password=" + $Password) $f $out
        $ok = ($LASTEXITCODE -in 0, 3)   # 3 = warnings but succeeded
    } else {
        & $gs -dNOPAUSE -dBATCH -dSAFER -dQUIET ("-sPDFPassword=" + $Password) `
            -sDEVICE=pdfwrite ("-sOutputFile=" + $out) $f
        $ok = ($LASTEXITCODE -eq 0)
    }
    if ($ok -and (Test-Path $out)) { Write-Host "  decoded -> $out" }
    else { Write-Host "  FAILED (wrong password or not encrypted?): $f" }
}

Write-Host ''
Write-Host 'Done.'
if ($Host.Name -eq 'ConsoleHost') { Read-Host 'Press Enter to close' | Out-Null }
