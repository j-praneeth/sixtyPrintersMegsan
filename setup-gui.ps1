<#
    Virtual Cloud Printer - GUI setup wizard
    =========================================
    A click-through (Next -> Next -> Install) front-end for setup.ps1, so nobody
    has to type into a console: every field is a normal text box (Ctrl+V paste
    works), the department/equipment lists are fetched from the hub, and the
    install output streams into the window.

    Launched elevated by install.bat (-Mode install) / add-printer.bat (-Mode add).
    It collects everything and then runs setup.ps1 fully parameterized (no
    console prompts remain - see -NoPassword / -CatalogShareDir in setup.ps1).
    Console/scripted use of setup.ps1 is unchanged.

      -Mode       install (first run: deps + first printer) | add (another printer)
      -DryRunOut  test hook: write the setup.ps1 argument line to this file
                  instead of running it (used by the automated UI tests)
#>
param(
    [ValidateSet('install', 'add')][string]$Mode = 'install',
    [string]$DryRunOut = ''
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$SetupPs1  = Join-Path $ScriptDir 'setup.ps1'
$DefaultHubUrl = 'http://192.168.1.172:8000'
$DeviceNameRegex = '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'   # must match setup.ps1 / hub

function Show-Err([string]$m) {
    [System.Windows.Forms.MessageBox]::Show($m, 'Virtual Cloud Printer setup', 'OK', 'Warning') | Out-Null
}

# setup.ps1 needs Administrator; tell the user early instead of failing later.
# (Skipped in dry-run so the automated tests can run unelevated.)
if (-not $DryRunOut) {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Show-Err "This wizard must run as Administrator.`nDouble-click install.bat / add-printer.bat (they elevate automatically)."
        return
    }
}

# ----------------------------------------------------------------- the form --
$form = New-Object System.Windows.Forms.Form
$form.Text = "Virtual Cloud Printer - $(if ($Mode -eq 'install') { 'install' } else { 'add a printer' })"
$form.Size = New-Object System.Drawing.Size(600, 520)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false

$header = New-Object System.Windows.Forms.Label
$header.Font = New-Object System.Drawing.Font('Segoe UI', 12, [System.Drawing.FontStyle]::Bold)
$header.Location = '18,12'; $header.Size = New-Object System.Drawing.Size(550, 26)
$form.Controls.Add($header)

$sub = New-Object System.Windows.Forms.Label
$sub.ForeColor = [System.Drawing.Color]::DimGray
$sub.Location = '18,38'; $sub.Size = New-Object System.Drawing.Size(550, 30)
$form.Controls.Add($sub)

function New-Panel {
    $p = New-Object System.Windows.Forms.Panel
    $p.Location = '14,72'; $p.Size = New-Object System.Drawing.Size(560, 350)
    $p.Visible = $false
    $form.Controls.Add($p)
    return $p
}
function Add-Label($panel, $text, $x, $y, $w = 250) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text; $l.Location = "$x,$y"; $l.Size = New-Object System.Drawing.Size($w, 18)
    $panel.Controls.Add($l); return $l
}
function Add-Text($panel, $x, $y, $w, $text = '') {
    $t = New-Object System.Windows.Forms.TextBox
    $t.Location = "$x,$y"; $t.Size = New-Object System.Drawing.Size($w, 24); $t.Text = $text
    $panel.Controls.Add($t); return $t
}

# ---- page 1: connection -----------------------------------------------------
$p1 = New-Panel
$rbHub = New-Object System.Windows.Forms.RadioButton
$rbHub.Text = 'LIMS hub printer (recommended) - enrolls on the central hub'
$rbHub.Location = '4,4'; $rbHub.Size = New-Object System.Drawing.Size(540, 22); $rbHub.Checked = $true
$p1.Controls.Add($rbHub)
$rbLegacy = New-Object System.Windows.Forms.RadioButton
$rbLegacy.Text = 'Legacy printer - POST straight to one URL (no hub)'
$rbLegacy.Location = '4,28'; $rbLegacy.Size = New-Object System.Drawing.Size(540, 22)
$p1.Controls.Add($rbLegacy)

Add-Label $p1 'Hub base URL  (just http://host:8000 - nothing after the port):' 4 66 540 | Out-Null
$txtHub = Add-Text $p1 4 86 420 $DefaultHubUrl
Add-Label $p1 'Enroll key  (shown in the hub console / dashboard Devices tab - paste it here):' 4 122 540 | Out-Null
$txtKey = Add-Text $p1 4 142 420

$btnLoad = New-Object System.Windows.Forms.Button
$btnLoad.Text = 'Test hub && load equipment'
$btnLoad.Location = '4,180'; $btnLoad.Size = New-Object System.Drawing.Size(200, 28)
$p1.Controls.Add($btnLoad)
$lblConn = New-Object System.Windows.Forms.Label
$lblConn.Location = '214,186'; $lblConn.Size = New-Object System.Drawing.Size(340, 40)
$p1.Controls.Add($lblConn)

Add-Label $p1 'Target URL (legacy mode only):' 4 236 540 | Out-Null
$txtUrl = Add-Text $p1 4 256 420
$txtUrl.Enabled = $false

$toggleMode = {
    $hub = $rbHub.Checked
    $txtHub.Enabled = $hub; $txtKey.Enabled = $hub; $btnLoad.Enabled = $hub
    $txtUrl.Enabled = -not $hub
}
$rbHub.Add_CheckedChanged($toggleMode)
$rbLegacy.Add_CheckedChanged($toggleMode)

# ---- page 2: printer & device ----------------------------------------------
$p2 = New-Panel
Add-Label $p2 'Printer name (as it appears in the print dialog):' 4 4 540 | Out-Null
$txtPrinter = Add-Text $p2 4 24 300
$lblDev  = Add-Label $p2 'Device name (UNIQUE on the hub, e.g. gcms-01):' 4 60 300
$txtDev  = Add-Text $p2 4 80 300
$lblDevOk = New-Object System.Windows.Forms.Label
$lblDevOk.Location = '312,84'; $lblDevOk.Size = New-Object System.Drawing.Size(240, 18)
$p2.Controls.Add($lblDevOk)
$txtDev.Add_TextChanged({
    if (-not $txtDev.Text) { $lblDevOk.Text = ''; return }
    if ($txtDev.Text -match $DeviceNameRegex) {
        $lblDevOk.Text = 'ok'; $lblDevOk.ForeColor = [System.Drawing.Color]::Green
    } else {
        $lblDevOk.Text = 'letters/digits then . _ - only'; $lblDevOk.ForeColor = [System.Drawing.Color]::Firebrick
    }
})
# Equipment is chosen first (loaded from the hub's `equipment` table); its
# department is then auto-filled from the equipment's own department.
$lblEquip = Add-Label $p2 'Equipment (loaded from the hub - pick one):' 4 110 400
$cmbEquip = New-Object System.Windows.Forms.ComboBox
$cmbEquip.Location = '4,128'; $cmbEquip.Size = New-Object System.Drawing.Size(300, 24)
# DropDownList = pick from the loaded list only; the user cannot type a free value.
$cmbEquip.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
$p2.Controls.Add($cmbEquip)

$lblDept = Add-Label $p2 'Department (fixed - set from the selected equipment):' 4 158 400
$cmbDept = Add-Text $p2 4 176 300
$cmbDept.ReadOnly = $true
$cmbDept.BackColor = [System.Drawing.Color]::FromArgb(240, 240, 240)

# PDF-password fields removed: printers are enrolled without per-printer
# encryption from this wizard (always -NoPassword). Set/change a password later
# via setup.ps1 -Action changepassword if ever needed.

# Catalog share folder is not collected: the dropdowns come from the hub /catalog
# fetch and there is no limsDocs SMB share to fall back to (Supabase-only).

# equipment name -> its department (populated when the equipment list loads).
$script:equipDeptMap = @{}
# Choosing an equipment auto-fills its department (fetched from the equipment table).
$cmbEquip.Add_SelectedIndexChanged({
    $sel = $cmbEquip.Text.Trim()
    if ($script:equipDeptMap -and $script:equipDeptMap.ContainsKey($sel)) {
        $cmbDept.Text = [string]$script:equipDeptMap[$sel]
    }
})

# ---- page 3: review + install ----------------------------------------------
$p3 = New-Panel
$txtSummary = New-Object System.Windows.Forms.TextBox
$txtSummary.Multiline = $true; $txtSummary.ReadOnly = $true
$txtSummary.Location = '4,4'; $txtSummary.Size = New-Object System.Drawing.Size(548, 96)
$txtSummary.BackColor = [System.Drawing.Color]::WhiteSmoke
$p3.Controls.Add($txtSummary)
$lblRun = New-Object System.Windows.Forms.Label
$lblRun.Location = '4,106'; $lblRun.Size = New-Object System.Drawing.Size(548, 20)
$lblRun.Text = 'Click Install to run the setup. Progress appears below (first run downloads tools - takes a few minutes).'
$p3.Controls.Add($lblRun)
$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Multiline = $true; $txtLog.ReadOnly = $true; $txtLog.ScrollBars = 'Vertical'
$txtLog.Font = New-Object System.Drawing.Font('Consolas', 8.5)
$txtLog.Location = '4,130'; $txtLog.Size = New-Object System.Drawing.Size(548, 186)
$p3.Controls.Add($txtLog)
$lblResult = New-Object System.Windows.Forms.Label
$lblResult.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
$lblResult.Location = '4,322'; $lblResult.Size = New-Object System.Drawing.Size(548, 24)
$p3.Controls.Add($lblResult)

# ---- navigation --------------------------------------------------------------
$btnBack = New-Object System.Windows.Forms.Button
$btnBack.Text = '< Back'; $btnBack.Location = '300,438'; $btnBack.Size = New-Object System.Drawing.Size(84, 30)
$form.Controls.Add($btnBack)
$btnNext = New-Object System.Windows.Forms.Button
$btnNext.Text = 'Next >'; $btnNext.Location = '390,438'; $btnNext.Size = New-Object System.Drawing.Size(84, 30)
$form.Controls.Add($btnNext)
$btnClose = New-Object System.Windows.Forms.Button
$btnClose.Text = 'Cancel'; $btnClose.Location = '484,438'; $btnClose.Size = New-Object System.Drawing.Size(84, 30)
$btnClose.Add_Click({ $form.Close() })
$form.Controls.Add($btnClose)

$pages = @($p1, $p2, $p3)
$script:page = 0
function Show-Page([int]$i) {
    $script:page = $i
    for ($j = 0; $j -lt $pages.Count; $j++) { $pages[$j].Visible = ($j -eq $i) }
    $btnBack.Enabled = ($i -gt 0)
    $btnNext.Text = if ($i -eq 2) { 'Install' } else { 'Next >' }
    $header.Text = @('Step 1 of 3 - Connection', 'Step 2 of 3 - Printer and device',
                     'Step 3 of 3 - Review and install')[$i]
    $sub.Text = @('Pick the mode and tell the wizard where the hub is. You can paste into every box.',
                  'Name the printer, give the device a unique name, and pick its equipment (the department is set automatically).',
                  'Check the summary, then click Install.')[$i]
}

function Assert-Clean([string]$v, [string]$what) {
    # Values travel to setup.ps1 on a command line; a double quote would break
    # the quoting. Friendlier to reject it than to mangle it.
    if ($v -match '"') { Show-Err "$what must not contain a double-quote (`") character."; throw 'bad input' }
}

$btnLoad.Add_Click({
    $lblConn.ForeColor = [System.Drawing.Color]::DimGray
    $lblConn.Text = 'Contacting the hub...'
    $form.Refresh()
    try {
        $u = $txtHub.Text.Trim()
        if (-not $u) { throw 'Enter the hub base URL first.' }
        if ($u -notmatch '^(?i)https?://') { $u = 'http://' + $u; $txtHub.Text = $u }
        if ($u -match '/ingest/') { throw 'That is an /ingest/ link, not the hub base URL. Use just http://host:8000.' }
        $key = $txtKey.Text.Trim()
        if (-not $key) { throw 'Paste the enroll key first.' }
        $resp = Invoke-WebRequest -UseBasicParsing -Uri ($u.TrimEnd('/') + '/equipment') `
            -Headers @{ 'X-Enroll-Key' = $key } -TimeoutSec 10
        $eq = @(($resp.Content | ConvertFrom-Json).equipment)
        $cmbEquip.Items.Clear(); $cmbDept.Text = ''
        $script:equipDeptMap = @{}
        foreach ($e in $eq) {
            $nm = [string]$e.name; $dn = [string]$e.department
            if (-not $nm) { continue }
            [void]$cmbEquip.Items.Add($nm)
            $script:equipDeptMap[$nm] = $dn
        }
        if ($cmbEquip.Items.Count) { $cmbEquip.SelectedIndex = 0 }   # auto-fills department
        $lblConn.ForeColor = [System.Drawing.Color]::Green
        $lblConn.Text = "Hub OK - enroll key accepted. Equipment loaded: $($cmbEquip.Items.Count)"
    } catch {
        $lblConn.ForeColor = [System.Drawing.Color]::Firebrick
        $m = $_.Exception.Message
        if ($m -match '401') { $m = 'The hub rejected the enroll key (401). Copy it from the hub console.' }
        $lblConn.Text = "Failed: $m"
    }
})

function Build-Summary {
    if ($rbHub.Checked) {
        $txtSummary.Text = ("Mode:          hub printer ($Mode)`r`n" +
            "Printer name:  $($txtPrinter.Text)`r`n" +
            "Hub URL:       $($txtHub.Text)`r`n" +
            "Device:        $($txtDev.Text)  ($($cmbDept.Text) / $($cmbEquip.Text))")
    } else {
        $txtSummary.Text = ("Mode:          legacy direct-URL printer ($Mode)`r`n" +
            "Printer name:  $($txtPrinter.Text)`r`n" +
            "Target URL:    $($txtUrl.Text)")
    }
}

# The exact setup.ps1 parameter names. A token is emitted UNQUOTED only if it is
# one of these; every other token is a user VALUE and is always quoted - so a
# value that happens to start with '-' (a password like "-x", a printer name)
# can never be misread as a flag. The password does NOT travel here: it is written
# to an ACL-locked file and passed as -PasswordFile (see Start-Install).
$script:KnownFlags = @('-Action', '-PrinterName', '-Url', '-HubUrl', '-DeviceName',
    '-Department', '-Equipment', '-EnrollKey', '-CatalogShareDir', '-Password', '-PasswordFile', '-NoPassword')

function Build-Args {
    # -> array of tokens (flag names + user values); Quote-Args wraps every value.
    $a = @('-Action', $Mode, '-PrinterName', $txtPrinter.Text.Trim())
    if ($rbHub.Checked) {
        # The catalog-share folder is no longer collected or sent (Supabase-only;
        # the hub /catalog fetch is the source of the dropdowns). setup.ps1 no longer
        # prompts for it either, so omitting the flag is safe.
        $a += @('-HubUrl', $txtHub.Text.Trim(), '-DeviceName', $txtDev.Text.Trim(),
                '-Department', $cmbDept.Text.Trim(), '-Equipment', $cmbEquip.Text.Trim(),
                '-EnrollKey', $txtKey.Text.Trim())
        $a += @('-NoPassword')  # this wizard never sets a per-printer PDF password
    } else {
        $a += @('-Url', $txtUrl.Text.Trim())
    }
    return $a
}

function Quote-Args($tokens) {
    # Quote everything that is not an exact known flag name (position-independent,
    # content-independent), so a leading '-' in a value is never a flag.
    ($tokens | ForEach-Object { if ($script:KnownFlags -contains $_) { $_ } else { '"' + $_ + '"' } }) -join ' '
}

$script:proc = $null
$script:outFile = $null
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 700
$timer.Add_Tick({
    try {
        if ($script:outFile -and (Test-Path $script:outFile)) {
            # read while setup.ps1 is still writing (shared read)
            $fs = [System.IO.File]::Open($script:outFile, 'Open', 'Read', 'ReadWrite')
            try { $sr = New-Object System.IO.StreamReader($fs); $txtLog.Text = $sr.ReadToEnd() } finally { $fs.Close() }
            $txtLog.SelectionStart = $txtLog.Text.Length; $txtLog.ScrollToCaret()
        }
        if ($script:proc -and $script:proc.HasExited) {
            $timer.Stop()
            # Decide success from the LOG, not just ExitCode: Start-Process -PassThru
            # with redirected output sometimes leaves .ExitCode null even on a clean
            # exit, which used to show a false "FAILED (exit )". setup.ps1 prints an
            # "ERROR:" line and exits 1 on real failure; on success it exits 0 with no
            # such line. So: failed = (a known non-zero exit) OR (an ERROR: line).
            try { $script:proc.WaitForExit() } catch {}
            $ec = $null; try { $ec = $script:proc.ExitCode } catch {}
            $logtxt = ''
            try { if (Test-Path $script:outFile) { $logtxt = [System.IO.File]::ReadAllText($script:outFile) } } catch {}
            # setup.ps1's failure path prints a line starting with "ERROR:" (its catch,
            # via Write-Host -> stdout) and exits 1. Don't scan stderr: first-run tool
            # installers (uv/winget/ghostscript) write benign noise there.
            $failed = (($null -ne $ec) -and ($ec -ne 0)) -or ($logtxt -match '(?m)^\s*ERROR:')
            if (-not $failed) {
                $lblResult.ForeColor = [System.Drawing.Color]::Green
                $lblResult.Text = 'DONE - the printer is ready. You can close this window and print.'
            } else {
                $lblResult.ForeColor = [System.Drawing.Color]::Firebrick
                $lblResult.Text = 'FAILED - read the log above for the reason.'
                $btnNext.Enabled = $true; $btnBack.Enabled = $true
            }
            $btnClose.Text = 'Close'
        }
    } catch { }
})

function Start-Install {
    # Every value that reaches the setup.ps1 command line is checked for an embedded
    # double-quote (which would break tokenization). The password is NOT on the
    # command line (it goes via -PasswordFile), so it may contain any character.
    try {
        Assert-Clean $txtPrinter.Text 'The printer name'
        Assert-Clean $txtUrl.Text 'The URL'
        if ($rbHub.Checked) {
            Assert-Clean $txtHub.Text 'The hub URL'
            Assert-Clean $txtKey.Text 'The enroll key'
            Assert-Clean $cmbDept.Text 'The department'
            Assert-Clean $cmbEquip.Text 'The equipment'
        }
    } catch { return }
    $quoted = Quote-Args (Build-Args)
    if ($DryRunOut) {
        # Dry run demonstrates the argument SHAPE only.
        [System.IO.File]::WriteAllText($DryRunOut, $quoted, (New-Object System.Text.UTF8Encoding($false)))
        $lblResult.ForeColor = [System.Drawing.Color]::Green
        $lblResult.Text = 'DRY RUN - arguments written.'
        return
    }
    # This wizard never sets a per-printer PDF password (-NoPassword), so no
    # passphrase file is created or passed.
    $pwFile = ''
    $btnNext.Enabled = $false; $btnBack.Enabled = $false; $btnClose.Text = 'Close'
    # $quoted holds no secret (only a file path), so it can be shown verbatim.
    $lblResult.Text = ''; $txtLog.Text = "Running setup.ps1 $quoted`r`n`r`n"
    $script:outFile = Join-Path $env:TEMP ('vcp-wizard-' + [guid]::NewGuid().ToString('n') + '.log')
    $psArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$SetupPs1`" $quoted"
    $script:proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $psArgs `
        -RedirectStandardOutput $script:outFile -RedirectStandardError ($script:outFile + '.err') `
        -WindowStyle Hidden -PassThru
    $timer.Start()
}

$btnBack.Add_Click({ if ($script:page -gt 0) { Show-Page ($script:page - 1) } })
$btnNext.Add_Click({
    switch ($script:page) {
        0 {
            if ($rbHub.Checked) {
                if (-not $txtHub.Text.Trim()) { Show-Err 'Enter the hub base URL.'; return }
                if ($txtHub.Text -match '/ingest/') { Show-Err 'That is an /ingest/ link, not the hub base URL. Use just http://host:8000.'; return }
                if ($txtHub.Text.Trim() -notmatch '^(?i)https?://') { $txtHub.Text = 'http://' + $txtHub.Text.Trim() }
                if (-not $txtKey.Text.Trim()) { Show-Err 'Paste the enroll key (from the hub console).'; return }
            } else {
                if (-not $txtUrl.Text.Trim()) { Show-Err 'Enter the target URL.'; return }
            }
            # legacy mode has no device fields
            $hub = $rbHub.Checked
            foreach ($c in @($lblDev, $txtDev, $lblDevOk, $lblDept, $cmbDept, $lblEquip, $cmbEquip)) {
                $c.Enabled = $hub
            }
            Show-Page 1
        }
        1 {
            if (-not $txtPrinter.Text.Trim()) { Show-Err 'Enter a printer name.'; return }
            if ($rbHub.Checked) {
                if ($txtDev.Text.Trim() -notmatch $DeviceNameRegex) { Show-Err 'Enter a valid device name (letters/digits then . _ - only, max 64).'; return }
                if (-not $cmbEquip.Text.Trim()) { Show-Err 'Pick an equipment.'; return }
                if (-not $cmbDept.Text.Trim()) { Show-Err 'Select an equipment so its department is set.'; return }
            }
            Build-Summary
            Show-Page 2
        }
        2 { Start-Install }
    }
})

Show-Page 0
[void]$form.ShowDialog()
