<#
    Per-print registration prompt
    ==============================
    Shows a dialog asking for the registration number, test method and test
    parameter for ONE print job and writes the choice as compact JSON to -OutFile.
    It is launched automatically by upload.py INTO the interactive user's session
    (via CreateProcessAsUser) each time you print.

    Modes (upload.py picks by passing -Department/-Equipment or not):
      * Hub printer (-Department/-Equipment given): THREE cascading searchable
        dropdowns driven by the printer_data catalog - Registration, then that
        registration's Methods, then that method's Parameters. All three are
        required. OK writes
        {"registration_number":..,"test_method":..,"test_parameter":..}.
        The catalog comes from -CatalogFile (pre-fetched from the hub by
        upload.py) or, if that is missing, "<FallbackDir>\<dept>__<equipment>.json"
        on the limsDocs share (this script runs AS THE USER, who can reach the UNC
        share even when the SYSTEM-side hub fetch failed). If neither loads, a
        warning banner plus free-text boxes appear so work never stops - the hub
        validates the tuple and HOLDS the job if it does not match.
      * Legacy printer (no -Department/-Equipment): the original free-text prompt,
        writing {"id": ...} only.

    Not meant to be run by hand (upload.py passes the args). The dialog is STRICT:
    "Cancel" / closing the window writes {"cancel":true}, and upload.py then
    DISCARDS the job - nothing is uploaded, nothing reaches the hub. Only the
    -TimeoutSec auto-close writes nothing (treated as "nobody was at the machine":
    the job proceeds without a number and the hub HOLDS it, so an unattended
    print is never silently destroyed).

      -OutFile      path the JSON is written to (upload.py reads + deletes it)
      -DocName      the document name, shown for context
      -Department   hub printer's department (enables catalog mode)
      -Equipment    hub printer's equipment name (GCMS, LCMS, ...)
      -CatalogFile  catalog JSON pre-fetched by upload.py (may be absent)
      -FallbackDir  UNC dir holding <dept>__<equipment>.json, exported by the hub
      -BatchNote    the answer applies to the WHOLE print batch (batch
                    coalescing in upload.py: one dialog for N concurrent jobs)
      -TimeoutSec   auto-close (job proceeds, hub holds) after this many seconds.
                    upload.py only waits prompt_timeout_seconds for the answer, so
                    the dialog must not outlive it: an answer given after upload.py
                    stopped listening would be silently discarded. A countdown
                    appears in the title bar for the last 60 seconds. 0 = no timeout.
#>
param(
    [Parameter(Mandatory = $true)][string]$OutFile,
    [string]$DocName = '',
    [string]$Department = '',
    [string]$Equipment = '',
    [string]$CatalogFile = '',
    [string]$FallbackDir = '',
    [string]$HubUrl = '',
    [switch]$BatchNote,
    [int]$TimeoutSec = 0
)

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    # BOM-less UTF-8, same convention as every other file this toolkit writes.
    function Write-OutJson([string]$Json) {
        [System.IO.File]::WriteAllText($OutFile, $Json,
            (New-Object System.Text.UTF8Encoding($false)))
    }

    function Test-HubReachable {
        if (-not $HubUrl) { return $true }
        try {
            $req = [System.Net.WebRequest]::Create($HubUrl.TrimEnd('/') + '/healthz')
            $req.Timeout = 3000
            $req.Method = 'GET'
            $resp = $req.GetResponse()
            $resp.Close()
            return $true
        } catch { return $false }
    }

    function Show-ServerUnavailable {
        [System.Windows.Forms.MessageBox]::Show(
            "Server Unavailable. Please contact the IT Department.`n`nDo not click the ""Attach && Print"" button again, as your previous print request has already been queued and will automatically appear in LIMS once the server connection is restored.",
            'Server Unavailable',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
    }

    # Match the hub's sanitize_segment BYTE-FOR-BYTE so the share-fallback
    # filename "<dept>__<equipment>.json" lines up (regex -> _, strip leading/
    # trailing '.'/space, truncate 100, re-strip, empty -> '_').
    function Sanitize-Segment([string]$s) {
        $out = ("$s" -replace '[^A-Za-z0-9._ -]', '_')
        $out = $out.Trim('.', ' ')
        if ($out.Length -gt 100) { $out = $out.Substring(0, 100) }
        $out = $out.Trim('.', ' ')
        if ([string]::IsNullOrEmpty($out)) { $out = '_' }
        return $out
    }

    # Strict-dialog bookkeeping: $answered is set by the OK handlers (an answer
    # was written); $timedOut by the auto-close timer. Anything else that closes
    # the form (Cancel button, X, Alt+F4) counts as an explicit user CANCEL and
    # writes {"cancel":true} after ShowDialog returns - upload.py discards the job.
    $script:answered = $false
    $script:timedOut = $false

    $hub = [bool]$Department -or [bool]$Equipment

    # ---- hub mode: load the catalog (-CatalogFile first, then the share copy) ----
    $script:regs = @()
    $catalogLoaded = $false
    if ($hub) {
        $candidates = @()
        if ($CatalogFile) { $candidates += $CatalogFile }
        if ($FallbackDir) {
            $fname = (Sanitize-Segment $Department) + '__' + (Sanitize-Segment $Equipment) + '.json'
            $candidates += (Join-Path $FallbackDir $fname)
        }
        foreach ($path in $candidates) {
            try {
                if (-not (Test-Path -LiteralPath $path)) { continue }
                $parsed = [System.IO.File]::ReadAllText($path) | ConvertFrom-Json
                $loaded = @()
                foreach ($r in @($parsed.registrations)) {
                    if (-not $r -or -not $r.registration_number) { continue }
                    $methods = @()
                    foreach ($m in @($r.methods)) {
                        if (-not $m -or -not $m.test_method) { continue }
                        $params = @()
                        if ($m.parameters) {
                            $params = @($m.parameters | ForEach-Object { [string]$_ } | Where-Object { $_ })
                        }
                        $methods += [pscustomobject]@{
                            Method     = [string]$m.test_method
                            Parameters = $params
                        }
                    }
                    if ($methods.Count -eq 0) { continue }
                    $loaded += [pscustomobject]@{
                        RegNo   = [string]$r.registration_number
                        Methods = $methods
                    }
                }
                # An empty catalog is as useless as no catalog: fall through to the
                # next candidate (and ultimately to the free-text fallback).
                if ($loaded.Count -gt 0) {
                    $script:regs = $loaded
                    $catalogLoaded = $true
                    break
                }
            } catch { }   # unreadable/corrupt candidate -> try the next one
        }
    }

    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'Registration'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false; $form.MinimizeBox = $false
    $form.TopMost = $true
    $form.ShowInTaskbar = $true
    # Pop to the foreground and take focus the instant it is ready, so it is never
    # hidden behind the app you printed from (which can make it feel slow to appear).
    $form.Add_Shown({ $form.Activate(); $form.BringToFront() })

    $lbl = New-Object System.Windows.Forms.Label
    # Batch mode: the answer covers every document printed together, so naming
    # one document (whichever job won the leader election) would mislead.
    if ($BatchNote) { $lbl.Text = "Registration for this print batch`n(applied to ALL documents printed together):" }
    elseif ($DocName) { $lbl.Text = "Registration for:`n$DocName" }
    else { $lbl.Text = 'Registration for this print:' }
    $lbl.Location = New-Object System.Drawing.Point(15, 12)

    if ($hub -and $catalogLoaded) {
        # ---- hub mode with catalog: search -> Registration -> Method -> Parameter ----
        $form.Size = New-Object System.Drawing.Size(490, 410)
        $lbl.Size = New-Object System.Drawing.Size(450, 40)
        $form.Controls.Add($lbl)

        $lblSearch = New-Object System.Windows.Forms.Label
        $lblSearch.Text = 'Search registration:'
        $lblSearch.Location = New-Object System.Drawing.Point(15, 56)
        $lblSearch.Size = New-Object System.Drawing.Size(450, 18)
        $form.Controls.Add($lblSearch)

        $txtSearch = New-Object System.Windows.Forms.TextBox
        $txtSearch.Location = New-Object System.Drawing.Point(15, 76)
        $txtSearch.Size = New-Object System.Drawing.Size(445, 25)
        $form.Controls.Add($txtSearch)

        $lblReg = New-Object System.Windows.Forms.Label
        $lblReg.Text = 'Registration number:'
        $lblReg.Location = New-Object System.Drawing.Point(15, 108)
        $lblReg.Size = New-Object System.Drawing.Size(450, 18)
        $form.Controls.Add($lblReg)

        $cmbReg = New-Object System.Windows.Forms.ComboBox
        $cmbReg.Location = New-Object System.Drawing.Point(15, 128)
        $cmbReg.Size = New-Object System.Drawing.Size(445, 25)
        $cmbReg.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
        $form.Controls.Add($cmbReg)

        $lblMethod = New-Object System.Windows.Forms.Label
        $lblMethod.Text = 'Test method:'
        $lblMethod.Location = New-Object System.Drawing.Point(15, 162)
        $lblMethod.Size = New-Object System.Drawing.Size(450, 18)
        $form.Controls.Add($lblMethod)

        $cmbMethod = New-Object System.Windows.Forms.ComboBox
        $cmbMethod.Location = New-Object System.Drawing.Point(15, 182)
        $cmbMethod.Size = New-Object System.Drawing.Size(445, 25)
        $cmbMethod.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
        $form.Controls.Add($cmbMethod)

        $lblParam = New-Object System.Windows.Forms.Label
        $lblParam.Text = 'Test parameter:'
        $lblParam.Location = New-Object System.Drawing.Point(15, 216)
        $lblParam.Size = New-Object System.Drawing.Size(450, 18)
        $form.Controls.Add($lblParam)

        $cmbParam = New-Object System.Windows.Forms.ComboBox
        $cmbParam.Location = New-Object System.Drawing.Point(15, 236)
        $cmbParam.Size = New-Object System.Drawing.Size(445, 25)
        $cmbParam.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
        $form.Controls.Add($cmbParam)

        $btnOk = New-Object System.Windows.Forms.Button
        $btnOk.Text = 'Attach && print'
        $btnOk.Location = New-Object System.Drawing.Point(130, 300)
        $btnOk.Size = New-Object System.Drawing.Size(125, 30)
        $btnOk.Enabled = $false      # all three dropdowns must have a selection
        $form.Controls.Add($btnOk)
        $form.AcceptButton = $btnOk

        $btnCancel = New-Object System.Windows.Forms.Button
        $btnCancel.Text = 'Cancel'
        $btnCancel.Location = New-Object System.Drawing.Point(265, 300)
        $btnCancel.Size = New-Object System.Drawing.Size(90, 30)
        $btnCancel.Add_Click({ $form.Close() })   # cancel JSON written after ShowDialog
        $form.Controls.Add($btnCancel)

        # $script:filtered[i] corresponds 1:1 to $cmbReg.Items[i].
        $script:filtered = @()
        $updateOk = {
            $btnOk.Enabled = ($cmbReg.SelectedIndex -ge 0 -and
                              $cmbMethod.SelectedIndex -ge 0 -and
                              $cmbParam.SelectedIndex -ge 0)
        }
        $refreshParams = {
            $cmbParam.Items.Clear()
            $ri = $cmbReg.SelectedIndex
            $mi = $cmbMethod.SelectedIndex
            if ($ri -ge 0 -and $ri -lt $script:filtered.Count -and $mi -ge 0) {
                $m = $script:filtered[$ri].Methods[$mi]
                foreach ($p in $m.Parameters) { [void]$cmbParam.Items.Add($p) }
                if ($cmbParam.Items.Count -eq 1) { $cmbParam.SelectedIndex = 0 }  # only one -> auto-pick
            }
            & $updateOk
        }
        $refreshMethods = {
            $cmbMethod.Items.Clear()
            $cmbParam.Items.Clear()
            $ri = $cmbReg.SelectedIndex
            if ($ri -ge 0 -and $ri -lt $script:filtered.Count) {
                foreach ($m in $script:filtered[$ri].Methods) { [void]$cmbMethod.Items.Add($m.Method) }
                if ($cmbMethod.Items.Count -eq 1) { $cmbMethod.SelectedIndex = 0 }  # only one -> auto-pick
            }
            & $refreshParams
        }
        $refreshRegs = {
            # Plain substring match on reg_no (no wildcard surprises).
            $q = $txtSearch.Text.Trim()
            $script:filtered = @($script:regs | Where-Object {
                (-not $q) -or
                ($_.RegNo.IndexOf($q, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
            })
            $cmbReg.BeginUpdate()
            $cmbReg.Items.Clear()
            foreach ($r in $script:filtered) { [void]$cmbReg.Items.Add($r.RegNo) }
            $cmbReg.EndUpdate()
            $cmbReg.SelectedIndex = $(if ($script:filtered.Count -eq 1) { 0 } else { -1 })
            & $refreshMethods
        }
        $txtSearch.Add_TextChanged($refreshRegs)
        $cmbReg.Add_SelectedIndexChanged($refreshMethods)
        $cmbMethod.Add_SelectedIndexChanged($refreshParams)
        $cmbParam.Add_SelectedIndexChanged($updateOk)

        $btnOk.Add_Click({
            $ri = $cmbReg.SelectedIndex
            if ($ri -lt 0 -or $ri -ge $script:filtered.Count -or
                $cmbMethod.SelectedIndex -lt 0 -or $cmbParam.SelectedIndex -lt 0) { return }
            if (-not (Test-HubReachable)) { Show-ServerUnavailable; return }
            $json = (@{
                registration_number = $script:filtered[$ri].RegNo
                test_method         = [string]$cmbMethod.SelectedItem
                test_parameter      = [string]$cmbParam.SelectedItem
            } | ConvertTo-Json -Compress)
            Write-OutJson $json
            $script:answered = $true
            $form.Close()
        })

        & $refreshRegs   # initial fill (empty search = everything)

    } elseif ($hub) {
        # ---- hub mode, NO catalog: free-text fallback so work never stops ----
        $form.Size = New-Object System.Drawing.Size(490, 400)
        $lbl.Size = New-Object System.Drawing.Size(450, 40)
        $form.Controls.Add($lbl)

        $lblWarn = New-Object System.Windows.Forms.Label
        $lblWarn.Text = 'Catalog unavailable - enter the values manually; the job will be HELD at the hub if they do not match a known registration.'
        $lblWarn.Location = New-Object System.Drawing.Point(15, 54)
        $lblWarn.Size = New-Object System.Drawing.Size(450, 48)
        $lblWarn.BackColor = [System.Drawing.Color]::FromArgb(255, 243, 205)
        $lblWarn.ForeColor = [System.Drawing.Color]::FromArgb(102, 77, 3)
        $lblWarn.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
        $lblWarn.Padding = New-Object System.Windows.Forms.Padding(6)
        $form.Controls.Add($lblWarn)

        $lblReg = New-Object System.Windows.Forms.Label
        $lblReg.Text = 'Registration number:'
        $lblReg.Location = New-Object System.Drawing.Point(15, 110)
        $lblReg.Size = New-Object System.Drawing.Size(450, 18)
        $form.Controls.Add($lblReg)

        $txtReg = New-Object System.Windows.Forms.TextBox
        $txtReg.Location = New-Object System.Drawing.Point(15, 130)
        $txtReg.Size = New-Object System.Drawing.Size(445, 25)
        $form.Controls.Add($txtReg)

        $lblMethod = New-Object System.Windows.Forms.Label
        $lblMethod.Text = 'Test method:'
        $lblMethod.Location = New-Object System.Drawing.Point(15, 164)
        $lblMethod.Size = New-Object System.Drawing.Size(450, 18)
        $form.Controls.Add($lblMethod)

        $txtMethod = New-Object System.Windows.Forms.TextBox
        $txtMethod.Location = New-Object System.Drawing.Point(15, 184)
        $txtMethod.Size = New-Object System.Drawing.Size(445, 25)
        $form.Controls.Add($txtMethod)

        $lblParam = New-Object System.Windows.Forms.Label
        $lblParam.Text = 'Test parameter:'
        $lblParam.Location = New-Object System.Drawing.Point(15, 218)
        $lblParam.Size = New-Object System.Drawing.Size(450, 18)
        $form.Controls.Add($lblParam)

        $txtParam = New-Object System.Windows.Forms.TextBox
        $txtParam.Location = New-Object System.Drawing.Point(15, 238)
        $txtParam.Size = New-Object System.Drawing.Size(445, 25)
        $form.Controls.Add($txtParam)

        $btnOk = New-Object System.Windows.Forms.Button
        $btnOk.Text = 'Attach && print'
        $btnOk.Location = New-Object System.Drawing.Point(130, 300)
        $btnOk.Size = New-Object System.Drawing.Size(125, 30)
        $btnOk.Enabled = $false      # strict: at least a registration number is required
        $btnOk.Add_Click({
            if (-not $txtReg.Text.Trim()) { return }
            if (-not (Test-HubReachable)) { Show-ServerUnavailable; return }
            $json = (@{
                registration_number = $txtReg.Text.Trim()
                test_method         = $txtMethod.Text.Trim()
                test_parameter      = $txtParam.Text.Trim()
            } | ConvertTo-Json -Compress)
            Write-OutJson $json
            $script:answered = $true
            $form.Close()
        })
        $form.Controls.Add($btnOk)
        $form.AcceptButton = $btnOk
        $txtReg.Add_TextChanged({ $btnOk.Enabled = [bool]$txtReg.Text.Trim() })

        $btnCancel = New-Object System.Windows.Forms.Button
        $btnCancel.Text = 'Cancel'
        $btnCancel.Location = New-Object System.Drawing.Point(265, 300)
        $btnCancel.Size = New-Object System.Drawing.Size(90, 30)
        $btnCancel.Add_Click({ $form.Close() })   # cancel JSON written after ShowDialog
        $form.Controls.Add($btnCancel)

    } else {
        # ---- legacy printer: the original free-text prompt, unchanged ----
        $form.Size = New-Object System.Drawing.Size(470, 215)
        $lbl.Size = New-Object System.Drawing.Size(430, 40)
        $form.Controls.Add($lbl)

        $txt = New-Object System.Windows.Forms.TextBox
        $txt.Location = New-Object System.Drawing.Point(15, 58)
        $txt.Size = New-Object System.Drawing.Size(300, 25)
        $form.Controls.Add($txt)

        $btnGen = New-Object System.Windows.Forms.Button
        $btnGen.Text = 'New UUID'
        $btnGen.Location = New-Object System.Drawing.Point(325, 57)
        $btnGen.Size = New-Object System.Drawing.Size(115, 26)
        $btnGen.Add_Click({ $txt.Text = [guid]::NewGuid().ToString() })
        $form.Controls.Add($btnGen)

        $btnOk = New-Object System.Windows.Forms.Button
        $btnOk.Text = 'Attach && print'
        $btnOk.Location = New-Object System.Drawing.Point(120, 115)
        $btnOk.Size = New-Object System.Drawing.Size(125, 30)
        $btnOk.Add_Click({
            $val = $txt.Text.Trim()
            Write-OutJson (@{ id = $val } | ConvertTo-Json -Compress)
            $script:answered = $true
            $form.Close()
        })
        $form.Controls.Add($btnOk)
        $form.AcceptButton = $btnOk

        $btnCancel = New-Object System.Windows.Forms.Button
        $btnCancel.Text = 'Cancel'
        $btnCancel.Location = New-Object System.Drawing.Point(255, 115)
        $btnCancel.Size = New-Object System.Drawing.Size(90, 30)
        $btnCancel.Add_Click({ $form.Close() })   # cancel JSON written after ShowDialog
        $form.Controls.Add($btnCancel)
    }

    # ---- auto-close at -TimeoutSec (writes nothing: the job proceeds and the
    #      hub HOLDS it - an unattended print must not be silently destroyed) ----
    if ($TimeoutSec -gt 0) {
        $script:form = $form
        $script:baseTitle = $form.Text
        $script:remaining = [int]$TimeoutSec
        $script:timer = New-Object System.Windows.Forms.Timer
        $script:timer.Interval = 1000
        $script:timer.Add_Tick({
            $script:remaining--
            if ($script:remaining -le 0) {
                $script:timer.Stop()
                $script:timedOut = $true
                $script:form.Close()      # no OutFile written -> job proceeds, hub holds
            } elseif ($script:remaining -le 60) {
                $script:form.Text = '{0}  (closes in {1}s)' -f $script:baseTitle, $script:remaining
            }
        })
        $form.Add_FormClosed({ $script:timer.Stop(); $script:timer.Dispose() })
        $script:timer.Start()
    }

    [void]$form.ShowDialog()

    # Strict dialog: the user dismissed it without attaching (Cancel button, X,
    # Alt+F4). Tell upload.py explicitly so it DISCARDS the job - it must not be
    # uploaded, held, or printed. A timeout is NOT a cancel (nobody was there to
    # decide): it writes nothing and the job proceeds to be held at the hub.
    if (-not $script:answered -and -not $script:timedOut) {
        Write-OutJson '{"cancel":true}'
    }
} catch {
    # Never let a UI error matter (upload.py treats "no file" as "no number"), but
    # record it next to OutFile so the failure is diagnosable.
    try {
        [System.IO.File]::WriteAllText(
            "$OutFile.err",
            ("prompt-id.ps1 error: " + $_.Exception.Message + "`n" + $_.ScriptStackTrace),
            (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}
