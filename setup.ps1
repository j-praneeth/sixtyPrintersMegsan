<#
    Virtual Cloud Printer - setup / manager
    =======================================
    Creates Windows virtual printers that convert every print job to a PDF and
    POST it (multipart/form-data: docname + file) to a per-printer HTTPS URL.

    It wires together:
      * mfilemon (or clawmon) print-port monitor   -> captures the job
      * MS Publisher (v3 pscript5) PostScript driver -> emits PostScript
      * Ghostscript                                -> PostScript -> PDF
      * uv-managed Python + upload.py              -> converts & uploads

    Usage (normally launched by the .bat wrappers, which handle elevation):
      powershell -ExecutionPolicy Bypass -File setup.ps1 -Action install
      powershell -ExecutionPolicy Bypass -File setup.ps1 -Action add     -PrinterName "Invoices" -Url "https://.../invoices"
      powershell -ExecutionPolicy Bypass -File setup.ps1 -Action changepassword -PrinterName "Invoices"
      powershell -ExecutionPolicy Bypass -File setup.ps1 -Action viewpassword   -PrinterName "Invoices"
      powershell -ExecutionPolicy Bypass -File setup.ps1 -Action setsuperadmin
      powershell -ExecutionPolicy Bypass -File setup.ps1 -Action uninstall
      powershell -ExecutionPolicy Bypass -File setup.ps1 -Action status

    install/add with NO -Url enters hub mode: the printer is enrolled as a device
    on the LAN hub (see ARCHITECTURE.md section 9) and its per-printer ingest
    URL + token land in config.json. Passing -Url keeps the legacy direct-URL
    behavior exactly as before (no enrollment, no device fields).
#>

[CmdletBinding()]
param(
    [ValidateSet('install', 'add', 'uninstall', 'status', 'fixqueue', 'setpassword',
                 'changepassword', 'viewpassword', 'setsuperadmin')]
    [string]$Action = 'install',
    [string]$PrinterName = '',
    [string]$Url = '',
    [string]$DeviceName = '',   # hub mode: unique device name (e.g. gcms-01)
    [string]$Department = '',   # hub mode: department the printer belongs to
    [string]$Equipment = '',    # hub mode: equipment/instrument name (e.g. GCMS / LCMS)
    [string]$Password = '',     # changepassword / hub install: skip the interactive prompt
    [switch]$NoPassword,        # hub install: explicitly NO pdf password (skip the prompt)
    [string]$PasswordFile = '', # read the password from this file (deleted after) so it
                                # never appears on the command line - used by the GUI wizard
    [string]$HubUrl = '',       # hub mode: hub base URL (e.g. http://192.168.1.172:8000)
    [string]$EnrollKey = '',    # hub mode: X-Enroll-Key from the hub console
    [string]$CatalogShareDir = '',  # hub mode: skip the catalog-share prompt (scripted installs)
    [switch]$RemoveTools    # uninstall: also remove the shared port monitor
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 may default to TLS 1.0, which SourceForge/GitHub reject
# (yielding failed or HTML-interstitial downloads). Force TLS 1.2.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

# --------------------------------------------------------------------------- #
# OS / PowerShell compatibility (Windows 7 SP1 / 10 / 11)
# --------------------------------------------------------------------------- #
# Windows 7 ships PowerShell 2.0, which lacks ConvertFrom-Json / Invoke-WebRequest
# and the .NET crypto types used below. WMF 5.1 (+ .NET Framework 4.6+) is a hard
# prerequisite there; this check fires before anything version-sensitive runs.
if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-Host 'ERROR: Windows PowerShell 5.1 is required.' -ForegroundColor Red
    Write-Host 'On Windows 7 SP1: install .NET Framework 4.8 (4.7.2 minimum) then WMF 5.1, and re-run.' -ForegroundColor Yellow
    Write-Host '  (.NET 4.7.2+ is required for the PBKDF2-SHA256 super-admin hash.)' -ForegroundColor Yellow
    Write-Host '  WMF 5.1: https://www.microsoft.com/download/details.aspx?id=54616' -ForegroundColor Yellow
    exit 1
}

# NT major < 10 => Windows 7/8.x ("legacy"):
#   * the PrintManagement cmdlets (Add-/Get-/Set-/Remove-Printer, Win8+) do not
#     exist on Win7 - printer/driver management is shimmed via WMI + printui;
#   * uv and CPython 3.12 both require Win10+ - the Python 3.8.10 EMBEDDABLE
#     package is provisioned instead (3.8 is the last CPython that runs on Win7).
# powershell.exe is manifested for Win10, so OSVersion reports the real version.
# VCP_COMPAT_FORCE_LEGACY=1 forces the legacy paths on a modern OS (testing aid).
$script:IsLegacyOs = ([Environment]::OSVersion.Version.Major -lt 10) -or ($env:VCP_COMPAT_FORCE_LEGACY -eq '1')
# Capability probe, not an OS sniff: Win8.x has the cmdlets and should use them.
$script:HasPrintCmdlets = ($env:VCP_COMPAT_FORCE_LEGACY -ne '1') -and
                          [bool](Get-Command Add-Printer -ErrorAction SilentlyContinue)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
$Base          = Join-Path $env:ProgramData 'VirtualCloudPrinter'
$SpoolDir      = Join-Path $Base 'spool'
$FailedDir     = Join-Path $Base 'failed'
$IdsDir        = Join-Path $Base 'ids'
$VenvDir       = Join-Path $Base 'venv'
$PythonDir     = Join-Path $Base 'python'
$LegacyPyDir   = Join-Path $PythonDir 'py38-embed'  # Win7: Python 3.8 embeddable
$QpdfDir       = Join-Path $Base 'qpdf'    # private qpdf (AES-256 PDF encryption)
$ConfigPath    = Join-Path $Base 'config.json'
$UploadScript  = Join-Path $Base 'upload.py'
$PythonwPath   = if ($IsLegacyOs) { Join-Path $LegacyPyDir 'pythonw.exe' }
                 else             { Join-Path $VenvDir 'Scripts\pythonw.exe' }
# -P (keep the script dir off sys.path) is a Python 3.11+ flag; on the legacy
# 3.8 interpreter -I (isolated mode, 3.4+) is the compatible superset. Both are
# interpreter flags, so upload.py's argv mapping is identical either way.
$PyIsoFlag     = if ($IsLegacyOs) { '-I' } else { '-P' }

$PortName      = 'VirtualCloudPrinter:'
# v3 PostScript driver. The inbox "Microsoft PS Class Driver" is a v4 *class*
# driver, and v4 drivers CANNOT be attached to a port owned by a third-party port
# monitor (mfilemon/clawmon): the spooler rejects Add-/Set-Printer with
# ERROR_NOT_SUPPORTED (0x80070032). The inbox "MS Publisher" models are v3
# pscript5 (PS5UI.DLL) PostScript drivers that bind cleanly and still emit
# PostScript for Ghostscript to convert. Tried in order; first available wins.
$DriverCandidates = @('MS Publisher Color Printer', 'MS Publisher Imagesetter')
$MonitorsKey   = 'SYSTEM\CurrentControlSet\Control\Print\Monitors'
$ClawmonName   = 'clawmon printer port monitor'
$MfilemonName  = 'Multi File Port Monitor'
# mfilemon's author publishes signed installers as GitHub release assets, which
# are far more reliable to fetch than SourceForge (whose /download endpoint
# serves an HTML interstitial / 403s to non-browser clients). GitHub is primary;
# SourceForge is kept only as a manual-download hint.
$MfilemonAssets = @(
    'https://github.com/lomo74/mfilemon/releases/download/v1.6.1/mfilemon-setup.exe',
    'https://github.com/lomo74/mfilemon/releases/download/v1.6.0/mfilemon-setup.exe'
)
$MfilemonUrl   = 'https://github.com/lomo74/mfilemon/releases'

$ScriptDir     = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$VendorDir     = Join-Path $ScriptDir 'vendor'

# Hub-enrollment state filled in by Read-PrinterAndUrl (hub mode only).
# NB: $CatalogShareDir is a script param (same script scope) - do NOT re-init it
# here or the -CatalogShareDir argument would be wiped after binding.
$script:HubMode         = $false
$script:HubToken        = ''
$script:PdfPassword     = ''
$DefaultHubUrl          = 'http://192.168.1.172:8000'
# Device names must be safe as a filesystem path segment on the hub (§9/§10).
$DeviceNameRegex        = '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
# Factory super-admin credential (ARCHITECTURE.md §5). ONLY the PBKDF2-SHA256
# record of the factory password is embedded - the plaintext appears nowhere in
# this repo. It was precomputed as PBKDF2-SHA256(password, salt, 200000) with the
# fixed salt below; Test-SuperAdminPassword verifies against it exactly like a
# rotated one. The factory password is distributed to deployment admins
# out-of-band; rotate it on every install with set-super-admin.bat.
$InitialSuperAdminHash  = [PSCustomObject]@{
    algo       = 'pbkdf2-sha256'
    iterations = 200000
    salt       = 'nUpOf4LknZoXD/0TXzfgdg=='
    hash       = 'biMBPndJ1a2L0NXr3i1TbFdW4i1tK1P6px4RsaXxaZA='
}

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
function Write-Step { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "    [ok] $m" -ForegroundColor Green }
function Write-Info { param($m) Write-Host "    $m" -ForegroundColor Gray }
function Write-Warn2{ param($m) Write-Host "    [!] $m" -ForegroundColor Yellow }

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script must run as Administrator. Right-click install.bat -> Run as administrator.'
    }
}

function Refresh-Path {
    $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $u = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = ($m, $u | Where-Object { $_ }) -join ';'
}

function Test-Monitor { param($name) Test-Path "Registry::HKEY_LOCAL_MACHINE\$MonitorsKey\$name" }

function Grant-JobDirAccess {
    # The spooler runs mfilemon's port I/O (stat spool\, create the .ps) while
    # IMPERSONATING the print-submitting user (print-to-file semantics, per the
    # Windows DDK / CVE-2020-1048 write-up). With $Base locked to SYSTEM+Admins that
    # user is denied -> GetFileAttributes(spool) fails -> mfilemon logs "can't create
    # output directory (183)" and the job dies before upload.py runs. Grant that user
    # JUST enough on spool\, with per-file ownership so users can't read each other's
    # in-flight documents (important for the ~100-user target):
    #   *S-1-5-11 (Authenticated Users): (CI)(RX,W) on the folder + subfolders ONLY -
    #     stat/list/traverse + create a file; NOT (OI), so no ACE lands on the .ps
    #     files themselves. (RX includes read-attributes, so DirectoryExists() ->
    #     TRUE and RecursiveCreateFolder returns SUCCESS without calling CreateDir;
    #     bypass-traverse-checking, on for Everyone by default, covers the locked
    #     $Base parent.)
    #   *S-1-3-0 (CREATOR OWNER): (OI)(CI)F - each creator gets full control of the
    #     file IT creates (needed to write the .ps), and nothing on others' files.
    # SYSTEM/Admins keep Full (inherited from $Base) so upload.py (SYSTEM) reads and
    # deletes every job file. failed\ is written by upload.py AS SYSTEM (never by the
    # impersonated user), so it stays SYSTEM+Admins-only - opening it would leak other
    # users' failed-job PDFs. This preserves the sibling-module hardening: spool\ is
    # not on upload.py's sys.path and holds no importable code.
    if (Test-Path $SpoolDir) {
        & icacls "$SpoolDir" /grant "*S-1-5-11:(CI)(RX,W)" "*S-1-3-0:(OI)(CI)F" | Out-Null
    }

    # The spooler ALSO launches the UserCommand while impersonating that same user,
    # and CreateProcess must OPEN venv\Scripts\pythonw.exe to start it. That
    # interpreter lives under the locked $Base, so the impersonated user is denied
    # and the launch silently fails: the .ps gets written but upload.py never runs
    # (no log.txt entry, .ps left in spool, no print-queue error). Restart-
    # reprocessed jobs run as SYSTEM and DO launch, which is why prints only landed
    # after a spooler restart. Grant the print context READ+EXECUTE (never write) on
    # the interpreter so the launch works. RX cannot plant an importable module, so
    # the sibling-module escalation stays closed; config.json, upload.py and log.txt
    # remain locked (they are at the $Base root, not granted here), so auth headers
    # stay unreadable. The launched upload.py still runs as SYSTEM (CreateProcess
    # uses the spooler's primary token), so it reads config.json / writes log.txt.
    foreach ($d in @($VenvDir, $PythonDir)) {
        if (Test-Path $d) { & icacls "$d" /grant "*S-1-5-11:(OI)(CI)(RX)" | Out-Null }
    }

    # prompt-id.ps1 (the per-print registration dialog) is opened by the user-session
    # PowerShell that upload.py launches, so the print user needs READ on it - but it
    # lives at the locked $Base root. Grant RX on just that one file (read+execute,
    # never write, so it cannot be tampered with). config.json / upload.py / log.txt
    # at the root stay locked.
    $promptScript = Join-Path $Base 'prompt-id.ps1'
    if (Test-Path $promptScript) { & icacls "$promptScript" /grant "*S-1-5-11:(RX)" | Out-Null }

    # Undo any earlier over-broad grant on failed\ (confidentiality).
    if (Test-Path $FailedDir) { & icacls "$FailedDir" /remove "*S-1-5-11" | Out-Null }
}

function New-DesktopShortcut {
    # Create a public-desktop .lnk pointing at one of our .bat helpers.
    param([string]$Name, [string]$BatFile, [string]$Description)
    $target = Join-Path $ScriptDir $BatFile
    if (-not (Test-Path $target)) { return }
    try {
        $lnk = Join-Path $env:PUBLIC "Desktop\$Name.lnk"
        $ws = New-Object -ComObject WScript.Shell
        $sc = $ws.CreateShortcut($lnk)
        $sc.TargetPath = $target
        $sc.WorkingDirectory = $ScriptDir
        $sc.Description = $Description
        $sc.Save()
        Write-Ok "Desktop shortcut created: '$Name'"
    } catch { Write-Warn2 "Could not create desktop shortcut '$Name': $($_.Exception.Message)" }
}

function Resolve-Exe {
    # Return a full path to an executable if reachable via PATH or the given candidates.
    param([string]$Name, [string[]]$Candidates = @())
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($c in $Candidates) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}

function Test-IsExe {
    # True if the file exists, is non-trivial, and starts with the PE 'MZ' magic.
    # SourceForge/CDNs sometimes serve an HTML interstitial page instead of the
    # binary; running that yields "the file is corrupted and unreadable".
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $false }
    try {
        if ((Get-Item $Path).Length -lt 10KB) { return $false }
        $fs = [System.IO.File]::OpenRead($Path)
        try {
            $b = New-Object byte[] 2
            [void]$fs.Read($b, 0, 2)
            return ($b[0] -eq 0x4D -and $b[1] -eq 0x5A)   # 'M','Z'
        } finally { $fs.Close() }
    } catch { return $false }
}

function Get-InstallerExe {
    # Download an installer to $Dest, following redirects, and verify it is a real
    # PE. Tries each URL in turn; returns $true on the first valid download.
    param([string]$Dest, [string[]]$Urls)
    $ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) VirtualCloudPrinter'
    foreach ($u in $Urls) {
        try {
            if (Test-Path $Dest) { Remove-Item $Dest -Force -ErrorAction SilentlyContinue }
            Write-Info "Downloading from $u"
            Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $Dest `
                -Headers @{ 'User-Agent' = $ua } -MaximumRedirection 10
            if (Test-IsExe $Dest) { return $true }
            Write-Warn2 'Downloaded file is not a valid executable (got an HTML page or truncated file); trying next source.'
        } catch { Write-Warn2 "Download failed: $($_.Exception.Message)" }
    }
    return $false
}

function Assert-NoProtectedPrint {
    # Windows 11 "Windows Protected Print" (WPP) restricts printing to the inbox
    # IPP class driver and BLOCKS third-party port monitors - mfilemon/clawmon
    # cannot load under it, so nothing in this toolkit can work. Fail fast with
    # instructions instead of a cryptic spooler error later. Policy and Settings
    # both land under a WPP key with WindowsProtectedPrintMode=1.
    foreach ($k in @('HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Printers\WPP',
                     'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Print\WPP')) {
        $v = $null
        try { $v = (Get-ItemProperty -Path $k -ErrorAction SilentlyContinue).WindowsProtectedPrintMode } catch {}
        if ($v -eq 1) {
            throw ("Windows Protected Print mode is ENABLED ($k) - it blocks third-party " +
                   "print-port monitors, so this toolkit cannot install. Turn it off in " +
                   "Settings > Bluetooth & devices > Printers & scanners > Windows protected " +
                   "print mode (or via Group Policy), then re-run.")
        }
    }
}

# --------------------------------------------------------------------------- #
# Printer management shims
# The PrintManagement cmdlets (Add-/Get-/Set-/Remove-Printer, Add-PrinterDriver)
# require Windows 8+. On Windows 7 the same operations go through WMI
# (Win32_Printer / Win32_PrinterDriver) and printui.dll for driver installs.
# All call sites go through these shims; never call the cmdlets directly.
# --------------------------------------------------------------------------- #
function Get-VcpPrinters {
    # Objects exposing Name / DriverName / PortName / PrinterStatus (the WMI
    # class uses the same property names as Get-Printer's output).
    if ($script:HasPrintCmdlets) { return @(Get-Printer -ErrorAction SilentlyContinue) }
    return @(Get-WmiObject -Class Win32_Printer -ErrorAction SilentlyContinue)
}

function Remove-VcpPrinter {
    param([string]$Name)
    if ($script:HasPrintCmdlets) { Remove-Printer -Name $Name -ErrorAction SilentlyContinue; return }
    Get-WmiObject -Class Win32_Printer -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq $Name } |
        ForEach-Object { try { [void]$_.Delete() } catch { Write-Warn2 "Could not remove '$Name': $($_.Exception.Message)" } }
}

function Test-VcpPrinterDriver {
    param([string]$DriverName)
    if ($script:HasPrintCmdlets) { return [bool](Get-PrinterDriver -Name $DriverName -ErrorAction SilentlyContinue) }
    # Win32_PrinterDriver names are 'Driver,Version,Environment'.
    return [bool](Get-WmiObject -Class Win32_PrinterDriver -ErrorAction SilentlyContinue |
                  Where-Object { ($_.Name -split ',')[0] -eq $DriverName })
}

function Add-VcpPrinterDriver {
    param([string]$DriverName)
    if ($script:HasPrintCmdlets) { Add-PrinterDriver -Name $DriverName -ErrorAction Stop; return }
    # Win7: install the inbox driver from its INF via printui. rundll32 exit
    # codes are meaningless, so success is re-checked via WMI after each try.
    # Win10/11 split the inbox printer INFs (prnge001.inf); Win7 bundles every
    # inbox printer driver in ntprint.inf - try both.
    foreach ($inf in @('prnge001.inf', 'ntprint.inf')) {
        $infPath = Join-Path $env:WINDIR "inf\$inf"
        if (-not (Test-Path $infPath)) { continue }
        Start-Process -FilePath (Join-Path $env:WINDIR 'System32\rundll32.exe') -Wait -ArgumentList (
            'printui.dll,PrintUIEntry /ia /q /m "{0}" /f "{1}"' -f $DriverName, $infPath)
        if (Test-VcpPrinterDriver $DriverName) { return }
    }
    throw "Driver '$DriverName' could not be installed via printui (tried prnge001.inf, ntprint.inf)."
}

function Add-VcpPrinter {
    param([string]$Name, [string]$Driver)
    if ($script:HasPrintCmdlets) { Add-Printer -Name $Name -DriverName $Driver -PortName $PortName; return }
    # WMI Put() calls AddPrinter under the hood - synchronous, real errors.
    $p = ([wmiclass]'\\.\root\cimv2:Win32_Printer').CreateInstance()
    $p.DeviceID   = $Name
    $p.DriverName = $Driver
    $p.PortName   = $PortName
    [void]$p.Put()
}

function Set-VcpPrinter {
    param([string]$Name, [string]$Driver)
    if ($script:HasPrintCmdlets) {
        Set-Printer -Name $Name -DriverName $Driver -PortName $PortName -ErrorAction SilentlyContinue
        return
    }
    Get-WmiObject -Class Win32_Printer -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq $Name } |
        ForEach-Object {
            $_.DriverName = $Driver
            $_.PortName   = $PortName
            try { [void]$_.Put() } catch { Write-Warn2 "Could not retarget '$Name': $($_.Exception.Message)" }
        }
}

# --------------------------------------------------------------------------- #
# DPAPI + super-admin + hub enrollment (ARCHITECTURE.md sections 4, 5, 9)
# --------------------------------------------------------------------------- #
function ConvertFrom-SecureStringPlain {
    # SecureString -> plain string (Read-Host -AsSecureString keeps the password
    # off the console; we still need the plaintext to DPAPI-protect / verify it).
    param([SecureString]$Secure)
    if (-not $Secure) { return '' }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Get-VcpEntropy {
    # DPAPI optional entropy: UTF-8 bytes of the literal 'VCP-DPAPI-v1' (exact, no
    # BOM). upload.py's CryptUnprotectData MUST use the same bytes - do not change.
    return [System.Text.Encoding]::UTF8.GetBytes('VCP-DPAPI-v1')
}

function Protect-VcpPassword {
    # Plain password -> base64(DPAPI blob), LocalMachine scope so the SYSTEM
    # process the spooler launches (upload.py) can decrypt it.
    param([string]$Plain)
    Add-Type -AssemblyName System.Security
    $blob = [Security.Cryptography.ProtectedData]::Protect(
        [System.Text.Encoding]::UTF8.GetBytes($Plain), (Get-VcpEntropy),
        [Security.Cryptography.DataProtectionScope]::LocalMachine)
    return [Convert]::ToBase64String($blob)
}

function Unprotect-VcpPassword {
    # base64(DPAPI blob) -> plain password (LocalMachine scope, same entropy).
    param([string]$Base64)
    Add-Type -AssemblyName System.Security
    $plain = [Security.Cryptography.ProtectedData]::Unprotect(
        [Convert]::FromBase64String($Base64), (Get-VcpEntropy),
        [Security.Cryptography.DataProtectionScope]::LocalMachine)
    return [System.Text.Encoding]::UTF8.GetString($plain)
}

function Get-PasswordHash {
    # PBKDF2-SHA256 record for config.json 'super_admin' - never the plaintext.
    param([string]$Plain)
    $salt = New-Object byte[] 16
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($salt) } finally { $rng.Dispose() }
    $kdf = New-Object Security.Cryptography.Rfc2898DeriveBytes(
        $Plain, $salt, 200000, [Security.Cryptography.HashAlgorithmName]::SHA256)
    try { $hash = $kdf.GetBytes(32) } finally { $kdf.Dispose() }
    return [PSCustomObject]@{
        algo       = 'pbkdf2-sha256'
        iterations = 200000
        salt       = [Convert]::ToBase64String($salt)
        hash       = [Convert]::ToBase64String($hash)
    }
}

function Test-SuperAdminPassword {
    param([string]$Plain, $Stored)
    if (-not $Stored -or -not $Stored.salt -or -not $Stored.hash) { return $false }
    $kdf = New-Object Security.Cryptography.Rfc2898DeriveBytes(
        $Plain, [Convert]::FromBase64String($Stored.salt), [int]$Stored.iterations,
        [Security.Cryptography.HashAlgorithmName]::SHA256)
    try { $calc = $kdf.GetBytes(32) } finally { $kdf.Dispose() }
    $expect = [Convert]::FromBase64String($Stored.hash)
    if ($calc.Length -ne $expect.Length) { return $false }
    # Constant-time compare (no early exit on the first differing byte).
    $diff = 0
    for ($i = 0; $i -lt $calc.Length; $i++) { $diff = $diff -bor ($calc[$i] -bxor $expect[$i]) }
    return ($diff -eq 0)
}

function Ensure-SuperAdmin {
    # Seed config 'super_admin' with the factory PBKDF2 record if absent (the
    # plaintext is never stored or embedded anywhere). Returns $true if it
    # seeded (the caller's config write persists it).
    param($Cfg)
    if ($Cfg.PSObject.Properties['super_admin'] -and $Cfg.super_admin -and $Cfg.super_admin.hash) {
        return $false
    }
    $Cfg | Add-Member -NotePropertyName super_admin -NotePropertyValue $InitialSuperAdminHash -Force
    Write-Warn2 'Super-admin password seeded with the factory default - rotate it with set-super-admin.bat.'
    return $true
}

function Read-SuperAdminVerified {
    # Prompt for the super-admin password (masked) and verify against the stored
    # PBKDF2 hash. Throws after 3 failed attempts - callers must not proceed.
    param($Cfg)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $pw = ConvertFrom-SecureStringPlain (Read-Host -AsSecureString 'Super admin password')
        if (Test-SuperAdminPassword $pw $Cfg.super_admin) { return }
        Write-Warn2 "Wrong super admin password (attempt $attempt of 3)."
    }
    throw 'Super admin verification failed (3 wrong attempts).'
}

function Get-HubDepartments {
    # GET <hub>/departments (X-Enroll-Key). Best-effort: the interactive prompt
    # just shows the list as a hint, so any failure returns @() rather than throws.
    param([string]$BaseUrl, [string]$Key)
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Method Get `
            -Uri (($BaseUrl.TrimEnd('/')) + '/departments') `
            -Headers @{ 'X-Enroll-Key' = $Key } -TimeoutSec 15
        $obj = $resp.Content | ConvertFrom-Json
        if ($obj.departments) { return @($obj.departments) }
    } catch { Write-Warn2 "Could not fetch departments from the hub: $($_.Exception.Message)" }
    return @()
}

function Get-HubEquipment {
    # GET <hub>/equipment?department=<dept> (X-Enroll-Key). Best-effort hint list.
    param([string]$BaseUrl, [string]$Key, [string]$Department)
    try {
        $uri = ($BaseUrl.TrimEnd('/')) + '/equipment'
        if ($Department) { $uri += '?department=' + [uri]::EscapeDataString($Department) }
        $resp = Invoke-WebRequest -UseBasicParsing -Method Get -Uri $uri `
            -Headers @{ 'X-Enroll-Key' = $Key } -TimeoutSec 15
        $obj = $resp.Content | ConvertFrom-Json
        if ($obj.equipment) { return @($obj.equipment) }
    } catch { Write-Warn2 "Could not fetch equipment from the hub: $($_.Exception.Message)" }
    return @()
}

function Invoke-HubEnrollment {
    # POST <hub>/admin/devices (X-Enroll-Key) -> {token, ingest_url, ...}.
    # 409 = device_name already taken (uniqueness is case-insensitive on the hub).
    # PdfPassword is stored (encrypted) in Supabase printer_devices by the hub.
    param([string]$BaseUrl, [string]$Key, [string]$DevName, [string]$Department,
          [string]$Equipment, [string]$Printer, [string]$PdfPassword)
    $body = @{
        device_name     = $DevName
        department_name = $Department
        equipment_name  = $Equipment
        printer_name    = $Printer
        hostname        = $env:COMPUTERNAME
        pdf_password    = $PdfPassword
    } | ConvertTo-Json -Compress
    $uri = ($BaseUrl.TrimEnd('/')) + '/admin/devices'
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $uri `
            -Headers @{ 'X-Enroll-Key' = $Key } -ContentType 'application/json' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 30
    } catch {
        $status = 0
        try { $status = [int]$_.Exception.Response.StatusCode } catch {}
        if ($status -eq 409) {
            throw "Device name '$DevName' is already in use on the hub - pick a unique name and re-run."
        }
        throw "Hub enrollment failed ($uri): $($_.Exception.Message)"
    }
    $obj = $resp.Content | ConvertFrom-Json
    if (-not $obj.token -or -not $obj.ingest_url) {
        throw 'Hub enrollment returned an unexpected response (no token/ingest_url).'
    }
    return $obj
}

# --------------------------------------------------------------------------- #
# Dependency installers
# --------------------------------------------------------------------------- #
function Get-Uv {
    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe')
    )
    Refresh-Path
    $uv = Resolve-Exe 'uv' $candidates
    if ($uv) { return $uv }

    Write-Info 'uv not found - installing...'
    # winget is a native exe: a failed install returns non-zero but does NOT throw,
    # so never assume success - always re-check and fall through if still missing.
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            winget install --id astral-sh.uv -e --silent `
                --accept-source-agreements --accept-package-agreements | Out-Null
        } catch { Write-Warn2 "winget install of uv failed: $($_.Exception.Message)" }
        Refresh-Path
        $uv = Resolve-Exe 'uv' $candidates
        if ($uv) { return $uv }
    }

    Write-Info 'Falling back to the standalone uv installer (astral.sh)...'
    powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
    Refresh-Path
    $uv = Resolve-Exe 'uv' $candidates
    if (-not $uv) { throw 'uv could not be installed. Install it manually from https://docs.astral.sh/uv/ and re-run.' }
    return $uv
}

function Ensure-LegacyPython {
    # Windows 7 path: uv and CPython 3.12 both require Win10+, so provision the
    # Python 3.8.10 EMBEDDABLE package instead (3.8 is the last CPython that
    # runs on Win7; upload.py is stdlib-only and 3.8-compatible, so no venv or
    # pip is needed). The embeddable zip is self-contained and relocatable, and
    # its python38._pth pins sys.path so the script's directory is never
    # importable - the same sibling-module hardening -P provides on 3.11+.
    # Lives under $PythonDir so Grant-JobDirAccess's RX grant covers it.
    $pyw = Join-Path $LegacyPyDir 'pythonw.exe'
    if (-not (Test-Path $pyw)) {
        New-Item -ItemType Directory -Force -Path $LegacyPyDir | Out-Null
        $zip = Join-Path $env:TEMP 'python-embed.zip'
        $vendorZip = Get-ChildItem $VendorDir -Filter 'python-3.8*-embed-amd64.zip' -ErrorAction SilentlyContinue |
                     Select-Object -First 1
        if ($vendorZip) {
            Write-Info "Using vendored Python: $($vendorZip.Name)"
            Copy-Item $vendorZip.FullName $zip -Force
        } else {
            Write-Info 'Downloading the Python 3.8.10 embeddable package (python.org)...'
            Invoke-WebRequest -UseBasicParsing -OutFile $zip -MaximumRedirection 10 `
                -Uri 'https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-amd64.zip'
        }
        try { Expand-Archive -Path $zip -DestinationPath $LegacyPyDir -Force }
        finally { Remove-Item $zip -Force -ErrorAction SilentlyContinue }
    }
    if (-not (Test-Path $pyw)) {
        throw ("Python 3.8 embeddable install failed under $LegacyPyDir. Download " +
               "python-3.8.10-embed-amd64.zip from python.org into vendor\ and re-run.")
    }
    # Same smoke test as the uv path: catch a corrupt/truncated interpreter at
    # install time, not as silent per-print failures. upload.py needs all of these.
    $py = Join-Path $LegacyPyDir 'python.exe'
    & $py -I -c "import ssl, json, io, re, mimetypes, subprocess, ctypes, urllib.request" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw ("The provisioned Python 3.8 failed a smoke test (exit $LASTEXITCODE) - the " +
               "interpreter is corrupt. Delete '$LegacyPyDir' and re-run install.")
    }
    return $pyw
}

function Find-GsExe {
    # Return the newest Ghostscript console exe, choosing by NUMERIC version
    # (so gs10.x beats gs9.x, which a plain string sort gets wrong).
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $root) { continue }
        $gsDir = Join-Path $root 'gs'
        if (-not (Test-Path $gsDir)) { continue }
        $found = Get-ChildItem -Path $gsDir -Recurse -Filter 'gswin*c.exe' -ErrorAction SilentlyContinue |
            Sort-Object { try { [version](($_.Directory.Parent.Name) -replace '[^0-9.]', '') } catch { [version]'0.0' } } -Descending |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

function Get-Ghostscript {
    $gs = Find-GsExe
    if ($gs) { return $gs }

    Write-Info 'Ghostscript not found - installing...'
    if ($script:IsLegacyOs) {
        # Win7: no winget, and current Ghostscript 10.x builds are not tested on
        # Win7 - pin 9.56.1, the last release broadly verified there. Prefer an
        # offline installer dropped into vendor\ (Win7 clients are often offline).
        $inst = Join-Path $env:TEMP 'gs-setup.exe'
        $vendorGs = Get-ChildItem $VendorDir -Filter 'gs*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($vendorGs -and (Test-IsExe $vendorGs.FullName)) {
            Write-Info "Using vendored Ghostscript installer: $($vendorGs.Name)"
            Copy-Item $vendorGs.FullName $inst -Force
        } elseif (-not (Get-InstallerExe -Dest $inst -Urls @(
                'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs9561/gs9561w64.exe'))) {
            $inst = $null
        }
        if ($inst) {
            Write-Info 'Running the Ghostscript 9.56.1 installer /S ...'
            Start-Process -FilePath $inst -ArgumentList '/S' -Wait
        }
        $gs = Find-GsExe
        if (-not $gs) {
            throw ('Ghostscript could not be installed. Download gs9561w64.exe ' +
                   '(https://ghostscript.com/releases/) into vendor\ and re-run.')
        }
        return $gs
    }
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            winget install --id ArtifexSoftware.GhostScript -e --silent `
                --accept-source-agreements --accept-package-agreements | Out-Null
        } catch { Write-Warn2 "winget install of Ghostscript failed: $($_.Exception.Message)" }
        $gs = Find-GsExe
        if ($gs) { return $gs }
    }

    # Fallback: download the official Ghostscript installer from GitHub.
    Write-Info 'Downloading Ghostscript installer from GitHub...'
    try {
        $rel = Invoke-RestMethod -UseBasicParsing `
            -Uri 'https://api.github.com/repos/ArtifexSoftware/ghostpdl-downloads/releases/latest' `
            -Headers @{ 'User-Agent' = 'VirtualCloudPrinter' }
        $asset = $rel.assets | Where-Object { $_.name -match 'gs\d+w64\.exe$' } | Select-Object -First 1
        if ($asset) {
            $tmp = Join-Path $env:TEMP $asset.name
            Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $tmp
            Write-Info "Running $($asset.name) /S ..."
            Start-Process -FilePath $tmp -ArgumentList '/S' -Wait
        }
    } catch { Write-Warn2 "Ghostscript download failed: $($_.Exception.Message)" }

    $gs = Find-GsExe
    if (-not $gs) { throw 'Ghostscript could not be installed. Install it from https://ghostscript.com/releases/ and re-run.' }
    return $gs
}

function Find-QpdfExe {
    # Prefer the private copy under $Base\qpdf, then PATH.
    if (Test-Path $QpdfDir) {
        $exe = Get-ChildItem $QpdfDir -Recurse -Filter 'qpdf.exe' -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($exe) { return $exe.FullName }
    }
    $cmd = Get-Command qpdf.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-Qpdf {
    # Provide qpdf for AES-256 PDF encryption. Best-effort: encryption is opt-in, so
    # a failure here is a warning (not fatal) - upload.py fails closed later if a
    # job needs encryption and qpdf is absent. Order: existing -> vendor zip ->
    # GitHub release zip -> winget. Extracted to a PRIVATE $Base\qpdf so it works
    # regardless of PATH and for the SYSTEM print process.
    $q = Find-QpdfExe
    if ($q) { return $q }

    New-Item -ItemType Directory -Force -Path $QpdfDir | Out-Null
    $zip = Join-Path $env:TEMP 'qpdf.zip'
    $vendorZip = Get-ChildItem $VendorDir -Filter 'qpdf-*.zip' -ErrorAction SilentlyContinue | Select-Object -First 1

    $extracted = $false
    if ($vendorZip) {
        try {
            Write-Info "Using vendored qpdf: $($vendorZip.Name)"
            Expand-Archive -Path $vendorZip.FullName -DestinationPath $QpdfDir -Force
            $extracted = $true
        } catch { Write-Warn2 "Vendored qpdf extract failed: $($_.Exception.Message)" }
    }
    if (-not $extracted) {
        try {
            Write-Info 'Downloading qpdf (AES-256) from GitHub releases...'
            $dlUrl = $null
            if ($script:IsLegacyOs) {
                # Win7: pin qpdf 10.6.3 - its mingw build is self-contained and
                # runs on Win7; current qpdf releases target newer Windows.
                $dlUrl = 'https://github.com/qpdf/qpdf/releases/download/release-qpdf-10.6.3/qpdf-10.6.3-bin-mingw64.zip'
            } else {
                $rel = Invoke-RestMethod -UseBasicParsing `
                    -Uri 'https://api.github.com/repos/qpdf/qpdf/releases/latest' `
                    -Headers @{ 'User-Agent' = 'VirtualCloudPrinter' }
                # Prefer the self-contained mingw64 build (bundles its runtime DLLs);
                # the msvc64 build needs the VC++ redistributable, not guaranteed present.
                $asset = $rel.assets | Where-Object { $_.name -match 'mingw64\.zip$' } |
                         Select-Object -First 1
                if (-not $asset) { $asset = $rel.assets | Where-Object { $_.name -match 'msvc64\.zip$' } | Select-Object -First 1 }
                if ($asset) { $dlUrl = $asset.browser_download_url }
            }
            if ($dlUrl) {
                Invoke-WebRequest -UseBasicParsing -Uri $dlUrl -OutFile $zip `
                    -Headers @{ 'User-Agent' = 'VirtualCloudPrinter' } -MaximumRedirection 10
                Expand-Archive -Path $zip -DestinationPath $QpdfDir -Force
                $extracted = $true
            }
        } catch { Write-Warn2 "qpdf download failed: $($_.Exception.Message)" }
        finally { if (Test-Path $zip) { Remove-Item $zip -Force -ErrorAction SilentlyContinue } }
    }

    $q = Find-QpdfExe
    if ($q) { return $q }

    # Last resort: winget (installs system-wide / on PATH).
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            winget install --id qpdf.qpdf -e --silent `
                --accept-source-agreements --accept-package-agreements | Out-Null
        } catch { Write-Warn2 "winget install of qpdf failed: $($_.Exception.Message)" }
        Refresh-Path
        $q = Find-QpdfExe
    }
    return $q   # may be $null - caller treats qpdf as optional
}

function Ensure-Monitor {
    if (Test-Monitor $ClawmonName)  { Write-Ok "Port monitor present: $ClawmonName";  return $ClawmonName }
    if (Test-Monitor $MfilemonName) { Write-Ok "Port monitor present: $MfilemonName"; return $MfilemonName }

    # Prefer clawmon binaries if the user dropped them in .\vendor\.
    $clawDll   = Join-Path $VendorDir 'clawmon.dll'
    $clawUiDll = Join-Path $VendorDir 'clawmonui.dll'
    $regmon    = Join-Path $VendorDir 'regmon.exe'
    if ((Test-Path $clawDll) -and (Test-Path $clawUiDll) -and (Test-Path $regmon)) {
        Write-Info 'Installing clawmon from .\vendor\ ...'
        Stop-Service -Name Spooler -Force
        Copy-Item $clawDll   (Join-Path $env:WINDIR 'system32\clawmon.dll')   -Force
        Copy-Item $clawUiDll (Join-Path $env:WINDIR 'system32\clawmonui.dll') -Force
        Start-Service -Name Spooler
        & $regmon -r | Out-Null
        if (Test-Monitor $ClawmonName) { Write-Ok "Installed $ClawmonName"; return $ClawmonName }
        Write-Warn2 'clawmon registration did not take; falling back to mfilemon.'
    }

    # Otherwise install mfilemon (identical interface). Prefer an offline copy in
    # .\vendor\; fall back to downloading (validating it's a real PE, not an
    # HTML interstitial page that would fail with "corrupted and unreadable").
    $setup = Join-Path $env:TEMP 'mfilemon-setup.exe'
    $vendorSetup = Join-Path $VendorDir 'mfilemon-setup.exe'

    if (Test-IsExe $vendorSetup) {
        Write-Info "Using vendored installer: $vendorSetup"
        Copy-Item $vendorSetup $setup -Force
    } else {
        Write-Info 'Downloading mfilemon-setup.exe (GitHub release) ...'
        $ok = Get-InstallerExe -Dest $setup -Urls $MfilemonAssets
        if (-not $ok) {
            throw ("Could not download a valid mfilemon installer (the download servers were unreachable or returned an HTML page). " +
                   "Download 'mfilemon-setup.exe' manually from $MfilemonUrl, save it as '$vendorSetup', then re-run install.bat. " +
                   "Alternatively drop clawmon binaries into $VendorDir.")
        }
    }

    # Pick the right silent-install flags for the installer's toolkit. The
    # SourceForge 1.5.2 build is NSIS (/S); the current GitHub v1.6.x build is
    # Inno Setup (/VERYSILENT). Passing the wrong flag pops the GUI wizard and
    # hangs here (Start-Process -Wait), so detect it from the binary.
    $silentArgs = @('/S')
    try {
        $head = [System.IO.File]::ReadAllBytes($setup)
        if ([System.Text.Encoding]::ASCII.GetString($head) -match 'Inno Setup') {
            $silentArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
        }
    } catch {}
    Write-Info "Installing mfilemon silently ($($silentArgs -join ' ')) ..."
    Start-Process -FilePath $setup -ArgumentList $silentArgs -Wait
    Start-Sleep -Seconds 2
    if (Test-Monitor $MfilemonName) { Write-Ok "Installed $MfilemonName"; return $MfilemonName }
    throw "Could not install a print-port monitor. Install mfilemon manually ($MfilemonUrl) or drop clawmon binaries into $VendorDir, then re-run."
}

# --------------------------------------------------------------------------- #
# Port (registry) + printer + config
# --------------------------------------------------------------------------- #
function Set-Port {
    param([string]$Monitor)

    # $PyIsoFlag (-P on 3.12, -I on the Win7 3.8 interpreter) keeps the script's
    # own directory off sys.path so a planted sibling module can never be imported
    # ahead of the stdlib (defense in depth with the $Base ACL). It is an
    # interpreter flag, so upload.py's argv is unchanged.
    # Args: %f=spool file, %j=job id, %r=printer, %u=user (for the set-id lookup),
    # %t=document title (tail, so spaces survive). Keep in sync with upload.py.
    $userCommand = ('"{0}" {1} "{2}" "%f" "%j" "%r" "%u" "%t"' -f $PythonwPath, $PyIsoFlag, $UploadScript)

    Write-Info "Creating port '$PortName' under monitor '$Monitor' (spooler will restart)..."
    Stop-Service -Name Spooler -Force

    $hklm = [Microsoft.Win32.Registry]::LocalMachine
    $monKey = $hklm.OpenSubKey("$MonitorsKey\$Monitor", $true)
    if (-not $monKey) { Start-Service -Name Spooler; throw "Monitor key not found for '$Monitor'." }
    try {
        $p = $monKey.CreateSubKey($PortName)
        try {
            $S = [Microsoft.Win32.RegistryValueKind]::String
            $D = [Microsoft.Win32.RegistryValueKind]::DWord
            $p.SetValue('OutputPath',      $SpoolDir,                       $S)
            $p.SetValue('FilePattern',     '%Y-%m-%d_%H-%n-%s_%i.ps',       $S)
            $p.SetValue('Overwrite',       0,                                $D)
            $p.SetValue('UserCommand',     $userCommand,                     $S)
            $p.SetValue('ExecPath',        $Base,                            $S)
            $p.SetValue('WaitTermination', 0,                                $D)
            $p.SetValue('WaitTimeout',     0,                                $D)
            $p.SetValue('PipeData',        0,                                $D)
            $p.SetValue('HideProcess',     1,                                $D)
            $p.SetValue('User',            '',                               $S)
            $p.SetValue('Domain',          '',                               $S)
            $p.SetValue('Password',        '',                               $S)
        } finally { $p.Close() }
    } finally { $monKey.Close() }

    Start-Service -Name Spooler
    Start-Sleep -Seconds 2
    Write-Ok "Port '$PortName' configured."
}

function Remove-Port {
    param([string]$Monitor)
    Stop-Service -Name Spooler -Force
    $hklm = [Microsoft.Win32.Registry]::LocalMachine
    $monKey = $hklm.OpenSubKey("$MonitorsKey\$Monitor", $true)
    if ($monKey) {
        try { $monKey.DeleteSubKeyTree($PortName, $false) } catch {}
        $monKey.Close()
    }
    Start-Service -Name Spooler
}

function Get-ActiveMonitor {
    if (Test-Monitor $ClawmonName)  { return $ClawmonName }
    if (Test-Monitor $MfilemonName) { return $MfilemonName }
    return $null
}

function Update-Config {
    param([string]$Name, [string]$TargetUrl, [string]$GsPath, [string]$QpdfPath,
          [string]$Token, [string]$DevName, [string]$Department, [string]$Equipment,
          [string]$PdfPassword, [string]$HubBaseUrl, [string]$CatalogDir)

    if (Test-Path $ConfigPath) {
        $cfg = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
    } else {
        $cfg = Get-Content -Raw -Path (Join-Path $ScriptDir 'config.template.json') | ConvertFrom-Json
    }

    # Seed the super-admin hash on install/upgrade if it is missing (§5) - the
    # BOM-less write below persists it.
    Ensure-SuperAdmin $cfg | Out-Null

    if ($GsPath) { $cfg | Add-Member -NotePropertyName ghostscript_path -NotePropertyValue $GsPath -Force }
    if ($QpdfPath) { $cfg | Add-Member -NotePropertyName qpdf_path -NotePropertyValue $QpdfPath -Force }

    # Top-level hub block + catalog share fallback dir (hub mode only; a legacy
    # direct-URL install never touches them).
    if ($HubBaseUrl) {
        $cfg | Add-Member -NotePropertyName hub -NotePropertyValue ([PSCustomObject]@{
            base_url = $HubBaseUrl; verify_tls = $true }) -Force
    }
    if ($CatalogDir) {
        $cfg | Add-Member -NotePropertyName catalog_share_dir -NotePropertyValue $CatalogDir -Force
    }

    if (-not $cfg.printers) {
        $cfg | Add-Member -NotePropertyName printers -NotePropertyValue (New-Object PSObject) -Force
    }

    if ($Name) {
        $entry = [PSCustomObject]@{
            url           = $TargetUrl
            docname_field = 'docname'
            file_field    = 'file'
            extra_fields  = (New-Object PSObject)
            headers       = (New-Object PSObject)
            verify_tls    = $true
        }
        if ($Token) {
            # Hub printer: device_name+token switch upload.py to the catalog-prompt
            # + metadata flow. department_name+equipment_name key the catalog and
            # the filing tree. The password is DPAPI-at-rest; plaintext is never
            # written to config.json.
            $entry | Add-Member -NotePropertyName token           -NotePropertyValue $Token
            $entry | Add-Member -NotePropertyName device_name     -NotePropertyValue $DevName
            $entry | Add-Member -NotePropertyName department_name -NotePropertyValue $Department
            $entry | Add-Member -NotePropertyName equipment_name  -NotePropertyValue $Equipment
            $enc = if ($PdfPassword) {
                [PSCustomObject]@{ enabled = $true;  password_dpapi = (Protect-VcpPassword $PdfPassword) }
            } else {
                [PSCustomObject]@{ enabled = $false; password_dpapi = '' }
            }
            $entry | Add-Member -NotePropertyName pdf_encryption -NotePropertyValue $enc
        }
        # Overwrite / add this printer's entry.
        if ($cfg.printers.PSObject.Properties[$Name]) {
            $cfg.printers.$Name = $entry
        } else {
            $cfg.printers | Add-Member -NotePropertyName $Name -NotePropertyValue $entry -Force
        }
        # Drop the placeholder sample if it is still present and unused.
        if ($Name -ne 'Virtual Cloud Printer' -and $cfg.printers.PSObject.Properties['Virtual Cloud Printer']) {
            $sample = $cfg.printers.'Virtual Cloud Printer'
            if ($sample.url -eq 'https://example.com/print-upload') {
                $cfg.printers.PSObject.Properties.Remove('Virtual Cloud Printer')
            }
        }
    }

    # Write UTF-8 WITHOUT a BOM. Windows PowerShell 5.1's `Set-Content -Encoding
    # UTF8` prepends a BOM, which makes Python's json.load in upload.py fail, so
    # use .NET to control the encoding explicitly.
    [System.IO.File]::WriteAllText(
        $ConfigPath,
        ($cfg | ConvertTo-Json -Depth 12),
        (New-Object System.Text.UTF8Encoding($false))
    )
    Write-Ok "config.json updated ($ConfigPath)."
}

function Resolve-PsDriver {
    # Ensure a v3 PostScript driver is present; return its name. These ship inbox
    # (prnge001.inf on Win10/11, ntprint.inf on Win7) so the driver store resolves
    # them by name. Goes through the shims so Win7 works (no PrintManagement).
    foreach ($d in $DriverCandidates) {
        if (Test-VcpPrinterDriver $d) { return $d }
    }
    foreach ($d in $DriverCandidates) {
        try {
            Write-Info "Adding printer driver '$d'..."
            Add-VcpPrinterDriver -DriverName $d
            return $d
        } catch { Write-Warn2 "Could not add driver '$d': $($_.Exception.Message)" }
    }
    throw "No suitable v3 PostScript driver could be installed (tried: $($DriverCandidates -join ', '))."
}

function Add-VirtualPrinter {
    param([string]$Name, [string]$TargetUrl)

    $driver = Resolve-PsDriver

    $existing = Get-VcpPrinters | Where-Object { $_.Name -eq $Name }
    if ($existing) {
        Write-Info "Printer '$Name' already exists - pointing it at our port."
        # Set the driver too: a pre-existing printer may carry a v4 driver that
        # cannot bind to our port (ERROR_NOT_SUPPORTED).
        Set-VcpPrinter -Name $Name -Driver $driver
    } else {
        Write-Info "Creating printer '$Name' (driver: $driver)..."
        Add-VcpPrinter -Name $Name -Driver $driver
    }
    Write-Ok "Printer '$Name' -> $TargetUrl"
}

function Read-PrinterAndUrl {
    if (-not $PrinterName) {
        $script:PrinterName = Read-Host 'Printer name (as it will appear in the print dialog)'
    }
    if (-not $PrinterName) { throw 'A printer name is required.' }

    if ($Url) {
        # Legacy direct-URL install: no enrollment, no device fields. Existing
        # direct-URL installs must keep working exactly as before.
        $script:HubMode = $false
        if ($Url -notmatch '^(?i)https?://') {
            Write-Warn2 "URL '$Url' does not start with http:// or https:// - continuing anyway."
        }
        return
    }

    # Hub mode: enroll this printer as a device on the LAN hub; the returned
    # per-device ingest URL + token become the printer's config entry.
    $script:HubMode = $true
    if (-not $HubUrl) {
        $h = Read-Host "Hub base URL [$DefaultHubUrl]"
        $script:HubUrl = if ($h) { $h } else { $DefaultHubUrl }
    }
    if ($HubUrl -notmatch '^(?i)https?://') { $script:HubUrl = 'http://' + $HubUrl }
    $script:HubUrl = $HubUrl.TrimEnd('/')

    # Validate the device name locally (same regex the hub enforces, §9) so a
    # typo fails here, not as an HTTP 400 after the prompts.
    if ($DeviceName -and $DeviceName -notmatch $DeviceNameRegex) {
        throw "Invalid -DeviceName '$DeviceName' (must match $DeviceNameRegex)."
    }
    while (-not $DeviceName) {
        $n = Read-Host 'Device name (unique on the hub, e.g. gcms-01)'
        if ($n -match $DeviceNameRegex) { $script:DeviceName = $n }
        else { Write-Warn2 "Invalid device name (letters/digits then letters/digits/._-, max 64 chars)." }
    }

    # The enroll key is needed before the device-type prompt: /device-types is
    # itself gated by X-Enroll-Key.
    if (-not $EnrollKey) {
        $script:EnrollKey = ConvertFrom-SecureStringPlain (Read-Host -AsSecureString 'Enroll key (printed by the hub console)')
    }
    if (-not $EnrollKey) { throw 'An enroll key is required to enroll with the hub.' }

    # First choose the DEPARTMENT, then the EQUIPMENT within it (both come from
    # the LIMS printer_data feed; /departments and /equipment are enroll-gated).
    if (-not $Department) {
        $depts = Get-HubDepartments -BaseUrl $HubUrl -Key $EnrollKey
        if ($depts.Count) { Write-Info ('Departments on the hub: ' + ($depts -join ', ')) }
        $script:Department = Read-Host 'Department (as in the LIMS)'
    }
    if (-not $Department) { throw 'A department is required.' }
    if (-not $Equipment) {
        $equips = Get-HubEquipment -BaseUrl $HubUrl -Key $EnrollKey -Department $Department
        if ($equips.Count) { Write-Info ('Equipment in that department: ' + ($equips -join ', ')) }
        $script:Equipment = Read-Host 'Equipment name (e.g. GCMS / LCMS)'
    }
    if (-not $Equipment) { throw 'An equipment name is required.' }

    # Per-printer AES-256 password (DPAPI-protected at rest). Blank = off.
    # -NoPassword says "explicitly none" so unattended callers (the GUI wizard,
    # scripted rollouts) never fall into the interactive prompt.
    if ($Password) {
        $script:PdfPassword = $Password
    } elseif ($NoPassword) {
        $script:PdfPassword = ''
    } else {
        $script:PdfPassword = ConvertFrom-SecureStringPlain (Read-Host -AsSecureString 'PDF encryption password for this printer (blank = encryption OFF)')
    }

    # Catalog share fallback dir: prompt-id.ps1 (running as the user) reads it
    # when the SYSTEM-side catalog fetch fails. Default derives from the hub host.
    # -CatalogShareDir skips the prompt so a fully-parameterized install (60-client
    # scripted rollout) never blocks on Read-Host.
    if (-not $CatalogShareDir) {
        $hubHost = ([Uri]$HubUrl).Host
        $defaultShare = "\\$hubHost\limsDocs\.vcp\catalog"
        $s = Read-Host "Catalog share folder [$defaultShare]"
        $script:CatalogShareDir = if ($s) { $s } else { $defaultShare }
    }

    Write-Info "Enrolling device '$DeviceName' ($Department / $Equipment) with the hub..."
    $enr = Invoke-HubEnrollment -BaseUrl $HubUrl -Key $EnrollKey `
        -DevName $DeviceName -Department $Department -Equipment $Equipment `
        -Printer $PrinterName -PdfPassword $script:PdfPassword
    $script:Url = "$($enr.ingest_url)"
    $script:HubToken = "$($enr.token)"
    Write-Ok "Enrolled '$DeviceName' - per-device ingest token issued."
}

# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
function Do-Install {
    Assert-Admin
    Assert-NoProtectedPrint
    Write-Host 'Virtual Cloud Printer - installer' -ForegroundColor White
    Read-PrinterAndUrl

    Write-Step 'Preparing folders'
    foreach ($d in @($Base, $SpoolDir, $FailedDir)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

    # SECURITY: lock $Base down to SYSTEM + Administrators only.
    # upload.py runs as SYSTEM from here; the default C:\ProgramData ACL lets any
    # user create files in child folders, so without this a standard user could
    # plant a sibling module (ssl.py/json.py/...) that Python imports ahead of the
    # stdlib and runs as SYSTEM (local privilege escalation). It also stops other
    # users reading config.json (which may hold auth headers) and log.txt.
    & icacls "$Base" /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null

    # The 'ids' subfolder is the ONE place standard users may write: set-id.bat
    # (running as the user) drops <user>.id here and upload.py (SYSTEM) reads it.
    # Safe because upload.py only reads it as opaque text - it is never on sys.path
    # and no code is imported from it. *S-1-5-11 = Authenticated Users.
    New-Item -ItemType Directory -Force -Path $IdsDir | Out-Null
    & icacls "$IdsDir" /grant "*S-1-5-11:(OI)(CI)M" | Out-Null

    # NOTE: Grant-JobDirAccess (which opens spool\ + the interpreter to the
    # impersonated print user) is called LATER, after the venv/python and
    # prompt-id.ps1 exist - see below.

    Copy-Item (Join-Path $ScriptDir 'upload.py') $UploadScript -Force
    # prompt-id.ps1 is launched by upload.py into the user session for the per-print
    # registration prompt, so it must live next to upload.py under $Base.
    $promptPs1 = Join-Path $ScriptDir 'prompt-id.ps1'
    if (Test-Path $promptPs1) { Copy-Item $promptPs1 (Join-Path $Base 'prompt-id.ps1') -Force }
    if (-not (Test-Path $ConfigPath)) {
        Copy-Item (Join-Path $ScriptDir 'config.template.json') $ConfigPath -Force
    }
    Write-Ok "Installed to $Base"

    if ($script:IsLegacyOs) {
        # Windows 7 path: uv/CPython 3.12 require Win10+ - use the 3.8 embeddable.
        Write-Step 'Ensuring Python runtime (Windows 7: Python 3.8 embeddable)'
        New-Item -ItemType Directory -Force -Path $PythonDir | Out-Null
        $script:PythonwPath = Ensure-LegacyPython
        Write-Ok "python: $PythonwPath"
    } else {
    Write-Step 'Ensuring uv + Python virtual environment'
    $uv = Get-Uv
    Write-Ok "uv: $uv"

    # The uploader runs as SYSTEM from $Base. uv's DEFAULT managed-Python dir lives
    # in the installing user's profile (%AppData%\uv\python) and can be broken or
    # unreadable for SYSTEM - if so the port's launch of pythonw.exe fails and every
    # print job errors with nothing uploaded. So install a PRIVATE CPython under
    # $Base (SYSTEM-accessible, tamper-proof via the ACL) and build the venv from it.
    $env:UV_PYTHON_INSTALL_DIR = $PythonDir
    New-Item -ItemType Directory -Force -Path $PythonDir | Out-Null
    Write-Info 'Installing a private CPython 3.12 under the install dir...'
    & $uv python install --install-dir $PythonDir --reinstall 3.12
    # Resolve the interpreter path via uv (respects UV_PYTHON_INSTALL_DIR); fall
    # back to the known layout - the interpreter is the python.exe directly inside
    # a cpython-3.12* folder (NOT the venv-template stub under Lib\venv\...).
    $basePy = (& $uv python find 3.12 2>$null | Select-Object -First 1)
    if ($basePy) { $basePy = "$basePy".Trim() }
    if (-not $basePy -or -not (Test-Path $basePy)) {
        $basePy = Get-ChildItem -Path $PythonDir -Directory -Filter 'cpython-3.12*' -ErrorAction SilentlyContinue |
                  ForEach-Object { Join-Path $_.FullName 'python.exe' } |
                  Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $basePy) { throw "uv did not install CPython 3.12 under $PythonDir." }
    # --relocatable so the venv works when launched by SYSTEM from any directory.
    & $uv venv --python $basePy --relocatable $VenvDir

    if (-not (Test-Path $PythonwPath)) {
        $pyExe = Join-Path $VenvDir 'Scripts\python.exe'
        if (Test-Path $pyExe) {
            $script:PythonwPath = $pyExe
            Write-Warn2 'pythonw.exe not found in venv; using python.exe instead.'
        } else {
            throw "Virtual environment python not found in $VenvDir\Scripts"
        }
    }

    # Smoke-test the interpreter now (same -P flag the port uses) so a corrupt or
    # unreachable base is caught here with a clear message, not as silent per-print
    # job errors. upload.py is stdlib-only, so these imports must all succeed.
    $venvPy = Join-Path $VenvDir 'Scripts\python.exe'
    & $venvPy -P -c "import ssl, json, io, re, mimetypes, subprocess, ctypes, urllib.request" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw ("The provisioned Python failed a smoke test (exit $LASTEXITCODE) - the interpreter is corrupt. " +
               "Run '`$env:UV_PYTHON_INSTALL_DIR='$PythonDir'; uv python install --reinstall 3.12' and re-run install.")
    }
    Write-Ok "venv: $VenvDir ($PythonwPath)"
    }

    # NOW that spool\, the interpreter (venv\/python\) and prompt-id.ps1 all exist,
    # open exactly what the spooler's impersonated print-user needs: create the .ps
    # in spool\, and read+execute the interpreter + prompt script to launch them.
    # (Without this the spooler - which impersonates the submitting user for port
    # I/O and for launching the UserCommand - is denied by the locked $Base, so the
    # job writes but upload.py never runs; jobs only upload after a spooler restart
    # reprocesses them as SYSTEM.) See Grant-JobDirAccess for the exact, minimal ACEs.
    Grant-JobDirAccess

    Write-Step 'Ensuring Ghostscript'
    $gs = Get-Ghostscript
    Write-Ok "Ghostscript: $gs"

    Write-Step 'Ensuring qpdf (AES-256 PDF encryption)'
    $qpdf = Get-Qpdf
    if ($qpdf) { Write-Ok "qpdf: $qpdf" }
    else { Write-Warn2 'qpdf not installed - PDF encryption will be unavailable until it is (re-run install or set qpdf_path). Printing is unaffected.' }

    Write-Step 'Ensuring print-port monitor'
    $monitor = Ensure-Monitor

    Write-Step 'Creating the redirection port'
    Set-Port -Monitor $monitor

    Write-Step 'Writing configuration'
    Update-Config -Name $PrinterName -TargetUrl $Url -GsPath $gs -QpdfPath $qpdf `
        -Token $script:HubToken -DevName $DeviceName -Department $Department -Equipment $Equipment `
        -PdfPassword $script:PdfPassword `
        -HubBaseUrl $(if ($script:HubMode) { $HubUrl } else { '' }) `
        -CatalogDir $script:CatalogShareDir

    Write-Step 'Creating the printer'
    Add-VirtualPrinter -Name $PrinterName -TargetUrl $Url

    # Convenience desktop shortcuts (run by the normal user, no elevation):
    #   Set Print ID     - tag the NEXT single print with a registration number
    #   Print & Register - pick files and give each its OWN number (batch)
    New-DesktopShortcut 'Set Print ID'     'set-id.bat'         'Set a registration number / UUID for your next single print'
    New-DesktopShortcut 'Print & Register' 'print-register.bat' 'Pick files and give each its own registration number (batch)'

    Write-Host "`nDONE." -ForegroundColor Green
    Write-Host "Printer '$PrinterName' is ready and will POST PDFs to:`n    $Url" -ForegroundColor Green
    Write-Host "Add more printers later with add-printer.bat. Logs: $Base\log.txt" -ForegroundColor Gray
}

function Do-Add {
    Assert-Admin
    Assert-NoProtectedPrint
    if (-not (Test-Path $UploadScript)) {
        throw 'Virtual Cloud Printer is not installed yet. Run install.bat first.'
    }
    $monitor = Get-ActiveMonitor
    if (-not $monitor) { throw 'Port monitor missing. Run install.bat first.' }
    Read-PrinterAndUrl

    # Make sure the shared port still exists (it is reused by every printer).
    $hklm = [Microsoft.Win32.Registry]::LocalMachine
    $exists = $hklm.OpenSubKey("$MonitorsKey\$monitor\$PortName")
    if ($exists) { $exists.Close() } else { Set-Port -Monitor $monitor }

    $gs = ''
    if (Test-Path $ConfigPath) {
        try { $gs = (Get-Content -Raw $ConfigPath | ConvertFrom-Json).ghostscript_path } catch {}
    }
    # A per-printer password needs qpdf (encryption fails closed without it).
    $qpdf = ''
    if ($script:PdfPassword) {
        $qpdf = Get-Qpdf
        if (-not $qpdf) { Write-Warn2 'qpdf not found - encryption will FAIL CLOSED (jobs kept in failed\) until it is installed.' }
    }
    Update-Config -Name $PrinterName -TargetUrl $Url -GsPath $gs -QpdfPath $qpdf `
        -Token $script:HubToken -DevName $DeviceName -Department $Department -Equipment $Equipment `
        -PdfPassword $script:PdfPassword `
        -HubBaseUrl $(if ($script:HubMode) { $HubUrl } else { '' }) `
        -CatalogDir $script:CatalogShareDir
    Add-VirtualPrinter -Name $PrinterName -TargetUrl $Url
    Write-Host "`nAdded printer '$PrinterName' -> $Url" -ForegroundColor Green
}

function Do-Uninstall {
    Assert-Admin
    Write-Step 'Removing virtual printers'
    Get-VcpPrinters |
        Where-Object { $_.PortName -eq $PortName } |
        ForEach-Object {
            Write-Info "Removing printer '$($_.Name)'"
            Remove-VcpPrinter -Name $_.Name
        }

    Write-Step 'Removing the redirection port'
    $monitor = Get-ActiveMonitor
    if ($monitor) {
        # Turn off the mfilemon debug logging that fix-queue may have enabled, so we
        # leave a clean slate (the value lives on the monitor key, which persists).
        try {
            $mk = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey("$MonitorsKey\$monitor", $true)
            if ($mk) { $mk.DeleteValue('LogLevel', $false); $mk.Close() }
        } catch { }
        Remove-Port -Monitor $monitor
    }

    Write-Step 'Removing files'
    if (Test-Path $Base) { Remove-Item -Recurse -Force $Base -ErrorAction SilentlyContinue }

    Write-Step 'Removing desktop shortcuts + diagnostics'
    foreach ($n in @('Set Print ID', 'Print & Register')) {
        $lnk = Join-Path $env:PUBLIC "Desktop\$n.lnk"
        if (Test-Path $lnk) { Remove-Item $lnk -Force -ErrorAction SilentlyContinue; Write-Info "Removed shortcut '$n'" }
    }
    $diag = Join-Path $ScriptDir 'vcp-diagnostics.txt'
    if (Test-Path $diag) { Remove-Item $diag -Force -ErrorAction SilentlyContinue }
    $mlog = Join-Path $env:SystemRoot 'System32\mfilemon.log'
    if (Test-Path $mlog) { Remove-Item $mlog -Force -ErrorAction SilentlyContinue }

    if ($RemoveTools -and $monitor -eq $MfilemonName) {
        Write-Warn2 'Leaving the mfilemon monitor installed (uninstall it from Add/Remove Programs if desired).'
    }
    Write-Host "`nUninstalled. (Ghostscript, uv and the port monitor were left installed for reuse.)" -ForegroundColor Green
    Write-Host "To also wipe the simulator's received data, delete simulator\data\ (recreated on next run.bat)." -ForegroundColor Gray
}

function Do-Status {
    Write-Host 'Virtual Cloud Printer - status' -ForegroundColor White
    $monitor = Get-ActiveMonitor
    Write-Host "Port monitor : $(if($monitor){$monitor}else{'NOT INSTALLED'})"
    Write-Host "Install dir  : $Base $(if(Test-Path $Base){'(present)'}else{'(missing)'})"
    Write-Host "Config       : $ConfigPath"
    Write-Host "`nPrinters on our port:" -ForegroundColor Cyan
    Get-VcpPrinters |
        Where-Object { $_.PortName -eq $PortName } |
        ForEach-Object { Write-Host ("  - {0}" -f $_.Name) }
    if (Test-Path $ConfigPath) {
        Write-Host "`nConfigured URLs:" -ForegroundColor Cyan
        $cfg = Get-Content -Raw $ConfigPath | ConvertFrom-Json
        $cfg.printers.PSObject.Properties | ForEach-Object {
            $line = ("  - {0,-30} {1}" -f $_.Name, $_.Value.url)
            # Hub printers carry a device identity; legacy entries do not.
            if ($_.Value.device_name) {
                $line += ("  [device: {0} / {1} / {2}]" -f $_.Value.device_name, $_.Value.department_name, $_.Value.equipment_name)
            }
            Write-Host $line
        }
    }
    $log = Join-Path $Base 'log.txt'
    if (Test-Path $log) {
        Write-Host "`nLast log lines:" -ForegroundColor Cyan
        Get-Content $log -Tail 12 | ForEach-Object { Write-Host "  $_" }
    }
}

function Do-FixQueue {
    # Unjam a stuck print queue (poison jobs can only be cleared by restarting the
    # spooler) and dump the SYSTEM-side diagnostics that live under the ACL-locked
    # $Base into a repo-local report file. Meant to be run elevated via fix-queue.bat.
    Assert-Admin

    $report = Join-Path $ScriptDir 'vcp-diagnostics.txt'
    "Virtual Cloud Printer diagnostics - $(Get-Date)" | Out-File -FilePath $report -Encoding utf8
    function Rpt { param($m) Write-Host $m; Add-Content -Path $report -Value $m }
    function RptCmd { param($label, [scriptblock]$sb)
        Rpt "`n=== $label ==="
        try { $out = & $sb 2>&1 | Out-String; Rpt $out.TrimEnd() } catch { Rpt "  <error: $($_.Exception.Message)>" }
    }

    $monitor = Get-ActiveMonitor

    # Turn ON mfilemon DEBUG logging (LogLevel=3) so the NEXT print records exactly
    # where it fails to C:\Windows\System32\mfilemon.log. It is read at spooler
    # start, so set it BEFORE the restart below.
    if ($monitor) {
        try {
            $mk = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey("$MonitorsKey\$monitor", $true)
            if ($mk) { $mk.SetValue('LogLevel', 3, [Microsoft.Win32.RegistryValueKind]::DWord); $mk.Close() }
        } catch { }
    }

    # REPAIR: refresh the SYSTEM-side scripts so an existing install picks up code
    # changes (e.g. the per-print registration prompt), then re-apply the ACLs the
    # impersonated print user needs (create the .ps in spool\ + launch the
    # interpreter). Fixes the "job runs but nothing uploads" and "183" cases.
    foreach ($f in @('upload.py', 'prompt-id.ps1')) {
        $src = Join-Path $ScriptDir $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $Base $f) -Force }
    }
    Grant-JobDirAccess
    Rpt "`nRefreshed upload.py + prompt-id.ps1 and re-applied job ACLs (spool write + interpreter RX)."

    Write-Step 'Clearing the print queue (restarting spooler) + enabling mfilemon debug log'
    Stop-Service -Name Spooler -Force
    Get-ChildItem (Join-Path $env:SystemRoot 'System32\spool\PRINTERS') -File -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Start-Service -Name Spooler
    Start-Sleep -Seconds 2
    Rpt 'Spooler restarted; stuck jobs cleared; mfilemon debug logging enabled.'

    # Remove orphaned spool files (e.g. large .ps from jobs whose launch failed).
    # Safe: the spooler was just restarted so nothing is mid-write here.
    $orphans = @(Get-ChildItem -Path (Join-Path $SpoolDir '*') -Include *.ps, *.pdf -File -ErrorAction SilentlyContinue)
    if ($orphans.Count) {
        $orphans | Remove-Item -Force -ErrorAction SilentlyContinue
        Rpt ("Cleared {0} orphaned spool file(s)." -f $orphans.Count)
    }

    Rpt "`nMonitor: $monitor"

    RptCmd '$Base ACL (does SYSTEM have access?)' { cmd /c "icacls `"$Base`"" }
    RptCmd 'spool ACL' { cmd /c "icacls `"$SpoolDir`"" }

    RptCmd 'Printers on our port' {
        Get-VcpPrinters | Where-Object { $_.PortName -eq $PortName } |
            Select-Object Name, DriverName, PortName, PrinterStatus | Format-List
    }
    RptCmd 'Port registry values (UserCommand etc.)' {
        Get-ItemProperty "Registry::HKEY_LOCAL_MACHINE\$MonitorsKey\$monitor\$PortName" -ErrorAction Stop |
            Select-Object * -ExcludeProperty PS* | Format-List
    }
    RptCmd 'config.json' { Get-Content -Raw $ConfigPath }
    RptCmd 'prompt-id.ps1 presence + ACL (can print user read it?)' {
        $p = Join-Path $Base 'prompt-id.ps1'
        if (Test-Path $p) { "present"; cmd /c "icacls `"$p`"" } else { "MISSING - re-run install/fixqueue" }
    }
    RptCmd 'ids\ contents (.prompt / .err files reveal dialog outcome)' {
        Get-ChildItem $IdsDir -ErrorAction SilentlyContinue | Select-Object Name, Length, LastWriteTime | Format-Table -Auto
    }

    # On Win7 the runtime is the 3.8 embeddable under python\, not a venv.
    $py  = if ($script:IsLegacyOs) { Join-Path $LegacyPyDir 'python.exe' }  else { Join-Path $VenvDir 'Scripts\python.exe' }
    $pyw = if ($script:IsLegacyOs) { Join-Path $LegacyPyDir 'pythonw.exe' } else { Join-Path $VenvDir 'Scripts\pythonw.exe' }
    $basePyw = Get-ChildItem -Path $PythonDir -Directory -ErrorAction SilentlyContinue |
               ForEach-Object { Join-Path $_.FullName 'pythonw.exe' } | Where-Object { Test-Path $_ } | Select-Object -First 1
    Rpt "`n=== interpreters (legacy OS: $($script:IsLegacyOs)) ==="
    Rpt ("  python.exe : {0} ({1})" -f (Test-Path $py), $py)
    Rpt ("  pythonw.exe: {0} ({1})" -f (Test-Path $pyw), $pyw)
    Rpt ("  base pythonw.exe: {0}" -f $basePyw)
    if (-not $script:IsLegacyOs) { RptCmd 'pyvenv.cfg' { Get-Content (Join-Path $VenvDir 'pyvenv.cfg') } }
    RptCmd "interpreter smoke test (python $PyIsoFlag -c import ...)" {
        & $py $PyIsoFlag -c "import ssl, json, io, re, mimetypes, subprocess, ctypes, urllib.request; print('PYTHON_OK', __import__('sys').version)"
        "exit=$LASTEXITCODE"
    }

    RptCmd 'spool\ contents (did any .ps get written?)' {
        Get-ChildItem $SpoolDir -ErrorAction SilentlyContinue | Select-Object Name, Length, LastWriteTime | Format-Table -Auto
    }

    # DECISIVE: launch upload.py EXACTLY like the port does (pythonw + -P + the 5
    # argv, cwd = ExecPath) and see whether it actually runs, by watching log.txt.
    # Result tells us whether the fault is the interpreter/command or mfilemon.
    $log = Join-Path $Base 'log.txt'
    foreach ($pair in @(@{n = 'venv pythonw'; exe = $pyw}, @{n = 'base pythonw'; exe = $basePyw})) {
        $exe = $pair.exe
        Rpt "`n=== direct launch test via $($pair.n) ==="
        if (-not $exe -or -not (Test-Path $exe)) { Rpt "  (skipped - not found)"; continue }
        $before = if (Test-Path $log) { (Get-Item $log).Length } else { -1 }
        try {
            Start-Process -FilePath $exe -WorkingDirectory $Base -Wait -ArgumentList @(
                $PyIsoFlag, "`"$UploadScript`"", 'C:\__vcp_nonexistent__.ps', '0', 'limsTry', $env:USERNAME, 'diagnostic test'
            )
            Start-Sleep -Seconds 2
            $after = if (Test-Path $log) { (Get-Item $log).Length } else { -1 }
            if ($after -gt $before) { Rpt "  RESULT: upload.py RAN (log.txt $before -> $after bytes). Interpreter + command are GOOD." }
            elseif ($after -ge 0) { Rpt "  RESULT: log.txt present but did NOT grow - upload.py did not execute." }
            else { Rpt "  RESULT: no log.txt produced - this interpreter cannot execute upload.py." }
        } catch { Rpt "  <launch error: $($_.Exception.Message)>" }
    }

    RptCmd 'log.txt (last 60 lines)' {
        if (Test-Path (Join-Path $Base 'log.txt')) { Get-Content (Join-Path $Base 'log.txt') -Tail 60 }
        else { 'no log.txt yet - upload.py has never run (port could not launch it)' }
    }
    RptCmd 'failed/ contents' {
        if (Test-Path $FailedDir) { Get-ChildItem $FailedDir -ErrorAction SilentlyContinue | Select-Object Name, Length, LastWriteTime | Format-Table -Auto }
    }
    RptCmd 'Ghostscript resolves?' {
        $g = ''
        try { $g = (Get-Content -Raw $ConfigPath | ConvertFrom-Json).ghostscript_path } catch {}
        "config ghostscript_path = '$g'  exists=$([bool]($g -and (Test-Path $g)))"
    }
    RptCmd 'mfilemon.log tail (System32)' {
        $ml = Join-Path $env:SystemRoot 'System32\mfilemon.log'
        if (Test-Path $ml) { Get-Content $ml -Tail 40 } else { 'none' }
    }

    Write-Host "`nDone. Report written to:" -ForegroundColor Green
    Write-Host "    $report" -ForegroundColor Green
    Write-Host "`nmfilemon DEBUG logging is now ON. Next step:" -ForegroundColor Cyan
    Write-Host "  1) Print ONE page to the virtual printer." -ForegroundColor Gray
    Write-Host "  2) The failure detail is written to C:\Windows\System32\mfilemon.log" -ForegroundColor Gray
    Write-Host "     (share that file, or re-run fix-queue.bat to capture it)." -ForegroundColor Gray
}

function Do-SetPassword {
    # Set (or clear) the AES-256 PDF encryption passphrase reliably, without the
    # machine-env-var / spooler-restart dance. Writes it into the SYSTEM+Admins
    # locked config.json (BOM-less), which upload.py reads on the next print.
    Assert-Admin
    if (-not (Test-Path $ConfigPath)) { throw 'Not installed yet. Run install.bat first.' }

    Write-Host 'Set PDF encryption passphrase' -ForegroundColor White
    Write-Host 'Enter a long, random passphrase. Leave BLANK to turn encryption OFF.' -ForegroundColor Gray
    $sec = Read-Host -AsSecureString 'Passphrase'
    $pw = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))

    $cfg = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json

    if ([string]::IsNullOrWhiteSpace($pw)) {
        $enc = [PSCustomObject]@{ enabled = $false; password_dpapi = '' }
        $cfg | Add-Member -NotePropertyName pdf_encryption -NotePropertyValue $enc -Force
        Write-Ok 'PDF encryption DISABLED.'
    } else {
        # DPAPI-protect at rest (LocalMachine), mirroring the per-printer paths -
        # so a copied/backed-up config.json never leaks the passphrase in the clear.
        $enc = [PSCustomObject]@{ enabled = $true; password_dpapi = (Protect-VcpPassword $pw) }
        $cfg | Add-Member -NotePropertyName pdf_encryption -NotePropertyValue $enc -Force
        # Make sure qpdf is available and its path is recorded.
        $q = Find-QpdfExe
        if (-not $q) { $q = Get-Qpdf }
        if ($q) {
            $cfg | Add-Member -NotePropertyName qpdf_path -NotePropertyValue $q -Force
            Write-Ok "qpdf: $q"
        } else {
            Write-Warn2 'qpdf not found - encryption will FAIL CLOSED (jobs kept in failed\) until it is installed. Re-run install.bat.'
        }
        Write-Ok 'PDF encryption ENABLED (AES-256).'
    }

    # BOM-less UTF-8 so upload.py's json.load is happy.
    [System.IO.File]::WriteAllText(
        $ConfigPath, ($cfg | ConvertTo-Json -Depth 12),
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "`nDone. It applies to the NEXT print - no spooler restart needed." -ForegroundColor Green
    Write-Host "Decode received PDFs with decode-pdf.bat (or any reader + the passphrase)." -ForegroundColor Gray
}

function Select-ConfiguredPrinter {
    # Resolve $PrinterName against config.json's printers, prompting (with a
    # list) when it was not passed on the command line. Returns the entry name.
    param($Cfg)
    $names = @($Cfg.printers.PSObject.Properties | ForEach-Object { $_.Name })
    if (-not $PrinterName) {
        if ($names.Count) {
            Write-Host 'Configured printers:' -ForegroundColor Cyan
            $names | ForEach-Object { Write-Host "  - $_" }
        }
        $script:PrinterName = Read-Host 'Printer name'
    }
    if (-not $PrinterName) { throw 'A printer name is required.' }
    if (-not $Cfg.printers.PSObject.Properties[$PrinterName]) {
        throw "Printer '$PrinterName' is not in config.json (configured: $($names -join ', '))."
    }
    return $PrinterName
}

function Do-ChangePassword {
    # Change (or clear) ONE printer's AES-256 PDF password, gated by the
    # super-admin password. Stored DPAPI-protected (LocalMachine) - the
    # plaintext never lands in config.json or any log.
    Assert-Admin
    if (-not (Test-Path $ConfigPath)) { throw 'Not installed yet. Run install.bat first.' }
    $cfg = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
    Ensure-SuperAdmin $cfg | Out-Null

    Write-Host 'Change a printer''s PDF encryption password' -ForegroundColor White
    $name = Select-ConfiguredPrinter $cfg
    Read-SuperAdminVerified $cfg

    $pw = $Password
    if (-not $pw) {
        Write-Host 'Leave BLANK to turn encryption OFF for this printer.' -ForegroundColor Gray
        $pw = ConvertFrom-SecureStringPlain (Read-Host -AsSecureString 'New PDF password')
        if ($pw) {
            $again = ConvertFrom-SecureStringPlain (Read-Host -AsSecureString 'Repeat new PDF password')
            if ($pw -cne $again) { throw 'Passwords do not match - nothing changed.' }
        }
    }

    if ($pw) {
        $enc = [PSCustomObject]@{ enabled = $true; password_dpapi = (Protect-VcpPassword $pw) }
        # Make sure qpdf is available and its path is recorded (as Do-SetPassword does).
        $q = Find-QpdfExe
        if (-not $q) { $q = Get-Qpdf }
        if ($q) {
            $cfg | Add-Member -NotePropertyName qpdf_path -NotePropertyValue $q -Force
            Write-Ok "qpdf: $q"
        } else {
            Write-Warn2 'qpdf not found - encryption will FAIL CLOSED (jobs kept in failed\) until it is installed. Re-run install.bat.'
        }
        Write-Ok "PDF encryption ENABLED for '$name' (AES-256, DPAPI at rest)."
    } else {
        $enc = [PSCustomObject]@{ enabled = $false; password_dpapi = '' }
        Write-Ok "PDF encryption DISABLED for '$name'."
    }
    # Replace the whole pdf_encryption object so any legacy plaintext 'password'
    # inside it is dropped at the same time.
    $cfg.printers.$name | Add-Member -NotePropertyName pdf_encryption -NotePropertyValue $enc -Force

    # BOM-less UTF-8 so upload.py's json.load is happy.
    [System.IO.File]::WriteAllText(
        $ConfigPath, ($cfg | ConvertTo-Json -Depth 12),
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "`nDone. It applies to the NEXT print - no spooler restart needed." -ForegroundColor Green
}

function Do-ViewPassword {
    # Show ONE printer's decrypted PDF password, gated by the super-admin
    # password. Console only - never written to any file or log.
    Assert-Admin
    if (-not (Test-Path $ConfigPath)) { throw 'Not installed yet. Run install.bat first.' }
    $cfg = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
    # Seed in memory only (config is not rewritten here); the factory default
    # verifies either way until it is rotated.
    Ensure-SuperAdmin $cfg | Out-Null

    Write-Host 'View a printer''s PDF encryption password' -ForegroundColor White
    $name = Select-ConfiguredPrinter $cfg
    Read-SuperAdminVerified $cfg

    $enc = $cfg.printers.$name.pdf_encryption
    if ($enc -and $enc.password_dpapi) {
        $pw = Unprotect-VcpPassword $enc.password_dpapi
        Write-Host "`n  WARNING: the password is shown below. Clear the console afterwards" -ForegroundColor Yellow
        Write-Host '  and do not screenshot or paste it anywhere.' -ForegroundColor Yellow
        Write-Host ("  Printer  : {0}" -f $name)
        Write-Host ("  Enabled  : {0}" -f [bool]$enc.enabled)
        Write-Host ("  Password : {0}" -f $pw)
    } elseif ($enc -and $enc.PSObject.Properties['password'] -and $enc.password) {
        Write-Host "`n  WARNING: this printer still has a legacy PLAINTEXT password in config.json." -ForegroundColor Yellow
        Write-Host '  Rotate it with change-password.bat to store it DPAPI-protected.' -ForegroundColor Yellow
        Write-Host ("  Printer  : {0}" -f $name)
        Write-Host ("  Password : {0}" -f $enc.password)
    } else {
        Write-Info "No per-printer PDF password is stored for '$name' (per-printer encryption is off; the global pdf_encryption / VCP_PDF_PASSWORD may still apply)."
    }
}

function Do-SetSuperAdmin {
    # Rotate the super-admin password (asks current, then new twice). Only the
    # PBKDF2-SHA256 record is stored - never the plaintext.
    Assert-Admin
    if (-not (Test-Path $ConfigPath)) { throw 'Not installed yet. Run install.bat first.' }
    $cfg = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
    Ensure-SuperAdmin $cfg | Out-Null

    Write-Host 'Rotate the super-admin password' -ForegroundColor White
    Write-Host 'First verify the CURRENT super-admin password.' -ForegroundColor Gray
    Read-SuperAdminVerified $cfg

    $new = ConvertFrom-SecureStringPlain (Read-Host -AsSecureString 'New super admin password')
    if (-not $new) { throw 'The super admin password cannot be blank.' }
    $again = ConvertFrom-SecureStringPlain (Read-Host -AsSecureString 'Repeat new super admin password')
    if ($new -cne $again) { throw 'Passwords do not match - nothing changed.' }

    $cfg | Add-Member -NotePropertyName super_admin -NotePropertyValue (Get-PasswordHash $new) -Force

    # BOM-less UTF-8 so upload.py's json.load is happy.
    [System.IO.File]::WriteAllText(
        $ConfigPath, ($cfg | ConvertTo-Json -Depth 12),
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok 'Super admin password updated.'
}

# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
try {
    # A password handed to us via -PasswordFile is read (then the file is deleted)
    # BEFORE any action runs, so the secret never travels on the command line where
    # WMI/Win32_Process CommandLine or 4688 audit records could capture it. The GUI
    # wizard uses this; console/scripted callers may still use -Password directly.
    if ($PasswordFile) {
        if (Test-Path -LiteralPath $PasswordFile) {
            try { $Password = [System.IO.File]::ReadAllText($PasswordFile) } catch {}
            Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
        }
    }
    switch ($Action) {
        'install'   { Do-Install }
        'add'       { Do-Add }
        'uninstall' { Do-Uninstall }
        'status'    { Do-Status }
        'fixqueue'  { Do-FixQueue }
        'setpassword' { Do-SetPassword }
        'changepassword' { Do-ChangePassword }
        'viewpassword'   { Do-ViewPassword }
        'setsuperadmin'  { Do-SetSuperAdmin }
    }
    exit 0
} catch {
    Write-Host "`nERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
    exit 1
}
